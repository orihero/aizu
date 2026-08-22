"""Tests for logsetup — level resolution, secret redaction, the spawned-run
file-only guard, ANSI-free files, idempotency, and the namespaced get_logger."""
import logging

import pytest

from aizu.core import logsetup


@pytest.fixture(autouse=True)
def _clean_logging(monkeypatch):
    """Each test starts from a clean slate: no log env vars, no handlers."""
    for key in ("AIZU_RUN_ID", "AIZU_LOG_LEVEL", "AIZU_LOG_FILE",
                "AIZU_LOG_FILE_LEVEL", "AIZU_LOG_COLOR", "NO_COLOR",
                "OPENROUTER_API_KEY", "AIZU_SECRET_KEY", "TELEGRAM_API_HASH"):
        monkeypatch.delenv(key, raising=False)
    logsetup.reset_logging()
    yield
    logsetup.reset_logging()


def _handlers():
    return logging.getLogger(logsetup.ROOT_LOGGER_NAME).handlers


def _console_handlers():
    """Handlers that target the console (not a file)."""
    return [h for h in _handlers() if not isinstance(h, logging.FileHandler)]


# ---- level resolution ----

def test_default_console_level_is_info(tmp_path):
    logsetup.configure_logging(log_file=str(tmp_path / "a.log"), force=True)
    console = _console_handlers()
    assert console and console[0].level == logging.INFO


def test_env_sets_console_level(monkeypatch, tmp_path):
    monkeypatch.setenv("AIZU_LOG_LEVEL", "DEBUG")
    logsetup.configure_logging(log_file=str(tmp_path / "a.log"), force=True)
    assert _console_handlers()[0].level == logging.DEBUG


def test_explicit_level_beats_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIZU_LOG_LEVEL", "WARNING")
    logsetup.configure_logging(level="DEBUG", log_file=str(tmp_path / "a.log"), force=True)
    assert _console_handlers()[0].level == logging.DEBUG


# ---- redaction ----

