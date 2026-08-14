"""Pull-loop control flow (sidecar.py, BUILD-PLAN §2.6).

``run_one_job`` and the HTTP client are faked: we assert the loop's lease → run →
ack/nack decisions, backoff on empty/failed leases, single-flight skip, and that no
job error ever crashes the loop.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import pytest

from aizu.worker import job_runner, sidecar
from aizu.worker.config import WorkerConfig
from aizu.worker.job_runner import CampaignNotFound
from aizu.worker.lease_client import Result
from aizu.worker.token_store import TokenStore
import threading

from aizu.worker.sidecar import (Controls, Sidecar, apply_heartbeat,
                                      apply_presence_flags)


class _FakeClient:
    """Scripts lease responses; records ack/nack calls. Heartbeats are inert.

    ``credential`` defaults to a benign 'nothing connected' success (matches the
    legitimate no-secret-yet case) so every EXISTING test — none of which lease a
    per-org-credentialed platform — is unaffected; tests that care about the fetch
    override it via ``credential_result`` and/or read ``credential_calls``.

    ``ack_result``/``nack_result`` override the report outcome (B10 uses them to answer
    401); ``lease_calls``/``register_calls`` are counted so a test can prove the loop
    STOPPED rather than merely backed off."""

    def __init__(self, *, leases: list[Result], heartbeat: Optional[Result] = None,
                 credential_result: Optional[Result] = None,
                 ack_result: Optional[Result] = None,
                 nack_result: Optional[Result] = None):
        self._leases = list(leases)
        self._heartbeat = heartbeat or Result(ok=True, data={})
        self._credential_result = credential_result or Result(
            ok=True, data={"credential": None})
        self._ack_result = ack_result or Result(ok=True, data={})
        self._nack_result = nack_result or Result(ok=True, data={})
        self.acks: list[dict] = []
        self.nacks: list[dict] = []
        self.credential_calls: list[str] = []
        self.registers: list[dict] = []
        self.registered = False
        self.register_calls = 0
        self.lease_calls = 0

    # The bearer this fake "presents" — the sidecar reads it to tell a 401 about ITS
    # OWN token from a stale one another process already rotated (ledger B10).
    token = "worker-token-1"

    def with_token(self, token):
        return self

    def register(self, body):
        self.register_calls += 1
        self.registers.append(body)
        self.registered = True
        return Result(ok=True, data={"workerId": "w1", "heartbeatIntervalSec": 0.05})

    def lease(self, body):
        self.lease_calls += 1
        return self._leases.pop(0) if self._leases else Result(ok=True, data=None)

    def heartbeat(self, job_id, body):
        return self._heartbeat

    def ack(self, job_id, body):
        self.acks.append(body)
        return self._ack_result

    def nack(self, job_id, body):
        self.nacks.append(body)
        return self._nack_result

    def credential(self, job_id):
        self.credential_calls.append(job_id)
        return self._credential_result

    def close(self):
        pass


class _DummyStore:
    def close(self):
        pass


def _lease_job(**over):
    job = {"id": "job-1", "orgId": 1, "campaignId": "c", "platform": "instagram"}
    job.update(over)
    return Result(ok=True, data={"job": job})


class _VirtualClock:
    """A monotonic stand-in that only moves when the sidecar SLEEPS.

    The revocation confirmation is bounded in TIME as well as in count (B10 follow-up), and
    that window is minutes — untestable against the real clock, and meaningless against a
    frozen one. Wiring it to the faked `sleep` makes the loop's own pacing the thing under
    test: a test that wants five minutes to pass has to let the sidecar back off for five
    minutes, exactly as a real box would."""

    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


def _sidecar(cfg: WorkerConfig, client: _FakeClient) -> Sidecar:
    sleeps: list[float] = []
    clock = _VirtualClock()

    def _sleep(t: float) -> None:
        sleeps.append(t)
        clock.advance(t)

    sc = Sidecar(cfg, client=client, store=_DummyStore(), sleep=_sleep, clock=clock)
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


# ----- credential fetch (SECURITY REVIEW CRITICAL/HIGH) --------------------------

def test_credentialed_platform_job_fetches_and_threads_the_credential(monkeypatch,
                                                                       cfg: WorkerConfig):
    """youtube/telegram/reddit: the sidecar fetches the credential BEFORE running and
    hands the enriched JobSpec (platform_credentials populated) to run_one_job — the
    delivery mechanism cli._resolve_platform_credentials' `baked` param expects."""
    seen_jobs = []
    monkeypatch.setattr(job_runner, "run_one_job",
                        lambda store, job, **k: seen_jobs.append(job) or {"matches": 1})
    client = _FakeClient(
        leases=[_lease_job(platform="youtube")],
        credential_result=Result(ok=True, data={"credential": {"api_key": "FETCHED"}}))
    sc = _sidecar(cfg, client)
    sc.run(max_iterations=1)
    assert client.credential_calls == ["job-1"]
    assert seen_jobs[0].platform_credentials == {"api_key": "FETCHED"}
    assert client.acks and client.nacks == []


def test_cdp_platform_job_never_fetches_a_credential(monkeypatch, cfg: WorkerConfig):
    """instagram/linkedin/x drive the warmed CDP browser, not an API key — the fetch
    must never even be attempted for them."""
    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {"matches": 1})
    client = _FakeClient(leases=[_lease_job(platform="instagram")])  # default platform
    sc = _sidecar(cfg, client)
    sc.run(max_iterations=1)
    assert client.credential_calls == []
    assert client.acks and client.nacks == []


