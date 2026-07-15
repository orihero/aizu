from reelradar.core.matching import compute_found_by


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
