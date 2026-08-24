"""FIX P0: the per-reel wall-clock budget bounds the BROWSER block, not the cascade.

``reel_start`` used to be anchored BEFORE ``cascade.gate_reel()``, so a reel whose
CLASSIFICATION overran ``per_reel_seconds`` was marked seen and then skipped before
``open_reel`` ever ran. ``store.is_seen`` is a bare existence check with no TTL and
nothing in ``aizu/`` deletes from ``seen_reels``, so that reel was blacklisted for
the campaign forever. The 2026-08-19 live Instagram run destroyed reel DFdnoSsgWBk
(relevant=True, score 0.85, confidence 0.90) exactly that way and still reported
completed / relevance_passes=0 / matches=0 / zero health flags.

These tests pin the corrected contract: classification time is not charged to the
browser budget, ``relevance_passes`` agrees with ``seen_reels.relevant``, and any
relevant reel that IS dropped after mark_seen leaves a health flag behind. The
clock is injected so the deadline is driven deterministically, with no sleeping.
"""
import os
import sqlite3
import tempfile

from aizu.core.config import campaign_from_brief
from aizu.core.feed import Comment, FakeFeed, Reel
from aizu.core.pacing import Pacer, PacingConfig
from aizu.core.router import Decision
from aizu.core.store import Store
from aizu.engines.instagram.session import (
    RELEVANT_REEL_DISCARDED_FLAG, Session, SessionConfig)


class _Clock:
    """Injectable monotonic clock the test (and the fakes below) advance by hand."""

    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SlowGateRouter:
    """Router whose RELEVANCE call burns wall-clock — this models the escalation
    path, which is slow precisely on the borderline-but-genuine content that is
    most likely to be a lead. Captions containing "acme" are relevant; comments
    containing "pricing" are matches."""

    def __init__(self, clock: _Clock, gate_seconds: float = 0.0):
        self._clock = clock
        self._gate_seconds = gate_seconds

    def classify_text(self, *, instruction, content, campaign_id, stage,
                      session_id=None, system=None):
        low = content.lower()
        if stage == "relevance":
            self._clock.advance(self._gate_seconds)
            rel = "acme" in low
            return Decision(label="relevant" if rel else "irrelevant",
                            score=0.9 if rel else 0.1, confidence=0.97)
        lead = "pricing" in low
        return Decision(label="yes" if lead else "no",
                        score=0.93 if lead else 0.1, confidence=0.97,
                        extracted={"phone": "+14155550142"} if lead else {})

    def classify_image(self, *, instruction, images_b64, campaign_id, stage,
                       session_id=None, system=None):
        return Decision(label="irrelevant", score=0.0, confidence=1.0)


class SlowOpenFeed(FakeFeed):
    """open_reel burns wall-clock — the real browser work the budget is meant to
    bound (permalink navigation on a wedged tab)."""

    def __init__(self, reels, clock: _Clock, open_seconds: float):
        super().__init__(reels)
        self._clock = clock
        self._open_seconds = open_seconds

    def open_reel(self, reel):
        self._clock.advance(self._open_seconds)
        return True


class UnavailableReelFeed(FakeFeed):
    """open_reel fails for the named reel — a deleted/unavailable permalink."""

    def __init__(self, reels, failing_id: str):
        super().__init__(reels)
        self._failing_id = failing_id

    def open_reel(self, reel):
        return reel.reel_id != self._failing_id


def _store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path), path


def _campaign():
    return campaign_from_brief("ig-leadgen", {
        "platform": "instagram", "threshold": 0.7,
        "relevance_def": "acme saas app posts",
        "match_def": "a commenter asking to buy / price / contact",
        "extract_def": "- phone",
    })


def _daytime_pacer():
    return Pacer(cfg=PacingConfig(enforce_daytime=False), sleep=lambda _t: None)


def _seen_relevant_ids(path: str) -> set[str]:
    con = sqlite3.connect(path)
    rows = con.execute(
        "SELECT reel_id FROM seen_reels WHERE relevant = 1").fetchall()
    con.close()
    return {r[0] for r in rows}


