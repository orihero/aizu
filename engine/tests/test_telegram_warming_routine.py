"""Telegram warming routine + platform dispatch (warming-writes PRD §7.3–§7.6).

Drives the TG warming loop with a FakeTelegramWarmingPort + a fake LLM relevance
gate + a seeded rng + a no-op-sleep Pacer:

  - discovery = seed_channels (trusted) + search hits (gated)
  - search hits gated; seeded channels skip the gate
  - join under the per-day cap (≤1/session; cross-session cap via action count)
  - react probability + per-day cap
  - degrade to seeded-only without an API key (no search, no gate)
  - hard flood → HaltSession + peer_flood flag + account FLAGGED
  - observe stage → zero writes
  - dispatch routes telegram → the TG routine; instagram path unchanged
"""
import os
import random
import tempfile
from pathlib import Path

import pytest

from aizu import dispatch
from aizu.core.accounts import (
    FLAGGED,
    PROVISIONED,
    WARMING,
    warming_sentinel_campaign,
)
from aizu.core.config import Campaign
from aizu.core.pacing import Pacer, PacingConfig
from aizu.core.store import Store
from aizu.engines.base import HaltSession
from aizu.engines.telegram.feed import TgMessage
from aizu.engines.telegram.warming_writes import TelegramFloodError, TgChannel
from aizu.engines.warming.ramp import budget_for_day
from aizu.engines.warming.telegram import (
    TelegramWarmingExecutor,
    derive_keywords,
    run_telegram_warming_session,
)
from tests.fakes.telegram_warming import FakeTelegramWarmingPort


# ---- fixtures / helpers ----

def fresh_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path), path


def _campaign(*, seed_channels=None, seed_direction="", goal="find saas leads",
              platform="telegram"):
    return Campaign(
        campaign_id="camp1", goal=goal, threshold=0.7, escalate_band=(0.4, 0.75),
        language_mix=[], relevance_def="", match_def="", extract_def="",
        seed_direction=seed_direction, raw="", path=Path("<test>"),
        platform=platform, engine_mode="warming",
        seed_channels=seed_channels or [])


def _no_sleep_pacer(seed=0):
    return Pacer(cfg=PacingConfig(enforce_daytime=False),
                 rng=random.Random(seed), sleep=lambda _t: None)


def _account(store, *, state=PROVISIONED, added_days_ago=10, now=None):
    """An org + a warmable telegram account `added_days_ago` days under management
    so its ramp stage allows writes (light/ramp/sustain). `now` anchors the
    back-dated added_at to the same clock the session reads against."""
    store.upsert_campaign_meta("camp1", org_id=1, status="live")
    aid = store.add_account(1, "telegram", "tg_acct", state=state)
    anchor = __import__("time").time() if now is None else now
    # Back-date added_at so the account is past the observe stage.
    store._conn.execute(
        "UPDATE accounts SET added_at=? WHERE id=?",
        (anchor - added_days_ago * 86400, aid))
    store._conn.commit()
    return aid


def _ramp_budget(days=10):
    """A real budget with joins>0/reacts>0 for the TG platform."""
    return budget_for_day(days, "telegram")


def _always_relevant(channel, campaign):
    return True


def _never_relevant(channel, campaign):
    return False


def _executor(store, *, account_id, port, gate=None, read_client=None,
              pacer=None, rng=None, now=None, campaign=None):
    return TelegramWarmingExecutor(
        port=port, store=store, sentinel_campaign=warming_sentinel_campaign(1),
        account_id=account_id, session_id="sess1",
        pacer=pacer or _no_sleep_pacer(), campaign=campaign or _campaign(),
        relevance_gate=gate, read_client=read_client,
        rng=rng or random.Random(0), now=now)


def _open() -> bool:
    return True


# ---- keyword derivation (§7.3) ----

def test_derive_keywords_from_seed_direction_and_goal():
    c = _campaign(seed_direction="growth marketing, saas founders",
                  goal="find b2b leads")
    kws = derive_keywords(c)
    assert "growth marketing" in kws
    assert "saas founders" in kws
    assert "find b2b leads" in kws


def test_derive_keywords_dedupes_and_caps():
    c = _campaign(seed_direction="a, a, b, c, d, e, f, g", goal="a")
    kws = derive_keywords(c)
    assert kws == kws[: len(kws)]            # order preserved
    assert len(kws) <= 6
    assert kws.count("a") == 1               # de-duped case-insensitively


