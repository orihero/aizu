"""Pull-loop control flow (sidecar.py, BUILD-PLAN §2.6).

``run_one_job`` and the HTTP client are faked: we assert the loop's lease → run →
ack/nack decisions, backoff on empty/failed leases, single-flight skip, and that no
job error ever crashes the loop.
"""
from __future__ import annotations

from typing import Optional

import pytest

from aizu.worker import job_runner, sidecar
from aizu.worker.config import WorkerConfig
from aizu.worker.job_runner import CampaignNotFound
from aizu.worker.lease_client import Result
import threading

from aizu.worker.sidecar import (Controls, Sidecar, apply_heartbeat,
                                      apply_presence_flags)


class _FakeClient:
    """Scripts lease responses; records ack/nack calls. Heartbeats are inert."""

    def __init__(self, *, leases: list[Result], heartbeat: Optional[Result] = None):
        self._leases = list(leases)
        self._heartbeat = heartbeat or Result(ok=True, data={})
        self.acks: list[dict] = []
        self.nacks: list[dict] = []
        self.registered = False

    def with_token(self, token):
        return self

    def register(self, body):
        self.registered = True
        return Result(ok=True, data={"workerId": "w1", "heartbeatIntervalSec": 0.05})

    def lease(self, body):
        return self._leases.pop(0) if self._leases else Result(ok=True, data=None)

    def heartbeat(self, job_id, body):
        return self._heartbeat

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
    job = {"id": "job-1", "orgId": 1, "campaignId": "c", "platform": "instagram"}
    job.update(over)
    return Result(ok=True, data={"job": job})


def _sidecar(cfg: WorkerConfig, client: _FakeClient) -> Sidecar:
    sleeps: list[float] = []
    sc = Sidecar(cfg, client=client, store=_DummyStore(),
                 sleep=lambda t: sleeps.append(t))
    sc._sleeps = sleeps  # type: ignore[attr-defined]
    return sc


def test_leases_runs_and_acks(monkeypatch, cfg: WorkerConfig):
    monkeypatch.setattr(job_runner, "run_one_job",
                        lambda *a, **k: {"matches": 3, "spend_usd": 0.0})
    client = _FakeClient(leases=[_lease_job()])
    sc = _sidecar(cfg, client)
    sc.run(max_iterations=2)
    assert client.registered
    assert len(client.acks) == 1
    assert client.acks[0]["summary"]["matches"] == 3
    assert client.nacks == []


def test_heartbeat_collects_local_run_events(tmp_path, cfg: WorkerConfig):
    """The job heartbeat thread reads NEW local run_events (after its cursor) and maps
    them to the wire batch it ships — the mechanism behind the fleet live feed (Gap A)."""
    from aizu.core.store import Store
    from aizu.worker.sidecar import _HeartbeatThread

    db = str(tmp_path / "hb.db")
    store = Store(db)
    store.start_session("s-1", "c-acme", "instagram", run_id="run-1", org_id=1)
    store.emit_run_event("run-1", 1, "scan", "info", "hello", session_id="s-1")
    store.emit_run_event("run-1", 2, "scan", "info", "world", session_id="s-1")
    store.close()

    hb = _HeartbeatThread(_FakeClient(leases=[]), "w1", "job-1", 0.05, Controls(),
                          db_path=db, run_id="run-1")
    events_store = Store(db)
    try:
        batch, max_id = hb._collect_run_events(events_store, 0)
        assert [e["message"] for e in batch] == ["hello", "world"]
        assert max_id > 0
        # Cursor at max_id → nothing new.
        assert hb._collect_run_events(events_store, max_id) == ([], max_id)
    finally:
        events_store.close()


def test_apply_presence_flags_stops_leasing_on_drain_or_halt():
    for flags in ({"drain": True}, {"halt": True}, {"drain": False, "halt": True}):
        ev = threading.Event()
        apply_presence_flags(ev, Result(ok=True, data=flags))
        assert ev.is_set(), flags


def test_apply_presence_flags_noop_when_clear():
    ev = threading.Event()
    apply_presence_flags(ev, Result(ok=True, data={"drain": False, "halt": False}))
    apply_presence_flags(ev, Result(ok=True, data=None))
    assert not ev.is_set()


