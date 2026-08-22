"""The 2026-08-20 fleet dead-letters and the two correctness bugs found alongside.

Five fleet attempts halted `cdp_call_wedged` while reels were still being scored
relevant. Root cause, observed live rather than inferred: Chrome accepts
`Input.dispatchMouseEvent` for a tab without input focus and never ACKs it, so the
call never returns, the Playwright owner thread stays poisoned, and the next three
bounded calls fast-fail into `_halt_if_owner_wedged`. Only the mouse path dies —
goto/evaluate/screenshot and response interception need no focus, which is why reels
kept passing the gate and only comment scoring died.
"""
import pytest

from aizu.core.human import HumanSim, HumanSimConfig
from aizu.engines.base import HaltSession
from aizu.engines.instagram.cdp import CDPFeed


class _Page:
    """Records the ORDER of protocol calls; that order is the whole contract."""

    def __init__(self, url="https://www.instagram.com/reel/ABC123/"):
        self.calls = []
        self.url = url
        self.mouse = self._Mouse(self)

    def bring_to_front(self):
        self.calls.append("bring_to_front")

    def evaluate(self, _js):
        self.calls.append("evaluate")
        return {"w": 1280, "h": 800}

    class _Mouse:
        def __init__(self, page):
            self._p = page

        def move(self, *_a, **_k):
            self._p.calls.append("mouse.move")

        def wheel(self, *_a, **_k):
            self._p.calls.append("mouse.wheel")

        def click(self, *_a, **_k):
            self._p.calls.append("mouse.click")


def _human():
    sim = HumanSim(HumanSimConfig(enabled=True))
    # Run the bounded calls inline: the owner thread is not what is under test here.
    sim._bounded = lambda fn: fn()
    return sim


def test_the_tab_is_focused_before_any_mouse_input_is_dispatched():
    """The fix. An unfocused tab never ACKs Input.dispatchMouseEvent, so the very
    first mouse.move hangs forever and poisons the owner for the rest of the run.
    bring_to_front must therefore come BEFORE the move, not after and not never."""
    page = _Page()
    _human().mouse_move(page)

    assert "mouse.move" in page.calls, "the mouse input never happened"
    assert page.calls.index("bring_to_front") < page.calls.index("mouse.move"), (
        f"mouse input dispatched to a possibly-unfocused tab: {page.calls}")


def test_focus_failure_never_breaks_the_run():
    """`focus` must never become the call that wedges: a browser that cannot raise a
    tab is not a reason to fail a walk, and this helper runs on every mouse call."""
    page = _Page()

    def boom():
        raise RuntimeError("bring_to_front exploded")

    page.bring_to_front = boom
    _human().mouse_move(page)          # must not raise
    assert "mouse.move" in page.calls  # and must still do its job


def test_open_reel_halts_on_a_login_wall_that_still_contains_the_reel_code():
    """CORRECTNESS. The landing guard was a bare substring test, and Instagram's
    wall redirect is `/accounts/login/?next=/reel/<code>/` — which CONTAINS the
    code, so the wall passed as a successful open. `_login_wall_reason` was only
    consulted from walk() after source navigation, never here, so an expired session
    would keep 'opening' reels and scoring whatever the wall rendered."""
    feed = CDPFeed.__new__(CDPFeed)
    reel = type("Reel", (), {"reel_id": "ABC123"})()
    walled = _Page("https://www.instagram.com/accounts/login/?next=/reel/ABC123/")

    assert reel.reel_id in walled.url, "fixture must reproduce the substring trap"
    with pytest.raises(HaltSession):
        feed._open_reel_landing_check(reel, walled)


