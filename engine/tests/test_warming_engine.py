"""Warming engine mode — dispatch routing, dwell-only session, config plumbing
(warming PRD §4). P0 invariant: a warming run emits ZERO write actions."""
import os
import tempfile

import pytest

from reelradar import dispatch
from reelradar.core.accounts import PROVISIONED, WARMING, warming_sentinel_campaign
from reelradar.core.config import Campaign, campaign_from_brief, campaign_to_brief
from reelradar.core.feed import Comment, FakeFeed, Reel
from reelradar.core.pacing import Pacer, PacingConfig
from reelradar.core.store import Store


def fresh_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path), path


def _campaign(platform="x", engine_mode="warming"):
    return Campaign(
        campaign_id="camp1", goal="g", threshold=0.7, escalate_band=(0.4, 0.75),
        language_mix=[], relevance_def="", match_def="", extract_def="",
        seed_direction="", raw="", path=__import__("pathlib").Path("<test>"),
        platform=platform, engine_mode=engine_mode)


def _feed():
    return FakeFeed([
        Reel(reel_id=f"r{i}", author=f"a{i}", caption="c",
             comments=[Comment(f"c{i}", "u", "hi", "en")])
        for i in range(12)
    ])


def _no_sleep_pacer():
    return Pacer(cfg=PacingConfig(enforce_daytime=False), sleep=lambda _t: None)


# ---- config plumbing ----

def test_engine_mode_defaults_to_harvest():
    c = campaign_from_brief("c", {"platform": "x"})
    assert c.engine_mode == "harvest"


def test_engine_mode_round_trips_through_brief():
    c = campaign_from_brief("c", {"platform": "x", "engine_mode": "warming"})
    assert c.engine_mode == "warming"
    assert campaign_to_brief(c)["engine_mode"] == "warming"


def test_invalid_engine_mode_rejected():
    with pytest.raises(ValueError, match="engine_mode"):
        campaign_from_brief("c", {"platform": "x", "engine_mode": "bogus"})


# ---- dispatch routing ----

def test_dispatch_routes_warming_without_calling_select_engine(monkeypatch):
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp1", org_id=1, status="live")
    store.add_account(1, "x", "acct")
    # If warming ever fell through to select_engine, this would explode.
    monkeypatch.setattr(dispatch, "select_engine",
                        lambda p: (_ for _ in ()).throw(AssertionError("harvest path!")))
    summary = dispatch.run_engine_session(
        campaign=_campaign(), store=store, router=None, feed=_feed(),
        soul=None, pacer=_no_sleep_pacer(), engine_mode="warming")
    assert "session_id" in summary and summary["halt_reason"] is None


# ---- warming session behavior ----

def test_warming_run_emits_zero_write_actions():
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp1", org_id=1, status="live")
    aid = store.add_account(1, "x", "acct")
    dispatch.run_engine_session(
        campaign=_campaign(), store=store, router=None, feed=_feed(),
        soul=None, pacer=_no_sleep_pacer(), engine_mode="warming")
    n = store._conn.execute("SELECT COUNT(*) AS n FROM actions").fetchone()["n"]
    assert n == 0                                        # P0: dwell-only, no writes
    # The session is recorded as a completed WARMING session for the warmth model.
    row = store._conn.execute(
        "SELECT engine_mode, status, account_id, campaign_id FROM sessions").fetchone()
    assert row["engine_mode"] == "warming" and row["status"] == "completed"
    assert row["account_id"] == aid
    assert row["campaign_id"] == warming_sentinel_campaign(1)


def test_warming_transitions_provisioned_to_warming():
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp1", org_id=1, status="live")
    aid = store.add_account(1, "x", "acct")
    assert store.get_account(aid)["state"] == PROVISIONED
    dispatch.run_engine_session(
        campaign=_campaign(), store=store, router=None, feed=_feed(),
        soul=None, pacer=_no_sleep_pacer(), engine_mode="warming")
    assert store.get_account(aid)["state"] == WARMING


def test_warming_halts_when_no_account():
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp1", org_id=1, status="live")
    summary = dispatch.run_engine_session(
        campaign=_campaign(), store=store, router=None, feed=_feed(),
        soul=None, pacer=_no_sleep_pacer(), engine_mode="warming")
    assert summary["halt_reason"] == "no warmable account"


def test_warming_halts_outside_daytime():
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp1", org_id=1, status="live")
    store.add_account(1, "x", "acct")
    from datetime import datetime
    night = Pacer(cfg=PacingConfig(enforce_daytime=True),
                  clock=lambda: datetime(2026, 1, 1, 3, 0), sleep=lambda _t: None)
    summary = dispatch.run_engine_session(
        campaign=_campaign(), store=store, router=None, feed=_feed(),
        soul=None, pacer=night, engine_mode="warming")
    assert summary["halt_reason"] == "outside daytime window"


def test_warming_records_activity_in_account_detail():
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp1", org_id=1, status="live")
    aid = store.add_account(1, "x", "acct")
    dispatch.run_engine_session(
        campaign=_campaign(), store=store, router=None, feed=_feed(),
        soul=None, pacer=_no_sleep_pacer(), engine_mode="warming")
    detail = store.get_account(aid)["detail"]
    assert detail["last_activity_kind"] == "observe"
    assert detail["dwell_windows"] >= 1
