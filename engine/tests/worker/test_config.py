"""JobSpec boundary validation + WorkerConfig.run_args (BUILD-PLAN §2.2, config.py)."""
from __future__ import annotations

import pytest

from reelradar.core.config import SUPPORTED_PLATFORMS
from reelradar.worker.config import JobSpec, JobSpecError, WorkerConfig


def test_from_payload_accepts_camelcase_and_coerces():
    job = JobSpec.from_payload({
        "id": "job-1", "orgId": "7", "campaignId": "c-acme", "platform": "instagram",
        "requiredAccountHandle": "acme.io", "targetLeads": "5", "durationMinutes": 30,
        "engineMode": "harvest", "soulText": "be helpful",
    })
    assert job.id == "job-1"
    assert job.org_id == 7 and isinstance(job.org_id, int)
    assert job.target_leads == 5
    assert job.soul_text == "be helpful"


def test_from_payload_parses_run_id():
    job = JobSpec.from_payload({"id": "j", "campaignId": "c", "platform": "x",
                                "runId": "run-abc123"})
    assert job.run_id == "run-abc123"
    # Absent → None (a legacy job; the worker generates one locally).
    assert JobSpec.from_payload({"id": "j", "campaignId": "c",
                                 "platform": "x"}).run_id is None


def test_from_payload_accepts_snakecase():
    job = JobSpec.from_payload({
        "job_id": "j2", "campaign_id": "c", "platform": "x"})
    assert job.id == "j2" and job.campaign_id == "c" and job.platform == "x"


def test_from_payload_defaults_engine_mode_to_harvest():
    job = JobSpec.from_payload({"id": "j", "campaignId": "c", "platform": "x"})
    assert job.engine_mode == "harvest"


@pytest.mark.parametrize("payload", [
    {"campaignId": "c", "platform": "x"},                 # no id
    {"id": "j", "platform": "x"},                          # no campaign
    {"id": "j", "campaignId": "c"},                        # no platform
    {"id": "j", "campaignId": "c", "platform": "x", "engineMode": "bogus"},
    {"id": "j", "campaignId": "c", "platform": "x", "targetLeads": "many"},
    {"id": "j", "campaignId": "c", "platform": "myspace"},   # unknown platform
    "not-an-object",
])
def test_from_payload_rejects_bad_input(payload):
    with pytest.raises(JobSpecError):
        JobSpec.from_payload(payload)


def test_lock_key_is_per_account():
    job = JobSpec(id="j", org_id=7, campaign_id="c", platform="instagram",
                  required_account_handle="acme.io")
    assert job.lock_key() == "7-instagram-acme.io"


def test_lock_key_defaults_account_when_absent():
    job = JobSpec(id="j", org_id=7, campaign_id="c", platform="x")
    assert job.lock_key() == "7-x-_default"


def test_run_args_builds_fresh_namespace_each_call(cfg: WorkerConfig):
    job = JobSpec(id="j", org_id=1, campaign_id="c", platform="instagram",
                  target_leads=3, duration_minutes=15)
    a, b = cfg.run_args(job), cfg.run_args(job)
    assert a is not b
    assert a.dry_run is False
    assert a.target_leads == 3
    assert a.duration_minutes == 15
    assert a.engine_mode == "harvest"
    assert a.cdp_url == cfg.cdp_url


def test_run_args_applies_duration_safety_cap_when_unbounded(cfg: WorkerConfig):
    job = JobSpec(id="j", org_id=1, campaign_id="c", platform="instagram")
    args = cfg.run_args(job)
    assert args.duration_minutes == cfg.max_job_minutes  # never unbounded


def test_from_env_requires_dispatch_url(monkeypatch):
    monkeypatch.delenv("REELRADAR_DISPATCH_URL", raising=False)
    with pytest.raises(ValueError, match="REELRADAR_DISPATCH_URL"):
        WorkerConfig.from_env()


def _base_env(monkeypatch):
    monkeypatch.setenv("REELRADAR_DISPATCH_URL", "http://127.0.0.1:8799")
    monkeypatch.delenv("REELRADAR_WORKER_CAPABILITIES", raising=False)
    monkeypatch.delenv("REELRADAR_WORKER_PLATFORMS", raising=False)


def test_from_env_no_capabilities_by_default(monkeypatch):
    _base_env(monkeypatch)
    assert WorkerConfig.from_env().capabilities == ()


def test_from_env_platforms_all_declares_every_supported_platform(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("REELRADAR_WORKER_PLATFORMS", "all")
    caps = WorkerConfig.from_env().capabilities
    plats = {c[1] for c in caps}
    assert plats == set(SUPPORTED_PLATFORMS)
    assert all(c[0] is None and c[2] is None for c in caps)  # pool-wide, unpinned


def test_from_env_platforms_list_drops_unknown(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("REELRADAR_WORKER_PLATFORMS", "instagram, x , bogus")
    assert WorkerConfig.from_env().capabilities == (
        [None, "instagram", None], [None, "x", None])


def test_from_env_explicit_json_capabilities_win_and_are_sanitized(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("REELRADAR_WORKER_PLATFORMS", "all")  # ignored when JSON present
    monkeypatch.setenv(
        "REELRADAR_WORKER_CAPABILITIES",
        '[[1,"instagram",null],[null,"x","acct1"],["bad"],[null,"nope",null]]')
    assert WorkerConfig.from_env().capabilities == (
        [1, "instagram", None], [None, "x", "acct1"])


def test_from_env_malformed_capabilities_json_does_not_raise(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("REELRADAR_WORKER_CAPABILITIES", "{not json")
    assert WorkerConfig.from_env().capabilities == ()


def test_machine_id_is_stable_across_calls(cfg: WorkerConfig):
    first = cfg.machine_id
    assert first and cfg.machine_id == first  # persisted, not regenerated


def test_rejects_non_positive_duration_and_target():
    from reelradar.worker.config import JobSpec, JobSpecError
    import pytest as _pt
    base = dict(id="j", campaignId="c", platform="instagram")
    with _pt.raises(JobSpecError):
        JobSpec.from_payload({**base, "durationMinutes": 0})
    with _pt.raises(JobSpecError):
        JobSpec.from_payload({**base, "targetLeads": -5})
    # a positive value still round-trips
    js = JobSpec.from_payload({**base, "targetLeads": 25, "durationMinutes": 30})
    assert js.target_leads == 25 and js.duration_minutes == 30


def test_child_dict_round_trips():
    from reelradar.worker.config import WorkerConfig
    from pathlib import Path
    cfg = WorkerConfig(dispatch_base_url="http://x", cfg_dir=Path("cfg"),
                       db_path="d.db", state_dir=Path("st"), cdp_url="http://127.0.0.1:9333",
                       spend_cap=12.5, max_job_minutes=45)
    rebuilt = WorkerConfig.from_child_dict(cfg.to_child_dict())
    assert rebuilt.db_path == "d.db" and rebuilt.cdp_url == "http://127.0.0.1:9333"
    assert rebuilt.spend_cap == 12.5 and rebuilt.max_job_minutes == 45
