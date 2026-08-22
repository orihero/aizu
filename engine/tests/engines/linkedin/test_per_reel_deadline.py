"""FIX P0 (LinkedIn): the per-post wall-clock budget bounds the BROWSER block, not
the cascade.

``reel_start`` used to be anchored BEFORE ``cascade.gate_post()``, so a post whose
CLASSIFICATION overran ``per_reel_seconds`` was marked seen and then skipped before
``open_reel`` ever ran. ``store.is_seen`` is a bare existence check with no TTL and
nothing in ``aizu/`` deletes from ``seen_reels``, so that post was blacklisted for
the campaign forever. Proven live on the shared Instagram loop (2026-08-19, reel
DFdnoSsgWBk: relevant, score 0.85, confidence 0.90, discarded in the same log
second) — this engine is a copy of it.

These tests pin the corrected contract: classification time is not charged to the
browser budget, ``relevance_passes`` agrees with ``seen_reels.relevant``, and any
relevant post dropped after mark_seen leaves a health flag behind.
"""
import os
import sqlite3
import tempfile

from aizu.core.config import campaign_from_brief
from aizu.core.feed import Comment, FakeFeed, Reel
from aizu.core.pacing import Pacer, PacingConfig
from aizu.core.router import Decision
from aizu.core.store import Store
from aizu.engines.linkedin.session import (
    RELEVANT_REEL_DISCARDED_FLAG, LinkedInSession, SessionConfig)


class _Clock:
    """Injectable monotonic clock the test (and the fakes below) advance by hand."""

    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SlowGateRouter:
    """Router whose RELEVANCE call burns wall-clock — the escalation path, which is
    slow precisely on the borderline-but-genuine content most likely to be a lead."""

    def __init__(self, clock: _Clock, gate_seconds: float = 0.0):
        self._clock = clock
        self._gate_seconds = gate_seconds

    def classify_text(self, *, instruction, content, campaign_id, stage,
                      session_id=None, system=None):
        low = content.lower()
        if stage == "relevance":
            self._clock.advance(self._gate_seconds)
            rel = "hiring" in low
            return Decision(label="relevant" if rel else "irrelevant",
                            score=0.9 if rel else 0.1, confidence=0.97)
        lead = "rate" in low
        return Decision(label="yes" if lead else "no",
                        score=0.93 if lead else 0.1, confidence=0.97,
                        extracted={"phone": "+14155550142"} if lead else {})

    def classify_image(self, *, instruction, images_b64, campaign_id, stage,
                       session_id=None, system=None):
        return Decision(label="irrelevant", score=0.0, confidence=1.0)


class SlowOpenFeed(FakeFeed):
    """open_reel burns wall-clock — the real browser work the budget must bound."""

    def __init__(self, reels, clock: _Clock, open_seconds: float):
        super().__init__(reels)
        self._clock = clock
        self._open_seconds = open_seconds

    def open_reel(self, reel):
        self._clock.advance(self._open_seconds)
        return True


class UnavailablePostFeed(FakeFeed):
    """open_reel fails for the named post — a deleted/unavailable permalink."""

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
    return campaign_from_brief("li-leadgen", {
        "platform": "linkedin", "threshold": 0.7,
        "relevance_def": "hiring posts",
        "match_def": "a commenter asking rate",
        "extract_def": "- phone"})


def _pacer():
    return Pacer(cfg=PacingConfig(enforce_daytime=False), sleep=lambda _t: None)


def _seen_relevant_ids(path: str) -> set[str]:
    con = sqlite3.connect(path)
    rows = con.execute("SELECT reel_id FROM seen_reels WHERE relevant = 1").fetchall()
    con.close()
    return {r[0] for r in rows}


def _discard_flags(store: Store) -> list[dict]:
    return [f for f in store.open_flags()
            if f["kind"] == RELEVANT_REEL_DISCARDED_FLAG]


def _relevant_post(post_id: str) -> Reel:
    return Reel(post_id, caption="hiring", author="Acme",
                comments=[Comment(f"c-{post_id}", f"u-{post_id}", "what's the rate?")])


def test_post_whose_classification_overruns_the_budget_still_reaches_comment_scoring():
    store, _path = _store()
    clock = _Clock()
    # 100s > the 90s default budget, spent entirely inside the relevance gate.
    out = LinkedInSession(store=store, router=SlowGateRouter(clock, gate_seconds=100.0),
                          feed=FakeFeed([_relevant_post("slow-gate")]), soul=None,
                          campaign=_campaign(), pacer=_pacer(), cfg=SessionConfig(),
                          clock=clock).run()

    # Before the fix the gate's own 100s tripped the pre-block deadline check and
    # the post was dropped between mark_seen and open_reel: matches==0.
    assert out["matches"] == 1
    assert out["relevance_passes"] == 1
    assert out["halt_reason"] is None
    assert _discard_flags(store) == []


