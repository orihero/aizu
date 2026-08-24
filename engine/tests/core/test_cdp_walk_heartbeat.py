"""The feed's own liveness heartbeat between two ``walk()`` yields.

WHY THIS FILE EXISTS. ``Session`` bumps ``sessions.last_activity_at`` when the
walk hands it a reel, and ``_HeartbeatRouter`` bumps it on every model verdict.
Neither can see the interval BETWEEN two yields: that is nav + the landed/login
probes + up to ``empty_scrolls_before_stop`` scroll rounds, and then the whole
thing again for the NEXT source. A brief whose sources are unproductive — six
redirecting hashtag pages, live 2026-08-19 — repeats that with no reel to yield
and therefore nothing at all to bump, for as many sources as the brief has.

That is the last unbounded gap on the feed-walk path, and the bridge watchdog
(``session_watchdog.STALL_TIMEOUT_SEC`` = 180s) halts the session when a gap
crosses it — which is how job-2099fb29e88b dead-lettered five times with
``{"reason": "worker_stall"}``.

``CDPFeedBase._progress()`` closes it. These tests lock the two properties that
make it a real bound rather than a hopeful one: it fires after every unit of
completed feed work, and it fires often enough that the WORST-CASE gap between
two consecutive bumps stays under the watchdog with margin.
"""
import random
import time as _time

from aizu.core.cdp import CDPBaseConfig, CDPFeedBase
from aizu.core.feed import Reel
from aizu.core.human import HumanSim, HumanSimConfig
from aizu.session_watchdog import STALL_TIMEOUT_SEC

# The worst-case wall clock each step of a walk can burn, from the ceilings the
# code actually enforces. Used to drive the fake clock in the invariant test, so
# it fails the moment a ceiling is loosened or a bump is removed.
#
#   nav      = human.goto: think (<=2.4) + page.goto (nav_timeout_ms 20s) +
#              nav_settle (2.5) + mouse_move (2 bounded calls @ js_timeout_ms
#              15s) + the "nav" delay (<=4.2, or <=22 on a 6% long idle)
#   probes   = _landed_url (a page.url property read) + _login_wall_reason (a
#              substring test) — free, but modelled as non-zero on purpose
#   scroll   = mouse_move (30) + the notch batch (max_scroll_seconds 20 + one
#              js_timeout_ms 15 = 35) + the post-batch "scroll" settle (<=2.3,
#              or <=22 on a long idle) + walk()'s own settle_seconds (1.5)
WORST_NAV_SEC = 2.4 + 20.0 + 2.5 + 30.0 + 22.0          # 76.9
WORST_PROBES_SEC = 1.0
WORST_SCROLL_ROUND_SEC = 30.0 + 20.0 + 15.0 + 22.0 + 1.5  # 88.5


