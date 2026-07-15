"""Warming session loop (warming PRD §4.5).

Mirrors the harvest session shape (``_emit`` phase ``'warming'``, ``start_session``
with the per-org sentinel campaign + ``engine_mode='warming'`` + ``account_id``,
``HaltSession`` folded into ``halt_reason``). In the ``observe`` stage it is
dwell-only (``read_only`` budget → ZERO writes, unchanged from P0). From the
``light`` stage on it dwells on every observed reel, then — if the reel is
relevant — lets a ``WarmingActionExecutor`` (constructed ONLY here) fire
like/save/follow/share under that day's per-account caps (warming-writes PRD §3).

Warming performs NO router scoring / ``upsert_match`` / ``add_to_watchlist`` /
``log_spend`` (§4.3): the relevance gate is a cheap zero-ML token-overlap heuristic
(O-relevance-model). The executor is the sole warming-path ``log_action`` caller.
``router``/``soul`` are accepted only to keep the engine entrypoint uniform.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import random

from ..base import HaltSession, session_crash_guard
from ...core import accounts as accounts_lib
from ...core.config import Campaign
from ...core.feed import FeedSource
from ...core.logsetup import SUCCESS_LEVEL, get_logger
from ...core.pacing import Pacer
from ...core.store import Store
from ... import warming_control
from .executor import WarmingActionExecutor
from .ramp import ActionBudget, budget_for_day

log = get_logger(__name__)

_LEVEL_MAP = {
    "debug": logging.DEBUG, "info": logging.INFO, "success": SUCCESS_LEVEL,
    "warn": logging.WARNING, "warning": logging.WARNING, "error": logging.ERROR,
}

MAX_SESSION_EVENTS = 500


@dataclass
class WarmingConfig:
    window_observe_items: int = 5        # home-feed items observed per dwell window
    # Stricter than harvest's 5 — a warming account must back off fast on drift.
    empty_interception_halt: int = 2


def _empty_summary(session_id: str, halt_reason: Optional[str] = None, *,
                   likes: int = 0, follows: int = 0) -> dict[str, Any]:
    """Uniform summary (engines.base.SUMMARY_KEYS) — warming reports zeros for the
    harvest-only counters and records its full work in accounts.detail (§4.7). From
    P1 it surfaces the warming engagement counts (likes/follows) it actually fired
    so the run summary is honest; saves/shares live in accounts.detail."""
    return {
        "session_id": session_id, "reels_seen": 0, "relevance_passes": 0,
        "matches": 0, "escalations": 0, "already_seen_skips": 0, "spend_usd": 0.0,
        "feed_health_flag": False, "likes": likes, "follows": follows,
        "halt_reason": halt_reason, "halt_kind": None,
    }


class WarmingSession:
    def __init__(self, *, store: Store, feed: FeedSource, campaign: Campaign,
                 account: Optional[dict[str, Any]], org_id: Optional[int],
                 pacer: Optional[Pacer] = None, cfg: Optional[WarmingConfig] = None,
                 session_id: Optional[str] = None, run_id: Optional[str] = None,
                 now: Optional[float] = None,
                 rng: Optional[random.Random] = None):
        self.store = store
        self.feed = feed
        self.campaign = campaign
        self.account = account
        self.org_id = org_id
        self.pacer = pacer or Pacer()
        self.cfg = cfg or WarmingConfig()
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.run_id = run_id
        self._now = now
        # The executor shares this rng so the deterministic-seed test pattern (and
        # the right-skewed delay distribution) is reproducible.
        self._rng = rng or random.Random()
        self._event_seq = 0
        self._empty_streak = 0
        self.dwell_windows = 0
        self.reels_observed = 0

    def _clock(self) -> float:
        import time
        return time.time() if self._now is None else self._now

    def _emit(self, phase: str, level: str, message: str,
              detail: Optional[dict] = None) -> None:
        ctx = (" · " + " ".join(
            f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}"
            for k, v in detail.items())) if detail else ""
        log.log(_LEVEL_MAP.get(level, logging.INFO), "[%s] %s%s", phase, message, ctx)
        if not self.run_id or self._event_seq >= MAX_SESSION_EVENTS:
            return
        self._event_seq += 1
        try:
            blob = json.dumps(detail, ensure_ascii=False) if detail else None
        except (TypeError, ValueError):
            blob = None
        self.store.emit_run_event(
            self.run_id, self._event_seq, phase, level, message,
            campaign_id=self.campaign.campaign_id, session_id=self.session_id,
            detail=blob)

    def _budget(self) -> ActionBudget:
        ramp_day = 0
        if self.account and self.account.get("added_at"):
            ramp_day = int((self._clock() - float(self.account["added_at"])) // 86400)
        return budget_for_day(ramp_day, self.campaign.platform)

    def _account_id(self) -> Optional[int]:
        return self.account["id"] if self.account else None

    def run(self) -> dict:
        plat = self.campaign.platform
        # A warming run with no warmable account cannot proceed — halt cleanly so
        # the batch records it (the account must be warm-registered first, §6.5).
        if self.account is None or self.org_id is None:
            self._emit("halt", "error", "No warmable account for campaign — register one")
            raise HaltSession("no warmable account")

        sentinel = accounts_lib.warming_sentinel_campaign(self.org_id)
        acct_id = self._account_id()
        self.store.start_session(self.session_id, sentinel, plat,
                                 run_id=self.run_id, org_id=self.org_id,
                                 engine_mode="warming", account_id=acct_id)
        # From here an unexpected exception (browser/network crash) would escape
        # before any end_session call, leaving the warming row stuck at 'running'.
        # The guard closes it as 'halted'; the completion / HaltSession end_session
        # calls below stay authoritative.
        with session_crash_guard(self.store, self.session_id):
            return self._run_after_start(plat, sentinel, acct_id)

    def _run_after_start(self, plat: str, sentinel: str,
                         acct_id: Optional[int]) -> dict:
        self._emit("warming", "info",
                   f"Warming started — @{self.account.get('username')} ({plat})",
                   {"accountId": acct_id, "platform": plat})

        # First warming touch moves a provisioned account into `warming` (§3.1).
        if self.account.get("state") == accounts_lib.PROVISIONED:
            self.store.update_account_lifecycle(
                acct_id, accounts_lib.WARMING, reason="warming_started",
                session_id=self.session_id)

        if not self.pacer.is_daytime():
            self.store.end_session(self.session_id, "halted", "outside daytime window")
            self._emit("halt", "error", "Halted: outside daytime window")
            raise HaltSession("outside daytime window")

        budget = self._budget()
        self._emit("warming", "info",
                   f"Ramp stage '{budget.stage}' — {budget.dwell_windows} window(s), "
                   f"{'read-only' if budget.read_only else 'engage'}",
                   {"stage": budget.stage, "readOnly": budget.read_only})

        # The executor is the SOLE warming-path write caller; it is constructed
        # only here, only when the stage permits writes. In the observe stage the
        # budget is read_only and we stay dwell-only (executor is never built).
        executor = None
        if not budget.read_only:
            executor = WarmingActionExecutor(
                feed=self.feed, store=self.store, sentinel_campaign=sentinel,
                account=self.account, account_id=acct_id,
                session_id=self.session_id, pacer=self.pacer, platform=plat,
                campaign=self.campaign, rng=self._rng, now=self._now)
        # Baseline the kill-switch at start. The CLI gates the kill-switch BEFORE a
        # run starts (warming_control), so the session does not re-enforce a switch
        # that was already closed when the run began — the mid-session re-check
        # (O-mid-session-daytime) only stops writes on a gate that OPEN→CLOSED transitions
        # during the run (daytime always; the kill-switch only if it was clear at start).
        self._kill_clear_at_start = warming_control.warming_kill_reason(
            self.store, org_id=self.org_id, platform=plat) is None

        halt_reason: Optional[str] = None
        try:
            walker = self.feed.walk()
            for _w in range(budget.dwell_windows):
                # O-mid-session-daytime: re-check BOTH gates at the top of every
                # dwell window. If either closes, stop issuing further writes this
                # run (finish observing, never abort mid-action).
                if executor is not None and self._writes_closed(plat):
                    executor = None

                # Empty-interception canary (PRD §9 tier 3) — stricter than harvest.
                if not self.feed.healthy():
                    self._empty_streak += 1
                    if self._empty_streak >= self.cfg.empty_interception_halt:
                        raise HaltSession("empty interception — feed capture drift")
                else:
                    self._empty_streak = 0

                observed = 0
                for reel in walker:
                    self.reels_observed += 1
                    observed += 1
                    self.pacer.dwell()                  # dwell is ALWAYS unconditional
                    # From light stage on, a relevant reel may get write actions
                    # under that day's caps (executor short-circuits read_only +
                    # irrelevant internally). A None executor = dwell-only window.
                    if executor is not None:
                        executor.maybe_act(reel, budget)
                    if observed >= self.cfg.window_observe_items:
                        break
                self.dwell_windows += 1
                self._emit("warming", "info",
                           f"Observed window {self.dwell_windows}/{budget.dwell_windows}",
                           {"observed": observed})
                if observed == 0:
                    break   # feed exhausted — nothing more to observe this run
                self.pacer.between_reels()

            self._persist_activity(budget)
            self.store.end_session(self.session_id, "completed")
            self._emit("warming", "success",
                       f"Warming completed — {self.dwell_windows} window(s), "
                       f"{self.reels_observed} observed",
                       {"windows": self.dwell_windows, "observed": self.reels_observed})
        except HaltSession as h:
            halt_reason = h.reason
            self._on_halt(h, acct_id)
            self.store.end_session(self.session_id, "halted", halt_reason)
            raise

        likes, follows = self._action_counts(acct_id)
        return _empty_summary(self.session_id, halt_reason,
                              likes=likes, follows=follows)

    def _writes_closed(self, platform: str) -> bool:
        """O-mid-session-daytime: True once the daytime window OR the warming
        kill-switch has closed — the run keeps observing but issues no more writes."""
        if not self.pacer.is_daytime():
            self._emit("warming", "warn",
                       "Daytime window closed mid-session — stopping further writes")
            return True
        # Only re-enforce the kill-switch if it was CLEAR when the run began (a
        # run started under a closed switch is a CLI concern, not re-litigated here).
        if self._kill_clear_at_start:
            reason = warming_control.warming_kill_reason(
                self.store, org_id=self.org_id, platform=platform)
            if reason:
                self._emit("warming", "warn",
                           f"Warming kill-switch closed mid-session — {reason}")
                return True
        return False

    def _on_halt(self, h: HaltSession, acct_id: Optional[int]) -> None:
        """Map a HaltSession to a health_flag (+ FLAGGED transition for an
        account-level action-block, O-halt-kind). An action-block raises kind
        ``account_challenge``; other halts keep the generic 'halt' flag."""
        if h.kind == "account_challenge":
            self.store.raise_flag("account_challenge", "halt", h.reason,
                                  org_id=self.org_id, account_id=acct_id,
                                  session_id=self.session_id)
            acct = self.store.get_account(acct_id) if acct_id else None
            if acct is not None and accounts_lib.can_transition(
                    acct["state"], accounts_lib.FLAGGED):
                self.store.update_account_lifecycle(
                    acct_id, accounts_lib.FLAGGED, reason="account_challenge",
                    session_id=self.session_id)
        else:
            self.store.raise_flag("halt", "halt", h.reason,
                                  org_id=self.org_id, account_id=acct_id,
                                  session_id=self.session_id)
        self._emit("halt", "error", f"Halted: {h.reason}", {"reason": h.reason})

    def _action_counts(self, acct_id: Optional[int]) -> tuple[int, int]:
        """The like/follow counts this account fired TODAY (for the run summary).
        Saves/shares live in accounts.detail; the summary's harvest-shaped fields
        only carry like/follow."""
        if acct_id is None:
            return 0, 0
        day = self.store.action_counts_for_account_day(acct_id, now=self._clock())
        return int(day.get("like", 0)), int(day.get("follow", 0))

    def _persist_activity(self, budget: ActionBudget) -> None:
        """Warming counters live in accounts.detail (§4.7), not the harvest columns.
        Refresh last_warmed_at + ramp_day on a clean run (no state change). From P1
        the per-action warming write counts (likes/saves/follows/shares fired TODAY)
        are stamped here so the warmth model + panel can read them off the account."""
        acct_id = self._account_id()
        acct = self.store.get_account(acct_id)
        if acct is None:
            return
        detail = dict(acct.get("detail") or {}) if isinstance(acct.get("detail"), dict) else {}
        # observe = dwell-only; engage = the stage issued (or could issue) writes.
        kind = "observe" if budget.read_only else "engage"
        day_counts = self.store.action_counts_for_account_day(
            acct_id, now=self._clock()) if acct_id else {}
        detail.update({
            "last_activity_kind": kind,
            "dwell_windows": self.dwell_windows,
            "reels_observed": self.reels_observed,
            "warming_likes": int(day_counts.get("like", 0)),
            "warming_saves": int(day_counts.get("save", 0)),
            "warming_follows": int(day_counts.get("follow", 0)),
            "warming_shares": int(day_counts.get("share", 0)),
        })
        ramp_day = 0
        if acct.get("added_at"):
            ramp_day = int((self._clock() - float(acct["added_at"])) // 86400)
        self.store.update_account_ramp(
            acct_id, ramp_day=ramp_day, warmth_floor=budget_floor(budget),
            last_warmed_at=self._clock())
        # No-op lifecycle write just to stamp detail (state unchanged).
        self.store.update_account_lifecycle(
            acct_id, acct["state"], reason="warming_observed", detail=detail)


def budget_floor(budget: ActionBudget) -> float:
    """A coarse ramp-curve floor for the stage (kept for the warmth `ramp` model)."""
    return {"observe": 0.1, "light": 0.35, "ramp": 0.6, "sustain": 0.85}.get(
        budget.stage, 0.0)


def run_warming_session(*, campaign, store, router, feed, soul, pacer,
                        run_id=None) -> dict:
    """Engine entrypoint for ``engine_mode='warming'`` (dispatch.run_engine_session).
    Resolves the campaign's backing pool account and runs ONE warming session. A
    HaltSession is folded into the summary's ``halt_reason``.

    Dispatch BY PLATFORM (warming-writes PRD §7): ``telegram`` runs the distinct
    search→join→dwell→react routine; every other platform runs the IG/feed-walk
    path below, byte-for-byte unchanged."""
    if campaign.platform == "telegram":
        from .telegram import run_telegram_warming_session
        return run_telegram_warming_session(
            campaign=campaign, store=store, feed=feed, pacer=pacer, run_id=run_id)
    org_id = store.org_for_campaign(campaign.campaign_id)
    account = store.resolve_account_for_campaign(campaign.campaign_id, campaign.platform)
    session = WarmingSession(store=store, feed=feed, campaign=campaign,
                             account=account, org_id=org_id, pacer=pacer,
                             run_id=run_id)
    try:
        return session.run()
    except HaltSession as h:
        return _empty_summary(session.session_id, h.reason)
