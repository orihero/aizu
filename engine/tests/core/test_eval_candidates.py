"""Negative capture — the flip-list substrate (schema v26, Sheet #3 / Remedy E).

Every comment the match gate REJECTS is scored, paid for, and discarded:
`session.py`'s `if res.is_match:` is the only path to `matches`. Measured on the
live DB 2026-08-21: 2 accepted comments, zero rejects, and no table anywhere
holding one. A gold set could not be built at all — and the negatives are its
expensive half, because easy positives do not discriminate between two prompts.
"""
import os
import tempfile

import pytest

from aizu.core.store import Store


@pytest.fixture
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = Store(path)
    yield s
    s.close()


# ---------------- banding ----------------

def test_an_accepted_verdict_is_its_own_band():
    assert Store.eval_band(0.9, 0.7, True) == "accepted"


def test_a_rejection_near_the_threshold_is_a_boundary_case():
    """These decide where a threshold lands, and they are rare."""
    assert Store.eval_band(0.62, 0.7, False) == "near"
    assert Store.eval_band(0.7 - Store.NEAR_BAND, 0.7, False) == "near"


def test_an_obvious_rejection_is_clear():
    assert Store.eval_band(0.05, 0.7, False) == "clear"


def test_a_missing_score_is_treated_as_clear_not_as_a_boundary():
    assert Store.eval_band(None, 0.7, False) == "clear"


# ---------------- sampling ----------------

def test_boundary_cases_are_never_sampled_away():
    assert all(Store.eval_should_capture(f"c{i}", "near") for i in range(50))
    assert all(Store.eval_should_capture(f"c{i}", "accepted") for i in range(50))


def test_obvious_noise_is_sampled_down():
    kept = sum(Store.eval_should_capture(f"c{i}", "clear") for i in range(800))
    expected = 800 / Store.CLEAR_SAMPLE_RATE
    assert 0.6 * expected < kept < 1.6 * expected


def test_sampling_is_deterministic_so_a_repoll_lands_the_same_way():
    """`random` would fill the table with near-duplicates of whatever happened to
    be re-scored, and no test could assert the decision."""
    first = [Store.eval_should_capture(f"c{i}", "clear") for i in range(100)]
    second = [Store.eval_should_capture(f"c{i}", "clear") for i in range(100)]
    assert first == second


# ---------------- persistence ----------------

def _capture(store, comment_id, band="near", **kw):
    kw.setdefault("text", "narxi qancha?")
    kw.setdefault("score", 0.62)
    kw.setdefault("threshold", 0.7)
    return store.record_eval_candidate(
        campaign_id="c1", comment_id=comment_id, band=band, **kw)


def test_a_rejected_comment_survives_the_run_that_scored_it(store):
    _capture(store, "k1", username="buyer", lang="uz", confidence=0.8,
             reason="asks price but unclear", tier="cloud", raw='{"score":0.62}')
    (row,) = store.eval_candidates("c1")
    assert row["text"] == "narxi qancha?"
    assert row["score"] == 0.62 and row["confidence"] == 0.8
    assert row["threshold"] == 0.7 and row["raw"] == '{"score":0.62}'
    assert row["label"] is None          # unlabelled until a human says otherwise


def test_an_empty_comment_is_not_worth_storing(store):
    assert _capture(store, "k1", text="") is False
    assert _capture(store, "k2", text="   ") is False
    assert store.eval_candidates("c1") == []


def test_a_repoll_refreshes_the_model_fields(store):
    _capture(store, "k1", score=0.62)
    _capture(store, "k1", score=0.81, band="accepted")
    (row,) = store.eval_candidates("c1")
    assert row["score"] == 0.81 and row["band"] == "accepted"


def test_a_repoll_never_overwrites_a_human_label(store):
    """The same rule as `matches.status`: a human verdict outranks every later
    machine opinion about it. This is ground truth — if a re-score could erase
    it, the gold set would silently drift toward whatever the model believes."""
    _capture(store, "k1")
    store.label_eval_candidate("c1", "k1", True, labeled_by="ali")
    _capture(store, "k1", score=0.02, band="clear")
    (row,) = store.eval_candidates("c1")
    assert row["label"] == 1 and row["labeled_by"] == "ali"
    assert row["score"] == 0.02          # the model's view did update


# ---------------- the labelling queue ----------------

def test_the_queue_puts_the_most_informative_items_first(store):
    _capture(store, "clear1", band="clear", score=0.02)
    _capture(store, "accepted1", band="accepted", score=0.91)
    _capture(store, "near1", band="near", score=0.68)
    order = [r["comment_id"] for r in store.eval_candidates("c1")]
    assert order == ["near1", "accepted1", "clear1"]


def test_near_band_items_are_ordered_by_closeness_to_the_threshold(store):
    _capture(store, "far", band="near", score=0.58)
    _capture(store, "closest", band="near", score=0.69)
    assert [r["comment_id"] for r in store.eval_candidates("c1")][0] == "closest"


def test_the_queue_can_show_only_what_still_needs_a_human(store):
    _capture(store, "done")
    _capture(store, "todo")
    store.label_eval_candidate("c1", "done", False)
    assert [r["comment_id"] for r in
            store.eval_candidates("c1", unlabelled_only=True)] == ["todo"]


def test_candidates_are_scoped_per_campaign_and_platform(store):
    _capture(store, "k1", platform="instagram")
    store.record_eval_candidate(campaign_id="c2", comment_id="k2", text="x",
                                band="near", platform="youtube")
    assert len(store.eval_candidates("c1")) == 1
    assert store.eval_candidates("c1", platform="youtube") == []


def test_the_session_cap_is_countable(store):
    for i in range(5):
        _capture(store, f"k{i}", session_id="s1")
    _capture(store, "other", session_id="s2")
    assert store.eval_candidate_count("c1", session_id="s1") == 5
    assert store.eval_candidate_count("c1") == 6
