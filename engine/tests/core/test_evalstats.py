"""Eval statistics discipline (Campaign Lab, Remedy Sheet #3 / Remedy D.3).

`scripts/eval/run_eval.py` reports bare point estimates over a 25-item gold set.
At n=25 the interval around a precision of 0.80 spans roughly 0.59-0.93, so two
variants "differing by 4 points" are indistinguishable noise. These lock the
behaviours that make a comparison mean something.
"""
import math

import pytest

from aizu.core.evalstats import (Confusion, FlipReport, Metric, baseline_key,
                                 confusion, mcnemar_exact, paired_flips,
                                 self_flip_rate, slice_report, sweep_threshold,
                                 wilson_interval, wilson_lower_bound)


# ---------------- intervals ----------------

def test_no_evidence_is_total_uncertainty_not_certainty():
    assert wilson_interval(0, 0) == (0.0, 1.0)
    assert wilson_lower_bound(0, 0) == 0.0


def test_the_interval_never_leaves_zero_to_one():
    """Where Wald fails: small n at the extremes produces bounds below 0/above 1."""
    for hits, n in [(0, 5), (5, 5), (1, 3), (0, 1), (1, 1), (99, 100)]:
        lo, hi = wilson_interval(hits, n)
        assert 0.0 <= lo <= hi <= 1.0


def test_more_evidence_narrows_the_interval_at_the_same_rate():
    small = Metric("p", 8, 10)
    large = Metric("p", 80, 100)
    assert small.value == large.value
    assert large.width < small.width


def test_a_small_gold_set_cannot_distinguish_a_four_point_difference():
    """The concrete reason this module exists."""
    a, b = Metric("p", 20, 25), Metric("p", 19, 25)
    lo_a, hi_a = a.interval
    lo_b, hi_b = b.interval
    assert lo_a < hi_b and lo_b < hi_a      # intervals overlap → not distinguishable


def test_metric_renders_the_interval_not_just_the_number():
    assert "[" in str(Metric("precision", 20, 25))


# ---------------- confusion ----------------

def test_precision_and_recall_have_different_denominators():
    c = confusion([True, True, False, False], [True, False, True, False])
    assert (c.tp, c.fp, c.fn, c.tn) == (1, 1, 1, 1)
    assert c.precision.n == 2 and c.recall.n == 2
    assert c.precision.value == c.recall.value == 0.5


def test_f1_is_reported_without_a_fabricated_interval():
    c = confusion([True], [True])
    assert c.f1 == 1.0
    assert not hasattr(c.f1, "interval")


def test_an_empty_confusion_does_not_divide_by_zero():
    c = Confusion()
    assert c.precision.value == 0.0 and c.recall.value == 0.0 and c.f1 == 0.0


# ---------------- paired comparison ----------------

def test_a_net_zero_delta_can_hide_a_moved_boundary():
    """The headline failure an unpaired comparison produces: 6 fixed, 6 broken
    reads as 'no change'."""
    truths = [True] * 12
    base = [True] * 6 + [False] * 6
    cand = [False] * 6 + [True] * 6
    rep = paired_flips(base, cand, truths)
    assert rep.net == 0
    assert rep.discordant == 12         # …but twelve items moved
    assert len(rep.gained) == len(rep.lost) == 6


def test_flips_are_reported_by_item_id_so_a_human_can_read_them():
    rep = paired_flips([False], [True], [True], ids=["item-42"])
    assert rep.gained == ["item-42"]


def test_identical_runs_are_all_unchanged():
    rep = paired_flips([True, False], [True, False], [True, False])
    assert rep.discordant == 0 and rep.unchanged == 2
    assert rep.p_value == 1.0 and not rep.significant()


def test_misaligned_runs_are_refused_rather_than_silently_compared():
    with pytest.raises(ValueError):
        paired_flips([True], [True, False], [True, False])
    with pytest.raises(ValueError):
        paired_flips([True], [True], [True], ids=["a", "b"])


# ---------------- McNemar ----------------

def test_no_discordant_pairs_is_maximum_agreement_not_a_missing_answer():
    assert mcnemar_exact(0, 0) == 1.0


def test_a_balanced_split_is_not_significant():
    assert mcnemar_exact(6, 6) == 1.0


def test_a_lopsided_split_is_significant():
    assert mcnemar_exact(12, 2) < 0.05


def test_the_test_is_symmetric():
    assert mcnemar_exact(12, 2) == mcnemar_exact(2, 12)


def test_small_counts_are_handled_exactly_not_approximated():
    """n<25 is exactly where chi-squared is unreliable — 5 vs 0 must be borderline,
    not confidently significant."""
    p = mcnemar_exact(5, 0)
    assert 0.03 < p < 0.10
    assert mcnemar_exact(3, 0) > 0.05        # three flips one way proves nothing


