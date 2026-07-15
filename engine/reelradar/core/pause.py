"""Cooperative run pause (campaign-lifecycle PRD, Phase 2).

A run is paused by the panel via RunManager.pause(), which TOUCHES a sentinel file
(``REELRADAR_PAUSE_FILE``). The engine polls that file at safe checkpoints — between
reels and between sessions — and idles while it exists, doing no DB writes and
holding no half-processed reel. resume() (or the run's natural end) deletes it.

The engine only READS the sentinel; its whole lifecycle (create on pause, delete on
resume / child-exit / startup-sweep) is owned by RunManager, so a multi-session run
can never accidentally delete a pause its operator set.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Optional


def pause_file_from_env() -> Optional[Path]:
    """The pause sentinel RunManager assigned this run, or None for a direct CLI run
    (the panel passes it by env; an unset var means pausing is simply inert)."""
    raw = os.environ.get("REELRADAR_PAUSE_FILE")
    return Path(raw) if raw else None


def is_paused(pause_file: Optional[Path]) -> bool:
    return pause_file is not None and pause_file.exists()


def wait_while_paused(pause_file: Optional[Path], *,
                      on_enter: Optional[Callable[[], None]] = None,
                      on_exit: Optional[Callable[[], None]] = None,
                      sleep: Callable[[float], None] = time.sleep,
                      poll_seconds: float = 2.0) -> bool:
    """Block while the sentinel exists, polling every ``poll_seconds``.

    Returns True iff it actually paused (so the caller can record paused/resumed
    exactly once). Call ONLY at a safe checkpoint — never mid-reel or mid-write."""
    if not is_paused(pause_file):
        return False
    if on_enter is not None:
        on_enter()
    while is_paused(pause_file):
        sleep(poll_seconds)
    if on_exit is not None:
        on_exit()
    return True
