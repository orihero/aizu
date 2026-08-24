"""Template-method behavior of the shared CDPFeedBase (no Playwright needed).

These lock the contract every CDP engine (Instagram, X, LinkedIn) inherits:
hinted JSON traffic sets the canary BEFORE classification, non-hinted/non-JSON
responses are ignored, ``_classify`` is the only routing hook, and the seed-source
walk drains the reel queue and scrolls when it's dry.
"""
import threading
import time as _time

import pytest

from aizu.core.cdp import CDPBaseConfig, CDPFeedBase, PlaywrightTimeout
from aizu.core.feed import Reel
from aizu.core.pw_owner import OwnedPW, PlaywrightThreadAffinityError
from aizu.engines.base import HaltSession


class FakeResponse:
    def __init__(self, url, body, raises_json=False, headers=None):
        self.url = url
        self._body = body
        self._raises = raises_json
        # None → no header surface at all (fail-open path); a dict → real headers.
        self.headers = headers

    def json(self):
        if self._raises:
            raise ValueError("not JSON")
        return self._body


class _FakePage:
    """Duck-typed page: only what walk()/_scroll need — goto, url, mouse.wheel.

    ``lands_on`` simulates a 302: goto(x) leaves ``url`` at ``lands_on`` instead
    of x, so redirect detection can be driven deterministically."""

    def __init__(self, url="https://example.test/feed", lands_on=None):
        self.url = url
        self._lands_on = lands_on

        class _Mouse:
            def wheel(self, dx, dy):
                pass

        self.mouse = _Mouse()

    def goto(self, url, **kwargs):
        self.url = self._lands_on if self._lands_on is not None else url

    # attach()/_ensure_ipage() now set per-call timeouts on every page; the fakes
    # accept them as no-ops so those paths don't AttributeError.
    def set_default_timeout(self, ms):
        pass

    def set_default_navigation_timeout(self, ms):
        pass

    def on(self, event, handler):
        pass


class _RecordingFeed(CDPFeedBase):
    """Minimal concrete subclass: records every classified body."""

    def __init__(self, cfg=None, clock=None):
        if clock is None:
            super().__init__(cfg)
        else:
            super().__init__(cfg, clock=clock)
        self.classified: list = []

    def _url_hints(self):
        return ("/api/posts", "graphql")

    def _classify(self, url, body, response):
        self.classified.append(body)
        for item in body.get("items", []):
            self._enqueue_reel(Reel(reel_id=item))

    def _sources(self):
        return ["https://example.test/feed"]


def test_hinted_json_sets_canary_then_classifies():
    feed = _RecordingFeed()
    feed._saw_data = False
    feed._on_response(FakeResponse("https://x.test/api/posts?cursor=1", {"items": []}))
    assert feed.healthy()                       # canary tripped by hinted traffic
    assert feed.classified == [{"items": []}]   # delegated to _classify


def test_non_hinted_url_is_ignored():
    feed = _RecordingFeed()
    feed._saw_data = False
    feed._on_response(FakeResponse("https://x.test/static/app.js", {"items": ["a"]}))
    assert not feed.healthy()
    assert feed.classified == []


def test_non_json_response_is_ignored():
    feed = _RecordingFeed()
    feed._saw_data = False
    feed._on_response(FakeResponse("https://x.test/api/posts", None, raises_json=True))
    assert not feed.healthy()
    assert feed.classified == []


def test_enqueue_reel_dedupes_by_id():
    feed = _RecordingFeed()
    feed._on_response(FakeResponse("https://x.test/api/posts", {"items": ["p1", "p2"]}))
    feed._on_response(FakeResponse("https://x.test/api/posts", {"items": ["p2", "p3"]}))
    assert [r.reel_id for r in feed._reel_queue] == ["p1", "p2", "p3"]


def test_walk_drains_queue_then_scrolls_until_dry(monkeypatch):
    cfg = CDPBaseConfig(per_source_reels=10, empty_scrolls_before_stop=2,
                        settle_seconds=0, nav_settle_seconds=0)
    feed = _RecordingFeed(cfg)
    # No real browser: stub nav/scroll. The queue is pre-seeded, so walk() drains
    # it and then scrolls empty_scrolls_before_stop times before giving up.
    monkeypatch.setattr(feed, "_navigate", lambda url: None)
    scrolls = []
    monkeypatch.setattr(feed, "_scroll", lambda page=None: scrolls.append(1))
    feed._reel_queue = [Reel(reel_id="a"), Reel(reel_id="b")]
    feed._seen_reel_ids = {"a", "b"}

    walked = [r.reel_id for r in feed.walk()]
    assert walked == ["a", "b"]
    assert len(scrolls) == 2     # gave up after empty_scrolls_before_stop dry scrolls


