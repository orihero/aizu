"""Pacing daytime guard + its env opt-out (REELRADAR_IGNORE_DAYTIME).

The guard halts live sessions outside 08:00–21:00 local for human-like cadence.
The env opt-out exists for off-hours testing; it must default OFF (guard ON) and an
explicit PacingConfig(enforce_daytime=...) must always beat the env.
"""
from datetime import datetime

import pytest

from reelradar.core.pacing import PacingConfig, Pacer


def _clock_at(hour: int):
    return lambda: datetime(2026, 6, 21, hour, 30, 0)


def _pacer(cfg: PacingConfig, hour: int) -> Pacer:
    return Pacer(cfg=cfg, clock=_clock_at(hour), sleep=lambda _t: None)


def test_daytime_guard_is_on_by_default(monkeypatch):
    monkeypatch.delenv("REELRADAR_IGNORE_DAYTIME", raising=False)
    assert PacingConfig().enforce_daytime is True
    assert _pacer(PacingConfig(), hour=2).is_daytime() is False   # 02:30 → halt
    assert _pacer(PacingConfig(), hour=12).is_daytime() is True   # midday → run


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_ignore_daytime_env_opts_out(monkeypatch, value):
    monkeypatch.setenv("REELRADAR_IGNORE_DAYTIME", value)
    cfg = PacingConfig()
    assert cfg.enforce_daytime is False
    assert _pacer(cfg, hour=0).is_daytime() is True   # runs even at 00:30


@pytest.mark.parametrize("value", ["0", "", "no", "off", "false"])
def test_falsey_env_keeps_the_guard(monkeypatch, value):
    monkeypatch.setenv("REELRADAR_IGNORE_DAYTIME", value)
    assert PacingConfig().enforce_daytime is True


def test_explicit_enforce_daytime_beats_the_env(monkeypatch):
    monkeypatch.setenv("REELRADAR_IGNORE_DAYTIME", "1")
    cfg = PacingConfig(enforce_daytime=True)          # explicit wins
    assert cfg.enforce_daytime is True
    assert _pacer(cfg, hour=2).is_daytime() is False
