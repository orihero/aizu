"""Sidecar ↔ control-surface wiring (current_job tracking, pause/resume, hard-stop).

Reuses the fake-client pattern from test_sidecar_loop.py.
"""
from __future__ import annotations

from typing import Optional

import pytest

from reelradar.worker import job_runner
from reelradar.worker.config import WorkerConfig
from reelradar.worker.lease_client import Result
from reelradar.worker.sidecar import Sidecar, SidecarControlSource


class _FakeClient:
    def __init__(self, *, leases):
        self._leases = list(leases)
        self.lease_calls = 0
        self.acks = []
        self.nacks = []

    def with_token(self, token):
        return self

    def register(self, body):
        return Result(ok=True, data={"workerId": "w1", "heartbeatIntervalSec": 0.05})

    def lease(self, body):
        self.lease_calls += 1
        return self._leases.pop(0) if self._leases else Result(ok=True, data=None)

    def heartbeat(self, job_id, body):
        return Result(ok=True, data={})

    def presence(self, body):
        return Result(ok=True, data={})

    def ack(self, job_id, body):
        self.acks.append(body)
        return Result(ok=True, data={})

    def nack(self, job_id, body):
        self.nacks.append(body)
        return Result(ok=True, data={})

    def close(self):
        pass


class _DummyStore:
    def close(self):
        pass


def _lease_job(**over):
    job = {"id": "job-1", "orgId": 1, "campaignId": "c", "platform": "instagram",
           "runId": "run-1"}
    job.update(over)
    return Result(ok=True, data={"job": job})


def _sidecar(cfg, client) -> Sidecar:
    return Sidecar(cfg, client=client, store=_DummyStore(), sleep=lambda t: None)


def test_current_job_reflects_running_job_then_clears(monkeypatch, cfg: WorkerConfig):
    seen = {}

    def fake_run(store, job, *, cfg, halt):
        seen["during"] = None
        # current_job is observable from another thread while the job runs.
        seen["during"] = _sc.current_job
        return {"matches": 0}

    client = _FakeClient(leases=[_lease_job()])
    _sc = _sidecar(cfg, client)
    monkeypatch.setattr(job_runner, "run_one_job", fake_run)
    _sc.run(max_iterations=1)
    assert seen["during"] is not None
    assert seen["during"].job_id == "job-1"
    assert seen["during"].run_id == "run-1"
    assert _sc.current_job is None  # cleared in finally


def test_request_stop_signals_halt_on_active_job(monkeypatch, cfg: WorkerConfig):
    captured = {}

    def fake_run(store, job, *, cfg, halt):
        captured["accepted"] = _sc.request_stop_current_job()
        captured["halt_set"] = halt.is_set()
        return {"matches": 0}

    client = _FakeClient(leases=[_lease_job()])
    _sc = _sidecar(cfg, client)
    monkeypatch.setattr(job_runner, "run_one_job", fake_run)
    _sc.run(max_iterations=1)
    assert captured["accepted"] is True
    assert captured["halt_set"] is True          # supervisor would kill the child
    assert client.nacks and client.nacks[0]["reason"] == "operator_stop"


def test_request_stop_when_idle_returns_false(cfg: WorkerConfig):
    sc = _sidecar(cfg, _FakeClient(leases=[]))
    assert sc.request_stop_current_job() is False


def test_pause_idles_without_leasing(cfg: WorkerConfig):
    client = _FakeClient(leases=[_lease_job()])
    sc = _sidecar(cfg, client)
    sc._worker_id = "w1"
    sc.pause()
    sc._loop(max_iterations=3)
    assert client.lease_calls == 0          # never leased while paused
    assert len(client._leases) == 1         # job still queued
    sc.resume()
    sc._loop(max_iterations=1)
    assert client.lease_calls == 1          # leases again after resume


def test_control_source_status_snapshot(cfg: WorkerConfig):
    sc = _sidecar(cfg, _FakeClient(leases=[]))
    sc._worker_id = "w1"
    snap = SidecarControlSource(sc).get_status()
    assert snap.worker_id == "w1"
    assert snap.current_job is None
    assert snap.paused is False
    assert snap.chrome is not None  # a (disconnected) chrome status is always present
