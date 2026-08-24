"""HumanSim micro-layer (core/human.py) — pure, no Playwright/network.

Locks the ported soliq ``human.ts`` contract: triangular jitter within the
action-kind window, per-endpoint keyed rate floors, distance-conserving
multi-tick scroll, best-effort mouse-move that never raises, and the
``HUMAN_SIM=off`` degrade to a single jitter-free action.
"""
import random
import threading
import time

from aizu.core.human import (
    RANGES,
    HumanSim,
    HumanSimConfig,
    triangular_pick,
)


def _enabled(rng=None, sleep=None, clock=None, speed=1.0):
    cfg = HumanSimConfig(enabled=True, speed=speed)
    return HumanSim(cfg=cfg, rng=rng or random.Random(0),
                    sleep=sleep or (lambda _t: None),
                    clock=clock or (lambda: 0.0))


def _disabled(sleep=None):
    cfg = HumanSimConfig(enabled=False)
    return HumanSim(cfg=cfg, rng=random.Random(0),
                    sleep=sleep or (lambda _t: None))


# ---- triangular_pick / pick ----

def test_triangular_pick_stays_within_bounds_and_is_deterministic():
    a = triangular_pick(random.Random(1), 2.0, 8.0)
    b = triangular_pick(random.Random(1), 2.0, 8.0)
    assert a == b                       # same seed → same draw
    for _ in range(2000):
        v = triangular_pick(random.Random(), 2.0, 8.0)
        assert 2.0 <= v <= 8.0


def test_triangular_pick_mean_is_midpoint():
    rng = random.Random(42)
    n = 20000
    avg = sum(triangular_pick(rng, 0.0, 10.0) for _ in range(n)) / n
    assert abs(avg - 5.0) < 0.15        # centred on the midpoint


def test_speed_scales_the_draw():
    slow = triangular_pick(random.Random(7), 2.0, 4.0, speed=2.0)
    base = triangular_pick(random.Random(7), 2.0, 4.0, speed=1.0)
    assert abs(slow - 2 * base) < 1e-9


# ---- human_seconds / human_delay ----

def test_human_seconds_within_action_window():
    h = _enabled()
    for _ in range(500):
        s = h.human_seconds("think")
        lo, hi = RANGES["think"]
        # long-idle only applies to nav/dwell/scroll, so think is bounded tight
        assert lo <= s <= hi


def test_disabled_human_seconds_is_zero_and_no_sleep():
    slept = []
    h = _disabled(sleep=lambda t: slept.append(t))
    assert h.human_seconds("nav") == 0.0
    assert h.human_delay("nav") == 0.0
    assert slept == []                  # disabled never sleeps


def test_human_delay_sleeps_the_drawn_duration():
    slept = []
    h = _enabled(sleep=lambda t: slept.append(t))
    got = h.human_delay("dwell")
    assert slept == [got] and got > 0


def test_long_idle_only_extends_nav_dwell_scroll():
    # Force the 6% branch to always fire, then to never fire, and confirm only
    # nav/dwell/scroll can exceed their base window.
    always = _enabled(rng=_ForcedRandom(0.0))     # random() == 0 < 0.06 → fires
    s = always.human_seconds("scroll")
    lo, hi = RANGES["scroll"]
    assert s > hi                                 # base window + long-idle add-on
    # "think" is not a long-idle kind → never extended even when the roll fires
    assert always.human_seconds("think") <= RANGES["think"][1]


# ---- keyed_fetch (per-endpoint rate floor) ----

def test_keyed_fetch_enforces_minimum_gap():
    now = {"t": 0.0}
    slept = []
    h = _enabled(sleep=lambda t: slept.append(t), clock=lambda: now["t"])
    # First call: no prior timestamp → floor is negative → no floor sleep,
    # just the jittered "profile" delay.
    h.keyed_fetch("web_profile_info", min_gap_s=10.0)
    first_sleeps = list(slept)
    assert all(s >= 0 for s in first_sleeps)
    # Advance the clock only 3s and call again → must sleep ~7s to reach the floor.
    now["t"] = 3.0
    slept.clear()
    h.keyed_fetch("web_profile_info", min_gap_s=10.0)
    assert any(abs(s - 7.0) < 1e-9 for s in slept)   # floor gap enforced


def test_keyed_fetch_no_floor_after_gap_elapsed():
    now = {"t": 0.0}
    slept = []
    h = _enabled(sleep=lambda t: slept.append(t), clock=lambda: now["t"])
    h.keyed_fetch("k", min_gap_s=5.0)
    now["t"] = 100.0                     # well past the floor
    slept.clear()
    h.keyed_fetch("k", min_gap_s=5.0)
    # Only the jittered profile delay, no multi-second floor sleep.
    assert all(s < RANGES["profile"][1] for s in slept)


def test_keyed_fetch_gap_map_is_per_instance():
    a = _enabled()
    b = _enabled()
    a.keyed_fetch("shared", min_gap_s=5.0)
    assert "shared" in a._last_keyed_call
    assert "shared" not in b._last_keyed_call   # no cross-instance leakage


