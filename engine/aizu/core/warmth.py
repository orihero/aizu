"""Warmth Score — the read-model (warming PRD §5).

A 0–100 composite **computed on every read, never a column** (C3). The account
row persists only lifecycle `state` + ramp state; the score is derived fresh from
insert-only signals (sessions / actions / health_flags / age) joined on
`account_id`. This module is pure: it takes already-queried raw signals and
returns a `WarmthScore` with a per-component breakdown for auditability. The
Store (`warmth_for_campaign` / `account_warmth`) does the querying; `panel_org`
is the single producer so every endpoint returns byte-identical scores (§5.6).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# ----- tunables (PRD O6 — provisional; tune in P1) -----
FULL_AGE_DAYS = 21          # age component saturates here ("time under management")
TARGET_CONNECTS = 20        # network component saturates here (IG/default divisor)
RAMP_WINDOW_DAYS = 14       # ramp consistency window
TRUST_WINDOW_DAYS = 14      # friction-absence window

# Per-platform network divisor (PRD §9.2, O-tg-network-divisor). Telegram joins
# are far more rate-limited than IG follows, so its `network` component saturates
# at fewer successes. Anything not listed defaults to TARGET_CONNECTS.
TARGET_NETWORK: dict[str, int] = {"telegram": 8}


def network_divisor(platform: str) -> int:
    """The `network` saturation divisor for `platform` (TG=8, else TARGET_CONNECTS)."""
    return TARGET_NETWORK.get(platform, TARGET_CONNECTS)

GATE_MIN_DEFAULT = 40       # "good to continue" (O1/O2 — parameterized here)
GATE_FULL_DEFAULT = 70      # full volume

# Per-platform component weights — CONFIG, each row sums to 100 (§5.5). `_default`
# is Instagram only. The `telegram` row maps the five components onto TG's
# search→join→dwell→react paradigm (PRD §9.3): network = channels-joined,
# profile = TG profile completeness, ramp = warming-session cadence,
# trust = flood-absence.
PLATFORM_WEIGHTS: dict[str, dict[str, float]] = {
    "x":        {"age": 15, "ramp": 30, "network": 20, "profile": 10, "trust": 25},
    "linkedin": {"age": 20, "ramp": 20, "network": 30, "profile": 20, "trust": 10},
    "telegram": {"age": 25, "ramp": 20, "network": 30, "profile": 10, "trust": 15},
    "_default": {"age": 20, "ramp": 25, "network": 20, "profile": 15, "trust": 20},
}
_COMPONENTS = ("age", "ramp", "network", "profile", "trust")

# The single shared challenge-kind anchor imported by BOTH the warming engine's
# raise_flag calls and the penalty here (§5.3). v1 the warming engine raises
# checkpoint / empty_interception / account_challenge; the rate/login kinds are
# scored the same way once raised.
WARMING_CHALLENGE_KINDS = frozenset({
    "checkpoint", "empty_interception", "account_challenge", "arkose",
    "rate_limit", "login_drift", "session_expired", "peer_flood"})

# `peer_flood` is a TG write-era flood block (PRD §8). Treated as a challenge-group
# kind (not a transient rate kind) so a fresh flood craters warmth like a checkpoint.
_CHALLENGE_KINDS = frozenset({"checkpoint", "empty_interception",
                              "account_challenge", "arkose", "peer_flood"})
_RATE_KINDS = frozenset({"rate_limit"})
_LOGIN_KINDS = frozenset({"login_drift", "session_expired"})

# Open-flag trust load weights (§5.2: soft −X, halt −Y).
_FLAG_LOAD = {"soft": 0.25, "halt": 0.5}

# ----- penalty windows + factors (§5.3, multiplicative) -----
_HOUR = 3600.0
CHALLENGE_FRESH_WINDOW = 24 * _HOUR
CHALLENGE_RECENT_WINDOW = 72 * _HOUR
RATE_LIMIT_WINDOW = 6 * _HOUR
PENALTY_CHALLENGE_FRESH = 0.10
PENALTY_CHALLENGE_RECENT = 0.35
PENALTY_RATE_LIMITED = 0.50
PENALTY_LOGIN_DRIFT = 0.40

# Login-health truthy markers for the `profile` component.
_LOGIN_OK = (True, "ok", "active", "logged_in", "connected")

_DAY = 86400.0


@dataclass(frozen=True)
class WarmthScore:
    """A computed warmth verdict + its breakdown (server-authoritative gate, §7.2)."""
    score: int                       # 0..100 (base × penalty)
    state: str                       # warming | ready | full | throttled
    base: float                      # pre-penalty 0..100
    penalty_factor: float
    components: dict[str, float]     # normalized 0..1 per component (or {} for neutral)
    meets_gate: bool
    gate_min: int = GATE_MIN_DEFAULT
    gate_full: int = GATE_FULL_DEFAULT
    trend: list[float] = field(default_factory=list)   # base-only; [] in v1
    eta_hours: Optional[float] = None                  # no producer in v1 (§5.7)

    def as_payload(self, checked_at: str) -> dict[str, Any]:
        """The §7.2 campaign-card payload (camelCase; travels WITH the campaign so
        the client never needs CONFIG to render the gate)."""
        comps = self.components or {}
        return {
            "score": self.score,
            "state": self.state,
            "gateMin": self.gate_min,
            "gateFull": self.gate_full,
            "meetsGate": self.meets_gate,
            "components": {k: round(comps.get(k, 0.0), 4) for k in _COMPONENTS},
            "trend": list(self.trend),
            "etaHours": self.eta_hours,
            "checkedAt": checked_at,
        }


def weights_for(platform: str) -> dict[str, float]:
    """The component weight row for `platform` (X/LinkedIn/Telegram explicit, else
    Instagram `_default`). YouTube/Reddit never reach here — they are not warmable."""
    return PLATFORM_WEIGHTS.get(platform, PLATFORM_WEIGHTS["_default"])


def _label(score: int, *, throttled: bool, gate_min: int, gate_full: int) -> str:
    """Panel-facing state (§7.2 enum: warming|ready|full|throttled). `throttled` is
    distinct so the operator knows a low score is transient rate-limiting (§5.4)."""
    if throttled:
        return "throttled"
    if score < gate_min:
        return "warming"
    if score < gate_full:
        return "ready"
    return "full"


def _profile_component(detail: Optional[dict[str, Any]]) -> float:
    """Login health from `accounts.detail` (§5.2). `profile_complete` is dropped
    (pure-observe warming never visits the profile page) — only login/checkpoint
    signals. Unknown (no detail yet) is neutral 0.5, not 0, so a freshly
    provisioned account isn't punished for a signal we haven't captured."""
    if not detail:
        return 0.5
    parts: list[float] = []
    if "login_status" in detail:
        parts.append(1.0 if detail.get("login_status") in _LOGIN_OK else 0.0)
    checkpoint = detail.get("checkpoint_detected") or detail.get("checkpoint")
    parts.append(0.0 if checkpoint else 1.0)
    return sum(parts) / len(parts) if parts else 0.5


def _trust_component(open_challenge_flags: list[dict[str, Any]]) -> float:
    """1 − min(1, weighted open-flag load) over unresolved warming-challenge flags
    in the trust window (§5.2). Caller filters by kind + resolved_at + window."""
    load = sum(_FLAG_LOAD.get(f.get("severity", "soft"), _FLAG_LOAD["soft"])
               for f in open_challenge_flags)
    return 1.0 - min(1.0, load)


def compute_penalty(open_challenge_flags: list[dict[str, Any]], now: float
                    ) -> tuple[float, Optional[str], bool]:
    """(penaltyFactor, reason, throttled) from the NEWEST unresolved challenge flag
    (§5.3). A fresh challenge craters the score; stale (>72h) decays to 1.0. The
    caller passes only unresolved flags whose kind ∈ WARMING_CHALLENGE_KINDS."""
    relevant = [f for f in open_challenge_flags
                if f.get("kind") in WARMING_CHALLENGE_KINDS]
    if not relevant:
        return 1.0, None, False
    newest = max(relevant, key=lambda f: f.get("created_at") or 0.0)
    age = now - (newest.get("created_at") or 0.0)
    kind = newest.get("kind")
    if kind in _RATE_KINDS:
        if age < RATE_LIMIT_WINDOW:
            return PENALTY_RATE_LIMITED, "rate_limited", True
        return 1.0, None, False
    if kind in _LOGIN_KINDS:
        return PENALTY_LOGIN_DRIFT, "login_drift", False
    if age < CHALLENGE_FRESH_WINDOW:
        return PENALTY_CHALLENGE_FRESH, "challenge_fresh", False
    if age < CHALLENGE_RECENT_WINDOW:
        return PENALTY_CHALLENGE_RECENT, "challenge_recent", False
    return 1.0, None, False


@dataclass
class WarmthInputs:
    """Raw, already-queried signals for one account (the Store fills these)."""
    platform: str
    age_days: float
    ramp_completed_days: int           # distinct days with a completed warming session (14d)
    network_successes: int             # cumulative successful follow/connect
    detail: Optional[dict[str, Any]]   # accounts.detail (login health)
    open_challenge_flags: list[dict[str, Any]]   # unresolved, kind∈WARMING_CHALLENGE_KINDS, in window


def neutral_default() -> WarmthScore:
    """No account / no warming signals → neutral, never blocking (§5.8). Hit by
    non-warmable platforms (YouTube/Reddit) and un-onboarded campaigns (Telegram is
    now warmable with a real weight row + scoring path, so it no longer lands here
    once onboarded)."""
    return WarmthScore(score=50, state="warming", base=50.0, penalty_factor=1.0,
                       components={}, meets_gate=True,
                       gate_min=GATE_MIN_DEFAULT, gate_full=GATE_FULL_DEFAULT)


def compute(inputs: WarmthInputs, *, now: float,
            gate_min: int = GATE_MIN_DEFAULT,
            gate_full: int = GATE_FULL_DEFAULT) -> WarmthScore:
    """Derive a WarmthScore from raw signals (§5.1–5.4). Pure + deterministic."""
    _net_div = network_divisor(inputs.platform)
    components = {
        "age": min(1.0, inputs.age_days / FULL_AGE_DAYS) if FULL_AGE_DAYS else 0.0,
        "ramp": min(1.0, inputs.ramp_completed_days / RAMP_WINDOW_DAYS),
        "network": min(1.0, inputs.network_successes / _net_div)
                   if _net_div else 0.0,
        "profile": _profile_component(inputs.detail),
        "trust": _trust_component(inputs.open_challenge_flags),
    }
    weights = weights_for(inputs.platform)
    base = sum(components[k] * weights.get(k, 0.0) for k in _COMPONENTS)
    penalty_factor, _reason, throttled = compute_penalty(
        inputs.open_challenge_flags, now)
    score = int(round(base * penalty_factor))
    score = max(0, min(100, score))
    return WarmthScore(
        score=score,
        state=_label(score, throttled=throttled,
                     gate_min=gate_min, gate_full=gate_full),
        base=round(base, 2),
        penalty_factor=penalty_factor,
        components=components,
        meets_gate=score >= gate_min,
        gate_min=gate_min, gate_full=gate_full)
