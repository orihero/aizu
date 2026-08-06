import json
import os
import tempfile
from pathlib import Path

from aizu.core.config import load_campaign, load_soul
from aizu.core.feed import Comment, FakeFeed, Reel
from aizu.core.mock_router import MockRouter
from aizu.core.pacing import PacingConfig, Pacer
from aizu.panel import build_raw
from aizu.engines.instagram.session import Session, SessionConfig
from aizu.core.store import Store

CONFIG = Path(__file__).resolve().parents[1] / "config"


def _seeded_store():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    store = Store(path)
    feed = FakeFeed([
        Reel("r1", author="acme.io", caption="Acme app — sprint planning, free trial",
             ocr_text="Pro from $12/seat", comments=[
                 Comment("c1", "dana", "How much is the Pro plan? +1 415 555 0142", "en"),
                 Comment("c2", "bot", "🔥", "en"),
             ]),
        Reel("r2", author="cats", caption="funny cats", comments=[Comment("c3", "x", "lol", "en")]),
    ])
    Session(store=store, router=MockRouter(store=store), feed=feed,
            soul=load_soul(CONFIG / "soul.md"),
            campaign=load_campaign(CONFIG / "campaign.md"),
            pacer=Pacer(cfg=PacingConfig(enforce_daytime=False), sleep=lambda _t: None),
            cfg=SessionConfig()).run()
    return store


def _raw():
    store = _seeded_store()
    return build_raw(store, load_soul(CONFIG / "soul.md"),
                     load_campaign(CONFIG / "campaign.md"))


def test_raw_has_all_keys():
    raw = _raw()
    for k in ["CONFIG", "CAMPAIGNS", "SESSIONS", "REELS", "MATCHES",
              "ESCALATION_LOG", "ALERTS", "HEALTH", "SOUL"]:
        assert k in raw


def test_raw_is_json_serializable():
    json.dumps(_raw(), ensure_ascii=False)  # must not raise


def test_sessions_and_matches_reconcile():
    raw = _raw()
    assert len(raw["SESSIONS"]) == 1
    s = raw["SESSIONS"][0]
    # runId is surfaced for deep-linking into the activity feed; None here since
    # this session ran without RunManager correlation.
    assert "runId" in s and s["runId"] is None
    assert s["reelsSeen"] == 2
    assert s["matches"] == len(raw["MATCHES"]) == 1   # only c1 is a lead
    m = raw["MATCHES"][0]
    assert m["reelId"] == "r1" and m["sessionId"] == s["id"]
    assert "555" in (m["extracted"].get("phone") or "")


def test_reels_only_relevant_with_content():
    raw = _raw()
    ids = {r["id"] for r in raw["REELS"]}
    assert "r1" in ids and "r2" not in ids       # r2 (cats) is irrelevant
    r1 = next(r for r in raw["REELS"] if r["id"] == "r1")
    assert r1["author"] == "acme.io" and "Acme" in r1["caption"]


def test_platform_surfaced_on_matches_sessions_and_summary():
    raw = _raw()
    # every match + session carries its platform (defaulting to instagram)
    assert all(m["platform"] == "instagram" for m in raw["MATCHES"])
    assert all(s["platform"] == "instagram" for s in raw["SESSIONS"])
    # the per-platform rollup is present and reconciles with the match count
    assert "PLATFORMS" in raw
    ig = next(p for p in raw["PLATFORMS"] if p["platform"] == "instagram")
    assert ig["matches"] == len(raw["MATCHES"])
    assert ig["sessions"] == len(raw["SESSIONS"])


def test_config_and_soul_from_brief():
    raw = _raw()
    assert raw["CONFIG"]["matchThreshold"] == 0.70
    assert raw["SOUL"]["rules"], "soul rules should be parsed"
    assert raw["CAMPAIGNS"][0]["goalType"] == "lead"
    assert "phone" in raw["CAMPAIGNS"][0]["extractFields"]


# --- Multi-platform card + brief form (Phase 4) ------------------------------

from datetime import datetime

from aizu.panel import (_all_campaign_platforms, _brief_form_from_campaign,
                             _brief_form_from_stored, _draft_campaign)
from aizu.core.config import campaign_from_brief


def _bare_store():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    return Store(path)


def test_all_campaign_platforms_from_channels():
    assert _all_campaign_platforms(
        {"channels": [{"platform": "instagram"}, {"platform": "youtube"}]}
    ) == ["instagram", "youtube"]
    # No channels → fall back to the single flat platform, else the default.
    assert _all_campaign_platforms({"platform": "x"}) == ["x"]
    assert _all_campaign_platforms({}) == ["instagram"]


def test_brief_form_from_stored_emits_channels():
    form = _brief_form_from_stored({"platform": "instagram", "channels": [
        {"platform": "youtube", "seed_channels": ["UC1"], "include_home_feed": False}]})
    assert form["channels"] == [{"platform": "youtube", "seedHashtags": [],
                                 "seedAccounts": [], "seedChannels": ["UC1"],
                                 "includeHomeFeed": False}]


def test_brief_form_from_stored_empty_channels_emits_empty_list():
    assert _brief_form_from_stored({"platform": "instagram"})["channels"] == []


def test_brief_form_from_campaign_emits_channels():
    c = campaign_from_brief("c", {"channels": [
        {"platform": "instagram"}, {"platform": "youtube"}]})
    form = _brief_form_from_campaign(c)
    assert [ch["platform"] for ch in form["channels"]] == ["instagram", "youtube"]


def test_draft_campaign_card_has_platforms_list():
    store = _bare_store()
    try:
        store.upsert_campaign_meta("multi", status="draft")
        store.upsert_campaign_brief("multi", {"channels": [
            {"platform": "instagram"}, {"platform": "youtube"}]})
        m = next(x for x in store.list_campaign_meta() if x["campaign_id"] == "multi")
        card = _draft_campaign(store, m, {}, datetime.now(), 20.0)
    finally:
        store.close()
    assert card["platforms"] == ["instagram", "youtube"]


def test_single_platform_card_platforms_list_fallback():
    store = _bare_store()
    try:
        store.upsert_campaign_meta("solo", status="draft")
        store.upsert_campaign_brief("solo", {"platform": "youtube",
                                             "seed_channels": ["UC1"]})
        m = next(x for x in store.list_campaign_meta() if x["campaign_id"] == "solo")
        card = _draft_campaign(store, m, {}, datetime.now(), 20.0)
    finally:
        store.close()
    assert card["platforms"] == ["youtube"]      # falls back to [platform]