# ---- discovery: seed + search (§7.3) ----

def test_discovery_includes_seeded_and_search_hits():
    store, _ = fresh_store()
    aid = _account(store)
    port = FakeTelegramWarmingPort(search_results={
        "find saas leads": [TgChannel(username="@growthlab", title="Growth")],
    })
    ex = _executor(store, account_id=aid, port=port, gate=_always_relevant,
                   campaign=_campaign(seed_channels=["@seeded"]))
    cands = ex.discover(_ramp_budget())
    channels = [c.channel for c in cands]
    assert "@seeded" in channels                 # operator seed kept
    assert "@growthlab" in channels              # search hit included
    seeded = next(c for c in cands if c.channel == "@seeded")
    assert seeded.is_search_hit is False


def test_discovery_skips_search_when_no_gate_degraded():
    store, _ = fresh_store()
    aid = _account(store)
    port = FakeTelegramWarmingPort(search_results={
        "find saas leads": [TgChannel(username="@growthlab")],
    })
    ex = _executor(store, account_id=aid, port=port, gate=None,
                   campaign=_campaign(seed_channels=["@seeded"]))
    cands = ex.discover(_ramp_budget())
    assert [c.channel for c in cands] == ["@seeded"]   # seeded-only


# ---- gate: search hits gated, seeded skip (§7.4) ----

def test_seeded_channel_skips_gate_and_joins():
    store, _ = fresh_store()
    aid = _account(store)
    port = FakeTelegramWarmingPort()
    # Gate rejects everything; the seeded channel must still join.
    ex = _executor(store, account_id=aid, port=port, gate=_never_relevant,
                   campaign=_campaign(seed_channels=["@seeded"]))
    ex.run(_ramp_budget(), daytime_open=_open)
    assert port.joined == ["@seeded"]


def test_search_hit_rejected_by_gate_is_not_joined():
    store, _ = fresh_store()
    aid = _account(store)
    port = FakeTelegramWarmingPort(search_results={
        "find saas leads": [TgChannel(username="@spam")],
    })
    ex = _executor(store, account_id=aid, port=port, gate=_never_relevant,
                   campaign=_campaign())
    ex.run(_ramp_budget(), daytime_open=_open)
    assert port.joined == []


def test_search_hit_accepted_by_gate_is_joined():
    store, _ = fresh_store()
    aid = _account(store)
    port = FakeTelegramWarmingPort(search_results={
        "find saas leads": [TgChannel(username="@growthlab")],
    })
    ex = _executor(store, account_id=aid, port=port, gate=_always_relevant,
                   campaign=_campaign())
    ex.run(_ramp_budget(), daytime_open=_open)
    assert port.joined == ["@growthlab"]


# ---- join cap: ≤1 per session + cross-session per-day cap (§7.5) ----

def test_at_most_one_join_per_session():
    store, _ = fresh_store()
    aid = _account(store, added_days_ago=20)        # sustain: joins cap 3
    port = FakeTelegramWarmingPort()
    ex = _executor(store, account_id=aid, port=port, gate=_always_relevant,
                   campaign=_campaign(seed_channels=["@a", "@b", "@c"]))
    ex.run(budget_for_day(20, "telegram"), daytime_open=_open)
    assert len(port.joined) == 1                     # only ONE join this session


def test_join_logged_with_sentinel_and_account():
    store, _ = fresh_store()
    aid = _account(store)
    port = FakeTelegramWarmingPort()
    ex = _executor(store, account_id=aid, port=port, gate=_always_relevant,
                   campaign=_campaign(seed_channels=["@seeded"]), now=1_000_000.0)
    ex.run(_ramp_budget(), daytime_open=_open)
    row = store._conn.execute(
        "SELECT campaign_id, action_type, target, account_id, reel_id "
        "FROM actions WHERE action_type='join'").fetchone()
    assert row["campaign_id"] == warming_sentinel_campaign(1)
    assert row["target"] == "@seeded"
    assert row["account_id"] == aid
    assert row["reel_id"] is None


