"""Account lifecycle — the pure state machine + identity constants for warming.

The durable warming entity is the ACCOUNT (warming PRD §1, §3). A campaign
*consumes* a warmed account from an org-level pool; its surfaced warmth is
derived from that backing account. This module holds the parts that have no DB
or Chrome dependency so they can be unit-tested in isolation and mirrored
TS-side later:

  - the lifecycle state set + the pure ``can_transition`` guard (§3.1),
  - the eligibility sets harvest/warming gate on,
  - ``WARMABLE_PLATFORMS`` (which platforms ever get an ``accounts`` row),
  - the reserved per-org sentinel campaign id that lets a pool warming session
    satisfy the existing ``sessions.campaign_id`` / ``actions.campaign_id``
    NOT NULL constraints with zero schema reshape (PRD C2/D1, §3.2).

The Store owns persistence; the warming engine owns Chrome. Both import from
here so the lifecycle vocabulary lives in exactly one place.
"""
from __future__ import annotations

# ----- lifecycle states (persisted on accounts.state) -----
PROVISIONED = "provisioned"   # operator logged in once; no warming yet
WARMING = "warming"           # ramp running; not harvest-eligible
READY = "ready"               # warmth gate satisfied; harvest-eligible; upkeep continues
ACTIVE = "active"             # currently backing live harvest campaign(s)
COOLING = "cooling"           # backed off (rate concern / post-incident), warming-only
FLAGGED = "flagged"           # checkpoint/challenge/login-drift; hard-blocked until cleared

ACCOUNT_STATES = frozenset(
    {PROVISIONED, WARMING, READY, ACTIVE, COOLING, FLAGGED})

# Allowed transitions (PRD §3.1). flagged → warming is operator-cleared only.
_TRANSITIONS: dict[str, frozenset[str]] = {
    PROVISIONED: frozenset({WARMING}),
    WARMING: frozenset({READY, FLAGGED}),
    READY: frozenset({ACTIVE, COOLING, WARMING, FLAGGED}),
    ACTIVE: frozenset({READY, COOLING, FLAGGED}),
    COOLING: frozenset({WARMING, READY, FLAGGED}),
    FLAGGED: frozenset({WARMING}),
}

# Coarse persisted gates (the warmth SCORE is the fine-grained derived number, §5).
HARVEST_ELIGIBLE = frozenset({READY, ACTIVE})
WARMING_ELIGIBLE = frozenset(ACCOUNT_STATES - {FLAGGED})

# Platforms that ever get an `accounts` row and a warmth score (PRD §2 locked #2,
# §11). X + LinkedIn first; Instagram generalizes; Telegram joins in P1 (its
# warming paradigm is search→join→dwell→react, PRD §7). YouTube/Reddit are
# API-quota platforms with no account to warm — their campaigns hit the neutral
# warmth default and are never gated.
WARMABLE_PLATFORMS = frozenset({"x", "linkedin", "instagram", "telegram"})


class InvalidAccountTransition(ValueError):
    """Raised when an account lifecycle move is not permitted by the state machine."""

    def __init__(self, from_state: str, to_state: str):
        super().__init__(f"invalid account transition: {from_state!r} -> {to_state!r}")
        self.from_state = from_state
        self.to_state = to_state


def can_transition(from_state: str, to_state: str) -> bool:
    """True iff ``from_state -> to_state`` is an allowed lifecycle move (§3.1).

    A no-op transition (``from == to``) is allowed — re-stamping the current
    state (e.g. an idempotent reconcile tick) is never an error. An unknown
    target state is always rejected.
    """
    if to_state not in ACCOUNT_STATES:
        return False
    if from_state == to_state:
        return True
    return to_state in _TRANSITIONS.get(from_state, frozenset())


def is_warmable(platform: str) -> bool:
    """True iff ``platform`` gets an ``accounts`` row + a warmth score (§11)."""
    return platform in WARMABLE_PLATFORMS


def warming_sentinel_campaign(org_id: int) -> str:
    """The reserved per-org sentinel campaign id for pool warming sessions/actions.

    ``sessions.campaign_id`` and ``actions.campaign_id`` are both NOT NULL, but a
    pool warming session backs an *account*, not a campaign. This TEXT sentinel
    satisfies the constraint with zero schema reshape (PRD C2/D1, §3.2). It is
    NEVER inserted into ``campaign_meta`` and NEVER returned by campaign/harvest
    queries; all warmth signals join on ``account_id``, never on this id.
    """
    return f"__warming__:{org_id}"


# Prefix every sentinel campaign id shares — used by harvest/campaign queries to
# exclude pool warming rows (``campaign_id NOT LIKE '__warming__:%'``).
WARMING_SENTINEL_PREFIX = "__warming__:"


def is_warming_sentinel(campaign_id: str | None) -> bool:
    """True iff ``campaign_id`` is a pool warming sentinel (not a real campaign)."""
    return bool(campaign_id) and campaign_id.startswith(WARMING_SENTINEL_PREFIX)
