"""Shared CDP harness — Playwright-over-CDP attach + interception plumbing.

Three platforms read their site's own internal JSON traffic over CDP — Instagram,
X, and LinkedIn (PRD §3, §5 of each). The generic mechanics are identical and live
here so each engine writes only what is platform-specific; engines still "share
only ``aizu.core``" (the locked per-engine architecture), and ``core`` is that
shared layer.

This base owns the parts that never differ by platform:

  - attach to a warmed, logged-in Chrome over CDP (never launch a fresh browser),
  - listen to the page's own responses, hint-filter + JSON-guard them, set the
    empty-interception canary, and delegate the actual routing to ``_classify``,
  - drive human-like scrolling and walk campaign-defined seed sources,
  - screenshot a frame for the vision/OCR tier,
  - back the empty-interception canary via ``healthy()``.

A subclass supplies only: ``_url_hints()`` (cheap pre-filter substrings),
``_classify(url, body, response)`` (shape-based routing into the queues),
``_sources()`` (discovery URLs to walk), ``open_reel()`` (open one item full-screen
so its comments load), and ``fetch_comments()``.

URL hints are a cheap pre-filter only — detection is by response *shape* in each
engine's pure ``parsers`` module, so a drifted endpoint still parses. Confirm the
current endpoints in DevTools once and expect drift (every CDP PRD §12/§13).
"""
from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

from .feed import Comment, FeedSource, Reel
from .human import HumanSim
from .logsetup import get_logger
# PlaywrightTimeout is DEFINED in pw_owner (so core/human.py can raise the same
# class without importing this module) and re-exported here: engines and tests
# import it from aizu.core.cdp, and that must keep resolving to one class.
from .pw_owner import OwnedPW, PlaywrightOwner, PlaywrightTimeout  # noqa: F401
# HaltSession is defined in engines/base.py (not any one engine) so every
# engine + the control plane share one exception type; core imports it ONLY
# for this one raise site (walk()'s login/challenge-wall detection below) —
# engines still depend on core, not the other way around, and engines/base.py
# itself has no core dependency, so this stays a one-way edge, not a cycle.
from ..engines.base import HaltKind, HaltSession

log = get_logger(__name__)

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except Exception:  # pragma: no cover - playwright optional at import time
    sync_playwright = None  # type: ignore
    PLAYWRIGHT_AVAILABLE = False


@dataclass
class CDPBaseConfig:
    """Platform-agnostic CDP knobs. Engine configs subclass this to add their own
    URL hints (and any platform-specific tuning)."""
    # 9333 is the canonical warmed-Chrome port repo-wide (ledger F10). The literal is
    # duplicated from readiness.DEFAULT_CDP_URL on purpose: importing `readiness` from
    # `core/` would invert the dependency direction (readiness already reaches INTO core).
    cdp_url: str = "http://127.0.0.1:9333"
    scroll_delta: int = 900
    settle_seconds: float = 1.5          # wait for interception after a scroll
    empty_scrolls_before_stop: int = 4   # no new items after N scrolls => source end
    max_comment_scrolls: int = 3         # paginate the comment list this many times
    # Multi-source discovery (works on any account, not just a warmed feed).
    seed_hashtags: tuple[str, ...] = ()
    seed_accounts: tuple[str, ...] = ()
    include_home_feed: bool = True
    per_source_reels: int = 12           # items to pull from each source before moving on
    nav_settle_seconds: float = 2.5      # wait after navigating to a new source
    nav_timeout_ms: int = 20000          # cap page.goto so a hung nav can't wedge a source
    # Per-call Playwright timeouts. GENEROUS defaults tuned for a slow worker PC on
    # real Instagram — a frozen CDP command must degrade, not wedge forever, but a
    # merely-slow one must not false-timeout. js_timeout_ms is the PAGE default
    # AND the hard wall-clock bound the Playwright owner thread
    # (`core/pw_owner.py`) enforces around every call that reaches it.
    #
    # CORRECTION (was wrong here before): js_timeout_ms does NOT actually bound
    # every evaluate/query_selector/screenshot/mouse.* call. Confirmed live
    # against a wedged CDP session (Playwright 1.61/1.62): `page.mouse.move` /
    # `mouse.wheel` / `mouse.click` take no `timeout=` argument and do not honor
    # the page default either, and once the CDP pipe goes quiet (e.g. the tab
    # got redirected to a login wall) even an `evaluate` call can hang with NO
    # exception ever raised — set_default_timeout has zero effect on that case.
    # This was the confirmed root cause of a session wedged forever with no
    # exception to trigger any except-guard, per-source budget, or crash guard.
    # The real deadline now comes from `core/pw_owner.py`: Playwright is created
    # and driven on ONE owner thread, callers submit closures and wait with a
    # bound, so the call never changes threads (the mistake that silently zeroed
    # every harvest — ledger D6) and only the WAIT crosses the boundary. It
    # reaches BOTH the explicitly wrapped call sites (`_call_bounded`:
    # `_wheel_once`'s mouse.wheel + evaluate fallback, `_click_centermost`'s
    # mouse.click, instagram/cdp.py's context.cookies, core/human.py's
    # mouse_move) AND — via the `OwnedPW` proxy that `attach()` installs on
    # self._pw/_browser/_page — every other attribute read and call on a live
    # Playwright object, including `_shoot`'s query_selector/screenshot, which
    # were never actually bounded before despite comments here claiming so.
    js_timeout_ms: int = 15000           # page-default cap; also the owner-thread deadline
    per_reel_seconds: float = 90.0       # session-level per-reel wall-clock backstop
    # Hard per-source wall-clock ceiling: no single source can hold the walk longer
    # than this, regardless of scroll/interception behavior (the anti-wedge guarantee).
    max_source_seconds: float = 45.0
    # How many CONSECUTIVE bounded Playwright calls may fail to come back before
    # walk() halts the whole session. The owner-thread deadline turns "hangs
    # forever" into "every call degrades", and every degrade path here is written
    # as skip-and-continue — so without this the walk finishes normally, yields
    # nothing, and the run is recorded as a clean, successful, zero-lead session
    # (indistinguishable from "the seed hashtags were dry") while Chrome was dead
    # the whole time. That is ledger B6's exact complaint and D6's failure shape.
    # 0 disables the halt. See CDPFeedBase._halt_if_owner_wedged.
    max_consecutive_wedged_calls: int = 3
    # Engagement timing (human-like pause before a click). Read-only engines unused.
    action_delay_min: float = 0.8
    action_delay_max: float = 2.5