class _RedirectFeed(_RecordingFeed):
    """A feed whose single source always 'redirects' to an empty page."""

    def _source_redirected(self, requested_url, landed_url):
        return "search" in landed_url


def test_redirected_source_yields_nothing_and_skips_fast():
    # Source lands on a search-style URL → walk must yield 0 reels for it and
    # NOT loop the empty-scroll budget (no scroll, no per-source dwell).
    cfg = CDPBaseConfig(per_source_reels=10, empty_scrolls_before_stop=4,
                        settle_seconds=0, nav_settle_seconds=0)
    feed = _RedirectFeed(cfg)
    # goto lands on a search-style URL → _source_redirected fires.
    feed._page = _FakePage(lands_on="https://example.test/search?q=x")
    scrolls = []
    feed._scroll = lambda page=None: scrolls.append(1)

    walked = list(feed.walk())
    assert walked == []          # redirected source contributes nothing
    assert scrolls == []         # skipped fast — no empty-scroll budget spent


def test_non_redirect_dry_source_stops_at_wall_clock_cap(monkeypatch):
    # A source that yields no reels and does NOT redirect must still terminate:
    # drive the injected clock so the wall-clock cap trips deterministically.
    cfg = CDPBaseConfig(per_source_reels=10, empty_scrolls_before_stop=999,
                        settle_seconds=0, nav_settle_seconds=0,
                        max_source_seconds=45.0)
    # start, first check (ok), second check (>cap), then the per-source epilogue's
    # own read (it records how long the walk spent on this source).
    ticks = iter([0.0, 10.0, 50.0, 50.0])
    feed = _RecordingFeed(cfg, clock=lambda: next(ticks))
    feed._page = _FakePage()
    scrolls = []
    monkeypatch.setattr(feed, "_scroll", lambda page=None: scrolls.append(1))

    walked = list(feed.walk())
    assert walked == []
    # One scroll happened at t=10 (still under cap); the t=50 check broke the loop
    # long before empty_scrolls_before_stop (999) would have.
    assert len(scrolls) == 1


def test_content_type_guard_skips_binary_but_keeps_json_and_javascript():
    feed = _RecordingFeed()
    # image/jpeg under a hinted URL → NOT classified (defensive drop)
    feed._on_response(FakeResponse(
        "https://x.test/api/posts", {"items": ["j"]},
        headers={"content-type": "image/jpeg"}))
    # text/html under a hinted URL → NOT classified
    feed._on_response(FakeResponse(
        "https://x.test/api/posts", {"items": ["h"]},
        headers={"content-type": "text/html; charset=utf-8"}))
    assert feed.classified == []
    # text/javascript (IG graphql) and application/json ARE classified
    feed._on_response(FakeResponse(
        "https://x.test/graphql", {"items": ["a"]},
        headers={"content-type": "text/javascript; charset=utf-8"}))
    feed._on_response(FakeResponse(
        "https://x.test/api/posts", {"items": ["b"]},
        headers={"content-type": "application/json"}))
    assert feed.classified == [{"items": ["a"]}, {"items": ["b"]}]


def test_content_type_guard_fails_open_when_headers_absent():
    # No header surface (headers=None) → must still classify (preserves real capture
    # and every existing header-less test/response).
    feed = _RecordingFeed()
    feed._on_response(FakeResponse("https://x.test/api/posts", {"items": ["a"]}))
    assert feed.classified == [{"items": ["a"]}]


def test_close_stops_driver_but_keeps_browser():
    feed = _RecordingFeed()
    calls = []

    class PW:
        def stop(self):
            calls.append("pw.stop")

    feed._pw = PW()
    feed._browser = object()
    feed.close()
    assert calls == ["pw.stop"]            # driver released; warmed Chrome untouched
    assert feed._pw is None and feed._browser is None
    feed.close()                           # idempotent when never/again attached


# ---- FIX 1: per-call CDP timeouts must degrade cleanly, never wedge the run ----

class _TimeoutPage:
    """A page whose Playwright ops all raise PlaywrightTimeout — models a frozen
    CDP command that, without a per-call timeout, would block the greenlet forever.
    Records which set_default_* calls attach()/_ensure_ipage() make."""

    def __init__(self):
        self.url = "https://example.test/feed"
        self.default_timeout_ms = None
        self.default_nav_timeout_ms = None

        class _Mouse:
            def wheel(self, dx, dy):
                raise PlaywrightTimeout("wheel timed out")

            def click(self, x, y):
                raise PlaywrightTimeout("click timed out")

        self.mouse = _Mouse()

    def set_default_timeout(self, ms):
        self.default_timeout_ms = ms

    def set_default_navigation_timeout(self, ms):
        self.default_nav_timeout_ms = ms

    def on(self, event, handler):
        pass

    def evaluate(self, *a, **k):
        raise PlaywrightTimeout("evaluate timed out")

    def query_selector(self, *a, **k):
        raise PlaywrightTimeout("query_selector timed out")

    def screenshot(self, *a, **k):
        raise PlaywrightTimeout("screenshot timed out")


