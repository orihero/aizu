"""Fixed-cadence next_fire — daily/weekdays/weekly + the tz-skew guard
(campaign-lifecycle PRD, Phase 3)."""
import calendar
from datetime import datetime, timedelta, timezone

import pytest

from reelradar.core.schedule import TASHKENT, next_fire


def _epoch_local(y, mo, d, h, mi):
    """A Tashkent-local wall-clock as a UTC epoch."""
    return datetime(y, mo, d, h, mi, tzinfo=TASHKENT).timestamp()


def test_daily_returns_today_when_time_not_passed():
    # 2026-06-29 08:00 local → next daily 09:00 is the SAME day at 09:00.
    after = _epoch_local(2026, 6, 29, 8, 0)
    fire = datetime.fromtimestamp(next_fire("daily", 9, 0, after_ts=after), TASHKENT)
    assert (fire.year, fire.month, fire.day, fire.hour, fire.minute) == (2026, 6, 29, 9, 0)


def test_daily_rolls_to_tomorrow_when_time_passed():
    after = _epoch_local(2026, 6, 29, 10, 0)   # 10:00, past 09:00
    fire = datetime.fromtimestamp(next_fire("daily", 9, 0, after_ts=after), TASHKENT)
    assert (fire.month, fire.day, fire.hour) == (6, 30, 9)


def test_weekdays_skips_the_weekend():
    # 2026-06-27 is a Saturday; next weekday 09:00 is Monday the 29th.
    sat = _epoch_local(2026, 6, 27, 10, 0)
    fire = datetime.fromtimestamp(next_fire("weekdays", 9, 0, after_ts=sat), TASHKENT)
    assert fire.weekday() == 0 and (fire.month, fire.day) == (6, 29)


def test_weekly_lands_on_the_requested_dow():
    # From Mon 2026-06-29, next Wednesday (dow=2) 09:00 is 2026-07-01.
    mon = _epoch_local(2026, 6, 29, 12, 0)
    fire = datetime.fromtimestamp(next_fire("weekly", 9, 0, dow=2, after_ts=mon), TASHKENT)
    assert fire.weekday() == 2 and (fire.month, fire.day) == (7, 1)


def test_weekly_requires_dow():
    with pytest.raises(ValueError):
        next_fire("weekly", 9, 0, after_ts=_epoch_local(2026, 6, 29, 8, 0))


def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        next_fire("hourly", 9, 0, after_ts=_epoch_local(2026, 6, 29, 8, 0))


def test_no_tz_skew_a_0900_schedule_lands_at_local_0900_inside_daytime_window():
    """The load-bearing correctness guard: a 09:00 schedule must fire at LOCAL 09:00
    (inside the daytime guard's [8, 21) window), not skewed off-hours by a tz bug.
    Tashkent is a fixed UTC+5 (no DST), so this holds in summer and winter alike."""
    for month in (1, 6):  # winter + summer — DST-free, must both be 09:00 local
        after = _epoch_local(2026, month, 15, 6, 0)
        fire_ts = next_fire("daily", 9, 0, after_ts=after)
        local = datetime.fromtimestamp(fire_ts, TASHKENT)
        assert local.hour == 9 and local.minute == 0
        assert 8 <= local.hour < 21               # inside the daytime guard window
        # And the underlying UTC instant is 04:00 (09:00 − 5h), proving the offset.
        utc = datetime.fromtimestamp(fire_ts, timezone.utc)
        assert utc.hour == 4
