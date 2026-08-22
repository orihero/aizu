"""B9 fleet spend roll-up — the store layer.

The cap the engine enforces is `store.total_spend(campaign_id) >= spend_cap_usd`, read
from whichever DB the process opened. On a worker box that is the BOX-LOCAL `AIZU_DB`,
so before this change a campaign that had already burned its budget on box A read $0 on
box B and the ceiling silently reset per machine. The fix is two-directional:

  - `lease_one_job` ships the campaign's CLOUD spend total as `priorSpendUsd`, and
  - `ack_job`/`nack_job` roll the box's spend DELTA back into the cloud `spend_log`.

The load-bearing properties here are the BOLA forcing (shared with `_sync_acked_leads`)
and the SAME-DATABASE guard: `spend_log` has an AUTOINCREMENT PK and no unique key, so
unlike the idempotent lead sync a second insert DOUBLES a campaign's spend — and the
worker's db_path can genuinely BE the cloud DB (AIZU_DB defaults to the same `aizu.db`
filename the bridge uses).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from aizu.core.store import MAX_SYNC_SPEND_ROWS, Store

PLAT = "instagram"
CAP_IG = [[1, PLAT, "acme_handle"]]


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(str(tmp_path / "spend.db"))
    s.upsert_campaign_meta("c-acme", org_id=1, display_name="Acme")
    yield s
    s.close()


def _register(s: Store, worker_id="w1") -> None:
    s.register_worker(worker_id=worker_id, token=f"tok-{worker_id}", org_id=1,
                      capabilities=CAP_IG)


def _enqueue_and_lease(s: Store, job_id="j1", worker_id="w1", max_attempts=5) -> dict:
    s.enqueue_job(job_id=job_id, campaign_id="c-acme", platform=PLAT,
                  required_account_handle="acme_handle", org_id=1,
                  spec={"target_leads": 1, "engine_mode": "harvest", "soul_text": "x"},
                  max_attempts=max_attempts)
    return s.lease_one_job(worker_id=worker_id, capabilities=CAP_IG, now=1000.0)


# ----- the worker-side read helpers (max_spend_id / spend_since) -----------------

def test_spend_since_rolls_up_only_rows_after_the_cursor(store: Store):
    store.log_spend("c-acme", "match", 0.10, model="m1")   # before the cursor
    cursor = store.max_spend_id("c-acme")
    store.log_spend("c-acme", "match", 0.20, model="m1")
    store.log_spend("c-acme", "match", 0.05, model="m1")
    store.log_spend("c-acme", "vision", 0.40, model="m2")
    store.log_spend("c-other", "match", 9.99, model="m1")  # another campaign

    rows = store.spend_since("c-acme", cursor)

    by_stage = {(r["stage"], r["model"]): r["usd"] for r in rows}
    assert by_stage == {("match", "m1"): pytest.approx(0.25),
                        ("vision", "m2"): pytest.approx(0.40)}
    assert store.total_spend("c-acme") == pytest.approx(0.75)


def test_spend_since_reports_the_earliest_created_at_per_group(store: Store):
    cursor = store.max_spend_id("c-acme")
    with_conn = store._conn
    for at in (100.0, 500.0, 300.0):
        with_conn.execute(
            """INSERT INTO spend_log(campaign_id, session_id, stage, model, usd, created_at)
               VALUES('c-acme', NULL, 'match', 'm1', 0.1, ?)""", (at,))
    with_conn.commit()

    rows = store.spend_since("c-acme", cursor)

    # MIN, not MAX/now: spend_by_day buckets on created_at, so a run that spanned
    # midnight must not have its whole cost parked on the ack day.
    assert len(rows) == 1 and rows[0]["at"] == 100.0


def test_spend_since_is_empty_when_nothing_new(store: Store):
    store.log_spend("c-acme", "match", 0.10, model="m1")
    assert store.spend_since("c-acme", store.max_spend_id("c-acme")) == []


def test_max_spend_id_is_an_id_cursor_scoped_to_the_campaign(store: Store):
    # An id cursor (not a run_id join) is deliberate: a requeued attempt REUSES the job's
    # run_id, so a run-scoped query would re-report attempt 1's rows on a same-box retry.
    assert store.max_spend_id("c-acme") == 0
    store.log_spend("c-other", "match", 0.10, model="m1")
    assert store.max_spend_id("c-acme") == 0     # another campaign never moves our mark
    store.log_spend("c-acme", "match", 0.10, model="m1")
    assert store.max_spend_id("c-acme") == 2


def _session(s: Store, session_id: str, run_id: str) -> None:
    s._conn.execute(
        """INSERT INTO sessions(session_id, campaign_id, platform, started_at, run_id)
           VALUES(?,?,?,?,?)""", (session_id, "c-acme", PLAT, 1.0, run_id))
    s._conn.commit()


def test_overlapping_runs_on_one_campaign_do_not_double_report(store: Store):
    """REVIEW FIX. `JobSpec.lock_key()` is `org-platform-account`, so it serialises per
    (org, PLATFORM), NOT per campaign: two jobs for the SAME campaign on DIFFERENT
    platforms take different single-flight locks and can overlap on one box. Their id
    windows then contain each other's rows — and no choice of cursor can prevent that,
    since B's rows land inside A's window either way. The cloud `spend_log` has an
    AUTOINCREMENT PK and no unique key, so BOTH reports get inserted: the campaign's
    spend is inflated and its effective cap correspondingly halved."""
    _session(store, "s-a", "run-a")
    _session(store, "s-b", "run-b")
    cursor_a = store.max_spend_id("c-acme")          # job A (run-a) starts
    store.log_spend("c-acme", "match", 1.00, model="m1", session_id="s-a")
    cursor_b = store.max_spend_id("c-acme")          # job B (run-b) starts, A still live
    store.log_spend("c-acme", "match", 2.00, model="m1", session_id="s-b")

    a = sum(r["usd"] for r in store.spend_since("c-acme", cursor_a, "run-a"))
    b = sum(r["usd"] for r in store.spend_since("c-acme", cursor_b, "run-b"))

    assert a == pytest.approx(1.00)     # A reports only its own dollar...
    assert b == pytest.approx(2.00)     # ...and B only its two
    assert a + b == pytest.approx(3.00)  # pre-fix this was $5.00 for $3.00 of real spend


def test_spend_since_keeps_rows_that_belong_to_no_session(store: Store):
    # An LLM call made outside a session cannot be attributed to another run, so it must
    # never be dropped by the run filter — that would lose real money from the roll-up.
    _session(store, "s-other", "run-other")
    cursor = store.max_spend_id("c-acme")
    store.log_spend("c-acme", "match", 0.40, model="m1")                     # no session
    store.log_spend("c-acme", "match", 5.00, model="m1", session_id="s-other")

    rows = store.spend_since("c-acme", cursor, "run-mine")

    assert sum(r["usd"] for r in rows) == pytest.approx(0.40)


def test_spend_since_keeps_a_prior_attempt_of_the_same_run(store: Store):
    # A requeue REUSES the run_id and the sidecar parks its cursor, so attempt 1's
    # unbanked dollars must still ride attempt 2's report.
    _session(store, "s-1", "run-x")
    cursor = store.max_spend_id("c-acme")
    store.log_spend("c-acme", "match", 1.00, model="m1", session_id="s-1")   # attempt 1
    _session(store, "s-2", "run-x")
    store.log_spend("c-acme", "match", 2.00, model="m1", session_id="s-2")   # attempt 2

    rows = store.spend_since("c-acme", cursor, "run-x")

    assert sum(r["usd"] for r in rows) == pytest.approx(3.00)


# ----- database_id (the same-database sentinel) ----------------------------------

def test_database_id_is_stable_across_store_instances(tmp_path: Path):
    path = str(tmp_path / "id.db")
    a = Store(path)
    first = a.database_id()
    assert first and a.database_id() == first      # cached, same value
    a.close()
    b = Store(path)
    try:
        assert b.database_id() == first            # persisted, not re-minted
    finally:
        b.close()


def test_database_id_differs_between_databases(tmp_path: Path):
    a, b = Store(str(tmp_path / "a.db")), Store(str(tmp_path / "b.db"))
    try:
        assert a.database_id() != b.database_id()
    finally:
        a.close()
        b.close()


# ----- lease: priorSpendUsd ------------------------------------------------------

def test_lease_carries_the_campaigns_cloud_spend_total(store: Store):
    store.log_spend("c-acme", "match", 3.00, model="m1")
    store.log_spend("c-acme", "vision", 1.50, model="m2")
    store.log_spend("c-other", "match", 99.0, model="m1")  # another campaign, excluded
    _register(store)

    lease = _enqueue_and_lease(store)

    assert lease["priorSpendUsd"] == pytest.approx(4.50)


def test_lease_prior_spend_is_zero_for_a_fresh_campaign(store: Store):
    _register(store)
    assert _enqueue_and_lease(store)["priorSpendUsd"] == 0.0


# ----- ack roll-up ---------------------------------------------------------------

def test_ack_rolls_spend_up_under_the_jobs_campaign(store: Store):
    _register(store)
    _enqueue_and_lease(store)

    ok = store.ack_job(
        job_id="j1", worker_id="w1", summary={"session_id": "s-1"},
        spend=[{"stage": "match", "model": "m1", "usd": 0.75, "at": 900.0},
               {"stage": "vision", "model": "m2", "usd": 0.25, "at": 950.0}],
        worker_db_id="a-different-box")

    assert ok is True
    assert store.total_spend("c-acme") == pytest.approx(1.00)
    rows = [dict(r) for r in store._conn.execute(
        "SELECT campaign_id, session_id, stage, model, usd, created_at FROM spend_log")]
    assert {r["stage"] for r in rows} == {"match", "vision"}
    assert all(r["campaign_id"] == "c-acme" for r in rows)
    assert all(r["session_id"] == "s-1" for r in rows)     # correlated to the run
    assert {r["created_at"] for r in rows} == {900.0, 950.0}  # honest timestamps kept


def test_ack_forces_the_jobs_campaign_ignoring_a_spoofed_one(store: Store):
    store.upsert_campaign_meta("c-victim", org_id=2, display_name="Victim")
    _register(store)
    _enqueue_and_lease(store)

    store.ack_job(job_id="j1", worker_id="w1", summary={},
                  spend=[{"stage": "match", "usd": 5.0,
                          "campaignId": "c-victim", "campaign_id": "c-victim"}],
                  worker_db_id="other-box")

    assert store.total_spend("c-acme") == pytest.approx(5.0)
    assert store.total_spend("c-victim") == 0.0


def test_ack_skips_malformed_spend_rows_without_failing(store: Store):
    _register(store)
    _enqueue_and_lease(store)

    ok = store.ack_job(
        job_id="j1", worker_id="w1", summary={},
        spend=["not-a-dict", {"stage": "match"}, {"stage": "match", "usd": "abc"},
               {"stage": "match", "usd": 0}, {"stage": "match", "usd": -3.0},
               {"stage": "match", "usd": float("inf")},
               {"usd": 0.5}],                      # no stage → defaults to 'fleet'
        worker_db_id="other-box")

    assert ok is True
    assert store.get_job("j1")["status"] == "done"   # the ack itself never fails
    assert store.total_spend("c-acme") == pytest.approx(0.5)
    stages = [r["stage"] for r in store._conn.execute("SELECT stage FROM spend_log")]
    assert stages == ["fleet"]


# A JSON integer with no float representation. json.loads accepts it (401 digits is well
# under the 4300-digit int-str limit), and `float()` on it raises OverflowError — which
# `_lead_float` did NOT catch, so it escaped the ack/nack transaction.
_HUGE_INT = int("9" * 401)


def test_ack_survives_a_spend_row_whose_usd_has_no_float_value(store: Store):
    """REVIEW FIX: `_lead_float` caught only TypeError/ValueError, so a huge-int `usd`
    raised OverflowError out of `_sync_acked_spend` INSIDE the ack transaction — rolling
    back the whole ack, discarding the run's synced leads, 500ing the worker, and leaving
    a finished job stuck `leased` until its lease expired. A malformed accounting row
    must be SKIPPED, never raised on."""
    _register(store)
    _enqueue_and_lease(store)

    ok = store.ack_job(job_id="j1", worker_id="w1", summary={},
                       spend=[{"stage": "match", "usd": _HUGE_INT},
                              {"stage": "match", "usd": 1.0, "at": _HUGE_INT},
                              {"stage": "match", "usd": 0.5}],
                       worker_db_id="other-box")

    assert ok is True
    assert store.get_job("j1")["status"] == "done"      # reached its terminal state
    # The unrepresentable usd row is dropped; the huge `at` degrades to now, not a raise.
    assert store.total_spend("c-acme") == pytest.approx(1.5)


def test_nack_survives_a_spend_row_whose_usd_has_no_float_value(store: Store):
    """The nack path is the sharper edge: the rollback left `attempts` un-incremented, so
    a genuinely poison job could never dead-letter — it just stayed `leased` until
    reclaim, forever."""
    _register(store)
    _enqueue_and_lease(store)

    out = store.nack_job(job_id="j1", worker_id="w1", reason="error",
                         spend=[{"stage": "match", "usd": _HUGE_INT},
                                {"stage": "match", "usd": 2.0, "at": _HUGE_INT}],
                         worker_db_id="other-box", now=2000.0)

    assert out["outcome"] == "requeued"
    assert out["attempts"] == 1                          # the attempt WAS counted
    assert store.get_job("j1")["status"] == "queued"     # not stranded 'leased'
    assert store.total_spend("c-acme") == pytest.approx(2.0)


def test_ack_clamps_a_future_created_at_to_now(store: Store):
    _register(store)
    _enqueue_and_lease(store)
    future = 4102444800.0  # 2100-01-01

    store.ack_job(job_id="j1", worker_id="w1", summary={},
                  spend=[{"stage": "match", "usd": 1.0, "at": future}],
                  worker_db_id="other-box")

    at = store._conn.execute("SELECT created_at FROM spend_log").fetchone()[0]
    assert at < future  # clamped to now — never parks spend in a day that hasn't happened


def test_ack_caps_the_spend_batch(store: Store):
    _register(store)
    _enqueue_and_lease(store)
    rows = [{"stage": f"s{i}", "usd": 1.0} for i in range(MAX_SYNC_SPEND_ROWS + 10)]

    store.ack_job(job_id="j1", worker_id="w1", summary={}, spend=rows,
                  worker_db_id="other-box")

    assert store.total_spend("c-acme") == pytest.approx(float(MAX_SYNC_SPEND_ROWS))


def test_a_second_ack_does_not_double_the_spend(store: Store):
    _register(store)
    _enqueue_and_lease(store)
    spend = [{"stage": "match", "usd": 2.0}]

    assert store.ack_job(job_id="j1", worker_id="w1", summary={}, spend=spend,
                         worker_db_id="other-box") is True
    # The exactly-once guarantee rides the ownership UPDATE's rowcount check: a replayed
    # ack finds no leased row of its own and writes NOTHING (spend_log is not idempotent).
    assert store.ack_job(job_id="j1", worker_id="w1", summary={}, spend=spend,
                         worker_db_id="other-box") is False
    assert store.total_spend("c-acme") == pytest.approx(2.0)


def test_ack_skips_the_rollup_when_the_worker_shares_this_database(store: Store):
    """BLOCKER: `AIZU_DB` defaults to the same `aizu.db` filename the bridge uses, and
    the same-box dev/desktop topology genuinely points both at one file. There the child
    ALREADY wrote these rows into this very table; rolling them up again would DOUBLE
    the campaign's spend and trip its cap at half the budget."""
    _register(store)
    _enqueue_and_lease(store)
    store.log_spend("c-acme", "match", 3.0, model="m1")  # what the child already wrote

    ok = store.ack_job(job_id="j1", worker_id="w1", summary={},
                       spend=[{"stage": "match", "model": "m1", "usd": 3.0}],
                       worker_db_id=store.database_id())

    assert ok is True
    assert store.total_spend("c-acme") == pytest.approx(3.0)  # NOT 6.0


