"""FIX 1 (Instagram surface): a frozen CDP command must degrade to a fail-safe
value, never block the greenlet forever. Every Playwright op below raises
PlaywrightTimeout (the per-call timeout now fires instead of hanging); each guard
must return its documented fail-safe without propagating.
"""
import threading
import time

from aizu.core.cdp import PlaywrightTimeout
from aizu.core.feed import Reel
from aizu.engines.instagram.cdp import CDPConfig, CDPFeed


class _LandingPage:
    """A page whose goto() 'lands' on a fixed URL (simulating where Instagram actually
    resolved the permalink). evaluate() records that it was reached."""

    def __init__(self, landed_url, start_url="https://www.instagram.com/saastr/reels/"):
        self._landed = landed_url
        self.url = start_url
        self.evaluated = False

    def set_default_timeout(self, ms):
        pass

    def set_default_navigation_timeout(self, ms):
        pass

    def on(self, event, handler):
        pass

    def goto(self, url, **kwargs):
        self.url = self._landed  # simulate the (possibly redirected) landing URL

    def evaluate(self, *a, **k):
        self.evaluated = True
        return False  # "available" (not the not-found screen)


def _feed_with_ipage(page):
    feed = CDPFeed(CDPConfig(nav_settle_seconds=0.0))  # no real sleep in tests
    feed._ipage = page
    feed._ensure_ipage = lambda: page  # type: ignore
    return feed


def test_open_reel_skips_when_permalink_bounces_to_swipeable_feed():
    # Instagram bounced /reel/<code>/ into the swipeable /reels/ feed → the code is gone
    # from the URL. open_reel must skip (return False) via the safe page.url check BEFORE
    # the blocking _page_unavailable evaluate — this is the reels-surface wedge guard.
    page = _LandingPage(landed_url="https://www.instagram.com/reels/")
    feed = _feed_with_ipage(page)
    assert feed.open_reel(Reel(reel_id="Dabz0s7j5-E")) is False
    assert page.evaluated is False  # short-circuited, never reached the evaluate


def test_open_reel_proceeds_when_landed_on_single_reel_permalink():
    code = "Dabz0s7j5-E"
    page = _LandingPage(landed_url=f"https://www.instagram.com/reel/{code}/")
    feed = _feed_with_ipage(page)
    assert feed.open_reel(Reel(reel_id=code)) is True
    assert page.evaluated is True  # reached the availability probe on a valid single reel


class _TimeoutPage:
    """Interaction page whose every op raises PlaywrightTimeout."""

    def __init__(self, url="https://www.instagram.com/reels/"):
        self.url = url

        class _Mouse:
            def click(self, x, y):
                raise PlaywrightTimeout("click timed out")

            def wheel(self, dx, dy):
                raise PlaywrightTimeout("wheel timed out")

        self.mouse = _Mouse()

    def set_default_timeout(self, ms):
        pass

    def set_default_navigation_timeout(self, ms):
        pass

    def on(self, event, handler):
        pass

    def goto(self, url, **kwargs):
        raise PlaywrightTimeout("goto timed out")

    def evaluate(self, *a, **k):
        raise PlaywrightTimeout("evaluate timed out")

    def query_selector(self, *a, **k):
        raise PlaywrightTimeout("query_selector timed out")


def test_open_reel_returns_false_on_goto_timeout():
    feed = CDPFeed()
    # Force the interaction page to our timing-out fake (avoid a real browser).
    feed._ipage = _TimeoutPage(url="https://www.instagram.com/other/")
    feed._ensure_ipage = lambda: feed._ipage  # type: ignore
    assert feed.open_reel(Reel(reel_id="Cabc123")) is False


def test_page_unavailable_returns_false_on_evaluate_timeout():
    # Fail-safe = "available" (False): a timed-out probe must not skip a good reel.
    assert CDPFeed._page_unavailable(_TimeoutPage()) is False


def test_scroll_comment_dialog_returns_false_on_timeout():
    feed = CDPFeed()
    assert feed._scroll_comment_dialog(_TimeoutPage()) is False


def test_open_comment_dialog_swallows_timeout():
    feed = CDPFeed()
    feed._open_comment_dialog(_TimeoutPage())   # must not raise


def test_detect_action_block_fails_open_on_timeout():
    # Fail-OPEN (False = "no block"): a timed-out probe must not false-halt the run.
    feed = CDPFeed()
    feed._ipage = _TimeoutPage()
    assert feed.detect_action_block() is False


def test_share_reel_returns_false_on_timeout():
    feed = CDPFeed()
    feed._ipage = _TimeoutPage()
    assert feed.share_reel(Reel(reel_id="Cabc123")) is False


# ---- FIX 2: Instagram's login/challenge-wall signature (core/cdp.py's
# CDPFeedBase.walk() calls _login_wall_reason after every source navigation) ----

def test_login_wall_reason_detects_login_url():
    feed = CDPFeed()
    assert feed._login_wall_reason(
        "https://www.instagram.com/accounts/login/?next=%2Freels%2F"
    ) == ("instagram_login_required", "login")


def test_login_wall_reason_detects_challenge_url():
    feed = CDPFeed()
    assert feed._login_wall_reason(
        "https://www.instagram.com/challenge/action/"
    ) == ("instagram_challenge_required", "checkpoint")


def test_login_wall_reason_none_for_an_ordinary_url():
    feed = CDPFeed()
    assert feed._login_wall_reason("https://www.instagram.com/reels/") is None


def test_login_wall_reason_none_for_empty_landed_url():
    feed = CDPFeed()
    assert feed._login_wall_reason("") is None


# ---- FIX 1: context.cookies() is a CDP round-trip with no timeout= of its
# own — a hang there must degrade _reel_media_request to None, not hang. ----

class _HangingCookiesPage:
    def __init__(self):
        class _Ctx:
            def cookies(self):
                threading.Event().wait()   # never returns — models a frozen CDP pipe
        self.context = _Ctx()

    def evaluate(self, *a, **k):
        return "some-agent/1.0"


def test_reel_media_request_degrades_when_cookies_call_hangs_forever():
    feed = CDPFeed(CDPConfig(js_timeout_ms=150))
    feed._page = _HangingCookiesPage()
    feed._code_to_media_url["Cabc123"] = "https://cdn.example.test/video.mp4"
    t0 = time.monotonic()
    result = feed._reel_media_request(Reel(reel_id="Cabc123"))
    assert time.monotonic() - t0 < 2.0
    # Degrades to an empty cookie header, never raises/hangs; the url/UA still work.
    assert result is not None
    url, headers = result
    assert url == "https://cdn.example.test/video.mp4"
    assert headers["cookies"] == ""
