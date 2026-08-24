"""The demand-side request-pattern matrix (Remedy Sheet #1 / Remedy A.2).

Buyers write "videograf kerak", not "#videography". These lock that the matrix
produces buyer-shaped strings, stays inside its cap, and never substitutes
English patterns for a language it has none for.
"""
from aizu.discovery.patterns import (REQUEST_PATTERNS, demand_queries,
                                     request_patterns)


def test_patterns_are_filtered_by_language():
    assert {r[0] for r in request_patterns(["uz"])} == {"uz"}
    assert {r[0] for r in request_patterns(["uz", "ru"])} == {"uz", "ru"}


def test_an_unsupported_language_yields_nothing_rather_than_english():
    assert request_patterns(["ja"]) == []


def test_locale_codes_are_accepted():
    assert request_patterns(["ru-RU"]) == request_patterns(["ru"])


def test_queries_are_buyer_shaped_and_noun_major():
    out = demand_queries(["videograf"], langs=["uz"], limit=4)
    assert out[0] == "videograf narxi"           # price ask ranks first
    assert all("videograf" in q for q in out)


def test_families_can_be_narrowed():
    out = demand_queries(["remont"], langs=["ru"], families=["price_ask"])
    assert out == ["remont сколько стоит", "remont цена"]


def test_leading_hash_and_blanks_are_ignored():
    assert demand_queries(["#remont", "", "  "], langs=["uz"], limit=1) \
        == ["remont narxi"]


def test_the_cap_is_enforced_across_all_nouns():
    out = demand_queries(["a", "b", "c"], limit=7)
    assert len(out) == 7


def test_no_duplicate_queries():
    out = demand_queries(["x", "x"], langs=["uz", "ru", "en"])
    assert len(out) == len(set(out))


def test_every_pattern_row_has_a_noun_slot():
    assert all("{}" in template for _lang, _fam, template in REQUEST_PATTERNS)
