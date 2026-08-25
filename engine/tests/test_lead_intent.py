"""`derive_intent` — the redaction boundary for the customer-facing lead line.

The org-facing payload has no username and no comment text (schema v27), so
every assertion here is a security assertion: whatever leaks into this string is
published to every viewer of the org.
"""
from aizu.core.matching import INTENT_MAX_CHARS, derive_intent


def test_model_intent_is_kept_and_whitespace_collapsed():
    out = derive_intent("  Wants red Nike\n sneakers,  size 42  ",
                        extracted={"phone": "+998901234567"},
                        post_caption="New running shoes drop",
                        comment_text="narxi qancha?")
    assert out == "Wants red Nike sneakers, size 42"


def test_verbatim_comment_echo_is_rejected_and_falls_back():
    comment = "Do you have these in size 42? How much?"
    out = derive_intent(comment,
                        extracted={"product": "sneakers", "size": "42"},
                        post_caption="Nike running shoes in stock",
                        comment_text=comment)
    # Not the comment: the fallback line built from the grounded fields instead.
    assert comment.lower() not in out.lower()
    assert out.startswith("Interested in sneakers, size 42")


def test_truncated_comment_echo_is_rejected_too():
    # A model that "summarizes" by copying the comment and dropping the last few
    # words is still handing back the comment.
    comment = ("Do you deliver these sneakers to Tashkent and how much is the "
               "size 42 pair in total")
    echo = comment.rsplit(" ", 2)[0]
    out = derive_intent(echo, extracted={"city": "Tashkent"},
                        post_caption="Sneaker delivery across Uzbekistan",
                        comment_text=comment)
    assert not out.lower().startswith("do you deliver")


def test_handle_email_and_phone_are_stripped_from_a_kept_intent():
    out = derive_intent(
        "@buyer_007 wants the size 42 pair, write to buyer@example.com "
        "or +998 90 123 45 67 about delivery",
        extracted={},
        post_caption="Sneakers in stock",
        comment_text="different text entirely")
    assert "@" not in out
    assert "buyer_007" not in out
    assert "example.com" not in out
    assert "998" not in out
    assert "size 42" in out


def test_profile_links_are_stripped_from_a_kept_intent():
    # A bare "t.me/handle" is a handle with a domain in front of it.
    out = derive_intent("Wants a quote, reach them on t.me/buyer_007 or "
                        "https://instagram.com/buyer.007",
                        extracted={}, post_caption="Renovation quotes",
                        comment_text="qancha?")
    assert "buyer_007" not in out and "buyer.007" not in out
    assert "t.me" not in out and "instagram.com" not in out


def test_a_budget_number_survives_the_phone_strip():
    # The phone rule counts DIGITS, not characters: "500 000" is a budget, not a
    # subscriber number.
    out = derive_intent("Wants a renovation quote, budget 500 000 sum",
                        extracted={}, post_caption="Tashkent renovations",
                        comment_text="qancha turadi?")
    assert "500 000" in out


def test_fallback_composes_non_contact_extracted_plus_post_topic():
    out = derive_intent(None,
                        extracted={"product": "sneakers", "size": "42",
                                   "phone": "+998901234567",
                                   "email": "buyer@example.com"},
                        post_caption="Running shoes restock! #nike #sneakers",
                        comment_text="bormi?")
    assert out == ("Interested in sneakers, size 42 — asking on a post about "
                   "Running shoes restock")
    assert "998" not in out and "example.com" not in out


def test_fallback_drops_contact_shaped_keys_whatever_they_are_named():
    out = derive_intent("", extracted={"whatsapp_contact": "+998901234567",
                                       "product": "sneakers"},
                        post_caption="", comment_text="?")
    assert out == "Interested in sneakers"


def test_fallback_strips_identity_out_of_a_non_contact_value():
    # A model that stuffed a handle into a free-text field doesn't get to
    # publish it through the back door.
    out = derive_intent(None, extracted={"note": "ping @buyer_007 about size 42"},
                        post_caption="", comment_text="hi")
    assert "@" not in out and "buyer_007" not in out


def test_fallback_never_echoes_the_comment():
    comment = "How much are the size 42 sneakers?"
    out = derive_intent(None, extracted={"question": comment},
                        post_caption="", comment_text=comment)
    # The only fact available WAS the comment, so there is nothing safe to say.
    assert out == ""


def test_topic_only_fallback_when_nothing_was_extracted():
    out = derive_intent(None, extracted={},
                        post_caption="Tashkent apartment renovation. Call us today!",
                        comment_text="narxi?")
    assert out == "Asking on a post about Tashkent apartment renovation"


def test_empty_everything_yields_empty_string():
    assert derive_intent(None, extracted=None, post_caption=None,
                         comment_text=None) == ""
    assert derive_intent("   ", extracted={}, post_caption="  ",
                         comment_text="  ") == ""
    # A non-string intent (a model returning a dict/number) is not usable either.
    assert derive_intent({"want": "x"}, extracted={}, post_caption="",
                         comment_text="") == ""


def test_long_intent_is_truncated_on_a_word_boundary():
    long_intent = "Wants " + " ".join(["renovation"] * 40)
    out = derive_intent(long_intent, extracted={}, post_caption="",
                        comment_text="x")
    assert len(out) <= INTENT_MAX_CHARS
    assert out.endswith("…")
    assert not out[:-1].endswith(" ")
    assert "renovatio…" not in out  # cut between words, not mid-word