def test_credential_fetch_failure_nacks_with_a_distinct_reason_and_never_runs(
        monkeypatch, cfg: WorkerConfig):
    """A failed fetch (bad bearer / lease lost / transport / 500) for a platform that
    NEEDS a credential must nack with its OWN diagnosable reason — never fall through
    to run_one_job and hit a confusing downstream 'needs YOUTUBE_API_KEY' error that
    matches no known halt kind and dead-letters after silent retries."""
    ran = []
    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: ran.append(1) or {})
    client = _FakeClient(leases=[_lease_job(platform="youtube")],
                         credential_result=Result(ok=False, error="lease lost", status=404))
    sc = _sidecar(cfg, client)
    sc.run(max_iterations=1)
    assert ran == []                     # never spawned the child
    assert client.acks == []
    assert client.nacks[0]["reason"] == sidecar.CREDENTIAL_FETCH_FAILED_REASON
    assert client.nacks[0]["poison"] is False  # transient — requeue with backoff


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
        sc._ack(SimpleNamespace(id="job-1", run_id="run-1"),
                {"matches": 1, "run_id": "run-1"})
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
    sc._ack(SimpleNamespace(id="job-1", run_id="run-1"), {"matches": 0})
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


def test_the_register_body_carries_a_host(cfg):
    """`workers.host` was NULL for every box in the fleet: the server has always accepted
    a `host` field and the sidecar simply never sent one. The B8 cutover's completion
    query selects `id, host` to tell an operator WHICH machines still need re-enrolling,
    so a column of NULLs is not merely cosmetic."""
    client = _FakeClient(leases=[])
    sc = _sidecar(cfg, client)
    sc.run(max_iterations=1)

    body = client.registers[0]
    assert isinstance(body["host"], str) and body["host"].strip()
    assert body["machineId"] and body["displayName"]     # the neighbours still ride along


@pytest.mark.parametrize("node, fqdn, expected", [
    # The only upgrade worth taking: the node name PLUS a real domain.
    ("worker-1", "worker-1.fleet.internal", "worker-1.fleet.internal"),
    # `getfqdn` resolves through reverse DNS and readily returns something WORSE than
    # what it started from. Each of these was observed or is standard resolver behaviour,
    # and each would leave `workers.host` less useful than the NULL it replaces.
    ("MacBook-Pro-AI.local",
     "1.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.ip6.arpa",
     "MacBook-Pro-AI.local"),
    ("worker-1", "localhost", "worker-1"),
    ("worker-1", "localhost.localdomain", "worker-1"),
    ("worker-1", "", "worker-1"),
    ("worker-1", "10.0.0.4", "worker-1"),
    # ...and a box with no node name at all still registers with SOMETHING.
    ("", "", "unknown-host"),
])
def test_the_host_never_degrades_below_the_node_name(monkeypatch, node, fqdn, expected):
    monkeypatch.setattr(sidecar, "_HOST_NAME_CACHE", None)
    monkeypatch.setattr(sidecar.platform, "node", lambda: node)
    monkeypatch.setattr(sidecar.socket, "getfqdn", lambda *a: fqdn)
    assert sidecar._host_name() == expected


def test_a_broken_resolver_never_blocks_the_register(monkeypatch):
    """An unidentifiable worker is a nuisance; an unregisterable one is an outage."""
    monkeypatch.setattr(sidecar, "_HOST_NAME_CACHE", None)
    monkeypatch.setattr(sidecar.platform, "node", lambda: "worker-1")

    def _boom(*_a):
        raise OSError("resolver unreachable")

    monkeypatch.setattr(sidecar.socket, "getfqdn", _boom)
    assert sidecar._host_name() == "worker-1"


def test_the_host_lands_in_the_workers_row_OVER_THE_WIRE(tmp_path, monkeypatch):
    """The store-layer-only trap (ledger B4): the register body is validated by
    `_validate_worker_register`, which WHITELISTS its keys, so a field the sidecar sends
    can still be dropped before it reaches `workers.host`. Asserted against the real
    server over a loopback socket and read back out of the DB, not out of the request."""
    import threading
    from pathlib import Path

    from aizu.core.store import Store
    from aizu.secrets import SecretCipher
    from aizu.server import serve

    monkeypatch.setenv("AIZU_WORKER_BOOTSTRAP_TOKEN", "boot-secret-host")
    monkeypatch.setenv("AIZU_SECRET_KEY", SecretCipher.generate_key())
    db_path = str(tmp_path / "dispatch.db")
    panel_dir = tmp_path / "spa"
    panel_dir.mkdir()
    (panel_dir / "index.html").write_text("<!doctype html><html></html>", encoding="utf-8")
    config_dir = Path(__file__).resolve().parents[2] / "config"

    httpd = serve(db_path, str(panel_dir), str(config_dir), port=0, billing_providers={})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        wcfg = WorkerConfig(
            dispatch_base_url=f"http://127.0.0.1:{httpd.server_address[1]}",
            cfg_dir=tmp_path / "config", db_path=db_path,
            state_dir=tmp_path / "worker-state",
            heartbeat_interval_sec=1, lease_poll_timeout_sec=0,
            # API-only, so the launch preflight is green and network-free: this test is
            # about `workers.host` surviving the register whitelist, not about parking.
            capabilities=((None, "youtube", None),),
            bootstrap_token="boot-secret-host")
        wcfg.cfg_dir.mkdir(parents=True, exist_ok=True)
        sc = Sidecar(wcfg, store=_DummyStore(), sleep=lambda t: None)
        try:
            sc.run(max_iterations=1)          # register + one (empty) lease, then stop
        finally:
            sc.close()
        store = Store(db_path)
        try:
            rows = [w for w in store.list_workers() if w["id"] == wcfg.machine_id]
        finally:
            store.close()
    finally:
        httpd.shutdown()

    assert rows, "the sidecar never registered against the real server"
    assert rows[0]["host"], f"workers.host is still NULL: {rows[0]!r}"


