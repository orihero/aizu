"""Engine agent readiness — is the shared warmed Chrome up AND Instagram logged in?

TASK A (hang-prevention) established that a mid-run login/challenge wall on the
shared CDP browser is only discoverable by actually walking a source
(``core/cdp.py``'s ``_login_wall_reason``) — there was no cheap, out-of-band way to
ask "is the agent ready to run?" BEFORE spawning a run. This module is that
out-of-band check: the bridge's ``POST /api/run`` gate, the CLI's ``aizu run``
pre-flight, the panel's readiness banner (``GET /api/agent/readiness``), and the
worker sidecar's startup gate all share it, so "ready" means exactly one thing
everywhere.

Two independent probes compose into one readiness verdict:
  - :func:`probe_cdp` — does the CDP endpoint even answer ``/json/version``?
    Borrowed from ``worker.chrome_manager``'s own HTTP pre-check: cheap, no
    Playwright, never raises.
  - :func:`probe_instagram_login` — a BOUNDED (``core.bounded.call_bounded`` — a
    hung Playwright call must never wedge a caller asking "are we ready?", the
    whole point of TASK A) Playwright ``connect_over_cdp`` read of the browser's
    Instagram cookie jar + open tabs. A non-expired ``sessionid`` cookie for
    instagram.com => ``logged_in``; a tab already parked on the login/challenge
    wall forces ``logged_out`` even over a not-yet-expired cookie; any probe
    failure/timeout degrades to ``unknown`` (never raises, never claims ready when
    it can't actually tell).

:func:`check_readiness` is the composed, CACHED (<= ``CACHE_TTL_SEC``) entry point
every caller uses. It deliberately does NOT reprobe while a run is active
(``run_active`` callable) — a run already owns the one CDP connection this
codebase's single-browser architecture allows, and attaching a second Playwright
client mid-run risks exactly the kind of CDP hiccup TASK A hardened against. In
that window the last-known result is returned instead (with ``detail`` explaining
why); a fresh probe resumes as soon as the run ends. :func:`invalidate` drops the
cache — called the moment the mid-run login/checkpoint health flag is raised
(``engines/instagram/session.py``) so a stale cached "ready" can never survive past
the moment the panel most needs a fresh answer.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Iterable, Optional

from .core.bounded import call_bounded
from .core.logsetup import get_logger

log = get_logger(__name__)

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except Exception:  # pragma: no cover - playwright optional at import time
    sync_playwright = None  # type: ignore
    PLAYWRIGHT_AVAILABLE = False

DEFAULT_CDP_URL = "http://127.0.0.1:9222"
# <=60s-old cached result is fine per the API contract; force_refresh (?refresh=1)
# bypasses it.
CACHE_TTL_SEC = 60.0
_CDP_PROBE_TIMEOUT_SEC = 3.0
_LOGIN_PROBE_TIMEOUT_SEC = 5.0

INSTAGRAM_SESSION_COOKIE = "sessionid"
_INSTAGRAM_COOKIE_DOMAIN_HINT = "instagram.com"
# Mirrors core/cdp.py's CDPFeedBase._login_wall_reason URL signature for Instagram.
_LOGIN_WALL_HINTS = ("/accounts/login/", "/challenge/")


class ReadinessTimeout(Exception):
    """A CDP/Playwright probe exceeded its hard deadline (core.bounded.call_bounded).
    Always caught internally — every probe below degrades to 'unreachable'/'unknown'
    instead of letting this escape to a caller."""


def default_cdp_url() -> str:
    return os.environ.get("AIZU_CDP_URL", DEFAULT_CDP_URL)


def probe_cdp(cdp_url: str, timeout: float = _CDP_PROBE_TIMEOUT_SEC) -> str:
    """'ok' if the CDP endpoint answers /json/version, else 'unreachable'. Cheap HTTP
    check (borrowed from worker.chrome_manager's own pre-check) — never Playwright,
    never raises."""
    from .worker.chrome_manager import _default_http_probe
    return "ok" if _default_http_probe(cdp_url, timeout) else "unreachable"


def _classify_login_state(page_urls: Iterable[str], cookies: Iterable[dict], *,
                          now: Optional[float] = None) -> str:
    """Pure classifier (no I/O) so cookie-present/absent/expired and login-wall-URL
    logic is unit-testable with zero Playwright. A tab already on the login/challenge
    wall forces logged_out even over an as-yet-unexpired cookie — the wall itself is
    the stronger signal (mirrors core/cdp.py's own _login_wall_reason check)."""
    for url in page_urls:
        if any(hint in (url or "") for hint in _LOGIN_WALL_HINTS):
            return "logged_out"
    now_v = time.time() if now is None else now
    for cookie in cookies:
        if cookie.get("name") != INSTAGRAM_SESSION_COOKIE:
            continue
        domain = cookie.get("domain") or ""
        if _INSTAGRAM_COOKIE_DOMAIN_HINT not in domain:
            continue
        expires = cookie.get("expires")
        # Playwright reports -1 for a session cookie (no explicit expiry) — that is
        # NOT "already expired", just "expires when the browser session ends".
        if expires is None or expires < 0 or expires > now_v:
            return "logged_in"
    return "logged_out"


def _read_instagram_state(cdp_url: str, timeout: float) -> tuple:
    """The real (unbounded on its own) Playwright attach + read: (page_urls,
    cookies). Always run this through call_bounded — never call it directly off the
    probing thread, since connect_over_cdp/cookies() are exactly the calls that can
    wedge (TASK A)."""
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(cdp_url, no_defaults=True,
                                               timeout=timeout * 1000)
        try:
            contexts = browser.contexts
            if not contexts:
                return ([], [])
            ctx = contexts[0]
            urls = []
            for page in ctx.pages:
                try:
                    urls.append(page.url)
                except Exception:  # noqa: BLE001 — a dead page must not sink the probe
                    continue
            try:
                cookies = ctx.cookies()
            except Exception:  # noqa: BLE001
                cookies = []
            return (urls, cookies)
        finally:
            browser.close()  # our connection only — never the warmed browser itself
    finally:
        pw.stop()


def probe_instagram_login(cdp_url: str, timeout: float = _LOGIN_PROBE_TIMEOUT_SEC, *,
                          read_state: Optional[Callable[[], tuple]] = None) -> str:
    """'logged_in' | 'logged_out' | 'unknown'. Bounded via call_bounded so a wedged
    CDP pipe (the exact TASK A failure mode) degrades to 'unknown' instead of hanging
    the caller. ``read_state`` is the injectable seam for tests — a zero-arg callable
    returning (page_urls, cookies); defaults to a real bounded Playwright attach."""
    if read_state is not None:
        reader = read_state
    elif PLAYWRIGHT_AVAILABLE:
        reader = lambda: _read_instagram_state(cdp_url, timeout)  # noqa: E731
    else:
        return "unknown"
    try:
        page_urls, cookies = call_bounded(reader, timeout, timeout_exc=ReadinessTimeout)
    except Exception:  # noqa: BLE001 — any probe failure/timeout → unknown, never raise
        return "unknown"
    return _classify_login_state(page_urls, cookies)


def _default_open_login_tab(cdp_url: str, timeout: float) -> bool:
    if not PLAYWRIGHT_AVAILABLE:
        return False
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(cdp_url, no_defaults=True,
                                               timeout=timeout * 1000)
        try:
            contexts = browser.contexts
            ctx = contexts[0] if contexts else browser.new_context()
            for page in ctx.pages:
                try:
                    if "instagram.com" in (page.url or ""):
                        page.bring_to_front()
                        return True
                except Exception:  # noqa: BLE001 — a dead page just isn't the one
                    continue
            page = ctx.new_page()
            page.goto("https://www.instagram.com/", wait_until="domcontentloaded",
                      timeout=timeout * 1000)
            return True
        finally:
            browser.close()
    finally:
        pw.stop()


def open_login_tab(cdp_url: str, timeout: float = _LOGIN_PROBE_TIMEOUT_SEC, *,
                   opener: Optional[Callable[[], bool]] = None) -> bool:
    """Open (or focus) a Chrome tab on instagram.com so a human can log in — shared by
    POST /api/agent/launch-login and the worker startup gate. Bounded + never raises;
    False just means it could not be opened (callers degrade gracefully, they don't
    treat it as fatal)."""
    op = opener or (lambda: _default_open_login_tab(cdp_url, timeout))
    try:
        return bool(call_bounded(op, timeout, timeout_exc=ReadinessTimeout))
    except Exception:  # noqa: BLE001
        return False


# ---- the composed, cached check ----

_lock = threading.Lock()
_cache: Optional[dict] = None


def invalidate() -> None:
    """Drop the cached readiness result. Called the moment a mid-run login/checkpoint
    health flag is raised (engines/instagram/session.py) so the NEXT check_readiness
    call recomputes instead of serving a stale pre-halt 'ready:true' for up to
    CACHE_TTL_SEC — that staleness is exactly what would delay the panel's alert."""
    global _cache
    with _lock:
        _cache = None


def check_readiness(cdp_url: str, *, timeout: float = _LOGIN_PROBE_TIMEOUT_SEC,
                    force_refresh: bool = False,
                    run_active: Optional[Callable[[], bool]] = None,
                    probe_cdp_fn: Callable[[str, float], str] = probe_cdp,
                    probe_login_fn: Callable[[str, float], str] = probe_instagram_login
                    ) -> dict:
    """The full agent-readiness contract dict: {ready, cdp, instagram, checkedAt,
    detail, cdpUrl}. ``ready`` is always exactly ``cdp == "ok" and instagram ==
    "logged_in"``.

    - A live run holds the ONE CDP connection this architecture allows: while
      ``run_active()`` is true, this NEVER attaches a second Playwright client — it
      returns the last-known cached result (with `detail` explaining why), or, with
      no history yet, a cheap cdp-only probe (safe: HTTP, no Playwright) plus
      instagram='unknown'.
    - Otherwise: a cached result <= CACHE_TTL_SEC old is reused unless
      ``force_refresh`` (the panel's `?refresh=1`); else both probes run fresh and
      the result is cached for the next caller.
    """
    global _cache   # declared up front: the write below is guarded by _lock, but a
                    # `global` after the read on the next line is a SyntaxError.
    is_active = bool(run_active()) if run_active is not None else False
    with _lock:
        cached = dict(_cache) if _cache is not None else None
    now = time.time()
    if is_active:
        if cached is not None:
            cached["detail"] = ("a run is active — reusing the last-known state "
                                "(never attaching a second Chrome connection mid-run)")
            return cached
        cdp_state = probe_cdp_fn(cdp_url, _CDP_PROBE_TIMEOUT_SEC)
        return {"ready": False, "cdp": cdp_state, "instagram": "unknown",
                "checkedAt": now, "cdpUrl": cdp_url,
                "detail": "a run is active; instagram login state is unknown until "
                          "it finishes"}
    if (not force_refresh and cached is not None
            and (now - cached["checkedAt"]) <= CACHE_TTL_SEC):
        return cached
    cdp_state = probe_cdp_fn(cdp_url, _CDP_PROBE_TIMEOUT_SEC)
    instagram_state = probe_login_fn(cdp_url, timeout) if cdp_state == "ok" else "unknown"
    ready = cdp_state == "ok" and instagram_state == "logged_in"
    detail: Optional[str] = None
    if cdp_state != "ok":
        detail = f"CDP not reachable at {cdp_url}"
    elif instagram_state != "logged_in":
        detail = f"instagram session is {instagram_state}"
    result = {"ready": ready, "cdp": cdp_state, "instagram": instagram_state,
              "checkedAt": now, "cdpUrl": cdp_url, "detail": detail}
    with _lock:
        _cache = dict(result)
    return result


# ---- the same contract, derived from the worker fleet ----

def fleet_readiness(workers: Iterable[dict], *, now: Optional[float] = None) -> dict:
    """The readiness contract for the DISTRIBUTED backend, where the cloud control
    plane has no browser of its own (distributed-workers PRD §2: PULL, never remote
    CDP). Probing this process's CDP would then always answer 'unreachable' and say
    nothing true about whether a live run can start — what actually gates a run here
    is whether any worker PC is online to lease the job.

    Ready iff at least one non-revoked worker is 'online'. ``instagram`` stays
    'unknown' on purpose: each box's login state lives on that box and is never
    reported up the presence heartbeat (``_validate_worker_heartbeat`` accepts
    ``chromeHealth`` and drops it), so claiming 'logged_in' here would be a guess.
    Callers read ``ready``; ``detail`` carries the human explanation.
    """
    online = [w for w in workers
              if w.get("status") == "online" and not w.get("revokedAt")]
    checked_at = now if now is not None else time.time()
    if online:
        detail = (f"{len(online)} worker(s) online — live runs are dispatched to the "
                  "fleet, which drives Chrome on the worker PC")
    else:
        detail = ("no worker is online — a live run would be queued with nothing to "
                  "pick it up. Start a worker (aizu-worker) on a warmed box.")
    return {"ready": bool(online), "cdp": "ok" if online else "unreachable",
            "instagram": "unknown", "checkedAt": checked_at,
            "cdpUrl": "", "detail": detail}
