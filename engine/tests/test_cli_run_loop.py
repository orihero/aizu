"""Tests for cli._run_session_loop — the multi-session loop that stops on the lead
target, the wall-clock safety cap, or a halt. `_run_one` is faked so no real session
(browser, router, store) is touched; we only assert the loop's control flow.

Also covers _run_one's feed teardown: each session must close its feed so the next
session in a multi-session run can attach a fresh Playwright driver."""
import argparse
import os
import tempfile

import pytest

import aizu.cli as cli
from aizu.core.config import ChannelSpec, campaign_from_brief
from aizu.core.store import COOLDOWN_MAX_SECONDS, Store


def _args(**kw):
    base = {"dry_run": False, "target_leads": None, "duration_minutes": None}
    base.update(kw)
    return argparse.Namespace(**base)


class _DummyCampaign:
    campaign_id = "c-test"
    platform = "instagram"
    channels = ()          # legacy single-platform → no fan-out


class _YtCampaign:
    campaign_id = "yt"
    platform = "youtube"
    channels = ()


def _loop(args, campaign=None):
    # Instagram (algorithmic feed) is the only platform that loops back-to-back to a
    # target, so the loop tests run against an Instagram campaign by default.
    return cli._run_session_loop(campaign=campaign or _DummyCampaign(),
                                 store=None, soul=None, args=args)


def test_single_session_when_no_flags(monkeypatch):
    calls = []

    def fake_run_one(*, lead_target=None, **_kw):
        calls.append(lead_target)
        return {"matches": 3, "spend_usd": 0.0}

    monkeypatch.setattr(cli, "_run_one", fake_run_one)
    summary = _loop(_args())
    assert len(calls) == 1
    assert summary["matches"] == 3


def test_dry_run_does_one_session_even_with_target(monkeypatch):
    calls = []

    def fake_run_one(*, lead_target=None, **_kw):
        calls.append(lead_target)
        return {"matches": 1, "spend_usd": 0.0}

    monkeypatch.setattr(cli, "_run_one", fake_run_one)
    _loop(_args(dry_run=True, target_leads=10))
    assert len(calls) == 1   # the fake feed is instant; looping would spin


def test_loop_stops_when_target_reached(monkeypatch):
    calls = []

    def fake_run_one(*, lead_target=None, **_kw):
        calls.append(lead_target)
        return {"matches": 4, "spend_usd": 0.0}

    monkeypatch.setattr(cli, "_run_one", fake_run_one)
    summary = _loop(_args(target_leads=10))
    # 4 + 4 + 4 = 12 >= 10 after three sessions; each is asked only for the shortfall.
    assert calls == [10, 6, 2]
    assert summary["sessions"] == 3
    assert summary["matches"] == 12
    assert summary["target_leads"] == 10


def test_time_cap_stops_unreachable_target(monkeypatch):
    # monotonic(): deadline calc → 0, first while-check → 0 (proceed), next → past cap.
    seq = iter([0.0, 0.0, 100.0])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(seq))
    calls = []

    def fake_run_one(*, lead_target=None, **_kw):
        calls.append(lead_target)
        return {"matches": 0, "spend_usd": 0.0}   # target can never be reached

    monkeypatch.setattr(cli, "_run_one", fake_run_one)
    summary = _loop(_args(target_leads=1000, duration_minutes=1))
    assert len(calls) == 2   # first session + one extra, then the cap fires
    assert summary["matches"] == 0


def test_target_run_keeps_looping_with_no_time_cap(monkeypatch):
    """The panel now launches with a lead target and NO operator-chosen time cap, so the
    loop's only stop is the target itself. Without a duration the deadline is None and
    the sessions keep coming until the leads are there."""
    calls = []

    def fake_run_one(*, lead_target=None, **_kw):
        calls.append(lead_target)
        return {"matches": 1, "spend_usd": 0.0}

    monkeypatch.setattr(cli, "_run_one", fake_run_one)
    summary = _loop(_args(target_leads=5, duration_minutes=None))
    assert calls == [5, 4, 3, 2, 1]           # five sessions for five leads
    assert summary["matches"] == 5
    assert summary["stop_reason"] == "target_met"


def test_time_cap_stop_is_named_in_the_summary(monkeypatch):
    seq = iter([0.0, 0.0, 100.0])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(seq))

    def fake_run_one(*, lead_target=None, **_kw):
        return {"matches": 0, "spend_usd": 0.0}

    monkeypatch.setattr(cli, "_run_one", fake_run_one)
    summary = _loop(_args(target_leads=1000, duration_minutes=1))
    # A run that ends on the wall clock rather than the target must say so — the lead
    # count alone cannot tell "the cap fired" from "there was nothing to find".
    assert summary["stop_reason"] == "time_cap"