# ----- spend roll-up on ack/nack (B9) --------------------------------------------

def test_ack_ships_only_this_attempts_spend_delta(cfg: WorkerConfig, tmp_path):
    """The cursor is taken BEFORE the run, so an ack reports exactly this attempt's
    spend — never the campaign's lifetime local total (which is what the summary's
    `spend_usd` is, and why that key must not be used as the rollup number)."""
    from types import SimpleNamespace
    from aizu.core.store import Store
    store = Store(str(tmp_path / "w.db"))
    try:
        store.log_spend("c", "match", 9.0, model="old")     # a PRIOR run on this box
        cursor = store.max_spend_id("c")
        store.log_spend("c", "match", 0.30, model="m1")
        store.log_spend("c", "match", 0.20, model="m1")
        store.log_spend("c", "vision", 0.50, model="m2")

        client = _FakeClient(leases=[])
        sc = Sidecar(cfg, client=client, store=store, sleep=lambda t: None)
        sc._ack(SimpleNamespace(id="job-1", campaign_id="c", run_id="run-1"),
                {"matches": 1, "run_id": "run-1"}, spend_cursor=cursor)

        body = client.acks[0]
        assert body["dbId"] == store.database_id()
        by_stage = {(r["stage"], r["model"]): r["usd"] for r in body["spend"]}
        assert by_stage == {("match", "m1"): pytest.approx(0.50),
                            ("vision", "m2"): pytest.approx(0.50)}
    finally:
        store.close()


def test_nack_ships_the_spend_of_a_crashed_attempt(cfg: WorkerConfig, tmp_path):
    """Every failure route (crash, halt, operator stop, timeout) funnels through _nack,
    and a requeue is UNPINNED — attempt 2 may land on a box with no record of this
    money. Without the nack rollup up to max_attempts' worth of spend goes unaccounted."""
    from types import SimpleNamespace
    from aizu.core.store import Store
    store = Store(str(tmp_path / "w.db"))
    try:
        cursor = store.max_spend_id("c")
        store.log_spend("c", "match", 1.25, model="m1")

        client = _FakeClient(leases=[])
        sc = Sidecar(cfg, client=client, store=store, sleep=lambda t: None)
        sc._nack(SimpleNamespace(id="job-1", campaign_id="c", run_id="run-1"), "error",
                 spend_cursor=cursor)

        body = client.nacks[0]
        assert body["dbId"] == store.database_id()
        assert [r["usd"] for r in body["spend"]] == [pytest.approx(1.25)]
    finally:
        store.close()


def test_reports_without_a_cursor_carry_no_spend(cfg: WorkerConfig, tmp_path):
    # The credential-fetch nack fires BEFORE the run, so there is no cursor and nothing
    # to report — dbId still rides along (it is the sentinel, not the payload).
    from types import SimpleNamespace
    from aizu.core.store import Store
    store = Store(str(tmp_path / "w.db"))
    try:
        store.log_spend("c", "match", 5.0, model="m1")
        client = _FakeClient(leases=[])
        sc = Sidecar(cfg, client=client, store=store, sleep=lambda t: None)
        sc._nack(SimpleNamespace(id="job-1", campaign_id="c", run_id="run-1"),
                 "credential_fetch_failed")
        assert "spend" not in client.nacks[0]
        assert client.nacks[0]["dbId"] == store.database_id()
    finally:
        store.close()


def test_a_local_store_failure_never_blocks_the_report(cfg: WorkerConfig):
    # _DummyStore has no max_spend_id/spend_since/database_id at all: every helper
    # swallows, the ack still goes out, and NO unattributed spend is shipped.
    from types import SimpleNamespace
    client = _FakeClient(leases=[])
    sc = Sidecar(cfg, client=client, store=_DummyStore(), sleep=lambda t: None)
    sc._ack(SimpleNamespace(id="job-1", campaign_id="c", run_id="run-1"), {"matches": 0},
            spend_cursor=7)
    assert len(client.acks) == 1
    assert "spend" not in client.acks[0] and "dbId" not in client.acks[0]


# ----- B9 REVIEW FIX: the spend cursor survives a reclaimed attempt ---------------

def _spend_sidecar(cfg: WorkerConfig, store) -> tuple[Sidecar, _FakeClient]:
    client = _FakeClient(leases=[])
    return Sidecar(cfg, client=client, store=store, sleep=lambda t: None), client