def test_idle_worker_stops_leasing_when_presence_flag_set(monkeypatch, cfg: WorkerConfig):
    """An idle box that has been drained (presence flag) must NOT lease a fresh job —
    it exits the loop at the top before calling lease(). Regression for Gap C: the
    idle worker previously kept leasing until a job's own heartbeat carried the flag."""
    ran: list = []
    monkeypatch.setattr(job_runner, "run_one_job",
                        lambda *a, **k: ran.append(1) or {})
    client = _FakeClient(leases=[_lease_job(), _lease_job()])
    sc = _sidecar(cfg, client)
    sc._stop_leasing.set()  # presence thread would set this on a drain/halt beat

    sc._loop(max_iterations=3)

    assert ran == []            # never ran a job
    assert client.acks == []    # never leased/acked anything
    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {})
    client = _FakeClient(leases=[])  # always empty
    sc = _sidecar(cfg, client)
    sc.run(max_iterations=3)
    assert client.acks == [] and client.nacks == []
    assert len(sc._sleeps) == 3  # backed off each empty poll


def test_failed_lease_backs_off_and_continues(monkeypatch, cfg: WorkerConfig):
    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {})
    client = _FakeClient(leases=[Result(ok=False, error="boom")])
    sc = _sidecar(cfg, client)
    sc.run(max_iterations=1)
    assert len(sc._sleeps) == 1  # transient failure → backoff, no crash


def test_halt_summary_nacks_instead_of_acking(monkeypatch, cfg: WorkerConfig):
    monkeypatch.setattr(job_runner, "run_one_job",
                        lambda *a, **k: {"halt_reason": "outside daytime window"})
    client = _FakeClient(leases=[_lease_job()])
    sc = _sidecar(cfg, client)
    sc.run(max_iterations=1)
    assert client.acks == []
    assert client.nacks[0]["reason"] == "outside daytime window"
    assert client.nacks[0]["poison"] is False


def test_poison_halt_is_marked_poison(monkeypatch, cfg: WorkerConfig):
    monkeypatch.setattr(job_runner, "run_one_job",
                        lambda *a, **k: {"halt_reason": "instagram needs reconnect: 401"})
    client = _FakeClient(leases=[_lease_job()])
    sc = _sidecar(cfg, client)
    sc.run(max_iterations=1)
    assert client.nacks[0]["poison"] is True


def test_campaign_not_found_nacks(monkeypatch, cfg: WorkerConfig):
    def boom(*a, **k):
        raise CampaignNotFound("c")
    monkeypatch.setattr(job_runner, "run_one_job", boom)
    client = _FakeClient(leases=[_lease_job()])
    sc = _sidecar(cfg, client)
    sc.run(max_iterations=1)
    assert client.nacks[0]["reason"] == "campaign_not_found"


def test_run_exception_nacks_and_loop_survives(monkeypatch, cfg: WorkerConfig):
    def boom(*a, **k):
        raise RuntimeError("engine exploded")
    monkeypatch.setattr(job_runner, "run_one_job", boom)
    client = _FakeClient(leases=[_lease_job(), _lease_job(id="job-2")])
    sc = _sidecar(cfg, client)
    sc.run(max_iterations=2)  # second lease still processed → loop survived
    assert len(client.nacks) == 2
    # Outbound reason is a fixed code, NOT the raw exception (security review M1); the
    # full detail is in the redacted local log only.
    assert client.nacks[0]["reason"] == "error"


def test_malformed_leased_job_is_rejected(monkeypatch, cfg: WorkerConfig):
    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {})
    # Missing platform → JobSpecError; loop must not crash.
    client = _FakeClient(leases=[Result(ok=True, data={"job": {"id": "bad", "campaignId": "c"}})])
    sc = _sidecar(cfg, client)
    sc.run(max_iterations=1)
    assert client.acks == []
    assert client.nacks[0]["jobId"] == "bad"
    assert "invalid_spec" in client.nacks[0]["reason"]


def test_single_flight_blocks_second_job_for_same_account(monkeypatch, cfg: WorkerConfig):
    from aizu.worker import single_flight
    # Pre-hold the lock for this account so the leased job is skipped (no run/ack/nack).
    held = single_flight.try_acquire(cfg.state_dir, "1-instagram-_default")
    assert held is not None
    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {"matches": 1})
    client = _FakeClient(leases=[_lease_job()])
    sc = _sidecar(cfg, client)
    sc.run(max_iterations=1)
    assert client.acks == [] and client.nacks == []  # skipped, re-leased later


def test_startup_sweeps_orphan_pause_file(monkeypatch, cfg: WorkerConfig):
    """The sidecar sweeps a crashed run's orphan pause sentinel before leasing, so it
    can't idle the next run (BUILD-PLAN C6 / §2.7)."""
    import os
    import time
    stale = cfg.state_dir / "run-deadbeef0001.pause"
    stale.write_text("x", encoding="utf-8")
    old = time.time() - 10_000_000
    os.utime(stale, (old, old))

    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {})
    sc = _sidecar(cfg, _FakeClient(leases=[]))
    sc.run(max_iterations=1)

    assert not stale.exists()