class CDPFeedBase(FeedSource):
    """Template-method base for a Playwright-over-CDP feed.

    Owns the browser lifecycle, the response-interception buffer, the seed-source
    walk, screenshots, and the canary. Subclasses implement the platform-specific
    hooks (``_url_hints``/``_classify``/``_sources``/``open_reel``/``fetch_comments``).
    """

    def __init__(self, cfg: Optional[CDPBaseConfig] = None,
                 clock: Callable[[], float] = time.monotonic,
                 human: Optional[HumanSim] = None):
        self.cfg = cfg or CDPBaseConfig()
        # Monotonic source of truth for the per-source budget; injectable so tests
        # can drive the wall-clock cap deterministically without real sleeping.
        self._clock = clock
        # Micro human-sim layer (jittered scroll/nav/click). Default reads
        # HUMAN_SIM/HUMAN_SIM_SPEED at construction, like Pacer. The pre-click
        # "engage" pause keeps CDPBaseConfig.action_delay_min/max as its window so
        # that existing knob still tunes it (now triangular instead of flat).
        self.human = human or HumanSim()
        self.human.cfg.ranges["engage"] = (
            self.cfg.action_delay_min, self.cfg.action_delay_max)
        # Wire the same hard deadline `_call_bounded` uses onto HumanSim's own
        # unbounded call sites (mouse_move's evaluate + mouse.move) — one knob
        # (cfg.js_timeout_ms) governs every otherwise-unbounded Playwright call.
        self.human.call_timeout_s = self.cfg.js_timeout_ms / 1000.0
        # The single thread that owns this feed's Playwright world (see
        # core/pw_owner.py). Lazily started on the first bounded call; a feed
        # that never attaches never spawns a thread. HumanSim must share it —
        # a HumanSim handed this feed's page but NOT this feed's owner would
        # submit work to a second thread and hit greenlet.error, so the
        # injection below is load-bearing, not tidiness.
        self._owner = PlaywrightOwner()
        self.human.owner = self._owner
        # Guards EVERY interception accumulator below, not just the reel queue:
        # `page.on("response")` handlers dispatch on the PLAYWRIGHT OWNER thread
        # now, so `_classify` writes there while `walk()`/`fetch_comments()` read
        # on the caller's. Before the owner thread there was one thread and the
        # reads were atomic by construction; the window is real but narrow —
        # events only dispatch while the caller is inside a Playwright call, or
        # after it has ABANDONED one (a deadline expiry: the caller walks away
        # and the owner keeps pumping the dispatcher). That second case is the
        # dangerous one, because it is exactly when `fetch_comments` runs a
        # non-atomic "slice the list, then read its length" and can return N
        # comments while persisting a watermark of N+k — the k in between are
        # never scored and never re-read. Engine `_classify`/`fetch_comments`
        # take this same lock; keep any new interception state under it too.
        self._queue_lock = threading.Lock()
        self._pw = None
        self._browser = None
        self._page = None
        # Discovery scrolls self._page; per-item interaction (comments, frames)
        # happens full-screen on a dedicated interaction page so it does not
        # disturb the discovery scroll position.
        self._ipage = None

        # Interception accumulators (subclass `_classify` fills these).
        self._reel_queue: list[Reel] = []
        self._seen_reel_ids: set[str] = set()
        self._comments_by_reel: dict[str, list[Comment]] = {}
        self._comment_cursor_by_reel: dict[str, Optional[str]] = {}
        # canary: did we parse any hinted response since the last health check?
        self._saw_data = False

    # ---- platform hooks (subclass MUST implement) ----
    def _url_hints(self) -> tuple[str, ...]:
        """URL substrings that hint a response is worth parsing (cheap pre-filter)."""
        raise NotImplementedError

    def _classify(self, url: str, body, response) -> None:
        """Route one intercepted (already JSON-parsed) response into the queues."""
        raise NotImplementedError

    def _sources(self) -> list[str]:
        """URLs to walk this session (home/feed + seed hashtags + seed accounts)."""
        raise NotImplementedError

    def _source_redirected(self, requested_url: str, landed_url: str) -> bool:
        """True when a source landed somewhere with no reels to harvest (e.g. IG's
        keyword-search fallback), so the walk should skip it FAST instead of
        burning the whole empty-scroll budget. Base default: never redirected;
        engines override with their platform-specific fallback signature."""
        return False

    def _login_wall_reason(self, landed_url: str) -> Optional[tuple[str, HaltKind]]:
        """(reason, HaltKind-value) when ``landed_url`` shows the platform booted
        the tab to a login/checkpoint wall mid-walk — a session cannot keep
        harvesting from there, so the caller (``walk()``) halts entirely instead
        of just skipping this one source. Base default: no signature (a platform
        with no known login-wall URL yet); engines override with their own."""
        return None

    # ---- attach / lifecycle ----
    def attach(self) -> None:
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "Playwright is not installed. `pip install playwright` (the "
                "engine attaches over CDP; it does not download a browser).")
        log.info("CDP attaching to warmed Chrome · url=%s", self.cfg.cdp_url)

        def _do():
            """EVERYTHING Playwright runs on the owner thread — including the
            driver start itself. That is what makes the owner the *owning*
            thread for the driver, browser, context, page and every handle they
            later hand out, instead of merely a thread we shove calls at (which
            is `greenlet.error` 100% of the time — ledger D6)."""
            pw = sync_playwright().start()
            # PUBLISH THE DRIVER HANDLE BEFORE ANYTHING CAN FAIL. `start()` has
            # already spawned a node subprocess; if `pw` stays a local, then the
            # explicitly-expected "No browser context" error (or any connect
            # failure) drops the only reference to it while `self._pw` is still
            # None, so `close()` stops nothing and every failed attach in a
            # `run-all` loop leaves an orphaned driver behind.
            self._pw = self._wrap_pw(pw)
            try:
                return _connect(pw)
            except BaseException:
                # Release the driver HERE — we are on its owning thread, which
                # is the only place `stop()` is legal — instead of leaving it to
                # a `close()` that may never be reached.
                try:
                    pw.stop()
                except BaseException:  # noqa: BLE001 — teardown must not mask the real error
                    log.debug("CDP attach cleanup: pw.stop() did not complete cleanly")
                self._pw = None
                raise

        def _connect(pw):
            # Attach to the already-running, warmed Chrome. Never launch vanilla.
            # no_defaults=True makes Playwright SKIP its on-connect Browser.setDownloadBehavior
            # call (driver: acceptDownloads→"internal-browser-default"). That single CDP command
            # is what Chrome for Testing 148 rejects — "Browser context management is not
            # supported" — once the browser has been up a few minutes / a couple of
            # connect+disconnect cycles, killing every attach. We never download anything (this
            # is a read-only observer), so skipping it is free and sidesteps the failure entirely
            # — no kill/relaunch dance, works even against an already-"degraded" instance.
            # Verified live 2026-07-01. See microsoft/playwright#30383.
            browser = pw.chromium.connect_over_cdp(self.cfg.cdp_url, no_defaults=True)
            contexts = browser.contexts
            if not contexts:
                raise RuntimeError("No browser context over CDP — is the warmed "
                                   "Chrome running with --remote-debugging-port?")
            ctx = contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.on("response", self._on_response)
            # Per-call timeout so a frozen CDP command degrades instead of blocking the
            # main greenlet forever. Covers evaluate/query_selector/screenshot — but NOT
            # mouse.* (no timeout= arg, and not honored via the page default either) and
            # not reliably every evaluate once the CDP pipe itself goes quiet. Those are
            # covered instead by the owner thread's own wall-clock deadline, which the
            # OwnedPW wrapping below applies to every call on these objects.
            page.set_default_timeout(self.cfg.js_timeout_ms)
            page.set_default_navigation_timeout(self.cfg.nav_timeout_ms)
            return browser, page, len(contexts)

        # Attach gets nav_timeout + slack: starting the node driver and the CDP
        # handshake are both slower than any ordinary page call.
        browser, page, n_ctx = self._owner.call(
            _do, self.cfg.nav_timeout_ms / 1000.0 + 10.0, PlaywrightTimeout)
        # INVARIANT: no RAW Playwright object may ever be stored on self. Every
        # one of them is thread-affine to the owner; a raw reference that escapes
        # here is a silent zero-harvest waiting to happen (a missed call raises
        # greenlet.error straight into an `except Exception: return False/None`).
        # tests/core/test_cdp.py has a mechanical regression test for this.
        # (self._pw was already published inside _do, before anything could fail.)
        self._browser = self._wrap_pw(browser)
        self._page = self._wrap_pw(page)
        log.success("CDP attached · %d context(s), interception wired", n_ctx)
        # walk() drives per-source navigation; don't hard-land on the home feed.

    def _wrap_pw(self, obj):
        """Bind a Playwright object to this feed's owner thread + deadlines."""
        return OwnedPW(
            self._owner, obj,
            {"default": self.cfg.js_timeout_ms / 1000.0,
             # Navigation calls carry Playwright's OWN timeout internally
             # (_goto_once passes timeout=nav_timeout_ms), so their queue
             # deadline must be strictly LARGER or we manufacture the wedges we
             # then recover from.
             "nav": self.cfg.nav_timeout_ms / 1000.0 + 5.0},
            PlaywrightTimeout)

    def _ensure_ipage(self):
        """Lazily open the interaction page (a second tab) and wire interception
        so comment responses on the opened item are captured too.

        No owner plumbing here on purpose: ``self._browser`` is already an
        ``OwnedPW``, so ``contexts``/``new_page()`` dispatch to the owner thread
        and hand back an already-wrapped page for free. (Verified by test, not
        by eye — see test_ensure_ipage_returns_an_owned_proxy_page.)

        RETURNS None INSTEAD OF RAISING, and that is load-bearing. Every caller
        is ``page = self._ensure_ipage(); if page is None: return False`` on the
        FIRST line of a bool-returning ``open_reel``, outside its try/except —
        so anything that escapes here escapes ``open_reel``, then the session's
        ``for reel in feed.walk()`` loop (which only catches ``HaltSession``),
        and kills the whole run instead of skipping one reel. Before the owner
        thread existed that could not happen: ``browser.contexts`` is a plain
        cached property in Playwright, so this method was infallible. Routing it
        through the owner made it a real cross-thread call that can time out or
        fast-fail while the owner is wedged, so the guard has to be here.

        ``PlaywrightThreadAffinityError`` is a ``BaseException`` and still
        escapes on purpose — an object off its owning thread is a code bug, not
        a flaky browser."""
        if self._ipage is None and self._browser is not None:
            try:
                ctx = self._browser.contexts[0]
                ipage = ctx.new_page()
                ipage.on("response", self._on_response)
                # The interaction page is where mouse.wheel/mouse.click run
                # (comment-dialog scroll, engagement clicks); those take NO timeout=
                # argument and do NOT honor this page default. The owner thread's
                # wall-clock deadline (core/pw_owner.py), applied through the proxy
                # and through `_call_bounded`, is what actually keeps them from
                # hanging the run indefinitely — this default only covers the
                # evaluate/query_selector/screenshot calls on this page.
                ipage.set_default_timeout(self.cfg.js_timeout_ms)
                ipage.set_default_navigation_timeout(self.cfg.nav_timeout_ms)
            except Exception as e:  # noqa: BLE001 — a bool contract, see docstring
                log.warning("CDP could not open the interaction page (%s) — the "
                            "item is skipped, not the run", type(e).__name__)
                return None
            # Publish only once fully wired: a half-built page on self would be
            # reused forever with no interception hook and no timeouts.
            self._ipage = ipage
        return self._ipage

    def _call_bounded(self, fn, timeout_s: Optional[float] = None):
        """Run ``fn`` under a REAL wall-clock deadline, on Playwright's OWNING thread.

        ``fn`` is submitted to this feed's ``PlaywrightOwner`` (``core/pw_owner.py``)
        — the same thread that ran ``sync_playwright().start()`` in ``attach()``,
        i.e. the thread every Playwright object here is affine to. The call
        therefore never changes threads; only the WAIT crosses the boundary. On
        expiry this raises ``PlaywrightTimeout``, so every existing
        ``except PlaywrightTimeout:`` / ``except Exception:`` degrade path at the
        wrapped call sites keeps working unchanged. ``timeout_s=None`` (the
        default) means ``cfg.js_timeout_ms``.

        HISTORY — this is why the trap exists; do not "simplify" it away.
        This once routed ``fn`` through ``core.bounded.call_bounded``, which
        gets its deadline by running the callable on a daemon thread. That was
        catastrophically wrong: Playwright's SYNC API is greenlet-based and
        thread-AFFINE, so every wrapped call raised ``greenlet.error: Cannot
        switch to a different thread`` — 100% of the time, not intermittently.
        In ``_wheel_once`` both the wheel AND its JS fallback were wrapped, and
        ``greenlet.error`` is an ordinary ``Exception``, so both were swallowed
        by the surrounding degrade guards: every scroll notch was skipped, the
        feed never advanced, and Instagram/X/LinkedIn harvested NOTHING with a
        fully green test suite. Reproduced live: the same ``page.mouse.wheel``
        succeeds called directly and fails through ``call_bounded``.

        The deadline was then removed entirely (``timeout_s`` accepted and
        ignored), which left the wedge risk it existed to prevent unguarded —
        a hung ``mouse.*`` could block a whole run — and five tests asserting
        the old contract had to be skipped (ledger D6). Both halves are fixed
        here: the deadline is back, and it is back on the owning thread.

        Any future edit that moves a Playwright call to another thread will
        surface as ``PlaywrightThreadAffinityError``, which subclasses
        ``BaseException`` precisely so no ``except Exception:`` below can hide
        it again.
        """
        if timeout_s is None:
            timeout_s = self.cfg.js_timeout_ms / 1000.0
        return self._owner.call(fn, timeout_s, PlaywrightTimeout)

    def close(self) -> None:
        """Disconnect from the warmed Chrome and release the Playwright driver so the
        NEXT session can attach fresh in the same process. We deliberately do NOT call
        ``browser.close()``: the Chrome was launched and warmed externally
        (warm_chrome.sh) and must outlive the run — ``pw.stop()`` drops only our CDP
        connection and driver, leaving the browser running.

        ``pw.stop()`` is itself thread-affine (it raises ``greenlet.error`` off
        the owning thread), so it routes through the owner like everything else
        — which means it can now TIME OUT if the owner is wedged. It is
        swallowed: ``close()`` has never been able to raise and callers (run
        teardown, halt paths) are not written for it. A permanently wedged owner
        leaks its node driver subprocess and one daemon thread; the run child
        still exits, which is the same tradeoff ``worker/job_runner.py``'s CDP
        probe already accepts."""
        try:
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:  # noqa: BLE001 — teardown must never raise
                    log.debug("CDP close: pw.stop() did not complete cleanly")
            # Retire the owner thread: a multi-session run builds one feed per
            # session in a single process, so a parked thread per finished
            # session would accumulate. A wedged owner never sees the sentinel
            # and stays leaked, which is the documented tradeoff.
            self._owner.shutdown()
        finally:
            self._pw = None
            self._browser = None
            self._page = None
            self._ipage = None

    # ---- interception ----
    def _on_response(self, response) -> None:
        """Template method: hint-filter → JSON-guard → set canary → delegate.

        ``_saw_data`` is set the moment hinted JSON traffic flows, BEFORE
        classification — so an item whose comment section is simply empty does not
        trip the empty-interception canary."""
        try:
            url = response.url
        except Exception:  # noqa: BLE001 — a malformed response must never crash interception
            return
        if not any(h in url for h in self._url_hints()):
            return
        if not self._is_json_like(response):
            return  # a binary/HTML body under a hinted URL is never a data payload
        try:
            body = response.json()
        except Exception:  # not JSON / already consumed — ignore
            return
        self._saw_data = True
        self._classify(url, body, response)

    @staticmethod
    def _is_json_like(response) -> bool:
        """Defensive content-type pre-check before reading a body: only attempt the
        read when the header says JSON or JavaScript (Instagram serves graphql as
        `text/javascript`; comment/reel payloads are `text/javascript` or
        `application/json`). FAIL-OPEN: if the header is absent or unreadable, fall
        through to the parse — this must NOT change what currently classifies, only
        cheaply drop obvious binary/HTML bodies under a hinted URL."""
        try:
            headers = response.headers
        except Exception:  # noqa: BLE001 — no headers surface → let the parse decide
            return True
        if not headers:
            return True
        try:
            ctype = (headers.get("content-type") or headers.get("Content-Type") or "").lower()
        except Exception:  # noqa: BLE001 — not a mapping → let the parse decide
            return True
        if not ctype:
            return True
        return "json" in ctype or "javascript" in ctype

    def _enqueue_reel(self, reel: Reel) -> None:
        """Append a freshly intercepted item, deduped by id (subclass `_classify`).

        Locked: this runs on the PLAYWRIGHT OWNER thread now (Playwright
        dispatches ``page.on("response")`` handlers there) while ``walk()``
        drains the queue on the caller's. The window is narrow — events only
        dispatch while the caller is inside a Playwright call, or after it has
        abandoned one — but it did not exist before the owner thread did."""
        with self._queue_lock:
            if reel.reel_id and reel.reel_id not in self._seen_reel_ids:
                self._seen_reel_ids.add(reel.reel_id)
                self._reel_queue.append(reel)

    # ---- discovery walk (across sources) ----
    def walk(self) -> Iterator[Reel]:
        for url in self._sources():
            log.info("CDP walking source · %s", url)
            self._navigate(url)
            self._halt_if_owner_wedged()
            # Fast skip: some sources 302 to a page with no reels grid (IG's
            # keyword-search fallback). Detect the landed URL and move on rather
            # than spending the whole empty-scroll budget on an empty page.
            landed = self._landed_url()
            if self._source_redirected(url, landed):
                log.info("CDP source redirected to keyword-search (no reels) · %s", url)
                continue
            # Mid-run login/challenge detection: the platform booted this CDP tab
            # to a login/checkpoint wall (e.g. Instagram's session cookie expired
            # mid-harvest). Nothing left to walk — halt the WHOLE session rather
            # than skip-and-continue, so a human gets alerted instead of the walk
            # quietly scraping (or, pre-fix, silently wedging on) a login page.
            wall = self._login_wall_reason(landed)
            if wall is not None:
                reason, kind = wall
                log.error("CDP landed on a login/challenge wall · url=%s · %s",
                          landed, reason)
                raise HaltSession(reason, kind=kind)
            source_start = self._clock()
            yielded = 0
            empty_scrolls = 0
            while yielded < self.cfg.per_source_reels:
                # Locked pop: _enqueue_reel now fires on the Playwright owner
                # thread (response interception), not this one.
                with self._queue_lock:
                    reel = self._reel_queue.pop(0) if self._reel_queue else None
                if reel is not None:
                    empty_scrolls = 0
                    yielded += 1
                    yield reel
                    continue
                # Hard wall-clock ceiling: no source can wedge the walk (the
                # key anti-wedge guarantee — checked before every empty scroll).
                if self._clock() - source_start > self.cfg.max_source_seconds:
                    log.info("CDP source budget exhausted · %s", url)
                    break
                with self._queue_lock:
                    before = len(self._seen_reel_ids)
                self._scroll()
                time.sleep(self.cfg.settle_seconds)
                # A dead browser must not read as "this source was dry".
                self._halt_if_owner_wedged()
                with self._queue_lock:
                    grew = len(self._seen_reel_ids) != before
                if not grew:
                    empty_scrolls += 1
                    if empty_scrolls >= self.cfg.empty_scrolls_before_stop:
                        break  # this source dry → move to the next
            log.debug("CDP source done · %s · yielded=%d", url, yielded)

    def _halt_if_owner_wedged(self) -> None:
        """Halt the session once N consecutive bounded Playwright calls have
        failed to come back (``cfg.max_consecutive_wedged_calls``).

        THIS IS THE OTHER HALF OF THE DEADLINE, not a nicety. Restoring the
        wall-clock bound converts "the run hangs forever" into "every call
        degrades" — and every degrade path here is deliberately
        skip-and-continue: ``_wheel_once`` skips the notch, ``_goto_once``
        swallows the nav, ``_landed_url`` returns ``""`` (so ``_login_wall_reason``
        sees nothing and never halts), ``_shoot`` returns no frame. So a wedged
        CDP session walks every source, burns its empty-scroll budget, yields
        zero reels and ends as ``completed`` — a green, successful, zero-lead run
        that looks exactly like "the seed hashtags were dry". The
        empty-interception canary cannot catch it either: ``healthy()`` is only
        consulted from ``_process_comments``, which is never reached when no reel
        is ever yielded.

        ``kind="canary"`` deliberately reuses the existing SOFT/account-level
        classification (``engines/base.py``): a wedged pipe is environmental and
        rate-limit-shaped — it auto-resumes on the exponential cooldown instead of
        parking the campaign on a human — and it poisons the SHARED warmed Chrome
        for the rest of a multi-platform fan-out, which is exactly right when the
        browser itself is the thing that died.
        """
        limit = self.cfg.max_consecutive_wedged_calls
        if limit <= 0:
            return
        streak = self._owner.wedge_streak
        if streak >= limit:
            log.error("CDP owner wedged · %d consecutive bounded calls did not "
                      "return (%d total) — halting instead of reporting a clean "
                      "zero-lead run", streak, self._owner.wedge_total)
            raise HaltSession("cdp_call_wedged", kind="canary")

    def _landed_url(self) -> str:
        """The URL the page actually landed on after `_navigate` (may differ from
        the requested source when the site 302-redirects)."""
        try:
            return str(self._page.url) if self._page is not None else ""
        except Exception:  # noqa: BLE001 — never let a URL read wedge the walk
            return ""

    def _navigate(self, url: str) -> None:
        if self._page is None:
            return
        # human.goto adds think→settle→orient around the raw nav; when HUMAN_SIM
        # is off it degrades to exactly _goto_once (byte-identical to before).
        self.human.goto(self._page, url, navigate_fn=self._goto_once)

    def _goto_once(self, url: str) -> None:
        """The raw navigation body (goto + nav-settle) — unchanged from before,
        now a callback so human.goto can wrap it without duplicating the
        timeout-safe nav. Kept timeout-degrading: a transient nav failure logs
        and falls through so the next source/scroll still works."""
        if self._page is None:
            return
        try:
            self._page.goto(url, wait_until="domcontentloaded",
                            timeout=self.cfg.nav_timeout_ms)
        except Exception as e:  # noqa: BLE001
            log.debug("CDP nav failed · url=%s · %s", url, e)
            pass  # transient nav failure → next source/scroll still works
        time.sleep(self.cfg.nav_settle_seconds)

    def _scroll(self, page=None) -> None:
        page = page or self._page
        if page is None:
            return
        # human.scroll splits one scroll into 3-7 wheel notches (each _wheel_once)
        # with jittered gaps; when HUMAN_SIM is off it fires _wheel_once ONCE with
        # the full scroll_delta — byte-identical to the previous single wheel.
        self.human.scroll(page, base=self.cfg.scroll_delta,
                          tick_fn=lambda d: self._wheel_once(page, d))

    def _wheel_once(self, page, delta: float) -> None:
        """One wheel notch of ``delta`` px, degrade-not-wedge: a frozen/failed wheel
        falls back to a JS scrollBy; if THAT fails too, skip this notch rather than
        wedge OR crash the walk. The fallback swallows ANY exception (not just
        PlaywrightTimeout) — a transient CDP hiccup like "Connection closed while
        reading from the driver" (seen when Chrome's network service crashes and
        restarts) must degrade to a skipped scroll, exactly like _shoot/_click_*.
        A dead connection just means the next scroll is a no-op and the source's
        wall-clock/empty-scroll caps end the walk cleanly.

        Both calls go through ``_call_bounded``: ``mouse.wheel`` takes no timeout=
        argument at all, and the JS fallback's ``evaluate`` is not reliably bounded
        either once the CDP pipe itself has gone quiet — without the hard deadline
        either one can hang forever with no exception to trigger the fallback.

        WHAT THE FALLBACK CAN AND CANNOT RESCUE. It rescues a wheel that FAILS
        FAST — captured by a modal/overlay, or a transient driver hiccup — which
        is the common case and leaves the pipe healthy enough to evaluate JS.
        It cannot rescue a wheel that truly HANGS: one thread owns Playwright, it
        is still stuck inside that wheel, and no JS can be evaluated on it until
        it returns. Feeding the fallback through anyway would only queue behind
        the hung call and burn a second full deadline before failing identically,
        so the owner fast-fails it instead. The log below says which of the two
        happened — reporting a fast-fail as "fallback failed" reads as "the JS
        was tried and the page rejected it", which is the opposite of true."""
        try:
            self._call_bounded(lambda: page.mouse.wheel(0, delta))
            return
        except PlaywrightTimeout:
            log.warning("CDP scroll wheel timed out — trying JS fallback")
        except Exception:
            pass  # wheel not usable (captured by layout / transient) → JS fallback
        try:
            self._call_bounded(lambda: page.evaluate(f"window.scrollBy(0, {delta})"))
        except Exception as e:  # noqa: BLE001 — a scroll must never crash the run
            if self._owner.is_wedged():
                log.warning("CDP scroll fallback SKIPPED — the owner thread is "
                            "still inside the hung wheel call, so no JS was "
                            "evaluated; skipping scroll")
            else:
                log.warning("CDP scroll fallback failed — skipping scroll (%s)",
                            type(e).__name__)

    # ---- vision frames ----
    def _shoot(self, page) -> Optional[str]:
        """One base64 JPEG of the page's video frame (or full page fallback).

        Neither call below is wrapped in ``_call_bounded``, and until the owner
        thread landed neither was actually bounded either — ``query_selector``
        honours the page default only while the CDP pipe is alive, and nothing
        capped it once the pipe went quiet, despite comments elsewhere claiming
        otherwise. On a live session ``page`` is an ``OwnedPW``, so both now get
        the owner's wall-clock deadline for free and a frozen pipe degrades to
        "no frame" (the vision tier tolerates a None) instead of hanging."""
        if page is None:
            return None
        try:
            el = page.query_selector("video")
            raw = (el.screenshot(type="jpeg") if el
                   else page.screenshot(type="jpeg"))
            return base64.b64encode(raw).decode("ascii")
        except PlaywrightTimeout:
            # A frozen screenshot/query_selector must degrade to "no frame" (the
            # vision tier tolerates a None) instead of hanging the run. Explicit
            # catch for log visibility; PlaywrightTimeout subclasses Exception so
            # behavior is unchanged vs. the generic fallback below.
            log.warning("CDP screenshot timed out — no frame captured")
            return None
        except Exception:
            return None

    def capture_frame(self, reel: Reel) -> Optional[str]:
        """Single representative frame (back-compat)."""
        frames = self.capture_frames(reel, n=1)
        return frames[0] if frames else None

    def capture_frames(self, reel: Reel, n: int = 3, interval: float = 0.6) -> list[str]:
        """Up to n frames across the dwell. Frames MUST come from the item being
        judged, so it is opened full-screen on the interaction page first."""
        if not self.open_reel(reel):
            return []
        out: list[str] = []
        for i in range(max(1, n)):
            shot = self._shoot(self._ipage)
            if shot:
                out.append(shot)
            if i < n - 1:
                time.sleep(interval)
        return out

    # ---- engagement click helpers (opt-in; read-only engines leave these unused) ----
    def _human_pause(self) -> None:
        # Pre-click pause, now triangular via the "engage" range (seeded from
        # cfg.action_delay_min/max in __init__). No-op when HUMAN_SIM is off.
        self.human.human_delay("engage")

    def _click_centermost(self, page, selector_js: str) -> bool:
        """Click the element matching selector_js whose centre is nearest the
        viewport vertical centre. selector_js is a JS expression returning a
        NodeList-like array of candidate elements."""
        if page is None:
            return False
        try:
            box = page.evaluate(
                """(sel) => {
                  const els = sel();
                  const mid = window.innerHeight / 2;
                  let best = null, bestD = Infinity;
                  for (const e of els) {
                    const r = e.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;
                    const d = Math.abs((r.y + r.height / 2) - mid);
                    if (d < bestD) { bestD = d; best = r; }
                  }
                  if (!best) return null;
                  return {x: best.x + best.width / 2, y: best.y + best.height / 2};
                }""", selector_js)
        except PlaywrightTimeout:
            log.warning("CDP click locate (evaluate) timed out — skipping click")
            return False
        except Exception:
            return False
        if not box:
            return False
        self._human_pause()
        try:
            # mouse.click takes no timeout= arg and does not honor the page
            # default either — _call_bounded is the only thing standing between
            # a frozen click and hanging the run indefinitely.
            self._call_bounded(lambda: page.mouse.click(box["x"], box["y"]))
            time.sleep(self.cfg.settle_seconds)
            return True
        except PlaywrightTimeout:
            log.warning("CDP click timed out — treated as no-op")
            return False
        except Exception:
            return False

    # ---- canary ----
    def healthy(self) -> bool:
        return self._saw_data
