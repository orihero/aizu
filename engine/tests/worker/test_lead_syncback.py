"""Phase 3 lead-body sync-back — the store layer.

A distributed worker captures leads into its OWN local DB during a leased job, then
ships them to the cloud on ack. `ack_job(..., leads=[...])` upserts each into the cloud
`matches` table. The load-bearing property is the BOLA guard: every synced lead is
FORCED under the job's own campaign/org, so a worker can never inject a lead into
another tenant's campaign, whatever it claims in the payload.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from aizu.core.store import MAX_SYNC_LEADS, Store

PLAT = "instagram"
CAP_IG = [[1, PLAT, "acme_handle"]]


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(str(tmp_path / "sync.db"))
    # Register the job's campaign to org 1 so org_for_campaign resolves on sync.
    s.upsert_campaign_meta("c-acme", org_id=1, display_name="Acme")
    yield s
    s.close()


def _register(s: Store, worker_id="w1") -> None:
    s.register_worker(worker_id=worker_id, token=f"tok-{worker_id}", org_id=1,
                      capabilities=CAP_IG)


def _enqueue_and_lease(s: Store, job_id="j1", worker_id="w1") -> None:
    s.enqueue_job(job_id=job_id, campaign_id="c-acme", platform=PLAT,
                  required_account_handle="acme_handle", org_id=1,
                  spec={"target_leads": 1, "engine_mode": "harvest", "soul_text": "x"})
    s.lease_one_job(worker_id=worker_id, capabilities=CAP_IG, now=1000.0)


def _lead(comment_id: str, **over) -> dict:
    base = {
        "commentId": comment_id, "reelId": "reel-1", "username": "buyer",
        "text": "need this now", "lang": "en", "score": 0.82,
        "reason": "high intent", "extracted": {"phone": "123"}, "tier": "local",
        "platform": PLAT, "sessionId": "s-loop-1", "capturedAt": 950.0,
    }
    base.update(over)
    return base


# ----- matches_for_run (the worker-side collection query) --------------------

def test_matches_for_run_joins_across_a_runs_sessions(store: Store):
    # Arrange: one run_id, two sessions, a match under each.
    store.start_session("s-a", "c-acme", PLAT, run_id="run-42", org_id=1)
    store.start_session("s-b", "c-acme", PLAT, run_id="run-42", org_id=1)
    store.start_session("s-other", "c-acme", PLAT, run_id="run-99", org_id=1)
    for sid, cid in [("s-a", "c1"), ("s-b", "c2"), ("s-other", "c3")]:
        store.upsert_match(campaign_id="c-acme", reel_id="r", comment_id=cid,
                           username="u", text="t", lang="en", score=0.7,
                           reason="ok", extracted={"k": "v"}, tier="local",
                           session_id=sid, platform=PLAT)

    # Act
    rows = store.matches_for_run("run-42")

    # Assert: both sessions of run-42, not the other run's match; extracted decoded.
    assert {r["comment_id"] for r in rows} == {"c1", "c2"}
    assert rows[0]["extracted"] == {"k": "v"}


def test_upsert_match_preserves_supplied_captured_at(store: Store):
    store.upsert_match(campaign_id="c-acme", reel_id="r", comment_id="c1",
                       username="u", text="t", lang="en", score=0.5, reason="ok",
                       extracted=None, tier="local", platform=PLAT, captured_at=123.0)
    assert store.matches("c-acme")[0]["captured_at"] == 123.0


# ----- ack_job lead sync -----------------------------------------------------

def test_ack_syncs_lead_bodies_under_the_jobs_campaign(store: Store):
    _register(store)
    _enqueue_and_lease(store)

    ok = store.ack_job(job_id="j1", worker_id="w1",
                       summary={"matches": 1, "session_id": "s-loop-1"},
                       leads=[_lead("cmt-1")])

    assert ok is True
    rows = store.matches("c-acme")
    assert len(rows) == 1
    m = rows[0]
    assert m["comment_id"] == "cmt-1"
    assert m["org_id"] == 1               # stamped from the job's campaign
    assert m["status"] == "new"           # fresh lead defaults to new
    assert m["captured_at"] == 950.0      # real capture time preserved
    assert m["extracted"] == {"phone": "123"}


def test_ack_forces_the_jobs_campaign_ignoring_a_spoofed_one(store: Store):
    # A second campaign owned by a DIFFERENT org — the worker must not reach it.
    store.upsert_campaign_meta("c-victim", org_id=2, display_name="Victim")
    _register(store)
    _enqueue_and_lease(store)

    store.ack_job(job_id="j1", worker_id="w1", summary={"session_id": "s-loop-1"},
                  leads=[_lead("cmt-x", campaignId="c-victim", campaign_id="c-victim")])

    # The lead landed under the job's campaign (org 1), NOT the spoofed victim campaign.
    assert [m["comment_id"] for m in store.matches("c-acme")] == ["cmt-x"]
    assert store.matches("c-victim") == []


def test_ack_skips_malformed_leads_without_failing(store: Store):
    _register(store)
    _enqueue_and_lease(store)

    ok = store.ack_job(
        job_id="j1", worker_id="w1", summary={"session_id": "s-loop-1"},
        leads=[_lead("good-1"), {"reelId": "r"}, "not-a-dict", {"commentId": "no-reel"}])

    assert ok is True
    assert [m["comment_id"] for m in store.matches("c-acme")] == ["good-1"]


def test_reack_is_idempotent_and_preserves_human_status(store: Store):
    _register(store)
    _enqueue_and_lease(store)
    store.ack_job(job_id="j1", worker_id="w1", summary={"session_id": "s-loop-1"},
                  leads=[_lead("cmt-1")])
    # A human works the lead in the cloud.
    store.set_status("c-acme", "cmt-1", "interested", platform=PLAT)

    # A duplicate ack (retry) is a no-op on the job and must not clobber the status.
    second = store.ack_job(job_id="j1", worker_id="w1",
                           summary={"session_id": "s-loop-1"}, leads=[_lead("cmt-1")])

    assert second is False
    assert store.matches("c-acme")[0]["status"] == "interested"


def test_ack_without_leads_still_works(store: Store):
    _register(store)
    _enqueue_and_lease(store)
    assert store.ack_job(job_id="j1", worker_id="w1",
                         summary={"session_id": "s-loop-1"}) is True
    assert store.matches("c-acme") == []


def test_ack_skips_sync_for_a_campaign_with_no_org(store: Store):
    # A job whose campaign resolves to no org must never write NULL-org leads.
    store.enqueue_job(job_id="j-orphan", campaign_id="c-orphan", platform=PLAT,
                      required_account_handle="acme_handle", org_id=1,
                      spec={"target_leads": 1, "engine_mode": "harvest", "soul_text": "x"})
    _register(store)
    store.lease_one_job(worker_id="w1", capabilities=CAP_IG, now=1000.0)

    ok = store.ack_job(job_id="j-orphan", worker_id="w1",
                       summary={"session_id": "s"}, leads=[_lead("cmt-1")])

    assert ok is True                       # the job still completes
    assert store.matches("c-orphan") == []  # but no orphan-org lead is written


def test_ack_drops_an_oversized_extracted_blob_but_keeps_the_lead(store: Store):
    _register(store)
    _enqueue_and_lease(store)
    huge = {"junk": "x" * 20_000}  # serializes well past MAX_EXTRACTED_BYTES

    store.ack_job(job_id="j1", worker_id="w1", summary={"session_id": "s"},
                  leads=[_lead("cmt-big", extracted=huge)])

    rows = store.matches("c-acme")
    assert [m["comment_id"] for m in rows] == ["cmt-big"]
    assert rows[0]["extracted"] in (None, {})  # blob dropped, lead kept


def test_ack_caps_an_overlong_string_field(store: Store):
    _register(store)
    _enqueue_and_lease(store)

    store.ack_job(job_id="j1", worker_id="w1", summary={"session_id": "s"},
                  leads=[_lead("cmt-long", text="y" * 50_000)])

    assert len(store.matches("c-acme")[0]["text"]) == 8192


def test_ack_caps_the_synced_batch(store: Store):
    _register(store)
    _enqueue_and_lease(store)
    leads = [_lead(f"cmt-{i}") for i in range(MAX_SYNC_LEADS + 25)]

    store.ack_job(job_id="j1", worker_id="w1", summary={"session_id": "s-loop-1"},
                  leads=leads)

    assert len(store.matches("c-acme")) == MAX_SYNC_LEADS


# ----- atomicity: job-done and lead sync commit (or roll back) together ------

def test_ack_lead_sync_is_atomic_with_job_completion(store: Store, monkeypatch):
    """A crash/failure while syncing leads must roll back the job-done marking too —
    the job stays leased so ReclaimManager requeues it and no lead batch is lost in a
    'done-but-leads-gone' limbo. Regression for the Phase-3 non-atomic sync residual.
    """
    _register(store)
    _enqueue_and_lease(store)

    # Fail the SECOND lead's write, simulating a mid-batch DB/infra failure.
    original = store._upsert_match_row
    calls = {"n": 0}

    def boom(c, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk gone")
        return original(c, **kw)

    monkeypatch.setattr(store, "_upsert_match_row", boom)

    with pytest.raises(RuntimeError):
        store.ack_job(job_id="j1", worker_id="w1", summary={"session_id": "s-loop-1"},
                      leads=[_lead("cmt-1"), _lead("cmt-2")])

    # The whole transaction rolled back: job is NOT done, and NEITHER lead persisted.
    assert store.get_job("j1")["status"] == "leased"
    assert store.matches("c-acme") == []


def test_ack_commits_job_and_leads_in_one_transaction(store: Store):
    """The happy path: after a successful ack the job is `done` AND every lead is
    present. (Atomic — there is no window where one is visible without the other.)"""
    _register(store)
    _enqueue_and_lease(store)

    ok = store.ack_job(job_id="j1", worker_id="w1", summary={"session_id": "s-loop-1"},
                       leads=[_lead("cmt-1"), _lead("cmt-2")])

    assert ok is True
    assert store.get_job("j1")["status"] == "done"
    assert {m["comment_id"] for m in store.matches("c-acme")} == {"cmt-1", "cmt-2"}


# ----- v17 found_by_models threading (model-comparison fan-out) --------------

def test_ack_syncs_found_by_models(store: Store):
    _register(store)
    _enqueue_and_lease(store)

    store.ack_job(job_id="j1", worker_id="w1", summary={"session_id": "s"},
                  leads=[_lead("cmt-1", foundByModels=["prod-model", "candidate-a"])])

    assert store.matches("c-acme")[0]["found_by_models"] == ["prod-model", "candidate-a"]


def test_ack_drops_a_non_list_found_by_models(store: Store):
    _register(store)
    _enqueue_and_lease(store)

    store.ack_job(job_id="j1", worker_id="w1", summary={"session_id": "s"},
                  leads=[_lead("cmt-1", foundByModels="not-a-list")])

    assert store.matches("c-acme")[0]["found_by_models"] in (None, [])


def test_ack_drops_non_string_entries_and_caps_length(store: Store):
    _register(store)
    _enqueue_and_lease(store)
    oversized = [f"model-{i}" for i in range(50)] + [123, None, {"x": 1}]

    store.ack_job(job_id="j1", worker_id="w1", summary={"session_id": "s"},
                  leads=[_lead("cmt-1", foundByModels=oversized)])

    found = store.matches("c-acme")[0]["found_by_models"]
    assert len(found) == 20
    assert all(isinstance(m, str) for m in found)
