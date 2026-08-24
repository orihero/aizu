"""Human-simulation timing + input jitter (ported from soliq's ``human.ts``).

A constant delay is NOT human; only jitter is. This module centralizes the
*micro* layer of human-like behaviour — triangular timing jitter, an action-kind
delay table, an occasional "wander off" long idle, per-endpoint rate floors, and
humanized mouse-move / multi-tick scroll / think→navigate→settle — so the CDP
engines (Instagram/X/LinkedIn) and the ``Pacer`` share one honest anti-bot
posture (residential IP + a real logged-in account + human pacing; no stealth
plugin, no ``navigator.webdriver`` patching, no UA spoofing).

Deliberate departures from soliq's ``human.ts``:

  - Seconds, not milliseconds — aizu's ``PacingConfig``/``CDPBaseConfig`` are
    already in seconds, so the RANGES table is soliq's ``/ 1000``.
  - A ``HumanSim`` *class* (soliq uses module-level functions + a module-global
    ``lastKeyedCall`` map). Instance state keeps ``rng``/``sleep``/``clock``
    injectable — the same test seam ``Pacer`` uses — and gives each instance its
    own keyed-gap map so pytest's shared process can't leak state across tests.
  - ``scroll()``/``goto()`` take a *callback* for the real ``page.mouse.wheel`` /
    ``page.goto`` so this module never imports ``core/cdp.py`` and ``CDPFeedBase``
    keeps its PlaywrightTimeout degrade-not-wedge body verbatim — the callback
    *is* that body, invoked once per tick / once total.

The whole layer is a no-op when ``HUMAN_SIM=off`` (degrading scroll/goto to a
single, jitter-free action — byte-identical to the pre-human-sim behaviour),
mirroring ``Pacer``'s ``AIZU_IGNORE_DAYTIME`` opt-out convention.
"""
from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Literal, Optional, Tuple

from .logsetup import get_logger
from .pw_owner import PlaywrightOwner, PlaywrightTimeout

log = get_logger(__name__)

ActionKind = Literal[
    "fetch", "profile", "nav", "scroll", "dwell", "think", "settle", "media",
    "engage"]

# Action-kind delay windows in SECONDS (soliq's RANGES table / 1000). "engage"
# is aizu-specific: the pre-click pause before a like/follow, seeded at wiring
# time from CDPBaseConfig.action_delay_min/max so that campaign knob survives.
RANGES: Dict[ActionKind, Tuple[float, float]] = {
    "fetch": (0.5, 1.9),
    "profile": (7.0, 15.0),   # rate-limit-sensitive endpoints, widened on purpose
    "nav": (1.8, 4.2),
    "scroll": (0.7, 2.3),
    "dwell": (3.5, 11.0),
    "think": (0.5, 2.4),
    "settle": (0.25, 1.2),
    "media": (0.4, 1.5),
    "engage": (0.8, 2.5),
}

# A small chance to tack a genuine "wander off" idle onto a nav/dwell/scroll,
# breaking up an otherwise-metronomic multi-scroll session.
LONG_IDLE_CHANCE = 0.06
LONG_IDLE_S: Tuple[float, float] = (7.0, 22.0)
_LONG_IDLE_KINDS = frozenset({"nav", "dwell", "scroll"})

# Gap between the discrete wheel notches of one humanized scroll.
WHEEL_TICK_GAP_S: Tuple[float, float] = (0.08, 0.26)


def triangular_pick(rng: random.Random, lo: float, hi: float,
                    speed: float = 1.0) -> float:
    """A triangular draw in ``[lo, hi]``, scaled by ``speed``.

    ``(u1 + u2) / 2`` clusters values near the midpoint (mean unchanged at
    ``(lo + hi) / 2``) instead of the flat-uniform distribution soliq calls out
    as a bot fingerprint. Shared by ``Pacer`` (macro dwell / between-reel timing)
    and ``HumanSim`` (micro jitter) — the one piece of timing code both layers
    agree on, so the "why triangular" rationale lives in exactly one place.
    """
    lo2 = max(0.0, lo)
    hi2 = max(lo2, hi)
    u = (rng.random() + rng.random()) / 2.0
    return (lo2 + u * (hi2 - lo2)) * speed


def _enabled_default() -> bool:
    """Human-sim is ON unless ``HUMAN_SIM=off`` (soliq's exact string toggle).
    Read at construction so a value in the env / inherited child env takes
    effect; an explicit ``HumanSimConfig(enabled=...)`` always wins."""
    return os.environ.get("HUMAN_SIM", "").strip().lower() != "off"


def _speed_default() -> float:
    """Global delay multiplier (``HUMAN_SIM_SPEED``): 0.5 = 2x faster, 2 = calmer.
    Malformed or non-positive values fall back to 1, matching soliq."""
    try:
        v = float(os.environ.get("HUMAN_SIM_SPEED", "1"))
    except ValueError:
        return 1.0
    return v if v > 0 else 1.0