class _Clock:
    """Manual monotonic clock — a walk's worst case is minutes of wall clock and
    nothing here may actually sleep."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, secs):
        self.now += secs


class _DryWalkFeed(CDPFeedBase):
    """A base subclass whose sources all land fine and never produce a reel — the
    unproductive-source shape, with every real Playwright call replaced by a clock
    advance of its documented worst case."""

    def __init__(self, sources, cfg=None, clock=None, queue_after=None):
        self.clock = clock or _Clock()
        human = HumanSim(HumanSimConfig(enabled=False), rng=random.Random(3),
                         sleep=lambda s: None, clock=self.clock)
        cfg = cfg or CDPBaseConfig(settle_seconds=0.0)
        super().__init__(cfg, clock=self.clock, human=human)
        self._page = object()          # non-None so _navigate isn't a no-op
        self._src = list(sources)
        self.navigations = []
        self.scrolls = 0
        # reel_id to enqueue on the Nth scroll of a source ({scroll_index: reel_id})
        self._queue_after = queue_after or {}
        self.beats = []                # clock value at each heartbeat

    # -- platform hooks --
    def _url_hints(self):
        return ("/api/posts",)

    def _classify(self, url, body, response):
        pass

    def _sources(self):
        return self._src

    # -- every real call, replaced by its worst-case cost --
    def _navigate(self, url):
        self.navigations.append(url)
        self.clock.advance(WORST_NAV_SEC)

    def _landed_url(self):
        self.clock.advance(WORST_PROBES_SEC)
        return self.navigations[-1] if self.navigations else ""

    def _scroll(self, page=None):
        self.scrolls += 1
        self.clock.advance(WORST_SCROLL_ROUND_SEC)
        rid = self._queue_after.get(self.scrolls)
        if rid:
            self._enqueue_reel(Reel(reel_id=rid, caption="c", author="a"))

    def _halt_if_owner_wedged(self):
        pass

    def record_beat(self):
        self.beats.append(self.clock.now)


def _walked(feed):
    feed.on_progress = feed.record_beat
    return list(feed.walk())


def test_a_source_that_yields_no_reel_still_beats_once_it_has_landed():
    feed = _DryWalkFeed(["https://x.test/a", "https://x.test/b"])
    assert _walked(feed) == []
    # One beat per navigation at minimum — otherwise a brief made entirely of
    # unproductive sources produces no heartbeat for its whole duration.
    assert len(feed.beats) >= len(feed.navigations) == 2


def test_every_completed_scroll_round_beats_not_just_the_last_one():
    cfg = CDPBaseConfig(settle_seconds=0.0, empty_scrolls_before_stop=4,
                        max_source_seconds=10_000.0)   # let the empty-scroll rule end it
    feed = _DryWalkFeed(["https://x.test/a"], cfg=cfg)
    assert _walked(feed) == []
    assert feed.scrolls == 4
    # 1 nav + 4 scroll rounds. A bump only at the end of the source would let the
    # four rounds run as one silent block.
    assert len(feed.beats) == 5


def test_the_worst_case_gap_between_two_heartbeats_stays_under_the_watchdog():
    """THE invariant. Ten unproductive sources, every step charged its documented
    worst case: no two consecutive bumps may be STALL_TIMEOUT_SEC apart."""
    feed = _DryWalkFeed([f"https://x.test/{i}" for i in range(10)])
    assert _walked(feed) == []
    assert feed.beats, "a walk that never beats is the stall itself"
    # The run's very first bump is preceded by Session's own (it bumps when the
    # session row is created), so measure from the walk's start too.
    marks = [1000.0] + feed.beats
    gaps = [b - a for a, b in zip(marks, marks[1:])]
    assert max(gaps) < STALL_TIMEOUT_SEC, (
        f"worst gap {max(gaps):.1f}s >= watchdog {STALL_TIMEOUT_SEC}s")
    # And with real margin, not by a hair — a ceiling that only just clears the
    # watchdog is one loosened constant away from the 2026-08-20 dead-letter.
    assert max(gaps) < STALL_TIMEOUT_SEC / 2


def test_the_gap_that_spans_a_source_change_is_measured_too():
    """The last scroll of source N and the nav of source N+1 are consecutive with
    no yield between them — the seam the per-source ceilings never covered."""
    cfg = CDPBaseConfig(settle_seconds=0.0, empty_scrolls_before_stop=1,
                        max_source_seconds=10_000.0)
    feed = _DryWalkFeed(["https://x.test/a", "https://x.test/b"], cfg=cfg)
    _walked(feed)
    gaps = [b - a for a, b in zip(feed.beats, feed.beats[1:])]
    # scroll(a) -> nav(b) is one such seam and must be a single step's worth.
    assert max(gaps) < STALL_TIMEOUT_SEC
    assert len(feed.beats) == 4          # nav a, scroll a, nav b, scroll b


def test_a_reel_yielded_between_scrolls_does_not_replace_the_scroll_beats():
    """The productive path keeps its own bumps: Session beats on the yield, the
    feed still beats on the work that produced it."""
    cfg = CDPBaseConfig(settle_seconds=0.0, per_source_reels=1,
                        max_source_seconds=10_000.0)
    feed = _DryWalkFeed(["https://x.test/a"], cfg=cfg, queue_after={2: "R1"})
    reels = _walked(feed)
    assert [r.reel_id for r in reels] == ["R1"]
    assert feed.scrolls == 2
    assert len(feed.beats) == 3          # nav + both scroll rounds


def test_a_feed_with_no_session_wired_walks_exactly_as_before():
    """The hook defaults to a no-op, so a feed used outside a Session (CLI probe,
    unit test) is untouched."""
    feed = _DryWalkFeed(["https://x.test/a"])
    assert list(feed.walk()) == []       # no on_progress assigned at all
    assert feed.navigations == ["https://x.test/a"]


def test_a_heartbeat_that_raises_never_breaks_the_walk():
    """The bump writes to SQLite. A locked/broken DB must cost a debug line, not
    the harvest — _flush() will surface it at the end of the reel anyway."""
    cfg = CDPBaseConfig(settle_seconds=0.0, max_source_seconds=10_000.0)
    feed = _DryWalkFeed(["https://x.test/a"], cfg=cfg, queue_after={1: "R1"})

    def boom():
        raise RuntimeError("database is locked")

    feed.on_progress = boom
    assert [r.reel_id for r in feed.walk()] == ["R1"]


# ---- the comment-dialog path beats too -------------------------------------
# `_open_comments_and_paginate` runs inside `fetch_comments`, which
# `_process_comments` calls BEFORE its first `_touch()`. Its own deadline
# (max_comment_pagination_seconds) only stops the NEXT round from starting, so
# without a per-round bump the dialog-open click plus every round in flight is one
# silent block — and a round that falls through to the page-wide `_scroll(page)`
# is worth a whole notch batch on its own.

class _DialogPage:
    """Interaction page for `_open_comments_and_paginate`. ``_impl_obj`` here and
    on the mouse is load-bearing: on a live session ``_ipage`` is an ``OwnedPW``
    and both callers use `page.evaluate` / `page.mouse.click` directly, so the
    proxy is the only thing bounding them."""

    _impl_obj = object()

    def __init__(self):
        self.evaluate_calls = 0

        class _Mouse:
            _impl_obj = object()

            def click(self, x, y):
                pass

            def move(self, x, y, steps=1):
                pass

            def wheel(self, dx, dy):
                pass

        self.mouse = _Mouse()

    def evaluate(self, *a, **k):
        self.evaluate_calls += 1
        # Truthy for both callers: _open_comment_dialog reads x/y, and
        # _scroll_comment_dialog only bool()s it (so it never falls through).
        return {"x": 10.0, "y": 20.0}


def test_every_comment_pagination_round_beats():
    from aizu.engines.instagram.cdp import CDPConfig as IGConfig
    from aizu.engines.instagram.cdp import CDPFeed as IGFeed

    feed = IGFeed(IGConfig(js_timeout_ms=200, settle_seconds=0.0,
                           max_comment_scrolls=3,
                           max_comment_pagination_seconds=10_000.0))
    beats = []
    feed.on_progress = lambda: beats.append(1)
    page = _DialogPage()
    feed._ipage = feed._wrap_pw(page)
    feed._open_comments_and_paginate()
    # One per completed round — not one for the whole method.
    assert len(beats) == 3, f"beats={len(beats)} evaluates={page.evaluate_calls}"


# ---- the session actually wires it -----------------------------------------

class _ProgressFeed:
    """The minimum surface `Session.__init__` touches, plus the hook."""
    on_progress = staticmethod(lambda: None)

    def walk(self):
        return iter(())

    def healthy(self):
        return True


def test_the_session_wires_its_heartbeat_into_the_feed():
    """A hook nothing assigns is a no-op, and the walk-side bound would silently
    not exist. Checked on all three CDP engines: they keep private session copies
    and a fix applied to one of them is the repo's classic drift."""
    from aizu.core.config import campaign_from_brief
    from aizu.engines.instagram.session import Session as IGSession
    from aizu.engines.linkedin.session import LinkedInSession
    from aizu.engines.x.session import XSession
    from tests.engines.heartbeat_harness import make_store

    platforms = {"instagram": IGSession, "linkedin": LinkedInSession,
                 "x": XSession}
    for platform, cls in platforms.items():
        store, _path = make_store([])
        campaign = campaign_from_brief(f"{platform}-c", {
            "platform": platform, "threshold": 0.7,
            "relevance_def": "r", "match_def": "m", "extract_def": "e"})
        feed = _ProgressFeed()
        cls(store=store, router=object(), feed=feed, soul=None, campaign=campaign)
        assert getattr(feed.on_progress, "__func__", None) is cls._touch, (
            f"{platform}: the session left the feed's heartbeat hook "
            f"unwired (on_progress={feed.on_progress!r})")
        store.close()


