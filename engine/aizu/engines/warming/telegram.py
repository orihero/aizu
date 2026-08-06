"""Telegram warming routine (warming-writes PRD §7.3–§7.6).

Telegram warming is a paradigm-distinct loop from the IG reel-walk: ``discover →
relevance-gate (search hits only) → join (per-day capped) → dwell → react
(occasional)``. No like/follow/share, no DMs, no message sends.

Discovery candidates are the campaign's operator-seeded ``seed_channels``
(TRUSTED — always kept, skip the gate) plus keyword-search hits from the
``TelegramWarmingPort`` (UNTRUSTED — gated by the thin LLM relevance helper). When
no ``OPENROUTER_API_KEY`` is available the routine DEGRADES to seeded-only (skip
search + its gate) so warming still runs — this is the single ML touch on any
warming path (PRD §7.4, X2), and it is fail-closed and degradeable.

Joins are capped at ONE per session, under the per-day ``joins`` cap read from
``store.action_counts_for_account_day`` (robust across sessions/days). Reacts fire
probabilistically (``p_react``) on dwelled messages under the per-day ``reacts``
cap. Every successful join/react is logged via ``store.log_action`` with the
per-org sentinel campaign + the backing ``account_id`` (the warmth ``network``
join key) — never a real campaign.

A HARD flood (``PeerFloodError`` / ``ChannelsTooMuchError`` / a large
``FloodWaitError``, surfaced as ``TelegramFloodError(is_hard=True)``) raises
``HaltSession`` so the session raises a ``peer_flood`` flag and FLAGS the account.
A SMALL ``FloodWaitError`` (``is_hard=False``) skips that one action and continues.
``observe`` stage stays read-only — zero writes.
"""
from __future__ import annotations

import random
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ... import warming_control
from ...core import accounts as accounts_lib
from ...core.config import Campaign
from ...core.logsetup import get_logger
from ...core.pacing import Pacer
from ...core.store import Store
from ..base import HaltSession, session_crash_guard
from ..telegram.feed import TelegramClientPort, TgMessage
from ..telegram.warming_writes import (
    REACTION_ALLOWLIST,
    TelegramFloodError,
    TelegramWarmingPort,
    TgChannel,
)
from .ramp import ActionBudget, budget_for_day
from .session import _empty_summary, budget_floor

log = get_logger(__name__)

# The HaltSession kind a hard TG flood raises. It is a distinct HaltKind member
# ("peer_flood", NOT the IG "account_challenge" — the two must not be conflated so
# a shared halt handler never mis-classifies a TG flood as an IG action-block).
# "peer_flood" is also a WARMING_CHALLENGE_KINDS member so a fresh block craters
# warmth; the TG session handler raises a peer_flood flag + FLAGGED transition.
HARD_FLOOD_HALT_KIND = "peer_flood"

# Recent messages to read per joined channel when dwelling (read-only history).
DWELL_HISTORY_LIMIT = 20

# Keyword phrases are derived from the campaign's existing relevance/seed prose
# (PRD §7.3 — no new TG-keyword config knob, YAGNI). Cap the count so a runaway
# brief can't fan out into dozens of search RPCs.
_MAX_KEYWORDS = 6
_KEYWORD_SPLIT = re.compile(r"[\n,.;]+")

# Over-fetch a little past the per-day join cap on each search keyword: most hits
# are filtered out by the relevance gate, so a bare ``cap`` would often leave zero
# joinable candidates. Slack keeps a few gate-survivors in reach without fanning
# out into a large RPC.
_SEARCH_SLACK = 5


# A relevance gate is `(TgChannel, campaign) -> bool`. The live build supplies a
# thin LLM helper (see `build_relevance_gate`); tests inject a fake. `None` means
# DEGRADE to seeded-only (no key / no gate available): search hits are skipped.
RelevanceGate = Callable[[TgChannel, Campaign], bool]


@dataclass(frozen=True)
class _Candidate:
    """A discovery candidate, carrying whether it came from search (gated) or a
    trusted operator seed (always kept)."""
    channel: str
    is_search_hit: bool
    tg: Optional[TgChannel] = None


