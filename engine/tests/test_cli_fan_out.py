"""Phase 2 — CLI channel fan-out (`_run_session_loop` over a multi-platform campaign).

`_run_one` is faked so no real session is touched; we assert only the fan-out's
control flow: aggregation, the CDP-poison rule, the daytime break, the API-halt
continue, and per-channel error isolation. The fake switches on the SYNTHETIC
campaign's `platform` (set by `_campaign_with_channel` for each channel)."""
import argparse

import reelradar.cli as cli
from reelradar.core.config import campaign_from_brief


def _args(**kw):
    base = {"dry_run": False, "target_leads": None, "duration_minutes": None}
    base.update(kw)
    return argparse.Namespace(**base)


def _multi(*platforms):
    """A campaign with one channel per platform (>=2 → real fan-out)."""
    return campaign_from_brief("c", {"channels": [{"platform": p} for p in platforms]})


def _loop(args, campaign):
    return cli._run_session_loop(campaign=campaign, store=None, soul=None, args=args)


def _fake_run_one(by_platform):
    """Return a fake `_run_one` that yields `by_platform[platform]`, raising it if
    the mapped value is an Exception (to exercise per-channel error isolation)."""
    def fake(*, campaign, lead_target=None, **_kw):
        result = by_platform[campaign.platform]
        if isinstance(result, Exception):
            raise result
        return dict(result)
    return fake


def test_two_channels_aggregate_matches_and_sessions(monkeypatch):
    monkeypatch.setattr(cli, "_run_one", _fake_run_one({
        "instagram": {"matches": 3, "spend_usd": 0.10},
        "youtube": {"matches": 2, "spend_usd": 0.0},
    }))
    summary = _loop(_args(), _multi("instagram", "youtube"))
    assert summary["matches"] == 5
    assert summary["sessions"] == 2
    assert set(summary["per_platform"]) == {"instagram", "youtube"}
    assert summary["spend_usd"] == 0.10


def test_legacy_single_platform_has_no_per_platform_key(monkeypatch):
    # channels == () → the campaign runs as one channel, exactly like before.
    monkeypatch.setattr(cli, "_run_one", _fake_run_one(
        {"instagram": {"matches": 4, "spend_usd": 0.0}}))
    c = campaign_from_brief("c", {"platform": "instagram"})
    assert c.channels == ()
    summary = _loop(_args(), c)
    assert summary["matches"] == 4
    assert "per_platform" not in summary


def test_action_block_poisons_remaining_cdp_only(monkeypatch):
    # instagram (CDP) action-blocks → poisons the browser: x (CDP) is skipped, but
    # youtube (API) still runs.
    monkeypatch.setattr(cli, "_run_one", _fake_run_one({
        "instagram": {"matches": 1, "spend_usd": 0.0,
                      "halt_reason": "action-block", "halt_kind": "action_block"},
        "youtube": {"matches": 2, "spend_usd": 0.0},
        "x": {"matches": 99, "spend_usd": 0.0},   # must never be called (skipped)
    }))
    summary = _loop(_args(), _multi("instagram", "x", "youtube"))
    assert summary["halt_kind"] == "action_block"
    assert summary["per_platform"]["x"] == {"skipped": "cdp_poisoned"}
    assert summary["per_platform"]["youtube"]["matches"] == 2
    assert summary["matches"] == 1 + 2                 # x's 99 never counted


def test_daytime_halt_ends_run_without_poisoning_api_peers(monkeypatch):
    # youtube (API) runs first, then instagram hits the daytime gate → the run ends
    # cleanly. Nothing is marked cdp_poisoned; the API peer's leads are kept.
    monkeypatch.setattr(cli, "_run_one", _fake_run_one({
        "youtube": {"matches": 2, "spend_usd": 0.0},
        "instagram": {"matches": 1, "spend_usd": 0.0,
                      "halt_reason": "outside daytime window", "halt_kind": "daytime"},
    }))
    summary = _loop(_args(), _multi("youtube", "instagram"))
    assert summary["halt_kind"] == "daytime"
    assert summary["matches"] == 3                     # youtube + instagram counted
    assert "youtube" in summary["per_platform"]
    assert all(v != {"skipped": "cdp_poisoned"}
               for v in summary["per_platform"].values())


def test_api_halt_does_not_poison_cdp_peers(monkeypatch):
    # youtube hits an API rate-limit (halt_kind None) → recorded, NOT poison; the
    # instagram (CDP) channel after it still runs.
    monkeypatch.setattr(cli, "_run_one", _fake_run_one({
        "youtube": {"matches": 0, "spend_usd": 0.0,
                    "halt_reason": "youtube quota", "halt_kind": None},
        "instagram": {"matches": 4, "spend_usd": 0.0},
    }))
    summary = _loop(_args(), _multi("youtube", "instagram"))
    assert summary["halt_reason"] == "youtube quota"   # the API halt is recorded
    assert summary["per_platform"]["instagram"]["matches"] == 4
    assert summary["per_platform"]["instagram"] != {"skipped": "cdp_poisoned"}


def test_channel_runtime_error_preserves_prior_leads(monkeypatch):
    # instagram finds leads, then youtube blows up mid-fan-out: the error is folded
    # under per_platform and instagram's leads are NOT lost.
    monkeypatch.setattr(cli, "_run_one", _fake_run_one({
        "instagram": {"matches": 3, "spend_usd": 0.0},
        "youtube": RuntimeError("youtube needs reconnect"),
    }))
    summary = _loop(_args(), _multi("instagram", "youtube"))
    assert summary["matches"] == 3
    assert summary["per_platform"]["youtube"] == {"error": "youtube needs reconnect"}
    assert summary["per_platform"]["instagram"]["matches"] == 3
    # A partial failure (some channel DID run) is NOT a whole-run halt.
    assert summary["halt_reason"] is None


def test_all_channels_config_error_surfaces_as_halt(monkeypatch):
    # EVERY channel fails to even start a session (e.g. the worker is missing
    # OPENROUTER_API_KEY). The run must surface a halt_reason instead of a silent
    # "done, 0 leads" — this is what made the fleet look like it "couldn't run".
    err = "OPENROUTER_API_KEY not set (required for live run)"
    monkeypatch.setattr(cli, "_run_one", _fake_run_one({
        "instagram": RuntimeError(err),
        "youtube": RuntimeError(err),
    }))
    summary = _loop(_args(), _multi("instagram", "youtube"))
    assert summary["sessions"] == 0
    assert summary["matches"] == 0
    assert summary["halt_reason"] == err
    assert summary["halt_kind"] == "setup"
    assert summary["per_platform"]["instagram"] == {"error": err}
    assert summary["per_platform"]["youtube"] == {"error": err}


def test_one_good_channel_prevents_config_halt(monkeypatch):
    # If at least one channel ran, the run found leads (or genuinely none) — the
    # all-failed guard must NOT fire, so no spurious halt_reason.
    monkeypatch.setattr(cli, "_run_one", _fake_run_one({
        "instagram": {"matches": 0, "spend_usd": 0.0},   # ran, found nothing
        "youtube": RuntimeError("youtube needs reconnect"),
    }))
    summary = _loop(_args(), _multi("instagram", "youtube"))
    assert summary["sessions"] == 1
    assert summary["halt_reason"] is None
