"""Store-layer leasing for the distributed jobs table (BUILD-PLAN Phase 3, §5).

The load-bearing test is `test_concurrent_lease_has_exactly_one_winner`: SQLite has no
SELECT … FOR UPDATE SKIP LOCKED (BUILD-PLAN C2), so leasing relies on BEGIN IMMEDIATE
(`_tx_immediate`) + a conditional UPDATE + the rowcount guard. N threads racing one job
must produce exactly one winner.
"""
from __future__ import annotations

import tempfile
import threading
from pathlib import Path

import pytest

from reelradar.core.store import (DEFAULT_JOB_MAX_ATTEMPTS, Store,
                                   default_lease_ttl_sec, nack_backoff_sec)

PLAT = "instagram"
CAP_IG = [[1, PLAT, "acme_handle"]]            # org 1, instagram, one account
CAP_POOL = [[None, PLAT, "acme_handle"]]       # pool-wide instagram


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(str(tmp_path / "jobs.db"))
    yield s
    s.close()


def _register_worker(s: Store, worker_id: str, caps: list, *, org_id=1) -> None:
    s.register_worker(worker_id=worker_id, token=f"tok-{worker_id}", org_id=org_id,
                      capabilities=caps)


def _enqueue(s: Store, job_id: str, *, org_id=1, platform=PLAT,
             account="acme_handle", spec=None, max_attempts=DEFAULT_JOB_MAX_ATTEMPTS):
    return s.enqueue_job(
        job_id=job_id, campaign_id="c-acme", platform=platform,
        required_account_handle=account, org_id=org_id,
        spec=spec or {"target_leads": 1, "engine_mode": "harvest",
                      "soul_text": "be helpful"},
        max_attempts=max_attempts)


# ----- enqueue -------------------------------------------------------------------

def test_enqueue_creates_a_queued_job(store: Store):
    job = _enqueue(store, "j1")
    assert job["status"] == "queued"
    assert job["attempts"] == 0
    assert job["spec"]["target_leads"] == 1


def test_enqueue_is_idempotent_on_job_id(store: Store):
    store.enqueue_job(job_id="j1", campaign_id="c-acme", platform=PLAT,
                      spec={"target_leads": 1})
    store.enqueue_job(job_id="j1", campaign_id="c-acme", platform=PLAT,
                      spec={"target_leads": 99})  # ignored — same id
    assert store.get_job("j1")["spec"]["target_leads"] == 1


def test_enqueue_deduped_skips_when_an_active_job_exists(store: Store):
    # First fleet dispatch enqueues a job for the campaign.
    first = store.enqueue_job_deduped(
        job_id="j1", campaign_id="c-acme", platform=PLAT,
        spec={"target_leads": 5, "engine_mode": "harvest"})
    assert first is not None and first["status"] == "queued"

    # A rapid double-Run (fresh uuid job id) must be SKIPPED, not duplicated.
    second = store.enqueue_job_deduped(
        job_id="j2", campaign_id="c-acme", platform=PLAT,
        spec={"target_leads": 5, "engine_mode": "harvest"})
    assert second is None
    assert store.get_job("j2") is None


def test_enqueue_deduped_ignores_a_dead_lettered_or_done_job(store: Store):
    store.enqueue_job_deduped(job_id="j1", campaign_id="c-acme", platform=PLAT,
                              spec={"engine_mode": "harvest"})
    # Drive the job to a terminal state so it no longer blocks a fresh run.
    _register_worker(store, "w1", CAP_IG)
    store.lease_one_job(worker_id="w1", capabilities=CAP_IG, now=1000.0)
    store.ack_job(job_id="j1", worker_id="w1", summary={"session_id": "s"})

    # A new run for the same campaign now enqueues — the prior job is done.
    again = store.enqueue_job_deduped(job_id="j2", campaign_id="c-acme", platform=PLAT,
                                      spec={"engine_mode": "harvest"})
    assert again is not None and again["status"] == "queued"


def test_lease_payload_carries_run_id_from_spec(store: Store):
    _register_worker(store, "w1", CAP_IG)
    store.enqueue_job(job_id="j1", campaign_id="c-acme", platform=PLAT,
                      required_account_handle="acme_handle", org_id=1,
                      spec={"engine_mode": "harvest", "run_id": "run-xyz"})
    lease = store.lease_one_job(worker_id="w1", capabilities=CAP_IG, now=1000.0)
    assert lease is not None and lease["runId"] == "run-xyz"


def test_enqueue_deduped_is_per_campaign(store: Store):
    a = store.enqueue_job_deduped(job_id="ja", campaign_id="c-a", platform=PLAT,
                                  spec={"engine_mode": "harvest"})
    b = store.enqueue_job_deduped(job_id="jb", campaign_id="c-b", platform=PLAT,
                                  spec={"engine_mode": "harvest"})
    assert a is not None and b is not None  # different campaigns never collide


