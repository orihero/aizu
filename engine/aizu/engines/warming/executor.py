"""WarmingActionExecutor — the P1 warming write core (warming-writes PRD §3.3–§3.7).

Constructed ONCE per session, INSIDE ``WarmingSession`` (never elsewhere). It is
the sole warming-path caller of the feed write helpers
(``like_reel``/``save_reel``/``follow_author``/``share_reel``) and of
``store.log_action`` — stamping the per-org sentinel ``campaign_id`` + the backing
``account_id`` so every warming write joins on the account, never a real campaign.

Per relevant reel (§3.4): roll each enabled action's probability INDEPENDENTLY,
fire the allowed ones under that DAY's caps (seeded from
``store.action_counts_for_account_day`` so caps hold across sessions, decremented
in memory on each successful fire), with a randomized RIGHT-SKEWED delay before
and between actions (§3.6 — never a fixed constant).

Relevance is a cheap ZERO-ML, NO-API-KEY heuristic (O-relevance-model, Instagram):
case-insensitive token overlap of the reel's caption/author/ocr_text against the
campaign's seed_hashtags / seed_accounts / relevance_def. The LLM relevance gate
is Telegram-only and ships with the Telegram build — warming here performs NO
router scoring / upsert_match / add_to_watchlist / log_spend.

On an action-block (``feed.detect_action_block()``) it raises ``HaltSession`` with
kind ``account_challenge`` (O-halt-kind); it never persists or swallows — the
session maps that to ``raise_flag`` + the account → FLAGGED transition.
"""
from __future__ import annotations

import random
import re
from typing import Any, Optional

from ...core.config import Campaign
from ...core.feed import FeedSource, Reel
from ...core.pacing import Pacer
from ...core.store import Store
from ..base import HaltSession

# Per-platform action order (§3.4). A NEW mapping — does not exist elsewhere. The
# Instagram tuple is the only live entry in this build; Telegram ships separately.
_PLATFORM_ACTIONS: dict[str, tuple[str, ...]] = {
    "instagram": ("like", "save", "follow", "share"),
}

# Actions that scope to a reel (logged with reel_id); `follow` targets the author.
_REEL_SCOPED = frozenset({"like", "save", "share"})

# IG action-block halt maps to this EXISTING WARMING_CHALLENGE_KINDS member
# (O-halt-kind) — no new kind is introduced in this build.
_HALT_KIND = "account_challenge"

# O-dm-regex: structural hook for DM-share failure phrases. The live-sampled regex
# lands after the first real IG DM-block capture — do NOT invent phrases here.
# TODO(O-dm-regex): populate with live-sampled DM-block phrases once captured; the
# CDP feed's detect_action_block() is the source of truth until then.
_DM_BLOCK_PHRASES: tuple[str, ...] = ()

