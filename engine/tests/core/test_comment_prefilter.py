"""The cheap comment pre-filter (Campaign Lab, Remedy Sheet #3 / Remedy C).

Three cascade docstrings promised "local pre-filter → local scoring →
escalate-if-unsure → cloud" and no pre-filter existed: every comment, including
every bare "🔥🔥", bought a model call.

The rule that shapes everything here: a pre-filtered comment is NEVER SCORED AND
NEVER STORED, so a wrong skip is an invisible lost lead. Only certainties are
filtered; anything arguable goes to the model.
"""
import pytest

from aizu.core.matching import (SKIP_DUPLICATE, SKIP_EMPTY, SKIP_NO_WORDS,
                                comment_prefilter_reason)


def f(text, username="someone", seen=None):
    return comment_prefilter_reason(text, username=username, seen=seen)


@pytest.mark.parametrize("text", ["", "   ", "\n\t", None])
def test_nothing_at_all_is_skipped(text):
    assert f(text) == SKIP_EMPTY


@pytest.mark.parametrize("text", ["🔥🔥", "👍", "+++", "!!!", "...", "❤️❤️❤️"])
def test_reaction_only_comments_are_skipped(text):
    assert f(text) == SKIP_NO_WORDS


@pytest.mark.parametrize("text", [
    "narxi qancha?", "how much?", "цена?", "👍 narxi?", "ok", "a",
])
def test_anything_with_a_letter_reaches_the_model(text):
    assert f(text) is None


@pytest.mark.parametrize("phone", [
    "+998 90 123 45 67", "+998901234567", "998901234567", "90 123 45 67",
])
def test_a_bare_phone_number_survives_despite_having_no_letters(phone):
    """A volunteered number is a real signal. A run-based digit test filtered
    exactly these, because a real number is written with spaces far more often
    than as one unbroken run."""
    assert f(phone) is None


@pytest.mark.parametrize("text", ["12", "1 2 3", "2024", "5"])
def test_short_digit_strings_are_still_noise(text):
    assert f(text) == SKIP_NO_WORDS


# ---------------- duplicates are keyed on the AUTHOR ----------------

def test_two_people_asking_the_same_common_question_are_two_leads():
    """THE case text-only dedupe gets wrong, and it gets it wrong precisely on the
    highest-value comments: short, common buyer phrases."""
    seen = set()
    assert f("narxi qancha?", username="buyer_one", seen=seen) is None
    assert f("narxi qancha?", username="buyer_two", seen=seen) is None


def test_one_account_repeating_itself_is_skipped():
    seen = set()
    assert f("check my page!", username="spammer", seen=seen) is None
    assert f("check my page!", username="spammer", seen=seen) == SKIP_DUPLICATE


def test_duplicate_matching_ignores_case_and_whitespace():
    seen = set()
    assert f("Narxi   Qancha?", username="u", seen=seen) is None
    assert f("  narxi qancha?  ", username="u", seen=seen) == SKIP_DUPLICATE


def test_the_same_author_saying_something_new_is_not_a_duplicate():
    seen = set()
    f("narxi?", username="u", seen=seen)
    assert f("qachon tayyor?", username="u", seen=seen) is None


def test_without_a_session_set_nothing_is_deduped():
    assert f("same", username="u") is None
    assert f("same", username="u") is None


def test_an_anonymous_comment_is_never_deduped():
    """No author means the one signal that makes dedupe safe is missing."""
    seen = set()
    assert f("same text", username="", seen=seen) is None
    assert f("same text", username="", seen=seen) is None


# ---------------- what must NOT be filtered ----------------

@pytest.mark.parametrize("seller", [
    "Bizda bor, yozing", "заказывайте у нас", "от 99 000 сум",
    "order now, DM me", "+998901112233 arzon narx",
])
def test_sellers_are_never_pre_filtered(seller):
    """Sheet #3 / Remedy B is explicit: supply-side commenters are ROUTED for
    competitor intel, never dropped. Detecting them is buyer_density's job;
    acting on them belongs to the output-contract redesign, not a silent skip."""
    assert f(seller) is None


def test_the_filter_is_conservative_on_anything_arguable():
    for text in ("hmm", "?", "??", "sotib olaman", "bu nima", "🔥 zor, narxi?"):
        reason = f(text)
        assert reason in (None, SKIP_NO_WORDS)
        if reason == SKIP_NO_WORDS:
            assert not any(c.isalpha() for c in text)
