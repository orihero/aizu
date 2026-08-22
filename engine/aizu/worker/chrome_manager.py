"""Managed Chrome lifecycle for the Phase-6 desktop shell (BUILD-PLAN Phase 6, risk 2).

The desktop app starts a warmed Chrome on launch; the engine attaches over CDP; on app
exit Chrome is killed; on a sidecar RESTART it must RECONNECT to the existing Chrome and
NEVER spawn a second (a relaunch would blow the warmed login state). :class:`ChromeManager`
owns exactly that lifecycle for ONE Chrome process per (account/box) profile.

Detection is a REAL CDP attach, not a bare HTTP 200 — ``scripts/warm_chrome.sh`` proves a
stale/degraded Chrome answers ``/json/version`` while REJECTING ``connect_over_cdp``. So a
cheap HTTP probe is only a pre-check to skip the heavier Playwright probe when nothing is
listening; the attach probe is the source of truth. On a listening-but-unattachable Chrome
we FAIL LOUD (:class:`ChromeUnhealthyError`) rather than kill a process we did not launch —
this manager never touches a Chrome it only attached to (mirrors ``core/cdp.py``'s ``close()``
that deliberately never calls ``browser.close()``).

Every side effect (process launch, CDP probe, HTTP probe, sleep, OS name, focus) is
injected with a real default, so the whole module is unit-testable with NO real Chrome.

Port note (RESOLVED 2026-08, ledger F10): **9333 is canonical**, and 9222 is retired as a
default but stays a first-class detected sibling forever. Every live run in this repo, the
``scripts/warm_chrome.sh`` runbook, ``engines.md §9`` and the desktop shell already used
9333; 9222 survived only as a Python literal, which is exactly how a box ended up running
Chrome on 9333 while its sidecar probed a dead 9222 and told the operator to "start Chrome"
on a machine where Chrome was already running.

This module still takes ``cdp_url`` from config with NO internal default — the wiring layer
sets it. What CHANGED is that the wiring layer no longer has to be right by luck:
``worker/preflight.py::resolve_cdp_url`` probes the configured port, then the one named
sibling, and ADOPTS a live sibling with a logged warning when the operator never pinned
``AIZU_CDP_URL`` (an explicit pin is reported as a named fatal instead, never silently
overridden). See ``memory/engine-live-run.md``.

Brand note (2026-08, proven live on a CLONE of a warmed profile): a Chrome profile belongs
to exactly ONE browser BRAND, and pointing the other brand at it DESTROYS its logins.
Chrome for Testing reads the macOS Keychain item ``Chromium Safe Storage``; system Google
Chrome writes ``Chrome Safe Storage``. Wrong key ⇒ cookie decryption fails ⇒ Chrome DELETES
the rows rather than quarantining them: 18 cookies → 0, live Instagram ``sessionid``
included, unrecoverable by any browser afterwards. It is not a version problem (the same
system Chrome run with ``--use-mock-keychain`` lost everything identically) and macOS has no
move-aside safety net (Chrome's DowngradeManager is ``#if BUILDFLAG(IS_WIN)``).

This matters HERE because ``resolve_chrome_binary`` PREFERS Chrome for Testing, while every
profile warmed by hand on this repo's boxes was warmed by system Chrome — so the helpful
default is exactly the one that wipes the operator's sessions.

The fix is the DIRECTORY, not a guard. ``AIZU_CHROME_PROFILE`` names a BASE, and the profile
actually opened is ``<base>/<brand>`` (:func:`profile_dir_for`, keyed off
:func:`brand_of_binary`). Two brands can then never open one directory, by construction:
there is nothing to mark, nothing to police, no refusal to survive and no question anyone
can answer wrong. Three earlier rounds tried to make two brands share one directory safely —
a ``.aizu-browser-brand`` marker, a decision table, a refusal, an operator declaration — and
each round fixed the last one's hole and opened a new one, the last of them shipping a
declaration button that ``resolve_chrome_binary`` never read. The path IS the ownership
record. ``desktop/src-tauri/src/chrome_manager.rs`` and ``scripts/warm_chrome.sh`` derive it
with the byte-identical contract, so all three components land in the same directory.

A base directory that itself holds a ``Default/`` is a profile from BEFORE that change,
warmed by an unknown brand. It is never opened, never moved, never repaired and never
deleted — we cannot know which browser owns it and guessing costs the operator every session
in it. :func:`legacy_profile_note` is the one thing that happens to it: an informational
paragraph naming both candidate destinations, logged at launch and published as the
warn-severity ``preflight.check_chrome_profile`` row so it reaches a box nobody can SSH into
(F12). Warn and never fatal: the directory is inert, the box runs every job perfectly
without it, and the only reason the row exists at all is that a box upgraded across this
change silently stops using its warmed logins — which reads as "not signed in" with no cause
attached unless we say this.
"""
from __future__ import annotations