def test_a_reclaimed_attempt_does_not_lose_its_spend(cfg: WorkerConfig, tmp_path):
    """REVIEW FIX. The cursor used to be re-taken from scratch on every LEASE, so an
    attempt that ended with NO ack and NO nack — the sidecar was killed, and
    `Store.reclaim_offline_jobs` requeued the job PINNED to this same box — permanently
    lost its dollars: the retry's fresh mark already sat past those rows. The cloud then
    handed that phantom headroom to another box, so the fleet-wide ceiling this whole
    change exists to enforce was exceeded, and the panel's `spent`/`cpl` under-reported
    forever."""
    from types import SimpleNamespace
    from aizu.core.store import Store
    store = Store(str(tmp_path / "w.db"))
    try:
        job = SimpleNamespace(id="job-1", campaign_id="c", run_id="run-1")
        sc, client = _spend_sidecar(cfg, store)

        cursor_1 = sc._spend_cursor(job)          # attempt 1 starts
        store.log_spend("c", "match", 5.0, model="m1")
        # ...and is SIGKILLed here: no ack, no nack, so nothing is ever reported.

        cursor_2 = sc._spend_cursor(job)          # reclaim → re-lease on this same box
        assert cursor_2 == cursor_1               # the parked mark is resumed
        store.log_spend("c", "match", 15.0, model="m1")
        sc._ack(job, {"matches": 0, "run_id": "run-1"}, spend_cursor=cursor_2)

        # The full $20 reaches the cloud, not just attempt 2's $15.
        assert sum(r["usd"] for r in client.acks[0]["spend"]) == pytest.approx(20.0)
    finally:
        store.close()


def test_an_accepted_report_clears_the_parked_cursor(cfg: WorkerConfig, tmp_path):
    # Once the cloud has BANKED the spend the mark must go, or the NEXT job for this run
    # would re-report money that is already counted (spend_log has no unique key).
    from types import SimpleNamespace
    from aizu.core.store import Store
    store = Store(str(tmp_path / "w.db"))
    try:
        job = SimpleNamespace(id="job-1", campaign_id="c", run_id="run-1")
        sc, _ = _spend_sidecar(cfg, store)
        cursor = sc._spend_cursor(job)
        assert sc._spend_cursor_path(job).exists()
        store.log_spend("c", "match", 1.0, model="m1")

        sc._ack(job, {"matches": 0, "run_id": "run-1"}, spend_cursor=cursor)

        assert not sc._spend_cursor_path(job).exists()
        assert sc._spend_cursor(job) == 1          # a fresh mark, past the banked row
    finally:
        store.close()


def test_a_rejected_report_keeps_the_parked_cursor(cfg: WorkerConfig, tmp_path):
    # Dispatch refused the nack, so the delta is still unbanked — the mark must survive
    # for the retry rather than being dropped along with the money.
    from types import SimpleNamespace
    from aizu.core.store import Store
    store = Store(str(tmp_path / "w.db"))
    try:
        job = SimpleNamespace(id="job-1", campaign_id="c", run_id="run-1")
        client = _FakeClient(leases=[])
        client.nack = lambda job_id, body: Result(ok=False, error="502")
        sc = Sidecar(cfg, client=client, store=store, sleep=lambda t: None)
        cursor = sc._spend_cursor(job)
        store.log_spend("c", "match", 1.0, model="m1")

        sc._nack(job, "error", spend_cursor=cursor)

        assert sc._spend_cursor_path(job).exists()
        assert sc._spend_cursor(job) == cursor
    finally:
        store.close()


def test_a_garbled_parked_cursor_degrades_to_a_fresh_mark(cfg: WorkerConfig, tmp_path):
    from types import SimpleNamespace
    from aizu.core.store import Store
    store = Store(str(tmp_path / "w.db"))
    try:
        job = SimpleNamespace(id="job-1", campaign_id="c", run_id="run-1")
        sc, _ = _spend_sidecar(cfg, store)
        store.log_spend("c", "match", 1.0, model="m1")
        path = sc._spend_cursor_path(job)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-an-int", encoding="utf-8")

        assert sc._spend_cursor(job) == 1          # falls back to the live high-water mark
    finally:
        store.close()


