"""walk()'s per-source accounting: what each seed produced, reported once per
source (Campaign Lab, Remedy Sheet #1 / Remedy D).

The behaviour under test is the one the 2026-08-19 live run got wrong: reels
intercepted on a redirected hashtag page but drained under a later seed account
were logged as that account's yield. `SourceOutcome` separates the two, and the
sink must fire for every source — including the one the consumer abandoned the
generator on, which is by definition the productive one.
"""
import pytest

from aizu.core.cdp import CDPBaseConfig, CDPFeedBase
from aizu.core.feed import (SOURCE_ACCOUNT, SOURCE_HASHTAG, SOURCE_HOME,
                            SOURCE_UNKNOWN, Reel)


class _Page:
    def __init__(self, url="https://example.test/feed"):
        self.url = url

        class _Mouse:
            def wheel(self, dx, dy):
                pass

        self.mouse = _Mouse()

    def goto(self, url, **kw):
        self.url = url

    def set_default_timeout(self, ms): pass
    def set_default_navigation_timeout(self, ms): pass
    def on(self, event, handler): pass


class _Feed(CDPFeedBase):
    """A feed whose sources mirror every CDP engine's shape:
    [home] + seed_hashtags + seed_accounts."""

    TAG_URL = "https://example.test/explore/tags/{t}/"
    ACCT_URL = "https://example.test/{a}/reels/"
    HOME_URL = "https://example.test/reels/"

    def __init__(self, cfg, queue_by_source=None):
        super().__init__(cfg)
        self._page = _Page()
        self.outcomes = []
        self.on_source_done = self.outcomes.append
        self._queue_by_source = queue_by_source or {}

    def _url_hints(self): return ("/api/",)
    def _classify(self, url, body, response): pass
    def _scroll(self, page=None): pass
    def _navigate(self, url):
        # Simulate interception landing during this source's nav.
        for rid in self._queue_by_source.get(url, []):
            self._enqueue_reel(Reel(reel_id=rid))

    def _sources(self):
        urls = []
        if self.cfg.include_home_feed:
            urls.append(self.HOME_URL)
        urls += [self.TAG_URL.format(t=t) for t in self.cfg.seed_hashtags]
        urls += [self.ACCT_URL.format(a=a) for a in self.cfg.seed_accounts]
        return urls or [self.HOME_URL]


def _cfg(**kw):
    base = dict(per_source_reels=10, empty_scrolls_before_stop=1, settle_seconds=0,
                nav_settle_seconds=0, seed_hashtags=("remont",),
                seed_accounts=("acme",), include_home_feed=True)
    base.update(kw)
    return CDPBaseConfig(**base)


def test_each_source_is_labelled_by_position_not_by_url_parsing():
    feed = _Feed(_cfg())
    list(feed.walk())
    assert [(o.source, o.kind) for o in feed.outcomes] == [
        ("home", SOURCE_HOME), ("remont", SOURCE_HASHTAG), ("acme", SOURCE_ACCOUNT)]


def test_a_handle_containing_a_seeded_tag_is_still_an_account():
    """Substring-matching the tag against the URL is what breaks here — the
    account URL contains the seeded tag verbatim."""
    feed = _Feed(_cfg(seed_hashtags=("remont",), seed_accounts=("remont_studio",),
                      include_home_feed=False))
    list(feed.walk())
    assert [(o.source, o.kind) for o in feed.outcomes] == [
        ("remont", SOURCE_HASHTAG), ("remont_studio", SOURCE_ACCOUNT)]


def test_an_unexpected_sources_shape_degrades_to_unknown_not_to_a_wrong_label():
    class _Odd(_Feed):
        def _sources(self):
            return ["https://example.test/one"]

    feed = _Odd(_cfg(seed_hashtags=("a", "b"), seed_accounts=("c",)))
    list(feed.walk())
    assert [o.kind for o in feed.outcomes] == [SOURCE_UNKNOWN]


def test_carried_over_reels_are_not_credited_to_the_draining_source():
    """The live 2026-08-19 shape, end to end: a redirected tag page intercepts
    reels it never gets to drain, and the NEXT source drains them."""
    cfg = _cfg(per_source_reels=2, include_home_feed=False)
    tag_url = _Feed.TAG_URL.format(t="remont")
    feed = _Feed(cfg, queue_by_source={tag_url: ["p1", "p2", "p3"]})
    # The tag source redirects, so it drains only what fits before it stops.
    feed._source_redirected = lambda req, landed: req == tag_url
    walked = [r.reel_id for r in feed.walk()]
    assert walked == ["p1", "p2", "p3"]
    tag, acct = feed.outcomes
    assert (tag.source, tag.yielded, tag.redirected) == ("remont", 2, True)
    assert (acct.source, acct.yielded, acct.carried_over) == ("acme", 0, 1)


def test_an_unavailable_source_is_recorded_and_never_scrolled(monkeypatch):
    feed = _Feed(_cfg(empty_scrolls_before_stop=4, include_home_feed=False,
                      seed_accounts=()))
    feed._page_unavailable = lambda page: True
    scrolls = []
    monkeypatch.setattr(feed, "_scroll", lambda page=None: scrolls.append(1))
    list(feed.walk())
    (out,) = feed.outcomes
    assert out.unavailable is True
    assert scrolls == []          # the four empty-scroll rounds are not burned


def test_a_failing_probe_means_available():
    def _boom(page):
        raise RuntimeError("evaluate timed out")

    feed = _Feed(_cfg(include_home_feed=False, seed_accounts=()))
    feed._page_unavailable = _boom
    list(feed.walk())
    assert feed.outcomes[0].unavailable is False


def test_the_source_the_consumer_abandons_is_still_recorded():
    """A run stops the moment it hits its lead target — on the source that
    worked. Recording only on normal exit would under-credit exactly that seed."""
    cfg = _cfg(include_home_feed=False, seed_accounts=())
    tag_url = _Feed.TAG_URL.format(t="remont")
    feed = _Feed(cfg, queue_by_source={tag_url: ["p1", "p2", "p3"]})
    gen = feed.walk()
    assert next(gen).reel_id == "p1"
    gen.close()                                    # consumer walks away
    (out,) = feed.outcomes
    assert (out.source, out.yielded) == ("remont", 1)


def test_a_raising_sink_never_breaks_the_walk():
    feed = _Feed(_cfg(include_home_feed=False, seed_accounts=()))

    def _boom(outcome):
        raise RuntimeError("db is gone")

    feed.on_source_done = _boom
    assert list(feed.walk()) == []                 # walk completes regardless


def test_a_page_that_served_items_is_never_called_unavailable():
    """The per-platform probes are innerText regexes and LinkedIn renders
    "this content isn't available" inline for a single removed post inside an
    otherwise healthy feed. Interception is the stronger evidence."""
    cfg = _cfg(include_home_feed=False, seed_accounts=())
    tag_url = _Feed.TAG_URL.format(t="remont")
    feed = _Feed(cfg, queue_by_source={tag_url: ["p1"]})
    feed._page_unavailable = lambda page: True
    walked = [r.reel_id for r in feed.walk()]
    assert walked == ["p1"]
    assert feed.outcomes[0].unavailable is False
