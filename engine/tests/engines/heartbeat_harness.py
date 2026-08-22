"""Shared fakes for the per-reel heartbeat tests of the three CDP engines.

Background (2026-08-20): the fleet's first real campaign dead-lettered after five
attempts, every one killed by the bridge-side SessionWatchdog with "stalled: no
activity for over 180s". ``sessions.last_activity_at`` was bumped only by
``_flush()`` -> ``store.update_counters`` — once per reel at the end of it, plus
the one flush between the cascade gate and the browser block. A single slow gate
(up to five chained model calls) or a comment loop N model calls deep therefore
emitted NOTHING for its whole duration and looked exactly like a wedge.

These fakes record an ORDERED TIMELINE of (model call, heartbeat write, browser
step) events. The assertions are about order and count, never about timestamps:
the injected clock does not move the real ``time.time()`` that ``last_activity_at``
records, so a timestamp assertion would pass vacuously against the old code — that
exact trap bit this repo on 2026-08-19.
"""
from __future__ import annotations

import os
import tempfile

from aizu.core.feed import FakeFeed
from aizu.core.router import Decision
from aizu.core.store import Store

# Timeline markers.
MODEL = "model:"          # a model call STARTED (prefix + stage)
TOUCH = "touch"           # store.touch_session — the fine-grained heartbeat
FLUSH = "flush"           # store.update_counters — the per-reel counters write
OPEN_REEL = "open_reel"
CAPTURE = "capture_frames"
FETCH = "fetch_comments"

BUMPS = (TOUCH, FLUSH)    # both write sessions.last_activity_at


class TimelineStore(Store):
    """Real Store (real SQLite, real schema) that records every write which bumps
    ``sessions.last_activity_at``. Subclassed rather than faked so the engines run
    against the production store code path."""

    def __init__(self, path: str, timeline: list[str]):
        super().__init__(path)
        self.timeline = timeline

    def touch_session(self, session_id: str) -> None:
        self.timeline.append(TOUCH)
        super().touch_session(session_id)

    def update_counters(self, session_id, counters) -> None:
        self.timeline.append(FLUSH)
        super().update_counters(session_id, counters)


class TimelineRouter:
    """Records every model call AT ENTRY, so a call that raises still appears.

    Relevance is deliberately UNSURE (confidence inside the default escalate band
    0.4–0.75) on both the copy and the vision pass, which is what drives the gate
    through its full chain: copy -> vision -> escalation. That chain is the one the
    live stall walked. A comment whose text contains "boom" raises — the auto-skip
    path, which must still leave a heartbeat behind (it burned the same wall-clock).
    """

    def __init__(self, timeline: list[str]):
        self.timeline = timeline

    def classify_text(self, *, instruction, content, campaign_id, stage,
                      session_id=None, system=None):
        self.timeline.append(MODEL + stage)
        if stage == "relevance":
            if content.startswith("[ESCALATED]"):
                return Decision(label="relevant", score=0.9, confidence=0.97)
            return Decision(label="relevant", score=0.5, confidence=0.5)   # unsure
        if "boom" in content:
            raise RuntimeError("malformed verdict")
        lead = "pricing" in content
        return Decision(label="yes" if lead else "no",
                        score=0.93 if lead else 0.05, confidence=0.97,
                        extracted={"phone": "+14155550142"} if lead else {})

    def classify_image(self, *, instruction, images_b64, campaign_id, stage,
                       session_id=None, system=None):
        self.timeline.append(MODEL + stage)
        return Decision(label="relevant", score=0.5, confidence=0.5)       # unsure


class TimelineFeed(FakeFeed):
    """FakeFeed that records the browser steps between which the watchdog used to
    see nothing: the lazy frame capture inside the gate, the permalink navigation,
    and the comment fetch. ``capture_frames`` returns a frame even though the reel
    has no pre-baked ones, so the cascade actually takes its vision tier."""

    def __init__(self, reels, timeline: list[str]):
        super().__init__(reels)
        self.timeline = timeline

    def capture_frames(self, reel, n: int = 3):
        self.timeline.append(CAPTURE)
        return ["ZmFrZS1mcmFtZQ=="]

    def open_reel(self, reel):
        self.timeline.append(OPEN_REEL)
        return True

    def fetch_comments(self, reel_id, since_cursor):
        self.timeline.append(FETCH)
        return super().fetch_comments(reel_id, since_cursor)


def make_store(timeline: list[str]) -> tuple[TimelineStore, str]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return TimelineStore(path, timeline), path


def indices(timeline: list[str], marker: str) -> list[int]:
    return [i for i, e in enumerate(timeline) if e == marker]


def model_indices(timeline: list[str], stage: str) -> list[int]:
    return indices(timeline, MODEL + stage)


def assert_bump_between_consecutive(timeline: list[str], positions: list[int],
                                    what: str) -> None:
    """Every adjacent pair in ``positions`` has at least one heartbeat write
    strictly between them — i.e. the worst-case gap spans ONE of ``what``, not N.

    Ordinal, not temporal, on purpose (see the module docstring)."""
    assert len(positions) >= 2, (
        f"the fixture must exercise at least two {what} for this to mean "
        f"anything; got {len(positions)} · timeline={timeline}")
    for a, b in zip(positions, positions[1:]):
        between = [e for e in timeline[a + 1:b] if e in BUMPS]
        assert between, (
            f"no heartbeat between two consecutive {what} "
            f"(timeline[{a}:{b + 1}]={timeline[a:b + 1]})")
