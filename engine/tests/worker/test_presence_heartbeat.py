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
# Generous on purpose. These waits are "spin until the background presence thread has done
# N beats", so a longer ceiling costs NOTHING when the predicate becomes true (the helper
# returns on the first passing poll) and is only ever spent on a genuine failure. 5s was
# enough for this file in isolation but starved roughly one full-suite run in five: the
# presence thread runs with `sleep=lambda t: None`, so it is pure GIL contention against
# every other thread the suite has alive at that moment. A flaky revocation test is worse
# than a slow one — B10 is the path that decides whether a box gets bricked.
_WAIT_TIMEOUT_SEC = 20.0
_POLL_SEC = 0.01


@pytest.fixture(autouse=True)
def _fast_heartbeat_floor(monkeypatch):
    """Lower the production heartbeat-cadence floor (security clamp, normally 5s) so
    the interval-driven presence tests beat within the test window instead of waiting
    5s per beat. The clamp itself is still exercised — just with a tiny floor.

    Same reason for the revocation-confirmation window: a confirmed 401 revocation now
    requires the streak to have LASTED `_UNAUTHORIZED_CONFIRM_WINDOW_SEC` (minutes) and
    spaces its retries accordingly, so a ~9s server-side blip can no longer brick a box.
    These tests are about what a PRESENCE 401 does, not about how long the wait is — that
    duration is pinned in `test_sidecar_loop.py` against a virtual clock. Compressed here
    so they still finish in a bounded wait; the CONSECUTIVE-count guard is untouched."""
    monkeypatch.setattr(sidecar, "_HEARTBEAT_MIN_SEC", 0.01)
    monkeypatch.setattr(sidecar, "_UNAUTHORIZED_CONFIRM_WINDOW_SEC", 0.0)
    monkeypatch.setattr(sidecar, "_UNAUTHORIZED_RETRY_MIN_SEC", 0.0)
    monkeypatch.setattr(sidecar, "_UNAUTHORIZED_RETRY_CAP_SEC", 0.0)


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

    token = "worker-token-1"

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


def test_presence_401_retires_the_token_and_stops_leasing(monkeypatch, cfg: WorkerConfig,
                                                          cipher):
    """B10. A 401 is the ONE presence outcome that is not merely observational: the token
    is dead, not the network. It stops NEW leasing (which presence is already allowed to
    do via drain/halt) and clears the stored token — but still never touches a RUNNING
    job, so LOCKED #9 holds."""
    from aizu.worker.token_store import TokenStore

    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {})
    client = _FakeClient(
        leases=[],
        # envelope=True: only the dispatch's OWN 401 body is a revocation signal — a
        # proxy's HTML 401 stays transient (test_lease_client.py).
        presence_result=Result(ok=False, error="invalid or revoked worker token",
                               status=401, envelope=True))
    sc = _sidecar(cfg, client)
    tokens = TokenStore(cfg.state_dir, cipher=cipher)
    tokens.save("worker-token-1")
    sc._tokens = tokens

    sc.run(max_iterations=1)
    revoked = _wait_until(lambda: sc.reenrolment_required)

    assert revoked, "a 401 presence beat never marked the box as revoked"
    assert tokens.load() is None
    assert sc._stop_leasing.is_set()
    # It takes CONSECUTIVE rejections, never a single one (see test_sidecar_loop).
    assert len(client.presence_calls) >= sidecar._UNAUTHORIZED_CONFIRM_LIMIT

    sc.close()


def test_a_revoked_box_STOPS_beating_instead_of_authenticating_forever(
        monkeypatch, cfg: WorkerConfig, cipher):
    """A parked (revoked) process must be inert. Before this, `_on_auth_revoked` never
    touched the presence thread and `main` blocked in `park_for_reenrolment` — the only
    caller of `close()` — so a revoked box posted a rejected /api/worker/heartbeat every
    interval FOREVER (~4k/day), silently, while the desktop UI and control surface both
    reported it halted."""
    from aizu.worker.token_store import TokenStore

    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {})
    client = _FakeClient(
        leases=[],
        presence_result=Result(ok=False, error="invalid or revoked worker token",
                               status=401, envelope=True))
    sc = _sidecar(cfg, client)
    tokens = TokenStore(cfg.state_dir, cipher=cipher)
    tokens.save("worker-token-1")
    sc._tokens = tokens

    sc.run(max_iterations=1)
    assert _wait_until(lambda: sc.reenrolment_required)
    thread = sc._presence_thread
    assert thread is not None
    stopped = _wait_until(lambda: not thread.is_alive())

    assert stopped, "the presence thread kept beating after the box was revoked"
    beats_at_halt = len(client.presence_calls)
    time.sleep(20 * 0.02)                      # ~20 intervals at the test cadence
    assert len(client.presence_calls) == beats_at_halt   # truly inert

    sc.close()


