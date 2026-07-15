"""Best-effort CDP status probe for the control surface (Phase 6, D3).

A short-lived, never-throwing read of the warmed Chrome's ``/json/version`` so the desktop
shell can show a connected/disconnected badge. Isolated in its own tiny module because it
is the one status-building piece with a real external dependency (httpx) and real
fallibility — a closed port is the common, expected case, never an error.
"""
from __future__ import annotations

from typing import Callable, Optional

from ..core.logsetup import get_logger
from .control_state import ChromeStatusView

log = get_logger("reelradar.worker.chrome_probe")

_PROBE_TIMEOUT_SEC = 1.0


def cdp_status(cdp_url: str, *,
               http_get: Optional[Callable[[str, float], Optional[dict]]] = None
               ) -> ChromeStatusView:
    """Return a ChromeStatusView for ``cdp_url``. Never raises — a disconnected Chrome
    yields ``connected=False``. ``http_get(url, timeout) -> dict|None`` is injectable so
    tests need no real Chrome/httpx."""
    getter = http_get or _default_http_get
    try:
        data = getter(cdp_url.rstrip("/") + "/json/version", _PROBE_TIMEOUT_SEC)
    except Exception:  # noqa: BLE001 — probe must never raise
        data = None
    if not isinstance(data, dict):
        return ChromeStatusView(connected=False, cdp_url=cdp_url, browser_version=None)
    version = data.get("Browser") if isinstance(data.get("Browser"), str) else None
    return ChromeStatusView(connected=True, cdp_url=cdp_url, browser_version=version)


def _default_http_get(url: str, timeout: float) -> Optional[dict]:
    try:
        import httpx
        resp = httpx.get(url, timeout=timeout)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:  # noqa: BLE001
        return None