def test_single_pass_platform_shortfall_is_named(monkeypatch):
    def fake_run_one(*, lead_target=None, **_kw):
        return {"matches": 2, "spend_usd": 0.0}

    monkeypatch.setattr(cli, "_run_one", fake_run_one)
    summary = _loop(_args(target_leads=50), _YtCampaign())
    assert summary["matches"] == 2
    assert summary["stop_reason"] == "single_pass"


def test_halt_breaks_the_loop(monkeypatch):
    results = iter([
        {"matches": 1, "spend_usd": 0.0},
        {"matches": 1, "spend_usd": 0.0, "halt_reason": "outside daytime window"},
    ])

    def fake_run_one(*, lead_target=None, **_kw):
        return next(results)

    monkeypatch.setattr(cli, "_run_one", fake_run_one)
    summary = _loop(_args(target_leads=100))
    assert summary["halt_reason"] == "outside daytime window"
    assert summary["sessions"] == 2


def test_first_session_halt_returns_immediately(monkeypatch):
    calls = []

    def fake_run_one(*, lead_target=None, **_kw):
        calls.append(lead_target)
        return {"matches": 0, "halt_reason": "outside daytime window"}

    monkeypatch.setattr(cli, "_run_one", fake_run_one)
    summary = _loop(_args(target_leads=50))
    assert len(calls) == 1
    assert summary["halt_reason"] == "outside daytime window"


def test_run_one_closes_feed_after_session(monkeypatch):
    """The fix for the loop crash: _run_one must close the feed when the session ends
    so the next session can start a fresh Playwright driver in the same process."""
    closed = []

    class SpyFeed:
        def close(self):
            closed.append(True)

    monkeypatch.setattr(cli, "_build_run_io", lambda *_a, **_k: (object(), SpyFeed(), object()))
    # _run_one now drives the engine via dispatch; patch that seam (not a Session class).
    monkeypatch.setattr(cli.dispatch, "run_engine_session",
                        lambda **_k: {"session_id": "s1", "matches": 0, "spend_usd": 0.0})
    cli._run_one(campaign=_DummyCampaign(), store=None, soul=None,
                 dry_run=False, args=_args())
    assert closed == [True]


def test_run_one_closes_feed_even_on_error(monkeypatch):
    closed = []

    class SpyFeed:
        def close(self):
            closed.append(True)

    def _boom(**_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "_build_run_io", lambda *_a, **_k: (object(), SpyFeed(), object()))
    monkeypatch.setattr(cli.dispatch, "run_engine_session", _boom)
    with pytest.raises(RuntimeError):
        cli._run_one(campaign=_DummyCampaign(), store=None, soul=None,
                     dry_run=False, args=_args())
    assert closed == [True]   # teardown still ran despite the failure


def _cooldown_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path)


def test_run_one_short_circuits_while_cooling_down(monkeypatch):
    """Gap #1: a prior SOFT halt (action_block/canary) escalated a cooldown for
    this (campaign, platform) via Store.record_soft_halt. Recorded "just now" its
    cooldown_until is ~15 minutes out, so _run_one must short-circuit BEFORE ever
    touching the browser/account again — no resolve_flag, no human step needed."""
    store = _cooldown_store()
    try:
        store.record_soft_halt("c-test", "instagram", "action_block")
        built = []
        monkeypatch.setattr(cli, "_build_run_io",
                            lambda *a, **k: built.append(1) or (object(), object(), object()))
        summary = cli._run_one(campaign=_DummyCampaign(), store=store, soul=None,
                               dry_run=False, args=_args())
        assert "cooling down" in summary["halt_reason"]
        assert built == []                 # never reached _build_run_io
    finally:
        store.close()


def test_run_one_proceeds_normally_once_cooldown_elapses(monkeypatch):
    """Once cooldown_until is in the past, the SAME campaign resumes automatically
    on the next attempt — the whole point of the self-healing cooldown."""
    store = _cooldown_store()
    try:
        # Escalate with a `now` far enough in the past that even the capped 6h
        # backoff has already elapsed relative to the real wall clock.
        import time
        stale_now = time.time() - COOLDOWN_MAX_SECONDS - 100
        store.record_soft_halt("c-test", "instagram", "action_block", now=stale_now)

        class SpyFeed:
            def close(self):
                pass

        monkeypatch.setattr(cli, "_build_run_io",
                            lambda *a, **k: (object(), SpyFeed(), object()))
        monkeypatch.setattr(cli.dispatch, "run_engine_session",
                            lambda **_k: {"session_id": "s1", "matches": 0, "spend_usd": 0.0})
        summary = cli._run_one(campaign=_DummyCampaign(), store=store, soul=None,
                               dry_run=False, args=_args())
        assert summary.get("halt_reason") is None
        assert summary["session_id"] == "s1"
    finally:
        store.close()