def test_the_parked_cursor_is_swept_with_the_other_orphan_job_files(cfg: WorkerConfig):
    import os
    import time
    from types import SimpleNamespace
    sc, _ = _spend_sidecar(cfg, _DummyStore())
    path = sc._spend_cursor_path(SimpleNamespace(run_id="run-old"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("7", encoding="utf-8")
    old = time.time() - 30 * 24 * 60 * 60
    os.utime(path, (old, old))

    swept = job_runner.sweep_orphan_job_files(cfg.state_dir, max_age_sec=7 * 24 * 60 * 60)

    assert swept == 1 and not path.exists()


# ----- B10: a 401 retires the token and stops the loop (revocation recovery) ------
#
# Before this, NOTHING in the sidecar looked at a 401: a revoked box re-presented its dead
# bearer forever, never cleared it, and never told anyone — a permanent silent brick that
# only a manual token-file deletion could undo. The contract now: 401 ⇒ clear the token,
# log CRITICAL with the operator action, stop leasing, DON'T auto-re-register (that would
# resurrect a revoked box off the still-valid shared bootstrap secret — ledger B8). Every
# OTHER failure keeps its old retry/backoff behaviour.

# `envelope=True` is load-bearing: only a 401 that really carried the dispatch's
# `{ok, data, error}` body counts as revocation (a proxy's HTML 401 stays transient — see
# test_lease_client.py). A hand-built Result defaults to envelope=False, so a test that
# means "the dispatch refused us" must say so explicitly.
_UNAUTHORIZED = Result(ok=False, error="invalid or revoked worker token", status=401,
                       envelope=True)
_CONFIRM = sidecar._UNAUTHORIZED_CONFIRM_LIMIT
_WINDOW = sidecar._UNAUTHORIZED_CONFIRM_WINDOW_SEC
# Enough loop turns for the confirmation WINDOW to elapse at the slowest legal pacing (the
# 401 retry floor is the per-turn minimum), with headroom. Bounded so a test that fails to
# confirm ends rather than spins.
_TURNS_TO_CONFIRM = int(_WINDOW / sidecar._UNAUTHORIZED_RETRY_MIN_SEC) + 4


def _tokened(sc: Sidecar, cipher, token: str = "worker-token-1") -> TokenStore:
    """Give the sidecar a REAL token store (the conftest cipher, not env) holding a
    persisted token, so a test can prove the token is actually gone afterwards rather
    than trusting a mock's call log."""
    store = TokenStore(sc._cfg.state_dir, cipher=cipher)
    store.save(token)
    sc._tokens = store
    return store


def test_a_401_on_lease_clears_the_token_and_stops_the_loop(monkeypatch, cfg, cipher,
                                                            caplog):
    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {"matches": 1})
    client = _FakeClient(leases=[_UNAUTHORIZED] * _TURNS_TO_CONFIRM + [_lease_job()])
    sc = _sidecar(cfg, client)
    tokens = _tokened(sc, cipher)

    with caplog.at_level(logging.CRITICAL, logger="aizu.worker.sidecar"):
        sc.run(max_iterations=_TURNS_TO_CONFIRM + 5)

    assert sc.reenrolment_required is True
    assert tokens.load() is None                    # really gone from the store...
    assert not (cfg.state_dir / "worker-token.enc").exists()   # ...and off the disk
    assert client.register_calls == 1               # NO auto-re-register (B8)
    assert client.acks == [] and client.nacks == []
    # It stopped the moment the streak was confirmed: it never reached the healthy lease
    # queued behind the 401s, so the loop is not merely backing off.
    assert client.lease_calls <= _TURNS_TO_CONFIRM
    fatal = [r.getMessage() for r in caplog.records if r.levelno >= logging.CRITICAL]
    assert fatal and "401" in fatal[0]
    assert "enrolment token" in fatal[0]            # names the concrete operator action


def test_a_NINE_SECOND_401_BLIP_LEAVES_THE_TOKEN_ALONE(monkeypatch, cfg, cipher):
    """THE regression. A bridge restarted on an empty DB answered 401 from 18:02:19 to
    18:02:28 — NINE SECONDS — and permanently bricked a box enrolled the documented B8 way,
    because `_UNAUTHORIZED_CONFIRM_LIMIT` counted three 401s while `_backoff` was still
    sub-second: three strikes was worth 2.5s of wall clock, measured. For a per-worker
    enrolment token the recovery is a hand-minted token and an operator visit, so this is
    the whole blast radius of B10's fix.

    Driven over the loop's OWN clock (the virtual clock only advances when the sidecar
    sleeps), so what is asserted is the wall-clock cost of a blip, not a call count: the
    dispatch refuses for nine seconds and then recovers, exactly like the observed one."""
    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {"matches": 1})
    blip_ends_at = 9.0
    healthy = _lease_job()
    sc = _sidecar(cfg, _FakeClient(leases=[]))
    tokens = _tokened(sc, cipher)

    served: list[str] = []

    def _lease(_body):
        sc._client.lease_calls += 1
        if sc._clock() < blip_ends_at:
            return _UNAUTHORIZED            # bridge is up, but on an EMPTY db
        if served:
            return Result(ok=True, data=None, envelope=True)   # idle, healthy
        served.append("job")                # DB mounted — the box works again
        return healthy

    sc._client.lease = _lease  # type: ignore[method-assign]

    sc.run(max_iterations=6)

    assert sc.reenrolment_required is False         # the box is still enrolled...
    assert tokens.load() == "worker-token-1"        # ...and still holds its credential
    assert len(sc._client.acks) == 1                # and it ran the next job normally
    # The mechanism, not just the outcome: the FIRST retry after a 401 already outlasts the
    # entire blip, so a nine-second outage cannot even accumulate a second strike.
    assert sc._sleeps[0] >= blip_ends_at


def test_a_401_STREAK_MUST_LAST_MINUTES_not_merely_repeat(cfg, cipher):
    """Count alone is not a duration. Even an unbounded burst of 401s inside a few seconds
    — a caller with no backoff, three threads reporting at once — must never confirm: it is
    indistinguishable from the blip above. Only elapsed time separates them."""
    # The constant itself is the contract — "minutes, not seconds". A future edit that
    # quietly shrinks it back towards zero must fail here, not in production.
    assert _WINDOW >= 60.0, "the confirmation window must be minutes of sustained refusal"
    assert sidecar._UNAUTHORIZED_RETRY_MIN_SEC >= 5.0, "and the retries must be spread"
    sc = _sidecar(cfg, _FakeClient(leases=[]))
    tokens = _tokened(sc, cipher)

    for _ in range(50 * _CONFIRM):                  # 150 consecutive 401s...
        sc._note_unauthorized("lease", presented="worker-token-1")
    sc._clock.advance(_WINDOW - 1.0)                # ...over just under the window
    sc._note_unauthorized("lease", presented="worker-token-1")

    assert sc.reenrolment_required is False
    assert tokens.load() == "worker-token-1"

    sc._clock.advance(2.0)                          # now the window is genuinely past
    sc._note_unauthorized("lease", presented="worker-token-1")

    assert sc.reenrolment_required is True          # B10 preserved: sustained ⇒ retire
    assert tokens.load() is None


