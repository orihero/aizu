import os
import tempfile
from pathlib import Path

from reelradar.engines.instagram.actions import ActionPolicy
from reelradar.core.config import campaign_from_brief, load_campaign, load_soul
from reelradar.core.feed import Comment, FakeFeed, Reel
from reelradar.core.mock_router import MockRouter
from reelradar.core.pacing import PacingConfig, Pacer
from reelradar.engines.instagram.session import HaltSession, Session, SessionConfig
from reelradar.core.store import Store

CONFIG = Path(__file__).resolve().parents[1] / "config"


def _store():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    return Store(path), path


def _pacer():
    return Pacer(cfg=PacingConfig(enforce_daytime=False), sleep=lambda _t: None)


def _sample():
    return FakeFeed([
        Reel("r1", caption="Acme app — plan your sprints, free trial", comments=[
            Comment("c1", "dana", "How much is the Pro plan? +1 415 555 0142", "en"),
            Comment("c2", "bot", "🔥🔥", "en"),
        ]),
        Reel("r2", caption="funny cats", comments=[Comment("c3", "x", "lol", "en")]),
        Reel("r3", caption="New Acme app dashboard, book a demo", comments=[
            Comment("c4", "maria", "How much? want to buy, +1 312 555 0199", "en"),
        ]),
    ])


def run_session(store, feed, **cfg):
    return Session(store=store, router=MockRouter(store=store), feed=feed,
                   soul=load_soul(CONFIG / "soul.md"),
                   campaign=load_campaign(CONFIG / "campaign.md"),
                   pacer=_pacer(), cfg=SessionConfig(**cfg)).run()


def test_session_honors_pause_file_between_reels(tmp_path):
    """v12: with the pause sentinel present, the loop idles at the between-reels
    checkpoint; once it's removed the run resumes and walks the feed. Emits exactly
    one paused + one resumed step to the activity feed."""
    store, _ = _store()
    pause_file = tmp_path / "run-x.pause"
    pause_file.touch()
    calls = {"n": 0}

    def fake_sleep(_secs):
        calls["n"] += 1
        pause_file.unlink()      # operator resumes after the first poll

    s = Session(store=store, router=MockRouter(store=store), feed=_sample(),
                soul=load_soul(CONFIG / "soul.md"),
                campaign=load_campaign(CONFIG / "campaign.md"),
                pacer=_pacer(), cfg=SessionConfig(),
                run_id="x", pause_file=pause_file, pause_sleep=fake_sleep)
    summary = s.run()

    assert calls["n"] == 1                       # idled once on the sentinel
    assert summary["reels_seen"] >= 1            # then resumed and walked the feed
    events = store.fetch_run_events("x")
    messages = [e["message"] for e in events]
    assert any("paused" in m.lower() for m in messages)
    assert any("resumed" in m.lower() for m in messages)


def test_session_no_pause_when_sentinel_absent(tmp_path):
    store, _ = _store()
    calls = {"n": 0}
    s = Session(store=store, router=MockRouter(store=store), feed=_sample(),
                soul=load_soul(CONFIG / "soul.md"),
                campaign=load_campaign(CONFIG / "campaign.md"),
                pacer=_pacer(), cfg=SessionConfig(),
                pause_file=tmp_path / "absent.pause",
                pause_sleep=lambda _s: calls.__setitem__("n", calls["n"] + 1))
    s.run()
    assert calls["n"] == 0                       # never idled — nothing to pause on


def test_runs_a_db_authored_brief():
    """A panel-authored brief (campaign_from_brief) runs through the same Session
    path as a file campaign and lands matches under its campaign_id."""
    store, _ = _store()
    campaign = campaign_from_brief("ui-leadgen", {
        "platform": "instagram", "threshold": 0.7,
        "relevance_def": "saas product", "match_def": "buyer intent",
        "extract_def": "- phone",
    })
    Session(store=store, router=MockRouter(store=store), feed=_sample(),
            soul=load_soul(CONFIG / "soul.md"), campaign=campaign,
            pacer=_pacer(), cfg=SessionConfig()).run()
    rows = store.matches("ui-leadgen")
    assert rows and all(r["platform"] == "instagram" for r in rows)


def test_end_to_end_dry_run_finds_leads():
    store, _ = _store()
    summary = run_session(store, _sample())
    assert summary["reels_seen"] == 3
    assert summary["relevance_passes"] >= 2     # r1 + r3 relevant, r2 not
    assert summary["matches"] >= 2              # the two intent+phone/buy comments
    matches = store.matches("acme-saas-leadgen")
    phones = [m["extracted"].get("phone") for m in matches if m["extracted"]]
    assert any(p and "555" in p for p in phones)


