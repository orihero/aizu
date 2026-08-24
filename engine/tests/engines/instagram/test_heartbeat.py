"""The session heartbeat must advance DURING a reel, not only at the end of it.

2026-08-20, first fleet run of the Tashkent renovation campaign: job-2099fb29e88b
failed after 5 attempts, every attempt killed by the bridge-side SessionWatchdog
with halt_reason "stalled: no activity for over 180s". Nothing was actually wedged
— ``sessions.last_activity_at`` was simply only bumped by ``_flush()`` at the end
of each reel (plus the single flush added between the gate and the browser block),
while ONE ``cascade.gate_reel`` can chain up to five model calls and one
``_process_comments`` can chain one per comment. A legitimately slow stage was
indistinguishable from a wedge.

Every assertion below is ORDINAL (order/count of store writes), never temporal:
the injected clock does not move the real ``time.time()`` that ``last_activity_at``
records, so a timestamp assertion passes vacuously — that trap bit this repo on
2026-08-19.
"""
import time

from aizu.core.config import campaign_from_brief
from aizu.core.feed import Comment, Reel
from aizu.core.pacing import Pacer, PacingConfig
from aizu.core.store import SessionCounters
from aizu.engines.instagram.session import Session, SessionConfig
from tests.engines.heartbeat_harness import (
    CAPTURE, FETCH, FLUSH, OPEN_REEL, TOUCH, TimelineFeed, TimelineRouter, indices,
    assert_bump_between_consecutive, make_store, model_indices)


LIKE = "like_reel"


class LikingFeed(TimelineFeed):
    """Engagement is opt-in per campaign and off in the other fixtures; this feed
    reports a successful like so the action path (and its bump) is exercised."""

    def like_reel(self, reel):
        self.timeline.append(LIKE)
        return True


def _campaign(**extra):
    return campaign_from_brief("ig-leadgen", {
        "platform": "instagram", "threshold": 0.7,
        "relevance_def": "acme saas app posts",
        "match_def": "a commenter asking to buy / price / contact",
        "extract_def": "- phone",
        **extra,
    })


def _pacer():
    return Pacer(cfg=PacingConfig(enforce_daytime=False), sleep=lambda _t: None)


def _reel(reel_id="r1", comment_texts=("pricing?", "how much?", "dm me")):
    return Reel(reel_id, caption="acme saas app dashboard rollout",
                comments=[Comment(f"c{i}", f"u{i}", t)
                          for i, t in enumerate(comment_texts)])


def _run(comment_texts=("pricing?", "how much?", "dm me")) -> list[str]:
    timeline: list[str] = []
    store, _path = make_store(timeline)
    feed = TimelineFeed([_reel(comment_texts=comment_texts)], timeline)
    Session(store=store, router=TimelineRouter(timeline), feed=feed, soul=None,
            campaign=_campaign(), pacer=_pacer(), cfg=SessionConfig()).run()
    return timeline


def test_the_heartbeat_advances_between_the_gate_model_calls_not_only_after_the_gate():
    timeline = _run()
    gate_calls = model_indices(timeline, "relevance")
    # copy -> vision -> escalation: the full chain the live stall walked.
    assert len(gate_calls) == 3, timeline
    assert_bump_between_consecutive(timeline, gate_calls, "gate model calls")


def test_the_lazy_frame_capture_inside_the_gate_also_bumps_the_heartbeat():
    """The capture/STT/ffmpeg tiers are not model calls, so nothing else on the
    heartbeat path can see them — a slow frame grab or ffmpeg download between two
    verdicts would otherwise be invisible to the watchdog."""
    timeline = _run()
    capture = indices(timeline, CAPTURE)
    assert capture, timeline
    assert TOUCH in timeline[capture[0] + 1:capture[0] + 3], timeline


def test_a_reel_arriving_from_the_feed_walk_bumps_before_the_gate_starts():
    """``feed.walk()`` scrolls and re-fetches INSIDE the generator, between two
    iterations, and bumped nothing — so the silence used to run from the previous
    reel's closing flush all the way to this reel's first verdict."""
    timeline = _run()
    first_model = model_indices(timeline, "relevance")[0]
    assert TOUCH in timeline[:first_model], timeline


