"""Local single-flight lock (single_flight.py, BUILD-PLAN §2.3 / risk #2,#3)."""
from __future__ import annotations

import os
import time
from pathlib import Path

from aizu.worker import single_flight


def test_acquire_then_second_attempt_is_blocked(state_dir: Path):
    first = single_flight.try_acquire(state_dir, "7-instagram-acme")
    assert first is not None
    # A second run on this box for the SAME account must NOT acquire.
    second = single_flight.try_acquire(state_dir, "7-instagram-acme")
    assert second is None


def test_release_frees_the_lock(state_dir: Path):
    lock = single_flight.try_acquire(state_dir, "k")
    assert lock is not None
    lock.release()
    again = single_flight.try_acquire(state_dir, "k")
    assert again is not None  # reclaimable after release


def test_different_keys_are_independent(state_dir: Path):
    a = single_flight.try_acquire(state_dir, "1-x-acct")
    b = single_flight.try_acquire(state_dir, "2-x-acct")
    assert a is not None and b is not None


def test_release_is_idempotent(state_dir: Path):
    lock = single_flight.try_acquire(state_dir, "k")
    lock.release()
    lock.release()  # must not raise


def test_sweep_reclaims_only_stale_locks(state_dir: Path):
    fresh = single_flight.try_acquire(state_dir, "fresh")
    stale = single_flight.try_acquire(state_dir, "stale")
    assert fresh is not None and stale is not None
    # Age the stale lock past the threshold.
    old = time.time() - 1000
    os.utime(stale.path, (old, old))

    reclaimed = single_flight.sweep_stale(state_dir, max_age_sec=500)

    assert reclaimed == 1
    assert not stale.path.exists()
    assert fresh.path.exists()  # a live run's lock is never reclaimed


def test_sweep_on_empty_dir_returns_zero(tmp_path: Path):
    assert single_flight.sweep_stale(tmp_path / "nope", max_age_sec=1) == 0


# A pid that is (essentially) never alive: max positive value pids can never reach.
_DEAD_PID = 2**31 - 1


def _write_lock(state_dir: Path, key: str, content: str) -> Path:
    """Create a lock file directly with arbitrary content (simulate a leak)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = single_flight._lock_path(state_dir, key)
    path.write_text(content, encoding="ascii")
    return path


def test_sweep_reclaims_dead_owner_lock_even_when_fresh(state_dir: Path):
    # A lock leaked by a crashed worker: recorded pid is dead, but the file is
    # recent (age < max_age_sec) so the age backstop would NOT catch it.
    path = _write_lock(state_dir, "dead-owner", f"{_DEAD_PID} {time.time():.0f}")

    reclaimed = single_flight.sweep_stale(state_dir, max_age_sec=10_000)

    assert reclaimed == 1
    assert not path.exists()  # dead owner => definitively orphaned


def test_sweep_keeps_live_owner_fresh_lock(state_dir: Path):
    # A lock held by THIS (alive) process, still fresh: must never be reclaimed.
    lock = single_flight.try_acquire(state_dir, "live-owner")
    assert lock is not None

    reclaimed = single_flight.sweep_stale(state_dir, max_age_sec=10_000)

    assert reclaimed == 0
    assert lock.path.exists()  # a live job's lock is sacrosanct


def test_sweep_age_backstop_reclaims_old_garbled_lock(state_dir: Path):
    # A garbled lock (no parseable pid) is reclaimed only via the age backstop.
    path = _write_lock(state_dir, "garbled", "not-a-pid-at-all")
    old = time.time() - 1000
    os.utime(path, (old, old))

    reclaimed = single_flight.sweep_stale(state_dir, max_age_sec=500)

    assert reclaimed == 1
    assert not path.exists()


def test_sweep_does_not_reclaim_fresh_garbled_lock(state_dir: Path):
    # An unreadable/garbled but FRESH lock must not be reclaimed (unknown owner,
    # rely on the age check only).
    path = _write_lock(state_dir, "fresh-garbled", "garbage")

    reclaimed = single_flight.sweep_stale(state_dir, max_age_sec=10_000)

    assert reclaimed == 0
    assert path.exists()


def test_pid_alive_true_for_self():
    assert single_flight._pid_alive(os.getpid()) is True


def test_pid_alive_false_for_dead_pid():
    assert single_flight._pid_alive(_DEAD_PID) is False