def test_the_comment_button_is_measured_in_the_state_it_is_clicked_in():
    """Focus FIRST, then measure, then click — the order is the contract.

    Focusing between the measurement and the click is the subtle half of this bug:
    `bring_to_front` can change layout/scroll, so the coordinates measured before it
    are stale by the time the click lands. The click then misses the comment button,
    the dialog never opens, no comment XHR fires, and every reel reports
    `new=0 total=0` — indistinguishable from reels that genuinely have no comments.
    Observed live 2026-08-20: reels that returned total=3 an hour earlier returned
    total=0 on every fetch until the focus call moved ahead of the measurement.
    """
    from aizu.engines.instagram.cdp import CDPFeed

    feed = CDPFeed.__new__(CDPFeed)
    feed._focus = lambda page: page.calls.append("bring_to_front")

    class _Cfg:
        settle_seconds = 0
    feed.cfg = _Cfg()

    page = _Page()
    page.evaluate = lambda _js: (page.calls.append("measure"), {"x": 10, "y": 20})[1]
    feed._open_comment_dialog(page)

    assert page.calls == ["bring_to_front", "measure", "mouse.click"], (
        f"measurement must happen after focus and before the click: {page.calls}")


def test_the_reel_permalink_is_the_post_form_not_the_swipeable_reel_form():
    """The permalink bounce, which was the biggest single loss in the funnel.

    `/reel/<code>/` is served into Instagram's swipeable /reels/ surface, which
    restores its own scroll position and drops the requested code — so the engine
    asks for reel X and lands on reel Y. `_open_reel_landing_check` then correctly
    refuses to read comments there (they would be attributed to the wrong reel), and
    the reel is skipped. Measured live 2026-08-21 on four real codes: /reel/ bounced
    3 of 4, /p/ landed correctly 4 of 4. Across a day of runs that cost 22 bounces
    plus 24 "unavailable" out of 65 relevant reels — ~70% of everything correctly
    identified as on-campaign never reached comment scoring.

    Pinned as a constant test because the failure is SILENT at this layer: a bounced
    reel is a warning and a skip, never an error, so a regression here shows up only
    as a quietly lower lead count.
    """
    from aizu.engines.instagram.cdp import REEL_PERMALINK, _CODE_IN_PAGE_URL

    url = REEL_PERMALINK.format(code="DXaiAECDLYZ")
    assert "/p/" in url, f"expected the post permalink form, got {url}"
    assert "/reel/" not in url, "the singular /reel/ form bounces off the swipeable surface"
    # The landing check and the attribution fallback both key off the code in the
    # URL, so the chosen form must remain parseable by the same regex.
    m = _CODE_IN_PAGE_URL.search(url)
    assert m and m.group(1) == "DXaiAECDLYZ"


def test_the_comment_scroller_is_found_on_the_post_page_not_only_in_a_dialog():
    """The last link in the funnel, and it broke the moment the permalink moved.

    `/reel/<code>/` opened a modal, so scoping the scroller lookup to
    `div[role=dialog]` was right. `/p/<code>/` renders comments INLINE — measured on
    a post with 12 comments: hasDialog=false, with a page-level scroller of
    scrollHeight 2172 vs clientHeight 382 holding the real comment list. The
    dialog-only lookup returned False on every such reel, pagination never ran, and
    23 of 23 fetches reported `new=0 total=0`.

    False is also the branch that falls through to the humanised mouse-scroll, which
    is the path the owner-thread wedge lives on — so this fix removes a whole class
    of mouse input from the comment path as well as unblocking pagination.
    """
    import inspect
    from aizu.engines.instagram.cdp import CDPFeed

    src = inspect.getsource(CDPFeed._scroll_comment_dialog)
    assert 'document.querySelector("div[role=dialog]") || document.body' in src, (
        "the scroller lookup must fall back to the page when no dialog is open")
    assert "if (!dlg) return false;" not in src, (
        "the dialog-only early return is what silenced comments on /p/")


def test_the_comment_scroller_reports_failure_when_nothing_is_scrollable():
    """The fallback must not become an unconditional True: a page with no scroller
    still has to report False so the caller stops paginating instead of looping."""
    from aizu.engines.instagram.cdp import CDPFeed

    feed = CDPFeed.__new__(CDPFeed)
    page = type("P", (), {"evaluate": staticmethod(lambda _js: False)})()
    assert feed._scroll_comment_dialog(page) is False
