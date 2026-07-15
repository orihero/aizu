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

Port note (unresolved policy): the engine default is 9222 but every LIVE run in this repo
has used 9333 (per-account BASE_PORT+ordinal). This module takes ``cdp_url`` from config
with NO internal default — the wiring layer MUST set it correctly or reproduce the 9333
ECONNREFUSED history. See ``memory/engine-live-run.md``.
"""
from __future__ import annotations

import os
import platform
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from ..core.logsetup import get_logger

log = get_logger("reelradar.worker.chrome_manager")

_LAUNCH_TIMEOUT_SEC = 15.0
_LAUNCH_POLL_INTERVAL_SEC = 0.5
_STOP_GRACE_SEC = 5.0
_HTTP_PROBE_TIMEOUT_SEC = 1.0
_CDP_PROBE_TIMEOUT_SEC = 5.0
# Fixed launch flags (never magic strings scattered across call sites).
_NO_FIRST_RUN = "--no-first-run"
_NO_DEFAULT_BROWSER_CHECK = "--no-default-browser-check"

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
    hardcoded 9222/9333). ``user_data_dir`` MUST be a dedicated non-default profile."""
    cdp_url: str
    user_data_dir: Path
    chrome_binary: Optional[str] = None
    extra_flags: tuple = ()
    launch_timeout_sec: float = _LAUNCH_TIMEOUT_SEC
    launch_poll_interval_sec: float = _LAUNCH_POLL_INTERVAL_SEC
    stop_grace_sec: float = _STOP_GRACE_SEC

    def __post_init__(self):
        # Path("") normalizes to ".", so reject both — a dedicated profile is required
        # (Chrome refuses --remote-debugging-port on the default profile).
        if str(self.user_data_dir).strip() in ("", "."):
            raise ValueError("user_data_dir must be a dedicated, non-default profile path")
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
        or :class:`ChromeLaunchError` on a failed/timed-out launch."""
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
        log.info("Launching managed Chrome: %s", binary)
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
    """Pure argv builder (no I/O). Raises ChromeLaunchError on an unparseable port."""
    if cfg.port is None:
        raise ChromeLaunchError(f"cdp_url {cfg.cdp_url!r} has no parseable port")
    return [binary, f"--remote-debugging-port={cfg.port}",
            f"--user-data-dir={cfg.user_data_dir}", _NO_FIRST_RUN,
            _NO_DEFAULT_BROWSER_CHECK, *cfg.extra_flags]


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
