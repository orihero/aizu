"""Central logging setup — colorful console + plain rotating file.

The engine logs *everything meaningful* through stdlib ``logging``: every module
does ``log = get_logger(__name__)`` and the entry points (``cli.main`` /
``server.serve``) call :func:`configure_logging` once. Console output is rendered
by Rich (color, emoji, aligned columns, pretty tracebacks); a rotating plain-text
file in ``engine/logs/`` keeps an ANSI-free archive.

NOT named ``logging.py`` on purpose — that would shadow the stdlib module.

Spawned-run safety (the one landmine): a panel-triggered run is a subprocess whose
stdout+stderr are redirected into one logfile that ``runner._extract_json`` parses
for the run's JSON summary. Colorful, brace-bearing log lines on that stream would
corrupt the parse. So when ``AIZU_RUN_ID`` is set we attach **file handlers
only** — never a console/stream handler — keeping stdout/stderr exactly as before.

Secrets never reach a sink: :class:`RedactingFilter` scrubs known credentials and
token shapes from every record before it is emitted.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

# Rich is a declared dependency, but keep the import soft (house style: httpx /
# telethon / dotenv are all soft-imported) so the engine still runs — with a plain
# console — if someone forgot ``pip install``.
try:  # pragma: no cover - exercised implicitly by whichever path is installed
    from rich.console import Console
    from rich.logging import RichHandler
    from rich.theme import Theme
    _HAVE_RICH = True
except Exception:  # pragma: no cover
    _HAVE_RICH = False

# ---- levels --------------------------------------------------------------

# A "success" tier between INFO(20) and WARNING(30) for green milestone lines
# (run completed, match found). Added to the stdlib so ``log.success(...)`` works.
SUCCESS_LEVEL = 25
logging.addLevelName(SUCCESS_LEVEL, "SUCCESS")


def _success(self: logging.Logger, message: str, *args, **kwargs) -> None:
    if self.isEnabledFor(SUCCESS_LEVEL):
        self._log(SUCCESS_LEVEL, message, args, **kwargs)


# Bind once; harmless if re-imported.
if not hasattr(logging.Logger, "success"):
    logging.Logger.success = _success  # type: ignore[attr-defined]

# Leading glyph per level — carried as text so it is safe in both the colored
# console and the plain file (it is unicode, never an ANSI escape).
_EMOJI = {
    logging.DEBUG: "·",
    logging.INFO: "▶",
    SUCCESS_LEVEL: "✓",
    logging.WARNING: "⚠",
    logging.ERROR: "✗",
    logging.CRITICAL: "🛑",
}

# Console palette (Rich theme keys are ``logging.level.<name>``).
_THEME = {
    "logging.level.debug": "dim cyan",
    "logging.level.info": "bold cyan",
    "logging.level.success": "bold green",
    "logging.level.warning": "bold yellow",
    "logging.level.error": "bold red",
    "logging.level.critical": "bold white on red",
}

ROOT_LOGGER_NAME = "aizu"
_DEFAULT_LEVEL = logging.INFO
_DEFAULT_FILE_LEVEL = logging.DEBUG
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 5

# Module-level guard so repeated calls are cheap no-ops unless force=True.
_configured = False


# ---- redaction -----------------------------------------------------------

# Token shapes to scrub regardless of value (covers headers, cookies, blobs).
_REDACT_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]+=*"),
    re.compile(r"(?i)(authorization\s*[:=]\s*)\S+"),
    re.compile(r"(rr_session=)[^;\s]+"),
    re.compile(r"(?i)(\"?(?:password|api[_-]?key|secret|token)\"?\s*[:=]\s*\"?)[^\s\",}]+"),
    re.compile(r"\bgAAAAA[A-Za-z0-9\-_=]+"),  # Fernet token
]
_REDACTED = "«redacted»"

# Env vars whose *literal value* must never appear in a log line.
_SECRET_ENV_VARS = ("OPENROUTER_API_KEY", "AIZU_SECRET_KEY", "TELEGRAM_API_HASH",
                    "POLAR_ACCESS_TOKEN", "POLAR_WEBHOOK_SECRET")


def _redact(text: str, literals: tuple[str, ...]) -> str:
    """Return ``text`` with known secret values and token shapes masked."""
    for value in literals:
        if value:
            text = text.replace(value, _REDACTED)
    for pat in _REDACT_PATTERNS:
        text = pat.sub(lambda m: (m.group(1) if m.lastindex else "") + _REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    """Scrub secrets from a record's rendered message before it reaches any sink.

    Renders ``msg % args`` once here (so substituted values are scrubbed too),
    masks it, and replaces ``record.msg`` with the clean string. Never raises —
    a redaction bug must not lose the log line."""

    def __init__(self) -> None:
        super().__init__()
        # Snapshot secret values present at configure time.
        self._literals = tuple(
            v for v in (os.environ.get(name, "") for name in _SECRET_ENV_VARS) if v
        )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
            cleaned = _redact(rendered, self._literals)
            if cleaned != rendered:
                record.msg = cleaned
                record.args = ()
            # A traceback (exc_info=True) is rendered by the Formatter OUTSIDE this filter
            # chain, so a secret in an exception message would otherwise reach the sink
            # unredacted. Pre-compute + scrub exc_text (and stack_info) here so the
            # Formatter emits our redacted version instead of recomputing a raw one.
            if record.exc_info:
                if not record.exc_text:
                    record.exc_text = logging.Formatter().formatException(record.exc_info)
                record.exc_text = _redact(record.exc_text, self._literals)
            if record.stack_info:
                record.stack_info = _redact(record.stack_info, self._literals)
        except Exception:  # noqa: BLE001 - never drop a line over redaction
            pass
        return True


class _EmojiFilter(logging.Filter):
    """Attach ``record.emoji`` so formatters can prefix a level glyph."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.emoji = _EMOJI.get(record.levelno, "•")
        return True


