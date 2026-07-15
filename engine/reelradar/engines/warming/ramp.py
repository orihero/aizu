"""Ramp budget as data (warming PRD §4.4).

Account days-under-management → an ``ActionBudget`` (how much engagement is
permitted this session). Pure data, ``min()``-clamped by a per-platform cap row.
The ``observe`` stage (days 0–3) is ``read_only`` — useful writes begin day 4
(§4.6). In P0 the WarmingSession ignores the write counts entirely (dwell-only);
the table ships now so P1 only flips the executor on, not the schedule.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ActionBudget:
    # --- P0 (unchanged): integer count ceilings + the read-only flag ---
    stage: str
    likes: int
    follows: int
    connects: int
    dwell_windows: int
    read_only: bool
    # --- P1 Instagram per-day count ceilings (warming-writes PRD §3.2) ---
    saves: int = 0
    shares: int = 0
    # --- P1 Telegram per-day count ceilings (warming-writes PRD §7). `joins` is
    # the network-growth write (channels-joined); `reacts` is the occasional
    # lightweight engagement. Both default 0 so IG/x/linkedin budgets are
    # unchanged. At most ONE join fires per session regardless of the daily cap. ---
    joins: int = 0
    reacts: int = 0
    # --- P1 per-action fire probabilities, rolled INDEPENDENTLY per relevant
    # reel (O6 — Instagram). Each new field defaults to 0/0.0 so every P0 call
    # site keeps constructing ActionBudget unchanged. ---
    p_like: float = 0.0
    p_save: float = 0.0
    p_follow: float = 0.0
    p_share: float = 0.0
    # Telegram react probability, rolled per dwelled message under the per-day
    # `reacts` cap (PRD §7). Defaults 0.0 so non-TG budgets never react.
    p_react: float = 0.0
    # --- P1 right-skewed inter-action delay envelope (seconds). A fixed cadence
    # is itself an automation fingerprint, so the executor draws from this. ---
    delay_min: float = 2.0
    delay_max: float = 8.0
    delay_long_p: float = 0.12     # prob. of an occasional longer "distraction" pause
    delay_long_max: float = 25.0   # ceiling of that longer pause


# O6 (Instagram) per-relevant-reel fire probabilities, rolled independently.
# Shared across the active stages; the per-day caps are the hard backstop.
_P_LIKE = 0.70
_P_SAVE = 0.30
_P_FOLLOW = 0.12
_P_SHARE = 0.05

# Telegram per-dwelled-message react probability (PRD §7). Constant across active
# stages; the per-day `reacts` cap is the hard backstop.
_P_REACT = 0.40

# (max_ramp_day_inclusive, budget). Conservative starts (O6 — tune in P1).
# `observe` stays read_only with every write cap 0. `share` only fires at
# ramp(8-14)+ (cap 0 below), at the lowest probability + tightest cap
# (O-share-ship). saves/shares are the P1 additions; likes/follows widen for IG.
_STAGES: tuple[tuple[int, ActionBudget], ...] = (
    # observe stays fully read_only — every write cap explicitly 0 (O6) so the
    # spec intent is on the line, not implied by ActionBudget's field defaults.
    (3,  ActionBudget("observe", likes=0, follows=0, connects=0, dwell_windows=2,
                      read_only=True, saves=0, shares=0,
                      joins=0, reacts=0, p_react=0.0)),
    # `share` is gated OFF at light (O-share-ship): both the cap (shares=0) AND the
    # probability (p_share=0.0) block it, so the data self-documents the intent and
    # an accidental shares=1 here still cannot leak a share at light stage. The TG
    # `joins`/`reacts` per-stage caps ride alongside (PRD §7); they reach output
    # only for the telegram platform cap — IG/x/linkedin caps zero them out.
    (7,  ActionBudget("light",   likes=15, follows=1, connects=1, dwell_windows=3,
                      read_only=False, saves=8, shares=0,
                      joins=1, reacts=3,
                      p_like=_P_LIKE, p_save=_P_SAVE, p_follow=_P_FOLLOW, p_share=0.0,
                      p_react=_P_REACT)),
    (14, ActionBudget("ramp",    likes=30, follows=3, connects=2, dwell_windows=3,
                      read_only=False, saves=15, shares=1,
                      joins=2, reacts=5,
                      p_like=_P_LIKE, p_save=_P_SAVE, p_follow=_P_FOLLOW, p_share=_P_SHARE,
                      p_react=_P_REACT)),
)
_SUSTAIN = ActionBudget("sustain", likes=50, follows=5, connects=3, dwell_windows=4,
                        read_only=False, saves=25, shares=2,
                        joins=3, reacts=8,
                        p_like=_P_LIKE, p_save=_P_SAVE, p_follow=_P_FOLLOW, p_share=_P_SHARE,
                        p_react=_P_REACT)

# Per-platform absolute ceilings, min()-clamped onto the stage budget. LinkedIn
# `connect` is high-commitment / hard-rate-limited, so its ceiling is the
# tightest (O6 may defer connects further). X has no `connect` surface. The
# Instagram row carries the P1 like/save/follow/share write ceilings (O6).
_PLATFORM_CAPS: dict[str, ActionBudget] = {
    "x":        ActionBudget("cap", likes=6, follows=2, connects=0, dwell_windows=4, read_only=False),
    "linkedin": ActionBudget("cap", likes=4, follows=0, connects=2, dwell_windows=4, read_only=False),
    "instagram": ActionBudget("cap", likes=50, follows=5, connects=0, dwell_windows=4,
                              read_only=False, saves=25, shares=2),
    # Telegram has no like/follow/save/share surface (PRD §7) — only join + react.
    # The ceilings are the sustain-stage values; per-stage caps from the stage
    # table are what actually shape the daily allowance. joins=3/reacts=8 backstop.
    "telegram": ActionBudget("cap", likes=0, follows=0, connects=0, dwell_windows=4,
                             read_only=False, saves=0, shares=0,
                             joins=3, reacts=8),
}


def _stage_for_day(ramp_day: int) -> ActionBudget:
    for max_day, budget in _STAGES:
        if ramp_day <= max_day:
            return budget
    return _SUSTAIN


def budget_for_day(ramp_day: int, platform: str) -> ActionBudget:
    """The ActionBudget for an account `ramp_day` days under management on
    `platform`, clamped by the platform's ceiling. `observe` stays read_only and
    is never widened by a cap."""
    stage = _stage_for_day(max(0, ramp_day))
    cap = _PLATFORM_CAPS.get(platform)
    if cap is None:
        return stage
    return ActionBudget(
        stage=stage.stage,
        likes=min(stage.likes, cap.likes),
        follows=min(stage.follows, cap.follows),
        connects=min(stage.connects, cap.connects),
        dwell_windows=stage.dwell_windows,
        read_only=stage.read_only,
        # P1 per-day count ceilings, also min()-clamped by the platform cap.
        saves=min(stage.saves, cap.saves),
        shares=min(stage.shares, cap.shares),
        # P1 Telegram per-day ceilings, min()-clamped by the platform cap. IG/x/
        # linkedin caps leave joins/reacts at 0, so only telegram carries them.
        joins=min(stage.joins, cap.joins),
        reacts=min(stage.reacts, cap.reacts),
        # Probabilities + delay envelope are stage-tuned, not platform-clamped.
        p_like=stage.p_like,
        p_save=stage.p_save,
        p_follow=stage.p_follow,
        p_share=stage.p_share,
        p_react=stage.p_react,
        delay_min=stage.delay_min,
        delay_max=stage.delay_max,
        delay_long_p=stage.delay_long_p,
        delay_long_max=stage.delay_long_max,
    )
