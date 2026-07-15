"""Pacing engine (PRD §10, soul.md).

Human-like, randomized, daytime-only timing. Caps are discovered empirically;
these are conservative starting ramps. soul.md sets ceilings a campaign may
tighten but never loosen.
"""
from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional


def _enforce_daytime_default() -> bool:
    """Default for ``enforce_daytime``, read from the environment at construction.

    The daytime window (human-like cadence, account safety) is ON by default. Set
    ``REELRADAR_IGNORE_DAYTIME=1`` to opt OUT for off-hours TESTING only — this lets
    a live run proceed at any hour. Read here (not as a static default) so a value in
    ``engine/.env`` or the panel child's inherited env takes effect; an explicit
    ``PacingConfig(enforce_daytime=...)`` always wins over the env.
    """
    return os.environ.get("REELRADAR_IGNORE_DAYTIME", "").strip().lower() not in (
        "1", "true", "yes", "on")


@dataclass
class PacingConfig:
    dwell_min: float = 3.0
    dwell_max: float = 30.0
    between_min: float = 2.0
    between_max: float = 8.0
    reels_per_session_min: int = 20
    reels_per_session_max: int = 40
    daytime_start: int = 8     # local hour
    daytime_end: int = 21
    enforce_daytime: bool = field(default_factory=_enforce_daytime_default)


class Pacer:
    def __init__(self, cfg: Optional[PacingConfig] = None,
                 rng: Optional[random.Random] = None,
                 clock: Callable[[], datetime] = datetime.now,
                 sleep: Optional[Callable[[float], None]] = None):
        self.cfg = cfg or PacingConfig()
        self.rng = rng or random.Random()
        self.clock = clock
        # Default sleep is real; tests inject a no-op.
        if sleep is None:
            import time
            sleep = time.sleep
        self._sleep = sleep
        self.reel_budget = self.rng.randint(
            self.cfg.reels_per_session_min, self.cfg.reels_per_session_max)

    def is_daytime(self) -> bool:
        if not self.cfg.enforce_daytime:
            return True
        h = self.clock().hour
        return self.cfg.daytime_start <= h < self.cfg.daytime_end

    def dwell(self) -> float:
        t = self.rng.uniform(self.cfg.dwell_min, self.cfg.dwell_max)
        self._sleep(t)
        return t

    def between_reels(self) -> float:
        t = self.rng.uniform(self.cfg.between_min, self.cfg.between_max)
        self._sleep(t)
        return t