# ---- configuration helpers ----------------------------------------------

def _coerce_level(level: Optional[object], default: int) -> int:
    """Map a level name/number/None to a logging int, defaulting tolerantly."""
    if level is None:
        return default
    if isinstance(level, int):
        return level
    name = str(level).strip().upper()
    resolved = logging.getLevelName(name)  # name → int, or the string back if unknown
    return resolved if isinstance(resolved, int) else default


def _want_color(color: Optional[str]) -> Optional[bool]:
    """Resolve the color choice to True/False/None(=auto-detect by Rich)."""
    choice = (color or os.environ.get("AIZU_LOG_COLOR") or "auto").strip().lower()
    if os.environ.get("NO_COLOR") is not None:  # https://no-color.org
        return False
    if choice == "always":
        return True
    if choice == "never":
        return False
    return None  # auto: let the Console decide from the stream's TTY-ness


def _engine_root() -> Path:
    """The engine dir (parent of the ``aizu`` package) — where ``logs/`` lives."""
    return Path(__file__).resolve().parent.parent


def _user_log_dir() -> Path:
    """A per-user, writable logs dir, used when the package itself lives somewhere
    unwritable (a frozen, code-signed app bundle). Platform-conventional so the files
    are where an operator expects (Console.app on macOS, %LOCALAPPDATA% on Windows)."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "AIZU Worker"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "AIZU Worker" / "logs"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "aizu-worker" / "logs"


def _default_log_dir() -> Path:
    """Where ``aizu.log`` lives when no explicit path is given.

    A frozen (PyInstaller) build runs from inside a READ-ONLY, code-signed app
    bundle — the ``aizu`` package sits at ``.../Resources/sidecar/.../_internal/
    aizu``. Defaulting to ``_engine_root()/logs`` there would write *inside* the
    bundle, which breaks the code signature on first run (and fails outright on a
    Gatekeeper-enforced install). So when frozen, log to the worker's writable state
    dir if the shell provided one, else a per-user logs dir. A normal source checkout
    keeps the familiar ``engine/logs``."""
    if getattr(sys, "frozen", False):
        state = os.environ.get("AIZU_WORKER_STATE")
        if state and state.strip():
            return Path(state).expanduser() / "logs"
        return _user_log_dir()
    return _engine_root() / "logs"


def _resolve_log_path(log_file: Optional[str]) -> Optional[Path]:
    """The base logfile path, or None when file logging is disabled."""
    value = log_file if log_file is not None else os.environ.get("AIZU_LOG_FILE")
    if value and value.strip().lower() == "off":
        return None
    if value:
        return Path(value).expanduser()
    return _default_log_dir() / "aizu.log"


def _file_formatter() -> logging.Formatter:
    """Plain, grep-friendly, ANSI-free file format."""
    return logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _make_file_handler(path: Path, level: int) -> RotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        str(path), maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    handler.setLevel(level)
    handler.setFormatter(_file_formatter())
    return handler


def _make_console_handler(level: int, color: Optional[str]) -> logging.Handler:
    """A Rich console handler (color/emoji/tracebacks), or a plain stderr fallback."""
    if not _HAVE_RICH:
        handler = logging.StreamHandler(sys.stderr)
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter("%(emoji)s %(levelname)-7s %(name)s: %(message)s"))
        return handler
    force = _want_color(color)
    console = Console(
        stderr=True,
        theme=Theme(_THEME),
        force_terminal=True if force is True else None,
        no_color=True if force is False else False,
    )
    handler = RichHandler(
        console=console,
        level=level,
        show_time=True,
        show_level=True,
        show_path=False,
        rich_tracebacks=True,
        markup=False,                 # never parse user content as markup (injection-safe)
        log_time_format="%H:%M:%S",
    )
    # Emoji + dimmed logger name; message content is left to Rich's highlighter,
    # which auto-colors numbers/urls/quoted strings for "informative" output.
    handler.setFormatter(logging.Formatter("%(emoji)s %(name)s — %(message)s"))
    return handler


def configure_logging(*, level: Optional[object] = None,
                      log_file: Optional[str] = None,
                      color: Optional[str] = None,
                      file_level: Optional[object] = None,
                      force: bool = False) -> None:
    """Configure the ``aizu`` logger tree. Idempotent unless ``force=True``.

    - Console (Rich, colorful) → stderr, at ``level`` — UNLESS this process is a
      spawned panel run (``AIZU_RUN_ID`` set), where it is omitted so the
      stdout/stderr stream the panel parses stays pristine.
    - Rotating file → ``engine/logs/aizu.log`` at ``file_level`` (default
      DEBUG so the archive is complete), unless ``AIZU_LOG_FILE=off``.
    - Per-run file → ``engine/logs/run-<RUN_ID>.log`` when spawned.
    Every handler carries the redaction + emoji filters.
    """
    global _configured
    if _configured and not force:
        return

    console_level = _coerce_level(level, _coerce_level(
        os.environ.get("AIZU_LOG_LEVEL"), _DEFAULT_LEVEL))
    file_lvl = _coerce_level(file_level, _coerce_level(
        os.environ.get("AIZU_LOG_FILE_LEVEL"), _DEFAULT_FILE_LEVEL))
    run_id = os.environ.get("AIZU_RUN_ID")
    is_spawned = bool(run_id)

    redactor = RedactingFilter()
    emoji = _EmojiFilter()
    handlers: list[logging.Handler] = []

    log_path = _resolve_log_path(log_file)
    if log_path is not None:
        try:
            handlers.append(_make_file_handler(log_path, file_lvl))
            if is_spawned:
                per_run = log_path.parent / f"run-{run_id}.log"
                handlers.append(_make_file_handler(per_run, file_lvl))
        except OSError:
            # A read-only FS must not crash startup; console still works.
            pass

    if not is_spawned:
        handlers.append(_make_console_handler(console_level, color))

    for handler in handlers:
        handler.addFilter(redactor)
        handler.addFilter(emoji)

    root = logging.getLogger(ROOT_LOGGER_NAME)
    for old in list(root.handlers):       # replace, never duplicate
        root.removeHandler(old)
    for handler in handlers:
        root.addHandler(handler)
    # Root must pass the lowest level any handler wants; handlers filter further.
    root.setLevel(min([h.level for h in handlers], default=console_level))
    root.propagate = False                # don't double-emit through the python root

    logging.captureWarnings(True)
    _configured = True


def run_log_path(run_id: str, *, log_file: Optional[str] = None) -> Optional[Path]:
    """The per-run log file path ``configure_logging`` writes when spawned under
    ``AIZU_RUN_ID`` — ``<logdir>/run-<run_id>.log``. Single source of truth so the
    worker's control surface (which points the desktop shell at a job's live log) and
    the logging setup itself never derive the path two different ways. Returns ``None``
    when file logging is disabled (``AIZU_LOG_FILE=off``)."""
    base = _resolve_log_path(log_file)
    if base is None:
        return None
    return base.parent / f"run-{run_id}.log"


def get_logger(name: str) -> logging.Logger:
    """A namespaced logger. Pass ``__name__`` (already ``aizu.*``).

    An entry point run as ``python -m aizu.<mod>`` sees ``__name__ ==
    "__main__"``, which would fall outside the configured ``aizu`` tree and
    never reach the handlers — fold that back under the namespace."""
    if name == "__main__" or not name.startswith(ROOT_LOGGER_NAME):
        name = f"{ROOT_LOGGER_NAME}.{name}" if name != "__main__" else ROOT_LOGGER_NAME
    return logging.getLogger(name)


def reset_logging() -> None:
    """Tear down handlers and clear the guard — for tests that reconfigure."""
    global _configured
    root = logging.getLogger(ROOT_LOGGER_NAME)
    for old in list(root.handlers):
        try:
            old.close()
        except Exception:  # noqa: BLE001
            pass
        root.removeHandler(old)
    _configured = False