import os
import platform
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from ..core.logsetup import get_logger

log = get_logger("aizu.worker.chrome_manager")

_LAUNCH_TIMEOUT_SEC = 15.0
_LAUNCH_POLL_INTERVAL_SEC = 0.5
_STOP_GRACE_SEC = 5.0
_HTTP_PROBE_TIMEOUT_SEC = 1.0
_CDP_PROBE_TIMEOUT_SEC = 5.0
# Fixed launch flags (never magic strings scattered across call sites).
_NO_FIRST_RUN = "--no-first-run"
_NO_DEFAULT_BROWSER_CHECK = "--no-default-browser-check"

# The brand tokens. SHARED CONTRACT with desktop/src-tauri/src/chrome_manager.rs and
# scripts/warm_chrome.sh — these two words are DIRECTORY NAMES now, so a drift in spelling
# does not misreport anything, it silently sends one component to a different profile than
# the other two and the operator's logins appear to vanish.
BRAND_CHROME_FOR_TESTING = "chrome-for-testing"
BRAND_CHROME = "chrome"
# Debian/Ubuntu's `chromium` is NOT Google Chrome: it is a separate build with its own
# libsecret/keyring entry, so sharing one directory with system Chrome destroys cookies the
# same way Chrome-for-Testing does. It only reaches this file on Linux, where
# `_SYSTEM_CHROME_PATHS` lists /usr/bin/chromium and /usr/bin/chromium-browser right next to
# google-chrome, and a two-token rule quietly filed both under "chrome".
BRAND_CHROMIUM = "chromium"
# The distro chromium executable NAMES (matched on the file name, not the path: the path can
# be anything, and rule 2's Playwright-cache segment has already claimed CfT by this point).
_DISTRO_CHROMIUM_NAMES = ("chromium", "chromium-browser")
_CFT_BINARY_SIGNATURE = "chrome for testing"
_CFT_PLAYWRIGHT_CACHE_SEGMENT = re.compile(r"^chromium(_headless_shell)?-[0-9]+$")
# Chrome writes its first profile into `<user-data-dir>/Default`, so a `Default` sitting
# DIRECTLY in the base (rather than under a `<base>/<brand>` subdirectory) is the signature
# of a profile warmed before profiles were split by brand. Deliberately a directory STAT and
# nothing more: reading, copying or opening the cookie DB to decide is exactly the kind of
# touch that can cost the operator the sessions we are refusing to gamble with.
LEGACY_PROFILE_DIR_NAME = "Default"

# Per-OS system-Chrome fallback paths (last resort — Playwright's Chrome-for-Testing is
# preferred because it is protocol-matched; system Chrome 149+ has broken connect_over_cdp).
_SYSTEM_CHROME_PATHS = {
    "Darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
    "Windows": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
    "Linux": ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
              "/usr/bin/chromium", "/usr/bin/chromium-browser"],
}


class ChromeError(RuntimeError):
    """Base for Chrome lifecycle failures."""


class ChromeLaunchError(ChromeError):
    """Chrome could not be launched or did not become CDP-attachable in time."""


class ChromeUnhealthyError(ChromeError):
    """A Chrome is listening on the port but rejects CDP — we will NOT kill someone
    else's process, so the operator must resolve it (quit the stale Chrome)."""


class ChromeBinaryNotFoundError(ChromeError):
    """No Chrome binary could be resolved on this box."""


@dataclass(frozen=True)
class ChromeStatus:
    """Immutable outcome of ``ensure_running``."""
    state: str          # 'attached_existing' | 'launched'
    launched_by_us: bool
    cdp_url: str