def test_a_transient_presence_failure_never_retires_the_token(monkeypatch,
                                                              cfg: WorkerConfig, cipher):
    """The flaky-network guard: a 500/transport presence failure must leave the token
    (and leasing) exactly as they were — only a 401 means revoked."""
    from aizu.worker.token_store import TokenStore

    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {})
    client = _FakeClient(leases=[],
                         presence_result=Result(ok=False, error="server error 500",
                                                status=500))
    sc = _sidecar(cfg, client)
    tokens = TokenStore(cfg.state_dir, cipher=cipher)
    tokens.save("worker-token-1")
    sc._tokens = tokens

    sc.run(max_iterations=1)
    assert _wait_until(lambda: client.presence_count >= 2)  # kept beating

    assert sc.reenrolment_required is False
    assert tokens.load() == "worker-token-1"
    assert not sc._stop_leasing.is_set()

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


# --- the preflight rides the presence beat (§4.4) ----------------------------
#
# The launch preflight's compact upstream body is what puts the REAL provisioning cause
# (no AIZU_SECRET_KEY, no capabilities, no LLM, Chrome on the other port) in front of an
# admin who cannot SSH into the box. `record_worker_heartbeat` COALESCEs an omitted
# report, so omitting is always safe — the cadence question is only how much noise the
# common case costs, and how stale the console may get.


def _preflight_thread(client, wire) -> sidecar._PresenceThread:
    """A bare presence thread (no Sidecar) beating fast against ``client``."""
    return sidecar._PresenceThread(client, "w1", 0.01, lambda: 0, threading.Event(),
                                   preflight_wire=wire)


def _beat(client, thread, *, at_least: int) -> list:
    thread.start()
    try:
        assert _wait_until(lambda: client.presence_count >= at_least), (
            f"only {client.presence_count} presence beats in {_WAIT_TIMEOUT_SEC}s")
    finally:
        thread.stop()
        thread.join(timeout=_WAIT_TIMEOUT_SEC)
    return list(client.presence_calls)


_GREEN = {"ok": True, "blocking": False, "enforced": True, "ranAt": 1.0, "failed": []}
_RED = {"ok": False, "blocking": True, "enforced": True, "ranAt": 2.0,
        "failed": [{"id": "capabilities", "severity": "fatal", "detail": "unset"}]}


def test_an_unchanged_preflight_does_not_ride_every_beat():
    """A box that has been green for a week must not re-send the same blob every ~20s."""
    client = _FakeClient()
    bodies = _beat(client, _preflight_thread(client, lambda: dict(_GREEN)), at_least=4)

    assert bodies[0].get("preflight") == _GREEN     # first beat: changed from nothing
    assert all("preflight" not in b for b in bodies[1:4])


def test_a_CHANGED_preflight_rides_the_very_next_beat():
    """The whole point: a box that just went red must not wait minutes to say so."""
    reports = [dict(_GREEN)]

    def _wire():
        return dict(reports[-1])

    client = _FakeClient()
    thread = _preflight_thread(client, _wire)
    thread.start()
    try:
        assert _wait_until(lambda: client.presence_count >= 2)
        reports.append(dict(_RED))
        assert _wait_until(
            lambda: any(c.get("preflight", {}).get("blocking") for c in
                        client.presence_calls))
    finally:
        thread.stop()
        thread.join(timeout=_WAIT_TIMEOUT_SEC)


def test_an_unchanged_preflight_is_still_re_sent_every_tenth_beat():
    """The staleness floor. Without it a console could be showing a diagnosis from an
    unbounded time ago — the same 'looks healthy, is dead' confusion this work exists to
    end, just moved one level up."""
    client = _FakeClient()
    thread = _preflight_thread(client, lambda: dict(_GREEN))
    bodies = _beat(client, thread, at_least=sidecar._PREFLIGHT_RESEND_EVERY_BEATS)

    tenth = bodies[sidecar._PREFLIGHT_RESEND_EVERY_BEATS - 1]
    assert tenth.get("preflight") == _GREEN


def test_no_preflight_key_at_all_before_the_first_report_exists():
    """None means 'checking…'. Sending nothing is right: the store COALESCEs an omitted
    report and keeps what it has, so this can never overwrite a real diagnosis with a
    placeholder."""
    client = _FakeClient()
    bodies = _beat(client, _preflight_thread(client, lambda: None), at_least=3)

    assert all("preflight" not in b for b in bodies[:3])


def test_a_raising_preflight_reader_never_breaks_the_beat():
    """Presence is liveness. A diagnostic that cannot be read must not cost the box its
    keepalive — that would turn a cosmetic bug into a reclaimed lease."""
    def _boom():
        raise RuntimeError("cannot read the report")

    client = _FakeClient()
    bodies = _beat(client, _preflight_thread(client, _boom), at_least=3)

    assert len(bodies) >= 3
    assert all("preflight" not in b for b in bodies)


def test_a_report_the_dispatch_REJECTED_is_re_sent_next_beat():
    """Memoized only on an ACCEPTED beat: a report the cloud never received is not 'sent',
    and marking it so would hide a changed diagnosis until the next forced resend."""
    client = _FakeClient(presence_result=Result(ok=False, status=500, error="kaboom"))
    bodies = _beat(client, _preflight_thread(client, lambda: dict(_RED)), at_least=3)

    assert all(b.get("preflight") == _RED for b in bodies[:3])
