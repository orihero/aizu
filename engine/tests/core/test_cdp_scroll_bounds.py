"""Wall-clock bounds on the two CDP scroll paths (2026-08-20 fleet stall).

The invariant these lock: NO single call inside a session may hold the caller
longer than the bridge session watchdog's ``STALL_TIMEOUT_SEC`` (180s), because
``sessions.last_activity_at`` is only bumped between reels. Both scroll paths
could exceed that by construction and neither had a bound of its own:

  * ``_scroll()`` is ``human.scroll()`` — 3-7 wheel NOTCHES, each of them two
    independently-bounded owner calls (``mouse.wheel``, then the JS fallback).
    ``js_timeout_ms`` (15s) bounded a CALL, so one batch was worth up to
    7 x (15 + 15) = 210s whenever the owner un-wedged between notches. That is
    exactly what the live log shows: dozens of full-deadline "CDP scroll wheel
    timed out" lines rather than free fast-fails.
  * ``instagram/cdp.py``'s ``_open_comments_and_paginate()`` bounded ROUNDS
    (``max_comment_scrolls``), never seconds, and each round can fall through to
    a whole ``_scroll(page)``.

The Instagram test lives here rather than under ``tests/engines/`` because it is
the same invariant as the base-class ones and the two halves only make sense read
together.
"""
import random
import threading
import time as _time

import pytest

from aizu.core.cdp import CDPBaseConfig, CDPFeedBase, PlaywrightTimeout
from aizu.core.feed import Reel
from aizu.core.human import HumanSim, HumanSimConfig
from aizu.engines.base import HaltSession
from aizu.engines.instagram.cdp import CDPConfig as IGConfig
from aizu.engines.instagram.cdp import CDPFeed as IGFeed


class _CountingFeed(CDPFeedBase):
    """Concrete base subclass with human-sim FORCED ON (so a scroll really is a
    multi-notch batch) and every sleep stubbed out."""

    def __init__(self, cfg, clock=_time.monotonic, seed=7):
        human = HumanSim(HumanSimConfig(enabled=True, speed=1.0),
                         rng=random.Random(seed), sleep=lambda s: None,
                         clock=clock)
        super().__init__(cfg, clock=clock, human=human)

    def _url_hints(self):
        return ("/api/posts",)

    def _classify(self, url, body, response):
        pass

    def _sources(self):
        return []


class _WheelPage:
    """Duck-typed page that counts wheel attempts and can make each one hang,
    time out, or succeed. ``evaluate`` (the JS fallback + human-sim's viewport
    probe) succeeds unless told otherwise."""

    def __init__(self, mode="ok", tick=None, evaluate_raises=False):
        self.wheel_calls = 0
        self.evaluated = []
        self._evaluate_raises = evaluate_raises
        page = self

        class _Mouse:
            def wheel(self, dx, dy):
                page.wheel_calls += 1
                if tick is not None:
                    tick()
                if mode == "hang":
                    threading.Event().wait()
                if mode == "timeout":
                    raise PlaywrightTimeout("wheel timed out")
                if mode == "fastfail":
                    raise RuntimeError("wheel captured by an overlay")

            def move(self, x, y, steps=1):
                pass

        self.mouse = _Mouse()

    def evaluate(self, *a, **k):
        self.evaluated.append(a)
        if self._evaluate_raises:
            raise PlaywrightTimeout("evaluate timed out")
        return {"w": 1280, "h": 800}


def _cfg(**kw):
    base = dict(js_timeout_ms=120, settle_seconds=0.0, nav_settle_seconds=0.0)
    base.update(kw)
    return CDPBaseConfig(**base)


# ---- 1. one bad notch ends the batch (N = 1) --------------------------------

