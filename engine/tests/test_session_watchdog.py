"""SessionWatchdog (hang-prevention fix #4) — the tick() pass that halts a
session whose heartbeat has gone stale and stops the matching in-process run,
modeled on reclaim_manager's tick()/fake-clock test style."""
from __future__ import annotations

import os
import tempfile

import pytest

from aizu.core.store import Store
from aizu.session_watchdog import STALL_TIMEOUT_SEC, SessionWatchdog


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = Store(path)
    store.close()
    yield path


class _FakeRunManager:
    """Records stop() calls; status() reports whichever run_id was injected as
    'currently active' (or None)."""

    def __init__(self, active_run_id=None):
        self.active_run_id = active_run_id
        self.stop_calls = 0

    def status(self, org_id=None):
        active = None
        if self.active_run_id:
            active = {"id": self.active_run_id, "scope": "campaign",
                     "campaignId": "c", "mode": "live", "startedAt": "x",
                     "paused": False, "launchSource": "manual"}
        return {"active": active, "recent": []}

    def stop(self, org_id=None):
        self.stop_calls += 1
        return True, None


def _seed_session(db_path, session_id, *, run_id=None, stale_by=0.0, now):
    store = Store(db_path)
    try:
        store.start_session(session_id, "c", run_id=run_id)
        if stale_by:
            with store._tx() as c:
                c.execute(
                    "UPDATE sessions SET last_activity_at=? WHERE session_id=?",
                    (now - stale_by, session_id))
    finally:
        store.close()


def test_tick_halts_stale_session_and_stops_matching_active_run(db_path):
    now = 10_000.0
    _seed_session(db_path, "s-stale", run_id="run-x",
                  stale_by=STALL_TIMEOUT_SEC + 30, now=now)
    rm = _FakeRunManager(active_run_id="run-x")
    watchdog = SessionWatchdog(db_path=db_path, run_manager=rm, clock=lambda: now)

    halted = watchdog.tick()

    assert halted == ["s-stale"]
    assert rm.stop_calls == 1
    store = Store(db_path)
    try:
        row = store.get_session("s-stale")
        assert row["status"] == "halted"
        assert "stalled" in (row["halt_reason"] or "")
    finally:
        store.close()


def test_tick_leaves_fresh_session_untouched(db_path):
    now = 10_000.0
    _seed_session(db_path, "s-fresh", run_id="run-x", stale_by=0.0, now=now)
    rm = _FakeRunManager(active_run_id="run-x")
    watchdog = SessionWatchdog(db_path=db_path, run_manager=rm, clock=lambda: now)

    halted = watchdog.tick()

    assert halted == []
    assert rm.stop_calls == 0
    store = Store(db_path)
    try:
        assert store.get_session("s-fresh")["status"] == "running"
    finally:
        store.close()


def test_tick_halts_stale_session_without_calling_stop_when_run_not_active(db_path):
    # The stale session's run_id does NOT match the currently-active run (or
    # there is no run_manager at all) — the DB row is still forced to a
    # terminal state, but stop() is never called for an unrelated/absent run.
    now = 10_000.0
    _seed_session(db_path, "s-stale-other", run_id="run-other",
                  stale_by=STALL_TIMEOUT_SEC + 30, now=now)
    rm = _FakeRunManager(active_run_id="run-x")  # a DIFFERENT run is active
    watchdog = SessionWatchdog(db_path=db_path, run_manager=rm, clock=lambda: now)

    halted = watchdog.tick()

    assert halted == ["s-stale-other"]
    assert rm.stop_calls == 0
    store = Store(db_path)
    try:
        assert store.get_session("s-stale-other")["status"] == "halted"
    finally:
        store.close()


def test_tick_works_with_no_run_manager_injected(db_path):
    now = 10_000.0
    _seed_session(db_path, "s-stale", stale_by=STALL_TIMEOUT_SEC + 30, now=now)
    watchdog = SessionWatchdog(db_path=db_path, run_manager=None, clock=lambda: now)

    halted = watchdog.tick()

    assert halted == ["s-stale"]
    store = Store(db_path)
    try:
        assert store.get_session("s-stale")["status"] == "halted"
    finally:
        store.close()


def test_tick_reconciles_orphans_excluding_the_active_run(db_path):
    # A session with NO heartbeat staleness (so find_stalled_sessions never
    # touches it) but whose run_id belongs to neither a live job nor the
    # currently-active run is an orphan for reconcile_orphan_sessions to catch —
    # while a session backing the ACTIVE run must survive the same tick.
    now = 10_000.0
    _seed_session(db_path, "s-orphan", run_id="run-dead", stale_by=0.0, now=now)
    _seed_session(db_path, "s-active", run_id="run-x", stale_by=0.0, now=now)
    rm = _FakeRunManager(active_run_id="run-x")
    watchdog = SessionWatchdog(db_path=db_path, run_manager=rm, clock=lambda: now)

    watchdog.tick()

    store = Store(db_path)
    try:
        assert store.get_session("s-orphan")["status"] == "halted"
        assert store.get_session("s-active")["status"] == "running"
    finally:
        store.close()


def test_start_stop_is_idempotent_and_uses_a_daemon_thread(db_path):
    watchdog = SessionWatchdog(db_path=db_path, tick_seconds=3600)
    watchdog.start()
    watchdog.start()  # idempotent — does not spawn a second thread
    assert watchdog._thread is not None
    assert watchdog._thread.daemon is True
    watchdog.stop()
