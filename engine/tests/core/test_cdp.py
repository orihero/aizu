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
    ticks = iter([0.0, 10.0, 50.0])   # start, first check (ok), second check (>cap)
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

        class _Mouse:
            def wheel(self, dx, dy):
                threading.Event().wait()

            def click(self, x, y):
                threading.Event().wait()

        self.mouse = _Mouse()

    def evaluate(self, *a, **k):
        if self._evaluate_hangs:
            threading.Event().wait()
        return None


def test_wheel_once_degrades_when_mouse_wheel_hangs_forever():
    cfg = CDPBaseConfig(js_timeout_ms=150)   # short deadline so the test stays fast
    feed = _RecordingFeed(cfg)
    page = _HangingPage()
    t0 = _time.monotonic()
    feed._wheel_once(page, 900)   # must not hang: mouse.wheel hangs → JS fallback runs
    assert _time.monotonic() - t0 < 2.0


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
