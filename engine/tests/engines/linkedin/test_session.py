"""LinkedIn session loop — CDP-shaped, copy-first, read-only.

Runs against the in-memory FakeFeed (no browser): maps relevant posts → matches
under platform='linkedin', respects the lead target + already-seen dedup, streams
run-activity events, and reports a uniform read-only summary.
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
    PROGRESS_EVENT_INTERVAL_SEC, LinkedInSession, run_session)


class SpyRouter:
    def __init__(self):
        self.image_calls = []

    def classify_text(self, *, instruction, content, campaign_id, stage,
                      session_id=None, system=None):
        low = content.lower()
        if stage == "relevance":
            rel = any(k in low for k in ("hiring", "cfo", "vacancy"))
            return Decision(label="relevant" if rel else "irrelevant",
                            score=0.9 if rel else 0.1, confidence=0.97)
        lead = any(k in low for k in ("rate", "price", "+1", "interested"))
        return Decision(label="yes" if lead else "no",
                        score=0.93 if lead else 0.1, confidence=0.97,
                        extracted={"phone": "+14155550142"} if lead else {})

    def classify_image(self, *, instruction, images_b64, campaign_id, stage,
                       session_id=None, system=None):
        self.image_calls.append(stage)
        return Decision(label="irrelevant", score=0.0, confidence=1.0)


def _store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path), path


def _campaign():
    return campaign_from_brief("li-leadgen", {
        "platform": "linkedin", "threshold": 0.7,
        "relevance_def": "posts hiring or seeking services",
        "match_def": "a commenter asking price / showing intent",
        "extract_def": "- phone"})


def _pacer():
    # No real sleeping, always daytime (deterministic in CI).
    return Pacer(cfg=PacingConfig(enforce_daytime=False), sleep=lambda _t: None)


def _feed():
    return FakeFeed([
        Reel(reel_id="urn:li:activity:111", caption="We are hiring a CFO",
             author="Acme", comments=[
                 Comment(comment_id="c1", username="Aziz", text="Interested — what's the rate?"),
                 Comment(comment_id="c2", username="Bot", text="Congrats! 🎉")]),
        Reel(reel_id="urn:li:activity:222", caption="cat pictures",
             author="Pets", comments=[
                 Comment(comment_id="c3", username="X", text="price? +1")]),
    ])


def _run(store, run_id=None, lead_target=None, feed=None, router=None):
    return run_session(campaign=_campaign(), store=store,
                       router=router or SpyRouter(), feed=feed or _feed(),
                       soul=None, pacer=_pacer(), run_id=run_id,
                       lead_target=lead_target)


def test_relevant_post_yields_match():
    store, _ = _store()
    summary = _run(store)
    assert summary["reels_seen"] == 2          # 111 relevant, 222 not
    assert summary["relevance_passes"] == 1
    assert summary["matches"] == 1             # c1 only (c2 is noise)
    rows = store.matches("li-leadgen")
    assert rows and all(r["platform"] == "linkedin" for r in rows)
    assert rows[0]["comment_id"] == "c1"
    assert rows[0]["extracted"].get("phone") == "+14155550142"


def test_summary_shape_is_readonly():
    store, _ = _store()
    s = _run(store)
    assert s["likes"] == 0 and s["follows"] == 0
    assert s["halt_reason"] is None
    assert set(s) >= {"session_id", "reels_seen", "relevance_passes", "matches",
                      "escalations", "already_seen_skips", "spend_usd",
                      "feed_health_flag", "likes", "follows", "halt_reason"}


def test_never_calls_vision_when_copy_present():
    store, _ = _store()
    spy = SpyRouter()
    _run(store, router=spy)
    assert spy.image_calls == []               # both posts have copy → text only


def test_lead_target_stops_early():
    store, _ = _store()
    feed = FakeFeed([
        Reel(reel_id="urn:li:activity:111", caption="hiring CFO", author="A",
             comments=[Comment(comment_id="c1", username="a", text="rate? +1")]),
        Reel(reel_id="urn:li:activity:222", caption="hiring CFO", author="B",
             comments=[Comment(comment_id="c2", username="b", text="price +1")]),
    ])
    summary = _run(store, lead_target=1, feed=feed)
    assert summary["matches"] == 1
    assert summary["reels_seen"] == 1          # stopped before the 2nd post


def test_already_seen_dedup():
    store, _ = _store()
    _run(store)
    second = _run(store)
    assert second["already_seen_skips"] == 2
    assert second["relevance_passes"] == 0


def test_run_events_streamed_when_run_id_set():
    store, path = _store()
    _run(store, run_id="run-li")
    con = sqlite3.connect(path)
    n = con.execute("SELECT count(*) FROM run_events WHERE run_id=?", ("run-li",)).fetchone()[0]
    con.close()
    assert n >= 2                              # at least lifecycle start + completed


# ---- FIX B: throttled scan-progress heartbeat -----------------------------


class _Clock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class _ClockAdvancingFeed(FakeFeed):
    """Bumps a shared clock by `step` seconds BEFORE yielding each post."""

    def __init__(self, reels, clock: _Clock, step: float):
        super().__init__(reels)
        self._clock = clock
        self._step = step

    def walk(self):
        for reel in self._reels:
            self._clock.now += self._step
            yield reel


def _irrelevant_posts(n: int):
    # "cat pictures" never matches the hiring/cfo/vacancy relevance → common path.
    return [Reel(reel_id=f"urn:li:activity:{i}", caption="cat pictures",
                 author="Pets", comments=[]) for i in range(n)]


def _progress_events(store, run_id):
    return [e for e in store.fetch_run_events(run_id)
            if e["phase"] == "feed_walk" and "Scanning" in e["message"]]


def test_scanning_irrelevant_posts_emits_throttled_progress_event():
    store, _ = _store()
    clock = _Clock(0.0)
    step = PROGRESS_EVENT_INTERVAL_SEC + 5.0
    feed = _ClockAdvancingFeed(_irrelevant_posts(4), clock, step)
    out = LinkedInSession(store=store, router=SpyRouter(), feed=feed, soul=None,
                          campaign=_campaign(), pacer=_pacer(), run_id="run-prog",
                          clock=clock).run()
    assert out["matches"] == 0
    assert out["reels_seen"] == 4
    events = _progress_events(store, "run-prog")
    assert events, "expected at least one 'Scanning' progress event on the common path"
    assert "posts seen" in events[0]["message"]


def test_progress_event_is_throttled_under_interval():
    store, _ = _store()
    clock = _Clock(0.0)
    step = PROGRESS_EVENT_INTERVAL_SEC / 10.0
    feed = _ClockAdvancingFeed(_irrelevant_posts(5), clock, step)
    out = LinkedInSession(store=store, router=SpyRouter(), feed=feed, soul=None,
                          campaign=_campaign(), pacer=_pacer(), run_id="run-throttle",
                          clock=clock).run()
    assert out["reels_seen"] == 5
    assert _progress_events(store, "run-throttle") == []
