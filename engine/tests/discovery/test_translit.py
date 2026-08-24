"""Uzbek script fan-out (Campaign Lab, Remedy Sheet #1 / Remedy A.3).

A hashtag typed in Latin is a different string from the same word in Cyrillic and
no platform folds them, so a brief seeded in one script silently misses the other.
"""
from aizu.discovery.translit import (is_cyrillic, script_variants,
                                     strip_apostrophes, to_cyrillic, to_latin)


def test_digraphs_win_over_single_letters():
    # "sh" must not decompose into "s" + "h".
    assert to_cyrillic("shifoxona") == "шифохона"
    assert to_cyrillic("chegara") == "чегара"


def test_latin_apostrophe_letters_map_to_their_own_cyrillic_letters():
    assert to_cyrillic("oʻzbek") == "ўзбек"
    assert to_cyrillic("gʻisht") == "ғишт"


def test_cyrillic_to_latin_uses_the_official_orthography():
    assert to_latin("ўзбек") == "oʻzbek"
    assert to_latin("ғишт") == "gʻisht"


def test_transliteration_is_directional_not_double_applied():
    assert to_cyrillic("ремонт") == "ремонт"     # already Cyrillic → unchanged
    assert to_latin("remont") == "remont"        # already Latin → unchanged


def test_is_cyrillic_is_about_the_majority_not_purity():
    assert is_cyrillic("ремонт квартир Toshkent")   # Cyrillic letters outnumber Latin
    assert not is_cyrillic("remont kvartir Тошкент")
    assert not is_cyrillic("2024 !!!")           # no letters at all


def test_apostrophe_variants_all_collapse():
    for mark in "'‘’ʻʼ`":
        assert strip_apostrophes(f"o{mark}zbek") == "ozbek"


def test_script_variants_lead_with_the_original_and_dedupe():
    out = script_variants("oʻzbek")
    assert out[0] == "oʻzbek"
    assert "ўзбек" in out and "ozbek" in out
    assert len(out) == len(set(out))


def test_script_variants_of_plain_ascii_add_the_cyrillic_spelling():
    assert script_variants("remont") == ["remont", "ремонт"]


def test_empty_input_yields_nothing():
    assert script_variants("") == [] and script_variants("   ") == []