class _FakeCtx:
    def __init__(self, page):
        self._page = page
        self.pages = [page]
        self.new_pages = []

    def new_page(self):
        p = _TimeoutPage()
        self.new_pages.append(p)
        return p


class _FakeBrowser:
    def __init__(self, ctx):
        self.contexts = [ctx]


def test_shoot_returns_none_on_screenshot_timeout():
    feed = _RecordingFeed()
    assert feed._shoot(_TimeoutPage()) is None   # degrades to "no frame", no raise


def test_scroll_swallows_wheel_and_evaluate_timeout():
    feed = _RecordingFeed()
    # wheel raises PlaywrightTimeout → JS fallback (evaluate) also raises it →
    # _scroll must return without propagating.
    feed._scroll(_TimeoutPage())   # must not raise


def test_click_centermost_returns_false_on_evaluate_timeout():
    feed = _RecordingFeed()
    assert feed._click_centermost(_TimeoutPage(), "() => []") is False


class _DriverClosedPage:
    """A page whose wheel AND evaluate raise a NON-timeout error — models the
    'Connection closed while reading from the driver' hiccup when Chrome's network
    service crashes and restarts. A scroll must degrade, never crash the run."""

    class _Mouse:
        def wheel(self, dx, dy):
            raise RuntimeError("Connection closed while reading from the driver")

    def __init__(self):
        self.mouse = self._Mouse()

    def evaluate(self, *a, **k):
        raise RuntimeError("Connection closed while reading from the driver")


def test_wheel_once_degrades_on_non_timeout_driver_error():
    feed = _RecordingFeed()
    feed._wheel_once(_DriverClosedPage(), 900)   # must NOT raise


def test_scroll_degrades_on_non_timeout_driver_error():
    feed = _RecordingFeed()
    feed._scroll(_DriverClosedPage())            # must NOT raise (whole scroll path)


def test_attach_sets_page_default_timeouts_from_config():
    cfg = CDPBaseConfig(js_timeout_ms=15000, nav_timeout_ms=20000)
    feed = _RecordingFeed(cfg)
    page = _TimeoutPage()
    ctx = _FakeCtx(page)

    class _FakePW:
        class chromium:
            @staticmethod
            def connect_over_cdp(url, **kwargs):
                return _FakeBrowser(ctx)

        def stop(self):
            pass

    import aizu.core.cdp as cdpmod
    orig_start = cdpmod.sync_playwright
    orig_flag = cdpmod.PLAYWRIGHT_AVAILABLE
    cdpmod.sync_playwright = lambda: type("S", (), {"start": staticmethod(lambda: _FakePW())})()
    cdpmod.PLAYWRIGHT_AVAILABLE = True
    try:
        feed.attach()
    finally:
        cdpmod.sync_playwright = orig_start
        cdpmod.PLAYWRIGHT_AVAILABLE = orig_flag
    assert page.default_timeout_ms == 15000
    assert page.default_nav_timeout_ms == 20000


def test_ensure_ipage_sets_default_timeouts_from_config():
    cfg = CDPBaseConfig(js_timeout_ms=15000, nav_timeout_ms=20000)
    feed = _RecordingFeed(cfg)
    ctx = _FakeCtx(_TimeoutPage())
    feed._browser = _FakeBrowser(ctx)
    ipage = feed._ensure_ipage()
    # CRITICAL: mouse.* on the interaction page rely SOLELY on this page default.
    assert ipage.default_timeout_ms == 15000
    assert ipage.default_nav_timeout_ms == 20000


# ---- hang-prevention fix #1: a call Playwright's OWN timeout does not bound
# (the confirmed real hang — no exception at all, ever) must still return
# control via _call_bounded's real wall-clock deadline. -----------------------

class _HangingPage:
    """A page whose mouse.wheel/mouse.click/evaluate never return at all — no
    exception, no timeout from Playwright itself — modeling the confirmed live
    root cause (a frozen CDP command once the tab redirected to a login wall)."""

    def __init__(self, evaluate_hangs: bool = False):
        self._evaluate_hangs = evaluate_hangs
        self.evaluated: list = []

        class _Mouse:
            def wheel(self, dx, dy):
                threading.Event().wait()

            def click(self, x, y):
                threading.Event().wait()

        self.mouse = _Mouse()

    def evaluate(self, *a, **k):
        self.evaluated.append(a)
        if self._evaluate_hangs:
            threading.Event().wait()
        return None


