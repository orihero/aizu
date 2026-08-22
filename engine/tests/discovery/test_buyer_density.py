"""Buyer density — the moat metric (Campaign Lab, Remedy Sheet #2 / Remedy B).

No commercial tool scores this: HypeAuditor and Modash score audience
AUTHENTICITY, nobody scores buyer-question density in comment sections. For an
engine whose leads ARE commenters, that is the number that decides whether a seed
is worth a warmed browser's time.

The hard case throughout is that a comment section full of COMPETING VENDORS looks
superficially identical to one full of buyers — both are dense with prices and
phone numbers. Direction is the discriminator.
"""
import pytest

from aizu.discovery.buyer_density import (BuyerDensity, classify_comment,
                                          rank_candidates, score_comments)


@pytest.mark.parametrize("text", [
    "Narxi qancha?", "narxi?", "Qancha turadi", "nechpul",
    "Нархи қанча?", "сколько стоит?", "Цена?", "почём",
    "how much?", "price?", "What's the pricing",
])
def test_price_asks_are_caught_across_languages_and_scripts(text):
    assert classify_comment(text).asks_price


@pytest.mark.parametrize("text", [
    "qanday buyurtma qilaman", "как заказать?", "how do i order",
])
def test_purchase_intent_beyond_price_is_a_buyer(text):
    sig = classify_comment(text)
    assert sig.is_buyer and not sig.asks_price


@pytest.mark.parametrize("text", [
    "Bormi hali?", "есть в наличии?", "still available?",
])
def test_availability_questions_are_buyers(text):
    assert classify_comment(text).is_buyer


@pytest.mark.parametrize("text", [
    "ajoyib 🔥", "🔥🔥🔥", "super", "", "   ", "Rahmat!",
])
def test_enthusiasm_is_not_intent(text):
    """The hard negative Sheet #3 also names: an enthusiastic non-buyer."""
    assert not classify_comment(text).is_buyer


@pytest.mark.parametrize("text", [
    "Bizda bor, yozing", "заказывайте у нас", "от 99 000 сум",
    "starting at $50", "order now", "пишите в директ",
])
def test_sellers_are_recognised_as_supply_side(text):
    assert classify_comment(text).seller


def test_a_vendor_quoting_a_price_is_not_a_buyer():
    """The single most important discrimination here — the word 'narx' fires for
    both sides, so the word alone proves nothing."""
    sig = classify_comment("narxi 500000 dan boshlab")
    assert sig.seller and not sig.is_buyer and not sig.asks_price


def test_a_volunteered_phone_number_is_seller_shaped():
    assert classify_comment("+998 90 123 45 67").seller


def test_a_buyer_leaving_a_callback_number_is_still_a_buyer():
    """Direction, not presence: a question WITH a number is someone asking to be
    called back, not someone advertising."""
    sig = classify_comment("narxi qancha? +998901234567")
    assert sig.is_buyer and not sig.seller


# ---------------- aggregate ----------------

def test_price_asks_are_reported_per_100_so_sizes_compare():
    small = score_comments(["narxi?"] + ["nice"] * 9)
    big = score_comments(["narxi?"] * 10 + ["nice"] * 90)
    assert small.price_asks_per_100 == big.price_asks_per_100 == 10.0


def test_the_owner_is_not_their_own_audience():
    d = score_comments(
        [{"text": "narxi?", "username": "buyer"},
         {"text": "500 000 so'm", "username": "acme"},
         {"text": "rahmat", "username": "acme"}],
        owner="@acme")
    assert d.comments == 1 and d.owner_replies == 2
    assert d.owner_reply_rate == 2.0     # replies per audience comment


def test_a_vendor_pit_scores_below_a_buyer_section():
    buyers = score_comments(["narxi qancha?"] * 5 + ["ajoyib"] * 15)
    vendors = score_comments(["от 99 000 сум, заказывайте"] * 5 + ["ajoyib"] * 15)
    assert buyers.score > vendors.score
    assert vendors.seller_share > 0


def test_an_empty_section_scores_zero_without_dividing_by_zero():
    d = score_comments([])
    assert d.score == 0.0
    assert d.price_asks_per_100 == 0.0 and d.buyer_share == 0.0


def test_the_score_stays_in_range_even_when_every_comment_is_a_seller():
    d = score_comments(["заказывайте, от 10 000 сум"] * 30)
    assert 0.0 <= d.score <= 1.0


def test_comment_objects_dicts_and_strings_are_all_accepted():
    from aizu.core.feed import Comment
    d = score_comments([
        Comment(comment_id="1", username="a", text="narxi?"),
        {"text": "сколько стоит?", "username": "b"},
        "how much?",
    ])
    assert d.comments == 3 and d.price_asks == 3


# ---------------- ranking ----------------

def test_under_sampled_candidates_are_dropped_not_ranked():
    """3 comments where one asks a price reads as 33 price-asks per 100 — nonsense."""
    tiny = score_comments(["narxi?", "a", "b"])
    real = score_comments(["narxi?"] * 3 + ["ok"] * 27)
    ranked = rank_candidates([("tiny", tiny), ("real", real)], min_comments=20)
    assert [seed for seed, _ in ranked] == ["real"]


def test_ranking_is_best_first_and_stable():
    hot = score_comments(["narxi qancha?"] * 10 + ["ok"] * 10)
    cold = score_comments(["ok"] * 20)
    ranked = rank_candidates([("cold", cold), ("hot", hot)])
    assert [seed for seed, _ in ranked] == ["hot", "cold"]


def test_as_dict_is_json_safe():
    import json
    json.dumps(score_comments(["narxi?"] * 5).as_dict())
