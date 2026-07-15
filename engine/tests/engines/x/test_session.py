"""X session loop — text-first, read-only, two match surfaces + read-budget.

Runs against the in-memory FakeFeed: maps relevant posts → matches under
platform='x', tags each match's surface (reply|quote) in extracted, respects the
lead target + already-seen dedup, soft-stops on the read-budget cap, and reports a
uniform read-only summary.
"""
import os
import tempfile

from reelradar.core.config import campaign_from_brief
from reelradar.core.feed import Comment, FakeFeed, Reel
from reelradar.core.pacing import Pacer, PacingConfig
from reelradar.core.router import Decision
from reelradar.core.store import Store
from reelradar.engines.x.session import (
    PROGRESS_EVENT_INTERVAL_SEC, SessionConfig, XSession)


class SpyRouter:
    def classify_text(self, *, instruction, content, campaign_id, stage,
                      session_id=None, system=None):
        low = content.lower()
        if stage == "relevance":
            rel = any(k in low for k in ("hiring", "cfo", "acme"))
            return Decision(label="relevant" if rel else "irrelevant",
                            score=0.9 if rel else 0.1, confidence=0.97)
        lead = any(k in low for k in ("pricing", "buy", "price", "+1"))
        return Decision(label="yes" if lead else "no",
                        score=0.93 if lead else 0.1, confidence=0.97,
                        extracted={"phone": "+14155550142"} if lead else {})

    def classify_image(self, *, instruction, images_b64, campaign_id, stage,
                       session_id=None, system=None):
        return Decision(label="irrelevant", score=0.0, confidence=1.0)


def _store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path), path


def _campaign():
    return campaign_from_brief("x-leadgen", {
        "platform": "x", "threshold": 0.7,
        "relevance_def": "posts hiring or promoting a saas product",
        "match_def": "a replier/quoter asking price / showing intent",
        "extract_def": "- phone"})


def _pacer():
    return Pacer(cfg=PacingConfig(enforce_daytime=False), sleep=lambda _t: None)


def _run(store, *, feed, cfg=None, lead_target=None, run_id=None):
    return XSession(store=store, router=SpyRouter(), feed=feed, soul=None,
                    campaign=_campaign(), pacer=_pacer(), cfg=cfg,
                    session_id=None, run_id=run_id, lead_target=lead_target).run()


def _feed():
    # One relevant post with a matching reply and a matching quote-post.
    return FakeFeed([
        Reel(reel_id="1500", caption="We are hiring a CFO", author="acme.io", comments=[
            Comment(comment_id="2001", username="aziz", text="pricing? +1", is_reply=True),
            Comment(comment_id="3001", username="buyer", text="where to buy? +1", is_reply=False),
            Comment(comment_id="2002", username="bot", text="gm 🔥", is_reply=True)]),
        Reel(reel_id="1600", caption="cat memes", author="pets", comments=[]),
    ])


def test_matches_both_surfaces_and_tags_surface():
    store, _ = _store()
    summary = _run(store, feed=_feed())
    assert summary["reels_seen"] == 2
    assert summary["relevance_passes"] == 1
    assert summary["matches"] == 2                 # the reply + the quote (bot is noise)
    rows = {r["comment_id"]: r for r in store.matches("x-leadgen")}
    assert rows["2001"]["extracted"]["surface"] == "reply"
    assert rows["3001"]["extracted"]["surface"] == "quote"
    assert rows["2001"]["extracted"]["phone"] == "+14155550142"
    assert all(r["platform"] == "x" for r in rows.values())


def test_summary_is_readonly_and_unhalted():
    store, _ = _store()
    s = _run(store, feed=_feed())
    assert s["likes"] == 0 and s["follows"] == 0 and s["halt_reason"] is None
    assert set(s) >= {"session_id", "reels_seen", "relevance_passes", "matches",
                      "escalations", "already_seen_skips", "spend_usd",
                      "feed_health_flag", "likes", "follows", "halt_reason"}


def test_read_budget_soft_cap_stops_before_lockout():
    store, path = _store()
    posts = [Reel(reel_id=f"p{i}", caption="cat memes", author="x", comments=[])
             for i in range(5)]
    summary = _run(store, feed=FakeFeed(posts),
                   cfg=SessionConfig(read_view_soft_cap=2))
    assert summary["reels_seen"] == 2             # stopped at the read-budget cap
    assert summary["halt_reason"] is None         # soft stop, not a hard halt
    import sqlite3
    con = sqlite3.connect(path)
    n = con.execute("SELECT count(*) FROM health_flags WHERE kind=?", ("read_budget",)).fetchone()[0]
    con.close()
    assert n == 1                                  # the soft read-budget flag was raised


def test_lead_target_stops_early():
    store, _ = _store()
    feed = FakeFeed([
        Reel(reel_id="1500", caption="hiring CFO", author="a", comments=[
            Comment(comment_id="2001", username="x", text="price +1", is_reply=True)]),
        Reel(reel_id="1600", caption="hiring CFO", author="b", comments=[
            Comment(comment_id="2002", username="y", text="buy +1", is_reply=True)]),
    ])
    summary = _run(store, feed=feed, lead_target=1)
    assert summary["matches"] == 1
    assert summary["reels_seen"] == 1


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
    # "cat memes" never matches the hiring/cfo/acme relevance → common path only.
    return [Reel(reel_id=f"p{i}", caption="cat memes", author="x", comments=[])
            for i in range(n)]


def _progress_events(store, run_id):
    return [e for e in store.fetch_run_events(run_id)
            if e["phase"] == "feed_walk" and "Scanning" in e["message"]]


def test_scanning_irrelevant_posts_emits_throttled_progress_event():
    store, _ = _store()
    clock = _Clock(0.0)
    step = PROGRESS_EVENT_INTERVAL_SEC + 5.0
    feed = _ClockAdvancingFeed(_irrelevant_posts(4), clock, step)
    out = XSession(store=store, router=SpyRouter(), feed=feed, soul=None,
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
    out = XSession(store=store, router=SpyRouter(), feed=feed, soul=None,
                   campaign=_campaign(), pacer=_pacer(), run_id="run-throttle",
                   clock=clock).run()
    assert out["reels_seen"] == 5
    assert _progress_events(store, "run-throttle") == []
