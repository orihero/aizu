"""ScheduleManager — the recurring-run daemon's tick logic (campaign-lifecycle PRD,
Phase 4). Driven deterministically with an injected clock and a fake RunManager; no
real thread, no real subprocess."""
import os
import tempfile

from aizu.core.schedule import TASHKENT, next_fire
from aizu.core.store import Store
from aizu.scheduler import ScheduleManager


def _store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path), path


class _FakeActive:
    run_id = "fake-run"


class FakeRunManager:
    """Records launch() specs; the result is scripted so we can simulate contention."""
    def __init__(self):
        self.launched = []
        self.next_result = "ok"        # 'ok' | 'busy'
        self.swept = 0

    def launch(self, spec):
        self.launched.append(spec)
        if self.next_result == "busy":
            return None, "a run is already active"
        return _FakeActive(), None

    def sweep_orphan_pause_files(self):
        self.swept += 1


def _armed_campaign(store, cid, *, next_run_at, status="live", goal=40,
                    target=None, org_id=1):
    store.upsert_campaign_meta(cid, org_id=org_id, status=status, goal_target=goal)
    store.set_campaign_schedule(cid, kind="daily", hour=9, minute=0,
                                next_run_at=next_run_at, target_leads=target)


def test_due_campaign_launches_once_and_advances():
    store, path = _store()
    _armed_campaign(store, "c", next_run_at=100.0, goal=40)
    rm = FakeRunManager()
    mgr = ScheduleManager(db_path=path, run_manager=rm, clock=lambda: 1000.0)

    mgr.tick()
    assert len(rm.launched) == 1
    spec = rm.launched[0]
    assert spec.campaign_id == "c" and spec.launch_source == "scheduled"
    assert spec.mode == "live" and spec.org_id == 1
    assert spec.target_leads == 40            # defaulted to goal_target

    # next_run_at advanced into the future → a second tick does NOT relaunch.
    meta = store.get_campaign_meta("c")
    assert meta["next_run_at"] > 1000.0
    assert meta["last_scheduled_run_at"] == 1000.0
    mgr.tick()
    assert len(rm.launched) == 1


def test_schedule_target_overrides_goal():
    store, path = _store()
    _armed_campaign(store, "c", next_run_at=100.0, goal=40, target=10)
    rm = FakeRunManager()
    ScheduleManager(db_path=path, run_manager=rm, clock=lambda: 1000.0).tick()
    assert rm.launched[0].target_leads == 10


def test_not_due_paused_archived_never_launch():
    store, path = _store()
    _armed_campaign(store, "future", next_run_at=9e12)            # not due
    _armed_campaign(store, "paused", next_run_at=100.0, status="paused")
    _armed_campaign(store, "arch", next_run_at=100.0)
    store.set_campaign_archived("arch", True)
    rm = FakeRunManager()
    ScheduleManager(db_path=path, run_manager=rm, clock=lambda: 1000.0).tick()
    assert rm.launched == []


def test_contention_advances_and_raises_skip_flag_no_retry_storm():
    store, path = _store()
    _armed_campaign(store, "c", next_run_at=100.0)
    rm = FakeRunManager()
    rm.next_result = "busy"            # a manual run already holds the lock
    mgr = ScheduleManager(db_path=path, run_manager=rm, clock=lambda: 1000.0)

    mgr.tick()
    assert len(rm.launched) == 1                     # tried once
    # Occurrence dropped (advanced), so the next tick does NOT retry-storm.
    assert store.get_campaign_meta("c")["next_run_at"] > 1000.0
    flags = [f for f in store.open_flags(org_id=1) if f["kind"] == "scheduled_run_skipped"]
    assert len(flags) == 1
    mgr.tick()
    assert len(rm.launched) == 1                     # still just the one attempt


def test_two_ticks_same_window_cannot_double_launch():
    store, path = _store()
    _armed_campaign(store, "c", next_run_at=100.0)
    rm = FakeRunManager()
    mgr = ScheduleManager(db_path=path, run_manager=rm, clock=lambda: 1000.0)
    mgr.tick()
    mgr.tick()        # same clock, same window — idempotent
    assert len(rm.launched) == 1


def test_start_sweeps_orphan_pause_files():
    store, path = _store()
    rm = FakeRunManager()
    mgr = ScheduleManager(db_path=path, run_manager=rm)
    mgr.start()
    try:
        assert rm.swept == 1
    finally:
        mgr.stop()


def test_advanced_next_run_matches_cadence():
    store, path = _store()
    _armed_campaign(store, "c", next_run_at=100.0)
    rm = FakeRunManager()
    now = 1000.0
    ScheduleManager(db_path=path, run_manager=rm, clock=lambda: now).tick()
    expected = next_fire("daily", 9, 0, after_ts=now)
    assert store.get_campaign_meta("c")["next_run_at"] == expected
