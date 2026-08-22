"""The session heartbeat must advance DURING a post, not only at the end of it.

2026-08-20, first fleet run of the Tashkent renovation campaign: job-2099fb29e88b
failed after 5 attempts, every attempt killed by the bridge-side SessionWatchdog
with halt_reason "stalled: no activity for over 180s". Nothing was actually wedged
— ``sessions.last_activity_at`` was simply only bumped by ``_flush()`` at the end
of each post (plus the single flush added between the gate and the browser block),
while ONE ``cascade.gate_post`` can chain three model calls and one
``_process_comments`` can chain one per comment. A legitimately slow stage was
indistinguishable from a wedge.

Every assertion below is ORDINAL (order/count of store writes), never temporal:
the injected clock does not move the real ``time.time()`` that ``last_activity_at``
records, so a timestamp assertion passes vacuously — that trap bit this repo on
2026-08-19.
"""
from aizu.core.config import campaign_from_brief
from aizu.core.feed import Comment, Reel
from aizu.core.pacing import Pacer, PacingConfig
from aizu.engines.linkedin.session import LinkedInSession, SessionConfig
from tests.engines.heartbeat_harness import (
    CAPTURE, FETCH, FLUSH, OPEN_REEL, TOUCH, TimelineFeed, TimelineRouter, indices,
    assert_bump_between_consecutive, make_store, model_indices)


def _campaign():
    return campaign_from_brief("li-leadgen", {
        "platform": "linkedin", "threshold": 0.7,
        "relevance_def": "acme saas app posts",
        "match_def": "a commenter asking to buy / price / contact",
        "extract_def": "- phone",
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
    LinkedInSession(store=store, router=TimelineRouter(timeline), feed=feed, soul=None,
                    campaign=_campaign(), pacer=_pacer(),
                    cfg=SessionConfig()).run()
    return timeline


def test_the_heartbeat_advances_between_the_gate_model_calls_not_only_after_the_gate():
    timeline = _run()
    gate_calls = model_indices(timeline, "relevance")
    # copy -> vision -> escalation: the full chain the live stall walked.
    assert len(gate_calls) == 3, timeline
    assert_bump_between_consecutive(timeline, gate_calls, "gate model calls")


def test_the_lazy_frame_capture_inside_the_gate_also_bumps_the_heartbeat():
    """The lazy frame capture is not a model call, so nothing else on the
    heartbeat path can see it — a slow screenshot between two verdicts would
    otherwise be invisible to the watchdog."""
    timeline = _run()
    capture = indices(timeline, CAPTURE)
    assert capture, timeline
    assert TOUCH in timeline[capture[0] + 1:capture[0] + 3], timeline


def test_a_post_arriving_from_the_feed_walk_bumps_before_the_gate_starts():
    """``feed.walk()`` scrolls and re-fetches INSIDE the generator, between two
    iterations, and bumped nothing — so the silence used to run from the previous
    post's closing flush all the way to this post's first verdict."""
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
    # ...and it is the fine-grained path doing it, not a per-post counters write
    # that happens to land there.
    span = timeline[match_calls[0]:match_calls[-1]]
    assert TOUCH in span and FLUSH not in span, timeline


def test_the_comment_fetch_and_the_permalink_navigation_each_bump_the_heartbeat():
    """Both are unbounded browser round-trips that sat between two bumps: the
    per-post deadline is only READ after open_reel returns, so a slow navigation
    could not interrupt itself."""
    timeline = _run()
    for step in (OPEN_REEL, FETCH):
        at = indices(timeline, step)[0]
        assert timeline[at + 1] == TOUCH, (step, timeline)


def test_a_comment_whose_scoring_raises_still_leaves_a_heartbeat_behind():
    """The auto-skip path burned the same wall-clock as a successful score — if it
    bumped nothing, a post full of malformed free-tier verdicts (the exact live
    failure mode) would be silent for its whole duration."""
    timeline = _run(comment_texts=("boom", "pricing?"))
    match_calls = model_indices(timeline, "match")
    assert len(match_calls) == 2, timeline
    assert_bump_between_consecutive(timeline, match_calls, "comment scorings")