def test_count_capable_workers_matches_platform_org_and_account(store: Store):
    _register_worker(store, "w-ig", CAP_IG)
    _register_worker(store, "w-pool", CAP_POOL)
    assert store.count_capable_workers(platform=PLAT, org_id=1,
                                       account_handle="acme_handle") == 2
    # An account this fleet does not declare → no capable worker.
    assert store.count_capable_workers(platform=PLAT, org_id=1,
                                       account_handle="other") == 0
    # A platform nobody runs → none.
    assert store.count_capable_workers(platform="youtube", org_id=1) == 0


def test_count_capable_workers_ignores_revoked(store: Store):
    _register_worker(store, "w-ig", CAP_IG)
    store.revoke_worker("w-ig")
    assert store.count_capable_workers(platform=PLAT, org_id=1,
                                       account_handle="acme_handle") == 0


# ----- lease ---------------------------------------------------------------------

def test_lease_claims_a_matching_job(store: Store):
    _enqueue(store, "j1")
    lease = store.lease_one_job(worker_id="w1", capabilities=CAP_IG, now=1000.0)
    assert lease is not None
    assert lease["id"] == "j1"
    assert lease["soulText"] == "be helpful"
    assert lease["leaseExpiresAt"] == pytest.approx(1000.0 + default_lease_ttl_sec())
    row = store.get_job("j1")
    assert row["status"] == "leased" and row["leasedBy"] == "w1"


def test_lease_returns_none_when_no_capability_matches(store: Store):
    _enqueue(store, "j1", account="acme_handle")
    # Worker only declares a DIFFERENT account → cannot serve the pinned job.
    lease = store.lease_one_job(worker_id="w1",
                                capabilities=[[1, PLAT, "someone_else"]])
    assert lease is None
    assert store.get_job("j1")["status"] == "queued"


def test_lease_returns_none_for_empty_capabilities(store: Store):
    _enqueue(store, "j1")
    assert store.lease_one_job(worker_id="w1", capabilities=[]) is None


def test_lease_skips_jobs_deferred_by_retry_after_at(store: Store):
    _enqueue(store, "j1")
    # Nack into the future, then a lease before that time finds nothing.
    store.lease_one_job(worker_id="w1", capabilities=CAP_IG, now=1000.0)
    res = store.nack_job(job_id="j1", worker_id="w1", reason="daytime",
                         retry_after_at=5000.0, now=1000.0)
    assert res["outcome"] == "requeued"
    assert store.lease_one_job(worker_id="w1", capabilities=CAP_IG,
                               now=4000.0) is None
    # …and finds it once the retry window has passed.
    assert store.lease_one_job(worker_id="w1", capabilities=CAP_IG,
                               now=6000.0) is not None


def test_lease_reclaims_an_expired_lease(store: Store):
    _enqueue(store, "j1")
    ttl = default_lease_ttl_sec()
    first = store.lease_one_job(worker_id="w1", capabilities=CAP_IG, now=1000.0)
    assert first is not None
    # Before expiry: not leaseable again.
    assert store.lease_one_job(worker_id="w2", capabilities=CAP_IG,
                               now=1000.0 + ttl - 1) is None
    # After expiry: reclaimable.
    reclaimed = store.lease_one_job(worker_id="w2", capabilities=CAP_IG,
                                    now=1000.0 + ttl + 1)
    assert reclaimed is not None and reclaimed["id"] == "j1"
    assert store.get_job("j1")["leasedBy"] == "w2"


def test_lease_never_touches_a_dead_lettered_job(store: Store):
    _enqueue(store, "j1", max_attempts=1)
    store.lease_one_job(worker_id="w1", capabilities=CAP_IG, now=1000.0)
    store.nack_job(job_id="j1", worker_id="w1", reason="boom", now=1000.0)
    assert store.get_job("j1")["status"] == "failed"
    assert store.lease_one_job(worker_id="w1", capabilities=CAP_IG,
                               now=2000.0) is None


def test_concurrent_lease_has_exactly_one_winner():
    """N threads, one queued job → exactly one lease, the rest None (BUILD-PLAN C2)."""
    fd_dir = tempfile.mkdtemp()
    db_path = str(Path(fd_dir) / "race.db")
    seed = Store(db_path)
    seed.enqueue_job(job_id="j1", campaign_id="c-acme", platform=PLAT,
                     required_account_handle="acme_handle", org_id=1,
                     spec={"target_leads": 1})
    seed.close()

    n = 12
    results: list = [None] * n
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        s = Store(db_path)  # its OWN connection — that is the real race
        try:
            barrier.wait()
            results[i] = s.lease_one_job(worker_id=f"w{i}", capabilities=CAP_IG,
                                         now=1000.0)
        finally:
            s.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"expected exactly one winner, got {len(winners)}"