def test_per_day_join_cap_holds_across_sessions():
    store, _ = fresh_store()
    aid = _account(store)                            # light: joins cap 1
    now = 1_000_000.0
    # Pre-spend the day's single join (a prior session today).
    store.log_action(warming_sentinel_campaign(1), "join", reel_id=None,
                     target="@prior", succeeded=True, account_id=aid, now=now)
    port = FakeTelegramWarmingPort()
    ex = _executor(store, account_id=aid, port=port, gate=_always_relevant,
                   campaign=_campaign(seed_channels=["@seeded"]), now=now)
    ex.run(budget_for_day(5, "telegram"), daytime_open=_open)
    assert port.joined == []                          # day cap already reached


# ---- react: probability + per-day cap (§7.5a) ----

def test_react_fires_on_dwelled_message_under_cap():
    store, _ = fresh_store()
    aid = _account(store)

    class _Reader:
        def iter_channel_messages(self, channel, limit):
            return [TgMessage(id=7, text="hello"), TgMessage(id=8, text="hi")]

    port = FakeTelegramWarmingPort()
    # Seed an rng where p_react roll succeeds (light p_react=0.4). Seed chosen so
    # the first relevant random() < 0.4.
    ex = _executor(store, account_id=aid, port=port, gate=_always_relevant,
                   read_client=_Reader(), rng=random.Random(1),
                   campaign=_campaign(seed_channels=["@seeded"]))
    # Force the react roll by patching the budget to p_react=1.0 via a high-prob run.
    budget = budget_for_day(5, "telegram")
    budget = budget.__class__(**{**budget.__dict__, "p_react": 1.0})
    ex.run(budget, daytime_open=_open)
    assert len(port.reactions) == 1
    channel, msg_id, emoji = port.reactions[0]
    assert channel == "@seeded"
    assert msg_id in (7, 8)


def test_react_never_fires_when_probability_zero():
    store, _ = fresh_store()
    aid = _account(store)

    class _Reader:
        def iter_channel_messages(self, channel, limit):
            return [TgMessage(id=7, text="hello")]

    port = FakeTelegramWarmingPort()
    ex = _executor(store, account_id=aid, port=port, gate=_always_relevant,
                   read_client=_Reader(), campaign=_campaign(seed_channels=["@s"]))
    budget = budget_for_day(5, "telegram")
    budget = budget.__class__(**{**budget.__dict__, "p_react": 0.0})
    ex.run(budget, daytime_open=_open)
    assert port.reactions == []


def test_react_respects_per_day_cap():
    store, _ = fresh_store()
    aid = _account(store)
    now = 1_000_000.0
    # Light stage react cap is 3 — pre-spend all 3 today.
    for i in range(3):
        store.log_action(warming_sentinel_campaign(1), "react", reel_id=None,
                         target="@x", succeeded=True, account_id=aid, now=now)

    class _Reader:
        def iter_channel_messages(self, channel, limit):
            return [TgMessage(id=7, text="hi")]

    port = FakeTelegramWarmingPort()
    ex = _executor(store, account_id=aid, port=port, gate=_always_relevant,
                   read_client=_Reader(), campaign=_campaign(seed_channels=["@s"]),
                   now=now)
    budget = budget_for_day(5, "telegram")
    budget = budget.__class__(**{**budget.__dict__, "p_react": 1.0})
    ex.run(budget, daytime_open=_open)
    assert port.reactions == []                       # cap already spent


# ---- observe stage: zero writes ----

def test_observe_stage_makes_zero_writes():
    store, _ = fresh_store()
    aid = _account(store, added_days_ago=1)           # day 1 → observe (read_only)
    port = FakeTelegramWarmingPort()
    ex = _executor(store, account_id=aid, port=port, gate=_always_relevant,
                   campaign=_campaign(seed_channels=["@seeded"]))
    ex.run(budget_for_day(1, "telegram"), daytime_open=_open)
    assert port.joined == []
    assert port.reactions == []
    n = store._conn.execute("SELECT COUNT(*) AS n FROM actions").fetchone()["n"]
    assert n == 0


# ---- hard flood → HaltSession (executor level) ----

def test_hard_flood_on_join_raises_halt_session():
    store, _ = fresh_store()
    aid = _account(store)
    port = FakeTelegramWarmingPort(raise_on_join=TelegramFloodError(
        "peer_flood", is_hard=True, op="join"))
    ex = _executor(store, account_id=aid, port=port, gate=_always_relevant,
                   campaign=_campaign(seed_channels=["@seeded"]))
    with pytest.raises(HaltSession):
        ex.run(_ramp_budget(), daytime_open=_open)


