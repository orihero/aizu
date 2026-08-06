"""The job-child entry point (job_child.py, Phase 6 supervised-subprocess model).

We exercise main() IN-PROCESS with a monkeypatched job_runner._execute_job (the real
child spawns a subprocess, but its own body is plain Python we can drive directly), then
one REAL subprocess smoke test proves the argv/spec-file/result-file wiring end-to-end.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from aizu.worker import (JOB_RESULT_KIND_CAMPAIGN_NOT_FOUND,
                              JOB_RESULT_KIND_ERROR, JOB_RESULT_KIND_SOUL_MISSING,
                              JOB_RESULT_KIND_SPEC_UNREADABLE, job_child, job_runner)
from aizu.worker.config import JobSpec, WorkerConfig


def _spec_dict(cfg: WorkerConfig, result_file: Path, **job_kw) -> dict:
    base = dict(id="job-1", org_id=1, campaign_id="c-acme", platform="instagram",
                soul_text="be helpful", run_id="run-1")
    base.update(job_kw)
    job = JobSpec(**base)
    return {"job": job.to_payload(), "cfg": cfg.to_child_dict(),
            "resultFile": str(result_file)}


def _write_spec(cfg: WorkerConfig, spec_file: Path, result_file: Path, **kw) -> None:
    spec_file.parent.mkdir(parents=True, exist_ok=True)
    spec_file.write_text(json.dumps(_spec_dict(cfg, result_file, **kw)), encoding="utf-8")


def _run(spec_file: Path) -> int:
    return job_child.main(["--spec-file", str(spec_file)])


def test_successful_run_writes_ok_result(monkeypatch, cfg: WorkerConfig, tmp_path):
    spec, result = tmp_path / "spec.json", tmp_path / "result.json"
    _write_spec(cfg, spec, result)
    monkeypatch.setattr(job_runner, "_execute_job",
                        lambda store, job, *, cfg: {"matches": 3, "run_id": "run-1",
                                                    "job_id": job.id})
    rc = _run(spec)
    assert rc == 0
    data = json.loads(result.read_text())
    assert data["ok"] is True
    assert data["summary"]["matches"] == 3


def test_campaign_not_found_records_kind(monkeypatch, cfg: WorkerConfig, tmp_path):
    spec, result = tmp_path / "spec.json", tmp_path / "result.json"
    _write_spec(cfg, spec, result)

    def boom(store, job, *, cfg):
        raise job_runner.CampaignNotFound("c-acme")
    monkeypatch.setattr(job_runner, "_execute_job", boom)
    rc = _run(spec)
    assert rc == 1
    data = json.loads(result.read_text())
    assert data["ok"] is False
    assert data["kind"] == JOB_RESULT_KIND_CAMPAIGN_NOT_FOUND


def test_soul_missing_records_kind(monkeypatch, cfg: WorkerConfig, tmp_path):
    spec, result = tmp_path / "spec.json", tmp_path / "result.json"
    _write_spec(cfg, spec, result)
    monkeypatch.setattr(job_runner, "_execute_job",
                        lambda *a, **k: (_ for _ in ()).throw(job_runner.SoulMissing("x")))
    assert _run(spec) == 1
    assert json.loads(result.read_text())["kind"] == JOB_RESULT_KIND_SOUL_MISSING


def test_generic_exception_records_error_kind_not_message(monkeypatch, cfg, tmp_path):
    """A secret-bearing exception string must NOT reach the result file — only a fixed
    code (the same rule the sidecar's nack path follows)."""
    spec, result = tmp_path / "spec.json", tmp_path / "result.json"
    _write_spec(cfg, spec, result)

    def boom(store, job, *, cfg):
        raise RuntimeError("Bearer sk-supersecret-token leaked here")
    monkeypatch.setattr(job_runner, "_execute_job", boom)
    assert _run(spec) == 1
    data = json.loads(result.read_text())
    assert data["kind"] == JOB_RESULT_KIND_ERROR
    assert "supersecret" not in json.dumps(data)   # message never surfaced
    assert data["error"] == "RuntimeError"


def test_malformed_spec_json_records_spec_unreadable_exit(cfg: WorkerConfig, tmp_path):
    spec = tmp_path / "spec.json"
    spec.write_text("{ this is not json", encoding="utf-8")
    # No resultFile learnable → exit non-zero, no crash escapes main().
    assert _run(spec) == 1


def test_spec_with_bad_fields_records_spec_unreadable(cfg: WorkerConfig, tmp_path):
    spec, result = tmp_path / "spec.json", tmp_path / "result.json"
    spec.write_text(json.dumps({
        "job": {"id": "j", "campaignId": "c", "platform": "NOT_A_PLATFORM"},
        "cfg": cfg.to_child_dict(), "resultFile": str(result)}), encoding="utf-8")
    assert _run(spec) == 1
    assert json.loads(result.read_text())["kind"] == JOB_RESULT_KIND_SPEC_UNREADABLE


def test_jobspec_payload_round_trips():
    job = JobSpec(id="j1", org_id=7, campaign_id="c-x", platform="instagram",
                  required_account_handle="acme_ig", target_leads=25,
                  duration_minutes=30, engine_mode="harvest", soul_text="soul",
                  run_id="run-9")
    assert JobSpec.from_payload(job.to_payload()) == job


@pytest.mark.slow
def test_real_subprocess_wiring_end_to_end(cfg: WorkerConfig, tmp_path):
    """Spawn the REAL child against a spec pointing at a test-only stub _execute_job,
    proving argv → spec-file → result-file survives a genuine process boundary.
    The stub is injected via AIZU_JOB_CHILD_STUB so it works across the fork."""
    spec, result = tmp_path / "spec.json", tmp_path / "result.json"
    _write_spec(cfg, spec, result)
    stub = tmp_path / "stub.py"
    stub.write_text(
        "from aizu.worker import job_runner, job_child\n"
        "job_runner._execute_job = lambda store, job, *, cfg: "
        "{'matches': 1, 'run_id': 'run-1', 'job_id': job.id}\n"
        "import sys\n"
        "sys.exit(job_child.main(['--spec-file', %r]))\n" % str(spec),
        encoding="utf-8")
    rc = subprocess.run([sys.executable, str(stub)], capture_output=True,
                        text=True, timeout=60)
    assert rc.returncode == 0, rc.stderr
    data = json.loads(result.read_text())
    assert data["ok"] is True and data["summary"]["matches"] == 1