def test_lead_target_stops_session_early():
    """With lead_target=1 the session stops as soon as it lands its first match —
    it must not keep walking the rest of the reel budget."""
    store, _ = _store()
    s = Session(store=store, router=MockRouter(store=store), feed=_sample(),
                soul=load_soul(CONFIG / "soul.md"),
                campaign=load_campaign(CONFIG / "campaign.md"),
                pacer=_pacer(), cfg=SessionConfig(), lead_target=1)
    summary = s.run()
    assert summary["matches"] == 1
    assert summary["reels_seen"] == 1   # broke before walking r2/r3


def test_lead_target_above_supply_walks_full_feed():
    """A target larger than what the feed can supply behaves like no target: the
    session walks its whole budget and returns every match it found."""
    store, _ = _store()
    s = Session(store=store, router=MockRouter(store=store), feed=_sample(),
                soul=load_soul(CONFIG / "soul.md"),
                campaign=load_campaign(CONFIG / "campaign.md"),
                pacer=_pacer(), cfg=SessionConfig(), lead_target=100)
    summary = s.run()
    assert summary["reels_seen"] == 3
    assert summary["matches"] >= 2


def test_resume_skips_already_seen():
    store, path = _store()
    run_session(store, _sample())
    # Second run on the same DB: everything already seen → all skipped.
    summary2 = run_session(store, _sample())
    assert summary2["already_seen_skips"] == 3
    assert summary2["relevance_passes"] == 0


def test_tired_feed_flag_fires():
    store, _ = _store()
    # Pre-seed 20 reels as seen, then feed them again → 100% already-seen.
    cid = "acme-saas-leadgen"
    reels = [Reel(f"x{i}", caption="acme saas app") for i in range(20)]
    for r in reels:
        store.mark_seen(cid, r.reel_id, relevant=True)
    summary = run_session(store, FakeFeed(reels),
                          tired_feed_min_reels=10, tired_feed_ratio=0.6)
    assert summary["feed_health_flag"] is True
    assert any(f["kind"] == "feed_health" for f in store.open_flags())


class BlindFeed(FakeFeed):
    """Interception canary always unhealthy → should halt."""
    def healthy(self):
        return False


def test_empty_interception_halts():
    store, _ = _store()
    reels = [Reel(f"r{i}", caption="acme saas app", comments=[Comment(f"c{i}", "u", "pricing?")])
             for i in range(10)]
    try:
        run_session(store, BlindFeed(reels), empty_interception_halt=3)
        assert False, "expected HaltSession"
    except HaltSession as h:
        assert "empty interception" in h.reason
    assert any(f["severity"] == "halt" for f in store.open_flags())


class RecordingFeed(FakeFeed):
    """Records like/follow calls; can simulate an action-block after N actions."""
    def __init__(self, reels, block_after=None):
        super().__init__(reels)
        self.likes = []
        self.follows = []
        self.opened = []
        self._block_after = block_after

    def open_reel(self, reel):
        self.opened.append(reel.reel_id)
        return True

    def like_reel(self, reel):
        self.likes.append(reel.reel_id)
        return True

    def follow_author(self, reel):
        self.follows.append(reel.author or reel.reel_id)
        return True

    def detect_action_block(self):
        if self._block_after is None:
            return False
        return (len(self.likes) + len(self.follows)) >= self._block_after


def _build_session(store, feed, actions=None, **cfg):
    s = Session(store=store, router=MockRouter(store=store), feed=feed,
                soul=load_soul(CONFIG / "soul.md"),
                campaign=load_campaign(CONFIG / "campaign.md"),
                pacer=_pacer(), cfg=SessionConfig(**cfg))
    if actions is not None:
        s.actions = actions
    return s


def test_likes_relevant_and_follows_on_lead():
    store, _ = _store()
    feed = RecordingFeed(_sample()._reels)
    # The shipped campaign is read-only now (warming PRD §4.3); enable engagement
    # explicitly to exercise the Instagram like/follow path.
    summary = _build_session(
        store, feed,
        actions=ActionPolicy(enabled=True, max_likes=8, max_follows=4)).run()
    assert len(feed.likes) >= 2                 # r1 + r3 are relevant → liked
    assert len(feed.follows) >= 1               # at least one lead → author followed
    assert len(feed.opened) >= 2                # relevant reels opened full-screen first
    assert summary["likes"] == len(feed.likes)
    counts = store.action_counts("acme-saas-leadgen")
    assert counts.get("like") == len(feed.likes)


class UnopenableFeed(FakeFeed):
    """Permalinks never resolve (deleted/unavailable reels)."""
    def __init__(self, reels):
        super().__init__(reels)
        self.fetches = []

    def open_reel(self, reel):
        return False

    def fetch_comments(self, reel_id, since_cursor):
        self.fetches.append(reel_id)
        return super().fetch_comments(reel_id, since_cursor)