@dataclass
class HumanSimConfig:
    enabled: bool = field(default_factory=_enabled_default)
    speed: float = field(default_factory=_speed_default)
    ranges: Dict[ActionKind, Tuple[float, float]] = field(
        default_factory=lambda: dict(RANGES))


class HumanSim:
    """Micro-layer human behaviour. Never raises from the input helpers
    (``mouse_move``/``scroll``/``goto`` degrade like every ``capture_*`` in
    ``core/cdp.py``); timing helpers are pure sleeps."""

    def __init__(self, cfg: Optional[HumanSimConfig] = None,
                 rng: Optional[random.Random] = None,
                 sleep: Optional[Callable[[float], None]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 call_timeout_s: Optional[float] = None):
        self.cfg = cfg or HumanSimConfig()
        self.rng = rng or random.Random()
        if sleep is None:
            sleep = time.sleep
        self._sleep = sleep
        self._clock = clock
        self._last_keyed_call: Dict[str, float] = {}
        # Hard wall-clock deadline for mouse_move's page.evaluate/mouse.move calls
        # (core/bounded.py) — neither one is reliably bounded by Playwright's own
        # per-page timeout (see core/cdp.py's CDPBaseConfig.js_timeout_ms note), so
        # without this a frozen viewport-probe/mouse-move call would hang the whole
        # walk despite mouse_move's own "never raises" contract. None (the default
        # for a bare ``HumanSim()``, e.g. in unit tests) disables bounding — CDP
        # engines wire this from ``cfg.js_timeout_ms`` in CDPFeedBase.__init__, the
        # same seeding pattern already used for ``cfg.ranges["engage"]``.
        self.call_timeout_s = call_timeout_s
        # The PlaywrightOwner whose thread owns the page these helpers touch.
        # ``CDPFeedBase.__init__`` INJECTS its own owner here — a HumanSim handed
        # a feed's page but not that feed's owner is a footgun: it would submit
        # work to a second thread and hit greenlet.error on every call. None
        # means "no owner wired"; ``_bounded`` then lazily creates one, which is
        # correct only because a bare HumanSim's page is never a real Playwright
        # object (see _bounded).
        self.owner = None

    # ---- timing ----
    def pick(self, rng_range: Tuple[float, float]) -> float:
        return triangular_pick(self.rng, rng_range[0], rng_range[1], self.cfg.speed)

    def human_seconds(self, kind: ActionKind) -> float:
        """The jittered duration for ``kind`` (no sleep). 0 when disabled."""
        if not self.cfg.enabled:
            return 0.0
        secs = self.pick(self.cfg.ranges[kind])
        if kind in _LONG_IDLE_KINDS and self.rng.random() < LONG_IDLE_CHANCE:
            secs += self.pick(LONG_IDLE_S)
        return secs

    def human_delay(self, kind: ActionKind) -> float:
        secs = self.human_seconds(kind)
        if secs > 0:
            self._sleep(secs)
        return secs

    def keyed_fetch(self, key: str, min_gap_s: float,
                    kind: ActionKind = "profile") -> None:
        """Enforce a MINIMUM wall-clock gap between calls sharing ``key`` (a
        per-endpoint rate floor), on top of the jittered ``kind`` delay. Several
        independent call sites can each jitter yet collectively machine-gun one
        endpoint; this floor guards against that (soliq's ``humanKeyedFetch``)."""
        if not self.cfg.enabled:
            return
        last = self._last_keyed_call.get(key, 0.0)
        floor = min_gap_s - (self._clock() - last)
        if floor > 0:
            self._sleep(floor * self.cfg.speed)
        self.human_delay(kind)
        self._last_keyed_call[key] = self._clock()

    def _bounded(self, fn):
        """Run ``fn()`` under ``call_timeout_s``, on Playwright's OWNING thread.

        ``fn`` is submitted to ``self.owner`` (``core/pw_owner.py``) — for a CDP
        engine that is the very thread that started Playwright, so the call
        never moves; only the wait crosses. This deliberately does NOT use
        ``core.bounded.call_bounded``, which runs the callable on a fresh daemon
        thread: Playwright's sync API is thread-affine and that raised
        ``greenlet.error`` on 100% of calls, silently killing every harvest. See
        ``CDPFeedBase._call_bounded`` for the full write-up (ledger D6).

        ``call_timeout_s is None`` (a bare ``HumanSim()``) keeps the old
        straight-through behaviour: no deadline, caller's thread, no thread
        spawned."""
        if self.call_timeout_s is None:
            return fn()
        owner = self.owner
        if owner is None:
            # No feed wired one in. Lazily own our own thread rather than
            # silently dropping the deadline: a caller that set call_timeout_s
            # asked for a bound. (Without this the AttributeError would be eaten
            # by mouse_move's outer `except Exception: return` and the deadline
            # would vanish with no signal at all.)
            owner = self.owner = PlaywrightOwner()
        return owner.call(fn, self.call_timeout_s, PlaywrightTimeout)

    # ---- input (best-effort; swallow ALL exceptions) ----
    def focus(self, page) -> None:
        """Bring ``page`` to the front before dispatching mouse input.

        THE FLEET-STALL ROOT CAUSE (2026-08-20). Chrome accepts
        ``Input.dispatchMouseEvent`` for a tab that does not hold input focus and
        then never ACKs it: the CDP command has no reply, so Playwright's future
        never resolves and the owner thread sits in that call forever. Only the
        WAIT is bounded, so the caller walks away while the owner stays poisoned —
        every later bounded call fast-fails, three of those trip
        ``_halt_if_owner_wedged`` and the whole session halts ``cdp_call_wedged``.
        Five fleet attempts dead-lettered that way while reels were still being
        scored relevant, because only the mouse path dies: goto/evaluate/screenshot
        and response interception need no focus and kept working.

        Directly observed, not inferred: the outstanding protocol message at the
        wedge was ``method=mouseMove`` with the owner idle in ``selectors.select()``
        and no lock held, and calling ``bring_to_front()`` from a separate process
        released a pending ``mouse.move`` in 26ms.

        Bounded and silent by the same contract as everything else here: this must
        never itself become the first call that wedges, and a browser that cannot
        raise a tab is not a reason to fail a run.
        """
        if page is None:
            return
        try:
            self._bounded(lambda: page.bring_to_front())
        except Exception as e:  # noqa: BLE001 — best-effort, never break the run
            # See core/cdp.py's _focus: a silent swallow here is exactly what hid
            # the original wedge. Never raise, but never be invisible either.
            log.debug("focus (bring_to_front) failed — mouse input may not ACK · %s",
                      type(e).__name__)
            return

    def mouse_move(self, page, box: Optional[dict] = None) -> None:
        """Drift the cursor to a random point (25-75% inside ``box``, else 15-85%
        of the viewport) with a 3-7 step interpolated path. Purely decorative —
        a frozen/absent mouse must never fail a run.

        Both Playwright calls below go through ``_bounded``: neither
        ``page.evaluate`` (once the CDP pipe itself has gone quiet) nor
        ``page.mouse.move`` (no timeout= argument, ever) is reliably capped by
        Playwright's own per-page timeout — without a real deadline here a
        frozen viewport probe or mouse move would silently hang the whole walk
        despite this method's "never raises" contract."""
        if not self.cfg.enabled or page is None:
            return
        # Focus FIRST: an unfocused tab never ACKs the mouse.move below, and that
        # single unreturned call poisons the owner thread for the rest of the run.
        self.focus(page)
        try:
            if box:
                x = box["x"] + box["width"] * (0.25 + self.rng.random() * 0.5)
                y = box["y"] + box["height"] * (0.25 + self.rng.random() * 0.5)
            else:
                try:
                    vp = self._bounded(lambda: page.evaluate(
                        "() => ({w: window.innerWidth || 1280, "
                        "h: window.innerHeight || 800})"))
                except Exception:  # noqa: BLE001 — viewport read is best-effort
                    vp = {"w": 1280, "h": 800}
                x = vp["w"] * (0.15 + self.rng.random() * 0.7)
                y = vp["h"] * (0.15 + self.rng.random() * 0.7)
            steps = 3 + int(self.rng.random() * 5)   # 3..7 interpolation steps
            self._bounded(lambda: page.mouse.move(round(x), round(y), steps=steps))
        except Exception:  # noqa: BLE001 — decorative, never break the run
            return

    def scroll(self, page, base: float, tick_fn: Callable[[float], None]) -> None:
        """Split a scroll of ~``base`` into 3-7 wheel notches (``tick_fn`` fires
        each ``page.mouse.wheel``), with triangular inter-tick gaps + a settle
        after. When disabled: ONE tick of the full ``base`` — byte-identical to a
        single wheel call. Total scroll distance is conserved (last tick clears
        the remainder)."""
        if page is None:
            return
        if not self.cfg.enabled:
            tick_fn(base)
            return
        total = round(base * (0.7 + self.rng.random() * 0.8))   # 0.7x..1.5x
        self.mouse_move(page)
        ticks = 3 + int(self.rng.random() * 5)   # 3..7 notches
        remaining = total
        for i in range(ticks):
            if remaining <= 0:
                break
            share = remaining if i == ticks - 1 else \
                round((total / ticks) * (0.6 + self.rng.random() * 0.8))
            delta = min(remaining, max(60, share))
            remaining -= delta
            tick_fn(delta)
            self._sleep(self.pick(WHEEL_TICK_GAP_S))
        self.human_delay("scroll")   # settle after the whole batch

    def goto(self, page, url: str, navigate_fn: Callable[[str], None],
             lead: bool = True) -> None:
        """think → navigate → cursor settle → orientation pause. When disabled:
        just ``navigate_fn(url)`` (its own nav-settle still applies). ``lead`` is
        False when the navigation is already preceded by a think/dwell elsewhere,
        to avoid double-pausing (soliq's ``humanGoto`` ``opts.lead``)."""
        if not self.cfg.enabled:
            navigate_fn(url)
            return
        if lead:
            self.human_delay("think")
        navigate_fn(url)
        self.mouse_move(page)
        self.human_delay("nav")
