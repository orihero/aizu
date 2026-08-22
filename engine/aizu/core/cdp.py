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

from .feed import (SOURCE_ACCOUNT, SOURCE_HASHTAG, SOURCE_HOME,
                   SOURCE_UNKNOWN, Comment, FeedSource, Reel, SourceOutcome)
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
    # WALL-CLOCK CEILINGS on the two scroll paths, added after the 2026-08-20 fleet
    # run dead-lettered five times on `stalled: no activity for over 180s`.
    # `sessions.last_activity_at` is only bumped between reels, so ANY single call
    # that blocks longer than the watchdog's 180s IS the stall — and both scroll
    # paths could, by construction:
    #   * `_scroll()` is `human.scroll()`, i.e. 3-7 wheel NOTCHES, and each notch
    #     is two independently-bounded owner calls (mouse.wheel, then the JS
    #     fallback). 7 x (15s + 15s) = 210s of unheartbeated wall clock from ONE
    #     `_scroll()` whenever the owner recovers between notches (which is exactly
    #     the live signature: dozens of "scroll wheel timed out" lines, none of
    #     them cheap fast-fails).
    #   * `_open_comments_and_paginate()` runs `max_comment_scrolls` rounds and
    #     falls back to a full `_scroll()` per round — 3 x 210s on top of that.
    # These caps bound the batch itself; `js_timeout_ms` only ever bounded ONE call.
    max_scroll_seconds: float = 20.0     # one _scroll() notch batch, start to finish
    max_comment_pagination_seconds: float = 30.0   # whole comment-dialog pagination
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
    # Hard per-source ceiling on the walk's OWN time: no single source can hold the
    # walk longer than this in nav/scroll/settle/dry-waiting, regardless of
    # scroll/interception behavior (the anti-wedge guarantee). It deliberately does
    # NOT include the time the CALLER spends on a yielded reel — walk() is a
    # generator, so a cascade that takes 90s on a reel used to be charged to the
    # source that produced it. In the live 2026-08-19 run that made one source last
    # 13m15s against this 45s cap and, worse, meant a source could be abandoned
    # WITHOUT ever being scrolled because the budget was already blown by the
    # consumer before the first dry pop. See walk().
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
        # `_current_source` is the source URL walk() is on, read by _enqueue_reel
        # to stamp provenance onto each reel; it is written under _queue_lock
        # because walk() writes it on the CALLER's thread while _enqueue_reel
        # reads it on the Playwright owner's.
        self._current_source: str = ""
        self._reel_queue: list[Reel] = []
        self._seen_reel_ids: set[str] = set()
        self._comments_by_reel: dict[str, list[Comment]] = {}
        self._comment_cursor_by_reel: dict[str, Optional[str]] = {}
        # canary: did we parse any hinted response since the last health check?
        self._saw_data = False
        # Per-_scroll()-batch state (see _scroll/_wheel_once). None => no batch is
        # open, which is how a direct _wheel_once() call (tests, and any future
        # caller outside human.scroll) keeps its old unbounded-batch semantics.
        self._scroll_deadline: Optional[float] = None
        self._scroll_batch_dead = False
        self._scroll_batch_warned = False
        # Session liveness hook — see _progress(). Default no-op so a feed used
        # without a Session (tests, CLI probes) behaves exactly as before.
        self.on_progress: Callable[[], None] = lambda: None

    # ---- session liveness ----
    def _progress(self) -> None:
        """Report that the feed made real progress WITHOUT yielding a reel.

        ``Session`` bumps ``sessions.last_activity_at`` when ``walk()`` hands it an
        item; the whole interval BETWEEN two yields is the feed's own, and nothing
        in it bumped anything. That interval is not small and it is not bounded by
        one source: on 2026-08-20 job-2099fb29e88b dead-lettered five times on
        ``stalled: no activity for over 180s`` (session_watchdog.STALL_TIMEOUT_SEC),
        and even with the per-batch scroll ceiling below, ONE source transition
        costs up to ~126s of silence (nav ~81s worst case + the landed-url and
        login-wall probes at js_timeout_ms each) — while a run of *unproductive*
        sources, which is exactly what the live Instagram brief hit when six
        hashtag pages redirected, multiplies that with no ceiling at all.

        Called only where something actually finished (a source landed, a scroll
        batch completed, a comment page paginated) — never on a timer, so a feed
        genuinely wedged inside a call still goes quiet and is still halted.

        Never raises: the heartbeat is a diagnostic, and a DB hiccup here must not
        end a walk that is otherwise healthy."""
        try:
            self.on_progress()
        except Exception:  # noqa: BLE001 — a heartbeat must never break the walk
            log.debug("CDP progress hook failed", exc_info=True)

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

    def _source_seeds(self, urls: list[str]) -> list[tuple[str, str]]:
        """Label each URL from ``_sources()`` as ``(kind, seed_term)`` for the
        per-source ledger (`FeedSource.SourceOutcome`).

        Classifies by POSITION, not by parsing the URL back apart: every CDP
        engine builds ``_sources()`` as ``[home?] + seed_hashtags + seed_accounts``
        in that order (instagram/x/linkedin ``_sources`` are identical in shape),
        so the counts on ``cfg`` name each slot exactly. Substring-matching a tag
        against a URL is what breaks the moment a handle contains a seeded tag.

        Any engine whose ``_sources()`` deviates from that shape reports every
        source as ``unknown``, labelled by URL, rather than mislabelling —
        accounting degrades, the walk does not. "Deviates" means the leftover
        count is anything other than 0 or 1: there is exactly one home feed, so a
        leftover of 2+ is proof the positions do not mean what this assumes, and
        folding several distinct URLs into one ``home`` row would merge their
        ledger entries."""
        tags = [str(t).lstrip("#") for t in (getattr(self.cfg, "seed_hashtags", ()) or ())]
        accounts = [str(a).lstrip("@").strip("/")
                    for a in (getattr(self.cfg, "seed_accounts", ()) or ())]
        home = len(urls) - len(tags) - len(accounts)
        if home not in (0, 1):
            return [(SOURCE_UNKNOWN, u) for u in urls]
        return ([(SOURCE_HOME, "home")] * home
                + [(SOURCE_HASHTAG, t) for t in tags]
                + [(SOURCE_ACCOUNT, a) for a in accounts])

    def _source_unavailable(self) -> bool:
        """True when the landed source page says the content does not exist — a
        banned/removed hashtag or a deleted/renamed account.

        Distinct from ``_source_redirected`` (the page loaded, just not a reels
        grid) and from "dry" (the page is fine, it simply had nothing new). Until
        now nothing separated the three: a dead seed burned a nav plus four
        empty-scroll rounds (~45s) every session, forever, with no flag and no DB
        row — and the campaign still reported ``completed``.

        Implemented once here on top of the per-platform ``_page_unavailable``
        DOM probe every CDP engine already ships (instagram/x/linkedin), which
        until now was only reachable from ``open_reel`` — i.e. it could tell a
        dead REEL from a live one but never a dead SEED. An engine that has no
        such probe (or a feed with no page attached yet) returns False and costs
        nothing."""
        probe = getattr(self, "_page_unavailable", None)
        page = self._page
        if probe is None or page is None:
            return False
        try:
            return bool(probe(page))
        except Exception:  # noqa: BLE001 — a failed probe means "assume available"
            log.debug("CDP _source_unavailable probe failed", exc_info=True)
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

    def _focus(self, page) -> None:
        """Raise ``page`` before dispatching mouse input to it.

        Chrome accepts ``Input.dispatchMouseEvent`` for a tab without input focus
        and never ACKs it, so the call never returns and the owner thread is
        poisoned for the rest of the run (see ``HumanSim.focus`` for the full
        root-cause note and the live evidence). Bounded and silent: this helper
        must never be the call that wedges, and it is only ever a best-effort
        precondition, never a step whose failure should stop a walk.
        """
        if page is None:
            return
        try:
            self._call_bounded(lambda: page.bring_to_front())
        except Exception as e:  # noqa: BLE001 — best-effort precondition only
            # Observable on purpose. Swallowing this silently is how the ORIGINAL
            # bug hid for five dead-lettered attempts: `HumanSim.mouse_move` ate its
            # own 15s expiry with a bare `except Exception: return`, so the call that
            # actually poisoned the owner left no trace and the log blamed the wheel.
            # If focus itself cannot be taken, the mouse input that follows is the
            # one that will hang — so this line is the earliest warning available.
            log.debug("CDP focus (bring_to_front) failed — mouse input may not ACK · %s",
                      type(e).__name__)
            return

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
        abandoned one — but it did not exist before the owner thread did.

        The same lock is what makes the `source` stamp correct: walk() publishes
        `_current_source` (on the caller's thread) BEFORE it navigates, so every
        XHR intercepted during that source's nav+settle+scroll is tagged with the
        source that caused it. Without the stamp the queue is anonymous and
        per-source accounting is guesswork — see Reel.source.

        The stamp is the SEED TERM, not the URL: it is written straight through to
        `seen_reels.source`, which `source_stats` joins on. Stamping the URL makes
        that join silently return zero for every seed."""
        with self._queue_lock:
            if reel.reel_id and reel.reel_id not in self._seen_reel_ids:
                self._seen_reel_ids.add(reel.reel_id)
                reel.source = self._current_source
                self._reel_queue.append(reel)

    # ---- discovery walk (across sources) ----
    def walk(self) -> Iterator[Reel]:
        sources = self._sources()
        # (kind, seed_term) parallel to `sources` — see _source_seeds. Computed
        # once, before the first navigation, so the ledger's labels can never
        # drift from the list actually being walked.
        seeds = self._source_seeds(sources)
        for url, (source_kind, seed) in zip(sources, seeds):
            log.info("CDP walking source · %s", url)
            # Publish the source BEFORE navigating: interception starts with the
            # first XHR of the nav, and everything that lands during the 7-10s of
            # nav+settle belongs to THIS source. _enqueue_reel stamps it (see
            # Reel.source) so the accounting below can never again credit one
            # source with another's reels. The SEED TERM is what gets stamped —
            # it is the ledger's key and it is what lands in seen_reels.source.
            with self._queue_lock:
                self._current_source = seed
                intercepted_before = len(self._seen_reel_ids)
            self._navigate(url)
            # The source landed: think → goto(nav_timeout_ms) → nav-settle →
            # mouse_move (two bounded calls) → nav delay is ~81s of worst-case
            # silence, and the three probes below add js_timeout_ms each. Bumping
            # here is what stops a string of unproductive sources from adding up
            # to a watchdog halt (see _progress).
            self._progress()
            self._halt_if_owner_wedged()
            # Some sources 302 to a page with no reels grid (IG's keyword-search
            # fallback). Such a source must not spend the empty-scroll budget on a
            # page that will never grow — but it must still DRAIN what it already
            # intercepted during nav+settle. This used to `continue` outright,
            # which dumped those reels onto whichever source ran next: in the live
            # 2026-08-19 run all six hashtag sources redirected and their 12 reels
            # were drained (and logged) under a seed ACCOUNT source. That accident
            # is the only thing that kept hashtag discovery alive — a brief with no
            # seed accounts and no home feed would have intercepted reels on every
            # tag page, skipped every source before draining, harvested zero, and
            # still reported `completed` with no health flag.
            landed = self._landed_url()
            scrollable = True
            # Does the page exist at all? Probed BEFORE the drain loop so a dead
            # seed skips the four empty-scroll rounds it used to burn (~45s per
            # dead source per session, forever) and so the verdict reaches the
            # ledger as `unavailable` instead of as an indistinguishable dry
            # source. Base default never probes, so this is free on any engine
            # that has not opted in.
            #
            # Gated on "the nav intercepted nothing": the per-platform probes are
            # innerText regexes, and LinkedIn renders "this content isn't
            # available" INLINE for a single removed post inside an otherwise
            # healthy feed. Interception is the stronger, cheaper evidence — if
            # items arrived, the page plainly exists and no amount of page copy
            # should be able to say otherwise.
            with self._queue_lock:
                intercepted_any = len(self._seen_reel_ids) > intercepted_before
            unavailable = (not intercepted_any) and self._source_unavailable()
            if unavailable:
                log.warning("CDP source does not exist (banned/removed/renamed) "
                            "· %s · not scrolling", url)
                scrollable = False
            redirected = self._source_redirected(url, landed)
            if redirected:
                log.info("CDP source redirected to keyword-search (no reels grid) "
                         "· %s · draining what it already intercepted, not scrolling",
                         url)
                scrollable = False
            # Mid-run login/challenge detection: the platform booted this CDP tab
            # to a login/checkpoint wall (e.g. Instagram's session cookie expired
            # mid-harvest). Nothing left to walk — halt the WHOLE session rather
            # than skip-and-continue, so a human gets alerted instead of the walk
            # quietly scraping (or, pre-fix, silently wedging on) a login page.
            wall = self._login_wall_reason(landed)
            if wall is not None:
                reason, wall_kind = wall
                log.error("CDP landed on a login/challenge wall · url=%s · %s",
                          landed, reason)
                raise HaltSession(reason, kind=wall_kind)
            source_start = self._clock()
            consumer_seconds = 0.0   # time the CALLER held the generator (excluded)
            drained = 0              # items popped under this source, whoever queued them
            from_source = 0          # …of which were actually intercepted HERE
            empty_scrolls = 0
            # try/finally, not a plain epilogue: walk() is a generator and the
            # caller abandons it the moment it hits its lead target — i.e.
            # exactly on the MOST productive source. Recording only on normal
            # exit would systematically under-credit the seed that worked.
            try:
                while drained < self.cfg.per_source_reels:
                    # Hard ceiling on the walk's OWN time, checked at the TOP of every
                    # iteration — not only when the queue is dry, as before. A source
                    # whose queue keeps refilling never took the dry branch, so the
                    # "no single source can hold the walk" guarantee simply did not
                    # hold for it; and once the budget IS blown, the old placement
                    # still yielded every reel already sitting in the queue.
                    # `consumer_seconds` is subtracted because walk() is a generator:
                    # the wall clock between `yield` and the next resume is the
                    # cascade scoring a reel (90s+ is normal), which is progress, not
                    # a wedge. Charging it here made a slow-but-healthy source abandon
                    # itself before it was ever scrolled once.
                    if self._clock() - source_start - consumer_seconds > self.cfg.max_source_seconds:
                        log.info("CDP source budget exhausted · %s", url)
                        break
                    # Locked pop: _enqueue_reel now fires on the Playwright owner
                    # thread (response interception), not this one.
                    with self._queue_lock:
                        reel = self._reel_queue.pop(0) if self._reel_queue else None
                    if reel is not None:
                        empty_scrolls = 0
                        drained += 1
                        if reel.source == seed:
                            from_source += 1
                        handed_off_at = self._clock()
                        yield reel
                        # max(0.0, …) so an injected/non-monotonic clock can only ever
                        # shorten the exclusion, never manufacture budget.
                        consumer_seconds += max(0.0, self._clock() - handed_off_at)
                        continue
                    if not scrollable:
                        break  # redirected page: drained, and it will never grow
                    with self._queue_lock:
                        before = len(self._seen_reel_ids)
                    self._scroll()
                    time.sleep(self.cfg.settle_seconds)
                    # A scroll batch finished. It is capped at max_scroll_seconds +
                    # one js_timeout_ms (~35s) plus human.scroll's mouse_move and
                    # settle — call it ~91s worst case — and empty_scrolls_before_stop
                    # allows four of them per source before this source is declared
                    # dry. Without this bump those four rounds are one unbroken
                    # silence far past STALL_TIMEOUT_SEC.
                    self._progress()
                    # A dead browser must not read as "this source was dry".
                    self._halt_if_owner_wedged()
                    with self._queue_lock:
                        grew = len(self._seen_reel_ids) != before
                    if not grew:
                        empty_scrolls += 1
                        if empty_scrolls >= self.cfg.empty_scrolls_before_stop:
                            break  # this source dry → move to the next
            finally:
                # `yielded=` now names what this source actually produced. It used
                # to count pops, which is why the live run credited a seed account
                # with 12 reels it never served; `carried_over=` makes the borrowed
                # ones visible instead of invisible. Both now also leave the file:
                # _record_source persists the row the 2026-08-19 run had nowhere to
                # put (Campaign Lab, Remedy Sheet #1 / Remedy D).
                log.debug("CDP source done · %s · yielded=%d · carried_over=%d",
                          url, from_source, drained - from_source)
                self._record_source(SourceOutcome(
                    source=seed, kind=source_kind, url=url,
                    yielded=from_source, carried_over=drained - from_source,
                    redirected=redirected, unavailable=unavailable,
                    seconds=max(0.0, self._clock() - source_start - consumer_seconds)))
        # Reels intercepted but never drained are invisible losses: they are
        # already in `_seen_reel_ids`, so nothing will re-queue them, and walk()
        # simply returns with them sitting in the queue. Nothing reported this.
        # (Deliberately NOT in a `finally`: a caller that stops early because it
        # hit its lead target is not losing anything worth a warning.)
        with self._queue_lock:
            leftover = [r.source or "?" for r in self._reel_queue]
        if leftover:
            log.warning("CDP walk finished with %d intercepted reel(s) never "
                        "yielded · source(s)=%s", len(leftover),
                        ", ".join(sorted(set(leftover))))

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
        """One humanized scroll BATCH, under a hard wall-clock ceiling.

        ``human.scroll`` splits this into 3-7 wheel notches (each ``_wheel_once``)
        with jittered gaps; when HUMAN_SIM is off it fires ``_wheel_once`` ONCE
        with the full ``scroll_delta`` — byte-identical to the previous single
        wheel.

        THE CEILING IS THE POINT (2026-08-20 fleet stall). Every notch is two
        independently-bounded owner calls, so ``js_timeout_ms`` bounded a NOTCH,
        never the batch: 7 notches x (15s wheel + 15s JS fallback) = 210s of wall
        clock inside ONE ``_scroll()``, against a 180s session watchdog that is
        only fed between reels. Whenever the owner un-wedges between notches —
        exactly the live signature, dozens of full-deadline "scroll wheel timed
        out" lines rather than free fast-fails — the batch got a fresh 15s per
        call and there was nothing to stop it.

        The deadline is published on ``self`` (not threaded through
        ``human.scroll``, which takes a bare ``tick_fn``) and torn down in a
        ``finally`` so a raise can never leave a stale batch armed."""
        page = page or self._page
        if page is None:
            return
        self._scroll_deadline = self._clock() + self.cfg.max_scroll_seconds
        self._scroll_batch_dead = False
        self._scroll_batch_warned = False
        try:
            self.human.scroll(page, base=self.cfg.scroll_delta,
                              tick_fn=lambda d: self._wheel_once(page, d))
        finally:
            self._scroll_deadline = None
            self._scroll_batch_dead = False
            self._scroll_batch_warned = False

    def _batch_spent(self) -> bool:
        """True when the open ``_scroll()`` batch is out of wall clock, or has
        already proved it cannot scroll. ``_scroll_deadline is None`` means
        ``_wheel_once`` was called outside a batch (tests, future callers) — then
        there is no batch to spend and the old semantics apply unchanged."""
        if self._scroll_batch_dead:
            return True
        return (self._scroll_deadline is not None
                and self._clock() >= self._scroll_deadline)

    def _kill_batch(self, why: str) -> None:
        """Abandon the REST of this notch batch. N = 1, deliberately: the notches
        of one batch are the same call, on the same page, through the same owner,
        within a ~2s window. Once one of them has failed to come back there is no
        mechanism by which notch 2..7 differs — retrying them buys nothing and
        costs up to another 6 x 30s of unheartbeated wall clock. This is also what
        turns "the same warning dozens of times" into one line per batch: the
        repetition was never new information, it was the loop re-asking a question
        it had already been answered."""
        if self._scroll_deadline is None:
            return  # no batch open (direct _wheel_once call) — nothing to kill
        self._scroll_batch_dead = True
        log.debug("CDP scroll batch abandoned after the first bad notch · %s", why)

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

        WHY THE WHEEL "HANGS" DESPITE ALL THAT, and why that is not a missing
        timeout: the owner-thread deadline bounds the WAIT, not the CALL. Python
        cannot kill a thread mid-syscall, so an expiry means the CALLER walks away
        at ``js_timeout_ms`` while the owner stays inside ``mouse.wheel``
        indefinitely. That is working as designed (``core/pw_owner.py``) — what was
        NOT designed is how much wall clock the batch around it could then burn.

        WHAT THE FALLBACK CAN AND CANNOT RESCUE. It rescues a wheel that FAILS
        FAST — captured by a modal/overlay, or a transient driver hiccup — which
        is the common case and leaves the pipe healthy enough to evaluate JS.
        It cannot rescue a wheel that truly HANGS: one thread owns Playwright, it
        is still stuck inside that wheel, and no JS can be evaluated on it until
        it returns. Feeding the fallback through anyway would only queue behind
        the hung call and burn a second full deadline before failing identically,
        so the owner fast-fails it instead. The log below says which of the two
        happened — reporting a fast-fail as "fallback failed" reads as "the JS
        was tried and the page rejected it", which is the opposite of true.

        A NOTE ON THE WEDGE STREAK. The wheel is submitted even when the owner is
        already known-wedged, and that is load-bearing: the fast-fail is what
        increments ``PlaywrightOwner.wedge_streak``, which is the ONLY thing that
        eventually trips ``_halt_if_owner_wedged``. Short-circuiting it to save a
        few free microseconds would silently disarm the halt. Only notches that
        the batch has already given up on are skipped, and a skipped batch still
        submits one wheel per ``_scroll()``."""
        # Unfocused tab => the wheel is accepted and never ACKed (see _focus).
        self._focus(page)
        if self._batch_spent():
            return  # this batch is out of clock / already proved dead → skip the notch
        timed_out = False
        try:
            self._call_bounded(lambda: page.mouse.wheel(0, delta))
            return
        except PlaywrightTimeout:
            timed_out = True
            # One line per BATCH. The live log's dozens of identical warnings were
            # the loop re-asking a question it had already been answered; outside a
            # batch (no deadline armed) every call still logs, unchanged.
            if self._scroll_deadline is None or not self._scroll_batch_warned:
                self._scroll_batch_warned = True
                log.warning("CDP scroll wheel timed out — trying JS fallback")
            else:
                log.debug("CDP scroll wheel timed out again — trying JS fallback")
        except Exception:
            pass  # wheel not usable (captured by layout / transient) → JS fallback
        if timed_out and self._batch_spent():
            # The wheel ate the rest of the batch's budget. Spending another full
            # deadline on the fallback is precisely the 15s+15s doubling that put
            # a single _scroll() over the watchdog.
            self._kill_batch("wheel timed out with no budget left for the fallback")
            return
        try:
            self._call_bounded(lambda: page.evaluate(f"window.scrollBy(0, {delta})"))
            return
        except Exception as e:  # noqa: BLE001 — a scroll must never crash the run
            if self._owner.is_wedged():
                log.warning("CDP scroll fallback SKIPPED — the owner thread is "
                            "still inside the hung wheel call, so no JS was "
                            "evaluated; skipping scroll")
            else:
                log.warning("CDP scroll fallback failed — skipping scroll (%s)",
                            type(e).__name__)
        if timed_out:
            # Wheel timed out AND the JS rescue failed: this batch has no way to
            # move the page. Kill it rather than re-running the same pair 2-6 more
            # times. A wheel that merely failed FAST and was rescued by the JS
            # fallback is the healthy path and never reaches here.
            self._kill_batch("wheel timed out and the JS fallback did not rescue it")

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
        # Same precondition as the wheel: a click on an unfocused tab never ACKs.
        self._focus(page)
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
