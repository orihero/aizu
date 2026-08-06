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
