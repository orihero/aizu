"""JobSpec boundary validation + WorkerConfig.run_args (BUILD-PLAN §2.2, config.py)."""
from __future__ import annotations

import pytest

from aizu.core.config import SUPPORTED_PLATFORMS
from aizu.worker.config import JobSpec, JobSpecError, WorkerConfig


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
    monkeypatch.delenv("AIZU_DISPATCH_URL", raising=False)
    with pytest.raises(ValueError, match="AIZU_DISPATCH_URL"):
        WorkerConfig.from_env()


def _base_env(monkeypatch):
    monkeypatch.setenv("AIZU_DISPATCH_URL", "http://127.0.0.1:8799")
    monkeypatch.delenv("AIZU_WORKER_CAPABILITIES", raising=False)
    monkeypatch.delenv("AIZU_WORKER_PLATFORMS", raising=False)


def test_from_env_no_capabilities_by_default(monkeypatch):
    _base_env(monkeypatch)
    assert WorkerConfig.from_env().capabilities == ()


def test_from_env_platforms_all_declares_every_supported_platform(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("AIZU_WORKER_PLATFORMS", "all")
    caps = WorkerConfig.from_env().capabilities
    plats = {c[1] for c in caps}
    assert plats == set(SUPPORTED_PLATFORMS)
    assert all(c[0] is None and c[2] is None for c in caps)  # pool-wide, unpinned


def test_from_env_platforms_list_drops_unknown(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("AIZU_WORKER_PLATFORMS", "instagram, x , bogus")
    assert WorkerConfig.from_env().capabilities == (
        [None, "instagram", None], [None, "x", None])


def test_from_env_explicit_json_capabilities_win_and_are_sanitized(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("AIZU_WORKER_PLATFORMS", "all")  # ignored when JSON present
    monkeypatch.setenv(
        "AIZU_WORKER_CAPABILITIES",
        '[[1,"instagram",null],[null,"x","acct1"],["bad"],[null,"nope",null]]')
    assert WorkerConfig.from_env().capabilities == (
        [1, "instagram", None], [None, "x", "acct1"])


def test_from_env_malformed_capabilities_json_does_not_raise(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("AIZU_WORKER_CAPABILITIES", "{not json")
    assert WorkerConfig.from_env().capabilities == ()


def test_machine_id_is_stable_across_calls(cfg: WorkerConfig):
    first = cfg.machine_id
    assert first and cfg.machine_id == first  # persisted, not regenerated


def test_rejects_non_positive_duration_and_target():
    from aizu.worker.config import JobSpec, JobSpecError
    import pytest as _pt
    base = dict(id="j", campaignId="c", platform="instagram")
    with _pt.raises(JobSpecError):
        JobSpec.from_payload({**base, "durationMinutes": 0})
    with _pt.raises(JobSpecError):
        JobSpec.from_payload({**base, "targetLeads": -5})
    # a positive value still round-trips
    js = JobSpec.from_payload({**base, "targetLeads": 25, "durationMinutes": 30})
    assert js.target_leads == 25 and js.duration_minutes == 30


def test_from_payload_round_trips_campaign_brief():
    brief = {"platform": "instagram", "goal": "lead"}
    job = JobSpec.from_payload({
        "id": "j", "campaignId": "c", "platform": "instagram",
        "campaignBrief": brief})
    assert job.campaign_brief == brief
    # snake_case is accepted too (same pick() convention as every other field).
    job2 = JobSpec.from_payload({
        "id": "j", "campaignId": "c", "platform": "instagram",
        "campaign_brief": brief})
    assert job2.campaign_brief == brief
    # Absent → None (legacy job; the worker falls back to the box-local DB lookup).
    assert JobSpec.from_payload({
        "id": "j", "campaignId": "c", "platform": "instagram"}).campaign_brief is None


def test_from_payload_rejects_non_dict_campaign_brief():
    for bad in ("not-a-dict", ["nope"], 5):
        with pytest.raises(JobSpecError):
            JobSpec.from_payload({"id": "j", "campaignId": "c", "platform": "instagram",
                                  "campaignBrief": bad})


def test_from_payload_rejects_oversized_campaign_brief():
    from aizu.core.config import MAX_CAMPAIGN_BRIEF_BYTES
    huge = {"platform": "instagram", "seed_direction": "x" * (MAX_CAMPAIGN_BRIEF_BYTES + 1)}
    with pytest.raises(JobSpecError):
        JobSpec.from_payload({"id": "j", "campaignId": "c", "platform": "instagram",
                              "campaignBrief": huge})


def test_to_payload_omits_campaign_brief_unless_baked():
    job = JobSpec(id="j", org_id=1, campaign_id="c", platform="instagram")
    assert "campaignBrief" not in job.to_payload()
    baked = JobSpec(id="j", org_id=1, campaign_id="c", platform="instagram",
                    campaign_brief={"platform": "instagram", "goal": "lead"})
    assert baked.to_payload()["campaignBrief"] == {"platform": "instagram", "goal": "lead"}
    assert JobSpec.from_payload(baked.to_payload()) == baked


def test_from_payload_round_trips_platform_credentials():
    """B4 fix review finding #2: platform_credentials (the org's per-platform secret)
    is baked exactly like campaign_brief so a remote worker's JobSpec carries it."""
    creds = {"api_key": "ORG-KEY"}
    job = JobSpec.from_payload({
        "id": "j", "campaignId": "c", "platform": "youtube",
        "platformCredentials": creds})
    assert job.platform_credentials == creds
    # snake_case is accepted too (same pick() convention as every other field).
    job2 = JobSpec.from_payload({
        "id": "j", "campaignId": "c", "platform": "youtube",
        "platform_credentials": creds})
    assert job2.platform_credentials == creds
    # Absent → None (legacy job / not a per-org-credentialed platform; cli.py falls
    # back to the box-local store lookup).
    assert JobSpec.from_payload({
        "id": "j", "campaignId": "c", "platform": "youtube"}).platform_credentials is None


def test_from_payload_rejects_non_dict_platform_credentials():
    for bad in ("not-a-dict", ["nope"], 5):
        with pytest.raises(JobSpecError):
            JobSpec.from_payload({"id": "j", "campaignId": "c", "platform": "youtube",
                                  "platformCredentials": bad})


def test_to_payload_omits_platform_credentials_unless_baked():
    job = JobSpec(id="j", org_id=1, campaign_id="c", platform="youtube")
    assert "platformCredentials" not in job.to_payload()
    baked = JobSpec(id="j", org_id=1, campaign_id="c", platform="youtube",
                    platform_credentials={"api_key": "ORG-KEY"})
    assert baked.to_payload()["platformCredentials"] == {"api_key": "ORG-KEY"}
    assert JobSpec.from_payload(baked.to_payload()) == baked


def test_run_args_threads_baked_platform_credentials():
    """The last hop: WorkerConfig.run_args must carry job.platform_credentials onto
    the argparse.Namespace so cli._build_run_io/_build_warming_io can read it via
    getattr(args, 'platform_credentials', None) and prefer it over the box-local
    store lookup (B4 fix review finding #2)."""
    from pathlib import Path
    cfg = WorkerConfig(dispatch_base_url="http://x", cfg_dir=Path("cfg"),
                       db_path="d.db", state_dir=Path("st"))
    job = JobSpec.from_payload({
        "id": "j", "campaignId": "c", "platform": "youtube",
        "platformCredentials": {"api_key": "ORG-KEY"}})
    args = cfg.run_args(job)
    assert args.platform_credentials == {"api_key": "ORG-KEY"}
    # A legacy/unbaked job → None, so the fallback path (local store lookup) runs.
    bare_job = JobSpec.from_payload({"id": "j", "campaignId": "c", "platform": "youtube"})
    assert cfg.run_args(bare_job).platform_credentials is None


def test_child_dict_round_trips():
    from aizu.worker.config import WorkerConfig
    from pathlib import Path
    cfg = WorkerConfig(dispatch_base_url="http://x", cfg_dir=Path("cfg"),
                       db_path="d.db", state_dir=Path("st"), cdp_url="http://127.0.0.1:9333",
                       spend_cap=12.5, max_job_minutes=45)
    rebuilt = WorkerConfig.from_child_dict(cfg.to_child_dict())
    assert rebuilt.db_path == "d.db" and rebuilt.cdp_url == "http://127.0.0.1:9333"
    assert rebuilt.spend_cap == 12.5 and rebuilt.max_job_minutes == 45


# ----- B9: priorSpendUsd on the JobSpec + the spend-cap override ------------------

def test_from_payload_parses_prior_spend_usd():
    job = JobSpec.from_payload({"id": "j", "campaignId": "c", "platform": "x",
                                "priorSpendUsd": 4.5})
    assert job.prior_spend_usd == 4.5
    # snake_case spelling too (the stub dispatch mirrors both).
    assert JobSpec.from_payload({"id": "j", "campaignId": "c", "platform": "x",
                                 "prior_spend_usd": "2"}).prior_spend_usd == 2.0
    # Absent → None: a legacy job / an older server. The box falls back to its local
    # total alone, i.e. exactly today's behaviour.
    assert JobSpec.from_payload({"id": "j", "campaignId": "c",
                                 "platform": "x"}).prior_spend_usd is None


@pytest.mark.parametrize("value", ["many", -1, True, float("nan"), float("inf"), []])
def test_from_payload_degrades_a_bad_prior_spend_to_none(value):
    # Deliberately tolerant, UNLIKE targetLeads: an accounting hint must never make an
    # otherwise-runnable job unrunnable, so a garbage value degrades instead of raising.
    job = JobSpec.from_payload({"id": "j", "campaignId": "c", "platform": "x",
                                "priorSpendUsd": value})
    assert job.prior_spend_usd is None


def test_to_payload_omits_prior_spend_unless_present():
    job = JobSpec(id="j", org_id=1, campaign_id="c", platform="instagram")
    assert "priorSpendUsd" not in job.to_payload()
    priced = JobSpec(id="j", org_id=1, campaign_id="c", platform="instagram",
                     prior_spend_usd=7.25)
    assert priced.to_payload()["priorSpendUsd"] == 7.25
    # Round-trip identity holds through the supervisor→child spec file.
    assert JobSpec.from_payload(priced.to_payload()) == priced


def test_run_args_spend_cap_override_replaces_the_box_cap(cfg: WorkerConfig):
    job = JobSpec(id="j", org_id=1, campaign_id="c", platform="instagram")
    assert cfg.run_args(job).spend_cap == cfg.spend_cap          # None → the box cap
    assert cfg.run_args(job, spend_cap_override=2.5).spend_cap == 2.5
    # 0.0 is a real value (a campaign already over budget), not "unset".
    assert cfg.run_args(job, spend_cap_override=0.0).spend_cap == 0.0