_TOKEN_RE = re.compile(r"[#@]?[\w]+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    """Case-insensitive token set, stripping a leading #/@ so '#marketing',
    '@growthlab' and the bare words all overlap."""
    out: set[str] = set()
    for raw in _TOKEN_RE.findall(text or ""):
        tok = raw.lstrip("#@").lower()
        if tok:
            out.add(tok)
    return out


class WarmingActionExecutor:
    def __init__(self, *, feed: FeedSource, store: Store, sentinel_campaign: str,
                 account: dict[str, Any], account_id: int, session_id: str,
                 pacer: Pacer, platform: str, campaign: Campaign,
                 rng: Optional[random.Random] = None,
                 now: Optional[float] = None) -> None:
        self.feed = feed
        self.store = store
        self.sentinel_campaign = sentinel_campaign
        self.account = account
        self.account_id = account_id
        self.session_id = session_id
        self.pacer = pacer
        self.platform = platform
        self.campaign = campaign
        self.rng = rng or random.Random()
        self._now = now
        self._actions = _PLATFORM_ACTIONS.get(platform, ())
        # Per-day SPENT counts per action_type, seeded from the DB (across-session
        # correct, §3.7) and INCREMENTED in memory on each successful fire. Remaining
        # capacity is always `cap - self._spent_today[action]`, never this dict raw.
        self._spent_today: dict[str, int] = self._seed_spent_today()
        # Relevance seed token set, built once from the campaign's seeds + def.
        self._relevance_tokens = _tokens(
            " ".join([*campaign.seed_hashtags, *campaign.seed_accounts,
                      campaign.relevance_def]))

    def _clock(self) -> float:
        import time
        return time.time() if self._now is None else self._now

    def _seed_spent_today(self) -> dict[str, int]:
        """The day's already-spent counts per action_type, read from the store so
        caps are per-account-per-day (robust across sessions). The cap itself is
        applied per-fire against the live ``budget`` (which carries the ceiling)."""
        return self.store.action_counts_for_account_day(
            self.account_id, now=self._clock()) or {}

    # ---- relevance (cheap, zero-ML heuristic; O-relevance-model) ----
    def _is_relevant(self, reel: Reel) -> bool:
        """True iff the reel shares a case-insensitive token with the campaign's
        seeds / relevance_def. With NO seeds defined the campaign cannot steer
        relevance, so every reel is treated as on-topic (home-feed warming)."""
        if not self._relevance_tokens:
            return True
        reel_tokens = _tokens(" ".join(
            [reel.caption or "", reel.author or "", reel.ocr_text or ""]))
        return bool(reel_tokens & self._relevance_tokens)

    # ---- right-skewed human delay (§3.6 — never a fixed constant) ----
    def _human_delay(self, budget) -> float:
        if self.rng.random() < budget.delay_long_p:
            t = self.rng.uniform(budget.delay_max, budget.delay_long_max)
        else:
            t = self.rng.uniform(budget.delay_min, budget.delay_max)
        self.pacer._sleep(t)
        return t

    def _spent(self, action: str) -> int:
        return int(self._spent_today.get(action, 0))

    def _cap_for(self, action: str, budget) -> int:
        return {"like": budget.likes, "save": budget.saves,
                "follow": budget.follows, "share": budget.shares}.get(action, 0)

    def _prob_for(self, action: str, budget) -> float:
        return {"like": budget.p_like, "save": budget.p_save,
                "follow": budget.p_follow, "share": budget.p_share}.get(action, 0.0)

    def _all_caps_exhausted(self, budget) -> bool:
        return all(self._cap_for(a, budget) - self._spent(a) <= 0
                   for a in self._actions)

    def maybe_act(self, reel: Reel, budget) -> None:
        """Evaluate ONE observed reel (§3.4). Read-only/observe short-circuits;
        irrelevant reels return (dwell already happened upstream); otherwise roll
        each action independently under its remaining per-day cap and fire the
        allowed ones with right-skewed delays. Raises HaltSession on an
        action-block. Returns None."""
        if budget.read_only or self._all_caps_exhausted(budget):
            return
        if not self._is_relevant(reel):
            return

        planned = [
            a for a in self._actions
            if (self._cap_for(a, budget) - self._spent(a)) > 0
            and self.rng.random() < self._prob_for(a, budget)
        ]
        if not planned:
            return

        self._human_delay(budget)                 # right-skewed pause BEFORE acting
        for i, action in enumerate(planned):
            if i > 0:
                self._human_delay(budget)          # right-skewed pause BETWEEN actions
            self._fire(action, reel)

    def _fire(self, action: str, reel: Reel) -> None:
        """Fire one action via the feed helper, log it, and decrement the day's cap
        on success. Consults ``detect_action_block`` after the attempt — a positive
        signal raises HaltSession (the session flags + FLAGGES the account)."""
        ok = self._invoke(action, reel)
        self.store.log_action(
            self.sentinel_campaign, action,
            reel_id=reel.reel_id if action in _REEL_SCOPED else None,
            target=self._target(action, reel), succeeded=ok,
            session_id=self.session_id, account_id=self.account_id,
            now=self._clock())
        if ok:
            self._spent_today[action] = self._spent(action) + 1
        if self.feed.detect_action_block():
            raise HaltSession("instagram action block during warming",
                              kind=_HALT_KIND)

    def _invoke(self, action: str, reel: Reel) -> bool:
        if action == "like":
            return bool(self.feed.like_reel(reel))
        if action == "save":
            return bool(self.feed.save_reel(reel))
        if action == "follow":
            return bool(self.feed.follow_author(reel))
        if action == "share":
            return bool(self.feed.share_reel(reel, target=self.campaign.share_target))
        return False

    def _target(self, action: str, reel: Reel) -> Optional[str]:
        if action == "follow":
            return reel.author or None
        if action == "share":
            return self.campaign.share_target or None
        return reel.reel_id
