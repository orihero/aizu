"""Worker-LEVEL presence heartbeat (Phase 2, sidecar.py + lease_client.py).

A background thread, separate from the per-job ``_HeartbeatThread``, pings
``POST /api/worker/heartbeat`` every interval for the worker's whole lifetime
(idle keepalive between jobs). It is observational: a failed presence post is
logged and retried, NEVER escalated to a halt (LOCKED #9). It stops cleanly on
``close()``.

These tests fake the dispatch client and use a tiny presence cadence so the
on-interval behavior is observable within a bounded wait; no real HTTP, no timing
flake beyond a generous poll loop.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

import httpx
import pytest

from aizu.worker import job_runner, sidecar
from aizu.worker.config import WorkerConfig
from aizu.worker.lease_client import LeaseClient, Result
from aizu.worker.sidecar import Sidecar

# Bounded wait so a missed/slow beat fails fast instead of hanging the suite.
_WAIT_TIMEOUT_SEC = 5.0
_POLL_SEC = 0.01


@pytest.fixture(autouse=True)
def _fast_heartbeat_floor(monkeypatch):
    """Lower the production heartbeat-cadence floor (security clamp, normally 5s) so
    the interval-driven presence tests beat within the test window instead of waiting
    5s per beat. The clamp itself is still exercised — just with a tiny floor."""
    monkeypatch.setattr(sidecar, "_HEARTBEAT_MIN_SEC", 0.01)


def _wait_until(predicate, timeout: float = _WAIT_TIMEOUT_SEC) -> bool:
    """Spin until ``predicate()`` is truthy or the timeout elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(_POLL_SEC)
    return predicate()


class _FakeClient:
    """Scripts lease responses; records presence/ack/nack calls. Thread-safe
    counters so the presence thread and the test thread can race safely."""

    def __init__(self, *, leases: Optional[list] = None,
                 presence_result: Optional[Result] = None,
                 presence_raises: bool = False):
        self._leases = list(leases or [])
        self._presence_result = presence_result or Result(
            ok=True, data={"drain": False, "halt": False, "updateRequired": False})
        self._presence_raises = presence_raises
        self._lock = threading.Lock()
        self.presence_calls: list[dict] = []
        self.acks: list[dict] = []
        self.nacks: list[dict] = []
        self.registered = False
        self.closed = False

    def with_token(self, token):
        return self

    def register(self, body):
        self.registered = True
        # A tiny cadence so the presence thread beats fast in the test window.
        return Result(ok=True, data={"workerId": "w1", "heartbeatIntervalSec": 0.02})

    def lease(self, body):
        return self._leases.pop(0) if self._leases else Result(ok=True, data=None)

    def presence(self, body):
        with self._lock:
            self.presence_calls.append(dict(body))
        if self._presence_raises:
            raise httpx.ConnectError("boom")  # transport-style explosion
        return self._presence_result

    def heartbeat(self, job_id, body):
        return Result(ok=True, data={})

    def ack(self, job_id, body):
        with self._lock:
            self.acks.append(body)
        return Result(ok=True, data={})

    def nack(self, job_id, body):
        with self._lock:
            self.nacks.append(body)
        return Result(ok=True, data={})

    def close(self):
        self.closed = True

    @property
    def presence_count(self) -> int:
        with self._lock:
            return len(self.presence_calls)


class _DummyStore:
    def close(self):
        pass


def _lease_job(**over):
    job = {"id": "job-1", "orgId": 1, "campaignId": "c", "platform": "instagram"}
    job.update(over)
    return Result(ok=True, data={"job": job})


def _sidecar(cfg: WorkerConfig, client: _FakeClient) -> Sidecar:
    sc = Sidecar(cfg, client=client, store=_DummyStore(),
                 sleep=lambda t: None)
    return sc


# --- LeaseClient.presence: the never-throw boundary --------------------------


def _http_client(handler) -> LeaseClient:
    transport = httpx.MockTransport(handler)
    return LeaseClient("http://stub.local",
                       client=httpx.Client(transport=transport))


def test_presence_posts_to_worker_heartbeat_path():
    # Arrange
    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        return httpx.Response(200, json={"ok": True, "data": {"drain": False}})

    lc = _http_client(handler)

    # Act
    res = lc.presence({"workerId": "w1"})

    # Assert: distinct from the job-scoped /jobs/{id}/heartbeat path.
    assert seen["path"] == "/api/worker/heartbeat"
    assert res.ok is True


def test_presence_never_throws_on_server_error():
    # Arrange
    lc = _http_client(lambda req: httpx.Response(500, text="kaboom"))

    # Act
    res = lc.presence({"workerId": "w1"})

    # Assert: a 500 is a typed failure, never an exception.
    assert res.ok is False
    assert res.status == 500