def test_ack_still_rolls_up_when_no_db_id_is_reported(store: Store):
    # An older worker binary sends no dbId. Roll up (its db_path is presumably remote) —
    # the same-db case is the one that must be proven, not assumed.
    _register(store)
    _enqueue_and_lease(store)

    store.ack_job(job_id="j1", worker_id="w1", summary={},
                  spend=[{"stage": "match", "usd": 1.25}])

    assert store.total_spend("c-acme") == pytest.approx(1.25)


def test_ack_skips_the_rollup_for_a_campaign_with_no_org(store: Store):
    store.enqueue_job(job_id="j-orphan", campaign_id="c-nobody", platform=PLAT,
                      required_account_handle="acme_handle", org_id=1,
                      spec={"target_leads": 1})
    _register(store)
    store.lease_one_job(worker_id="w1", capabilities=CAP_IG, now=1000.0)

    assert store.ack_job(job_id="j-orphan", worker_id="w1", summary={},
                         spend=[{"stage": "match", "usd": 1.0}],
                         worker_db_id="other-box") is True
    assert store.total_spend("c-nobody") == 0.0


# ----- nack roll-up (BLOCKER: the widened SELECT) --------------------------------

def test_nack_requeue_records_spend_without_raising(store: Store):
    """BLOCKER regression: `nack_job`'s SELECT used to fetch only attempts/max_attempts/
    status/leased_by/spec. Reading `job["campaign_id"]`/`job["id"]` off that sqlite3.Row
    raises IndexError, which propagates out of `_tx()`, 500s the nack, and leaves the job
    LEASED until ReclaimManager notices — for every crashed fleet job carrying spend."""
    _register(store)
    _enqueue_and_lease(store)

    out = store.nack_job(job_id="j1", worker_id="w1", reason="error",
                         spend=[{"stage": "match", "usd": 1.5}],
                         worker_db_id="other-box", now=2000.0)

    assert out["outcome"] == "requeued"
    assert store.get_job("j1")["status"] == "queued"
    assert store.total_spend("c-acme") == pytest.approx(1.5)