def derive_keywords(campaign: Campaign) -> list[str]:
    """Keyword phrases from the campaign's seed_direction + goal (PRD §7.3).

    Split on sentence/list separators, trimmed, de-duped, order-preserving, and
    capped. Empty when the campaign has no prose to search from."""
    source = " . ".join(p for p in (campaign.seed_direction, campaign.goal) if p)
    out: list[str] = []
    seen: set[str] = set()
    for raw in _KEYWORD_SPLIT.split(source):
        phrase = raw.strip()
        key = phrase.lower()
        if phrase and key not in seen:
            seen.add(key)
            out.append(phrase)
    return out[:_MAX_KEYWORDS]


class TelegramWarmingExecutor:
    """Drives the TG warming write loop for ONE session. Constructed by
    ``run_telegram_warming_session`` (the only caller), mirroring how the IG
    ``WarmingActionExecutor`` is built only inside ``WarmingSession``.

    All collaborators are injected so tests drive a ``FakeTelegramWarmingPort`` +
    a fake relevance gate + a seeded rng + a no-op-sleep ``Pacer``.
    """

    def __init__(self, *, port: TelegramWarmingPort, store: Store,
                 sentinel_campaign: str, account_id: int, session_id: str,
                 pacer: Pacer, campaign: Campaign,
                 relevance_gate: Optional[RelevanceGate] = None,
                 read_client: Optional[TelegramClientPort] = None,
                 rng: Optional[random.Random] = None,
                 now: Optional[float] = None) -> None:
        self.port = port
        self.store = store
        self.sentinel_campaign = sentinel_campaign
        self.account_id = account_id
        self.session_id = session_id
        self.pacer = pacer
        self.campaign = campaign
        self.relevance_gate = relevance_gate
        self.read_client = read_client
        self.rng = rng or random.Random()
        self._now = now
        # Per-day SPENT counts, seeded from the DB (across-session correct) and
        # incremented in memory on each successful fire.
        spent = self.store.action_counts_for_account_day(
            account_id, now=self._clock()) or {}
        self._joins_today = int(spent.get("join", 0))
        self._reacts_today = int(spent.get("react", 0))
        self._joined_this_session = 0

    def _clock(self) -> float:
        import time
        return time.time() if self._now is None else self._now

    # ---- discovery (§7.3) ----
    def discover(self, budget: ActionBudget) -> list[_Candidate]:
        """Seeded channels first (trusted), then keyword-search hits (gated).

        Search is skipped entirely when no relevance gate is available (degrade to
        seeded-only) — the gate is the only thing that can vet an untrusted hit, so
        without it an untrusted hit cannot be joined safely (PRD §7.4 fail-closed).
        """
        out: list[_Candidate] = [
            _Candidate(channel=ch, is_search_hit=False)
            for ch in self.campaign.seed_channels if ch
        ]
        seen = {c.channel for c in out}
        if self.relevance_gate is None:
            return out                       # degraded: seeded-only
        for keyword in derive_keywords(self.campaign):
            for hit in self._search(keyword, budget):
                if hit.username and hit.username not in seen:
                    seen.add(hit.username)
                    out.append(_Candidate(channel=hit.username,
                                          is_search_hit=True, tg=hit))
        return out

    def _search(self, keyword: str, budget: ActionBudget) -> list[TgChannel]:
        """A single read-only search RPC, flood-classified like the writes: a hard
        flood halts the whole session; a soft flood skips THIS keyword and the
        caller continues. Search is read-only but Telegram still rate-limits it."""
        try:
            return list(self.port.search(
                keyword, limit=budget.joins + _SEARCH_SLACK))
        except TelegramFloodError as e:
            if e.is_hard:
                raise HaltSession("telegram flood during warming search",
                                  kind=HARD_FLOOD_HALT_KIND) from e
            log.info("Telegram search soft-skip (flood wait %ss) · keyword=%r",
                     e.retry_seconds, keyword)
            return []

    def _passes_gate(self, cand: _Candidate) -> bool:
        """Trusted seeds always pass; search hits are vetted by the gate."""
        if not cand.is_search_hit:
            return True
        if self.relevance_gate is None or cand.tg is None:
            return False                     # fail-closed: no gate ⇒ no join
        return bool(self.relevance_gate(cand.tg, self.campaign))

    # ---- right-skewed inter-action delay (§8.1a — never a fixed constant) ----
    def _delay(self, budget: ActionBudget) -> None:
        if self.rng.random() < budget.delay_long_p:
            t = self.rng.uniform(budget.delay_max, budget.delay_long_max)
        else:
            t = self.rng.uniform(budget.delay_min, budget.delay_max)
        self.pacer._sleep(t)

    def run(self, budget: ActionBudget, *,
            daytime_open: Callable[[], bool]) -> None:
        """Execute the discover → join → dwell → react loop for this session.

        ``observe`` (read_only) and a zero join+react budget short-circuit to a
        pure no-op (zero writes). Re-checks the daytime gate before every write so
        a session crossing the boundary stops issuing writes mid-run (it finishes
        cleanly, never aborts mid-action). Raises ``HaltSession`` on a hard flood.
        """
        if budget.read_only or (budget.joins + budget.reacts) == 0:
            return
        for cand in self.discover(budget):
            if not daytime_open():
                return                       # window closed — stop issuing writes
            if self._joined_this_session >= 1:
                break                        # at most ONE join per session
            if self._joins_today >= budget.joins:
                break                        # per-day join cap reached
            if not self._passes_gate(cand):
                continue
            self._delay(budget)              # right-skewed pause before the join
            joined = self._join(cand.channel)
            if joined:
                self._dwell_and_react(cand.channel, budget, daytime_open)

    def _join(self, channel: str) -> bool:
        """Attempt a join; log it; advance the day/session counters on success.

        A hard flood raises HaltSession; a soft flood skips THIS join and the
        caller continues. The flood error is classified by the port, never here.
        """
        try:
            ok = bool(self.port.join(channel))
        except TelegramFloodError as e:
            if e.is_hard:
                raise HaltSession("telegram flood during warming join",
                                  kind=HARD_FLOOD_HALT_KIND) from e
            log.info("Telegram join soft-skip (flood wait %ss) · %s",
                     e.retry_seconds, channel)
            return False
        self.store.log_action(
            self.sentinel_campaign, "join", reel_id=None, target=channel,
            succeeded=ok, session_id=self.session_id,
            account_id=self.account_id, now=self._clock())
        if ok:
            self._joins_today += 1
            self._joined_this_session += 1
        return ok

    def _dwell_and_react(self, channel: str, budget: ActionBudget,
                         daytime_open: Callable[[], bool]) -> None:
        """Read recent history (read-only dwell) and probabilistically react to one
        dwelled message under the per-day react cap."""
        messages = self._read_history(channel)
        self.pacer.dwell()                   # dwell is unconditional after a join
        if self._reacts_today >= budget.reacts:
            return
        if not messages:
            return
        if self.rng.random() >= budget.p_react:
            return                           # occasional, not every channel
        if not daytime_open():
            return
        msg = self.rng.choice(messages)
        emoji = self.rng.choice(REACTION_ALLOWLIST)
        self._delay(budget)                  # right-skewed pause before the react
        self._react(channel, msg.id, emoji)

    def _read_history(self, channel: str) -> list[TgMessage]:
        """Read recent messages via the read path (no write). Tolerant — a missing
        read client or a read error degrades to an empty history (still dwells)."""
        if self.read_client is None:
            return []
        try:
            return list(self.read_client.iter_channel_messages(
                channel, DWELL_HISTORY_LIMIT))
        except Exception as e:  # noqa: BLE001 - read failures must not crash warming
            log.debug("Telegram dwell history read failed · %s · %s", channel, e)
            return []

    def _react(self, channel: str, message_id: int, emoji: str) -> None:
        """Attempt a react; log it; advance the react counter on success. A soft
        flood skips this react; a hard flood raises HaltSession."""
        try:
            ok = bool(self.port.react(channel, message_id, emoji))
        except TelegramFloodError as e:
            if e.is_hard:
                raise HaltSession("telegram flood during warming react",
                                  kind=HARD_FLOOD_HALT_KIND) from e
            log.info("Telegram react soft-skip (flood wait %ss) · %s",
                     e.retry_seconds, channel)
            return
        self.store.log_action(
            self.sentinel_campaign, "react", reel_id=None, target=channel,
            succeeded=ok, session_id=self.session_id,
            account_id=self.account_id, now=self._clock())
        if ok:
            self._reacts_today += 1