def test_presence_never_throws_on_transport_error():
    def boom(req):
        raise httpx.ConnectError("connection refused")

    lc = _http_client(boom)

    res = lc.presence({"workerId": "w1"})

    assert res.ok is False
    assert "transport" in (res.error or "").lower()


# --- _PresenceThread lifecycle: starts, beats, stops -------------------------


def test_presence_thread_starts_after_register(monkeypatch, cfg: WorkerConfig):
    # Arrange: an empty lease loop so run() registers then idles.
    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {})
    client = _FakeClient(leases=[])
    sc = _sidecar(cfg, client)

    # Act
    sc.run(max_iterations=1)

    # Assert: register happened and the presence thread is live.
    assert client.registered
    assert sc._presence_thread is not None
    assert sc._presence_thread.is_alive()

    sc.close()


def test_presence_thread_fires_on_interval(monkeypatch, cfg: WorkerConfig):
    # Arrange
    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {})
    client = _FakeClient(leases=[])
    sc = _sidecar(cfg, client)

    # Act
    sc.run(max_iterations=1)
    fired = _wait_until(lambda: client.presence_count >= 1)

    # Assert: at least one presence post landed on /api/worker/heartbeat.
    assert fired, f"presence never fired (count={client.presence_count})"
    assert client.presence_calls[0]["workerId"] == "w1"

    sc.close()


def test_presence_failure_does_not_crash_loop(monkeypatch, cfg: WorkerConfig):
    """A failed presence post is logged and the thread keeps beating — it never
    raises out of the loop and never halts the worker (LOCKED #9)."""
    # Arrange: presence always returns ok=False.
    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {})
    client = _FakeClient(
        leases=[],
        presence_result=Result(ok=False, error="dispatch down"))
    sc = _sidecar(cfg, client)

    # Act
    sc.run(max_iterations=1)
    # The thread must keep beating despite ok=False (proves it didn't die on failure).
    kept_beating = _wait_until(lambda: client.presence_count >= 2)

    # Assert
    assert kept_beating, "presence thread stopped after a failed post"
    assert sc._presence_thread is not None and sc._presence_thread.is_alive()

    sc.close()


def test_presence_exception_does_not_crash_loop(monkeypatch, cfg: WorkerConfig):
    """Even a raising presence call (transport explosion that somehow escapes the
    client) is swallowed inside the thread — it never kills the worker."""
    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {})
    client = _FakeClient(leases=[], presence_raises=True)
    sc = _sidecar(cfg, client)

    sc.run(max_iterations=1)
    kept_beating = _wait_until(lambda: client.presence_count >= 2)

    assert kept_beating, "presence thread died on a raising post"
    assert sc._presence_thread is not None and sc._presence_thread.is_alive()

    sc.close()


def test_close_stops_presence_thread(monkeypatch, cfg: WorkerConfig):
    # Arrange
    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {})
    client = _FakeClient(leases=[])
    sc = _sidecar(cfg, client)
    sc.run(max_iterations=1)
    assert _wait_until(lambda: client.presence_count >= 1)
    thread = sc._presence_thread
    assert thread is not None and thread.is_alive()

    # Act
    sc.close()

    # Assert: the thread is stopped/joined and the reference cleared.
    assert not thread.is_alive()
    assert sc._presence_thread is None
    assert client.closed


def test_active_jobs_reported_in_presence(monkeypatch, cfg: WorkerConfig):
    """The presence body's currentSessions reflects live load: 1 while a job runs,
    0 when idle."""
    seen_during_job = threading.Event()

    def slow_job(*a, **k):
        # While inside the job, _active_jobs must be 1; let the presence thread beat.
        _wait_until(lambda: seen_during_job.is_set(), timeout=_WAIT_TIMEOUT_SEC)
        return {"matches": 0}

    monkeypatch.setattr(job_runner, "run_one_job", slow_job)
    client = _FakeClient(leases=[_lease_job()])
    sc = _sidecar(cfg, client)

    # Drive register + presence start, then a job, on a worker thread so we can
    # observe presence beats mid-job from here. close() runs in finally so a failed
    # assertion never leaks the presence daemon into later tests.
    runner = threading.Thread(target=lambda: sc.run(max_iterations=1), daemon=True)
    try:
        runner.start()

        # Wait for a presence beat that reports the box as busy (currentSessions == 1).
        busy = _wait_until(
            lambda: any(c.get("currentSessions") == 1 for c in client.presence_calls))
        seen_during_job.set()
        runner.join(timeout=_WAIT_TIMEOUT_SEC)

        assert busy, "no presence beat reported currentSessions == 1 during the job"

        # After the job, presence should report idle again.
        idle_again = _wait_until(
            lambda: client.presence_calls and
            client.presence_calls[-1].get("currentSessions") == 0)
        assert idle_again, "presence did not return to currentSessions == 0 when idle"
    finally:
        sc.close()