# ----- extend (job heartbeat) ----------------------------------------------------

def test_extend_lease_marks_running_and_pushes_the_deadline(store: Store):
    _enqueue(store, "j1")
    store.lease_one_job(worker_id="w1", capabilities=CAP_IG, now=1000.0)
    ok = store.extend_lease(job_id="j1", worker_id="w1", now=1030.0)
    assert ok is True
    row = store.get_job("j1")
    assert row["status"] == "running"
    assert row["leaseExpiresAt"] == pytest.approx(1030.0 + default_lease_ttl_sec())


def test_extend_lease_rejects_a_foreign_worker(store: Store):
    _enqueue(store, "j1")
    store.lease_one_job(worker_id="w1", capabilities=CAP_IG, now=1000.0)
    assert store.extend_lease(job_id="j1", worker_id="w2", now=1030.0) is False


def test_extend_lease_false_on_a_terminal_job(store: Store):
    _enqueue(store, "j1")
    store.lease_one_job(worker_id="w1", capabilities=CAP_IG, now=1000.0)
    store.ack_job(job_id="j1", worker_id="w1", summary={"matches": 2})
    assert store.extend_lease(job_id="j1", worker_id="w1", now=1030.0) is False


# ----- ack -----------------------------------------------------------------------

def test_ack_marks_done_and_is_idempotent(store: Store):
    _enqueue(store, "j1")
    store.lease_one_job(worker_id="w1", capabilities=CAP_IG, now=1000.0)
    assert store.ack_job(job_id="j1", worker_id="w1",
                         summary={"matches": 3, "session_id": "s-1"}) is True
    assert store.get_job("j1")["status"] == "done"
    # A second ack changes nothing and reports False (write-once mirror).
    assert store.ack_job(job_id="j1", worker_id="w1",
                         summary={"matches": 3, "session_id": "s-1"}) is False


def test_ack_mirrors_a_session_row(store: Store):
    _enqueue(store, "j1")
    store.lease_one_job(worker_id="w1", capabilities=CAP_IG, now=1000.0)
    store.ack_job(job_id="j1", worker_id="w1",
                  summary={"matches": 5, "reels_seen": 40, "spend_usd": 0.2,
                           "session_id": "s-mirror", "run_id": "r-1"})
    sess = store.get_session("s-mirror")
    assert sess is not None
    assert sess["matches"] == 5 and sess["status"] == "completed"


def test_ack_by_foreign_worker_is_rejected(store: Store):
    _enqueue(store, "j1")
    store.lease_one_job(worker_id="w1", capabilities=CAP_IG, now=1000.0)
    assert store.ack_job(job_id="j1", worker_id="w2", summary={}) is False
    assert store.get_job("j1")["status"] == "leased"


# ----- nack ----------------------------------------------------------------------

def test_nack_requeues_with_exponential_backoff(store: Store):
    _enqueue(store, "j1")
    store.lease_one_job(worker_id="w1", capabilities=CAP_IG, now=1000.0)
    res = store.nack_job(job_id="j1", worker_id="w1", reason="error", now=1000.0)
    assert res["outcome"] == "requeued"
    assert res["attempts"] == 1
    assert res["retryAfterAt"] == pytest.approx(1000.0 + nack_backoff_sec(1))
    assert store.get_job("j1")["status"] == "queued"


def test_nack_dead_letters_after_max_attempts(store: Store):
    _enqueue(store, "j1", max_attempts=2)
    # attempt 1 → requeue
    store.lease_one_job(worker_id="w1", capabilities=CAP_IG, now=1000.0)
    assert store.nack_job(job_id="j1", worker_id="w1", reason="e",
                          now=1000.0)["outcome"] == "requeued"
    # attempt 2 → reaches max_attempts → dead-letter
    store.lease_one_job(worker_id="w1", capabilities=CAP_IG, now=2000.0)
    res = store.nack_job(job_id="j1", worker_id="w1", reason="e", now=2000.0)
    assert res["outcome"] == "dead_lettered"
    assert store.get_job("j1")["status"] == "failed"
    assert store.get_job("j1")["deadLetteredAt"] is not None


def test_nack_poison_dead_letters_immediately(store: Store):
    _enqueue(store, "j1", max_attempts=5)
    store.lease_one_job(worker_id="w1", capabilities=CAP_IG, now=1000.0)
    res = store.nack_job(job_id="j1", worker_id="w1", reason="needs reconnect",
                         poison=True, now=1000.0)
    assert res["outcome"] == "dead_lettered"
    assert store.get_job("j1")["status"] == "failed"