def test_the_401_retry_floor_spaces_the_confirmation_over_real_time(cfg, cipher):
    """The window is only a window if the retries are spread. The lease loop's ordinary
    `_backoff` starts at 0.5s, so without a floor a "five minute" confirmation would still
    complete in seconds. Every 401 retry waits at least `_UNAUTHORIZED_RETRY_MIN_SEC`, and
    a healthy loop's pacing is untouched."""
    sc = _sidecar(cfg, _FakeClient(leases=[]))
    _tokened(sc, cipher)

    assert sc._unauthorized_retry_floor() == 0.0    # no streak ⇒ no floor at all
    sc._note_unauthorized("lease", presented="worker-token-1")
    for _ in range(10):
        assert sc._unauthorized_retry_floor() >= sidecar._UNAUTHORIZED_RETRY_MIN_SEC
        assert sc._unauthorized_retry_floor() <= sidecar._UNAUTHORIZED_RETRY_CAP_SEC
    sc._note_authorized()
    assert sc._unauthorized_retry_floor() == 0.0    # ...and it lifts the moment we're back


def test_a_confirmed_revocation_takes_MINUTES_of_sustained_401s(monkeypatch, cfg, cipher):
    """The time bound is load-bearing, so pin it: the token survives every one of the first
    `_WINDOW` seconds of refusal and is only destroyed after them. Guards against a future
    edit re-deriving the confirmation from the call count alone."""
    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {"matches": 1})
    client = _FakeClient(leases=[_UNAUTHORIZED] * _TURNS_TO_CONFIRM)
    sc = _sidecar(cfg, client)
    tokens = _tokened(sc, cipher)
    seen_alive_at: list[float] = []
    original = sc._note_unauthorized

    def _spy(call, presented=None):
        out = original(call, presented=presented)
        if not sc.reenrolment_required:
            seen_alive_at.append(sc._clock())
        return out

    sc._note_unauthorized = _spy  # type: ignore[method-assign]

    sc.run(max_iterations=_TURNS_TO_CONFIRM + 5)

    assert sc.reenrolment_required is True and tokens.load() is None
    assert seen_alive_at, "the box must survive at least one 401 before confirming"
    assert max(seen_alive_at) >= _WINDOW / 2        # it really waited, minutes not seconds
    assert sc._clock() >= _WINDOW                   # ...and the full window elapsed


def test_an_unconfirmed_401_keeps_the_token_and_keeps_leasing(monkeypatch, cfg, cipher):
    """The blast-radius guard on the fix itself. The bridge's worker auth FAILS CLOSED:
    a restart against a not-yet-mounted volume, a restored snapshot, or a mistyped --db
    answers a real 401 envelope to a perfectly VALID token for as long as it lasts. One
    such response must not destroy the box's only credential — a 90-second server-side
    blip would otherwise disenrol every box in the fleet, each needing a hand-minted
    enrolment token to come back."""
    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {"matches": 1})
    assert _CONFIRM > 1, "one 401 must never be enough to destroy a box's credential"
    client = _FakeClient(leases=[_UNAUTHORIZED, _lease_job()])
    sc = _sidecar(cfg, client)
    tokens = _tokened(sc, cipher)

    sc.run(max_iterations=_CONFIRM + 2)

    assert sc.reenrolment_required is False
    assert tokens.load() == "worker-token-1"        # credential untouched
    assert len(client.acks) == 1                    # and it ran the job that followed


def test_the_401_count_resets_on_any_accepted_call(monkeypatch, cfg, cipher):
    """`_UNAUTHORIZED_CONFIRM_LIMIT` means CONSECUTIVE. A 401 every other poll is a
    flapping intermediary, not a revocation, and must never accumulate to a halt."""
    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {"matches": 1})
    empty = Result(ok=True, data=None, envelope=True)
    client = _FakeClient(leases=[_UNAUTHORIZED, empty] * (_CONFIRM + 2))
    sc = _sidecar(cfg, client)
    tokens = _tokened(sc, cipher)

    sc.run(max_iterations=2 * (_CONFIRM + 2))

    assert sc.reenrolment_required is False
    assert tokens.load() == "worker-token-1"


def test_a_401_for_a_TOKEN_ANOTHER_PROCESS_ROTATED_adopts_it_instead_of_clearing(
        monkeypatch, cfg, cipher):
    """Two sidecars can share one AIZU_WORKER_STATE (the packaged desktop worker plus a
    hand-launched `aizu-worker`), and a restart can overlap the old process's long-poll.
    Every register mints a FRESH token, so the loser's in-flight call 401s while the
    winner's brand-new token sits in the shared store. Clearing there would delete the
    OTHER process's valid credential — invisibly, since that process keeps running on its
    in-memory copy and only discovers the loss at its next restart."""
    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {"matches": 1})
    client = _FakeClient(leases=[_UNAUTHORIZED] * (_CONFIRM + 2))
    client.token = "the-stale-token-this-process-holds"
    sc = _sidecar(cfg, client)
    tokens = _tokened(sc, cipher, token="the-token-the-OTHER-process-just-saved")

    sc.run(max_iterations=_CONFIRM + 2)

    assert sc.reenrolment_required is False
    assert tokens.load() == "the-token-the-OTHER-process-just-saved"  # NOT destroyed