def test_run_one_dry_run_bypasses_cooldown_gate(monkeypatch):
    """Dry runs use the fake feed (no real anti-bot signal ever fires), so a
    cooldown recorded for a live run must never block a dry run of the same
    campaign — mirrors the warming kill-switch's own dry-run bypass."""
    store = _cooldown_store()
    try:
        store.record_soft_halt("c-test", "instagram", "action_block")
        built = []
        monkeypatch.setattr(cli, "_build_run_io",
                            lambda *a, **k: built.append(1) or (object(), object(), object()))
        monkeypatch.setattr(cli.dispatch, "run_engine_session",
                            lambda **_k: {"session_id": "s1", "matches": 0, "spend_usd": 0.0})
        cli._run_one(campaign=_DummyCampaign(), store=store, soul=None,
                     dry_run=True, args=_args(dry_run=True))
        assert built == [1]                 # the cooldown never gated a dry run
    finally:
        store.close()


def test_deterministic_platform_does_single_pass(monkeypatch):
    """YouTube/Telegram are deterministic: re-running re-fetches the same items at full
    API cost for no new leads, so a target does ONE pass — never the back-to-back loop."""
    calls = []

    def fake_run_one(*, lead_target=None, **_kw):
        calls.append(lead_target)
        return {"matches": 2, "spend_usd": 0.0}   # target (100) never met → would loop

    monkeypatch.setattr(cli, "_run_one", fake_run_one)
    summary = _loop(_args(target_leads=100), campaign=_YtCampaign())
    assert len(calls) == 1                          # single pass, no re-search loop
    assert summary["matches"] == 2


# --- Multi-platform channel helpers (Phase 1) --------------------------------


def test_effective_channels_legacy_scalar_produces_single_spec():
    # channels == () → one synthetic ChannelSpec from the flat scalar seeds.
    c = campaign_from_brief("c", {"platform": "instagram", "seed_hashtags": ["a"]})
    assert c.channels == ()
    chans = cli._effective_channels(c)
    assert chans == [ChannelSpec(platform="instagram", seed_hashtags=("a",),
                                 include_home_feed=False)]


def test_effective_channels_multi_channel_returns_stored_list():
    c = campaign_from_brief("c", {"channels": [
        {"platform": "instagram"}, {"platform": "youtube"}]})
    assert [ch.platform for ch in cli._effective_channels(c)] == ["instagram", "youtube"]


def test_effective_channels_returns_new_list_each_call():
    c = campaign_from_brief("c", {"channels": [
        {"platform": "instagram"}, {"platform": "youtube"}]})
    a, b = cli._effective_channels(c), cli._effective_channels(c)
    assert a == b and a is not b      # value-equal but a fresh list each call


def test_campaign_with_channel_seed_lists_are_not_aliased():
    c = campaign_from_brief("c", {"platform": "instagram"})
    ch = ChannelSpec(platform="youtube", seed_hashtags=("a",))
    one = cli._campaign_with_channel(c, ch)
    two = cli._campaign_with_channel(c, ch)
    one.seed_hashtags.append("mutated")
    assert two.seed_hashtags == ["a"]            # the two copies share no list
    assert one.platform == "youtube"


def test_campaign_with_channel_knobs_are_not_aliased():
    c = campaign_from_brief("c", {"platform": "instagram", "goal": "lead"})
    out = cli._campaign_with_channel(c, ChannelSpec(platform="x"))
    assert out.knobs == c.knobs and out.knobs is not c.knobs


def test_campaign_with_channel_channels_cleared():
    c = campaign_from_brief("c", {"channels": [
        {"platform": "instagram"}, {"platform": "youtube"}]})
    out = cli._campaign_with_channel(c, c.channels[0])
    assert out.channels == ()                    # the copy never re-fans-out


def test_campaign_with_channel_does_not_mutate_base():
    c = campaign_from_brief("c", {"channels": [
        {"platform": "instagram"}, {"platform": "youtube"}]})
    cli._campaign_with_channel(c, ChannelSpec(platform="x", seed_hashtags=("z",)))
    assert len(c.channels) == 2 and c.platform == "instagram"


def test_legacy_duration_only_run_still_loops(monkeypatch):
    seq = iter([0.0, 0.0, 0.0, 999.0])  # deadline(=300), two proceeds, then past cap
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(seq))
    calls = []

    def fake_run_one(*, lead_target=None, **_kw):
        calls.append(lead_target)
        return {"matches": 2, "spend_usd": 0.0}

    monkeypatch.setattr(cli, "_run_one", fake_run_one)
    summary = _loop(_args(duration_minutes=5))
    # No target → each session is unbounded by leads (lead_target stays None).
    assert calls == [None, None, None]
    assert summary["matches"] == 6