def _discard_flags(store: Store) -> list[dict]:
    return [f for f in store.open_flags()
            if f["kind"] == RELEVANT_REEL_DISCARDED_FLAG]


def _relevant_reel(reel_id: str) -> Reel:
    return Reel(reel_id, caption="acme saas app dashboard",
                comments=[Comment(f"c-{reel_id}", f"u-{reel_id}", "pricing?")])


def test_reel_whose_classification_overruns_the_budget_still_reaches_comment_scoring():
    store, _path = _store()
    clock = _Clock()
    # 100s > the 90s default budget, spent entirely inside the relevance gate.
    router = SlowGateRouter(clock, gate_seconds=100.0)
    out = Session(store=store, router=router, feed=FakeFeed([_relevant_reel("slow-gate")]),
                  soul=None, campaign=_campaign(), pacer=_daytime_pacer(),
                  cfg=SessionConfig(), clock=clock).run()

    # Before the fix the gate's own 100s tripped the pre-block deadline check and
    # the reel was dropped between mark_seen and open_reel: matches==0.
    assert out["matches"] == 1
    assert out["relevance_passes"] == 1
    assert out["halt_reason"] is None
    assert _discard_flags(store) == []       # nothing was lost, so nothing to flag


def test_every_relevant_reel_is_scored_even_when_all_classifications_overrun():
    store, _path = _store()
    clock = _Clock()
    router = SlowGateRouter(clock, gate_seconds=100.0)
    reels = [_relevant_reel("first"), _relevant_reel("second")]
    out = Session(store=store, router=router, feed=FakeFeed(reels), soul=None,
                  campaign=_campaign(), pacer=_daytime_pacer(),
                  cfg=SessionConfig(), clock=clock).run()

    # The old loop charged classification to the browser budget, so EVERY reel
    # breached and the walk produced zero leads while reporting success.
    assert out["reels_seen"] == 2
    assert out["relevance_passes"] == 2
    assert out["matches"] == 2
    assert out["halt_reason"] is None


def test_relevance_passes_never_disagrees_with_the_persisted_seen_reels_verdict():
    store, path = _store()
    clock = _Clock()
    router = SlowGateRouter(clock, gate_seconds=100.0)
    reels = [_relevant_reel("hit"), Reel("miss", caption="cat memes", comments=[])]
    out = Session(store=store, router=router, feed=FakeFeed(reels), soul=None,
                  campaign=_campaign(), pacer=_daytime_pacer(),
                  cfg=SessionConfig(), clock=clock).run()

    # The live run reported relevance_passes=0 while seen_reels held a relevant=1
    # row — that disagreement is what hid the lost lead in the summary.
    assert _seen_relevant_ids(path) == {"hit"}
    assert out["relevance_passes"] == len(_seen_relevant_ids(path)) == 1


def test_browser_work_that_burns_the_budget_skips_comments_and_flags_the_lost_reel():
    store, _path = _store()
    clock = _Clock()
    router = SlowGateRouter(clock, gate_seconds=0.0)
    # open_reel alone eats 100s > the 90s budget → the comment stage is not entered.
    feed = SlowOpenFeed([_relevant_reel("wedged")], clock, open_seconds=100.0)
    out = Session(store=store, router=router, feed=feed, soul=None,
                  campaign=_campaign(), pacer=_daytime_pacer(),
                  cfg=SessionConfig(), clock=clock).run()

    assert out["relevance_passes"] == 1      # it did pass relevance...
    assert out["matches"] == 0               # ...but its comments were never fetched
    assert out["halt_reason"] is None        # a breach never halts the walk
    flags = _discard_flags(store)
    assert len(flags) == 1
    assert flags[0]["severity"] == "soft"
    assert "wedged" in flags[0]["detail"]


