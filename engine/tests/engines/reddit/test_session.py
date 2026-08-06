"""Reddit session loop — standalone, text-first, read-only.

Asserts the loop maps submissions→matches under platform='reddit', scores the WHOLE
nested comment tree (a deeply-nested reply can be a match), NEVER invokes vision,
streams run-activity events when a run_id is set, respects the lead target and
already-seen dedup, and reports a uniform summary (no engagement, no feed-health
flag, no halt).
"""
import os
import sqlite3
import tempfile

from aizu.core.config import campaign_from_brief
from aizu.core.router import Decision
from aizu.engines.reddit.feed import RedditComment, RedditFeed, RedditSubmission
from aizu.engines.reddit.session import run_session


class SpyRouter:
    def __init__(self):
        self.image_calls = []

    def classify_text(self, *, instruction, content, campaign_id, stage,
                      session_id=None, system=None):
        low = content.lower()
        if stage == "relevance":
            relevant = any(k in low for k in ("acme", "app", "saas", "demo"))
            return Decision(label="relevant" if relevant else "irrelevant",
                            score=0.9 if relevant else 0.1, confidence=0.96)
        is_lead = any(k in low for k in ("pricing", "+1", "price", "buy"))
        return Decision(label="yes" if is_lead else "no",
                        score=0.92 if is_lead else 0.1, confidence=0.96,
                        extracted={"phone": "+14155550142"} if is_lead else {})

    def classify_image(self, *, instruction, images_b64, campaign_id, stage,
                       session_id=None, system=None):
        self.image_calls.append(stage)
        return Decision(label="irrelevant", score=0.0, confidence=1.0)


class FakeRedditApi:
    """A Port-level fake (no network): canned submissions per subreddit and a
    canned (already-flattened, depth-bearing) comment tree per submission."""

    def __init__(self, by_subreddit=None, trees=None):
        self._by_subreddit = by_subreddit or {}
        self._trees = trees or {}

    def list_submissions(self, *, subreddit, query, limit):
        if query is not None:
            return []                                  # no search results in this fixture
        return list(self._by_subreddit.get(subreddit, []))[:limit]

    def comment_tree(self, submission_id, subreddit):
        return list(self._trees.get(submission_id, []))


def _store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from aizu.core.store import Store
    return Store(path), path


def _campaign():
    return campaign_from_brief("reddit-leadgen", {
        "platform": "reddit", "threshold": 0.7,
        "relevance_def": "saas product submissions",
        "match_def": "a commenter asking to buy / price / contact",
        "extract_def": "- phone",
    })


def _feed(trees=None):
    return RedditFeed(client=FakeRedditApi(
        by_subreddit={"saas": [
            RedditSubmission("s1", title="Acme app demo", selftext="Free trial available",
                             author="dev"),
            RedditSubmission("s2", title="funny cats", selftext="lol", author="cats"),
        ]},
        trees=trees or {"s1": [
            RedditComment("c1", "Pricing? +1 415 555 0142", "aziz",
                          depth=0, created_utc=100.0),
            RedditComment("c2", "🔥🔥", "bot", depth=0, created_utc=101.0),
            RedditComment("c3", "price? buy +1 2", "deepuser",
                          depth=3, created_utc=102.0),   # deeply-nested reply
        ]},
    ), subreddits=["saas"])


def _run(store, run_id=None, lead_target=None, feed=None):
    return run_session(campaign=_campaign(), store=store, router=SpyRouter(),
                       feed=feed or _feed(), soul=None, pacer=None, run_id=run_id,
                       lead_target=lead_target)


def test_session_maps_relevant_submission_to_match():
    store, _ = _store()
    summary = _run(store)
    assert summary["reels_seen"] == 2          # s1 relevant, s2 not
    assert summary["relevance_passes"] == 1
    assert summary["matches"] == 2             # c1 (top-level) + c3 (deep reply); c2 noise
    rows = store.matches("reddit-leadgen")
    assert rows and all(r["platform"] == "reddit" for r in rows)
    ids = {r["comment_id"] for r in rows}
    assert ids == {"s1/c1", "s1/c3"}
    assert any(r["comment_id"] == "s1/c1" and r["extracted"].get("phone") == "+14155550142"
               for r in rows)


def test_deeply_nested_reply_is_matched():
    store, _ = _store()
    _run(store)
    rows = store.matches("reddit-leadgen")
    # The depth=3 comment (a deep reply, not a top-level one) is in scope and matched.
    assert "s1/c3" in {r["comment_id"] for r in rows}


def test_never_calls_vision():
    store, _ = _store()
    spy = SpyRouter()
    run_session(campaign=_campaign(), store=store, router=spy, feed=_feed(),
                soul=None, pacer=None)
    assert spy.image_calls == []


def test_summary_shape_is_readonly_and_unhalted():
    store, _ = _store()
    s = _run(store)
    assert s["feed_health_flag"] is False
    assert s["likes"] == 0 and s["follows"] == 0
    assert s["halt_reason"] is None
    assert set(s) >= {"session_id", "reels_seen", "relevance_passes", "matches",
                      "escalations", "already_seen_skips", "spend_usd",
                      "feed_health_flag", "likes", "follows", "halt_reason"}


def test_lead_target_stops_early():
    store, _ = _store()
    # Two relevant submissions, each with exactly one matching comment; target 1
    # stops after the first submission (before walking the second).
    feed = RedditFeed(client=FakeRedditApi(
        by_subreddit={"saas": [
            RedditSubmission("s1", title="acme app", author="x"),
            RedditSubmission("s3", title="saas demo", author="y")]},
        trees={"s1": [RedditComment("c1", "price +1", "a", 0, 100.0)],
               "s3": [RedditComment("c3", "buy +1", "b", 0, 100.0)]}),
        subreddits=["saas"])
    summary = _run(store, lead_target=1, feed=feed)
    assert summary["matches"] == 1
    assert summary["reels_seen"] == 1          # stopped before the 2nd submission


def test_already_seen_dedup():
    store, _ = _store()
    _run(store)
    second = _run(store)
    assert second["already_seen_skips"] == 2   # both submissions already seen
    assert second["relevance_passes"] == 0


def test_run_events_streamed_when_run_id_set():
    store, path = _store()
    _run(store, run_id="run-abc")
    con = sqlite3.connect(path)
    n = con.execute("SELECT count(*) FROM run_events WHERE run_id=?", ("run-abc",)).fetchone()[0]
    con.close()
    assert n >= 2                              # at least lifecycle start + completed
