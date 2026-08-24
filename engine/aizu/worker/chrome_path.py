"""Resolve — and, on demand, download — Playwright's Chrome for Testing, for a box that
has no interpreter.

The desktop shell picks the browser it launches by asking a Python that owns Playwright.
In the DEV tree that Python is ``engine/.venv/bin/python``; in a PACKAGED install there is
none — a Finder/LaunchAgent launch inherits no shell profile, so ``CHROME_BIN`` is unset,
and there is no venv relative to the app's cwd. The shell therefore falls through to system
Chrome, and THAT is the whole defect: Chrome 149+ answers ``/json/version`` with a 200 while
REFUSING ``connect_over_cdp`` (``scripts/warm_chrome.sh``). So the shell launches a browser
the engine can never attach to, its own probe returns Unknown (ledger A4 — no interpreter to
probe with, so it cannot say Rejected), and the operator is left on a green launch with a
red ``cdp_attachable`` at step 5 of the setup wizard.

The frozen sidecar binary DOES carry Playwright, so this module makes it answerable::

    <sidecar-binary> -m aizu.worker.chrome_path              # where is Chrome for Testing?
    <sidecar-binary> -m aizu.worker.chrome_path --install    # ...and put it there if it isn't

That is the same trick ledger A2 used to route the supervised job child, and the in-process
answer ledger A4 asked for. The argv branch that makes it reachable from the freeze lives in
``desktop/pyinstaller/run_sidecar.py`` and is NOT optional: PyInstaller's bootloader ignores
``-m``, so an argv that shim does not recognise boots a SECOND sidecar instead of erroring.

WHY ``--install`` EXISTS. The app ships at ~30 MB and deliberately does NOT bundle a browser:
Chrome for Testing is a 356 MB download, and on macOS the .app lives in /Applications and is
REPLACED wholesale on every update, so anything inside the bundle would be re-downloaded
forever. Instead the browser is fetched once into the per-user cache that
``run_sidecar.py::_pin_playwright_browsers_path`` pins (the same
``~/Library/Caches/ms-playwright`` a dev tree's ``playwright install chromium`` and
``scripts/warm_chrome.sh`` already use), where it survives app updates. The download is
driven by the Node driver ALREADY inside the bundle — never by an interpreter or a
``pip install playwright``, neither of which exists on a packaged box.

Two rules the callers depend on, both load-bearing:

* STDOUT carries the path and NOTHING else; every diagnostic goes to stderr. Merged-stream
  parsing is not safe here — starting Playwright just to read this one string routinely
  leaves an unrelated ``TargetClosedError`` traceback (and a "Task was destroyed but it is
  pending!") on stderr as the driver unwinds at interpreter shutdown. Stderr noise is NORMAL
  and must never be read as failure by any caller; the exit code is the only verdict. Note
  that ``--install`` has to WORK for this rule, not just respect it: the Playwright driver
  writes its download progress AND its failures to its own STDOUT (verified — a failed
  install put "Failed to install browsers" on stdout and nothing on stderr), so the whole
  driver stream is captured and re-emitted on ours.
* The resolved path is STAT-ED before it is printed. ``pw.chromium.executable_path`` is a
  plain dict read off the driver's initializer, not a filesystem check — on a box that never
  ran ``playwright install chromium`` it hands back a well-formed path to a file that does
  not exist, and Playwright only notices at launch time. Printing that phantom would give the
  shell a dead binary, i.e. the same stuck-at-step-5 dead end by a new route. Only an
  existing file is ever printed; anything else is a non-zero exit with one remedy line. That
  is also why ``--install`` finishes by RE-RESOLVING instead of trusting the driver's own
  "downloaded to ..." line: one owner for the fact, and it is stat-ed like every other answer.

Import must stay cheap (the shell runs this on its boot path, inside a 30s Chrome grace):
Playwright is imported lazily inside the resolver, exactly as ``chrome_manager`` already does
it, and nothing here reaches for the bridge/RBAC/billing side of the package.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from typing import Callable, Optional

# The one existing resolver, reused rather than re-implemented: Playwright's own idea of
# where Chrome for Testing lives is a single fact and must have a single owner (it is also
# the exact value `resolve_chrome_binary`'s Chrome-for-Testing tier consumes).
from .chrome_manager import _default_playwright_executable_path

# Exit codes: 0 = a real, existing Chrome-for-Testing binary is on stdout; 1 = stdout is
# empty and one remedy line is on stderr (the caller falls through to its next tier).
_EXIT_OK = 0
_EXIT_UNRESOLVED = 1

# The download, as the operator is told about it. `chromium` in this Playwright IS Chrome for
# Testing (the driver prints "Chrome for Testing 151.x (playwright chromium v1234)"), and it
# is the exact target `pw.chromium.executable_path` then points at. `--no-shell` skips the
# headless shell: measured at another 196 MB on top of the 356 MB the wizard advertises, and
# this repo never launches it — it attaches over CDP to a HEADED Chrome for Testing.
_INSTALL_TARGET = ("install", "chromium", "--no-shell")
_DOWNLOAD_SIZE_HUMAN = "~356 MB"

# Remedy lines: ONE line each, actionable, and safe to surface verbatim — a filesystem path
# is a name, never a secret VALUE (the rule preflight's `CheckResult.detail` already follows).
_REMEDY_NO_PLAYWRIGHT = (
    "aizu.worker.chrome_path: Playwright is unavailable in this process, so Chrome for "
    "Testing cannot be located — reinstall the engine (playwright is a hard dependency, "
    "not an extra), or point CHROME_BIN at a Chrome-for-Testing binary."
)
# NOT "run `playwright install chromium`": that is unreachable on a packaged box, which has
# no interpreter and no pip. Name the in-app action the operator can actually take.
_REMEDY_NOT_INSTALLED = (
    "aizu.worker.chrome_path: Chrome for Testing is not installed on this box — nothing at "
    "{path}. Open the setup wizard's Chrome step and use Download browser ({size}), or "
    "point CHROME_BIN at a Chrome-for-Testing binary (system Chrome 149+ "
    "answers /json/version but refuses connect_over_cdp, so it is not a substitute)."
)
# A browser whose download was interrupted leaves a COMPLETE-LOOKING tree behind: the
# executable is on disk and stats fine, but Playwright knows better and re-downloads it
# ("Removing unused browser at ..."). Observed for real — a killed install left 322 MB and a
# 52 KB launcher that an existence check happily accepts. Launching that half-extracted
# browser fails later, at the CDP port, under a remedy about port conflicts that names
# nothing an operator could act on. The marker Playwright itself writes is the honest gate.
_INSTALLATION_COMPLETE_MARKER = "INSTALLATION_COMPLETE"
# Playwright's cache-entry names, as written under PLAYWRIGHT_BROWSERS_PATH. Only these
# count as a revision directory — see `_looks_like_revision_dir` for why a looser rule
# manufactured a false failure.
_PLAYWRIGHT_CACHE_NAMES = frozenset({
    "chromium", "chromium_headless_shell", "chrome", "msedge",
    "firefox", "webkit", "ffmpeg",
})
# How far up from the executable the revision dir can sit: mac is the deepest at
# <rev>/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/<exe> (5 hops).
_MARKER_SEARCH_DEPTH = 6
_REMEDY_HALF_INSTALLED = (
    "aizu.worker.chrome_path: Chrome for Testing at {path} is only half-installed — its "
    "download was interrupted. Open the setup wizard's Chrome step and use Download browser "
    "({size}) to fetch it again; the incomplete copy is replaced automatically."
)
_REMEDY_NO_DRIVER = (
    "aizu.worker.chrome_path: Playwright's bundled browser driver is unavailable in this "
    "process, so Chrome for Testing cannot be downloaded — reinstall the app (the driver "
    "ships inside it), or point CHROME_BIN at a Chrome-for-Testing binary."
)
_REMEDY_DOWNLOAD_FAILED = (
    "aizu.worker.chrome_path: downloading Chrome for Testing failed (the Playwright driver "
    "exited {code}) — the lines above say why; the usual cause is no network/proxy route to "
    "cdn.playwright.dev, or no room for {size} in {cache}. Fix that and use Download browser "
    "again."
)
_REMEDY_DOWNLOAD_UNVERIFIED = (
    "aizu.worker.chrome_path: the Playwright driver reported a successful download into "
    "{cache} but Chrome for Testing still does not resolve — the download and this process "
    "disagree about PLAYWRIGHT_BROWSERS_PATH (pinned in "
    "desktop/pyinstaller/run_sidecar.py); unset or align it, or point "
    "CHROME_BIN at a Chrome-for-Testing binary."
)

# The driver dims its "Downloading X from <url>" line with an SGR pair. The wizard renders
# these lines as TEXT, not in a terminal, so the escapes would show up as literal `[2m`.
_ANSI_CSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _browsers_cache_description() -> str:
    """What the operator should look at for a disk-space complaint. The pin is in the frozen
    shim, so in the freeze this is always set; in a dev tree it is normally not."""
    return os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "Playwright's browser cache"


def _default_driver_invocation() -> tuple[list, dict]:
    """``(argv, env)`` for the BUNDLED Playwright Node driver, asked of Playwright itself.

    ``compute_driver_executable`` resolves ``<playwright pkg>/driver/{node,package/cli.js}``,
    which under PyInstaller is ``_internal/playwright/driver/...`` — verified live in the
    freeze (the driver it names is the same one the resolver above already spawns). Going
    through Playwright rather than re-deriving that layout keeps one owner for it, and
    ``get_driver_env`` is ``os.environ.copy()`` plus PW_LANG_* — it does NOT apply the frozen
    ``PLAYWRIGHT_BROWSERS_PATH="0"`` forcing that ``_transport`` does, so the shim's pin is
    what the driver sees."""
    from playwright._impl._driver import compute_driver_executable, get_driver_env
    node, cli = compute_driver_executable()
    return [node, cli, *_INSTALL_TARGET], get_driver_env()


def _default_progress(line: str) -> None:
    """One driver line -> one stderr line. Flushed per line because the Tauri command turns
    each of these into a `chrome-install-progress` event, and a download that buffers for
    five minutes is indistinguishable to the operator from one that hung."""
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


def resolve(*,
            executable_path: Callable[[], Optional[str]] = _default_playwright_executable_path,
            path_exists: Callable[[str], bool] = os.path.exists,
            ) -> tuple[Optional[str], Optional[str]]:
    """Resolve Chrome for Testing, or say why not: ``(path, None)`` on success, ``(None,
    remedy)`` otherwise. Never raises, and never returns a path that does not exist.

    Both effects are injected with real defaults, so the whole module is unit-testable with
    NO Playwright and NO browser — the same seam ``chrome_manager`` uses throughout."""
    cft = executable_path()
    if not cft:
        # `_default_playwright_executable_path` swallows everything and returns None, so
        # this covers both "playwright not importable" and "the driver refused to start".
        return None, _REMEDY_NO_PLAYWRIGHT
    if not path_exists(cft):
        return None, _REMEDY_NOT_INSTALLED.format(path=cft, size=_DOWNLOAD_SIZE_HUMAN)
    if _install_is_incomplete(cft, path_exists):
        return None, _REMEDY_HALF_INSTALLED.format(path=cft, size=_DOWNLOAD_SIZE_HUMAN)
    return cft, None


def _install_is_incomplete(executable: str,
                           path_exists: Callable[[str], bool]) -> bool:
    """True only when we can SEE a Playwright revision directory above ``executable`` and it
    is missing its ``INSTALLATION_COMPLETE`` marker.

    Deliberately asymmetric: an unrecognised layout (a CHROME_BIN a human pointed at, a
    future Playwright that moves the marker) returns False and the browser is accepted. A
    false red on a box whose Chrome is fine is the worst outcome this module has — the same
    rule the preflight's tri-state checks follow. We only reject what we can positively
    prove is half-written."""
    parent = os.path.dirname(executable)
    for _ in range(_MARKER_SEARCH_DEPTH):
        if not parent or parent == os.path.dirname(parent):
            break
        # A revision dir is the one Playwright stamps: `<cache>/chromium-1234/`.
        if path_exists(os.path.join(parent, _INSTALLATION_COMPLETE_MARKER)):
            return False
        if _looks_like_revision_dir(parent):
            # Found the revision dir and the marker was NOT there — genuinely incomplete.
            return True
        parent = os.path.dirname(parent)
    return False


def _looks_like_revision_dir(path: str) -> bool:
    """`chromium-1234`, `chromium_headless_shell-1234`, `ffmpeg-1011` — Playwright's
    `<name>-<revision>` cache entries, which is the level the marker is written at.

    The browser name is matched against a KNOWN list rather than "any word before a dash".
    The loose version matched `pytest-188` in a tmpdir path and declared a perfectly good
    browser half-installed — the false red this function's whole asymmetry exists to avoid,
    reached by an ancestor directory that had nothing to do with Playwright. A name we do
    not know simply falls through and the browser is accepted."""
    stem, sep, revision = os.path.basename(path).rpartition("-")
    return bool(sep) and revision.isdigit() and stem in _PLAYWRIGHT_CACHE_NAMES


def install(*,
            driver_invocation: Callable[[], tuple] = _default_driver_invocation,
            spawn: Callable[..., object] = subprocess.Popen,
            progress: Callable[[str], None] = _default_progress,
            resolve_installed: Optional[Callable[[], tuple]] = None,
            ) -> tuple[Optional[str], Optional[str]]:
    """Download Chrome for Testing with the bundled driver, then resolve it. Same
    ``(path, remedy)`` shape as :func:`resolve`, so ``--install`` and a plain query are
    interchangeable to a caller. Never raises.

    Deliberately UNBOUNDED in time: 356 MB over a bad hotel link is minutes, and there is no
    honest Python-side timeout for it. The Tauri command that spawns this owns the deadline
    (>= 15 min) and kills us; that is the one place a number belongs.

    Idempotent, because the wizard's button is: on a box that already has the browser the
    driver exits 0 in ~0.1s having printed nothing at all, so the framing lines below are
    also what tells the operator that anything happened."""
    if resolve_installed is None:
        # Read the module global at CALL time, so a test (and `main`) can stub `resolve`.
        resolve_installed = resolve
    try:
        argv, env = driver_invocation()
    except Exception:  # noqa: BLE001 — a missing/renamed driver must be a remedy, not a crash
        return None, _REMEDY_NO_DRIVER

    cache = _browsers_cache_description()
    progress(f"Downloading Chrome for Testing ({_DOWNLOAD_SIZE_HUMAN}) into {cache}...")
    try:
        # stderr folded into stdout: the driver puts BOTH progress and errors on its stdout,
        # and every byte of it belongs on OUR stderr (stdout is reserved for the path). An
        # explicit utf-8/replace rather than a bare `text=True`, because the progress bar is
        # made of U+2588 blocks and a box whose locale decodes as ASCII would otherwise die
        # on a UnicodeDecodeError halfway through a 356 MB download — a mangled bar is
        # survivable, a lost download is not.
        proc = spawn(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                     encoding="utf-8", errors="replace", bufsize=1, env=env)
    except Exception as exc:  # noqa: BLE001
        progress(f"{type(exc).__name__}: {exc}")
        return None, _REMEDY_NO_DRIVER

    try:
        for line in proc.stdout:
            progress(_ANSI_CSI.sub("", line).rstrip("\r\n"))
        code = proc.wait()
    except Exception as exc:  # noqa: BLE001
        # One remedy line, never a traceback out of the packaged app's Download button.
        # Killing the process tree belongs to the Tauri command that owns the deadline.
        progress(f"{type(exc).__name__}: {exc}")
        code = "abnormally"     # not 0, and reads correctly in "...driver exited {code}"
    if code != 0:
        return None, _REMEDY_DOWNLOAD_FAILED.format(
            code=code, size=_DOWNLOAD_SIZE_HUMAN, cache=cache)

    path, remedy = resolve_installed()
    if path is None:
        # The driver said it succeeded, so "not installed" is the WRONG story to tell here —
        # what actually differs is which cache each side looked at.
        return None, _REMEDY_DOWNLOAD_UNVERIFIED.format(cache=cache)
    progress(f"Chrome for Testing is ready at {path}")
    return path, remedy


def main(argv: Optional[list] = None) -> int:
    """Print the Chrome-for-Testing path to stdout and return 0, or print one remedy line
    to stderr and return non-zero. Callers accept the answer only on exit code 0 with a
    non-empty stdout; they must ignore stderr entirely (see the module docstring)."""
    parser = argparse.ArgumentParser(
        prog="aizu.worker.chrome_path",
        description="Print Playwright's Chrome-for-Testing executable path.")
    # Parsing means a typo'd flag from a future caller exits 2 with a usage message instead
    # of being silently ignored and answered with a path.
    parser.add_argument(
        "--install", action="store_true",
        help=f"download Chrome for Testing ({_DOWNLOAD_SIZE_HUMAN}) with the bundled "
             "Playwright driver first; progress goes to stderr")
    args = parser.parse_args(argv)

    path, remedy = install() if args.install else resolve()
    if path is None:
        print(remedy, file=sys.stderr)
        return _EXIT_UNRESOLVED
    # Write AND flush explicitly: Playwright's driver teardown can unwind noisily at
    # interpreter shutdown, and the caller's only payload must already be out of the pipe
    # by the time that starts.
    sys.stdout.write(path + "\n")
    sys.stdout.flush()
    return _EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
