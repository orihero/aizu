"""YouTube session loop — standalone, text-only, read-only.

Asserts the loop maps videos→matches under platform='youtube', NEVER invokes
vision, streams run-activity events when a run_id is set, respects the lead
target and already-seen dedup, and reports a uniform summary (no engagement, no
feed-health flag, no halt).
"""
import os
import sqlite3
import tempfile

from aizu.core.config import campaign_from_brief
from aizu.core.router import Decision
from aizu.engines.youtube.feed import YouTubeFeed, YtComment, YtVideo
from aizu.engines.youtube.session import run_session


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


class FakeYouTubeApi:
    def __init__(self, by_channel=None, comments=None):
        self._by_channel = by_channel or {}
        self._comments = comments or {}

    def search_videos(self, *, channel_id, query, limit):
        if channel_id is not None:
            return list(self._by_channel.get(channel_id, []))[:limit]
        return []

    def list_comments(self, video_id, page_token):
        for token, items, nxt in self._comments.get(video_id, []):
            if token == page_token:
                return items, nxt
        return [], None


def _store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from aizu.core.store import Store
    return Store(path), path


def _campaign():
    return campaign_from_brief("yt-leadgen", {
        "platform": "youtube", "threshold": 0.7,
        "relevance_def": "saas product videos",
        "match_def": "a commenter asking to buy / price / contact",
        "extract_def": "- phone",
    })


def _feed(comments=None):
    return YouTubeFeed(client=FakeYouTubeApi(
        by_channel={"UC_dev": [
            YtVideo("v1", title="Acme app demo", description="Acme app free trial",
                    channel_title="Acme Inc."),
            YtVideo("v2", title="funny cats", description="lol", channel_title="cats"),
        ]},
        comments=comments or {"v1": [
            (None, [YtComment("c1", "How much is the Pro plan? pricing? +1 415 555 0142", "aziz"),
                    YtComment("c2", "🔥🔥", "bot")], None)]},
    ), channels=["UC_dev"])


def _run(store, run_id=None, lead_target=None):
    return run_session(campaign=_campaign(), store=store, router=SpyRouter(),
                       feed=_feed(), soul=None, pacer=None, run_id=run_id,
                       lead_target=lead_target)


def test_session_maps_relevant_video_to_match():
    store, _ = _store()
    summary = _run(store)
    assert summary["reels_seen"] == 2          # v1 relevant, v2 not
    assert summary["relevance_passes"] == 1
    assert summary["matches"] == 1             # only the intent+phone comment
    rows = store.matches("yt-leadgen")
    assert rows and all(r["platform"] == "youtube" for r in rows)
    assert rows[0]["comment_id"] == "v1/c1"
    assert rows[0]["extracted"].get("phone") == "+14155550142"


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
    # Two relevant videos each with a matching comment; target 1 stops after the first.
    comments = {"v1": [(None, [YtComment("c1", "price? +1 1", "a")], None)],
                "v3": [(None, [YtComment("c3", "buy +1 2", "b")], None)]}
    feed = YouTubeFeed(client=FakeYouTubeApi(
        by_channel={"UC_dev": [
            YtVideo("v1", title="acme", channel_title="x"),
            YtVideo("v3", title="app", channel_title="y")]},
        comments=comments), channels=["UC_dev"])
    summary = run_session(campaign=_campaign(), store=store, router=SpyRouter(),
                          feed=feed, soul=None, pacer=None, lead_target=1)
    assert summary["matches"] == 1
    assert summary["reels_seen"] == 1          # stopped before walking the 2nd video


def test_already_seen_dedup():
    store, _ = _store()
    _run(store)
    second = _run(store)
    assert second["already_seen_skips"] == 2   # both videos already seen
    assert second["relevance_passes"] == 0


def test_run_events_streamed_when_run_id_set():
    store, path = _store()
    _run(store, run_id="run-abc")
    con = sqlite3.connect(path)
    n = con.execute("SELECT count(*) FROM run_events WHERE run_id=?", ("run-abc",)).fetchone()[0]
    con.close()
    assert n >= 2                              # at least lifecycle start + completed


# ----- crash guard (session_crash_guard) -----------------------------------------

class CrashingYtFeed:
    """A feed whose walk() raises a generic exception mid-run (NOT YouTubeApiError),
    to prove the crash guard terminalizes the session row."""

    def walk(self):
        raise RuntimeError("boom: network closed")

    def fetch_comments(self, reel_id, cursor):  # pragma: no cover - never reached
        return [], cursor


def test_crash_guard_terminalizes_session_on_unexpected_error():
    store, path = _store()
    try:
        run_session(campaign=_campaign(), store=store, router=SpyRouter(),
                    feed=CrashingYtFeed(), soul=None, pacer=None)
        assert False, "expected the RuntimeError to propagate"
    except RuntimeError as e:
        assert "boom" in str(e)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT status, halt_reason, ended_at FROM sessions").fetchall()]
    con.close()
    assert len(rows) == 1
    assert rows[0]["status"] == "halted"
    assert rows[0]["ended_at"] is not None
    assert rows[0]["halt_reason"].startswith("crashed:")
    assert "RuntimeError" in rows[0]["halt_reason"]
