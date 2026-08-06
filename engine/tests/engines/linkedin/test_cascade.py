"""LinkedInCascade — copy-first relevance (vision when thin) + comment matching."""
from aizu.core.config import campaign_from_brief
from aizu.core.feed import Comment, Reel
from aizu.core.router import Decision
from aizu.engines.linkedin.cascade import LinkedInCascade


class SpyRouter:
    def __init__(self):
        self.text_calls = []
        self.image_calls = []

    def classify_text(self, *, instruction, content, campaign_id, stage,
                      session_id=None, system=None):
        self.text_calls.append(stage)
        low = content.lower()
        if stage == "relevance":
            rel = any(k in low for k in ("hiring", "cfo", "vacancy"))
            return Decision(label="relevant" if rel else "irrelevant",
                            score=0.9 if rel else 0.1, confidence=0.96)
        lead = any(k in low for k in ("rate", "price", "+1", "interested"))
        return Decision(label="yes" if lead else "no",
                        score=0.92 if lead else 0.1, confidence=0.96,
                        extracted={"phone": "+14155550142", "junk": "x"} if lead else {})

    def classify_image(self, *, instruction, images_b64, campaign_id, stage,
                       session_id=None, system=None):
        self.image_calls.append(stage)
        return Decision(label="relevant", score=0.88, confidence=0.95)


def _campaign():
    return campaign_from_brief("li-leadgen", {
        "platform": "linkedin", "threshold": 0.7,
        "relevance_def": "posts hiring or seeking services",
        "match_def": "a commenter asking price / showing intent",
        "extract_def": "- phone"})


def test_relevant_post_passes_on_copy():
    spy = SpyRouter()
    casc = LinkedInCascade(spy, _campaign())
    res = casc.gate_post(Reel(reel_id="a", caption="We are hiring a CFO", author="Acme"))
    assert res.relevant is True
    assert res.used_vision is False
    assert spy.image_calls == []


def test_thin_copy_falls_back_to_vision():
    spy = SpyRouter()
    casc = LinkedInCascade(spy, _campaign())
    reel = Reel(reel_id="b", caption="", author="Acme", on_screen_frames=["b64frame"])
    res = casc.gate_post(reel)
    assert res.used_vision is True
    assert spy.image_calls == ["relevance"]
    assert res.relevant is True


def test_match_coerces_extracted_to_declared_fields():
    spy = SpyRouter()
    casc = LinkedInCascade(spy, _campaign())
    reel = Reel(reel_id="a", caption="We are hiring a CFO", author="Acme")
    res = casc.score_comment(Comment(comment_id="c1", username="Aziz",
                                     text="Interested — what's the rate?"), reel)
    assert res.is_match is True
    assert res.decision.extracted == {"phone": "+14155550142"}   # 'junk' dropped


def test_noise_comment_is_not_a_match():
    spy = SpyRouter()
    casc = LinkedInCascade(spy, _campaign())
    res = casc.score_comment(Comment(comment_id="c2", username="Bot", text="Congrats! 🎉"))
    assert res.is_match is False
