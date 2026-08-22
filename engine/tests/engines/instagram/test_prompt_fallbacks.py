"""Instagram prompt fallbacks (Campaign Lab, Remedy Sheet #3 / Remedy C).

Two defects here, and the second is the one the sheet got wrong.

1. Instagram was the ONE engine with no fallback: every cascade call site passed
   `campaign.<x>_prompt` straight through, so a panel-authored campaign with blank
   prompts ran on core.prompts.SYSTEM_GENERIC (~50 words) while this engine's own
   prompts module was never imported at all.

2. The obvious fix — "wire engines/instagram/prompts.py in like the other engines"
   — would have imposed the shipped ACME SAAS rubric on every Instagram campaign,
   including the Tashkent renovation brief. Those constants are an eval baseline
   locked verbatim to config/campaign.md, not defaults. Separate vertical-neutral
   fallbacks were written instead.
"""
import pytest

from aizu.core.config import campaign_from_brief
from aizu.core.prompts import SYSTEM_GENERIC
from aizu.engines.instagram.cascade import Cascade
from aizu.engines.instagram.prompts import (IG_RELEVANCE, IG_VISION,
                                            SYSTEM_MATCH, SYSTEM_RELEVANCE,
                                            SYSTEM_VISION, ig_match)


def _campaign(**over):
    brief = {"platform": "instagram", "threshold": 0.7,
             "relevance_def": "renovation in Tashkent",
             "match_def": "wants to hire a renovator", "extract_def": "- phone"}
    brief.update(over)
    return campaign_from_brief("c1", brief)


def _cascade(**over):
    return Cascade(router=object(), campaign=_campaign(**over))


# ---------------- the fallback actually engages ----------------

def test_a_blank_campaign_gets_this_engines_prompts_not_the_generic_one():
    c = _cascade()
    assert c._relevance_system == IG_RELEVANCE
    assert c._vision_system == IG_VISION
    assert c._relevance_system != SYSTEM_GENERIC
    assert c._match_system.startswith("You are a precise PURCHASE/ACTION-INTENT")


def test_a_campaign_with_its_own_prompts_still_wins():
    c = _cascade(relevance_prompt="MINE-R", match_prompt="MINE-M",
                 vision_prompt="MINE-V")
    assert (c._relevance_system, c._match_system, c._vision_system) \
        == ("MINE-R", "MINE-M", "MINE-V")


def test_no_cascade_call_site_bypasses_the_fallback():
    """Nine call sites across four methods is why these are properties. A new one
    that reaches for `campaign.<x>_prompt` directly reintroduces the bug."""
    import ast
    import pathlib
    src = pathlib.Path(
        f"{pathlib.Path(__file__).parents[3]}/aizu/engines/instagram/cascade.py"
    ).read_text()
    tree = ast.parse(src)
    direct = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "system":
                continue
            # `system=self.campaign.<anything>` is the bug; `self._x_system` is fine.
            if (isinstance(kw.value, ast.Attribute)
                    and isinstance(kw.value.value, ast.Attribute)
                    and kw.value.value.attr == "campaign"):
                direct.append(kw.value.attr)
    assert direct == [], f"cascade passes campaign prompts directly: {direct}"


# ---------------- the fallbacks are vertical-neutral ----------------

@pytest.mark.parametrize("term", ["SaaS", "software", "subscribe", "free trial",
                                  "integration", "onboarding"])
def test_the_fallbacks_carry_no_vertical_assumptions(term):
    """The brief supplies the domain — same contract engines/x/prompts.py states."""
    blob = (IG_RELEVANCE + IG_VISION + ig_match(0.7)).lower()
    assert term.lower() not in blob


def test_the_shipped_saas_baseline_is_untouched_and_is_not_a_default():
    """Those three constants are the eval baseline and are locked verbatim to
    config/campaign.md by tests/test_config.py — they must never become defaults."""
    assert "SaaS" in SYSTEM_MATCH and "SOFTWARE / SaaS" in SYSTEM_RELEVANCE
    assert "software / SaaS product" in SYSTEM_VISION
    c = _cascade()
    assert c._relevance_system is not SYSTEM_RELEVANCE
    assert c._match_system != SYSTEM_MATCH
    assert c._vision_system is not SYSTEM_VISION


# ---------------- the threshold is templated, not hard-coded ----------------

@pytest.mark.parametrize("threshold", [0.3, 0.55, 0.7, 0.85])
def test_the_rubric_quotes_the_campaigns_own_threshold(threshold):
    """The gate is `score >= campaign.threshold`; the shipped prompt hard-codes
    'the 0.70 threshold separates genuine inquiry from banter' in prose. Moving
    the knob silently desynchronized the rubric from the gate."""
    text = _cascade(threshold=threshold)._match_system
    assert f"{threshold:.2f}" in text
    assert 'iff score>=%.2f' % threshold in text


def test_no_stale_070_survives_at_another_threshold():
    text = ig_match(0.55)
    assert "0.70" not in text and "0.55" in text


def _bands(text):
    """The `lo-hi  LABEL` rubric rows, as (lo, hi) pairs."""
    import re
    return [(float(a), float(b)) for a, b in
            re.findall(r"^\s+(\d\.\d\d)-(\d\.\d\d)\s+[A-Z]", text, re.M)]


@pytest.mark.parametrize("t", [0.05, 0.2, 0.3, 0.5, 0.7, 0.85, 0.95])
def test_the_bands_are_ordered_and_contiguous_wherever_the_threshold_sits(t):
    """Fixed offsets do not survive the extremes: at t=0.05 a `t - 0.20` edge
    collapses to three zero-width bands, and a hard-coded top band of `0.90-1.00`
    INVERTS at t=0.95 (`0.95-0.90`). Both printed happily before."""
    bands = _bands(ig_match(t))
    assert len(bands) == 5
    for lo, hi in bands:
        assert lo < hi, f"degenerate or inverted band {lo}-{hi} at threshold {t}"
    for (_, prev_hi), (next_lo, _) in zip(bands, bands[1:]):
        assert prev_hi == next_lo, "bands must be contiguous, leaving no gap"
    assert bands[0][0] == 0.0 and bands[-1][1] == 1.0
    # The threshold itself must be exactly the yes/no boundary.
    assert bands[2][1] == round(t, 2) == bands[3][0]


@pytest.mark.parametrize("t", [0.0, 1.0])
def test_an_impossible_threshold_degrades_to_qualitative_guidance(t):
    """0 and 1 leave no room for five bands. The bridge rejects both now, but the
    prompt must still never print a contradictory ladder."""
    text = ig_match(t)
    assert _bands(text) == []
    assert "Score BELOW the threshold" in text


def test_a_threshold_outside_0_1_is_clamped_rather_than_producing_nonsense():
    assert 'score>=1.00' in ig_match(5.0)
    assert 'score>=0.00' in ig_match(-3.0)
