"""Engine agent readiness — is the shared warmed Chrome up AND signed in?

TASK A (hang-prevention) established that a mid-run login/challenge wall on the
shared CDP browser is only discoverable by actually walking a source
(``core/cdp.py``'s ``_login_wall_reason``) — there was no cheap, out-of-band way to
ask "is the agent ready to run?" BEFORE spawning a run. This module is that
out-of-band check: the bridge's ``POST /api/run`` gate, the CLI's ``aizu run``
pre-flight, the panel's readiness banner (``GET /api/agent/readiness``), and the
worker sidecar's launch preflight (``worker/preflight.py``) all share it, so
"ready" means exactly one thing everywhere.

Two independent probes compose into one readiness verdict:
  - :func:`probe_cdp` — does the CDP endpoint even answer ``/json/version``?
    Borrowed from ``worker.chrome_manager``'s own HTTP pre-check: cheap, no
    Playwright, never raises. NOTE that an HTTP 200 here is NOT proof the browser
    is usable — ledger B6/D3: a stale/degraded Chrome answers ``/json/version``
    while REJECTING ``connect_over_cdp``. Only :func:`probe_browser` settles that.
  - :func:`probe_browser` — a BOUNDED (``core.bounded.call_bounded`` — a hung
    Playwright call must never wedge a caller asking "are we ready?", the whole
    point of TASK A) ``connect_over_cdp`` read of the browser's cookie jar + open
    tabs, classified into a login state PER PLATFORM off that ONE attach. A
    non-expired session cookie on the platform's own domain => ``logged_in``; a tab
    already parked on that platform's login/challenge wall forces ``logged_out``
    even over a not-yet-expired cookie; any probe failure/timeout degrades to
    ``unknown`` (never raises, never claims ready when it can't actually tell).
    :func:`probe_instagram_login` is the original single-platform façade, kept
    byte-compatible for its existing callers. A bounded call is a deadline, not a
    kill switch, so the reader also owns a HARD STOP that outlives a caller which
    walked away — see "Playwright driver lifetime" at :func:`read_browser_state`.

Per-platform signatures live in :data:`PLATFORM_LOGIN_SIGNATURES`. ⚠️ The linkedin
(``li_at``) and x (``auth_token``) cookie names could NOT be validated against a
live logged-in session when they were written — a wrong name yields a permanent
false ``logged_out``. That is precisely why every consumer treats a login check as
WARN-level (the worker preflight never blocks on it) and why the desktop wizard's
sign-in step offers a Skip. Validate them against a real session before anyone
promotes a login check to fatal.

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
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional
from urllib.parse import urlparse, urlunparse

from .core.bounded import call_bounded
from .core.config import SUPPORTED_PLATFORMS
from .core.logsetup import get_logger

log = get_logger(__name__)

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except Exception:  # pragma: no cover - playwright optional at import time
    sync_playwright = None  # type: ignore
    PLAYWRIGHT_AVAILABLE = False

# 9333, not 9222 (resolved 2026-08, ledger F10). Every live run in this repo, the
# warm_chrome.sh runbook, engines.md §9 and the desktop shell already use 9333; 9222
# survived only as a literal in a handful of Python files. `worker/preflight.py`
# still probes the sibling port and names the drift out loud, so a box provisioned
# either way keeps working — see preflight.resolve_cdp_url.
DEFAULT_CDP_URL = "http://127.0.0.1:9333"
# The only two ports this codebase has ever used for the warmed browser. Two
# candidates, deliberately NOT a port scan: `alternate_cdp_url` maps either onto the
# other so a misconfigured box gets a NAMED error instead of a bare ECONNREFUSED.
_ALTERNATE_CDP_PORTS = (9222, 9333)
# <=60s-old cached result is fine per the API contract; force_refresh (?refresh=1)
# bypasses it.
CACHE_TTL_SEC = 60.0
_CDP_PROBE_TIMEOUT_SEC = 3.0
_ALT_CDP_PROBE_TIMEOUT_SEC = 1.5
# The INTERACTIVE budget: a human (panel banner, `aizu run` pre-flight) is waiting on
# an HTTP request for this answer and a wrong one only costs them a stale badge that
# refreshes in 60s. Deliberately NOT the budget for a probe whose failure PARKS a box —
# see ATTACH_FATAL_BUDGET_SEC.
_LOGIN_PROBE_TIMEOUT_SEC = 5.0
# The budget a caller must allow before a failed browser probe may be treated as PROOF
# the browser is broken — i.e. before `worker/preflight.py` may fail its FATAL
# `cdp_attachable` check on it. Sized off the REAL job this gate stands in front of:
# `core/cdp.py`'s harvest attach passes NO timeout to connect_over_cdp at all and gives
# the whole attach `nav_timeout_ms/1000 + 10s` on its owner thread — 30s at
# CDPBaseConfig's default — with a comment calling those "generous defaults tuned for a
# slow worker PC". This number IS that number. A gate stricter than the
# job it gates parks healthy boxes (F-1): a cold node-driver spawn plus AV scanning on a
# Windows worker routinely runs past five seconds, and a false fatal is the worst
# outcome this whole feature can produce. This is not a latency budget — it is the point
# past which "the browser never answered" is a TRUE statement.
ATTACH_FATAL_BUDGET_SEC = 30.0
# How much of a caller's budget is held back for the CDP round trips that FOLLOW the
# attach (browser.contexts, ctx.pages, ctx.cookies) — a share of it, capped, so a small
# budget still splits sensibly and a large one does not hand the reads 12 idle seconds.
_READ_RESERVE_SHARE = 0.4
_READ_RESERVE_CAP_SEC = 5.0
# How long past the caller's own deadline an abandoned reader may keep its node driver
# alive before we terminate it outright. See "Playwright driver lifetime" below.
_DRIVER_HARD_STOP_GRACE_SEC = 5.0


@dataclass(frozen=True)
class LoginSignature:
    """How to tell, from a cookie jar and a list of open tab URLs, whether the warmed
    browser is signed in to ONE platform.

    ``domains`` is used twice and both uses matter: a session cookie only counts when
    its cookie-domain carries one of them, AND a login-wall URL only counts when the
    URL itself carries one of them. Without that second requirement an open Instagram
    login tab (``/accounts/login/``) would mark X logged out, because a bare "/login"
    substring says nothing about which site it belongs to."""
    domains: tuple
    session_cookie: str
    wall_hints: tuple
    home_url: str


# Mirrors core/cdp.py's CDPFeedBase._login_wall_reason URL signatures.
# ⚠️ instagram's `sessionid` is confirmed against live sessions. linkedin's `li_at`
# and x's `auth_token` are NOT — see the module docstring. Warn-level only.
PLATFORM_LOGIN_SIGNATURES: dict = {
    "instagram": LoginSignature(
        domains=("instagram.com",),
        session_cookie="sessionid",
        wall_hints=("/accounts/login/", "/challenge/"),
        home_url="https://www.instagram.com/"),
    "linkedin": LoginSignature(
        domains=("linkedin.com",),
        session_cookie="li_at",
        wall_hints=("/login", "/checkpoint/", "/uas/login"),
        home_url="https://www.linkedin.com/feed/"),
    "x": LoginSignature(
        domains=("x.com", "twitter.com"),
        session_cookie="auth_token",
        wall_hints=("/i/flow/login", "/login", "/account/access"),
        home_url="https://x.com/home"),
}

# Kept as module constants because they were public-ish and read as documentation of
# the Instagram contract; they are now just the instagram signature's fields.
INSTAGRAM_SESSION_COOKIE = PLATFORM_LOGIN_SIGNATURES["instagram"].session_cookie
_INSTAGRAM_COOKIE_DOMAIN_HINT = PLATFORM_LOGIN_SIGNATURES["instagram"].domains[0]
_LOGIN_WALL_HINTS = PLATFORM_LOGIN_SIGNATURES["instagram"].wall_hints


@dataclass(frozen=True)
class BrowserProbe:
    """The result of ONE bounded ``connect_over_cdp``: did we attach, and what is each
    requested platform's login state.

    ``attached`` is the B6/D3 signal the sidecar never used to check — an HTTP 200 on
    /json/version proves a socket, this proves the browser will actually talk to
    Playwright. ``error`` is an exception TYPE NAME only, never a message: it rides
    the wire up to the fleet console (see worker/preflight.py §5.1) and a message can
    carry a path or a URL with credentials in it.

    Declared HERE rather than in worker/preflight.py on purpose: `preflight` imports
    `readiness`, so the reverse import would be a cycle. preflight re-exports it."""
    attached: bool
    error: Optional[str] = None
    logins: dict = field(default_factory=dict)   # platform -> logged_in|logged_out|unknown


class ReadinessTimeout(Exception):
    """A CDP/Playwright probe exceeded its hard deadline (core.bounded.call_bounded).
    Always caught internally — every probe below degrades to 'unreachable'/'unknown'
    instead of letting this escape to a caller."""


def default_cdp_url() -> str:
    return os.environ.get("AIZU_CDP_URL", DEFAULT_CDP_URL)


def alternate_cdp_url(cdp_url: str) -> Optional[str]:
    """The SIBLING of the two ports this repo has ever warmed Chrome on (9222/9333),
    for the same scheme/host — or None when the URL uses neither. Pure, no I/O.

    This is the whole of the F10 port-ambiguity detection: a box provisioned per the
    warming runbook launches Chrome on 9333 while a sidecar with an inherited 9222
    config probes a dead port and reports "start Chrome" at a box where Chrome is
    already running. Probing exactly one alternate turns that into a named error."""
    try:
        parsed = urlparse(cdp_url or "")
        port = parsed.port
    except ValueError:  # noqa: BLE001 — an unparseable netloc has no sibling
        return None
    if port not in _ALTERNATE_CDP_PORTS:
        return None
    other = _ALTERNATE_CDP_PORTS[0] if port == _ALTERNATE_CDP_PORTS[1] else _ALTERNATE_CDP_PORTS[1]
    host = parsed.hostname or "127.0.0.1"
    # Rebuild from hostname:port rather than string-replacing the port, so a URL
    # carrying userinfo or an IPv6 literal cannot be silently mangled.
    netloc = f"[{host}]:{other}" if ":" in host else f"{host}:{other}"
    return urlunparse((parsed.scheme or "http", netloc, parsed.path, parsed.params,
                       parsed.query, parsed.fragment))


def probe_cdp(cdp_url: str, timeout: float = _CDP_PROBE_TIMEOUT_SEC) -> str:
    """'ok' if the CDP endpoint answers /json/version, else 'unreachable'. Cheap HTTP
    check (borrowed from worker.chrome_manager's own pre-check) — never Playwright,
    never raises. 'ok' means SOMETHING listens, NOT that it is attachable (B6/D3)."""
    from .worker.chrome_manager import _default_http_probe
    return "ok" if _default_http_probe(cdp_url, timeout) else "unreachable"


def _classify_login_state(page_urls: Iterable[str], cookies: Iterable[dict], *,
                          platform: str = "instagram",
                          now: Optional[float] = None) -> str:
    """Pure classifier (no I/O) so cookie-present/absent/expired and login-wall-URL
    logic is unit-testable with zero Playwright. A tab already on the platform's
    login/challenge wall forces logged_out even over an as-yet-unexpired cookie — the
    wall itself is the stronger signal (mirrors core/cdp.py's own _login_wall_reason
    check).

    A wall-URL hit ALSO requires the URL to carry that platform's own domain: the
    warmed browser holds every platform's tabs at once, and "/login" alone would let
    an open LinkedIn login tab declare X logged out. Returns 'unknown' — never a
    verdict — for a platform we have no signature for."""
    signature = PLATFORM_LOGIN_SIGNATURES.get(platform)
    if signature is None:
        return "unknown"
    # Everything read below comes off a LIVE browser, so it is coerced rather than
    # trusted: `str(...)` on a tab URL and the try/except around the expiry compare are
    # not defensive noise, they are what keeps probe_browser's "never raises" contract
    # true. A TypeError escaping here would 500 the panel's readiness banner and, on a
    # worker, demote a FATAL cdp_attachable check into a warning via preflight's
    # _guarded — i.e. a junk cookie would quietly unblock a box.
    for url in page_urls:
        text = str(url or "")
        if not any(domain in text for domain in signature.domains):
            continue
        if any(hint in text for hint in signature.wall_hints):
            return "logged_out"
    now_v = time.time() if now is None else now
    for cookie in cookies:
        if not isinstance(cookie, dict) or cookie.get("name") != signature.session_cookie:
            continue
        domain = str(cookie.get("domain") or "")
        if not any(hint in domain for hint in signature.domains):
            continue
        expires = cookie.get("expires")
        # Playwright reports -1 for a session cookie (no explicit expiry) — that is
        # NOT "already expired", just "expires when the browser session ends".
        if expires is None:
            return "logged_in"
        try:
            if expires < 0 or expires > now_v:
                return "logged_in"
        except TypeError:
            # An unorderable expiry (a string, say) tells us nothing either way. Treat
            # it like a session cookie rather than dropping the match: the cookie IS
            # present on the right domain, and inventing a logged_out from a value we
            # could not read is the false-red this module keeps warning about.
            return "logged_in"
    return "logged_out"


def _attach_timeout_sec(budget: float) -> float:
    """How long ``connect_over_cdp`` itself may take, out of a caller's TOTAL budget of
    ``budget`` seconds. Always strictly less than the budget.

    F-1: what shipped passed the caller's whole budget straight through as the attach's
    own ``timeout=``, so the inner deadline EQUALLED the outer one. An attach that lands
    at 4.9s of a 5.0s budget then leaves 0.1s for ``browser.contexts``/``ctx.pages``/
    ``ctx.cookies()`` — three more CDP round trips — the OUTER deadline fires instead,
    and a slow-but-perfectly-alive Chrome scores identically to a dead one. On the
    worker that difference is a FATAL ``cdp_attachable`` and a parked box.

    This is a SPLIT, not a shortening: the attach ends up with MORE absolute time than
    it had, because the budget it is now a fraction of is the one sized for the job
    (:data:`ATTACH_FATAL_BUDGET_SEC`) rather than a five-second guess."""
    reserve = min(_READ_RESERVE_CAP_SEC, max(budget, 0.0) * _READ_RESERVE_SHARE)
    return max(budget - reserve, 0.0)


def _quietly(step: Callable[[], object]) -> None:
    """Run a teardown step whose failure must never become the caller's answer.

    A ``browser.close()``/``pw.stop()`` that throws on the way out of a SUCCESSFUL read
    would propagate to :func:`probe_browser`, come back as ``attached=False``, and park
    a box over a browser that just answered every question we asked it. Same posture as
    ``core/cdp.py``'s attach cleanup: teardown must not mask the real result."""
    try:
        step()
    except BaseException:  # noqa: BLE001 — teardown must not mask the real result
        log.debug("playwright teardown step did not complete cleanly", exc_info=True)


# ---- Playwright driver lifetime (F-7) ----
#
# ``call_bounded`` hands a caller a deadline, not a kill switch: Python cannot stop a
# thread mid-syscall, so on expiry the reader thread is ABANDONED and keeps the node
# driver it spawned — a wedged CDP pipe can hang ``ctx.cookies()`` with no exception
# ever raised (see core/bounded.py). That was survivable while a probe ran once; the
# worker's ``_park_for_preflight`` re-probes every 30s for as long as the box stays
# parked, which turns one abandoned driver into ~120 orphaned node processes an hour on
# a machine nobody can SSH into.
#
# The fix cannot live in ``call_bounded``: a generic bounder has no idea what its
# callable is holding. It lives here, with the code that OWNS the driver. The reader
# arms a wall-clock hard stop BEFORE it spawns anything, and that timer terminates the
# driver process even when the caller walked away minutes earlier.
#
# Killing the driver is legal from any thread — it is a signal to a subprocess, not a
# Playwright call, so pw_owner.py's thread-affinity rule (ledger D6) does not apply —
# and it is also what UNWEDGES the reader: every pending Playwright call awaits
# ``transport.on_error_future`` alongside its own reply, so a dead driver resolves it
# with "Connection closed while reading from the driver", the reader unwinds through its
# own ``finally``, and the thread is reclaimed too. Verified live against a silent TCP
# peer (playwright 1.62): thread unwound, ``pw.stop()`` returned cleanly and instantly,
# no orphaned process, no CPU spin.

def _driver_pid(manager) -> Optional[int]:
    """The node driver subprocess PID behind a started Playwright manager, or None.

    Reaches through Playwright privates deliberately and defensively: there is no public
    handle on the driver process, and the alternative is the leak above. Every step is
    guarded, so a Playwright release that renames any of these degrades to "no hard
    stop" — today's behaviour — rather than to an exception on a timer thread with
    nobody to catch it. Checked against playwright 1.62's ``PipeTransport._proc``."""
    try:
        pid = int(manager._connection._transport._proc.pid)  # noqa: SLF001
    except Exception:  # noqa: BLE001 — no handle just means no hard stop
        return None
    return pid if pid > 0 else None


def _hard_stop_driver(manager, finished: threading.Event) -> bool:
    """Terminate the node driver an abandoned reader is still stuck behind. Returns True
    only when a signal was actually sent. Never raises."""
    if finished.is_set():
        # The reader got there first and is tearing its own driver down. Skipping is
        # also what keeps this from ever signalling a RECYCLED pid: we only fire while
        # the owning thread is provably still inside the read.
        return False
    pid = _driver_pid(manager)
    if pid is None:
        return False
    try:
        # SIGTERM, not SIGKILL: the node driver exits on it (verified live), and on
        # Windows every signal except CTRL_C_EVENT/CTRL_BREAK_EVENT routes to
        # TerminateProcess anyway, so this is the one spelling that works on both.
        os.kill(pid, signal.SIGTERM)
    except Exception:  # noqa: BLE001 — already gone, or not ours to signal
        log.debug("playwright driver hard stop could not signal pid %s", pid,
                  exc_info=True)
        return False
    log.warning("Playwright driver pid %s outlived its hard stop after a wedged CDP "
                "read — terminated so an abandoned probe cannot leak it", pid)
    return True


def _arm_driver_hard_stop(manager, after_sec: float,
                          finished: threading.Event) -> threading.Timer:
    """Schedule :func:`_hard_stop_driver`. Daemon, so the hard stop itself can never be
    the thing that holds up interpreter shutdown."""
    timer = threading.Timer(max(after_sec, 0.0), _hard_stop_driver,
                            args=(manager, finished))
    timer.daemon = True
    timer.name = "pw-driver-hard-stop"
    timer.start()
    return timer


def read_browser_state(cdp_url: str, timeout: float) -> tuple:
    """The real Playwright attach + read: (page_urls, cookies) for the WHOLE browser,
    platform-agnostic. Always run this through call_bounded — never call it directly
    off the probing thread, since connect_over_cdp/cookies() are exactly the calls that
    can wedge (TASK A).

    ``timeout`` is the caller's WHOLE wall-clock budget, not the attach's: the attach
    gets a strict fraction of it (:func:`_attach_timeout_sec`) so a slow-but-alive
    Chrome still has time left for the reads that follow. It is also the clock for this
    call's own hard stop — the one thing that survives ``call_bounded``'s caller walking
    away, and the reason a re-probing worker cannot leak a driver per tick.

    One attach serves every platform: the cookie jar and the tab list are read once
    and classified N times, so checking three platforms costs one CDP connection, not
    three (this codebase allows exactly one)."""
    manager = sync_playwright()
    finished = threading.Event()
    # Armed BEFORE start(): the driver subprocess is spawned inside start(), so a spawn
    # that never finishes its handshake has to be killable too.
    hard_stop = _arm_driver_hard_stop(
        manager, timeout + _DRIVER_HARD_STOP_GRACE_SEC, finished)
    pw = manager.start()
    try:
        browser = pw.chromium.connect_over_cdp(
            cdp_url, no_defaults=True, timeout=_attach_timeout_sec(timeout) * 1000)
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
            # our connection only — never the warmed browser itself
            _quietly(browser.close)
    finally:
        finished.set()
        hard_stop.cancel()
        _quietly(pw.stop)


def probe_browser(cdp_url: str, platforms: Iterable[str],
                  timeout: float = _LOGIN_PROBE_TIMEOUT_SEC, *,
                  read_state: Optional[Callable[[], tuple]] = None) -> BrowserProbe:
    """ONE bounded connect_over_cdp → (attached?, per-platform login state). Never
    raises: a wedged CDP pipe (the exact TASK A failure mode), a refused attach (the
    B6/D3 degraded-Chrome case) or a missing Playwright all come back as
    ``attached=False`` with an exception TYPE NAME and every platform 'unknown'.

    ``timeout`` is the WHOLE budget for attach + reads, and it is the only thing that
    separates "the browser is broken" from "the browser was slow". A caller that treats
    ``attached=False`` as FATAL must pass :data:`ATTACH_FATAL_BUDGET_SEC` — the default
    here is the interactive one, sized for a human waiting on an HTTP response.

    ``read_state`` is the injectable seam for tests — a zero-arg callable returning
    (page_urls, cookies); defaults to a real bounded Playwright attach."""
    wanted = tuple(platforms or ())
    if read_state is not None:
        reader = read_state
    elif PLAYWRIGHT_AVAILABLE:
        reader = lambda: read_browser_state(cdp_url, timeout)  # noqa: E731
    else:
        # Not a failure of the box — the CALLER decides what a missing Playwright
        # means (worker preflight reports it as its own warn-level check and never
        # fails cdp_attachable on it).
        return BrowserProbe(attached=False, error="PlaywrightUnavailable",
                            logins={p: "unknown" for p in wanted})
    try:
        page_urls, cookies = call_bounded(reader, timeout, timeout_exc=ReadinessTimeout)
    except Exception as e:  # noqa: BLE001 — any probe failure/timeout, never raise
        return BrowserProbe(attached=False, error=type(e).__name__,
                            logins={p: "unknown" for p in wanted})
    # Classification is guarded SEPARATELY from the attach, because the two failures
    # mean opposite things: the attach failing is B6/D3 (attached=False, the fatal
    # signal), whereas a classifier that chokes on unreadable browser state must still
    # report attached=True — the browser talked to us, we just could not tell who is
    # signed in. Collapsing the two would turn a cosmetic parse problem into a fatal
    # cdp_attachable and park a working box.
    try:
        logins = {p: _classify_login_state(page_urls, cookies, platform=p)
                  for p in wanted}
    except Exception:  # noqa: BLE001 — unreadable state is 'unknown', never a verdict
        log.debug("login classification failed for %s", cdp_url, exc_info=True)
        logins = {p: "unknown" for p in wanted}
    return BrowserProbe(attached=True, logins=logins)


def probe_instagram_login(cdp_url: str, timeout: float = _LOGIN_PROBE_TIMEOUT_SEC, *,
                          read_state: Optional[Callable[[], tuple]] = None) -> str:
    """'logged_in' | 'logged_out' | 'unknown'. The original single-platform façade —
    signature deliberately unchanged so server.py / cli.py / check_readiness need no
    edit; it is now a one-platform call into :func:`probe_browser`."""
    return probe_browser(cdp_url, ("instagram",), timeout,
                         read_state=read_state).logins.get("instagram", "unknown")


def _default_open_login_tab(cdp_url: str, timeout: float, platform: str) -> bool:
    """Same driver-lifetime and split-budget rules as :func:`read_browser_state` — this
    is the other place in this module that spawns a Playwright driver on a thread its
    caller may abandon, so it leaks exactly the same way if it is left out."""
    if not PLAYWRIGHT_AVAILABLE:
        return False
    signature = PLATFORM_LOGIN_SIGNATURES.get(platform)
    if signature is None:
        return False
    deadline = time.monotonic() + timeout
    manager = sync_playwright()
    finished = threading.Event()
    hard_stop = _arm_driver_hard_stop(
        manager, timeout + _DRIVER_HARD_STOP_GRACE_SEC, finished)
    pw = manager.start()
    try:
        browser = pw.chromium.connect_over_cdp(
            cdp_url, no_defaults=True, timeout=_attach_timeout_sec(timeout) * 1000)
        try:
            contexts = browser.contexts
            ctx = contexts[0] if contexts else browser.new_context()
            for page in ctx.pages:
                try:
                    url = page.url or ""
                    if any(domain in url for domain in signature.domains):
                        page.bring_to_front()
                        return True
                except Exception:  # noqa: BLE001 — a dead page just isn't the one
                    continue
            page = ctx.new_page()
            # What is actually LEFT of the budget, not a second full copy of it: the
            # attach already spent some of it, and `goto` is the last step before the
            # outer call_bounded deadline would fire anyway. Floored so a budget already
            # blown does not turn into `timeout=0` (Playwright reads that as "no
            # timeout", i.e. wait forever — the opposite of what we mean).
            remaining = max(deadline - time.monotonic(), 0.5)
            page.goto(signature.home_url, wait_until="domcontentloaded",
                      timeout=remaining * 1000)
            return True
        finally:
            _quietly(browser.close)
    finally:
        finished.set()
        hard_stop.cancel()
        _quietly(pw.stop)


def open_login_tab(cdp_url: str, timeout: float = _LOGIN_PROBE_TIMEOUT_SEC, *,
                   platform: str = "instagram",
                   opener: Optional[Callable[[], bool]] = None) -> bool:
    """Open (or focus) a Chrome tab on ``platform``'s home so a human can log in —
    shared by POST /api/agent/launch-login, the CLI, and the desktop wizard's sign-in
    step (control-surface ``openLoginTab``). ``platform`` is keyword-only with the old
    default so every existing call site is byte-identical. Bounded + never raises;
    False just means it could not be opened (callers degrade gracefully, they don't
    treat it as fatal)."""
    op = opener or (lambda: _default_open_login_tab(cdp_url, timeout, platform))
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
        # F10: the CLI/panel half of the port decision. A human is reading this
        # message, so — unlike the unattended worker, which adopts the sibling port —
        # we only DETECT and NAME the drift and let them fix the config. One extra
        # short probe, and only on the failure path.
        alternate = alternate_cdp_url(cdp_url)
        if alternate and probe_cdp_fn(alternate, _ALT_CDP_PROBE_TIMEOUT_SEC) == "ok":
            detail += (f" — but a CDP endpoint IS answering at {alternate}. Set "
                       f"AIZU_CDP_URL={alternate} to match the Chrome you started.")
    elif instagram_state != "logged_in":
        detail = f"instagram session is {instagram_state}"
    result = {"ready": ready, "cdp": cdp_state, "instagram": instagram_state,
              "checkedAt": now, "cdpUrl": cdp_url, "detail": detail}
    with _lock:
        _cache = dict(result)
    return result


# ---- the same contract, derived from the worker fleet ----

def _normalised_platforms(platforms: Iterable) -> tuple:
    """The caller's requested platforms, trimmed, lowercased and with blanks dropped.

    Applied to the REQUEST side only, never to a worker's advertised capabilities: those
    are matched against `SUPPORTED_PLATFORMS` with exactly the rules
    ``store._job_capability_covers`` uses, and quietly repairing a capability here would
    let this function count a box the lease scan would then refuse — the "banner
    promises work nothing can place" lie the capability filter exists to stop."""
    out = []
    for entry in platforms or ():
        # Falsy entries are dropped BEFORE str(): `str(None)` is "none", a perfectly
        # plausible-looking platform name that matches nothing and narrows the fleet to
        # zero — a caller passing [None] means "no platform in particular".
        if not entry:
            continue
        try:
            name = str(entry).strip().lower()
        except Exception:  # noqa: BLE001 — an unstringable element is just not a platform
            continue
        if name:
            out.append(name)
    return tuple(out)


def _advertises_a_usable_platform(worker: dict,
                                  platforms: Optional[frozenset]) -> bool:
    """Does this worker declare at least one capability the dispatcher could ever
    match? A capability is ``[org, platform, handle]`` — the SAME shape
    ``store._job_capability_covers`` requires, and a shape that matcher would skip
    must not count as ready here either, or the banner promises work the lease scan
    can never place. ``platforms``, when given, narrows it to the platforms a caller
    actually cares about."""
    for entry in worker.get("capabilities") or []:
        if not isinstance(entry, (list, tuple)) or len(entry) != 3:
            continue
        platform = entry[1]
        if platform not in SUPPORTED_PLATFORMS:
            continue
        if platforms is not None and platform not in platforms:
            continue
        return True
    return False


def _preflight_is_blocking(worker: dict) -> bool:
    """True only when the box itself reported a BLOCKING preflight. A worker dict with
    no "preflight" key (or an explicit null) counts as fine — that is a pre-upgrade
    sidecar that never learned to report one, and treating silence as failure would
    dark an entire existing fleet the moment this shipped."""
    reported = worker.get("preflight")
    if not isinstance(reported, dict):
        return False
    return bool(reported.get("blocking"))


def fleet_readiness(workers: Iterable[dict], *, platforms: Optional[Iterable[str]] = None,
                    now: Optional[float] = None) -> dict:
    """The readiness contract for the DISTRIBUTED backend, where the cloud control
    plane has no browser of its own (distributed-workers PRD §2: PULL, never remote
    CDP). Probing this process's CDP would then always answer 'unreachable' and say
    nothing true about whether a live run can start — what actually gates a run here
    is whether any worker PC is online AND ABLE to lease the job.

    Ready iff at least one non-revoked, 'online' worker (a) advertises at least one
    supported platform (narrowed by ``platforms`` when given) and (b) is not parked by
    its own launch preflight.

    The capability requirement is ledger F9.2, and it is the reason this function was
    lying: ``worker/config.py``'s ``_parse_capabilities_env`` returns () when neither
    AIZU_WORKER_PLATFORMS nor AIZU_WORKER_CAPABILITIES is set, so a box registers with
    ``capabilities: []``, can NEVER be leased to — and used to flip this banner from
    an accurate ready:false to a false ready:true, which is strictly worse than no
    banner. Counting presence alone answered "is a PC switched on", not "can a run
    start".

    ``instagram`` stays 'unknown' on purpose: each box's login state lives on that box
    and the worker preflight reports its login checks as warn-level precisely because
    the linkedin/x cookie signatures are unvalidated — promoting them into a
    tenant-facing ready verdict would gate real runs on a guess. Callers read
    ``ready``; ``detail`` carries the human explanation.
    """
    # An EMPTY narrowing collapses to no narrowing (`or None`): a caller that passed
    # [] — or a list of blanks — is saying "no platform in particular", not "narrow to
    # nothing". Without this it silently answers ready:false for every fleet and
    # renders a dangling " for " in the operator's detail string.
    #
    # Names are normalised on the way in because the two sides are authored in different
    # places: the narrowing comes from a campaign brief a human wrote, the capability
    # comes from AIZU_WORKER_PLATFORMS on the box, and both land here as bare strings
    # compared with `in`. A stray " Instagram" would match nothing, and the failure mode
    # of a missed match is a false ready:false that dark-banners a healthy fleet.
    wanted = (frozenset(_normalised_platforms(platforms)) or None
              ) if platforms is not None else None
    online = [w for w in workers
              if w.get("status") == "online" and not w.get("revokedAt")]
    capable = [w for w in online if _advertises_a_usable_platform(w, wanted)]
    usable = [w for w in capable if not _preflight_is_blocking(w)]
    checked_at = now if now is not None else time.time()
    scope = "" if wanted is None else f" for {', '.join(sorted(wanted))}"
    if usable:
        detail = (f"{len(usable)} worker(s) online and able to take work{scope} — live "
                  "runs are dispatched to the fleet, which drives Chrome on the "
                  "worker PC")
    elif capable:
        detail = (f"{len(capable)} worker(s) online{scope} but every one is parked by "
                  "its own launch preflight — open the fleet console for the failing "
                  "check and its remedy")
    elif online:
        detail = (f"{len(online)} worker(s) online but none advertises a platform the "
                  f"fleet can dispatch{scope} — a job would be queued with nothing "
                  "able to lease it. Set AIZU_WORKER_PLATFORMS on the box (Setup → "
                  "Platforms in the desktop app) and restart the worker.")
    else:
        detail = ("no worker is online — a live run would be queued with nothing to "
                  "pick it up. Start a worker (aizu-worker) on a warmed box.")
    return {"ready": bool(usable), "cdp": "ok" if usable else "unreachable",
            "instagram": "unknown", "checkedAt": checked_at,
            "cdpUrl": "", "detail": detail}