def test_a_500_lease_keeps_the_token_and_keeps_retrying(monkeypatch, cfg, cipher):
    """The WORSE bug guard: a flaky network / 5xx / timeout must never brick the box.
    Only a 401 is revocation — a transport failure does not even carry a status."""
    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {"matches": 1})
    transient = [Result(ok=False, error="server error 500", status=500),
                 Result(ok=False, error="transport: timed out"),   # status is None
                 Result(ok=False, error="malformed JSON response", status=502)]
    client = _FakeClient(leases=list(transient) + [_lease_job()])
    sc = _sidecar(cfg, client)
    tokens = _tokened(sc, cipher)

    sc.run(max_iterations=4)

    assert sc.reenrolment_required is False
    assert tokens.load() == "worker-token-1"        # token untouched
    assert client.lease_calls == 4                  # backed off and kept going...
    assert len(client.acks) == 1                    # ...and ran the job that followed


def test_a_401_on_register_stops_once_confirmed(monkeypatch, cfg, cipher):
    """A revoked box's re-register is refused too. Retrying it forever (the old
    `_await_registration` behaviour for ANY failure) is exactly the silent hang — but the
    stop comes on CONFIRMATION, not on the first response."""
    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {"matches": 1})
    client = _FakeClient(leases=[_lease_job()])
    client.register = lambda body: (
        setattr(client, "register_calls", client.register_calls + 1) or _UNAUTHORIZED)
    sc = _sidecar(cfg, client)
    tokens = _tokened(sc, cipher)

    sc.run(max_iterations=_TURNS_TO_CONFIRM + 5)

    assert sc.reenrolment_required is True
    assert tokens.load() is None
    assert sc._clock() >= _WINDOW                   # sustained, not a restart blip...
    assert client.register_calls <= _TURNS_TO_CONFIRM   # ...then stopped dead
    assert client.lease_calls == 0                  # never entered the lease loop


def test_a_transient_register_failure_still_retries(monkeypatch, cfg, cipher):
    """Companion guard to the above: only the 401 short-circuits `_await_registration`."""
    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {"matches": 1})
    client = _FlakyRegisterClient(register_fails=2, leases=[_lease_job()])
    sc = _sidecar(cfg, client)
    tokens = _tokened(sc, cipher)

    sc.run(max_iterations=3)

    assert sc.reenrolment_required is False
    assert tokens.load() == "worker-token-1"
    assert client.register_calls == 3 and client.acks


def test_a_401_on_ack_retires_the_token(cfg, cipher):
    """The job already ran; its result is lost to the cloud either way. The box must
    still notice it is revoked instead of looping on to lease the next job."""
    from types import SimpleNamespace
    client = _FakeClient(leases=[], ack_result=_UNAUTHORIZED)
    sc = _sidecar(cfg, client)
    tokens = _tokened(sc, cipher)
    job = SimpleNamespace(id="job-1", campaign_id="c", run_id="run-1")

    # Jobs are minutes apart in reality, so the streak's TIME bound is met naturally here;
    # the virtual clock stands in for that spacing rather than freezing it.
    for _ in range(_CONFIRM):
        sc._ack(job, {"matches": 0})
        sc._clock.advance(_WINDOW)

    assert sc.reenrolment_required is True and tokens.load() is None
    assert sc._stop_leasing.is_set()                # the loop exits at its next top


def test_a_401_on_ack_inside_ONE_window_does_not_retire_the_token(cfg, cipher):
    """Companion to the above, and the reason it advances the clock: a burst of ack 401s
    inside a single blip is the same non-proof as a burst of lease 401s."""
    from types import SimpleNamespace
    client = _FakeClient(leases=[], ack_result=_UNAUTHORIZED)
    sc = _sidecar(cfg, client)
    tokens = _tokened(sc, cipher)
    job = SimpleNamespace(id="job-1", campaign_id="c", run_id="run-1")

    for _ in range(_CONFIRM + 3):
        sc._ack(job, {"matches": 0})                # no time passes between them

    assert sc.reenrolment_required is False and tokens.load() == "worker-token-1"


def test_a_401_on_nack_retires_the_token(cfg, cipher):
    from types import SimpleNamespace
    client = _FakeClient(leases=[], nack_result=_UNAUTHORIZED)
    sc = _sidecar(cfg, client)
    tokens = _tokened(sc, cipher)
    job = SimpleNamespace(id="job-1", campaign_id="c", run_id="run-1")

    for _ in range(_CONFIRM):
        sc._nack(job, "error")
        sc._clock.advance(_WINDOW)

    assert sc.reenrolment_required is True and tokens.load() is None


def test_a_401_on_the_credential_fetch_nacks_transiently_and_is_counted(
        monkeypatch, cfg, cipher, caplog):
    """A 401 there is about the BEARER (a lost/foreign lease answers 404), so it is
    reported like every other 401 — but the JOB is blameless and must not be poisoned.

    On its own it can never CONFIRM a revocation, and that is deliberate: a lease that
    keeps being accepted is positive proof the dispatch still honours this bearer, so the
    consecutive count resets. A genuinely revoked box 401s on the lease too (see
    test_a_401_on_lease_clears_the_token_and_stops_the_loop) and halts there."""
    monkeypatch.setattr(job_runner, "run_one_job", lambda *a, **k: {"matches": 1})
    client = _FakeClient(leases=[_lease_job(platform="youtube")],
                         credential_result=_UNAUTHORIZED)
    sc = _sidecar(cfg, client)
    tokens = _tokened(sc, cipher)

    with caplog.at_level(logging.WARNING, logger="aizu.worker.sidecar"):
        sc.run(max_iterations=3)

    assert client.nacks[0]["reason"] == sidecar.CREDENTIAL_FETCH_FAILED_REASON
    assert client.nacks[0]["poison"] is False
    assert any("credential fetch (HTTP 401" in r.getMessage() for r in caplog.records)
    assert sc.reenrolment_required is False        # leasing works ⇒ not revoked
    assert tokens.load() == "worker-token-1"


