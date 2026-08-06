"""Fixed-cadence campaign scheduling (campaign-lifecycle PRD, Phase 3).

No cron string — the UI offers only Daily / Weekdays / Weekly at an HH:MM, so the
whole schedule is (kind, day-of-week, hour, minute) and `next_fire` is a small,
testable function. Times are LOCAL to Asia/Tashkent, which is a fixed UTC+5 with no
DST (mirrors store.TZ_SQL_SHIFT='+5 hours' and panel.TASHKENT) — so a scheduled
09:00 lands at local wall-clock 09:00, INSIDE the engine's daytime guard window
([8, 21) local), never skewed off-hours by a timezone bug.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Asia/Tashkent — fixed UTC+5, no DST. The single source of truth for schedule math.
TASHKENT = timezone(timedelta(hours=5))

SCHEDULE_KINDS = ("daily", "weekdays", "weekly")


def _matches(kind: str, dow: int | None, dt: datetime) -> bool:
    weekday = dt.weekday()  # Mon=0 .. Sun=6
    if kind == "daily":
        return True
    if kind == "weekdays":
        return weekday < 5  # Mon-Fri
    if kind == "weekly":
        return weekday == dow
    return False


def next_fire(kind: str, hour: int, minute: int, *,
              dow: int | None = None, after_ts: float,
              tz: timezone = TASHKENT) -> float:
    """Epoch (UTC seconds) of the next fire at local ``hour:minute`` strictly after
    ``after_ts``, for the given cadence.

    kind: 'daily' (every day), 'weekdays' (Mon-Fri), 'weekly' (the given ``dow``,
    Mon=0..Sun=6). Raises ValueError on an unknown kind or a 'weekly' with no dow.
    """
    if kind not in SCHEDULE_KINDS:
        raise ValueError(f"invalid schedule kind: {kind!r}")
    if kind == "weekly" and dow is None:
        raise ValueError("weekly schedule requires a day-of-week")
    after = datetime.fromtimestamp(after_ts, tz)
    candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(days=1)
    # At most a week away for any cadence; the +1 guards a same-day match landing
    # exactly on the loop bound.
    for _ in range(8):
        if _matches(kind, dow, candidate):
            return candidate.timestamp()
        candidate += timedelta(days=1)
    raise ValueError(f"no fire within a week for kind={kind!r} dow={dow!r}")
