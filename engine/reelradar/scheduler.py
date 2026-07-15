"""Recurring-schedule daemon (campaign-lifecycle PRD, Phase 4).

A single background thread that, every ``tick_seconds``, finds campaigns whose
fixed-cadence schedule is due and launches a LIVE run for each through the shared
RunManager (same single-run lock as a manual run — the engine still gates on the
daytime window and the warmth gate).

Idempotency: each due campaign's ``next_run_at`` is advanced to its NEXT fire
BEFORE the launch, in its own write, so a second tick (or a tick + a server restart
mid-launch) can never launch the same window twice. On lock contention ("a run is
already active") the occurrence is DROPPED (already advanced) and a visible
health_flag is raised — never a 60s retry storm.

Injected into ``serve()`` exactly like RunManager, so existing server tests pass
``schedule_manager=None`` and no DB-touching daemon spawns inside their fixtures.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from .core.logsetup import get_logger
from .core.schedule import next_fire
from .core.store import Store
from .runner import RunManager, RunSpec

log = get_logger(__name__)

DEFAULT_TICK_SECONDS = 60


class ScheduleManager:
    def __init__(self, *, db_path: str, run_manager: RunManager,
                 tick_seconds: int = DEFAULT_TICK_SECONDS,
                 clock: Callable[[], float] = time.time):
        self._db_path = str(db_path)
        self._run_manager = run_manager
        self._tick_seconds = tick_seconds
        self._clock = clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Sweep orphaned pause sentinels (a restart safety net), then start the tick
        loop on a daemon thread."""
        self._run_manager.sweep_orphan_pause_files()
        if self._thread is not None:
            return  # already started — idempotent
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="schedule-manager")
        self._thread.start()
        log.info("Schedule daemon started · tick=%ss", self._tick_seconds)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # `Event.wait` returns True only when stop() was set, so the loop exits cleanly
        # on shutdown and otherwise ticks every tick_seconds.
        while not self._stop.wait(self._tick_seconds):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — one bad tick must never kill the daemon
                log.exception("Schedule tick failed")

    def tick(self) -> None:
        """One scheduler pass: fire every campaign whose schedule is due at now.
        Public so tests can drive it deterministically with an injected clock."""
        now = self._clock()
        store = Store(self._db_path)
        try:
            for camp in store.due_scheduled_campaigns(now):
                self._fire(store, camp, now)
        finally:
            store.close()

    def _fire(self, store: Store, camp: dict, now: float) -> None:
        cid = camp["campaign_id"]
        # 1. Advance next_run_at BEFORE launching (idempotency): this occurrence is
        #    consumed even if the launch is rejected or the process restarts.
        try:
            nxt = next_fire(camp["schedule_kind"], camp["schedule_hour"],
                            camp["schedule_minute"], dow=camp.get("schedule_dow"),
                            after_ts=now)
        except ValueError as e:
            log.error("Bad schedule for campaign %s — disabling: %s", cid, e)
            store.clear_campaign_schedule(cid)
            return
        store.advance_scheduled_run(cid, next_run_at=nxt, fired_at=now)
        # 2. Launch through the shared single-run lock. A scheduled run defaults its
        #    lead target to the campaign's goal_target (never unbounded).
        spec = RunSpec(
            scope="campaign", campaign_id=cid, mode="live", org_id=camp.get("org_id"),
            target_leads=camp.get("schedule_target_leads") or camp.get("goal_target"),
            duration_minutes=camp.get("schedule_duration_minutes"),
            launch_source="scheduled")
        active, err = self._run_manager.launch(spec)
        if active is None:
            # Contention or spawn failure — the occurrence is already dropped. Make it
            # visible (no silent no-op, no retry storm) and move on.
            store.raise_flag("scheduled_run_skipped", "soft",
                             f"scheduled run skipped: {err}", campaign_id=cid)
            log.warning("Scheduled run skipped · campaign=%s · %s", cid, err)
        else:
            log.info("Scheduled run launched · campaign=%s · run=%s", cid, active.run_id)
