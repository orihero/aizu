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