def test_a_feed_without_the_hook_is_left_alone():
    """FakeFeed and every non-CDP feed have no `on_progress`; the session must not
    invent one on them."""
    from aizu.core.config import campaign_from_brief
    from aizu.core.feed import FakeFeed
    from aizu.engines.instagram.session import Session as IGSession
    from tests.engines.heartbeat_harness import make_store

    store, _path = make_store([])
    feed = FakeFeed([])
    IGSession(store=store, router=object(), feed=feed, soul=None,
              campaign=campaign_from_brief("c", {
                  "platform": "instagram", "threshold": 0.7,
                  "relevance_def": "r", "match_def": "m", "extract_def": "e"}))
    assert not hasattr(feed, "on_progress")
    store.close()


# ---- the sibling engines paginate the same way and needed the same bump -----
# linkedin/cdp.py and x/cdp.py keep private copies of the comment-pagination
# loop, and neither has instagram's `max_comment_pagination_seconds` deadline —
# they bound ROUNDS only. X is the worst of the three: `_load_replies` and
# `_load_quotes` are `max_comment_scrolls` rounds EACH, back to back inside one
# `fetch_comments`, i.e. 6 full notch batches (~530s) before `_process_comments`
# reaches its first `_touch()`. Three engines, one watchdog.

class _SilentPage:
    """A page whose every call is free — the loop structure is what is under test,
    not the browser. ``_impl_obj`` makes it wrappable as an ``OwnedPW``."""

    _impl_obj = object()

    def __init__(self):
        class _Mouse:
            _impl_obj = object()

            def click(self, x, y):
                pass

            def move(self, x, y, steps=1):
                pass

            def wheel(self, dx, dy):
                pass

        self.mouse = _Mouse()

    def evaluate(self, *a, **k):
        return {"x": 1.0, "y": 2.0}

    def goto(self, *a, **k):
        pass

    def query_selector(self, *a, **k):
        return None