def test_nack_honors_an_explicit_retry_after_at(store: Store):
    _enqueue(store, "j1")
    store.lease_one_job(worker_id="w1", capabilities=CAP_IG, now=1000.0)
    res = store.nack_job(job_id="j1", worker_id="w1", reason="daytime",
                         retry_after_at=9999.0, now=1000.0)
    assert res["retryAfterAt"] == 9999.0


def test_nack_by_foreign_worker_is_ignored(store: Store):
    _enqueue(store, "j1")
    store.lease_one_job(worker_id="w1", capabilities=CAP_IG, now=1000.0)
    res = store.nack_job(job_id="j1", worker_id="w2", reason="e", now=1000.0)
    assert res["outcome"] == "ignored"
    assert store.get_job("j1")["status"] == "leased"  # untouched


# ----- FIX 2: fleet-run lifecycle reads (get_job_for_run / last_event_at / active) --

def test_get_job_for_run_finds_by_spec_run_id(store: Store):
    store.enqueue_job(job_id="j1", campaign_id="c-acme", platform=PLAT, org_id=1,
                      spec={"run_id": "run-xyz", "target_leads": 3})
    job = store.get_job_for_run("run-xyz", org_id=1)
    assert job is not None
    assert job["id"] == "j1"
    assert job["status"] == "queued"
    assert job["spec"]["run_id"] == "run-xyz"


def test_get_job_for_run_returns_latest_when_multiple(store: Store):
    store.enqueue_job(job_id="old", campaign_id="c-acme", platform=PLAT, org_id=1,
                      spec={"run_id": "run-dup"})
    store.enqueue_job(job_id="new", campaign_id="c-acme", platform=PLAT, org_id=1,
                      spec={"run_id": "run-dup"})
    # created_at is time.time() at insert; the second insert is newest.
    job = store.get_job_for_run("run-dup", org_id=1)
    assert job is not None and job["id"] == "new"


def test_get_job_for_run_is_org_scoped_bola(store: Store):
    store.enqueue_job(job_id="j1", campaign_id="c-acme", platform=PLAT, org_id=1,
                      spec={"run_id": "run-secret"})
    # Same run id, different org must NOT resolve (cross-tenant read is denied).
    assert store.get_job_for_run("run-secret", org_id=2) is None


def test_get_job_for_run_returns_none_when_absent(store: Store):
    assert store.get_job_for_run("nope", org_id=1) is None


def test_last_event_at_for_run_returns_max(store: Store):
    store.emit_run_event("run-e", 1, "lifecycle", "info", "a", campaign_id="c-acme",
                         org_id=1)
    store.emit_run_event("run-e", 2, "lifecycle", "info", "b", campaign_id="c-acme",
                         org_id=1)
    last = store.last_event_at_for_run("run-e", org_id=1)
    assert last is not None and isinstance(last, float)
    # Org-scoping: another org sees nothing for this run.
    assert store.last_event_at_for_run("run-e", org_id=2) is None


def test_last_event_at_for_run_none_when_no_events(store: Store):
    assert store.last_event_at_for_run("empty-run", org_id=1) is None


def test_active_fleet_runs_for_org_maps_campaign_to_run_id(store: Store):
    store.enqueue_job(job_id="j1", campaign_id="c-one", platform=PLAT, org_id=1,
                      spec={"run_id": "run-1"})
    store.enqueue_job(job_id="j2", campaign_id="c-two", platform=PLAT, org_id=1,
                      spec={"run_id": "run-2"})
    active = store.active_fleet_runs_for_org(1)
    assert active == {"c-one": "run-1", "c-two": "run-2"}


def test_active_fleet_runs_for_org_excludes_terminal_jobs(store: Store):
    _register_worker(store, "w1", CAP_IG)
    store.enqueue_job(job_id="j1", campaign_id="c-acme", platform=PLAT, org_id=1,
                      required_account_handle="acme_handle",
                      spec={"run_id": "run-done"})
    store.lease_one_job(worker_id="w1", capabilities=CAP_IG, now=1000.0)
    store.ack_job(job_id="j1", worker_id="w1", summary={"session_id": "s"})
    # The job is now done → no longer an active fleet run.
    assert store.active_fleet_runs_for_org(1) == {}


def test_active_fleet_runs_for_org_keeps_newest_per_campaign(store: Store):
    store.enqueue_job(job_id="old", campaign_id="c-acme", platform=PLAT, org_id=1,
                      spec={"run_id": "run-old"})
    store.enqueue_job(job_id="new", campaign_id="c-acme", platform=PLAT, org_id=1,
                      spec={"run_id": "run-new"})
    active = store.active_fleet_runs_for_org(1)
    assert active == {"c-acme": "run-new"}