# --- TG flood flag (brief DECISION): a hard flood raises a halt-severity
# `peer_flood` flag keyed on the account; it is a WARMING_CHALLENGE_KINDS member so
# a fresh block craters warmth. Distinct from the IG `account_challenge` flag. ---
_FLOOD_FLAG_KIND = "peer_flood"


def run_telegram_warming_session(*, campaign: Campaign, store: Store,
                                 feed: Any, pacer: Pacer,
                                 run_id: Optional[str] = None,
                                 port: Optional[TelegramWarmingPort] = None,
                                 relevance_gate: Optional[RelevanceGate] = None,
                                 session_id: Optional[str] = None,
                                 now: Optional[float] = None) -> dict:
    """Run ONE Telegram warming session (PRD §7.3–§7.6).

    Mirrors ``WarmingSession``'s lifecycle (start_session with the sentinel
    campaign + engine_mode='warming' + account_id, the forced daytime guard, the
    PROVISIONED→WARMING transition, persist-activity, end_session) but drives the
    distinct TG ``TelegramWarmingExecutor`` instead of the IG feed-walk.

    ``port`` / ``relevance_gate`` are injectable so tests drive a
    ``FakeTelegramWarmingPort`` + a fake gate; live runs build the Telethon write
    client + the thin LLM gate (which degrades to seeded-only without an API key).
    The read ``feed`` (a ``TelegramFeed``) doubles as the dwell-history read client.
    """
    sid = session_id or uuid.uuid4().hex[:12]
    org_id = store.org_for_campaign(campaign.campaign_id)
    account = store.resolve_account_for_campaign(
        campaign.campaign_id, campaign.platform)
    if account is None or org_id is None:
        log.error("Telegram warming: no warmable account for campaign %s",
                  campaign.campaign_id)
        raise HaltSession("no warmable account")

    sentinel = accounts_lib.warming_sentinel_campaign(org_id)
    acct_id = int(account["id"])
    store.start_session(sid, sentinel, campaign.platform, run_id=run_id,
                        org_id=org_id, engine_mode="warming", account_id=acct_id)
    # From here an unexpected exception (browser/network crash) would escape before
    # any end_session call, leaving the warming row stuck at 'running'. The guard
    # closes it as 'halted'; the completion / HaltSession end_session calls below
    # stay authoritative.
    with session_crash_guard(store, sid):
        return _run_tg_warming_after_start(
            campaign=campaign, store=store, feed=feed, pacer=pacer,
            org_id=org_id, account=account, sentinel=sentinel, acct_id=acct_id,
            sid=sid, port=port, relevance_gate=relevance_gate, now=now)