# ---- scroll (multi-tick, distance-conserving) ----

def test_scroll_conserves_distance_and_bounds_tick_count():
    h = _enabled()
    deltas = []
    h.scroll(_FakePage(), base=2400, tick_fn=lambda d: deltas.append(d))
    assert 1 <= len(deltas) <= 7
    total = sum(deltas)
    assert 0.7 * 2400 - 1 <= total <= 1.5 * 2400 + 1   # within 0.7x..1.5x band


def test_scroll_disabled_is_single_full_tick():
    h = _disabled()
    deltas = []
    h.scroll(_FakePage(), base=900, tick_fn=lambda d: deltas.append(d))
    assert deltas == [900]              # byte-identical to a single wheel


def test_scroll_none_page_is_noop():
    h = _enabled()
    called = []
    h.scroll(None, base=900, tick_fn=lambda d: called.append(d))
    assert called == []


# ---- mouse_move (best-effort) ----

def test_mouse_move_never_raises_on_frozen_page():
    h = _enabled()

    class _Frozen:
        def evaluate(self, *a, **k):
            raise RuntimeError("frozen")

        class mouse:  # noqa: N801 — mimic playwright's page.mouse
            @staticmethod
            def move(*a, **k):
                raise RuntimeError("frozen")

    h.mouse_move(_Frozen())            # must not raise


def test_mouse_move_uses_box_and_moves_with_steps():
    h = _enabled()
    page = _FakePage()
    h.mouse_move(page, box={"x": 100, "y": 200, "width": 40, "height": 20})
    assert page.mouse.moves, "expected a mouse.move call"
    (mx, my, steps) = page.mouse.moves[-1]
    assert 100 + 40 * 0.25 <= mx <= 100 + 40 * 0.75
    assert 200 + 20 * 0.25 <= my <= 200 + 20 * 0.75
    assert 3 <= steps <= 7


def test_mouse_move_disabled_is_noop():
    h = _disabled()
    page = _FakePage()
    h.mouse_move(page)
    assert page.mouse.moves == []


# Was SKIPPED while `_call_bounded`/`_bounded` enforced no deadline (ledger D6) —
# with nothing bounding the call this HUNG FOREVER instead of failing. The deadline
# is back, on Playwright's owning thread (`core/pw_owner.py`), so it is live again.
def test_mouse_move_call_timeout_s_bounds_a_page_that_hangs_forever():
    """Hang-prevention fix #1: neither page.evaluate (once the CDP pipe itself
    goes quiet) nor page.mouse.move (no timeout= argument, ever) is reliably
    bounded by Playwright's own per-page timeout — call_timeout_s (wired from
    CDPFeedBase.__init__ via cfg.js_timeout_ms) is what actually returns control."""
    h = _enabled()
    h.call_timeout_s = 0.15

    class _HangingPage:
        def evaluate(self, *a, **k):
            threading.Event().wait()   # never returns — models a frozen CDP pipe

        class mouse:  # noqa: N801 — mimic playwright's page.mouse
            @staticmethod
            def move(*a, **k):
                threading.Event().wait()

    t0 = time.monotonic()
    h.mouse_move(_HangingPage())   # must not hang despite both calls freezing
    assert time.monotonic() - t0 < 2.0


def test_bare_human_sim_has_no_call_timeout_by_default():
    # A caller that never wires call_timeout_s (e.g. a bare HumanSim() in tests
    # outside a CDP engine) keeps the pre-existing, straight-through behavior.
    h = _enabled()
    assert h.call_timeout_s is None


# ---- goto (think → navigate → settle → orient) ----

def test_goto_disabled_only_navigates():
    h = _disabled()
    navs = []
    h.goto(_FakePage(), "https://x.test", navigate_fn=lambda u: navs.append(u))
    assert navs == ["https://x.test"]


def test_goto_enabled_navigates_once_and_delays():
    slept = []
    h = _enabled(sleep=lambda t: slept.append(t))
    navs = []
    h.goto(_FakePage(), "https://x.test", navigate_fn=lambda u: navs.append(u))
    assert navs == ["https://x.test"]  # navigation happens exactly once
    assert len(slept) >= 2             # a think lead-in + a nav orient pause


# ---- helpers ----

class _ForcedRandom(random.Random):
    """random() always returns a fixed value; everything else is real."""

    def __init__(self, value):
        super().__init__(0)
        self._value = value

    def random(self):
        return self._value


class _FakePage:
    def __init__(self):
        self.mouse = _FakeMouse()

    def evaluate(self, *a, **k):
        return {"w": 1280, "h": 800}


class _FakeMouse:
    def __init__(self):
        self.moves = []
        self.wheels = []

    def move(self, x, y, steps=1):
        self.moves.append((x, y, steps))

    def wheel(self, dx, dy):
        self.wheels.append((dx, dy))
