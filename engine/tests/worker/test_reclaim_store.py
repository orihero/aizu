"""Store-layer offline→interrupted→requeue reclaim (BUILD-PLAN Phase 4).

The invariant under test: a job is reclaimed ONLY when its lease has expired AND its
worker is offline (never for mere slowness, risk #2); a reclaimed job is requeued PINNED
to its original box (one account ↔ one box, no cross-box failover) or dead-lettered when
attempts run out.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from aizu.core.store import (DEFAULT_JOB_MAX_ATTEMPTS, Store,
                                   WORKER_PINNED_DEAD_LETTER_SEC,
                                   WORKER_RECLAIM_ALERT_SEC,
                                   WORKER_RECLAIM_OFFLINE_SEC,
                                   default_lease_ttl_sec)

PLAT = "instagram"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(str(tmp_path / "reclaim.db"))
    yield s
    s.close()


def _worker(s: Store, wid: str, account: str, *, org_id=1, last_hb=None):
    s.register_worker(worker_id=wid, token=f"tok-{wid}", org_id=org_id,
                      capabilities=[[org_id, PLAT, account]])
    if last_hb is not None:
        s.record_worker_heartbeat(worker_id=wid)  # ensure row is heartbeat-touched
        with s._tx() as c:  # backdate the heartbeat for offline simulation
            c.execute("UPDATE workers SET last_heartbeat_at=? WHERE id=?", (last_hb, wid))


def _enqueue(s: Store, jid: str, account: str, *, org_id=1, max_attempts=DEFAULT_JOB_MAX_ATTEMPTS):
    return s.enqueue_job(job_id=jid, campaign_id="c-acme", platform=PLAT,
                         required_account_handle=account, org_id=org_id,
                         spec={"target_leads": 1, "engine_mode": "harvest",
                               "soul_text": "x"}, max_attempts=max_attempts)


# ----- reclaim conditions --------------------------------------------------------

def test_reclaim_requeues_expired_lease_of_offline_worker(store: Store):
    _worker(store, "w1", "acct", last_hb=1000.0)
    _enqueue(store, "j1", "acct")
    lease = store.lease_one_job(worker_id="w1", capabilities=[[1, PLAT, "acct"]],
                                now=1000.0)
    assert lease is not None
    # now = far past the lease deadline AND past the offline threshold.
    now = 1000.0 + default_lease_ttl_sec() + WORKER_RECLAIM_OFFLINE_SEC + 10
    reclaimed = store.reclaim_offline_jobs(now=now)
    assert len(reclaimed) == 1
    assert reclaimed[0] == {"jobId": "j1", "workerId": "w1",
                            "offlineSec": now - 1000.0, "outcome": "requeued",
                            "attempts": 1, "orgId": 1, "campaignId": "c-acme",
                            "platform": PLAT}
    job = store.get_job("j1")
    assert job["status"] == "queued" and job["pinnedWorkerId"] == "w1"
    assert job["leasedBy"] is None and job["attempts"] == 1


def test_reclaim_requeues_expired_lease_of_online_worker(store: Store):
    # DELIBERATE semantic change (2026-07-03): an EXPIRED lease is the death signal even
    # when the box still beats its SEPARATE presence heartbeat — a wedged job child stops
    # extending the lease while presence stays alive, so the old "skip if online" rule let
    # the job stay leased forever and blocked redispatch ("already running"). Now reclaimed.
    now = 5000.0
    _worker(store, "w1", "acct", last_hb=now - 1.0)  # fresh presence heartbeat (online)
    _enqueue(store, "j1", "acct")
    store.lease_one_job(worker_id="w1", capabilities=[[1, PLAT, "acct"]],
                        now=now - default_lease_ttl_sec() - 100)  # lease already expired
    reclaimed = store.reclaim_offline_jobs(now=now)
    assert [r["jobId"] for r in reclaimed] == ["j1"]
    assert reclaimed[0]["outcome"] == "requeued"
    job = store.get_job("j1")
    assert job["status"] == "queued" and job["pinnedWorkerId"] == "w1"
    assert job["leasedBy"] is None and job["attempts"] == 1


def test_reclaim_skips_unexpired_lease(store: Store):
    # Worker offline, but the lease has NOT expired yet → leave it (TTL protects it).
    _worker(store, "w1", "acct", last_hb=1000.0)
    _enqueue(store, "j1", "acct")
    store.lease_one_job(worker_id="w1", capabilities=[[1, PLAT, "acct"]], now=1000.0)
    # now is past offline threshold but BEFORE the lease deadline.
    now = 1000.0 + WORKER_RECLAIM_OFFLINE_SEC + 1
    now = min(now, 1000.0 + default_lease_ttl_sec() - 1)
    assert store.reclaim_offline_jobs(now=now) == []


def test_reclaim_dead_letters_when_attempts_exhausted(store: Store):
    _worker(store, "w1", "acct", last_hb=1000.0)
    _enqueue(store, "j1", "acct", max_attempts=1)  # next attempt exhausts it
    store.lease_one_job(worker_id="w1", capabilities=[[1, PLAT, "acct"]], now=1000.0)
    now = 1000.0 + default_lease_ttl_sec() + WORKER_RECLAIM_OFFLINE_SEC + 10
    reclaimed = store.reclaim_offline_jobs(now=now)
    assert reclaimed[0]["outcome"] == "dead_lettered"
    job = store.get_job("j1")
    assert job["status"] == "failed" and job["deadLetteredAt"] is not None


# ----- pinning -------------------------------------------------------------------

def test_pinned_job_only_releasable_by_original_worker(store: Store):
    _worker(store, "w1", "acct", last_hb=1000.0)
    _worker(store, "w2", "acct", last_hb=1000.0)  # artificial: 2 boxes, same account
    _enqueue(store, "j1", "acct")
    store.lease_one_job(worker_id="w1", capabilities=[[1, PLAT, "acct"]], now=1000.0)
    now = 1000.0 + default_lease_ttl_sec() + WORKER_RECLAIM_OFFLINE_SEC + 10
    store.reclaim_offline_jobs(now=now)  # requeued pinned to w1, with a retry backoff
    later = now + 10000  # past the requeue's retry_after_at backoff
    # A different box cannot pick up the pinned job.
    assert store.lease_one_job(worker_id="w2", capabilities=[[1, PLAT, "acct"]],
                               now=later) is None
    # The original box can — and the pin clears on lease.
    got = store.lease_one_job(worker_id="w1", capabilities=[[1, PLAT, "acct"]],
                              now=later)
    assert got is not None and got["id"] == "j1"
    assert store.get_job("j1")["pinnedWorkerId"] is None


# ----- pinned-to-dead-box dead-letter (Gap D) ------------------------------------

def _requeue_pinned(store: Store, jid="j1", wid="w1", account="acct",
                    max_attempts=DEFAULT_JOB_MAX_ATTEMPTS):
    """Arrange: a job leased then reclaimed → queued + pinned to its (offline) box."""
    _worker(store, wid, account, last_hb=1000.0)
    _enqueue(store, jid, account, max_attempts=max_attempts)
    store.lease_one_job(worker_id=wid, capabilities=[[1, PLAT, account]], now=1000.0)
    now = 1000.0 + default_lease_ttl_sec() + WORKER_RECLAIM_OFFLINE_SEC + 10
    store.reclaim_offline_jobs(now=now)
    assert store.get_job(jid)["pinnedWorkerId"] == wid  # pinned, queued
    return now


def test_pinned_queued_job_is_dead_lettered_after_box_dark_past_grace(store: Store):
    now = _requeue_pinned(store)
    # The box never returns; advance past the long dead-letter grace and sweep again.
    much_later = now + WORKER_PINNED_DEAD_LETTER_SEC + 10
    reclaimed = store.reclaim_offline_jobs(now=much_later)
    outcomes = {r["jobId"]: r["outcome"] for r in reclaimed}
    assert outcomes.get("j1") == "pinned_dead_lettered"
    job = store.get_job("j1")
    assert job["status"] == "failed" and job["deadLetteredAt"] is not None
    assert job["pinnedWorkerId"] is None


def test_pinned_queued_job_kept_while_box_only_briefly_dark(store: Store):
    now = _requeue_pinned(store)
    # Still within the dead-letter grace → the job waits for its own box, not killed.
    soon = now + WORKER_PINNED_DEAD_LETTER_SEC / 2
    assert store.reclaim_offline_jobs(now=soon) == []
    assert store.get_job("j1")["status"] == "queued"


def test_pinned_queued_job_dead_lettered_when_worker_row_is_gone(store: Store):
    now = _requeue_pinned(store)
    with store._tx() as c:  # the box's registration vanished entirely
        c.execute("DELETE FROM workers WHERE id='w1'")
    reclaimed = store.reclaim_offline_jobs(now=now + WORKER_PINNED_DEAD_LETTER_SEC + 10)
    assert any(r["outcome"] == "pinned_dead_lettered" for r in reclaimed)
    assert store.get_job("j1")["status"] == "failed"


# ----- alerting ------------------------------------------------------------------

def test_reclaim_alerts_when_offline_beyond_threshold(store: Store):
    _worker(store, "w1", "acct", last_hb=1000.0)
    _enqueue(store, "j1", "acct")
    store.lease_one_job(worker_id="w1", capabilities=[[1, PLAT, "acct"]], now=1000.0)
    now = 1000.0 + WORKER_RECLAIM_ALERT_SEC + 10  # dark well beyond the alert threshold
    store.reclaim_offline_jobs(now=now)
    flags = store.open_flags(org_id=1)
    assert any(f["kind"] == "worker_offline" for f in flags)


def test_reclaim_no_alert_below_threshold(store: Store):
    _worker(store, "w1", "acct", last_hb=1000.0)
    _enqueue(store, "j1", "acct")
    store.lease_one_job(worker_id="w1", capabilities=[[1, PLAT, "acct"]], now=1000.0)
    # Past offline+lease, but BELOW the 5-min alert threshold.
    now = 1000.0 + default_lease_ttl_sec() + WORKER_RECLAIM_OFFLINE_SEC + 5
    assert now - 1000.0 < WORKER_RECLAIM_ALERT_SEC
    store.reclaim_offline_jobs(now=now)
    flags = store.open_flags(org_id=1)
    assert not any(f["kind"] == "worker_offline" for f in flags)


# ----- ReclaimManager daemon -----------------------------------------------------

def test_reclaim_manager_tick_reclaims(tmp_path: Path):
    from aizu.reclaim_manager import ReclaimManager
    db = str(tmp_path / "rm.db")
    s = Store(db)
    _worker(s, "w1", "acct", last_hb=1000.0)
    _enqueue(s, "j1", "acct")
    s.lease_one_job(worker_id="w1", capabilities=[[1, PLAT, "acct"]], now=1000.0)
    s.close()
    fixed_now = 1000.0 + default_lease_ttl_sec() + WORKER_RECLAIM_OFFLINE_SEC + 10
    mgr = ReclaimManager(db_path=db, clock=lambda: fixed_now)
    reclaimed = mgr.tick()
    assert [r["jobId"] for r in reclaimed] == ["j1"]
    s2 = Store(db)
    assert s2.get_job("j1")["status"] == "queued"
    s2.close()


def test_reclaim_manager_tick_noop_when_nothing_stranded(tmp_path: Path):
    from aizu.reclaim_manager import ReclaimManager
    db = str(tmp_path / "rm2.db")
    s = Store(db)
    _worker(s, "w1", "acct", last_hb=1000.0)
    _enqueue(s, "j1", "acct")  # queued, never leased → nothing to reclaim
    s.close()
    mgr = ReclaimManager(db_path=db, clock=lambda: 9999.0)
    assert mgr.tick() == []


# ----- startup orphan-session reconcile (FIX 2) ----------------------------------

def _running_session(s: Store, sid: str, *, run_id=None, campaign_id="c-acme"):
    """Seed a session row at status='running', ended_at NULL (start_session shape),
    optionally stamped with a run_id (the fleet correlation key)."""
    s.start_session(sid, campaign_id, platform=PLAT, run_id=run_id)


def _job_for_run(s: Store, jid: str, run_id: str, status: str, *,
                 dead_lettered=False):
    """Enqueue a job whose spec carries `run_id`, then force it to `status` (and an
    optional dead-letter stamp) — directly, since enqueue_job always starts queued.
    Sessions correlate to fleet jobs by run_id, NOT session_id (empty until ack)."""
    s.enqueue_job(job_id=jid, campaign_id="c-acme", platform=PLAT,
                  required_account_handle="acct", org_id=1,
                  spec={"target_leads": 1, "engine_mode": "harvest",
                        "soul_text": "x", "run_id": run_id})
    with s._tx() as c:
        c.execute("UPDATE jobs SET status=?, dead_lettered_at=? WHERE id=?",
                  (status, (1234.0 if dead_lettered else None), jid))


def test_reconcile_closes_orphaned_running_session(store: Store):
    _running_session(store, "s-orphan")
    n = store.reconcile_orphan_sessions(now=5000.0)
    assert n == 1
    row = store.get_session("s-orphan")
    assert row["status"] == "halted"
    assert row["ended_at"] == 5000.0
    assert "reconciled" in (row["halt_reason"] or "")


def test_reconcile_leaves_completed_session_untouched(store: Store):
    _running_session(store, "s-done")
    store.end_session("s-done", "completed")
    before = store.get_session("s-done")
    n = store.reconcile_orphan_sessions(now=5000.0)
    assert n == 0
    after = store.get_session("s-done")
    assert after["status"] == "completed"
    assert after["ended_at"] == before["ended_at"]
    assert after["halt_reason"] == before["halt_reason"]


def test_reconcile_excludes_session_of_in_flight_job(store: Store):
    # A 'running' session whose run_id belongs to a still-in-flight (leased) job is a
    # live fleet run, not an orphan — it must NOT be reconciled. This holds even though
    # jobs.session_id is still empty (a fleet job populates it only at ack), which the
    # old session_id-based exclusion could not protect.
    _running_session(store, "s-inflight", run_id="run-live")
    _job_for_run(store, "j-live", "run-live", "leased")
    n = store.reconcile_orphan_sessions(now=5000.0)
    assert n == 0
    assert store.get_session("s-inflight")["status"] == "running"


def test_reconcile_reclaims_session_of_terminal_job(store: Store):
    # A 'running' session whose run_id maps ONLY to a 'done' job is an orphan (the job
    # is terminal, so no live run backs the session) → reconcile it.
    _running_session(store, "s-doneJob", run_id="run-done")
    _job_for_run(store, "j-done", "run-done", "done")
    n = store.reconcile_orphan_sessions(now=5000.0)
    assert n == 1
    assert store.get_session("s-doneJob")["status"] == "halted"


def test_reconcile_reclaims_session_of_dead_lettered_job(store: Store):
    # Reproduces the exact live shape: a job status='failed' whose session run_id is
    # NOT in the live set → its orphaned 'running' session must be reconciled.
    _running_session(store, "s-deadJob", run_id="run-dead")
    _job_for_run(store, "j-dead", "run-dead", "failed", dead_lettered=True)
    n = store.reconcile_orphan_sessions(now=5000.0)
    assert n == 1
    assert store.get_session("s-deadJob")["status"] == "halted"


# ----- v20: active_run_ids exclusion (periodic-safe reconcile, SessionWatchdog) --


def test_reconcile_excludes_session_of_currently_active_in_process_run(store: Store):
    # A 'running' session whose run_id is NOT in the jobs table at all (an
    # in-process RunManager run, never a fleet job) must still be spared when its
    # run_id is passed as currently active — this is what makes it safe for
    # SessionWatchdog to call reconcile_orphan_sessions every tick, not just once
    # at startup.
    _running_session(store, "s-active", run_id="run-inproc")
    n = store.reconcile_orphan_sessions(now=5000.0, active_run_ids=frozenset({"run-inproc"}))
    assert n == 0
    assert store.get_session("s-active")["status"] == "running"


def test_reconcile_reclaims_once_active_run_ids_no_longer_includes_it(store: Store):
    # The same session, once its run is no longer the active one (e.g. the next
    # tick after the run finished without closing its session row) — no longer
    # protected, gets reconciled exactly like any other stale session.
    _running_session(store, "s-nowstale", run_id="run-inproc")
    n = store.reconcile_orphan_sessions(now=5000.0, active_run_ids=frozenset())
    assert n == 1
    assert store.get_session("s-nowstale")["status"] == "halted"


# ----- job-failure paths close the orphaned session (no restart needed) ----------

def _enqueue_run(s: Store, jid: str, run_id: str, account: str = "acct", *,
                 max_attempts=DEFAULT_JOB_MAX_ATTEMPTS):
    """Enqueue a job whose spec carries `run_id` (fleet correlation key)."""
    return s.enqueue_job(job_id=jid, campaign_id="c-acme", platform=PLAT,
                         required_account_handle=account, org_id=1,
                         spec={"target_leads": 1, "engine_mode": "harvest",
                               "soul_text": "x", "run_id": run_id},
                         max_attempts=max_attempts)


def test_nack_dead_letter_closes_run_session(store: Store):
    # A permanently-failed (poison) job must close the 'running' session its dead
    # worker child stranded — otherwise it shows as live forever.
    _worker(store, "w1", "acct")
    _enqueue_run(store, "j1", "run-x", max_attempts=1)
    store.lease_one_job(worker_id="w1", capabilities=[[1, PLAT, "acct"]], now=1000.0)
    store.start_session("s1", "c-acme", platform=PLAT, run_id="run-x")
    out = store.nack_job(job_id="j1", worker_id="w1", reason="boom",
                         poison=True, now=2000.0)
    assert out["outcome"] == "dead_lettered"
    row = store.get_session("s1")
    assert row["status"] == "halted" and row["ended_at"] == 2000.0
    assert "dead-lettered" in (row["halt_reason"] or "")


def test_nack_requeue_closes_prior_attempt_session(store: Store):
    # The bug: each retry mints a fresh session under the SAME run_id while the prior
    # attempt's session stays 'running'. After requeue closes the prior one, a fresh
    # start_session leaves EXACTLY ONE 'running' row for that run_id.
    _worker(store, "w1", "acct")
    _enqueue_run(store, "j1", "run-trio")
    store.lease_one_job(worker_id="w1", capabilities=[[1, PLAT, "acct"]], now=1000.0)
    store.start_session("s-attempt1", "c-acme", platform=PLAT, run_id="run-trio")
    out = store.nack_job(job_id="j1", worker_id="w1", reason="crash", now=1500.0)
    assert out["outcome"] == "requeued"
    assert store.get_session("s-attempt1")["status"] == "halted"
    # Next attempt opens a new session under the same run_id.
    store.start_session("s-attempt2", "c-acme", platform=PLAT, run_id="run-trio")
    running = [r for r in ("s-attempt1", "s-attempt2")
               if store.get_session(r)["status"] == "running"]
    assert running == ["s-attempt2"]


def test_reclaim_expired_lease_closes_session(store: Store):
    _worker(store, "w1", "acct", last_hb=1000.0)
    _enqueue_run(store, "j1", "run-r")
    store.lease_one_job(worker_id="w1", capabilities=[[1, PLAT, "acct"]], now=1000.0)
    store.start_session("s1", "c-acme", platform=PLAT, run_id="run-r")
    now = 1000.0 + default_lease_ttl_sec() + 10
    reclaimed = store.reclaim_offline_jobs(now=now)
    assert reclaimed[0]["outcome"] == "requeued"
    assert store.get_session("s1")["status"] == "halted"


def test_reclaim_pinned_dead_closes_session(store: Store):
    # A queued job pinned to a long-dead box is dead-lettered by the second pass; its
    # abandoned session must close too.
    _worker(store, "w1", "acct", last_hb=1000.0)
    _enqueue_run(store, "j1", "run-p")
    store.lease_one_job(worker_id="w1", capabilities=[[1, PLAT, "acct"]], now=1000.0)
    store.start_session("s1", "c-acme", platform=PLAT, run_id="run-p")
    # First reclaim requeues it PINNED to w1 (also closes the session)...
    now = 1000.0 + default_lease_ttl_sec() + 10
    store.reclaim_offline_jobs(now=now)
    # ...re-open a session as if a later attempt started, then let the box go dark past
    # the pinned dead-letter grace so the second pass dead-letters the queued pinned job.
    store.start_session("s2", "c-acme", platform=PLAT, run_id="run-p")
    later = now + WORKER_PINNED_DEAD_LETTER_SEC + 10
    reclaimed = store.reclaim_offline_jobs(now=later)
    assert any(r["outcome"] == "pinned_dead_lettered" for r in reclaimed)
    assert store.get_session("s2")["status"] == "halted"