# These three were SKIPPED while `_call_bounded` enforced no deadline (ledger D6):
# with nothing bounding the call they did not fail, they HUNG FOREVER on the fake
# page's `threading.Event().wait()`, wedging the suite and CI. The deadline is back —
# on Playwright's OWNING thread this time, via `core/pw_owner.py` — so they are live
# again and are the acceptance criteria for that fix. If one of them ever hangs again,
# somebody removed the owner-thread deadline; do not re-skip without reading D6.
def test_wheel_once_degrades_when_mouse_wheel_hangs_forever():
    cfg = CDPBaseConfig(js_timeout_ms=150)   # short deadline so the test stays fast
    feed = _RecordingFeed(cfg)
    page = _HangingPage()
    t0 = _time.monotonic()
    feed._wheel_once(page, 900)   # must not hang; the notch is skipped
    assert _time.monotonic() - t0 < 2.0
    # And it must NOT claim it tried the JS fallback. One thread owns Playwright
    # and it is still stuck inside the hung wheel, so no JS can be evaluated on
    # it — the owner fast-fails the fallback rather than burning a second full
    # deadline to fail identically. (An earlier version of this test asserted the
    # opposite in a comment and never checked, which is how that stayed wrong.)
    assert page.evaluated == []


def test_wheel_once_does_use_the_js_fallback_when_the_wheel_fails_fast():
    # The case the fallback actually exists for, and the reason it must stay:
    # the wheel is rejected/captured (modal, overlay, transient driver hiccup)
    # but the pipe is healthy, so window.scrollBy still advances the feed.
    cfg = CDPBaseConfig(js_timeout_ms=150)
    feed = _RecordingFeed(cfg)

    class _WheelRefusesPage(_HangingPage):
        def __init__(self):
            super().__init__()

            class _Mouse:
                def wheel(self, dx, dy):
                    raise RuntimeError("wheel captured by an overlay")

            self.mouse = _Mouse()

    page = _WheelRefusesPage()
    feed._wheel_once(page, 900)
    assert page.evaluated, "the JS fallback must run when the wheel fails fast"


def test_wheel_once_degrades_when_both_wheel_and_evaluate_fallback_hang_forever():
    cfg = CDPBaseConfig(js_timeout_ms=150)
    feed = _RecordingFeed(cfg)
    page = _HangingPage(evaluate_hangs=True)
    t0 = _time.monotonic()
    feed._wheel_once(page, 900)   # both hang → still returns, no crash
    assert _time.monotonic() - t0 < 2.0


def test_click_centermost_returns_false_when_mouse_click_hangs_forever():
    cfg = CDPBaseConfig(js_timeout_ms=150)
    feed = _RecordingFeed(cfg)

    class _ClickPage(_HangingPage):
        def evaluate(self, *a, **k):
            return {"x": 10, "y": 20}   # locate succeeds fast; only the click hangs

    t0 = _time.monotonic()
    assert feed._click_centermost(_ClickPage(), "() => []") is False
    assert _time.monotonic() - t0 < 2.0


# ---- THREAD-AFFINITY REGRESSION GUARD ---------------------------------------
# The whole trap (ledger D6): Playwright's sync API is thread-AFFINE, and the
# previous breakage — routing every bounded call onto a daemon thread — was
# INVISIBLE to a green suite, because every other fake page in this file is
# thread-agnostic and would happily answer from any thread. These fakes are not:
# they mimic real Playwright and raise greenlet.error on any touch from a thread
# other than the one that built them. If a future edit stores a RAW Playwright
# object on the feed, or moves a call off the owner, these fail loudly.

try:
    from greenlet import error as _greenlet_error
except Exception:  # pragma: no cover - greenlet ships with playwright
    class _greenlet_error(Exception):  # type: ignore[no-redef]
        pass


class _ThreadAffine:
    """Test double with real Playwright semantics: born on one thread, raises
    ``greenlet.error`` on ANY public touch from another. Carries ``_impl_obj``
    so ``OwnedPW`` recognises it exactly as it recognises a real sync object."""

    def __init__(self):
        object.__setattr__(self, "_impl_obj", object())
        object.__setattr__(self, "_born", threading.get_ident())

    def __getattribute__(self, name):
        if not name.startswith("_"):
            born = object.__getattribute__(self, "_born")
            if threading.get_ident() != born:
                raise _greenlet_error("Cannot switch to a different thread")
        return object.__getattribute__(self, name)


class _AffinePage(_ThreadAffine):
    def __init__(self):
        super().__init__()
        object.__setattr__(self, "url", "https://example.test/feed")
        object.__setattr__(self, "default_timeout_ms", None)
        object.__setattr__(self, "handlers", [])

    def on(self, event, handler):
        self.handlers.append(event)

    def set_default_timeout(self, ms):
        object.__setattr__(self, "default_timeout_ms", ms)

    def set_default_navigation_timeout(self, ms):
        pass

    def query_selector(self, sel):
        return _AffineElement() if sel == "video" else None

    def screenshot(self, **kw):
        return b"full-page"