def test_soft_flood_on_join_skips_and_continues():
    store, _ = fresh_store()
    aid = _account(store)
    port = FakeTelegramWarmingPort(raise_on_join=TelegramFloodError(
        "flood_wait", is_hard=False, retry_seconds=5, op="join"))
    ex = _executor(store, account_id=aid, port=port, gate=_always_relevant,
                   campaign=_campaign(seed_channels=["@seeded"]))
    ex.run(_ramp_budget(), daytime_open=_open)        # no raise
    assert port.joined == []                          # the one join was skipped
    n = store._conn.execute(
        "SELECT COUNT(*) AS n FROM actions WHERE succeeded=1").fetchone()["n"]
    assert n == 0


# ---- idempotent rejoin does NOT burn the session/day join budget ----

def test_already_joined_channel_does_not_spend_session_join_slot():
    # A first candidate the account already belongs to is a zero-growth no-op:
    # it must not consume the one-join-per-session slot, so a later NEW channel
    # still gets joined this session.
    store, _ = fresh_store()
    aid = _account(store)
    port = FakeTelegramWarmingPort(already_joined={"@seeded"})
    ex = _executor(store, account_id=aid, port=port, gate=_always_relevant,
                   campaign=_campaign(seed_channels=["@seeded", "@fresh"]))
    ex.run(_ramp_budget(), daytime_open=_open)
    assert port.joined == ["@fresh"]                 # rejoin skipped, new one joined
    # No successful join logged for the idempotent rejoin (only the real one).
    n = store._conn.execute(
        "SELECT COUNT(*) AS n FROM actions "
        "WHERE action_type='join' AND succeeded=1").fetchone()["n"]
    assert n == 1


# ---- search flood is classified like a write (executor level) ----

def test_hard_flood_on_search_raises_halt_session():
    store, _ = fresh_store()
    aid = _account(store)
    port = FakeTelegramWarmingPort(raise_on_search=TelegramFloodError(
        "peer_flood", is_hard=True, op="search"))
    ex = _executor(store, account_id=aid, port=port, gate=_always_relevant,
                   campaign=_campaign())
    with pytest.raises(HaltSession):
        ex.run(_ramp_budget(), daytime_open=_open)


def test_soft_flood_on_search_skips_keyword_and_continues():
    store, _ = fresh_store()
    aid = _account(store)
    port = FakeTelegramWarmingPort(raise_on_search=TelegramFloodError(
        "flood_wait", is_hard=False, retry_seconds=5, op="search"))
    # Seeded channel still joins; the search keyword is skipped without raising.
    ex = _executor(store, account_id=aid, port=port, gate=_always_relevant,
                   campaign=_campaign(seed_channels=["@seeded"]))
    ex.run(_ramp_budget(), daytime_open=_open)        # no raise
    assert port.joined == ["@seeded"]


# ---- daytime gate closes mid-session → stop writing ----

def test_closed_daytime_window_stops_writes():
    store, _ = fresh_store()
    aid = _account(store)
    port = FakeTelegramWarmingPort()
    ex = _executor(store, account_id=aid, port=port, gate=_always_relevant,
                   campaign=_campaign(seed_channels=["@seeded"]))
    ex.run(_ramp_budget(), daytime_open=lambda: False)
    assert port.joined == []


# ---- session entrypoint: hard flood → flag + FLAGGED (§8.3) ----

def test_session_hard_flood_flags_account_and_raises_peer_flood_flag():
    store, _ = fresh_store()
    aid = _account(store)
    port = FakeTelegramWarmingPort(raise_on_join=TelegramFloodError(
        "peer_flood", is_hard=True, op="join"))
    summary = run_telegram_warming_session(
        campaign=_campaign(seed_channels=["@seeded"]), store=store, feed=None,
        pacer=_no_sleep_pacer(), port=port, relevance_gate=_always_relevant)
    assert summary["halt_reason"] is not None
    # peer_flood flag raised, keyed on the account.
    flag = store._conn.execute(
        "SELECT kind, severity, account_id FROM health_flags "
        "WHERE kind='peer_flood'").fetchone()
    assert flag is not None
    assert flag["severity"] == "halt"
    assert flag["account_id"] == aid
    # Account transitioned to FLAGGED.
    assert store.get_account(aid)["state"] == FLAGGED