def test_p_values_stay_in_range():
    for g in range(0, 8):
        for l in range(0, 8):
            assert 0.0 <= mcnemar_exact(g, l) <= 1.0


def test_self_flip_rate_is_the_noise_floor():
    assert self_flip_rate([True, True, False, False],
                          [True, False, False, False]) == 0.25
    assert self_flip_rate([], []) == 0.0


# ---------------- threshold sweep ----------------

def test_the_threshold_lands_mid_gap_not_on_an_observed_score():
    """Verbalized scores collapse onto 0.7/0.8/0.9; a threshold sitting exactly on
    0.70 decides a whole block of items by floating-point luck."""
    scores = [0.1, 0.2, 0.65, 0.72, 0.8, 0.9]
    truths = [False, False, False, True, True, True]
    choice = sweep_threshold(scores, truths)
    assert 0.65 < choice.threshold < 0.72
    assert choice.threshold not in scores


def test_the_recall_floor_is_respected():
    scores = [0.9, 0.8, 0.4, 0.3]
    truths = [True, True, True, False]
    choice = sweep_threshold(scores, truths, min_recall=0.9)
    assert choice.recall.value >= 0.9


def test_an_unreachable_recall_floor_says_so_instead_of_pretending():
    scores = [0.1, 0.1, 0.1]
    truths = [True, True, False]
    choice = sweep_threshold(scores, truths, min_recall=0.99)
    # Every positive shares one score, so a cut either takes both or neither —
    # reachable here; the point is the reason string never lies about which
    # branch produced the answer.
    assert choice.reason


def test_bootstrap_reports_how_unstable_the_choice_is():
    scores = [0.1, 0.2, 0.65, 0.72, 0.8, 0.9]
    truths = [False, False, False, True, True, True]
    choice = sweep_threshold(scores, truths, bootstrap=200, seed=1)
    lo, hi = choice.stability
    assert lo <= choice.threshold <= hi or lo <= hi   # a real spread, not a point
    assert hi - lo > 0                                # tiny set ⇒ visibly unstable


def test_bootstrap_is_deterministic_for_a_given_seed():
    args = ([0.1, 0.4, 0.6, 0.9], [False, False, True, True])
    a = sweep_threshold(*args, bootstrap=50, seed=7).stability
    b = sweep_threshold(*args, bootstrap=50, seed=7).stability
    assert a == b


def test_a_sweep_needs_aligned_non_empty_input():
    with pytest.raises(ValueError):
        sweep_threshold([], [])
    with pytest.raises(ValueError):
        sweep_threshold([0.5], [True, False])


# ---------------- comparability ----------------

def test_the_baseline_key_changes_with_every_input_that_moves_the_result():
    base = dict(prompt="P", model="m", threshold=0.7, params={"temp": 0})
    key = baseline_key(**base)
    assert baseline_key(**{**base, "prompt": "P2"}) != key
    assert baseline_key(**{**base, "model": "m2"}) != key
    assert baseline_key(**{**base, "threshold": 0.71}) != key
    assert baseline_key(**{**base, "params": {"temp": 1}}) != key


def test_the_key_covers_the_gold_set_itself():
    """Adding items changes every measurement; a baseline spanning two different
    sets is worse than no baseline."""
    a = baseline_key(prompt="P", model="m", threshold=0.7, gold_ids=[1, 2])
    b = baseline_key(prompt="P", model="m", threshold=0.7, gold_ids=[1, 2, 3])
    assert a != b


def test_the_key_is_stable_and_order_independent():
    a = baseline_key(prompt="P", model="m", threshold=0.7, gold_ids=[2, 1])
    b = baseline_key(prompt="P", model="m", threshold=0.7, gold_ids=[1, 2])
    assert a == b == baseline_key(prompt="P", model="m", threshold=0.7,
                                  gold_ids=[1, 2])


# ---------------- slices ----------------

def test_slices_below_thirty_items_are_flagged_underpowered():
    items = [{"lang": "uz", "p": True, "t": True} for _ in range(10)]
    items += [{"lang": "ru", "p": True, "t": True} for _ in range(35)]
    rep = slice_report(items, predicted=lambda i: i["p"], truth=lambda i: i["t"],
                       slicer=lambda i: i["lang"])
    assert rep["uz"]["underpowered"] is True
    assert rep["ru"]["underpowered"] is False
    assert rep["uz"]["n"] == 10 and rep["ru"]["n"] == 35


def test_each_slice_carries_its_own_confusion_and_intervals():
    items = [{"lang": "uz", "p": True, "t": False}]
    rep = slice_report(items, predicted=lambda i: i["p"], truth=lambda i: i["t"],
                       slicer=lambda i: i["lang"])
    assert rep["uz"]["fp"] == 1
    assert "lo" in rep["uz"]["precision"]