@dataclass(frozen=True)
class ChromeManagerConfig:
    """One frozen config per (account/box) Chrome. ``cdp_url`` is caller-supplied (no
    hardcoded 9222/9333). ``profile_base_dir`` MUST be a dedicated non-default path, and is
    a BASE, not the profile: the directory Chrome is actually pointed at is
    ``profile_base_dir/<brand>`` (:func:`profile_dir_for`), derived at launch from the
    binary we resolved."""
    cdp_url: str
    profile_base_dir: Path
    chrome_binary: Optional[str] = None
    extra_flags: tuple = ()
    launch_timeout_sec: float = _LAUNCH_TIMEOUT_SEC
    launch_poll_interval_sec: float = _LAUNCH_POLL_INTERVAL_SEC
    stop_grace_sec: float = _STOP_GRACE_SEC

    def __post_init__(self):
        # Path("") normalizes to ".", so reject both — a dedicated profile is required
        # (Chrome refuses --remote-debugging-port on the default profile).
        if str(self.profile_base_dir).strip() in ("", "."):
            raise ValueError("profile_base_dir must be a dedicated, non-default path")
        if self.port is None:
            raise ValueError(f"cdp_url {self.cdp_url!r} has no parseable port")

    @property
    def port(self) -> Optional[int]:
        try:
            return urlparse(self.cdp_url).port
        except ValueError:
            return None


class _PopenHandle:
    """Adapts subprocess.Popen to the tiny handle interface ChromeManager needs, so tests
    inject a fake without touching the OS."""

    def __init__(self, proc: "subprocess.Popen"):
        self._proc = proc

    @property
    def pid(self) -> int:
        return self._proc.pid

    def poll(self) -> Optional[int]:
        return self._proc.poll()

    def terminate(self) -> None:
        self._proc.terminate()

    def kill(self) -> None:
        self._proc.kill()


def _default_launcher(argv: list) -> _PopenHandle:
    return _PopenHandle(subprocess.Popen(argv))


def _default_http_probe(cdp_url: str, timeout: float) -> bool:
    """Cheap pre-check: does anything answer /json/version? Never raises."""
    try:
        import httpx
        resp = httpx.get(cdp_url.rstrip("/") + "/json/version", timeout=timeout)
        return resp.status_code == 200
    except Exception:  # noqa: BLE001 — a closed port is the common, expected case
        return False