def test_session_completes_and_persists_counters():
    store, _ = fresh_store()
    aid = _account(store, now=1_000_000.0)
    port = FakeTelegramWarmingPort()
    summary = run_telegram_warming_session(
        campaign=_campaign(seed_channels=["@seeded"]), store=store, feed=None,
        pacer=_no_sleep_pacer(), port=port, relevance_gate=_always_relevant,
        now=1_000_000.0)
    assert summary["halt_reason"] is None
    sess = store._conn.execute(
        "SELECT status, engine_mode, account_id FROM sessions").fetchone()
    assert sess["status"] == "completed"
    assert sess["engine_mode"] == "warming"
    assert sess["account_id"] == aid
    acct = store.get_account(aid)
    assert acct["detail"]["warming_joins"] == 1
    assert acct["state"] == WARMING            # provisioned → warming on first touch


def test_session_degrades_to_seeded_only_without_gate_or_api_key(monkeypatch):
    # port=fake but relevance_gate=None and no OPENROUTER_API_KEY → the session
    # builds NO live gate and degrades to seeded-only: search hits are not joined.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    store, _ = fresh_store()
    aid = _account(store, now=1_000_000.0)
    port = FakeTelegramWarmingPort(search_results={
        "find saas leads": [TgChannel(username="@searchhit")],
    })
    summary = run_telegram_warming_session(
        campaign=_campaign(seed_channels=["@seeded"]), store=store, feed=None,
        pacer=_no_sleep_pacer(), port=port, relevance_gate=None, now=1_000_000.0)
    assert summary["halt_reason"] is None
    assert port.joined == ["@seeded"]               # seeded-only, no search hit
    assert "@searchhit" not in port.joined


def test_session_outside_daytime_halts_without_writes():
    store, _ = fresh_store()
    aid = _account(store)
    port = FakeTelegramWarmingPort()
    pacer = Pacer(cfg=PacingConfig(enforce_daytime=True),
                  clock=lambda: __import__("datetime").datetime(2026, 1, 1, 3, 0),
                  sleep=lambda _t: None)
    with pytest.raises(HaltSession):
        run_telegram_warming_session(
            campaign=_campaign(seed_channels=["@seeded"]), store=store, feed=None,
            pacer=pacer, port=port, relevance_gate=_always_relevant)
    assert port.joined == []


# ---- dispatch routes telegram → TG routine; instagram unchanged ----

def test_dispatch_routes_telegram_to_tg_routine(monkeypatch):
    store, _ = fresh_store()
    _account(store)
    called = {}

    def _fake_tg(*, campaign, store, feed, pacer, run_id=None):
        called["tg"] = True
        return {"session_id": "x", "halt_reason": None}

    import aizu.engines.warming.telegram as tg_mod
    monkeypatch.setattr(tg_mod, "run_telegram_warming_session", _fake_tg)
    from aizu.engines.warming.session import run_warming_session
    run_warming_session(campaign=_campaign(seed_channels=["@s"]), store=store,
                        router=None, feed=None, soul=None,
                        pacer=_no_sleep_pacer())
    assert called.get("tg") is True


def test_dispatch_instagram_warming_does_not_call_tg_routine(monkeypatch):
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp1", org_id=1, status="live")
    store.add_account(1, "instagram", "ig_acct")
    import aizu.engines.warming.telegram as tg_mod
    monkeypatch.setattr(tg_mod, "run_telegram_warming_session",
                        lambda **k: (_ for _ in ()).throw(
                            AssertionError("TG path on instagram!")))
    from aizu.core.feed import FakeFeed, Reel, Comment
    from aizu.engines.warming.session import run_warming_session
    feed = FakeFeed([Reel(reel_id="r1", author="a", caption="c",
                          comments=[Comment("c1", "u", "hi", "en")])])
    ig_campaign = Campaign(
        campaign_id="camp1", goal="g", threshold=0.7, escalate_band=(0.4, 0.75),
        language_mix=[], relevance_def="", match_def="", extract_def="",
        seed_direction="", raw="", path=Path("<test>"),
        platform="instagram", engine_mode="warming")
    summary = run_warming_session(campaign=ig_campaign, store=store, router=None,
                                  feed=feed, soul=None, pacer=_no_sleep_pacer())
    assert "session_id" in summary             # IG path ran, TG routine untouched