def test_a_scroll_batch_stops_after_the_first_notch_that_cannot_scroll():
    # N = 1. Every remaining notch of the SAME batch is the same call, on the
    # same page, through the same owner, inside a ~2s window — there is no
    # mechanism by which notch 2..7 differs once the wheel has timed out AND the
    # JS fallback has failed to rescue it. Retrying them buys nothing and costs
    # up to another 6 x (15s + 15s) of wall clock the session watchdog is
    # counting, plus the dozens of identical warnings the live log filled with.
    feed = _CountingFeed(_cfg())
    page = _WheelPage(mode="timeout", evaluate_raises=True)
    feed._scroll(page)
    assert page.wheel_calls == 1, (
        "the batch must give up after the first notch that proved it cannot "
        f"scroll, not retry all 3-7 (saw {page.wheel_calls})")


def test_a_hung_wheel_never_hangs_the_batch():
    # Regression guard (passes before and after): the owner-thread deadline
    # bounds the WAIT, not the CALL, so a wheel that never returns must still
    # release the caller. This is the D6 contract; the fix above must not
    # weaken it.
    feed = _CountingFeed(_cfg())
    page = _WheelPage(mode="hang")
    t0 = _time.monotonic()
    feed._scroll(page)
    assert _time.monotonic() - t0 < 2.0


def test_a_healthy_scroll_still_fires_every_notch_of_the_batch():
    # The regression guard for the above: a working wheel must keep its full
    # 3-7 notch humanized batch, or the anti-bot posture quietly degrades to one
    # metronomic jump per scroll.
    feed = _CountingFeed(_cfg())
    page = _WheelPage(mode="ok")
    feed._scroll(page)
    assert page.wheel_calls >= 3


def test_a_wheel_rescued_by_the_js_fallback_does_not_kill_the_batch():
    # The fallback exists for a wheel that fails FAST (modal/overlay/driver
    # hiccup) on a healthy pipe. That is the working path — it must not be
    # mistaken for a wedge and abort the rest of the scroll.
    feed = _CountingFeed(_cfg())
    page = _WheelPage(mode="fastfail")
    feed._scroll(page)
    assert page.wheel_calls >= 3
    assert page.evaluated, "the JS fallback must still run when the wheel fails fast"


# ---- 2. the batch has a wall clock, not just a notch count ------------------

def test_a_scroll_batch_stops_when_its_wall_clock_ceiling_is_gone():
    # The live shape: the wheel burns a FULL deadline, the owner then un-wedges,
    # the JS fallback succeeds, and the next notch gets a fresh full deadline.
    # Nothing marks the batch dead, so only a wall clock can stop it — without
    # one this page would be wheeled 3-7 times at 15s each (45-105s) inside a
    # single _scroll(), against a 180s watchdog fed only between reels.
    now = {"t": 0.0}
    clock = lambda: now["t"]

    def burn_a_full_deadline():
        now["t"] += 15.0

    feed = _CountingFeed(_cfg(max_scroll_seconds=20.0), clock=clock)
    page = _WheelPage(mode="timeout", tick=burn_a_full_deadline)
    feed._scroll(page)
    # deadline = 20s. notch1 runs at t=0 (→15), notch2 at t=15 (→30), notch3+
    # are past the ceiling and skipped.
    assert page.wheel_calls == 2, (
        f"the batch must stop on its own wall clock (saw {page.wheel_calls} "
        "wheel calls, i.e. ~30s+ of unheartbeated scrolling)")
    assert now["t"] <= 20.0 + 15.0   # ceiling + at most one in-flight call


def test_a_scroll_batch_that_is_already_out_of_clock_skips_the_js_fallback_too():
    # Wheel + fallback are two INDEPENDENT deadlines; spending the second one
    # after the first has already blown the ceiling is precisely the 15s+15s
    # doubling that put one _scroll() over the watchdog.
    now = {"t": 0.0}

    def burn_a_full_deadline():
        now["t"] += 15.0

    feed = _CountingFeed(_cfg(max_scroll_seconds=10.0), clock=lambda: now["t"])
    page = _WheelPage(mode="timeout", tick=burn_a_full_deadline)
    feed._scroll(page)
    assert page.wheel_calls == 1
    # human-sim's viewport probe evaluates once; the scroll FALLBACK must not.
    assert len(page.evaluated) <= 1, (
        "the JS fallback must not burn a second full deadline once the batch "
        "ceiling is already gone")


# ---- 3. the comment-dialog path has its OWN bound --------------------------

