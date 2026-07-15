"""Slice B — Instagram CDP write helpers: save_reel + share_reel (warming-writes
PRD §4.3, O-share-target, O-dm-regex).

These exercise the *logic* with no live Chrome:
  - the FeedSource base methods are no-ops returning False (harvest stays read-only);
  - share_reel's recipient resolution (given share_target handle → search+type;
    else FIRST contact positional pick) is wired correctly;
  - the share choreography aborts cleanly when a step's element is missing;
  - the DM-share structural hook lives in detect_action_block (O-dm-regex TODO).
"""
from reelradar.core.feed import FeedSource, Reel
from reelradar.engines.instagram.cdp import CDPFeed, CDPConfig


# ---- base no-ops: harvest/read-only feeds must not write ----

def test_feedsource_save_reel_is_noop_false():
    assert FeedSource().save_reel(Reel(reel_id="r1")) is False


def test_feedsource_share_reel_is_noop_false():
    assert FeedSource().share_reel(Reel(reel_id="r1")) is False
    assert FeedSource().share_reel(Reel(reel_id="r1"), target="@x") is False


# ---- a fake Playwright page that records the share choreography ----

class _FakeElement:
    def __init__(self, recorder, name):
        self._rec = recorder
        self._name = name
        self.typed: list[str] = []

    def click(self):
        self._rec.clicks.append(self._name)

    def type(self, text):
        self.typed.append(text)
        self._rec.typed.append((self._name, text))


class _FakePage:
    """Resolves a configured set of selectors to elements; everything else None.

    ``selector_map`` maps a *substring* of the query to the element name to
    return, so we don't have to reproduce IG's exact selector strings.
    """
    def __init__(self, selector_map, *, centermost_ok=True, blocked=False):
        self.url = "https://www.instagram.com/reel/abc123/"
        self._selector_map = selector_map
        self.centermost_ok = centermost_ok
        self.blocked = blocked
        self.clicks: list[str] = []
        self.typed: list[tuple[str, str]] = []
        self.elements: dict[str, _FakeElement] = {}

    def query_selector(self, selector):
        for needle, name in self._selector_map.items():
            if needle in selector:
                el = self.elements.setdefault(name, _FakeElement(self, name))
                return el
        return None

    def evaluate(self, *_args, **_kwargs):
        # detect_action_block reads body text; report blocked state.
        return self.blocked


def _feed_with_page(page):
    feed = CDPFeed(CDPConfig(settle_seconds=0, nav_settle_seconds=0))
    feed._ipage = page
    # Neutralize the human pacing jitter so tests stay fast/deterministic.
    feed._human_pause = lambda: None
    # _click_centermost drives JS over a real page; stub it to a recorded boolean
    # so share_reel's *resolution* logic (not the DOM click mechanics) is tested.
    feed._click_centermost = lambda pg, sel: page.centermost_ok
    return feed


# ---- share_target resolution ----

def test_share_reel_with_target_searches_and_types_handle():
    page = _FakePage({"Search": "search_box", "option": "recipient", "Send": "send"})
    feed = _feed_with_page(page)

    assert feed.share_reel(Reel(reel_id="r1"), "warming_safe") is True
    # The handle was typed into the search box (target path), recipient + send clicked.
    assert ("search_box", "warming_safe") in page.typed
    assert page.clicks == ["recipient", "send"]


def test_share_reel_without_target_picks_first_contact_no_typing():
    page = _FakePage({"option": "recipient", "Send": "send"})
    feed = _feed_with_page(page)

    assert feed.share_reel(Reel(reel_id="r1"), None) is True
    # Positional fallback: no search box typed into, first contact picked.
    assert page.typed == []
    assert page.clicks == ["recipient", "send"]


def test_share_reel_target_defaults_to_none():
    page = _FakePage({"option": "recipient", "Send": "send"})
    feed = _feed_with_page(page)
    # target is optional; omitting it must behave like the positional fallback.
    assert feed.share_reel(Reel(reel_id="r1")) is True
    assert page.typed == []


# ---- abort paths ----

def test_share_reel_returns_false_when_no_ipage():
    feed = CDPFeed(CDPConfig(settle_seconds=0))
    feed._ipage = None
    assert feed.share_reel(Reel(reel_id="r1"), "x") is False


def test_share_reel_aborts_when_share_sheet_will_not_open():
    page = _FakePage({"option": "recipient", "Send": "send"}, centermost_ok=False)
    feed = _feed_with_page(page)
    assert feed.share_reel(Reel(reel_id="r1"), None) is False
    assert page.clicks == []                      # never reached recipient/send


def test_share_reel_aborts_when_search_box_missing_for_target():
    # target given but no search box → can't resolve the named recipient.
    page = _FakePage({"option": "recipient", "Send": "send"})
    feed = _feed_with_page(page)
    assert feed.share_reel(Reel(reel_id="r1"), "warming_safe") is False
    assert page.clicks == []


def test_share_reel_aborts_when_no_recipient():
    page = _FakePage({"Send": "send"})
    feed = _feed_with_page(page)
    assert feed.share_reel(Reel(reel_id="r1"), None) is False
    assert page.clicks == []


def test_share_reel_aborts_when_no_send_button():
    page = _FakePage({"option": "recipient"})
    feed = _feed_with_page(page)
    assert feed.share_reel(Reel(reel_id="r1"), None) is False
    assert page.clicks == ["recipient"]           # picked contact but no Send


def test_share_reel_false_when_action_block_after_send():
    page = _FakePage({"option": "recipient", "Send": "send"}, blocked=True)
    feed = _feed_with_page(page)
    # Send clicked but an action-block toast appeared → not a success.
    assert feed.share_reel(Reel(reel_id="r1"), None) is False
    assert page.clicks == ["recipient", "send"]


# ---- save_reel wiring ----

def test_save_reel_clicks_when_control_present():
    page = _FakePage({})
    feed = _feed_with_page(page)            # _click_centermost stubbed → True
    assert feed.save_reel(Reel(reel_id="r1")) is True


def test_save_reel_false_when_control_absent():
    page = _FakePage({}, centermost_ok=False)
    feed = _feed_with_page(page)
    assert feed.save_reel(Reel(reel_id="r1")) is False


# ---- O-dm-regex: structural hook present (no invented phrases) ----

def test_detect_action_block_has_dm_share_structural_hook():
    import inspect
    src = inspect.getsource(CDPFeed.detect_action_block)
    # The hook + TODO must exist; we deliberately do NOT assert specific phrases
    # (those await a live DM-block sample per O-dm-regex).
    assert "TODO" in src
    assert "O-dm-regex" in src