def test_the_flush_between_the_gate_and_the_browser_block_is_still_there():
    """Regression guard for the 2026-08-19 fix: the fine-grained touches COMPOSE
    with that flush, they do not replace it."""
    timeline = _run()
    last_gate_call = model_indices(timeline, "relevance")[-1]
    open_at = indices(timeline, OPEN_REEL)[0]
    assert FLUSH in timeline[last_gate_call:open_at], timeline


def test_the_heartbeat_advances_between_two_comment_scorings():
    timeline = _run()
    match_calls = model_indices(timeline, "match")
    assert len(match_calls) == 3, timeline
    assert_bump_between_consecutive(timeline, match_calls, "comment scorings")
    # ...and it is the fine-grained path doing it, not a per-reel counters write
    # that happens to land there.
    span = timeline[match_calls[0]:match_calls[-1]]
    assert TOUCH in span and FLUSH not in span, timeline


def test_the_comment_fetch_and_the_permalink_navigation_each_bump_the_heartbeat():
    """Both are unbounded browser round-trips that sat between two bumps: the
    per-reel deadline is only READ after open_reel returns, so a slow navigation
    could not interrupt itself."""
    timeline = _run()
    for step in (OPEN_REEL, FETCH):
        at = indices(timeline, step)[0]
        assert timeline[at + 1] == TOUCH, (step, timeline)


def test_an_engagement_action_bumps_the_heartbeat_before_the_comment_fetch():
    """open_reel -> like -> action-block probe -> fetch_comments was one unbroken
    silence. The like ran and was logged, so the bump is progress, not a ticker."""
    timeline: list[str] = []
    store, _path = make_store(timeline)
    feed = LikingFeed([_reel()], timeline)
    Session(store=store, router=TimelineRouter(timeline), feed=feed, soul=None,
            campaign=_campaign(enable_actions=True), pacer=_pacer(),
            cfg=SessionConfig()).run()
    like_at = indices(timeline, LIKE)[0]
    fetch_at = indices(timeline, FETCH)[0]
    assert like_at < fetch_at, timeline
    assert TOUCH in timeline[like_at:fetch_at], timeline


def test_a_comment_whose_scoring_raises_still_leaves_a_heartbeat_behind():
    """The auto-skip path burned the same wall-clock as a successful score — if it
    bumped nothing, a reel full of malformed free-tier verdicts (the exact live
    failure mode) would be silent for its whole duration."""
    timeline = _run(comment_texts=("boom", "pricing?"))
    match_calls = model_indices(timeline, "match")
    assert len(match_calls) == 2, timeline
    assert_bump_between_consecutive(timeline, match_calls, "comment scorings")


def test_touching_a_session_writes_the_activity_stamp_without_rewriting_counters():
    """``touch_session`` is the cheap path requirement: one single-row UPDATE, no
    aggregate spend SELECT and no counter rewrite, so per-verdict granularity is
    affordable. Real time here, not the injected clock — this is the one place the
    stamp itself is the subject."""
    timeline: list[str] = []
    store, _path = make_store(timeline)
    store.start_session("s1", "ig-leadgen", platform="instagram")
    store.update_counters("s1", SessionCounters(reels_seen=7, matches=3))
    before = store.get_session("s1")
    time.sleep(0.01)

    store.touch_session("s1")

    after = store.get_session("s1")
    assert after["last_activity_at"] > before["last_activity_at"]
    assert after["reels_seen"] == 7 and after["matches"] == 3


def test_the_heartbeat_facade_does_not_swallow_configuration_writes():
    """A read-only proxy in front of the router is a trap.

    `setattr(router, "x", v)` through a facade that only defines `__getattr__`
    binds `x` on the FACADE; every router method still reads `self.x` on the
    WRAPPED object and sees the old value. The write looks successful — the caller
    reads it straight back — and never reaches the code that consumes it. Concrete
    case this was found on: the Cascade sets `default_threshold` so
    `_classify_text_with_comparison` can decide agreement, and with a swallowing
    proxy `model_comparison_log.agreed` would have kept writing NULL, which is the
    very bug the assignment exists to fix.
    """
    from aizu.engines.instagram.session import _HeartbeatRouter

    class _Router:
        def __init__(self):
            self.default_threshold = None

    real = _Router()
    facade = _HeartbeatRouter(real, lambda: None)

    facade.default_threshold = 0.7
    assert real.default_threshold == 0.7, "the write never reached the real router"
    assert facade.default_threshold == 0.7

    # The facade's own state must NOT leak onto the wrapped router.
    assert not hasattr(real, "_on_verdict")