class _AffineElement(_ThreadAffine):
    def screenshot(self, **kw):
        return b"video-frame"


class _AffineCtx(_ThreadAffine):
    def __init__(self, page):
        super().__init__()
        object.__setattr__(self, "pages", [page])

    def new_page(self):
        return _AffinePage()


class _AffineBrowser(_ThreadAffine):
    def __init__(self, ctx):
        super().__init__()
        object.__setattr__(self, "contexts", [ctx])


class _AffineChromium(_ThreadAffine):
    def connect_over_cdp(self, url, **kw):
        return _AffineBrowser(_AffineCtx(_AffinePage()))


class _AffinePW(_ThreadAffine):
    def __init__(self, born_idents):
        super().__init__()
        object.__setattr__(self, "chromium", _AffineChromium())
        born_idents.append(threading.get_ident())

    def stop(self):
        return "stopped"


def _attach_affine(feed, born_idents):
    import aizu.core.cdp as cdpmod
    orig_start, orig_flag = cdpmod.sync_playwright, cdpmod.PLAYWRIGHT_AVAILABLE
    cdpmod.sync_playwright = lambda: type(
        "S", (), {"start": staticmethod(lambda: _AffinePW(born_idents))})()
    cdpmod.PLAYWRIGHT_AVAILABLE = True
    try:
        feed.attach()
    finally:
        cdpmod.sync_playwright = orig_start
        cdpmod.PLAYWRIGHT_AVAILABLE = orig_flag


def test_attach_creates_playwright_on_the_owner_thread_not_the_callers():
    feed = _RecordingFeed()
    born: list[int] = []
    _attach_affine(feed, born)
    assert born and born[0] != threading.get_ident()
    assert born[0] == feed._owner._ident


def test_attach_stores_only_owned_proxies_never_a_raw_playwright_object():
    # The single failure mode of the owner-thread design: one raw reference that
    # escapes is a silent zero-harvest (greenlet.error under `except Exception`).
    feed = _RecordingFeed()
    _attach_affine(feed, [])
    for name in ("_pw", "_browser", "_page"):
        assert isinstance(getattr(feed, name), OwnedPW), f"{name} is not proxied"


def test_attached_page_is_usable_from_the_callers_thread_through_the_proxy():
    feed = _RecordingFeed()
    _attach_affine(feed, [])
    # Every one of these would raise greenlet.error against a raw object.
    assert feed._landed_url() == "https://example.test/feed"
    assert feed._shoot(feed._page) is not None          # query_selector + screenshot
    ipage = feed._ensure_ipage()
    assert isinstance(ipage, OwnedPW)                   # proxied for free by _browser
    feed.close()                                        # pw.stop() routes to the owner
    assert feed._pw is None


def test_wheel_once_does_not_swallow_a_thread_affinity_error():
    # The historical catastrophe itself: greenlet.error is an ordinary Exception,
    # so `_wheel_once`'s degrade guards hid it and every scroll notch was skipped
    # while the suite stayed green. It must now escape as a BaseException.
    feed = _RecordingFeed(CDPBaseConfig(js_timeout_ms=150))

    class _OffThreadPage:
        class _Mouse:
            @staticmethod
            def wheel(dx, dy):
                raise _greenlet_error("Cannot switch to a different thread")

        mouse = _Mouse()

        def evaluate(self, *a, **k):
            raise AssertionError("the JS fallback must not run — this is not a "
                                 "degradable error")

    with pytest.raises(PlaywrightThreadAffinityError):
        feed._wheel_once(_OffThreadPage(), 900)


# ---- A WEDGE MUST NEVER LOOK LIKE A CLEAN, EMPTY RUN ------------------------
# The deadline turns "hangs forever" into "every call degrades", and every
# degrade path here is skip-and-continue. Without the halt below, a dead browser
# produces a walk that yields nothing and returns normally → end_session
# "completed", 0 leads, no halt reason, no health flag — indistinguishable from
# "the seed hashtags were dry" (ledger B6/D6).

def test_walk_halts_instead_of_finishing_clean_when_the_owner_is_wedged():
    cfg = CDPBaseConfig(js_timeout_ms=120, settle_seconds=0.0,
                        nav_settle_seconds=0.0, max_source_seconds=30.0,
                        max_consecutive_wedged_calls=3)
    feed = _RecordingFeed(cfg)
    feed._page = _HangingPage()          # every mouse.wheel never returns

    t0 = _time.monotonic()
    with pytest.raises(HaltSession) as ei:
        list(feed.walk())
    assert _time.monotonic() - t0 < 5.0
    assert ei.value.reason == "cdp_call_wedged"
    # SOFT + account-level (engines/base.py): a wedged pipe is environmental, so
    # it auto-resumes on the cooldown and poisons the shared warmed Chrome.
    assert ei.value.kind == "canary"


