"""Session-liveness watchdog (hang-prevention fix #4).

Even with bounded CDP calls (core/bounded.py) and mid-run login/challenge
detection (CDPFeedBase.walk()), a session can still wedge somewhere neither
guard covers — a pathological stack of slow-but-not-timed-out calls, a stuck
subprocess, a network stall inside a library this codebase doesn't wrap. When
that happens there is no exception to trip any except-guard, per-source
budget, or ``session_crash_guard`` — the session row just sits at
``status='running'`` forever and RunManager's ``_monitor`` thread blocks
uninterrupted on ``proc.wait()``.

This is the backstop: a single background thread that, every ``tick_seconds``,
sweeps sessions whose heartbeat (``sessions.last_activity_at``, bumped by every
``Store.update_counters`` call — i.e. every engine's periodic ``_flush``) has
gone quiet for over ``STALL_TIMEOUT_SEC``, stops the matching in-process run
(reusing RunManager's existing terminate path) if one is active, and forces the
session row to a terminal state either way. It also re-runs
``reconcile_orphan_sessions`` each tick (v20: now periodic-safe via the
``active_run_ids`` exclusion) so a crashed/killed process's orphaned rows don't
have to wait for the next server restart to clear.

Modeled closely on ``reclaim_manager.py`` — same class shape (start/stop daemon
thread, ``tick()`` with except-log), same "inject a run_manager, fresh Store per
tick" pattern — so this reads as one family of daemon with that file, not a
new pattern to learn.
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Callable, Optional

from .core.logsetup import get_logger
from .core.store import Store

if TYPE_CHECKING:
    from .runner import RunManager

log = get_logger(__name__)

# Sweep cadence. Comfortably below STALL_TIMEOUT_SEC so a wedged session is
# caught within one tick of crossing the threshold, without hammering the
# single SQLite writer (mirrors ReclaimManager's tick/lease ratio).
DEFAULT_TICK_SECONDS = 30

# How long a 'running' session can go with no heartbeat before it's considered
# wedged. Comfortably above Session's own PROGRESS_EVENT_INTERVAL_SEC (45s) and
# per_reel_seconds backstops (90s) — a HEALTHY run always bumps last_activity_at
# well inside this window via update_counters (called once per reel and once per
# already-seen skip), so this only fires on a genuine stall.
STALL_TIMEOUT_SEC = 180


class SessionWatchdog:
    def __init__(self, *, db_path: str, tick_seconds: int = DEFAULT_TICK_SECONDS,
                 run_manager: Optional["RunManager"] = None,
                 clock: Callable[[], float] = time.time):
        self._db_path = str(db_path)
        self._tick_seconds = tick_seconds
        self._run_manager = run_manager
        self._clock = clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the tick loop on a daemon thread (idempotent)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="session-watchdog")
        self._thread.start()
        log.info("Session watchdog started · tick=%ss stall_timeout=%ss",
                 self._tick_seconds, STALL_TIMEOUT_SEC)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self._tick_seconds):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — one bad tick must never kill the daemon
                log.exception("Session watchdog tick failed")

    def _active_run_ids(self) -> frozenset:
        """The run_id(s) RunManager currently has in flight (at most one — only
        one engine run executes at a time), or empty when there is no injected
        run_manager (e.g. a distributed-worker-only deployment) or nothing active.
        Never raises: a broken status() call degrades to "nothing active", which
        only makes this tick slightly more conservative (still-live run wrongly
        looks stalled), never less safe."""
        if self._run_manager is None:
            return frozenset()
        try:
            active = self._run_manager.status().get("active")
        except Exception:  # noqa: BLE001 — never let a bad status() kill the tick
            log.warning("Session watchdog: run_manager.status() failed", exc_info=True)
            return frozenset()
        run_id = active.get("id") if active else None
        return frozenset({run_id}) if run_id else frozenset()

    def tick(self) -> list[str]:
        """One watchdog pass. Public so tests can drive it deterministically.
        Returns the session_ids halted for staleness (empty when none were)."""
        store = Store(self._db_path)
        halted: list[str] = []
        try:
            now = self._clock()
            active_run_ids = self._active_run_ids()
            stalled = store.find_stalled_sessions(
                stall_timeout_s=STALL_TIMEOUT_SEC, now=now)
            for row in stalled:
                session_id = row["session_id"]
                run_id = row.get("run_id")
                if run_id and run_id in active_run_ids:
                    # This stalled session backs the CURRENTLY active in-process
                    # run — stop it via RunManager's own terminate path (reused,
                    # not reimplemented) so the child process actually dies
                    # instead of just being marked halted in the DB while it
                    # keeps running underneath.
                    try:
                        self._run_manager.stop()  # type: ignore[union-attr]
                    except Exception:  # noqa: BLE001 — DB halt below still proceeds
                        log.warning(
                            "Session watchdog: failed to stop run %s for stalled "
                            "session %s", run_id, session_id, exc_info=True)
                try:
                    store.end_session(
                        session_id, "halted",
                        f"stalled: no activity for over {STALL_TIMEOUT_SEC:.0f}s")
                    halted.append(session_id)
                except Exception:  # noqa: BLE001 — one bad row must not skip the rest
                    log.warning("Session watchdog: failed to halt stalled session %s",
                               session_id, exc_info=True)
            reconciled = store.reconcile_orphan_sessions(
                now=now, active_run_ids=active_run_ids)
        finally:
            store.close()
        if halted:
            log.warning("Session watchdog halted %d stalled session(s): %s",
                       len(halted), ", ".join(halted))
        if reconciled:
            log.info("Session watchdog reconciled %d orphaned session(s)", reconciled)
        return halted
