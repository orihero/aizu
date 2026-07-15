"""Gap A — fleet run live activity feed (run_events sync-back).

A distributed worker emits run_events into its OWN local store during a leased job.
They reach the cloud's activity feed via the job heartbeat: the worker ships new events
each beat and `store.sync_run_events` inserts them under the JOB's own run_id/org/campaign
(FORCED — the BOLA guard, exactly like the lead sync). The org's `/api/run/activity`
drawer then shows the fleet run live.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reelradar.core.store import MAX_RUN_EVENTS_SYNC, Store

PLAT = "instagram"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(str(tmp_path / "events.db"))
    s.upsert_campaign_meta("c-acme", org_id=1, display_name="Acme")
    yield s
    s.close()


def _ev(seq: int, **over) -> dict:
    base = {"seq": seq, "phase": "scan", "level": "info",
            "message": f"event {seq}", "detail": None,
            "sessionId": "s-1", "createdAt": 900.0 + seq}
    base.update(over)
    return base


# ----- sync_run_events -------------------------------------------------------

def test_sync_inserts_events_under_forced_org_and_campaign(store: Store):
    n = store.sync_run_events("run-1", [_ev(1), _ev(2)], org_id=1, campaign_id="c-acme")
    assert n == 2
    rows = store.fetch_run_events("run-1")
    assert [r["seq"] for r in rows] == [1, 2]
    assert [r["message"] for r in rows] == ["event 1", "event 2"]


def test_sync_is_org_scoped_on_read(store: Store):
    store.sync_run_events("run-1", [_ev(1)], org_id=1, campaign_id="c-acme")
    # A different org must not see another org's run events.
    assert store.fetch_run_events("run-1", org_id=1)
    assert store.fetch_run_events("run-1", org_id=2) == []


def test_sync_is_idempotent_on_run_session_seq(store: Store):
    store.sync_run_events("run-1", [_ev(1), _ev(2)], org_id=1, campaign_id="c-acme")
    # A heartbeat retry re-ships the same batch (+ one new) — no duplicate lines.
    added = store.sync_run_events("run-1", [_ev(1), _ev(2), _ev(3)],
                                  org_id=1, campaign_id="c-acme")
    assert added == 1  # only the genuinely-new event 3
    rows = store.fetch_run_events("run-1")
    assert [r["seq"] for r in rows] == [1, 2, 3]


def test_sync_distinguishes_same_seq_across_sessions(store: Store):
    # A per-session seq resets to 1 in each session — (run, session, seq) is the key,
    # so seq=1 in two different sessions are two distinct events, not a dup.
    store.sync_run_events("run-1", [_ev(1, sessionId="s-a"), _ev(1, sessionId="s-b")],
                          org_id=1, campaign_id="c-acme")
    assert len(store.fetch_run_events("run-1")) == 2


def test_sync_skips_non_dict_rows_without_failing(store: Store):
    n = store.sync_run_events("run-1", [_ev(1), "nope", 42, _ev(2)],
                              org_id=1, campaign_id="c-acme")
    assert n == 2


def test_sync_empty_or_bad_input_is_noop(store: Store):
    assert store.sync_run_events("run-1", [], org_id=1, campaign_id="c-acme") == 0
    assert store.sync_run_events("run-1", None, org_id=1, campaign_id="c-acme") == 0  # type: ignore[arg-type]


def test_sync_caps_the_batch(store: Store):
    events = [_ev(i) for i in range(MAX_RUN_EVENTS_SYNC + 25)]
    n = store.sync_run_events("run-1", events, org_id=1, campaign_id="c-acme")
    assert n == MAX_RUN_EVENTS_SYNC