def test_a_reel_lost_to_slow_browser_work_does_not_stop_the_walk():
    store, _path = _store()
    clock = _Clock()
    router = SlowGateRouter(clock, gate_seconds=0.0)

    class _OnlyFirstIsSlow(FakeFeed):
        def open_reel(self, reel):
            if reel.reel_id == "wedged":
                clock.advance(100.0)
            return True

    out = Session(store=store, router=router,
                  feed=_OnlyFirstIsSlow([_relevant_reel("wedged"), _relevant_reel("ok")]),
                  soul=None, campaign=_campaign(), pacer=_daytime_pacer(),
                  cfg=SessionConfig(), clock=clock).run()

    assert out["reels_seen"] == 2
    assert out["relevance_passes"] == 2
    assert out["matches"] == 1               # only the wedged reel lost its comments
    assert len(_discard_flags(store)) == 1


def test_relevant_reel_whose_permalink_never_opens_is_flagged_as_a_lost_lead():
    store, _path = _store()
    clock = _Clock()
    router = SlowGateRouter(clock, gate_seconds=0.0)
    feed = UnavailableReelFeed([_relevant_reel("gone")], failing_id="gone")
    out = Session(store=store, router=router, feed=feed, soul=None,
                  campaign=_campaign(), pacer=_daytime_pacer(),
                  cfg=SessionConfig(), clock=clock).run()

    assert out["relevance_passes"] == 1
    assert out["matches"] == 0
    # mark_seen already ran and seen_reels has no TTL, so this reel is gone for
    # good — the run must not be able to finish clean with no health flag.
    assert len(_discard_flags(store)) == 1
    assert any(f["kind"] == "reel_unavailable" for f in store.open_flags())


def test_no_flag_and_no_skip_when_the_reel_stays_under_the_budget():
    store, _path = _store()
    clock = _Clock()
    router = SlowGateRouter(clock, gate_seconds=1.0)
    out = Session(store=store, router=router, feed=FakeFeed([_relevant_reel("r1")]),
                  soul=None, campaign=_campaign(), pacer=_daytime_pacer(),
                  cfg=SessionConfig(), clock=clock).run()
    assert out["reels_seen"] == 1
    assert out["matches"] == 1
    assert out["halt_reason"] is None
    assert _discard_flags(store) == []


def test_the_gate_bumps_the_session_heartbeat_before_the_browser_block():
    """Watchdog regression introduced BY the re-anchor. ``last_activity_at`` is
    bumped only by ``_flush()`` -> ``store.update_counters``, which the feed loop
    reaches once per reel, at the very end. Re-anchoring serialized two expensive
    stages behind that single heartbeat: a reel used to cost dwell+gate, and now
    costs dwell+gate+open_reel+comments. ``session_watchdog`` halts any running
    session idle for over 180s, and the 2026-08-19 log already held a 186s gap on
    the 159s-vision reel — so without a heartbeat between the stages the fix would
    trade one silently lost reel for a whole session halted as stalled, on exactly
    the escalated reels most likely to be leads.

    Asserted by ORDER, not by timestamp: the injected clock does not move the
    wall-clock that last_activity_at records, so counting the flush is what
    discriminates. Fails with the _flush() call removed.
    """
    store, _path = _store()
    clock = _Clock()
    router = SlowGateRouter(clock, gate_seconds=100.0)
    session = Session(store=store, router=router,
                      feed=FakeFeed([_relevant_reel("slow-gate")]), soul=None,
                      campaign=_campaign(), pacer=_daytime_pacer(),
                      cfg=SessionConfig(), clock=clock)

    trace: list[str] = []
    real_update = store.update_counters
    real_open = session.feed.open_reel

    def recording_update(*a, **kw):
        trace.append("flush")
        return real_update(*a, **kw)

    def recording_open(reel):
        trace.append("open_reel")
        return real_open(reel)

    store.update_counters = recording_update
    session.feed.open_reel = recording_open
    out = session.run()

    assert out["matches"] == 1
    assert "open_reel" in trace, "the browser block was never reached"
    # A heartbeat must land between the gate finishing and the browser block
    # starting, i.e. before the FIRST open_reel of the walk.
    assert trace.index("flush") < trace.index("open_reel"), (
        "no heartbeat between the cascade gate and open_reel — a slow reel would "
        f"stall the watchdog past 180s (trace={trace})")