class _CommentPage:
    """Interaction page for _open_comments_and_paginate: the dialog-scroll
    evaluate can hang (wedging the owner) or cost wall clock.

    ``_impl_obj`` on the page AND its mouse is load-bearing: on a live session
    ``self._ipage`` is an ``OwnedPW``, and ``_scroll_comment_dialog`` /
    ``_open_comment_dialog`` call ``page.evaluate`` / ``page.mouse.click``
    DIRECTLY (not through ``_call_bounded``) — the proxy is the only thing
    bounding them. A raw fake here would hang the suite forever, which is
    precisely the production failure mode, so the fake has to be wrappable.
    """

    _impl_obj = object()

    def __init__(self, hang_after=None, tick=None):
        self.evaluate_calls = 0
        self._hang_after = hang_after
        self._tick = tick
        page = self

        class _Mouse:
            _impl_obj = object()

            def click(self, x, y):
                pass

            def move(self, x, y, steps=1):
                pass

            def wheel(self, dx, dy):
                if page._hang_after is not None:
                    threading.Event().wait()

        self.mouse = _Mouse()

    def evaluate(self, *a, **k):
        self.evaluate_calls += 1
        if self._tick is not None:
            self._tick()
        if self._hang_after is not None and self.evaluate_calls > self._hang_after:
            threading.Event().wait()
        # A truthy dict satisfies both callers: _open_comment_dialog reads x/y off
        # it, _scroll_comment_dialog only bool()s it (→ "a scroller was found", so
        # it never falls through to the page-wide _scroll).
        return {"x": 10.0, "y": 20.0}


def _ig_feed(**cfg_kw):
    base = dict(js_timeout_ms=120, settle_seconds=0.0, max_comment_scrolls=3)
    base.update(cfg_kw)
    feed = IGFeed(IGConfig(**base))
    # Force the multi-notch humanized scroll on, with no real sleeping.
    feed.human = HumanSim(HumanSimConfig(enabled=True), rng=random.Random(3),
                          sleep=lambda s: None)
    feed.human.owner = feed._owner
    feed.human.call_timeout_s = feed.cfg.js_timeout_ms / 1000.0
    return feed


def test_comment_pagination_stops_and_halts_once_the_owner_is_wedged():
    # `max_comment_scrolls` is a ROUND count, and every round of a wedged session
    # re-runs a dialog scroll it has already been told cannot work — the live
    # log's dozens of "comment-dialog scroll timed out — falling back" lines.
    # Worse, this path is reached from `_process_comments`, NOT from `walk()`, so
    # nothing ever escalated it: the session limped with no halt and no heartbeat.
    feed = _ig_feed()
    # hang_after=1: the dialog-open evaluate succeeds, the FIRST dialog scroll hangs.
    page = _CommentPage(hang_after=1)
    feed._ipage = feed._wrap_pw(page)

    t0 = _time.monotonic()
    with pytest.raises(HaltSession) as ei:
        feed._open_comments_and_paginate()
    assert _time.monotonic() - t0 < 3.0
    assert ei.value.reason == "cdp_call_wedged"
    assert ei.value.kind == "canary"
    # dialog-open evaluate + exactly ONE dialog-scroll evaluate; round 2 and 3
    # must never be attempted.
    assert page.evaluate_calls == 2, (
        f"the loop retried a wedged dialog scroll (evaluates={page.evaluate_calls})")


def test_comment_pagination_gives_up_on_its_own_wall_clock_not_just_round_count():
    now = {"t": 0.0}
    feed = _ig_feed(max_comment_pagination_seconds=30.0)
    feed._clock = lambda: now["t"]
    # Every evaluate (dialog open, then each dialog scroll) costs 20s.
    def spend_twenty_seconds():
        now["t"] += 20.0

    page = _CommentPage(tick=spend_twenty_seconds)
    feed._ipage = feed._wrap_pw(page)
    feed._open_comments_and_paginate()
    # open(→20) · round0 at t=20 (<30) scrolls (→40) · round1 at t=40 is over.
    assert page.evaluate_calls == 2
    assert now["t"] <= 30.0 + 20.0
