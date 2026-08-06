"""Telegram cascade — text-only relevance + reply matching, no vision tier.

Telegram is text-first, so the cascade must NEVER call classify_image and must
use the channel message as reply context with a Telegram-shaped scaffold
("CHANNEL MESSAGE BEING REPLIED TO").
"""
from aizu.core.config import campaign_from_brief
from aizu.core.feed import Comment, Reel
from aizu.core.router import Decision
from aizu.engines.telegram.cascade import TelegramCascade, _comment_content


class SpyRouter:
    def __init__(self):
        self.text_calls = []
        self.image_calls = []

    def classify_text(self, *, instruction, content, campaign_id, stage,
                      session_id=None, system=None):
        self.text_calls.append({"stage": stage, "content": content})
        low = content.lower()
        if stage == "relevance":
            relevant = any(k in low for k in ("acme", "app", "saas", "demo"))
            return Decision(label="relevant" if relevant else "irrelevant",
                            score=0.9 if relevant else 0.1, confidence=0.96)
        is_lead = any(k in low for k in ("pricing", "+1", "demo", "price", "buy"))
        return Decision(label="yes" if is_lead else "no",
                        score=0.92 if is_lead else 0.1, confidence=0.96,
                        extracted={"phone": "+14155550142", "junk": "x"} if is_lead else {})

    def classify_image(self, *, instruction, images_b64, campaign_id, stage,
                       session_id=None, system=None):
        self.image_calls.append({"stage": stage})
        return Decision(label="irrelevant", score=0.0, confidence=1.0)


def _campaign():
    return campaign_from_brief("tg-test", {
        "platform": "telegram", "threshold": 0.7,
        "relevance_def": "saas product posts",
        "match_def": "a replier asking to buy / price / contact",
        "extract_def": "- phone",
    })


def test_gate_message_relevant_is_text_only():
    spy = SpyRouter()
    res = TelegramCascade(spy, _campaign()).gate_message(
        Reel(reel_id="@dev/10", caption="Acme app demo, free trial", author="@dev"))
    assert res.relevant is True
    assert spy.image_calls == []
    assert spy.text_calls[0]["stage"] == "relevance"


def test_gate_message_irrelevant():
    spy = SpyRouter()
    res = TelegramCascade(spy, _campaign()).gate_message(
        Reel(reel_id="@dev/11", caption="funny cat compilation", author="@dev"))
    assert res.relevant is False
    assert spy.image_calls == []


def test_score_reply_match_coerces_to_declared_fields():
    spy = SpyRouter()
    reel = Reel(reel_id="@dev/10", caption="Acme app demo", author="@dev")
    res = TelegramCascade(spy, _campaign()).score_comment(
        Comment("@dev/10/3", "aziz", "how much is the Pro plan? +1 415 555 0142", is_reply=True), reel)
    assert res.is_match is True
    assert set(res.decision.extracted) == {"phone"}
    assert spy.image_calls == []


def test_score_reply_uses_channel_message_scaffold():
    spy = SpyRouter()
    reel = Reel(reel_id="@dev/10", caption="Acme app demo", author="@dev")
    TelegramCascade(spy, _campaign()).score_comment(
        Comment("@dev/10/3", "aziz", "pricing?", is_reply=True), reel)
    match_call = [c for c in spy.text_calls if c["stage"] == "match"][0]
    assert "CHANNEL MESSAGE BEING REPLIED TO" in match_call["content"]
    assert "REEL BEING COMMENTED ON" not in match_call["content"]
    assert "VIDEO BEING COMMENTED ON" not in match_call["content"]


def test_comment_content_bare_when_no_message_text():
    assert _comment_content(Comment("c", "u", "hi"), None) == "hi"
    assert _comment_content(Comment("c", "u", "hi"), Reel(reel_id="@d/1", caption="")) == "hi"