def test_walk_does_not_halt_when_calls_keep_coming_back():
    # The wedge streak must RESET on any call that completes, so a merely-slow
    # session (or one flaky notch) never trips the halt.
    cfg = CDPBaseConfig(js_timeout_ms=120, settle_seconds=0.0,
                        nav_settle_seconds=0.0, max_consecutive_wedged_calls=3,
                        empty_scrolls_before_stop=2)
    feed = _RecordingFeed(cfg)
    feed._page = _FakePage()
    assert list(feed.walk()) == []       # dry source, but a clean, non-halting one


def test_ensure_ipage_returns_none_rather_than_raising_when_the_owner_is_wedged():
    # `browser.contexts` used to be a plain cached property (it could not raise);
    # routing it through the owner made it a real cross-thread call. Every caller
    # is `page = self._ensure_ipage()` on the FIRST line of a bool-returning
    # open_reel, OUTSIDE its try — so a raise here escapes the session's
    # `for reel in feed.walk()` loop (which catches only HaltSession) and kills
    # the whole run where the design says "skip this one item".
    cfg = CDPBaseConfig(js_timeout_ms=120)
    feed = _RecordingFeed(cfg)

    class _Ctx:
        _impl_obj = object()

        def new_page(self):
            return _FakePage()

    class _Browser:
        _impl_obj = object()
        contexts = [_Ctx()]

    feed._browser = feed._wrap_pw(_Browser())
    feed._wheel_once(_HangingPage(), 900)          # wedge the owner
    assert feed._owner.is_wedged()
    assert feed._ensure_ipage() is None            # must NOT raise
    assert feed._ipage is None                     # and must not half-publish one


def test_attach_stops_the_driver_when_the_connect_fails():
    # `sync_playwright().start()` has already spawned a node subprocess by the
    # time anything can fail. If that handle only escapes on the success path,
    # every failed attach in a `run-all` loop orphans a driver: self._pw stays
    # None so close() stops nothing.
    import aizu.core.cdp as cdpmod

    stopped: list[int] = []

    class _PW:
        _impl_obj = object()

        def __init__(self):
            self.chromium = self

        def connect_over_cdp(self, url, **kw):
            class _NoCtxBrowser:
                _impl_obj = object()
                contexts: list = []
            return _NoCtxBrowser()

        def stop(self):
            stopped.append(1)

    feed = _RecordingFeed()
    orig, origflag = cdpmod.sync_playwright, cdpmod.PLAYWRIGHT_AVAILABLE
    cdpmod.sync_playwright = lambda: type(
        "S", (), {"start": staticmethod(_PW)})()
    cdpmod.PLAYWRIGHT_AVAILABLE = True
    try:
        with pytest.raises(RuntimeError, match="No browser context"):
            feed.attach()
    finally:
        cdpmod.sync_playwright, cdpmod.PLAYWRIGHT_AVAILABLE = orig, origflag
    assert stopped == [1], "the node driver must be released on a failed attach"
    assert feed._pw is None


# ---- hang-prevention fix #2: mid-run login/challenge-wall detection ---------

class _LoginWallFeed(_RecordingFeed):
    """A feed whose single source always lands on a fixed URL (no real nav), so
    ``_login_wall_reason`` can be exercised deterministically through walk()."""

    def __init__(self, landed_url, cfg=None):
        super().__init__(cfg)
        self._landed_url_value = landed_url

    def _sources(self):
        return ["https://example.test/feed"]

    def _navigate(self, url):
        pass   # no real page — walk() only needs _landed_url() below

    def _landed_url(self):
        return self._landed_url_value

    def _login_wall_reason(self, landed_url):
        if "/accounts/login/" in landed_url:
            return "instagram_login_required", "login"
        if "/challenge/" in landed_url:
            return "instagram_challenge_required", "checkpoint"
        return None


def test_walk_raises_halt_session_on_login_wall():
    cfg = CDPBaseConfig(settle_seconds=0, nav_settle_seconds=0)
    feed = _LoginWallFeed("https://www.instagram.com/accounts/login/", cfg)
    with pytest.raises(HaltSession) as exc_info:
        list(feed.walk())
    assert exc_info.value.kind == "login"
    assert exc_info.value.reason == "instagram_login_required"


def test_walk_raises_halt_session_on_challenge_wall():
    cfg = CDPBaseConfig(settle_seconds=0, nav_settle_seconds=0)
    feed = _LoginWallFeed("https://www.instagram.com/challenge/action/", cfg)
    with pytest.raises(HaltSession) as exc_info:
        list(feed.walk())
    assert exc_info.value.kind == "checkpoint"


