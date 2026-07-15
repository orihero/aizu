"""Instagram session crash-guard + halt-path fidelity.

Proves the shared ``session_crash_guard`` (engines.base) closes a session row that
would otherwise be left stuck at status='running' when an UNEXPECTED exception
(e.g. Playwright TargetClosedError when Chrome closes mid-run) escapes ``run()`` —
while leaving a legitimate HaltSession's original reason (e.g. 'outside daytime
window') untouched.
"""
import os
import sqlite3
import tempfile
from datetime import datetime

from reelradar.core.config import campaign_from_brief
from reelradar.core.feed import Comment, FakeFeed, Reel
from reelradar.core.mock_router import MockRouter
from reelradar.core.pacing import Pacer, PacingConfig
from reelradar.engines.instagram.session import (
    PROGRESS_EVENT_INTERVAL_SEC, HaltSession, Session, SessionConfig)


def _store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from reelradar.core.store import Store
    return Store(path), path


def _campaign():
    return campaign_from_brief("ig-leadgen", {
        "platform": "instagram", "threshold": 0.7,
        "relevance_def": "acme saas app posts",
        "match_def": "a commenter asking to buy / price / contact",
        "extract_def": "- phone",
    })


def _daytime_pacer():
    return Pacer(cfg=PacingConfig(enforce_daytime=False), sleep=lambda _t: None)


def _session_rows(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT session_id, status, halt_reason, ended_at FROM sessions").fetchall()]
    con.close()
    return rows


class CrashingFeed(FakeFeed):
    """walk() raises a generic exception on first iteration — mirrors Chrome/CDP
    closing mid-run (TargetClosedError), which is NOT a HaltSession."""

    def __init__(self):
        super().__init__([])

    def walk(self):
        raise RuntimeError("boom: target closed")
        yield  # pragma: no cover — unreachable, keeps this a generator


def test_crash_guard_terminalizes_session_on_unexpected_error():
    store, path = _store()
    session = Session(store=store, router=MockRouter(store=store),
                      feed=CrashingFeed(), soul=None, campaign=_campaign(),
                      pacer=_daytime_pacer(), cfg=SessionConfig())
    sid = session.session_id
    try:
        session.run()
        assert False, "expected the RuntimeError to propagate"
    except RuntimeError as e:
        assert "boom" in str(e)
    rows = _session_rows(path)
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == sid
    assert row["status"] == "halted"
    assert row["ended_at"] is not None
    assert row["halt_reason"].startswith("crashed:")
    assert "RuntimeError" in row["halt_reason"]


def test_halt_session_keeps_original_reason_not_crashed():
    """A daytime-window halt must record its ORIGINAL reason — the crash guard must
    re-raise HaltSession without clobbering the row with a 'crashed:' reason."""
    store, path = _store()
    # A clock fixed at 03:00 → outside the [08:00, 21:00) daytime window.
    night = Pacer(cfg=PacingConfig(enforce_daytime=True),
                  clock=lambda: datetime(2026, 7, 3, 3, 0, 0),
                  sleep=lambda _t: None)
    session = Session(store=store, router=MockRouter(store=store),
                      feed=FakeFeed([Reel("r1", caption="acme app",
                                          comments=[Comment("c1", "u", "pricing?")])]),
                      soul=None, campaign=_campaign(), pacer=night,
                      cfg=SessionConfig())
    try:
        session.run()
        assert False, "expected HaltSession"
    except HaltSession as h:
        assert h.reason == "outside daytime window"
    rows = _session_rows(path)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "halted"
    assert row["halt_reason"] == "outside daytime window"
    assert not (row["halt_reason"] or "").startswith("crashed:")


# ---- FIX B: throttled scan-progress heartbeat -----------------------------
#
# A healthy run scanning ordinary (irrelevant) reels emitted NO run_event, so the
# panel's stall banner fired at 120s even though the run was fine. FIX B adds a
# time-throttled "Scanning …" progress event on the common per-reel path.


class _Clock:
    """A settable monotonic clock: `_clock()` returns the current `now`, so the
    number of internal `_clock()` calls doesn't matter — the test drives time."""

    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class _ClockAdvancingFeed(FakeFeed):
    """Bumps a shared clock by `step` seconds BEFORE yielding each reel, so time
    advances deterministically as the loop walks the feed (no real sleeping)."""

    def __init__(self, reels, clock: _Clock, step: float):
        super().__init__(reels)
        self._clock = clock
        self._step = step

    def walk(self):
        for reel in self._reels:
            self._clock.now += self._step
            yield reel


def _irrelevant_reels(n: int):
    # "cat memes" never matches the "acme saas app" relevance_def → common path only.
    return [Reel(f"r{i}", caption="cat memes", comments=[]) for i in range(n)]


def _progress_events(path, run_id):
    from reelradar.core.store import Store
    store = Store(path)
    return [e for e in store.fetch_run_events(run_id)
            if e["phase"] == "feed_walk" and "Scanning" in e["message"]]


def test_scanning_irrelevant_reels_emits_throttled_progress_event():
    store, path = _store()
    clock = _Clock(0.0)
    # Advance well past the interval on every reel → each reel would be eligible,
    # but the throttle still spaces the emits out (first fires ~interval in).
    step = PROGRESS_EVENT_INTERVAL_SEC + 5.0
    feed = _ClockAdvancingFeed(_irrelevant_reels(4), clock, step)
    session = Session(store=store, router=MockRouter(store=store), feed=feed,
                      soul=None, campaign=_campaign(), pacer=_daytime_pacer(),
                      cfg=SessionConfig(), run_id="run-prog", clock=clock)
    out = session.run()

    assert out["matches"] == 0                    # all irrelevant → no lead logic changed
    assert out["reels_seen"] == 4
    events = _progress_events(path, "run-prog")
    assert events, "expected at least one 'Scanning' progress event on the common path"
    # The message reflects live counters.
    assert "reels seen" in events[0]["message"]


def test_progress_event_is_throttled_under_interval():
    store, path = _store()
    clock = _Clock(0.0)
    # Advance far LESS than the interval per reel → after the run-start baseline,
    # no single reel ever crosses the throttle window ⇒ zero progress events.
    step = PROGRESS_EVENT_INTERVAL_SEC / 10.0
    feed = _ClockAdvancingFeed(_irrelevant_reels(5), clock, step)
    session = Session(store=store, router=MockRouter(store=store), feed=feed,
                      soul=None, campaign=_campaign(), pacer=_daytime_pacer(),
                      cfg=SessionConfig(), run_id="run-throttle", clock=clock)
    out = session.run()

    assert out["reels_seen"] == 5
    assert _progress_events(path, "run-throttle") == []  # throttled: no extra emit
