"""Caption co-occurrence miner (Campaign Lab, Remedy Sheet #1 / Remedy A.4).

The point of the ranking is that it must NOT return the generic tag that appears
on every post in the niche — the thing raw frequency returns by construction.
"""
import pytest

from aizu.core.parsers import co_occurring_tags, extract_hashtags
from aizu.core.tagmine import (DEFAULT_MIN_SUPPORT, mine_captions,
                               wilson_lower_bound)


def test_extract_hashtags_is_unicode_aware_and_normalized():
    tags = extract_hashtags("Ремонт под ключ #ремонт #Toshkentda #remont2024")
    assert tags == ["ремонт", "toshkentda", "remont2024"]


def test_extract_hashtags_drops_noise_and_dedupes():
    assert extract_hashtags("#123 #_leading #a #A") == ["a"]


def test_co_occurring_tags_drops_the_seed_that_found_the_post():
    # A post discovered on #remont carries #remont, so an un-excluded seed wins
    # its own ranking and buries everything the miner exists to surface.
    assert co_occurring_tags("#remont #dizayn", exclude=("#remont",)) == ["dizayn"]


def _rows(spec):
    return [{"caption": cap, "relevant": rel} for cap, rel in spec]


def test_generic_tag_loses_to_the_discriminative_one():
    rows = _rows([("#remont #toshkent #dizayn", 1)] * 4
                 + [("#remont #toshkent #memes", 0)] * 4)
    ranked = mine_captions(rows, exclude=("remont",))
    assert [c.tag for c in ranked] == ["dizayn", "toshkent", "memes"]
    # #toshkent is the MOST frequent co-occurrence and still does not rank first.
    assert ranked[1].support > ranked[0].support


def test_ungated_rows_count_for_neither_side():
    """`relevant` is tri-state; NULL means 'never scored'. Treating it as a
    negative would bury every tag from a session that halted early."""
    rows = _rows([("#a", 1), ("#a", 1), ("#a", 1)] + [("#a", None)] * 20)
    (cand,) = mine_captions(rows)
    assert (cand.support, cand.relevant) == (3, 3)


def test_thin_support_cannot_top_the_ranking():
    rows = _rows([("#lucky", 1)]
                 + [("#solid", 1)] * 9 + [("#solid", 0)]
                 + [("#filler", 0)] * 10)
    ranked = mine_captions(rows, min_support=1)
    assert ranked[0].tag == "solid"      # 9/10 beats a perfect 1/1
    lucky = next(c for c in ranked if c.tag == "lucky")
    assert lucky.precision == 1.0 and lucky.score < ranked[0].score


def test_min_support_floor_is_applied():
    rows = _rows([("#a #b", 1)] * (DEFAULT_MIN_SUPPORT - 1) + [("#b", 1)] * 5)
    assert [c.tag for c in mine_captions(rows)] == ["b"]


def test_empty_and_ungated_corpora_return_nothing():
    assert mine_captions([]) == []
    assert mine_captions(_rows([("#a", None)] * 5)) == []


def test_lift_is_zero_when_the_campaign_has_never_scored_a_pass():
    ranked = mine_captions(_rows([("#a", 0)] * 5))
    assert ranked[0].lift == 0.0        # not infinity, and not a crash


def test_wilson_lower_bound_bounds():
    assert wilson_lower_bound(0, 0) == 0.0
    assert wilson_lower_bound(0, 10) == 0.0
    assert 0 < wilson_lower_bound(1, 1) < 1.0
    # More evidence for the same rate ⇒ a tighter (higher) lower bound.
    assert wilson_lower_bound(90, 100) > wilson_lower_bound(9, 10)


def test_mine_campaign_reads_the_store(tmp_path):
    from aizu.core.store import Store
    store = Store(tmp_path / "t.db")
    for i in range(4):
        store.mark_seen("c1", f"r{i}", relevant=True, caption="#remont #dizayn")
    for i in range(4, 8):
        store.mark_seen("c1", f"r{i}", relevant=False, caption="#remont #memes")
    from aizu.core.tagmine import mine_campaign
    assert [c.tag for c in mine_campaign(store, "c1", exclude=("remont",))] \
        == ["dizayn", "memes"]
    store.close()