def test_walk_proceeds_normally_when_no_login_wall():
    cfg = CDPBaseConfig(per_source_reels=1, settle_seconds=0, nav_settle_seconds=0)
    feed = _LoginWallFeed("https://www.instagram.com/reels/", cfg)
    feed._reel_queue = [Reel(reel_id="a")]
    feed._seen_reel_ids = {"a"}
    walked = [r.reel_id for r in feed.walk()]
    assert walked == ["a"]


def test_login_wall_reason_base_default_never_fires():
    # CDPFeedBase itself (no platform override) must never halt on ANY landed
    # URL — a platform with no known login-wall signature is unaffected.
    feed = _RecordingFeed()
    assert feed._login_wall_reason("https://example.test/accounts/login/") is None


# ---- per-source attribution (the 2026-08-19 live-run bug) -------------------
#
# Interception is ONE process-wide queue. Before Reel.source existed, walk()
# credited every reel it popped to whatever source it happened to be naming, and
# a source that 302'd was `continue`d BEFORE its drain loop — so the reels its own
# XHRs had already queued during nav+settle were paid out under a LATER source.
# The live run logged `.../acme.io/reels/ · yielded=12` for 12 reels whose authors
# were all hashtag-page accounts. These lock the fix down.

class _NavInterceptingFeed(_RecordingFeed):
    """Reels arrive DURING navigation, like the real thing: a source's XHRs land
    in the seconds of nav+settle, before any scroll ever happens."""

    def __init__(self, cfg=None, sources=(), by_source=None, clock=None):
        super().__init__(cfg, clock=clock)
        self._source_urls = list(sources)
        self._by_source = dict(by_source or {})

    def _sources(self):
        return list(self._source_urls)

    def _navigate(self, url):
        for item in self._by_source.get(url, []):
            self._on_response(FakeResponse("https://x.test/api/posts",
                                           {"items": [item]}))


class _RedirectingNavFeed(_NavInterceptingFeed):
    """…and then the source turns out to have 302'd to keyword-search."""

    def _source_redirected(self, requested_url, landed_url):
        return True


class _RecordingLog:
    """Captures formatted log lines. Assertions never go through caplog:
    configure_logging sets propagate=False on the aizu tree, so any earlier test
    in the session can silence it (same reasoning as tests/worker/test_preflight)."""

    def __init__(self):
        self.lines: dict[str, list[str]] = {
            "debug": [], "info": [], "warning": [], "error": []}

    def _rec(self, level, msg, *args):
        self.lines[level].append(msg % args if args else msg)

    def debug(self, msg, *a):
        self._rec("debug", msg, *a)

    def info(self, msg, *a):
        self._rec("info", msg, *a)

    def warning(self, msg, *a):
        self._rec("warning", msg, *a)

    def error(self, msg, *a):
        self._rec("error", msg, *a)


def _cfg(**kw):
    base = dict(settle_seconds=0, nav_settle_seconds=0)
    base.update(kw)
    return CDPBaseConfig(**base)


def test_intercepted_reels_are_stamped_with_the_source_they_arrived_on():
    feed = _NavInterceptingFeed(
        _cfg(per_source_reels=10, empty_scrolls_before_stop=1),
        sources=["src-a", "src-b"],
        by_source={"src-a": ["a1", "a2"], "src-b": ["b1"]})
    feed._scroll = lambda page=None: None

    walked = list(feed.walk())
    assert [(r.reel_id, r.source) for r in walked] == [
        ("a1", "src-a"), ("a2", "src-a"), ("b1", "src-b")]


def test_a_redirected_source_still_drains_the_reels_it_already_intercepted():
    # The exact live shape: the tag page 302s to keyword-search, but its XHRs
    # already fired during nav. Those reels must be paid out HERE — the old
    # `continue` threw them at whichever source ran next, and with no next source
    # (seed_accounts empty, no home feed) they were lost with a clean exit code.
    feed = _RedirectingNavFeed(
        _cfg(per_source_reels=10, empty_scrolls_before_stop=4),
        sources=["tag-1"], by_source={"tag-1": ["r1", "r2"]})
    scrolls = []
    feed._scroll = lambda page=None: scrolls.append(1)

    walked = list(feed.walk())
    assert [r.reel_id for r in walked] == ["r1", "r2"]   # drained, not discarded
    assert scrolls == []                                 # …and still never scrolled


def test_the_source_done_line_counts_only_the_reels_that_source_produced(monkeypatch):
    import aizu.core.cdp as cdpmod
    rec = _RecordingLog()
    monkeypatch.setattr(cdpmod, "log", rec)
    # src-a intercepts two reels but its per-source cap pays out one, so the
    # second is drained under src-b. src-b must not claim it as its own.
    feed = _NavInterceptingFeed(
        _cfg(per_source_reels=1, empty_scrolls_before_stop=1),
        sources=["src-a", "src-b"], by_source={"src-a": ["a1", "a2"]})
    feed._scroll = lambda page=None: None

    walked = [r.reel_id for r in feed.walk()]
    assert walked == ["a1", "a2"]
    assert [ln for ln in rec.lines["debug"] if "source done" in ln] == [
        "CDP source done · src-a · yielded=1 · carried_over=0",
        "CDP source done · src-b · yielded=0 · carried_over=1",
    ]


