"""In-process job runner seam (job_runner.py, BUILD-PLAN §2.2).

``cli._run_session_loop`` and ``resolve_campaign`` are faked so no browser/router/DB is
touched — we assert the runner's resolution, env-stamping, and cleanup contract.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from aizu.worker import job_runner
from aizu.worker.config import JobSpec, WorkerConfig
from aizu.worker.job_runner import CampaignNotFound, SoulMissing


class _Campaign:
    campaign_id = "c-acme"
    platform = "instagram"
    engine_mode = "harvest"


def _job(**kw) -> JobSpec:
    base = dict(id="job-1", org_id=1, campaign_id="c-acme", platform="instagram",
                soul_text="be a helpful agent")
    base.update(kw)
    return JobSpec(**base)


@pytest.fixture
def patched(monkeypatch):
    """resolve_campaign → a dummy campaign; capture what the loop receives."""
    captured = {}

    def fake_loop(*, campaign, store, soul, args):
        captured["soul"] = soul
        captured["run_id_env"] = os.environ.get("AIZU_RUN_ID")
        captured["pause_env"] = os.environ.get("AIZU_PAUSE_FILE")
        captured["args"] = args
        return {"matches": 2, "spend_usd": 0.0, "sessions": 1}

    monkeypatch.setattr(job_runner, "resolve_campaign", lambda *_a, **_k: _Campaign())
    monkeypatch.setattr(job_runner.cli, "_run_session_loop", fake_loop)
    return captured


def test_runs_loop_with_baked_soul_and_stamps_run_id(patched, cfg: WorkerConfig):
    store = object()
    summary = job_runner._execute_job(store, _job(), cfg=cfg)
    assert summary["matches"] == 2
    assert summary["job_id"] == "job-1"
    assert len(summary["run_id"]) == 12
    assert patched["soul"].text == "be a helpful agent"  # baked soul drop-in
    assert patched["run_id_env"] == summary["run_id"]     # env stamped during the run
    assert patched["pause_env"].endswith(f"run-{summary['run_id']}.pause")


def test_uses_the_cloud_assigned_run_id_when_present(patched, cfg: WorkerConfig):
    # A fleet job carries the run_id the cloud assigned at enqueue — the worker must run
    # under it (so the drawer + run_events sync all key on the same id), not a fresh one.
    summary = job_runner._execute_job(object(), _job(run_id="run-cloud-1"), cfg=cfg)
    assert summary["run_id"] == "run-cloud-1"
    assert patched["run_id_env"] == "run-cloud-1"


def test_env_is_restored_after_the_run(patched, cfg: WorkerConfig, monkeypatch):
    monkeypatch.setenv("AIZU_RUN_ID", "prior-run")
    job_runner._execute_job(object(), _job(), cfg=cfg)
    assert os.environ["AIZU_RUN_ID"] == "prior-run"  # not leaked


def test_pause_sentinel_is_cleaned_up(monkeypatch, cfg: WorkerConfig):
    created = {}

    def fake_loop(*, campaign, store, soul, args):
        # Simulate the engine creating the cooperative-pause sentinel mid-run.
        p = Path(os.environ["AIZU_PAUSE_FILE"])
        p.write_text("paused", encoding="utf-8")
        created["path"] = p
        return {"matches": 0, "spend_usd": 0.0}

    monkeypatch.setattr(job_runner, "resolve_campaign", lambda *_a, **_k: _Campaign())
    monkeypatch.setattr(job_runner.cli, "_run_session_loop", fake_loop)
    job_runner._execute_job(object(), _job(), cfg=cfg)
    assert not created["path"].exists()  # unlinked in finally


def test_campaign_not_found_raises(monkeypatch, cfg: WorkerConfig):
    monkeypatch.setattr(job_runner, "resolve_campaign", lambda *_a, **_k: None)
    with pytest.raises(CampaignNotFound):
        job_runner._execute_job(object(), _job(), cfg=cfg)


def test_malformed_brief_propagates_valueerror(monkeypatch, cfg: WorkerConfig):
    def boom(*_a, **_k):
        raise ValueError("bad brief")
    monkeypatch.setattr(job_runner, "resolve_campaign", boom)
    with pytest.raises(ValueError, match="bad brief"):
        job_runner._execute_job(object(), _job(), cfg=cfg)


def test_prefers_baked_campaign_brief_over_local_resolve(monkeypatch, cfg: WorkerConfig):
    # The DB/file lookup must never be consulted when a brief is baked into the job —
    # asserting on a call raises AssertionError so any accidental fallback fails loud.
    def must_not_be_called(*_a, **_k):
        raise AssertionError("must not be called when a brief is baked")
    monkeypatch.setattr(job_runner, "resolve_campaign", must_not_be_called)
    captured = {}

    def fake_loop(*, campaign, store, soul, args):
        captured["campaign"] = campaign
        return {"matches": 1, "spend_usd": 0.0}
    monkeypatch.setattr(job_runner.cli, "_run_session_loop", fake_loop)
    brief = {"platform": "instagram", "goal": "lead"}
    job = _job(campaign_brief=brief)
    summary = job_runner._execute_job(object(), job, cfg=cfg)
    assert summary["matches"] == 1
    assert captured["campaign"].campaign_id == job.campaign_id
    assert captured["campaign"].platform == "instagram"


def test_malformed_baked_brief_raises_without_touching_local_resolve(
        monkeypatch, cfg: WorkerConfig):
    def must_not_be_called(*_a, **_k):
        raise AssertionError("must not be called when a brief is baked")
    monkeypatch.setattr(job_runner, "resolve_campaign", must_not_be_called)
    job = _job(campaign_brief={"platform": "not-a-real-platform"})
    with pytest.raises(ValueError):
        job_runner._execute_job(object(), job, cfg=cfg)


def test_legacy_job_with_no_baked_brief_still_uses_local_resolve(patched, cfg: WorkerConfig):
    # job.campaign_brief defaults to None (_job() never sets it) — the module-patched
    # resolve_campaign must still be the one consulted, proving zero behavior change
    # for an already-queued/legacy job.
    summary = job_runner._execute_job(object(), _job(), cfg=cfg)
    assert summary["matches"] == 2  # patched fixture's fake_loop return value


def test_baked_brief_lets_a_job_run_on_a_box_with_no_local_campaign_row(
        monkeypatch, tmp_path, cfg: WorkerConfig):
    """The literal known-issues.md B4 regression: a REMOTE worker whose local SQLite
    DB has no campaign_briefs row for this campaign_id (and no campaign.md on disk)
    must still be able to run the job, off the baked brief alone. resolve_campaign is
    deliberately NOT monkeypatched — this exercises the real DB-miss/file-miss path."""
    from aizu.core.config import campaign_to_brief, load_campaign
    from aizu.core.store import Store

    store = Store(tmp_path / "empty-remote.db")
    try:
        real_campaign = load_campaign(Path(__file__).resolve().parents[2]
                                      / "config" / "campaign.md")
        brief = campaign_to_brief(real_campaign)
        # cfg.cfg_dir (from the `cfg` fixture) is an empty tmp dir with no campaign.md.
        job = _job(campaign_id=real_campaign.campaign_id, platform=real_campaign.platform,
                   campaign_brief=brief)

        captured = {}

        def fake_loop(*, campaign, store, soul, args):
            captured["campaign"] = campaign
            return {"matches": 0, "spend_usd": 0.0}
        monkeypatch.setattr(job_runner.cli, "_run_session_loop", fake_loop)

        summary = job_runner._execute_job(store, job, cfg=cfg)
        assert "run_id" in summary
        assert captured["campaign"].campaign_id == real_campaign.campaign_id
    finally:
        store.close()


def test_soul_missing_raises_when_no_baked_and_no_file(monkeypatch, cfg: WorkerConfig):
    monkeypatch.setattr(job_runner, "resolve_campaign", lambda *_a, **_k: _Campaign())
    with pytest.raises(SoulMissing):
        job_runner._execute_job(object(), _job(soul_text=None), cfg=cfg)


def test_local_soul_md_used_when_not_baked(patched, cfg: WorkerConfig):
    (cfg.cfg_dir / "soul.md").write_text("local soul file", encoding="utf-8")
    job_runner._execute_job(object(), _job(soul_text=None), cfg=cfg)
    assert patched["soul"].text == "local soul file"


def test_pause_and_env_restored_when_loop_raises(monkeypatch, cfg: WorkerConfig):
    """The finally path must unlink the pause sentinel AND restore env even when the
    engine run raises (BUILD-PLAN §2.7; code review env-restore-on-exception gap)."""
    monkeypatch.setenv("AIZU_RUN_ID", "prior-run")
    created = {}

    def boom(*, campaign, store, soul, args):
        p = Path(os.environ["AIZU_PAUSE_FILE"])
        p.write_text("paused", encoding="utf-8")
        created["path"] = p
        raise RuntimeError("engine boom")

    monkeypatch.setattr(job_runner, "resolve_campaign", lambda *_a, **_k: _Campaign())
    monkeypatch.setattr(job_runner.cli, "_run_session_loop", boom)
    with pytest.raises(RuntimeError, match="engine boom"):
        job_runner._execute_job(object(), _job(), cfg=cfg)
    assert not created["path"].exists()                    # unlinked despite the raise
    assert os.environ["AIZU_RUN_ID"] == "prior-run"   # env restored


def test_run_env_guard_rejects_reentry(monkeypatch, cfg: WorkerConfig):
    """A second in-process run while one holds the env guard fails loudly, so a future
    concurrency mistake is caught at test time, not silently in prod (code review M-C)."""
    def reenter(*, campaign, store, soul, args):
        with pytest.raises(RuntimeError, match="one at a time"):
            job_runner._execute_job(object(), _job(id="job-2"), cfg=cfg)
        return {"matches": 0, "spend_usd": 0.0}

    monkeypatch.setattr(job_runner, "resolve_campaign", lambda *_a, **_k: _Campaign())
    monkeypatch.setattr(job_runner.cli, "_run_session_loop", reenter)
    job_runner._execute_job(object(), _job(), cfg=cfg)


def test_dispatch_seam_signature_is_stable():
    """Regression gate: the engine seam the worker drives must keep the keyword
    params the worker relies on (BUILD-PLAN §2.7 signature lock)."""
    import inspect

    from aizu import dispatch
    params = inspect.signature(dispatch.run_engine_session).parameters
    for name in ("campaign", "store", "router", "feed", "soul", "pacer",
                 "run_id", "lead_target", "engine_mode"):
        assert name in params, f"dispatch.run_engine_session lost param {name!r}"


def test_orphan_pause_sweep_removes_only_stale(cfg: WorkerConfig):
    import time
    fresh = cfg.state_dir / "run-aaaa.pause"
    stale = cfg.state_dir / "run-bbbb.pause"
    fresh.write_text("x", encoding="utf-8")
    stale.write_text("x", encoding="utf-8")
    old = time.time() - 10_000
    os.utime(stale, (old, old))

    swept = job_runner.sweep_orphan_pause_files(cfg.state_dir, max_age_sec=500)

    assert swept == 1
    assert fresh.exists() and not stale.exists()


# ----- Phase 6: supervisor rendezvous-file sweep + per-run log path -------------

def test_orphan_job_file_sweep_removes_only_stale(cfg: WorkerConfig):
    import time
    from aizu.worker import job_runner as jr
    specs = cfg.state_dir / "job-specs"
    results = cfg.state_dir / "job-results"
    logs = cfg.state_dir / "logs"
    for d in (specs, results, logs):
        d.mkdir(parents=True, exist_ok=True)
    fresh = specs / "job-fresh.json"
    stale_spec = specs / "job-stale.json"
    stale_result = results / "job-stale.json"
    stale_log = logs / "job-stale.log"
    for p in (fresh, stale_spec, stale_result, stale_log):
        p.write_text("{}", encoding="utf-8")
    old = time.time() - 10 * 24 * 60 * 60
    for p in (stale_spec, stale_result, stale_log):
        os.utime(p, (old, old))

    swept = jr.sweep_orphan_job_files(cfg.state_dir, max_age_sec=7 * 24 * 60 * 60)

    assert swept == 3
    assert fresh.exists()
    assert not stale_spec.exists() and not stale_result.exists() and not stale_log.exists()


def test_run_log_path_matches_configure_logging_convention():
    from aizu.core.logsetup import run_log_path
    p = run_log_path("run-xyz", log_file="/var/state/logs/aizu.log")
    assert p == Path("/var/state/logs/run-run-xyz.log")


def test_run_log_path_none_when_logging_disabled():
    from aizu.core.logsetup import run_log_path
    assert run_log_path("run-1", log_file="off") is None


def test_sweep_removes_orphan_json_tmp_files(cfg: WorkerConfig):
    import time
    from aizu.worker import job_runner as jr
    specs = cfg.state_dir / "job-specs"
    specs.mkdir(parents=True, exist_ok=True)
    tmp = specs / "job-x.json.tmp"          # an atomic-write temp a hard kill left behind
    tmp.write_text("{}", encoding="utf-8")
    old = time.time() - 10 * 24 * 60 * 60
    os.utime(tmp, (old, old))
    swept = jr.sweep_orphan_job_files(cfg.state_dir, max_age_sec=7 * 24 * 60 * 60)
    assert swept == 1 and not tmp.exists()


# ----- B9: the effective spend cap -----------------------------------------------

class _SpendStore:
    """Just enough store for the cap math: a campaign's box-LOCAL spend total."""

    def __init__(self, total: float = 0.0):
        self._total = total

    def total_spend(self, _campaign_id: str) -> float:
        return self._total