def test_nack_dead_letter_records_spend_without_raising(store: Store):
    _register(store)
    _enqueue_and_lease(store, max_attempts=1)

    out = store.nack_job(job_id="j1", worker_id="w1", reason="boom", poison=True,
                         spend=[{"stage": "match", "usd": 2.5}],
                         worker_db_id="other-box", now=2000.0)

    assert out["outcome"] == "dead_lettered"
    assert store.get_job("j1")["status"] == "failed"
    assert store.total_spend("c-acme") == pytest.approx(2.5)


def test_an_ignored_nack_records_no_spend(store: Store):
    _register(store)
    _enqueue_and_lease(store)
    store.ack_job(job_id="j1", worker_id="w1", summary={})  # already terminal

    out = store.nack_job(job_id="j1", worker_id="w1", reason="late",
                         spend=[{"stage": "match", "usd": 4.0}],
                         worker_db_id="other-box", now=2000.0)

    assert out["outcome"] == "ignored"
    assert store.total_spend("c-acme") == 0.0


def test_nack_skips_the_rollup_when_the_worker_shares_this_database(store: Store):
    _register(store)
    _enqueue_and_lease(store)
    store.log_spend("c-acme", "match", 1.0, model="m1")

    store.nack_job(job_id="j1", worker_id="w1", reason="error",
                   spend=[{"stage": "match", "model": "m1", "usd": 1.0}],
                   worker_db_id=store.database_id(), now=2000.0)

    assert store.total_spend("c-acme") == pytest.approx(1.0)  # NOT 2.0


def test_database_id_minting_is_first_writer_wins(tmp_path: Path):
    """Shared-db topology: the worker's Store and the bridge's Store are the SAME file
    and can mint concurrently. A last-writer upsert would leave the two processes
    holding DIFFERENT ids — defeating the same-database guard exactly when it matters."""
    path = str(tmp_path / "race.db")
    a, b = Store(path), Store(path)
    try:
        first = a.database_id()
        assert b.database_id() == first     # b adopts a's id rather than overwriting it
    finally:
        a.close()
        b.close()