def test_the_revocation_handler_is_idempotent(cfg, cipher, caplog):
    """Loop, presence and heartbeat threads can all see the same 401 storm; the box must
    clear + log ONCE, not once per rejected call."""
    sc = _sidecar(cfg, _FakeClient(leases=[]))
    _tokened(sc, cipher)

    with caplog.at_level(logging.CRITICAL, logger="aizu.worker.sidecar"):
        for call in ("lease", "presence heartbeat", "job heartbeat", "ack"):
            sc._on_auth_revoked(call)

    fatal = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    assert len(fatal) == 1 and "lease" in fatal[0].getMessage()


def test_a_revoked_box_parks_instead_of_exiting(cfg, cipher):
    """The process stays up (control surface serving) so a supervisor/desktop app does
    not crash-loop a worker that can only fail — and re-states the reason periodically.
    A dispatch that keeps refusing keeps it parked: the re-probe below cannot resurrect a
    genuinely revoked box, it just re-parks."""
    client = _FakeClient(leases=[])
    client.register = lambda body: _UNAUTHORIZED     # still revoked on every probe
    sc = _sidecar(cfg, client)
    _tokened(sc, cipher)
    sc._on_auth_revoked("lease")

    recovered = sc.park_for_reenrolment(max_waits=3)  # bounded stand-in for "forever"

    assert recovered is False
    assert sc.reenrolment_required is True            # still halted, still no leasing
    assert sc._stop_leasing.is_set()
    assert sc._sleeps == [sidecar._REENROLMENT_REMINDER_SEC] * 3


def test_a_parked_box_recovers_by_itself_when_the_dispatch_accepts_it_again(cfg, cipher):
    """The other half of parking. If the 401s came from the SERVER (a bridge restarted
    against a rolled-back or not-yet-mounted DB — C3), the box is one successful register
    away from working, and parking must not be the thing that prevents it forever: a
    supervisor sees a healthy process and never restarts it, so nobody would notice until
    a human logged in. The park re-probes on its reminder cadence and resumes."""
    sc = _sidecar(cfg, _FakeClient(leases=[]))       # register succeeds again
    _tokened(sc, cipher)
    sc._on_auth_revoked("lease")
    assert sc.reenrolment_required is True

    recovered = sc.park_for_reenrolment(max_waits=3)

    assert recovered is True
    assert sc.reenrolment_required is False          # halt lifted...
    assert not sc._stop_leasing.is_set()             # ...and leasing re-enabled
    assert sc._sleeps == [sidecar._REENROLMENT_REMINDER_SEC]   # recovered on probe 1
    sc.close()


def test_a_401_heartbeat_is_still_only_one_strike_against_the_running_job():
    """DELIBERATE: a 401 mid-job does NOT itself abandon the child. The pre-existing
    three-strike rule (a worker that cannot heartbeat has lost its lease claim, so the
    server may re-dispatch the job and running blind risks a double run) is what decides
    the job's fate — unchanged by revocation."""
    c = Controls()
    failures = apply_heartbeat(c, _UNAUTHORIZED, 0)
    assert failures == 1 and not c.halt.is_set()


def test_the_job_heartbeat_thread_reports_a_401_upward(monkeypatch):
    monkeypatch.setattr(sidecar, "_HEARTBEAT_MIN_SEC", 0.01)
    seen: list[str] = []
    hb = sidecar._HeartbeatThread(_FakeClient(leases=[], heartbeat=_UNAUTHORIZED),
                                  "w1", "job-1", 0.01, Controls(),
                                  on_unauthorized=lambda call, presented: (
                                      seen.append(call) or None))
    hb.start()
    deadline = time.time() + 5.0
    while not seen and time.time() < deadline:
        time.sleep(0.01)
    hb.stop()
    hb.join(timeout=2.0)
    assert seen and seen[0] == "job heartbeat"


def test_a_report_with_no_determined_delta_keeps_a_prior_attempts_cursor(
        cfg: WorkerConfig, tmp_path):
    """The credential-fetch nack fires BEFORE the run, so it carries no cursor and no
    delta. It must NOT retire a mark a PRIOR attempt of this same run parked — that mark
    is holding real, unbanked money, and dropping it loses those dollars for good."""
    from types import SimpleNamespace
    from aizu.core.store import Store
    store = Store(str(tmp_path / "w.db"))
    try:
        job = SimpleNamespace(id="job-1", campaign_id="c", run_id="run-1")
        sc, client = _spend_sidecar(cfg, store)
        parked = sc._spend_cursor(job)             # attempt 1 starts...
        store.log_spend("c", "match", 5.0, model="m1")
        # ...and dies unreported; attempt 2 is re-leased but its credential fetch fails.
        sc._nack(job, "credential_fetch_failed")

        assert sc._spend_cursor_path(job).exists()
        assert sc._spend_cursor(job) == parked
        # Attempt 3 finally runs and reports attempt 1's money along with its own.
        sc._ack(job, {"matches": 0, "run_id": "run-1"}, spend_cursor=parked)
        assert sum(r["usd"] for r in client.acks[0]["spend"]) == pytest.approx(5.0)
    finally:
        store.close()