def test_every_relevant_post_is_scored_even_when_all_classifications_overrun():
    store, _path = _store()
    clock = _Clock()
    posts = [_relevant_post("first"), _relevant_post("second")]
    out = LinkedInSession(store=store, router=SlowGateRouter(clock, gate_seconds=100.0),
                          feed=FakeFeed(posts), soul=None, campaign=_campaign(),
                          pacer=_pacer(), cfg=SessionConfig(), clock=clock).run()

    assert out["reels_seen"] == 2
    assert out["relevance_passes"] == 2
    assert out["matches"] == 2
    assert out["halt_reason"] is None


def test_relevance_passes_never_disagrees_with_the_persisted_seen_reels_verdict():
    store, path = _store()
    clock = _Clock()
    posts = [_relevant_post("hit"),
             Reel("miss", caption="cat pictures", author="Pets", comments=[])]
    out = LinkedInSession(store=store, router=SlowGateRouter(clock, gate_seconds=100.0),
                          feed=FakeFeed(posts), soul=None, campaign=_campaign(),
                          pacer=_pacer(), cfg=SessionConfig(), clock=clock).run()

    assert _seen_relevant_ids(path) == {"hit"}
    assert out["relevance_passes"] == len(_seen_relevant_ids(path)) == 1


def test_browser_work_that_burns_the_budget_skips_comments_and_flags_the_lost_post():
    store, _path = _store()
    clock = _Clock()
    feed = SlowOpenFeed([_relevant_post("wedged")], clock, open_seconds=100.0)
    out = LinkedInSession(store=store, router=SlowGateRouter(clock, gate_seconds=0.0),
                          feed=feed, soul=None, campaign=_campaign(), pacer=_pacer(),
                          cfg=SessionConfig(), clock=clock).run()

    assert out["relevance_passes"] == 1
    assert out["matches"] == 0
    assert out["halt_reason"] is None
    flags = _discard_flags(store)
    assert len(flags) == 1
    assert flags[0]["severity"] == "soft"
    assert "wedged" in flags[0]["detail"]


def test_a_post_lost_to_slow_browser_work_does_not_stop_the_walk():
    store, _path = _store()
    clock = _Clock()

    class _OnlyFirstIsSlow(FakeFeed):
        def open_reel(self, reel):
            if reel.reel_id == "wedged":
                clock.advance(100.0)
            return True

    out = LinkedInSession(store=store, router=SlowGateRouter(clock, gate_seconds=0.0),
                          feed=_OnlyFirstIsSlow([_relevant_post("wedged"),
                                                 _relevant_post("ok")]),
                          soul=None, campaign=_campaign(), pacer=_pacer(),
                          cfg=SessionConfig(), clock=clock).run()

    assert out["reels_seen"] == 2
    assert out["relevance_passes"] == 2
    assert out["matches"] == 1
    assert len(_discard_flags(store)) == 1


def test_relevant_post_whose_permalink_never_opens_is_flagged_as_a_lost_lead():
    store, _path = _store()
    clock = _Clock()
    feed = UnavailablePostFeed([_relevant_post("gone")], failing_id="gone")
    out = LinkedInSession(store=store, router=SlowGateRouter(clock, gate_seconds=0.0),
                          feed=feed, soul=None, campaign=_campaign(), pacer=_pacer(),
                          cfg=SessionConfig(), clock=clock).run()

    assert out["relevance_passes"] == 1
    assert out["matches"] == 0
    assert len(_discard_flags(store)) == 1
    assert any(f["kind"] == "post_unavailable" for f in store.open_flags())


def test_no_flag_and_no_skip_when_the_post_stays_under_the_budget():
    store, _path = _store()
    clock = _Clock()
    out = LinkedInSession(store=store, router=SlowGateRouter(clock, gate_seconds=1.0),
                          feed=FakeFeed([_relevant_post("p1")]), soul=None,
                          campaign=_campaign(), pacer=_pacer(), cfg=SessionConfig(),
                          clock=clock).run()
    assert out["reels_seen"] == 1
    assert out["matches"] == 1
    assert out["halt_reason"] is None
    assert _discard_flags(store) == []