def test_unopenable_reel_skips_comments_and_flags():
    store, _ = _store()
    feed = UnopenableFeed(_sample()._reels)
    summary = run_session(store, feed)
    # Never scrape a page that didn't open — that's how leads get attributed
    # to the wrong reel/author.
    assert feed.fetches == []
    assert summary["matches"] == 0
    assert any(f["kind"] == "reel_unavailable" for f in store.open_flags())


def test_actions_disabled_means_no_actions():
    store, _ = _store()
    feed = RecordingFeed(_sample()._reels)
    _build_session(store, feed, actions=ActionPolicy(enabled=False)).run()
    assert feed.likes == [] and feed.follows == []


def test_action_caps_enforced():
    store, _ = _store()
    feed = RecordingFeed(_sample()._reels)
    _build_session(store, feed,
                   actions=ActionPolicy(enabled=True, max_likes=1, max_follows=0)).run()
    assert len(feed.likes) == 1 and feed.follows == []


def test_action_block_halts_session():
    store, _ = _store()
    feed = RecordingFeed(_sample()._reels, block_after=1)
    try:
        _build_session(
            store, feed,
            actions=ActionPolicy(enabled=True, max_likes=8, max_follows=4)).run()
        assert False, "expected HaltSession"
    except HaltSession as h:
        assert "action-block" in h.reason
    assert any(f["severity"] == "halt" for f in store.open_flags())


def test_extraction_fills_lead_contact_from_comment_only():
    """The lead's phone/email come only from the COMMENT — the company's contact
    details on the reel (its own number in the OCR) must never leak into the lead;
    intent is read from the comment."""
    store, _ = _store()
    feed = FakeFeed([
        Reel("r9", author="acme.io",
             caption="Acme app — sprint planning, free trial",
             ocr_text="ACME · Pro from $12/seat · Tel: +1 800 555 0100",
             comments=[
                 Comment("c9", "marina", "How much? want to buy, +1 312 555 0199", "en"),
                 Comment("c10", "timur", "How much is the Pro plan?", "en"),
             ]),
    ])
    run_session(store, feed)
    by_id = {m["comment_id"]: m for m in store.matches("acme-saas-leadgen")}

    with_phone = by_id["c9"]["extracted"]
    assert "312 555 0199" in with_phone["phone"]
    assert with_phone["intent"] in ("buy", "pricing", "inquire")

    no_phone = by_id["c10"]["extracted"]
    assert no_phone["phone"] is None, "company's phone on the reel must not leak"
    assert no_phone["intent"] in ("pricing", "inquire")


# ----- v10: live run activity events -----

def _run_with_run_id(store, feed, run_id, **cfg):
    return Session(store=store, router=MockRouter(store=store), feed=feed,
                   soul=load_soul(CONFIG / "soul.md"),
                   campaign=load_campaign(CONFIG / "campaign.md"),
                   pacer=_pacer(), cfg=SessionConfig(**cfg), run_id=run_id).run()


def test_session_with_run_id_emits_milestone_events():
    """A panel-launched session streams an ordered narrative: started → relevance →
    match → completed, with the right levels."""
    store, _ = _store()
    _run_with_run_id(store, _sample(), "run-1")
    events = store.fetch_run_events("run-1")
    assert events, "expected activity events for a run with a run_id"
    messages = [e["message"] for e in events]
    levels = {e["message"]: e["level"] for e in events}
    # Lifecycle bookends.
    assert messages[0].startswith("Run started")
    assert events[0]["level"] == "info"
    assert any(m.startswith("Run completed") for m in messages)
    assert levels[next(m for m in messages if m.startswith("Run completed"))] == "success"
    # The discovery milestones.
    assert any(m.startswith("Relevant ✓") for m in messages)
    assert any(m.startswith("Match: @") for m in messages)
    # seq is monotonic within the session; id is globally ordered.
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)
    assert [e["id"] for e in events] == sorted(e["id"] for e in events)


def test_session_without_run_id_emits_nothing():
    """A direct CLI / unit run (run_id unset) must not write any activity rows."""
    store, _ = _store()
    run_session(store, _sample())   # the default helper passes no run_id
    count = store._conn.execute("SELECT COUNT(*) AS n FROM run_events").fetchone()["n"]
    assert count == 0


def test_halt_emits_error_event():
    store, _ = _store()
    reels = [Reel(f"r{i}", caption="acme saas app",
                  comments=[Comment(f"c{i}", "u", "pricing?")]) for i in range(10)]
    try:
        _run_with_run_id(store, BlindFeed(reels), "run-h", empty_interception_halt=3)
    except HaltSession:
        pass
    events = store.fetch_run_events("run-h")
    halts = [e for e in events if e["phase"] == "halt"]
    assert halts and halts[-1]["level"] == "error"
    assert halts[-1]["message"].startswith("Halted:")