def test_redacting_filter_scrubs_known_secrets(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-supersecretvalue")
    flt = logsetup.RedactingFilter()
    record = logging.LogRecord(
        "aizu.t", logging.INFO, __file__, 1,
        "key=sk-supersecretvalue Bearer abc.def.ghi rr_session=cookietok "
        'password: hunter2 blob=gAAAAABabc_123-XYZ',
        (), None)
    assert flt.filter(record) is True
    msg = record.getMessage()
    assert "sk-supersecretvalue" not in msg   # env literal
    assert "abc.def.ghi" not in msg           # bearer token
    assert "cookietok" not in msg             # session cookie
    assert "hunter2" not in msg               # password field
    assert "gAAAAABabc_123-XYZ" not in msg    # fernet blob
    assert "«redacted»" in msg


def test_redacting_filter_scrubs_credential_shaped_keywords():
    """SECURITY REVIEW hardening: the worker's fetched-on-demand platform_credentials
    (sidecar.py) crosses this box's own log stream on its way to/from the credential
    endpoint and the child spec file — the keyword list must catch every shape that
    dict's keys (and its own wire/spec-file field name) can appear under, including a
    Python dict repr's single quotes, not just JSON's double quotes."""
    flt = logsetup.RedactingFilter()
    record = logging.LogRecord(
        "aizu.t", logging.INFO, __file__, 1,
        '{"platformCredentials": {"api_key": "YT-LIVE", \'client_secret\': \'CS-1\', '
        "'session': 'TG-SESSION-STR', 'api_hash': 'AH-1', 'bot_token': 'BT-1'}}",
        (), None)
    flt.filter(record)
    msg = record.getMessage()
    for secret in ("YT-LIVE", "CS-1", "TG-SESSION-STR", "AH-1", "BT-1"):
        assert secret not in msg, msg
    assert "«redacted»" in msg


def test_redacting_filter_keeps_innocuous_text():
    flt = logsetup.RedactingFilter()
    record = logging.LogRecord("aizu.t", logging.INFO, __file__, 1,
                               "matches=3 spend=$0.0042 reel=r1", (), None)
    flt.filter(record)
    assert record.getMessage() == "matches=3 spend=$0.0042 reel=r1"


# ---- the spawned-run landmine guard ----

def test_spawned_mode_attaches_no_console_handler(monkeypatch, tmp_path):
    monkeypatch.setenv("AIZU_RUN_ID", "run123")
    logsetup.configure_logging(log_file=str(tmp_path / "main.log"), force=True)
    handlers = _handlers()
    assert handlers, "expected file handlers in spawned mode"
    # Every handler must be a file sink — no console/stream that could pollute the
    # stdout/stderr stream the panel parses for the run summary.
    assert all(isinstance(h, logging.FileHandler) for h in handlers)


def test_spawned_mode_adds_per_run_logfile(monkeypatch, tmp_path):
    monkeypatch.setenv("AIZU_RUN_ID", "abc999")
    logsetup.configure_logging(log_file=str(tmp_path / "main.log"), force=True)
    paths = [getattr(h, "baseFilename", "") for h in _handlers()]
    assert any(p.endswith("main.log") for p in paths)
    assert any(p.endswith("run-abc999.log") for p in paths)


def test_non_spawned_mode_has_a_console_handler(tmp_path):
    logsetup.configure_logging(log_file=str(tmp_path / "a.log"), force=True)
    assert _console_handlers(), "interactive runs must log to the console"


# ---- file sink ----

def test_file_output_is_ansi_free(tmp_path):
    path = tmp_path / "rr.log"
    logsetup.configure_logging(level="DEBUG", log_file=str(path),
                               color="always", force=True)
    logsetup.get_logger("aizu.t").info("url=https://x.io score=0.5 n=42")
    for handler in _handlers():
        handler.flush()
    data = path.read_bytes()
    assert b"\x1b" not in data                 # no ANSI escape codes ever
    assert b"url=https://x.io" in data


def test_file_logging_disabled_with_off(monkeypatch):
    monkeypatch.setenv("AIZU_LOG_FILE", "off")
    logsetup.configure_logging(force=True)
    assert not any(isinstance(h, logging.FileHandler) for h in _handlers())


# ---- frozen (PyInstaller app-bundle) log path ----

def test_default_log_dir_is_engine_logs_when_not_frozen():
    """A normal source checkout keeps logging to engine/logs."""
    assert logsetup._default_log_dir() == logsetup._engine_root() / "logs"


def test_frozen_never_logs_inside_the_package(monkeypatch):
    """Frozen + no state dir: logs go to a per-user dir, NEVER beside the (read-only,
    signed) package — writing there would break the bundle signature on first run."""
    monkeypatch.setattr(logsetup.sys, "frozen", True, raising=False)
    monkeypatch.delenv("AIZU_WORKER_STATE", raising=False)
    path = logsetup._resolve_log_path(None)
    assert path is not None
    assert logsetup._engine_root() not in path.parents  # not inside the bundle
    assert path == logsetup._user_log_dir() / "aizu.log"


def test_frozen_prefers_worker_state_dir(monkeypatch, tmp_path):
    """Frozen + a shell-provided writable state dir: co-locate logs under it."""
    monkeypatch.setattr(logsetup.sys, "frozen", True, raising=False)
    monkeypatch.setenv("AIZU_WORKER_STATE", str(tmp_path / "state"))
    assert logsetup._resolve_log_path(None) == tmp_path / "state" / "logs" / "aizu.log"


def test_explicit_log_file_still_wins_when_frozen(monkeypatch, tmp_path):
    """An explicit AIZU_LOG_FILE always overrides the frozen default."""
    monkeypatch.setattr(logsetup.sys, "frozen", True, raising=False)
    monkeypatch.setenv("AIZU_LOG_FILE", str(tmp_path / "custom.log"))
    assert logsetup._resolve_log_path(None) == tmp_path / "custom.log"


# ---- idempotency + namespacing ----

def test_configure_is_idempotent(tmp_path):
    path = str(tmp_path / "a.log")
    logsetup.configure_logging(log_file=path, force=True)
    n1 = len(_handlers())
    logsetup.configure_logging(log_file=path)   # no force → no-op, no duplicates
    assert len(_handlers()) == n1 == 2          # one file + one console


def test_get_logger_folds_main_into_namespace():
    assert logsetup.get_logger("__main__").name.startswith(logsetup.ROOT_LOGGER_NAME)
    assert logsetup.get_logger("aizu.session").name == "aizu.session"


def test_success_level_registered():
    assert logging.getLevelName(logsetup.SUCCESS_LEVEL) == "SUCCESS"
    assert hasattr(logsetup.get_logger("aizu.t"), "success")


def test_traceback_secrets_are_redacted(caplog):
    """exc_info tracebacks must be scrubbed too — a secret in an exception message must
    not reach a sink unredacted (review HIGH)."""
    import logging as _logging
    from aizu.core.logsetup import RedactingFilter
    rec = _logging.LogRecord("aizu.t", _logging.ERROR, __file__, 1,
                             "job crashed", (), None)
    try:
        raise RuntimeError("Bearer sk-supersecret-abc123 leaked in the traceback")
    except RuntimeError:
        import sys as _sys
        rec.exc_info = _sys.exc_info()
    RedactingFilter().filter(rec)
    assert rec.exc_text is not None
    assert "sk-supersecret-abc123" not in rec.exc_text
    assert "«redacted»" in rec.exc_text


# ---- console line cap (unbounded-log DoS backstop) ----

def test_line_cap_formatter_truncates_a_huge_line():
    """One absurdly long line is trimmed to _CONSOLE_LINE_MAX + a marker. The console
    sink renders per character while holding the GIL, so an unbounded line is a stall
    for every other thread — see server._LOG_PATH_MAX for the first line of defence."""
    fmt = logsetup.LineCapFormatter("%(message)s")
    rec = logging.LogRecord("aizu.t", logging.INFO, __file__, 1,
                            "A" * 64_000, (), None)
    out = fmt.format(rec)
    assert len(out) < logsetup._CONSOLE_LINE_MAX + 100
    assert out.startswith("A" * 100)
    assert out.endswith(f"[+{64_000 - logsetup._CONSOLE_LINE_MAX} chars truncated]")


def test_line_cap_formatter_leaves_normal_lines_untouched():
    fmt = logsetup.LineCapFormatter("%(message)s")
    rec = logging.LogRecord("aizu.t", logging.INFO, __file__, 1, "hello", (), None)
    assert fmt.format(rec) == "hello"


def test_line_cap_formatter_keeps_every_traceback_frame():
    """The cap is PER LINE, so a multi-line traceback keeps all its frames — only a
    single overlong line inside it is trimmed."""
    fmt = logsetup.LineCapFormatter("%(message)s")
    rec = logging.LogRecord("aizu.t", logging.ERROR, __file__, 1, "boom", (), None)
    rec.exc_text = "Traceback:\n  frame one\n  " + "B" * 64_000 + "\n  frame three"
    out = fmt.format(rec)
    lines = out.split("\n")
    assert lines[0] == "boom"
    assert "  frame one" in lines and "  frame three" in lines
    assert max(len(line) for line in lines) < logsetup._CONSOLE_LINE_MAX + 100


def test_console_handler_uses_the_line_cap_formatter(tmp_path):
    """Wiring check: the cap only helps if the configured console handler carries it."""
    logsetup.configure_logging(log_file=str(tmp_path / "a.log"), force=True)
    console = _console_handlers()
    assert console
    assert isinstance(console[0].formatter, logsetup.LineCapFormatter)