def test_walk_warns_when_intercepted_reels_are_left_undrained(monkeypatch):
    import aizu.core.cdp as cdpmod
    rec = _RecordingLog()
    monkeypatch.setattr(cdpmod, "log", rec)
    # Three reels intercepted, a per-source cap of one, one source: two reels are
    # already in _seen_reel_ids (so nothing will ever re-queue them) and walk()
    # returns leaving them in the queue. That loss used to be completely silent.
    # `seed_hashtags` names the one source so the reels are stamped with the SEED
    # TERM the ledger keys on (`source_stats`), not with the URL — see
    # CDPFeedBase._source_seeds.
    feed = _NavInterceptingFeed(
        _cfg(per_source_reels=1, seed_hashtags=("src-a",), include_home_feed=False),
        sources=["src-a"], by_source={"src-a": ["a1", "a2", "a3"]})
    feed._scroll = lambda page=None: None

    walked = [r.reel_id for r in feed.walk()]
    assert walked == ["a1"]
    warnings = [ln for ln in rec.lines["warning"] if "never yielded" in ln]
    assert warnings == [
        "CDP walk finished with 2 intercepted reel(s) never yielded · "
        "source(s)=src-a"]


def test_a_fully_drained_walk_does_not_warn(monkeypatch):
    import aizu.core.cdp as cdpmod
    rec = _RecordingLog()
    monkeypatch.setattr(cdpmod, "log", rec)
    feed = _NavInterceptingFeed(
        _cfg(per_source_reels=10, empty_scrolls_before_stop=1),
        sources=["src-a"], by_source={"src-a": ["a1"]})
    feed._scroll = lambda page=None: None

    assert [r.reel_id for r in feed.walk()] == ["a1"]
    assert rec.lines["warning"] == []


# ---- max_source_seconds: the walk's OWN time, enforced every iteration ------

def test_a_slow_consumer_does_not_forfeit_the_sources_scroll_budget():
    # walk() is a generator: the wall clock between `yield` and the next resume is
    # the cascade scoring a reel (90s+ is normal), not a wedge. Charging it to the
    # source made a healthy source abandon itself before it was ever scrolled ONCE
    # — the live run's 45s cap against a 13m15s source is this same accounting.
    now = [0.0]
    feed = _NavInterceptingFeed(
        _cfg(per_source_reels=10, empty_scrolls_before_stop=2,
             max_source_seconds=45.0),
        sources=["src-a"], by_source={"src-a": ["r1", "r2", "r3"]},
        clock=lambda: now[0])
    scrolls = []
    feed._scroll = lambda page=None: scrolls.append(1)

    walked = []
    for reel in feed.walk():
        walked.append(reel.reel_id)
        now[0] += 100.0          # the cascade spends 100s on each reel
    assert walked == ["r1", "r2", "r3"]
    assert scrolls == [1, 1]     # source still got its full dry-scroll budget


def test_no_reel_is_yielded_after_the_walks_own_budget_is_blown():
    # A source whose queue keeps refilling never reached the old budget check at
    # all (it lived in the `reel is None` branch), so "no single source can hold
    # the walk longer than max_source_seconds" simply did not hold for it — and
    # once the budget WAS blown it still paid out everything already queued.
    now = [0.0]
    feed = _NavInterceptingFeed(
        _cfg(per_source_reels=100, empty_scrolls_before_stop=999,
             max_source_seconds=45.0),
        sources=["src-a"], by_source={"src-a": ["r0"]}, clock=lambda: now[0])
    counter = iter(range(1, 500))

    def _refilling_scroll(page=None):
        now[0] += 20.0           # every scroll costs the walk 20s of its own time
        feed._on_response(FakeResponse(
            "https://x.test/api/posts",
            {"items": [f"r{next(counter)}", f"r{next(counter)}"]}))

    feed._scroll = _refilling_scroll

    elapsed_at_yield = []
    walked = []
    for reel in feed.walk():
        walked.append(reel.reel_id)
        elapsed_at_yield.append(now[0])

    assert [t for t in elapsed_at_yield if t > 45.0] == []
    # r0 (t=0) + two per scroll at t=20 and t=40; the t=60 scroll's reels are
    # never paid out because the budget check now runs before every pop.
    assert walked == ["r0", "r1", "r2", "r3", "r4"]
    assert len(walked) < feed.cfg.per_source_reels