def _run_tg_warming_after_start(*, campaign: Campaign, store: Store, feed: Any,
                                pacer: Pacer, org_id: int, account: dict,
                                sentinel: str, acct_id: int, sid: str,
                                port: Optional[TelegramWarmingPort],
                                relevance_gate: Optional[RelevanceGate],
                                now: Optional[float]) -> dict:
    if account.get("state") == accounts_lib.PROVISIONED:
        store.update_account_lifecycle(
            acct_id, accounts_lib.WARMING, reason="warming_started", session_id=sid)

    if not pacer.is_daytime():
        store.end_session(sid, "halted", "outside daytime window")
        raise HaltSession("outside daytime window")

    clock = (lambda: __import__("time").time()) if now is None else (lambda: now)
    ramp_day = 0
    if account.get("added_at"):
        ramp_day = int((clock() - float(account["added_at"])) // 86400)
    budget = budget_for_day(ramp_day, campaign.platform)

    # Kill-switch baseline + mid-session re-check (mirrors WarmingSession): stop
    # issuing writes the moment daytime closes or a mid-run kill-switch flips.
    kill_clear_at_start = warming_control.warming_kill_reason(
        store, org_id=org_id, platform=campaign.platform) is None

    def daytime_open() -> bool:
        if not pacer.is_daytime():
            return False
        if kill_clear_at_start and warming_control.warming_kill_reason(
                store, org_id=org_id, platform=campaign.platform):
            return False
        return True

    # Build only the live pieces the caller did NOT inject. A supplied port is
    # never replaced (and we never construct a live Telethon client just to fetch a
    # gate), so a test injecting port=fake + relevance_gate=None degrades cleanly to
    # seeded-only via build_relevance_gate()→None without touching the network.
    if port is None:
        port = _build_live_tg_port(store, org_id, acct_id)
    if relevance_gate is None:
        relevance_gate = _build_live_relevance_gate()

    executor = TelegramWarmingExecutor(
        port=port, store=store, sentinel_campaign=sentinel, account_id=acct_id,
        session_id=sid, pacer=pacer, campaign=campaign,
        relevance_gate=relevance_gate, read_client=feed,
        rng=pacer.rng, now=now)

    halt_reason: Optional[str] = None
    try:
        executor.run(budget, daytime_open=daytime_open)
        _persist_tg_activity(store, account, budget, clock())
        store.end_session(sid, "completed")
    except HaltSession as h:
        halt_reason = h.reason
        store.raise_flag(_FLOOD_FLAG_KIND, "halt", h.reason,
                         org_id=org_id, account_id=acct_id, session_id=sid)
        acct = store.get_account(acct_id)
        if acct is not None and accounts_lib.can_transition(
                acct["state"], accounts_lib.FLAGGED):
            store.update_account_lifecycle(
                acct_id, accounts_lib.FLAGGED, reason=_FLOOD_FLAG_KIND,
                session_id=sid)
        store.end_session(sid, "halted", halt_reason)

    return _empty_summary(sid, halt_reason)


def _build_live_tg_port(store: Store, org_id: int, acct_id: int):
    """Build the live ``TelethonWarmingPort`` from the per-org stored Telegram
    secret. Loud failures bubble from the adapter — the caller folds any
    HaltSession into the summary. Built ONLY when the caller injects no port."""
    from ..telegram.warming_writes import TelethonWarmingClient

    credentials = store.get_account_secret(org_id, "telegram", acct_id)
    return TelethonWarmingClient.from_credentials(credentials)


def _build_live_relevance_gate():
    """Build the thin LLM relevance gate. DEGRADES to ``None`` (seeded-only) when
    there is no ``OPENROUTER_API_KEY`` — the single ML touch on any warming path,
    fail-closed and degradeable. Built ONLY when the caller injects no gate."""
    from .tg_relevance import build_relevance_gate

    return build_relevance_gate()


def _persist_tg_activity(store: Store, account: dict, budget: ActionBudget,
                         now: float) -> None:
    """Persist TG warming counters into accounts.detail (P0 §4.7 idiom): today's
    join/react counts + ramp_day + last_warmed_at. Mirrors WarmingSession's
    ``_persist_activity`` but for the TG action vocabulary."""
    acct_id = int(account["id"])
    acct = store.get_account(acct_id)
    if acct is None:
        return
    detail = dict(acct.get("detail") or {}) if isinstance(
        acct.get("detail"), dict) else {}
    day = store.action_counts_for_account_day(acct_id, now=now) or {}
    detail.update({
        "last_activity_kind": "observe" if budget.read_only else "engage",
        "warming_joins": int(day.get("join", 0)),
        "warming_reacts": int(day.get("react", 0)),
    })
    ramp_day = 0
    if acct.get("added_at"):
        ramp_day = int((now - float(acct["added_at"])) // 86400)
    store.update_account_ramp(acct_id, ramp_day=ramp_day,
                              warmth_floor=budget_floor(budget),
                              last_warmed_at=now)
    store.update_account_lifecycle(
        acct_id, acct["state"], reason="warming_observed", detail=detail)
