"""Threshold and escalate-band range validation (Campaign Lab, Sheet #3 / Remedy E).

Both knobs were shape-checked but never range-checked, and both fail SILENTLY when
out of range — no exception, no flag, a campaign that looks like it is working.
"""
import pytest

from aizu.campaign_gen import ProductContext, assemble_draft
from aizu.core.config import _validated_band, campaign_from_brief
from aizu.server import _validate_campaign


def _payload(threshold):
    return {"campaignId": "c1", "brief": {"threshold": threshold}}


# ---------------- threshold ----------------

@pytest.mark.parametrize("bad", [0, 0.0, 1, 1.0, -0.5, 2, 1.5])
def test_the_bridge_rejects_a_threshold_outside_the_open_interval(bad):
    """The gate is `score >= campaign.threshold`: 0 accepts every comment ever
    scored, 1 accepts none. Both used to pass validation."""
    parsed, err = _validate_campaign(_payload(bad))
    assert parsed is None
    assert err and "between 0 and 1" in err


@pytest.mark.parametrize("ok", [0.01, 0.5, 0.7, 0.99])
def test_a_sane_threshold_passes(ok):
    parsed, err = _validate_campaign(_payload(ok))
    assert err is None and parsed is not None


def test_a_non_finite_threshold_is_still_rejected_first():
    parsed, err = _validate_campaign(_payload(float("nan")))
    assert parsed is None and err


def test_a_blank_threshold_still_means_keep_the_stored_one():
    parsed, err = _validate_campaign({"campaignId": "c1", "brief": {}})
    assert err is None and parsed is not None


def test_a_numeric_string_keeps_its_historic_tolerance():
    """Only the explicit numeric form is range-checked — the string path has
    always been lenient and downstream coercion owns it."""
    parsed, err = _validate_campaign(_payload("0.7"))
    assert err is None and parsed is not None


def test_a_generated_draft_lands_inside_what_the_bridge_accepts():
    """A model answering 0 or 1 used to yield a draft the save endpoint then
    rejected, which reads to an operator as 'generation is broken'."""
    for raw_threshold in (0, 1, 5, -3):
        draft = assemble_draft({"threshold": raw_threshold}, ProductContext())
        parsed, err = _validate_campaign(_payload(draft["threshold"]))
        assert err is None, f"generated {draft['threshold']} but bridge said: {err}"


# ---------------- escalate band ----------------

def test_a_valid_band_passes_through():
    assert _validated_band([0.4, 0.75]) == (0.4, 0.75)


def test_an_inverted_band_is_rejected():
    """The likelier typo, and the one that silently DISABLES escalation entirely:
    `_unsure()` can never be true when lo > hi."""
    with pytest.raises(ValueError, match="0 <= lo <= hi <= 1"):
        _validated_band([0.9, 0.2])


@pytest.mark.parametrize("bad", [[-0.1, 0.5], [0.5, 1.5], [-1, 5]])
def test_a_band_outside_zero_to_one_is_rejected(bad):
    with pytest.raises(ValueError, match="0 <= lo <= hi <= 1"):
        _validated_band(bad)


@pytest.mark.parametrize("bad", ["x", [1], [], [1, 2, 3], None])
def test_a_malformed_band_is_still_rejected_on_shape(bad):
    with pytest.raises(ValueError, match="2-element list"):
        _validated_band(bad)


def test_non_numeric_band_values_say_so():
    with pytest.raises(ValueError, match="must be numbers"):
        _validated_band([None, 2])


def test_a_non_finite_band_is_rejected():
    with pytest.raises(ValueError, match="finite"):
        _validated_band([0.1, float("inf")])


def test_a_degenerate_but_legal_band_is_allowed():
    # lo == hi is a zero-width band: escalation effectively off, but deliberate.
    assert _validated_band([0.5, 0.5]) == (0.5, 0.5)


def test_the_band_reaches_the_campaign_from_a_brief():
    c = campaign_from_brief("c1", {
        "platform": "instagram", "escalate_band": [0.35, 0.8],
        "relevance_def": "x", "match_def": "y", "extract_def": "- z"})
    assert c.escalate_band == (0.35, 0.8)


def test_a_bad_band_in_a_brief_fails_loudly_rather_than_at_run_time():
    with pytest.raises(ValueError, match="0 <= lo <= hi <= 1"):
        campaign_from_brief("c1", {
            "platform": "instagram", "escalate_band": [0.9, 0.1],
            "relevance_def": "x", "match_def": "y", "extract_def": "- z"})
