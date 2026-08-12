"""End-to-end wire contract: the real sidecar + real httpx lease client against the
REAL server.py worker plane over a loopback socket (BUILD-PLAN Phase 3).

Supersedes the Phase-1 stub-dispatch integration test — now that server.py serves the
real lease/heartbeat/ack/nack routes, this proves the JSON envelope and endpoint paths
the sidecar sends actually match what the cloud serves: bootstrap register → lease →
run → heartbeat → ack, all over the wire. The engine run itself is faked (no warmed
Chrome here); the true live-smoke exit gate runs on a worker box.
"""
from __future__ import annotations

import tempfile
import threading
from pathlib import Path

import pytest

from aizu.core.store import Store
from aizu.secrets import SecretCipher
from aizu.server import serve
from aizu.worker import job_runner
from aizu.worker.config import WorkerConfig
from aizu.worker.sidecar import Sidecar

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config"
_INDEX_HTML = '<!doctype html><html><body><div id="root"></div></body></html>'
BOOTSTRAP = "boot-secret-int"
ACCOUNT = "acme_handle"
CAP = (1, "instagram", ACCOUNT)


@pytest.fixture
def dispatch(tmp_path: Path, monkeypatch):
    """A real server on an ephemeral port with one job pre-enqueued for the worker's
    capability. Bootstrap registration is enabled so the sidecar can first-register."""
    monkeypatch.setenv("AIZU_WORKER_BOOTSTRAP_TOKEN", BOOTSTRAP)
    # The sidecar persists its minted token via TokenStore (Fernet, keyed on this).
    monkeypatch.setenv("AIZU_SECRET_KEY", SecretCipher.generate_key())
    db_path = str(tmp_path / "dispatch.db")
    panel_dir = tmp_path / "spa"
    panel_dir.mkdir()
    (panel_dir / "index.html").write_text(_INDEX_HTML, encoding="utf-8")

    # Seed a worker capability row + a queued job directly (enqueue validation requires
    # a capable worker to exist; we exercise the WIRE register below, not enqueue auth).
    seed = Store(db_path)
    seed.register_worker(worker_id="seed-cap", token="seed-tok", org_id=1,
                         capabilities=[list(CAP)])
    seed.enqueue_job(job_id="job-int", campaign_id="c-acme", platform="instagram",
                     required_account_handle=ACCOUNT, org_id=1,
                     spec={"target_leads": 1, "engine_mode": "harvest",
                           "soul_text": "be helpful"})
    seed.close()

    httpd = serve(db_path, str(panel_dir), str(CONFIG), port=0, billing_providers={})
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}", db_path
    httpd.shutdown()


def test_sidecar_round_trips_one_job_against_the_real_server(dispatch, tmp_path,
                                                             monkeypatch):
    base_url, db_path = dispatch
    cfg = WorkerConfig(
        dispatch_base_url=base_url,
        cfg_dir=tmp_path / "config",
        db_path=db_path,
        state_dir=tmp_path / "worker-state",
        heartbeat_interval_sec=1,
        lease_poll_timeout_sec=0,
        capabilities=(CAP,),
        bootstrap_token=BOOTSTRAP,
    )
    (cfg.cfg_dir).mkdir(parents=True, exist_ok=True)

    # Fake the engine run (no warmed Chrome): return a summary as run_one_job would.
    # Accept **_ so the supervisor's halt= kwarg (Phase 6) is absorbed without spawning.
    def fake_run_one_job(store, job, *, cfg, **_):
        return {"session_id": f"s-{job.id}", "reels_seen": 10, "matches": 2,
                "escalations": 0, "spend_usd": 0.05, "halt_reason": None,
                "run_id": "r-int", "job_id": job.id}

    monkeypatch.setattr(job_runner, "run_one_job", fake_run_one_job)

    sidecar = Sidecar(cfg)
    try:
        sidecar.run(max_iterations=1)  # register → lease job-int → run → ack
    finally:
        sidecar.close()

    store = Store(db_path)
    try:
        job = store.get_job("job-int")
        assert job["status"] == "done", job
        assert job["result"]["matches"] == 2
        # The worker was minted a real token (the seed-cap one is unrelated).
        worker_ids = {w["id"] for w in store.list_workers()}
        assert cfg.machine_id in worker_ids
        # ack mirrored a cloud-side session row from the summary.
        assert store.get_session("s-job-int") is not None
    finally:
        store.close()


def test_sidecar_runs_a_credentialed_job_with_a_fresh_fetch_on_an_empty_local_db(
        dispatch, tmp_path, monkeypatch):
    """SECURITY REVIEW CRITICAL/HIGH, end to end over the real wire: a worker whose OWN
    local DB is a genuinely EMPTY, freshly-created file (no campaign_meta/
    integration_secrets row could possibly resolve there) still gets the org's youtube
    credential — because it fetches it fresh from the real server, per this job's
    lease, via POST /api/worker/jobs/{id}/credential, rather than depending on a local
    row or a baked job spec. This is the exact scenario the removed server-side bake
    used to compensate for, now closed a different way."""
    base_url, dispatch_db_path = dispatch
    # Seed the credentialed job + the org's secret on the SERVER's db.
    seed = Store(dispatch_db_path)
    try:
        seed.set_integration_secret(1, "youtube", {"api_key": "REMOTE-ORG-KEY"})
        seed.register_worker(worker_id="seed-cap-yt", token="seed-tok-yt", org_id=1,
                             capabilities=[[1, "youtube", None]])
        seed.enqueue_job(job_id="job-yt-int", campaign_id="c-yt", platform="youtube",
                         org_id=1,
                         spec={"target_leads": 1, "engine_mode": "harvest"})
    finally:
        seed.close()

    # The worker's OWN db is a SEPARATE, untouched file — genuinely empty, unlike the
    # shared-db round-trip test above. store.org_for_campaign('c-yt') on THIS db can
    # only ever resolve None; the credential must arrive over the wire, not locally.
    worker_local_db = str(tmp_path / "worker-local-empty.db")

    cfg = WorkerConfig(
        dispatch_base_url=base_url,
        cfg_dir=tmp_path / "config",
        db_path=worker_local_db,
        state_dir=tmp_path / "worker-state",
        heartbeat_interval_sec=1,
        lease_poll_timeout_sec=0,
        capabilities=((1, "youtube", None),),
        bootstrap_token=BOOTSTRAP,
    )
    (cfg.cfg_dir).mkdir(parents=True, exist_ok=True)

    captured_job = {}

    def fake_run_one_job(store, job, *, cfg, **_):
        captured_job["job"] = job
        return {"session_id": f"s-{job.id}", "reels_seen": 1, "matches": 0,
                "escalations": 0, "spend_usd": 0.0, "halt_reason": None,
                "run_id": "r-yt-int", "job_id": job.id}

    monkeypatch.setattr(job_runner, "run_one_job", fake_run_one_job)

    sidecar = Sidecar(cfg)
    try:
        sidecar.run(max_iterations=1)  # register -> lease job-yt-int -> fetch -> run -> ack
    finally:
        sidecar.close()

    # The fetched credential reached the JobSpec run_one_job actually received.
    assert captured_job["job"].platform_credentials == {"api_key": "REMOTE-ORG-KEY"}
    # The worker's own local store never had (and still doesn't have) this secret —
    # proving it came over the wire, not from a local row.
    from aizu.core.store import Store as _Store
    local = _Store(worker_local_db)
    try:
        assert local.org_for_campaign("c-yt") is None
    finally:
        local.close()

    store = Store(dispatch_db_path)
    try:
        assert store.get_job("job-yt-int")["status"] == "done"
    finally:
        store.close()
