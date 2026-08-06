from aizu.core.matching import (compute_found_by, corroboration_needs_review,
                                ground_extracted)


def test_found_by_always_includes_primary():
    assert compute_found_by("prod-model", [], 0.5) == ["prod-model"]


def test_found_by_adds_comparisons_that_clear_threshold():
    comparisons = [
        {"model": "candidate-a", "score": 0.8, "error": None},
        {"model": "candidate-b", "score": 0.3, "error": None},
    ]
    assert compute_found_by("prod-model", comparisons, 0.5) == ["prod-model", "candidate-a"]


def test_found_by_excludes_a_failed_comparison():
    comparisons = [{"model": "candidate-a", "score": None, "error": "timeout"}]
    assert compute_found_by("prod-model", comparisons, 0.5) == ["prod-model"]


def test_found_by_deduplicates():
    comparisons = [
        {"model": "prod-model", "score": 0.9, "error": None},
        {"model": "candidate-a", "score": 0.9, "error": None},
        {"model": "candidate-a", "score": 0.9, "error": None},
    ]
    assert compute_found_by("prod-model", comparisons, 0.5) == ["prod-model", "candidate-a"]


def test_found_by_threshold_edge_is_inclusive():
    comparisons = [{"model": "candidate-a", "score": 0.5, "error": None}]
    assert compute_found_by("prod-model", comparisons, 0.5) == ["prod-model", "candidate-a"]


def test_found_by_empty_primary_model_yields_only_comparisons():
    comparisons = [{"model": "candidate-a", "score": 0.9, "error": None}]
    assert compute_found_by("", comparisons, 0.5) == ["candidate-a"]


# ----- ground_extracted (gap #4 grounding check) -----

def test_ground_extracted_drops_hallucinated_phone():
    extracted = {"phone": "+19995551234", "intent": "pricing"}
    grounded = ground_extracted(extracted, "how much is the Pro plan?")
    assert grounded == {"phone": None, "intent": "pricing"}


def test_ground_extracted_keeps_a_real_phone_despite_formatting_differences():
    extracted = {"phone": "+14155550142"}
    # source text has spaces the model's normalized output doesn't — digits-only
    # comparison must still ground it.
    grounded = ground_extracted(extracted, "call me at +1 415 555 0142 thanks")
    assert grounded == {"phone": "+14155550142"}


def test_ground_extracted_drops_hallucinated_email():
    extracted = {"email": "made-up@nowhere.com"}
    grounded = ground_extracted(extracted, "reach me on whatsapp only")
    assert grounded == {"email": None}


def test_ground_extracted_keeps_a_real_email():
    extracted = {"email": "Marina@Acme.io"}
    grounded = ground_extracted(extracted, "email me at marina@acme.io please")
    assert grounded == {"email": "Marina@Acme.io"}


def test_ground_extracted_leaves_non_contact_fields_untouched():
    # A budget/plan field that happens not to appear verbatim must NOT be dropped
    # — grounding only applies to contact-shaped fields.
    extracted = {"budget": "5000000", "plan": "Pro"}
    grounded = ground_extracted(extracted, "totally unrelated comment text")
    assert grounded == {"budget": "5000000", "plan": "Pro"}


def test_ground_extracted_noop_on_empty_or_missing():
    assert ground_extracted(None, "hi") == {}
    assert ground_extracted({}, "hi") == {}


def test_ground_extracted_multiple_sources_searched():
    # sources = (comment content, reel caption) — a value grounded in EITHER.
    extracted = {"phone": "+14155550142"}
    grounded = ground_extracted(extracted, "how much?", "call +1 415 555 0142 for a demo")
    assert grounded == {"phone": "+14155550142"}


# ----- corroboration_needs_review (gap #4 corroboration gate) -----

def test_corroboration_no_comparisons_is_a_noop():
    assert corroboration_needs_review(0.8, [], 0.5) is False


def test_corroboration_agreement_keeps_verdict():
    comparisons = [{"model": "candidate-a", "score": 0.9, "error": None}]
    assert corroboration_needs_review(0.8, comparisons, 0.5) is False


def test_corroboration_disagreement_needs_review():
    comparisons = [{"model": "candidate-a", "score": 0.2, "error": None}]
    assert corroboration_needs_review(0.8, comparisons, 0.5) is True


def test_corroboration_error_is_inconclusive():
    comparisons = [{"model": "candidate-a", "score": None, "error": "timeout"}]
    assert corroboration_needs_review(0.8, comparisons, 0.5) is True


def test_corroboration_scoreless_no_error_is_inconclusive():
    comparisons = [{"model": "candidate-a", "score": None, "error": None}]
    assert corroboration_needs_review(0.8, comparisons, 0.5) is True


def test_corroboration_one_disagreement_among_many_still_flags():
    comparisons = [
        {"model": "candidate-a", "score": 0.9, "error": None},
        {"model": "candidate-b", "score": 0.1, "error": None},
    ]
    assert corroboration_needs_review(0.8, comparisons, 0.5) is True