# --- apply_heartbeat: pure flag-folding logic --------------------------------


def test_apply_heartbeat_sets_drain():
    c = Controls()
    apply_heartbeat(c, Result(ok=True, data={"drain": True}), 0)
    assert c.drain.is_set() and not c.halt.is_set()


def test_apply_heartbeat_sets_halt():
    c = Controls()
    apply_heartbeat(c, Result(ok=True, data={"halt": True}), 0)
    assert c.halt.is_set() and c.halt_reason == "halted"


def test_apply_heartbeat_three_failures_escalate_to_halt():
    c = Controls()
    f = 0
    for _ in range(3):
        f = apply_heartbeat(c, Result(ok=False, error="down"), f)
    assert c.halt.is_set() and c.halt_reason == "heartbeat_failed"


def test_apply_heartbeat_success_resets_failures():
    c = Controls()
    f = apply_heartbeat(c, Result(ok=False, error="x"), 0)
    assert f == 1
    f = apply_heartbeat(c, Result(ok=True, data={}), f)
    assert f == 0 and not c.halt.is_set()


# ----- lead sync-back on ack (Phase 3) -------------------------------------------

def test_ack_ships_captured_leads_from_the_local_store(cfg: WorkerConfig, tmp_path):
    """_ack reads the run's captured leads from the LOCAL store and includes them in
    the ack body as camelCase DTOs (campaign omitted — forced server-side)."""
    from types import SimpleNamespace
    from aizu.core.store import Store
    store = Store(str(tmp_path / "w.db"))
    try:
        store.start_session("s-1", "c", "instagram", run_id="run-1", org_id=1)
        store.upsert_match(campaign_id="c", reel_id="r", comment_id="cmt-1",
                           username="u", text="need it", lang="en", score=0.8,
                           reason="intent", extracted={"phone": "9"}, tier="local",
                           session_id="s-1", platform="instagram", captured_at=900.0)
        client = _FakeClient(leases=[])
        sc = Sidecar(cfg, client=client, store=store, sleep=lambda t: None)
        sc._ack(SimpleNamespace(id="job-1"), {"matches": 1, "run_id": "run-1"})
        assert len(client.acks) == 1
        leads = client.acks[0]["leads"]
        assert [x["commentId"] for x in leads] == ["cmt-1"]
        assert leads[0]["capturedAt"] == 900.0
        assert leads[0]["extracted"] == {"phone": "9"}
        assert "campaignId" not in leads[0]  # campaign is forced server-side
    finally:
        store.close()


def test_ack_without_a_run_id_sends_no_leads(cfg: WorkerConfig):
    from types import SimpleNamespace
    client = _FakeClient(leases=[])
    sc = Sidecar(cfg, client=client, store=_DummyStore(), sleep=lambda t: None)
    sc._ack(SimpleNamespace(id="job-1"), {"matches": 0})
    assert len(client.acks) == 1 and "leads" not in client.acks[0]


def test_signal_halt_first_writer_wins():
    """Two threads (heartbeat + control surface) can both halt; the FIRST reason must
    survive, never be clobbered by a later last-writer (review LOW #9)."""
    c = Controls()
    c.signal_halt("operator_stop")
    c.signal_halt("heartbeat_failed")
    assert c.halt.is_set()
    assert c.halt_reason == "operator_stop"


class _FlakyRegisterClient(_FakeClient):
    """register() fails the first N calls, then succeeds — models a transient/unreachable
    dispatch (or a freshly-switched one) that the sidecar must survive."""

    def __init__(self, *, register_fails: int, leases):
        super().__init__(leases=leases)
        self._register_fails = register_fails
        self.register_calls = 0

    def register(self, body):
        self.register_calls += 1
        if self.register_calls <= self._register_fails:
            return Result(ok=False, error="dispatch unreachable")
        self.registered = True
        return Result(ok=True, data={"workerId": "w1", "heartbeatIntervalSec": 0.05})


def test_survives_transient_register_failure_then_registers(monkeypatch, cfg):
    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {"matches": 1})
    client = _FlakyRegisterClient(register_fails=2, leases=[_lease_job()])
    sc = _sidecar(cfg, client)
    sc.run(max_iterations=3)
    assert client.register_calls == 3        # retried past the 2 failures
    assert client.acks                        # eventually registered → leased → acked


def test_never_registers_returns_without_leasing(monkeypatch, cfg):
    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {"matches": 1})
    client = _FlakyRegisterClient(register_fails=999, leases=[_lease_job()])
    sc = _sidecar(cfg, client)
    sc.run(max_iterations=3)                  # bounded → gives up without hanging
    assert sc._worker_id is None
    assert not client.acks                    # never leased