@pytest.mark.parametrize("local, prior, cap, expected", [
    # Fresh box, campaign already burned $18 elsewhere → only $2 of headroom left.
    (0.0, 18.0, 20.0, 2.0),
    # This box's own $18 was already rolled up to the cloud → still $20, NOT $38:
    # `max(prior, local)` counts the shared money once instead of summing it.
    (18.0, 18.0, 20.0, 20.0),
    # This box holds $18 the cloud never learned about (an attempt that died before its
    # ack/nack, re-pinned here by reclaim) → the local floor still counts it.
    (18.0, 0.0, 20.0, 20.0),
    # Already over budget → exactly the local total, and the guard is `>=`, so call one
    # is blocked. The degenerate local==0 case yields 0.0, which _spend_guard treats as a
    # real cap (it only short-circuits on `spend_cap_usd is None`).
    (0.0, 25.0, 20.0, 0.0),
    (5.0, 25.0, 20.0, 5.0),
    # No cloud number at all (legacy job / older server) → today's behaviour.
    (5.0, None, 20.0, 20.0),
    (0.0, None, 20.0, 20.0),
])
def test_effective_spend_cap_table(local, prior, cap, expected):
    assert job_runner._effective_spend_cap(local, prior, cap) == pytest.approx(expected)


def test_effective_spend_cap_stays_none_when_uncapped():
    assert job_runner._effective_spend_cap(3.0, 18.0, None) is None


def test_execute_job_hands_the_adjusted_cap_to_the_run(patched, cfg: WorkerConfig):
    # cfg.spend_cap is the box's AIZU_SPEND_CAP (20.0 by default); the campaign already
    # spent $18 on another box, so this run gets $2 — not a fresh $20 (B9).
    job_runner._execute_job(_SpendStore(0.0), _job(prior_spend_usd=18.0), cfg=cfg)
    assert patched["args"].spend_cap == pytest.approx(2.0)


def test_execute_job_keeps_the_box_cap_without_a_cloud_number(patched,
                                                              cfg: WorkerConfig):
    job_runner._execute_job(_SpendStore(0.0), _job(), cfg=cfg)
    assert patched["args"].spend_cap == pytest.approx(cfg.spend_cap)


def test_execute_job_survives_a_local_spend_read_failure(patched, cfg: WorkerConfig):
    class _Broken:
        def total_spend(self, _cid):
            raise RuntimeError("db locked")

    # A bookkeeping hiccup must not fail a runnable job — it degrades to local 0.0.
    job_runner._execute_job(_Broken(), _job(prior_spend_usd=18.0), cfg=cfg)
    assert patched["args"].spend_cap == pytest.approx(2.0)
