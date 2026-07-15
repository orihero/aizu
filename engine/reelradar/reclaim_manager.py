"""Distributed-jobs reclaim daemon (distributed-workers BUILD-PLAN Phase 4).

A single background thread that, every ``tick_seconds``, sweeps jobs whose lease has
EXPIRED (their per-job heartbeat stopped extending → the run wedged/crashed) and requeues
them PINNED to their original box (one account ↔ one box — no cross-box failover,
BUILD-PLAN risk #2). The correctness lives in ``Store.reclaim_offline_jobs`` (BEGIN
IMMEDIATE + expired-lease reclaim regardless of worker presence — see its docstring for
why the old offline-worker gate let a wedged-but-present box block redispatch); this
daemon only drives it on a cadence.

Injected into ``serve()`` exactly like ScheduleManager, so existing server tests that
pass no reclaim_manager never spawn a DB-touching daemon inside their fixtures.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from .core.logsetup import get_logger
from .core.store import Store

log = get_logger(__name__)

# Sweep cadence. Faster than the schedule daemon: a lease is 60s and a worker is offline
# at ~120s, so a 30s tick reclaims within one lease window of the offline declaration
# without hammering the single writer.
DEFAULT_TICK_SECONDS = 30


class ReclaimManager:
    def __init__(self, *, db_path: str, tick_seconds: int = DEFAULT_TICK_SECONDS,
                 clock: Callable[[], float] = time.time):
        self._db_path = str(db_path)
        self._tick_seconds = tick_seconds
        self._clock = clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the tick loop on a daemon thread (idempotent)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="reclaim-manager")
        self._thread.start()
        log.info("Reclaim daemon started · tick=%ss", self._tick_seconds)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self._tick_seconds):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — one bad tick must never kill the daemon
                log.exception("Reclaim tick failed")

    def tick(self) -> list:
        """One reclaim pass. Public so tests can drive it with an injected clock.
        Returns the reclaimed records (empty when nothing was stranded)."""
        store = Store(self._db_path)
        try:
            reclaimed = store.reclaim_offline_jobs(now=self._clock())
        finally:
            store.close()
        if reclaimed:
            log.warning("Reclaimed %d stranded job(s) from offline workers: %s",
                        len(reclaimed),
                        ", ".join(f"{r['jobId']}→{r['outcome']}" for r in reclaimed))
        return reclaimed