def _default_cdp_prober(cdp_url: str, timeout: float) -> bool:
    """The REAL detection signal: can Playwright actually attach over CDP? Mirrors
    core/cdp.py's connect_over_cdp(no_defaults=True). Never raises; a missing Playwright
    or a rejected attach both read as 'not attachable'."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        log.warning("Playwright unavailable — cannot CDP-probe Chrome")
        return False
    pw = None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(cdp_url, no_defaults=True, timeout=timeout * 1000)
        browser.close()  # closes only OUR connection, not the browser (core/cdp.py note)
        return True
    except Exception:  # noqa: BLE001 — degraded/absent Chrome
        return False
    finally:
        if pw is not None:
            try:
                pw.stop()
            except Exception:  # noqa: BLE001
                pass


def _macos_focus() -> bool:
    try:
        subprocess.run(["osascript", "-e",
                        'tell application "Google Chrome" to activate'],
                       check=True, timeout=5)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("macOS Chrome focus failed: %s", e)
        return False


def _unsupported_focus() -> bool:
    log.warning("Chrome focus not implemented on this OS — 2FA button is a no-op here")
    return False


class ChromeManager:
    """Idempotent launch/detect/stop/focus of ONE Chrome process."""

    def __init__(self, cfg: ChromeManagerConfig, *,
                 launcher: Callable[[list], object] = _default_launcher,
                 cdp_prober: Callable[[str, float], bool] = _default_cdp_prober,
                 http_probe: Callable[[str, float], bool] = _default_http_probe,
                 sleep: Callable[[float], None] = time.sleep,
                 os_name: Callable[[], str] = platform.system,
                 focus_macos: Callable[[], bool] = _macos_focus,
                 focus_windows: Callable[[], bool] = _unsupported_focus,
                 focus_linux: Callable[[], bool] = _unsupported_focus,
                 path_exists: Callable[[str], bool] = os.path.exists,
                 playwright_executable_path: Optional[Callable[[], Optional[str]]] = None):
        self._cfg = cfg
        self._launcher = launcher
        self._cdp_prober = cdp_prober
        self._http_probe = http_probe
        self._sleep = sleep
        self._os_name = os_name
        self._focus = {"Darwin": focus_macos, "Windows": focus_windows, "Linux": focus_linux}
        self._path_exists = path_exists
        self._playwright_executable_path = playwright_executable_path
        self._handle = None
        self._launched_by_us = False

    def ensure_running(self) -> ChromeStatus:
        """Reconnect to an attachable Chrome if present, else launch one. Idempotent —
        safe to call on every sidecar/app start. Raises :class:`ChromeUnhealthyError` if
        a Chrome is listening but not CDP-attachable (never kills a process we don't own),
        or :class:`ChromeLaunchError` on a failed/timed-out launch.

        Which profile directory a launch opens is decided in :func:`_build_launch_args`,
        from the brand of the binary we resolved — attaching to a Chrome that is already
        running picks no directory at all, because it opens nothing."""
        url = self._cfg.cdp_url
        if self._http_probe(url, _HTTP_PROBE_TIMEOUT_SEC):
            if self._cdp_prober(url, _CDP_PROBE_TIMEOUT_SEC):
                self._launched_by_us = False
                log.info("Attached to existing Chrome at %s", url)
                return ChromeStatus("attached_existing", False, url)
            raise ChromeUnhealthyError(
                f"Chrome at {url} answers HTTP but rejects CDP — quit the stale Chrome "
                "and retry (this manager will not kill a browser it did not launch)")
        return self._launch()

    def _launch(self) -> ChromeStatus:
        binary = resolve_chrome_binary(
            self._cfg, os_name=self._os_name(), path_exists=self._path_exists,
            playwright_executable_path=self._playwright_executable_path)
        argv = _build_launch_args(self._cfg, binary)
        # Informational, never blocking: a pre-split profile in the base is inert and this
        # launch does not touch it, but it IS why a box upgraded across the split suddenly
        # looks signed out. Say so at the one moment an operator is looking at a launch.
        if has_legacy_profile(self._cfg.profile_base_dir):
            log.warning("%s", legacy_profile_note(self._cfg.profile_base_dir))
        log.info("Launching managed Chrome: %s into %s", binary, argv_profile(argv))
        self._handle = self._launcher(argv)
        waited = 0.0
        while waited < self._cfg.launch_timeout_sec:
            if self._cdp_prober(self._cfg.cdp_url, _CDP_PROBE_TIMEOUT_SEC):
                self._launched_by_us = True
                return ChromeStatus("launched", True, self._cfg.cdp_url)
            self._sleep(self._cfg.launch_poll_interval_sec)
            waited += self._cfg.launch_poll_interval_sec
        raise ChromeLaunchError(
            f"Chrome launched but did not become CDP-attachable within "
            f"{self._cfg.launch_timeout_sec}s at {self._cfg.cdp_url}")

    def stop(self) -> None:
        """Terminate a Chrome THIS manager launched. A no-op when we only attached — a
        Chrome we did not start must outlive us (LOCKED reconnect-never-kill semantics)."""
        if not self._launched_by_us or self._handle is None:
            log.info("Chrome stop: nothing we launched to stop (attached-only)")
            return
        try:
            self._handle.terminate()
        except Exception as e:  # noqa: BLE001
            log.warning("Chrome terminate() raised (continuing to kill): %s", e)
        waited = 0.0
        while waited < self._cfg.stop_grace_sec:
            try:
                if self._handle.poll() is not None:
                    break
            except Exception:  # noqa: BLE001
                break
            self._sleep(self._cfg.launch_poll_interval_sec)
            waited += self._cfg.launch_poll_interval_sec
        else:
            try:
                self._handle.kill()
            except Exception as e:  # noqa: BLE001
                log.warning("Chrome kill() raised: %s", e)
        self._launched_by_us = False
        self._handle = None

    def focus_window(self) -> bool:
        """Bring the warmed Chrome to the foreground for the operator's 2FA/captcha
        button. Never raises — a focus failure must not crash the checkpoint flow.
        Windows/Linux are DESIGN-ONLY stubs pending real-hardware implementation."""
        strategy = self._focus.get(self._os_name(), _unsupported_focus)
        try:
            return strategy()
        except Exception as e:  # noqa: BLE001
            log.warning("focus_window strategy raised: %s", e)
            return False


def _build_launch_args(cfg: ChromeManagerConfig, binary: str) -> list:
    """Pure argv builder (no I/O). Raises ChromeLaunchError on an unparseable port.

    The ONE place this process decides which directory a Chrome opens, and it derives it
    rather than reading it: ``--user-data-dir`` is ``<base>/<brand of the binary>``. A call
    site that passed ``cfg.profile_base_dir`` straight through would put two brands back in
    one directory, which is the whole failure this shape exists to make impossible."""
    if cfg.port is None:
        raise ChromeLaunchError(f"cdp_url {cfg.cdp_url!r} has no parseable port")
    profile = profile_dir_for(cfg.profile_base_dir, brand_of_binary(binary))
    return [binary, f"--remote-debugging-port={cfg.port}",
            f"--user-data-dir={profile}", _NO_FIRST_RUN,
            _NO_DEFAULT_BROWSER_CHECK, *cfg.extra_flags]


def argv_profile(argv: list) -> str:
    """The ``--user-data-dir`` value out of a built argv, for the launch log. Reads what we
    are ABOUT to do rather than recomputing it, so the log can never disagree with the
    process."""
    for arg in argv:
        if isinstance(arg, str) and arg.startswith("--user-data-dir="):
            return arg.split("=", 1)[1]
    return "?"


# The human names for the two tokens, spelled ONCE: `legacy_profile_note` is asking an
# operator to identify a browser from memory, and "Chrome for Testing" written two slightly
# different ways in two places is enough to make them pick the wrong directory.
_BRAND_LABELS = {BRAND_CHROME_FOR_TESTING: "Chrome for Testing",
                 BRAND_CHROME: "system Google Chrome",
                 BRAND_CHROMIUM: "Chromium"}


def brand_of_binary(binary: str) -> str:
    """The brand token for a Chrome binary PATH. Pure-ish (one symlink resolution), and the
    only place the rule lives. Its answer is a DIRECTORY NAME, so a wrong answer does not
    warn anybody — it quietly opens a different profile.

    Symlinks are resolved FIRST, then the path is lowercased with backslashes normalised,
    then three rules in order:

      1. contains "chrome for testing"                     -> chrome-for-testing
      2. any PATH SEGMENT matches ^chromium(_headless_shell)?-[0-9]+$
                                                           -> chrome-for-testing
      3. otherwise                                         -> chrome

    Resolving first is not tidiness: a wrapper script or a convenience symlink
    (``/usr/local/bin/chrome`` -> Playwright's build) is exactly how a Chrome-for-Testing
    binary reaches rule 3 and gets pointed at the system-Chrome directory. Only an existing
    path is resolved — ``realpath`` on a non-existent or foreign-platform path prepends the
    CURRENT WORKING DIRECTORY, which would let a test runner's cwd decide the brand.

    Rule 2 is not decoration: it is the ONLY rule that fires on linux-x64, linux-arm64 and
    win-x64, where Playwright's Chrome for Testing is installed as a bare `chrome` /
    `chrome.exe` (see `_CFT_PLAYWRIGHT_CACHE_SEGMENT` for why it is anchored to a whole
    segment)."""
    raw = binary or ""
    try:
        if raw and os.path.lexists(raw):
            raw = os.path.realpath(raw)
    except Exception:  # noqa: BLE001 — a path we cannot stat is judged as written
        pass
    path = raw.lower().replace("\\", "/")
    if _CFT_BINARY_SIGNATURE in path:
        return BRAND_CHROME_FOR_TESTING
    if any(_CFT_PLAYWRIGHT_CACHE_SEGMENT.match(seg) for seg in path.split("/")):
        return BRAND_CHROME_FOR_TESTING
    # Rule 3 (Linux): distro Chromium is a THIRD brand, not a spelling of Chrome. It seals
    # cookies under its own keyring entry, so filing it under `chrome` — as a two-token rule
    # did — hands /usr/bin/chromium and /usr/bin/google-chrome the same directory and wipes
    # whichever warmed it first. Matched on the file NAME: the path is unconstrained, and
    # rule 2 has already claimed anything in Playwright's cache.
    if _basename(path) in _DISTRO_CHROMIUM_NAMES:
        return BRAND_CHROMIUM
    return BRAND_CHROME


def _basename(posix_path: str) -> str:
    """Last segment of an already-normalised, already-lowercased path, minus a Windows
    `.exe` suffix so `chromium.exe` and `chromium` agree."""
    name = posix_path.rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".exe") else name


def profile_dir_for(base, brand: str) -> Path:
    """``<base>/<brand>`` — the profile directory a browser of that brand owns.

    The entire cross-brand class of bug is this one line. The path IS the ownership record,
    so there is no marker to read, no declaration to collect and no way for a human to
    answer wrong. FROZEN CONTRACT, identical in
    ``desktop/src-tauri/src/chrome_manager.rs`` and ``scripts/warm_chrome.sh``: change the
    layout on one side and the operator's warmed logins are simply somewhere the other two
    do not look."""
    return Path(base) / brand


def _brand_label(token: str) -> str:
    return _BRAND_LABELS.get(token, token)


def has_legacy_profile(base) -> bool:
    """Does the BASE itself hold a profile from before the per-brand split? A
    ``<base>/Default`` directory stat and nothing else (see `LEGACY_PROFILE_DIR_NAME`).

    Never raises and never true for the new layout: after the split, ``Default`` lives at
    ``<base>/<brand>/Default``."""
    try:
        return Path(base, LEGACY_PROFILE_DIR_NAME).is_dir()
    except Exception:  # noqa: BLE001
        return False


def legacy_profile_note(base) -> str:
    """The ONE thing that ever happens to a pre-split profile: this paragraph.

    Written once and shared by both surfaces that show it — the launch log here and
    ``preflight.check_chrome_profile``'s remedy — because two copies of an instruction that
    moves an operator's logins is two chances to disagree about where they should land.

    It never guesses the brand: that guess is precisely what the old design got wrong, and
    getting it wrong deletes every session in the directory. Both candidate destinations are
    spelled out in full so the move is a copy-paste rather than a puzzle, and the recipe
    moves the base aside first — ``mv <base>/* <base>/<brand>/`` would try to move the
    destination into itself."""
    base = Path(base)
    cft = profile_dir_for(base, BRAND_CHROME_FOR_TESTING)
    chrome = profile_dir_for(base, BRAND_CHROME)
    return (
        f"{base} holds a browser profile from before aizu gave each browser brand its own "
        f"profile directory (a {LEGACY_PROFILE_DIR_NAME}/ folder sits directly in it). "
        f"Nothing launches it any more, and it has been left EXACTLY as it was — not "
        f"moved, not copied, not renamed, not deleted. Whichever browser warmed it still "
        f"owns it, and only you can know which one that was: opening a profile with the "
        f"other brand DELETES every saved login in it, so aizu will not guess. If you DO "
        f"know, move it into the matching directory yourself and its logins come back. "
        # Both destinations, never one. An earlier version pre-typed the Chrome-for-Testing
        # path into the command, which meant the app DID guess the brand — and published
        # that guess to the fleet console as a preflight remedy. An operator whose profile
        # was warmed by system Chrome would have pasted the exact wrong-brand open this
        # whole redesign exists to make unreachable.
        f"Pick the line that matches the browser that warmed it: "
        f"DEST={chrome} for {_brand_label(BRAND_CHROME)}, or "
        f"DEST={cft} for {_brand_label(BRAND_CHROME_FOR_TESTING)}. Then: "
        # Moves ONLY the profile folder. Moving the base itself would bury the per-brand
        # directories the launcher has already created inside the destination.
        f'mkdir -p "$DEST" && mv {base / LEGACY_PROFILE_DIR_NAME} "$DEST/{LEGACY_PROFILE_DIR_NAME}". '
        f"If you do not know, leave it alone and sign in again in the new profile.")


def _default_playwright_executable_path() -> Optional[str]:
    """Chrome-for-Testing's own bundled binary path, when Playwright is installed —
    the protocol-matched preference ``resolve_chrome_binary`` wants (see its
    docstring). A real (if short-lived) Playwright start just to read this path;
    never raises."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        return None
    pw = None
    try:
        pw = sync_playwright().start()
        return pw.chromium.executable_path
    except Exception:  # noqa: BLE001
        return None
    finally:
        if pw is not None:
            try:
                pw.stop()
            except Exception:  # noqa: BLE001
                pass


# The profile base and the binary override, ONE spelling each, repo-wide. Both names and
# both defaults are the ones `scripts/warm_chrome.sh` — the launcher this repo actually
# documents and operators actually run — has always used, because this module is the side
# that was wrong: it read AIZU_CHROME_PROFILE_DIR defaulting to ~/.aizu-chrome-profile, so
# the preflight's profile row inspected a directory nothing on the box ever warmed and
# reported green about it, while every real session sat in ~/.aizu-cft-profile. A row about
# a directory nothing warms is worse than no row at all. `desktop/src-tauri/src/
# chrome_manager.rs` reads the same two names.
CHROME_PROFILE_ENV = "AIZU_CHROME_PROFILE"
DEFAULT_CHROME_PROFILE_BASE = ".aizu-cft-profile"
# The binary override is TWO names for ONE knob, in this order, matching warm_chrome.sh and
# desktop/src-tauri/src/chrome_manager.rs exactly. AIZU_CHROME_BINARY wins: it is ours and
# unambiguous, while CHROME_BIN is a generic name other tooling on the box may already set
# for its own reasons — and the loser of that tie is not a preference, it is a directory.
# CHROME_BIN stays as the legacy spelling the launcher has always honoured.
#
# The ORDER, not just the set, has to match all three implementations. It did not: bash read
# AIZU_CHROME_BINARY first while this module and the Rust read CHROME_BIN first, so a box
# with BOTH set warmed one profile directory and harvested another — the launcher opening
# <base>/chrome-for-testing while the worker opened <base>/chrome. The binary now chooses
# the PROFILE DIRECTORY, so a precedence disagreement is a split-brain about which logins
# exist, and the operator just sees them gone.
CHROME_BINARY_ENVS = ("AIZU_CHROME_BINARY", "CHROME_BIN")


def config_from_env(cdp_url: Optional[str] = None) -> ChromeManagerConfig:
    """Build a ChromeManagerConfig from env for the ONE shared default-account Chrome
    this repo's on-demand-run/dev flow uses (TASK B readiness/launch-login; distinct
    from the per-account warming pool's own --chrome-profile/--cdp-port, which is
    unrelated). AIZU_CDP_URL is the same var every other CDP call site reads;
    AIZU_CHROME_PROFILE/CHROME_BIN are the launcher's own, both optional, both defaulted
    for a bare dev box so `ensure_chrome_running` works with zero configuration.

    AIZU_CHROME_PROFILE is a BASE, not a profile: the launch opens `<base>/<brand>`. An
    operator who points it at a pre-split profile therefore loses nothing — the old
    directory is left where it is and reported by `legacy_profile_note`."""
    url = cdp_url or os.environ.get("AIZU_CDP_URL", "http://127.0.0.1:9333")
    profile = os.environ.get(CHROME_PROFILE_ENV) or str(
        Path.home() / DEFAULT_CHROME_PROFILE_BASE)
    binary = next((os.environ[name] for name in CHROME_BINARY_ENVS
                   if os.environ.get(name)), None)
    return ChromeManagerConfig(cdp_url=url, profile_base_dir=Path(profile),
                              chrome_binary=binary)


def ensure_chrome_running(cdp_url: Optional[str] = None) -> ChromeStatus:
    """One-shot convenience: attach to the shared Chrome at AIZU_CDP_URL if it is
    already up (the common case on a dev box that warmed it out of band), else
    launch one from AIZU_CHROME_PROFILE/CHROME_BIN. The single call site
    POST /api/agent/launch-login and the worker startup gate (TASK B) both use, so
    Chrome-launch policy lives in exactly one place. Raises a ChromeError subclass on
    failure — callers decide how to surface that (e.g. a 500 launch_failed)."""
    cfg = config_from_env(cdp_url)
    mgr = ChromeManager(cfg, playwright_executable_path=_default_playwright_executable_path)
    return mgr.ensure_running()


def resolve_chrome_binary(cfg: ChromeManagerConfig, *, os_name: str,
                          path_exists: Callable[[str], bool] = os.path.exists,
                          playwright_executable_path: Optional[Callable[[], Optional[str]]] = None
                          ) -> str:
    """Resolve the Chrome binary (precedence, ported from warm_chrome.sh): explicit
    override → Playwright's Chrome-for-Testing (protocol-matched) → per-OS system Chrome.
    Raises :class:`ChromeBinaryNotFoundError` if nothing resolvable exists on disk."""
    if cfg.chrome_binary:
        if path_exists(cfg.chrome_binary):
            return cfg.chrome_binary
        raise ChromeBinaryNotFoundError(
            f"configured chrome_binary does not exist: {cfg.chrome_binary}")
    if playwright_executable_path is not None:
        cft = playwright_executable_path()
        if cft and path_exists(cft):
            return cft
    for candidate in _SYSTEM_CHROME_PATHS.get(os_name, []):
        if path_exists(candidate):
            return candidate
    raise ChromeBinaryNotFoundError(
        f"no Chrome binary found for {os_name} — set chrome_binary explicitly")
