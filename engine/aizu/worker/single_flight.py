"""Local single-flight lock: one live job per (org, platform, account) on this box.

The DB lease prevents two *boxes* from taking the same job; this prevents two job
runs on the SAME box from both attaching the one warmed Chrome for an account —
which would corrupt the session or trip a ban (BUILD-PLAN risk #2).

Implementation is an atomic ``O_CREAT | O_EXCL`` file create (cross-platform on
mac/Windows/Linux — no third-party ``filelock`` dependency). A crashed run leaves a
stale lock; :func:`sweep_stale` reclaims it on startup — immediately if its recorded
owner pid is dead, otherwise via an mtime-age backstop — so steady state never needs
a manual delete (BUILD-PLAN risk #3).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..core.logsetup import get_logger
from ._atomic_io import pid_alive

log = get_logger("aizu.worker.single_flight")

_LOCK_PREFIX = "single-flight-"
_LOCK_SUFFIX = ".lock"


@dataclass(frozen=True)
class Lock:
    """A held single-flight lock; release() unlinks its file. Immutable handle."""

    path: Path

    def release(self) -> None:
        """Remove the lock file. Idempotent and never raises — releasing on a
        finally path must not mask the job's real result."""
        try:
            self.path.unlink(missing_ok=True)
        except OSError as e:  # noqa: BLE001 — best-effort cleanup
            log.warning("single-flight release failed for %s: %s", self.path, e)


def _lock_path(state_dir: Path, key: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
    return state_dir / f"{_LOCK_PREFIX}{safe}{_LOCK_SUFFIX}"


def _pid_alive(pid: int) -> bool:
    """Return True if ``pid`` names a live process on this box.

    Cross-platform (see ``_atomic_io.pid_alive``): POSIX probes with
    ``os.kill(pid, 0)``, Windows via ``OpenProcess``. A dead pid is the only case we
    treat as not-alive, so a leaked lock whose owner is EPERM is (correctly) kept.
    Ambiguous cases (malformed pid, unexpected OS error) resolve to alive so we never
    false-reclaim a still-live owner; the age backstop still applies.
    """
    return pid_alive(pid, unknown_is_alive=True)


def _lock_owner_pid(path: Path) -> Optional[int]:
    """Parse the owner pid from a lock file's first whitespace token.

    ``try_acquire`` writes ``f"{os.getpid()} {time:.0f}"``. Returns the pid, or
    ``None`` when the file is unreadable, empty, or the first token is not an int
    (a garbled lock — caller falls back to the age backstop). Never raises.
    """
    try:
        text = path.read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    token = text.strip().split(maxsplit=1)[:1]
    if not token:
        return None
    try:
        return int(token[0])
    except (ValueError, OverflowError):
        return None


def try_acquire(state_dir: Path, key: str) -> Optional[Lock]:
    """Atomically claim the lock for ``key``. Returns a :class:`Lock` on success, or
    ``None`` if another run on this box already holds it (the job is skipped and
    re-leased later — never double-attached)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = _lock_path(state_dir, key)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        log.info("single-flight busy for %s — skipping job", key)
        return None
    try:
        os.write(fd, f"{os.getpid()} {time.time():.0f}".encode("ascii"))
    finally:
        os.close(fd)
    return Lock(path=path)


def _unlink_reclaimed(path: Path) -> bool:
    """Unlink a reclaimed lock file. Returns True on success. Never raises."""
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError as e:  # noqa: BLE001 — best-effort reclaim
        log.warning("Could not reclaim stale lock %s: %s", path, e)
        return False


def sweep_stale(state_dir: Path, *, max_age_sec: float) -> int:
    """Reclaim orphaned single-flight locks left by a crashed/restarted run.

    A lock is reclaimed if EITHER:

    - its recorded owner pid is no longer alive (a dead owner means the lock is
      definitively orphaned — reclaimed regardless of age), or
    - the file is older than ``max_age_sec`` (the age backstop, for a lock whose
      pid is unreadable/garbled or belongs to a still-live-but-wrong process).

    Returns the number reclaimed. Called once on sidecar startup, before the lease
    loop. A still-running job's pid IS alive, so its lock is never reclaimed — this
    is why PID reuse only ever risks NOT reclaiming (age backstop still applies),
    never a false reclaim of a live job. ``max_age_sec`` should still exceed the
    worst-case job length for the backstop to stay safe against garbled files.
    """
    if not state_dir.exists():
        return 0
    now = time.time()
    reclaimed = 0
    for path in state_dir.glob(f"{_LOCK_PREFIX}*{_LOCK_SUFFIX}"):
        pid = _lock_owner_pid(path)
        if pid is not None and not _pid_alive(pid):
            if _unlink_reclaimed(path):
                reclaimed += 1
                log.warning("Reclaimed single-flight lock %s (dead owner pid %d)",
                            path.name, pid)
            continue
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age > max_age_sec and _unlink_reclaimed(path):
            reclaimed += 1
            log.warning("Reclaimed stale single-flight lock %s (age %.0fs)",
                        path.name, age)
    return reclaimed
