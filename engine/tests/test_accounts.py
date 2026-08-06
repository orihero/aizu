"""Unit tests for the pure account lifecycle state machine (warming PRD §3.1)."""
import pytest

from aizu.core.accounts import (
    ACCOUNT_STATES,
    ACTIVE,
    COOLING,
    FLAGGED,
    HARVEST_ELIGIBLE,
    PROVISIONED,
    READY,
    WARMABLE_PLATFORMS,
    WARMING,
    WARMING_ELIGIBLE,
    can_transition,
    is_warmable,
    is_warming_sentinel,
    warming_sentinel_campaign,
)


# ---- can_transition ----

@pytest.mark.parametrize("frm,to", [
    (PROVISIONED, WARMING),
    (WARMING, READY),
    (WARMING, FLAGGED),
    (READY, ACTIVE),
    (READY, COOLING),
    (READY, WARMING),
    (READY, FLAGGED),
    (ACTIVE, READY),
    (ACTIVE, COOLING),
    (ACTIVE, FLAGGED),
    (COOLING, WARMING),
    (COOLING, READY),
    (COOLING, FLAGGED),
    (FLAGGED, WARMING),
])
def test_allowed_transitions(frm, to):
    assert can_transition(frm, to) is True


@pytest.mark.parametrize("frm,to", [
    (PROVISIONED, READY),       # must warm first
    (PROVISIONED, ACTIVE),
    (WARMING, ACTIVE),          # ready gates active
    (FLAGGED, READY),           # operator clears to warming only
    (FLAGGED, ACTIVE),
    (ACTIVE, WARMING),          # cool/ready before re-warming
    (READY, PROVISIONED),       # never go backwards to provisioned
])
def test_disallowed_transitions(frm, to):
    assert can_transition(frm, to) is False


def test_noop_transition_allowed():
    # Re-stamping the current state (idempotent reconcile) is never an error.
    for state in ACCOUNT_STATES:
        assert can_transition(state, state) is True


def test_unknown_target_rejected():
    assert can_transition(READY, "bogus") is False
    assert can_transition("bogus", "bogus") is False


# ---- eligibility sets ----

def test_harvest_eligible_is_ready_and_active_only():
    assert HARVEST_ELIGIBLE == {READY, ACTIVE}


def test_warming_eligible_excludes_only_flagged():
    assert FLAGGED not in WARMING_ELIGIBLE
    assert WARMING_ELIGIBLE == ACCOUNT_STATES - {FLAGGED}


# ---- warmable platforms ----

def test_warmable_platforms_v1():
    assert WARMABLE_PLATFORMS == {"x", "linkedin", "instagram", "telegram"}
    assert is_warmable("x")
    assert is_warmable("instagram")
    assert is_warmable("telegram")
    assert not is_warmable("youtube")
    assert not is_warmable("reddit")


# ---- sentinel campaign id ----

def test_sentinel_is_per_org_and_recognized():
    s = warming_sentinel_campaign(7)
    assert s == "__warming__:7"
    assert is_warming_sentinel(s)
    assert is_warming_sentinel("__warming__:99")
    assert not is_warming_sentinel("real-campaign")
    assert not is_warming_sentinel(None)
    assert not is_warming_sentinel("")