def _beating_feed(cls, cfg):
    feed = cls(cfg)
    feed._scroll = lambda page=None: None      # the batch itself is bounded elsewhere
    feed._ipage = feed._wrap_pw(_SilentPage())
    beats = []
    feed.on_progress = lambda: beats.append(1)
    return feed, beats


def test_linkedin_comment_pagination_beats_every_round():
    from aizu.engines.linkedin.cdp import LinkedInCDPConfig, LinkedInFeed

    feed, beats = _beating_feed(
        LinkedInFeed, LinkedInCDPConfig(js_timeout_ms=200, settle_seconds=0.0,
                                        max_comment_scrolls=3))
    feed._open_comments_and_paginate()
    assert len(beats) == 3, beats


def test_x_beats_every_round_of_BOTH_its_reply_and_quote_passes():
    from aizu.engines.x.cdp import XCDPConfig, XFeed

    feed, beats = _beating_feed(
        XFeed, XCDPConfig(js_timeout_ms=200, settle_seconds=0.0,
                          nav_settle_seconds=0.0, max_comment_scrolls=3))
    feed.fetch_comments("1234567890", None)
    # 3 reply rounds + 3 quote rounds. Counting only one pass would leave the
    # other one as a single silent block of the same size.
    assert len(beats) == 6, beats
