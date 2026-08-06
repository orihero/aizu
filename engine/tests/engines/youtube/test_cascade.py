"""YouTube cascade — text-only relevance + comment matching, no vision tier.

The Data API exposes no frames, so the cascade must NEVER call classify_image and
must use the video (title+description) as comment context with a YouTube-shaped
scaffold ("VIDEO BEING COMMENTED ON").
"""
from aizu.core.config import campaign_from_brief
from aizu.core.feed import Comment, Reel
from aizu.core.router import Decision
from aizu.engines.youtube.cascade import YouTubeCascade, _comment_content


class SpyRouter:
    """Records calls and returns scripted, escalation-free Decisions."""

    def __init__(self):
        self.text_calls = []
        self.image_calls = []

    def classify_text(self, *, instruction, content, campaign_id, stage,
                      session_id=None, system=None):
        self.text_calls.append({"stage": stage, "content": content, "system": system})
        low = content.lower()
        if stage == "relevance":
            relevant = any(k in low for k in ("acme", "app", "saas", "demo"))
            return Decision(label="relevant" if relevant else "irrelevant",
                            score=0.9 if relevant else 0.1, confidence=0.96)
        is_lead = any(k in low for k in ("pricing", "+1", "demo", "buy"))
        return Decision(label="yes" if is_lead else "no",
                        score=0.92 if is_lead else 0.1, confidence=0.96,
                        extracted={"phone": "+14155550142", "junk": "x"} if is_lead else {})

    def classify_image(self, *, instruction, images_b64, campaign_id, stage,
                       session_id=None, system=None):
        # Recorded (never raises) so a violation is caught by an explicit assert,
        # not masked by the session's parse-error auto-skip.
        self.image_calls.append({"stage": stage})
        return Decision(label="irrelevant", score=0.0, confidence=1.0)


def _campaign():
    return campaign_from_brief("yt-test", {
        "platform": "youtube", "threshold": 0.7,
        "relevance_def": "saas product videos",
        "match_def": "a commenter asking to buy / pricing / contact",
        "extract_def": "- phone",
    })


def test_gate_video_relevant_is_text_only():
    spy = SpyRouter()
    casc = YouTubeCascade(spy, _campaign())
    res = casc.gate_video(Reel(reel_id="v1", caption="Acme app demo, free trial",
                               author="Acme Inc."))
    assert res.relevant is True
    assert spy.image_calls == []                       # no vision tier on YouTube
    assert spy.text_calls[0]["stage"] == "relevance"


def test_gate_video_irrelevant():
    spy = SpyRouter()
    res = YouTubeCascade(spy, _campaign()).gate_video(
        Reel(reel_id="v2", caption="funny cat compilation", author="cats"))
    assert res.relevant is False
    assert spy.image_calls == []


def test_score_comment_match_coerces_to_declared_fields():
    spy = SpyRouter()
    casc = YouTubeCascade(spy, _campaign())
    reel = Reel(reel_id="v1", caption="Acme app demo", author="Acme Inc.")
    res = casc.score_comment(Comment("v1/c1", "aziz", "pricing? +1 415 555 0142"), reel)
    assert res.is_match is True
    # extract contract: exactly the declared keys (stray 'junk' dropped, no others)
    assert set(res.decision.extracted) == {"phone"}
    assert spy.image_calls == []


def test_score_comment_uses_video_scaffold():
    spy = SpyRouter()
    reel = Reel(reel_id="v1", caption="Acme app demo", author="Acme Inc.")
    YouTubeCascade(spy, _campaign()).score_comment(Comment("v1/c1", "aziz", "pricing?"), reel)
    match_call = [c for c in spy.text_calls if c["stage"] == "match"][0]
    assert "VIDEO BEING COMMENTED ON" in match_call["content"]
    assert "REEL BEING COMMENTED ON" not in match_call["content"]


def test_comment_content_bare_when_no_caption():
    assert _comment_content(Comment("c", "u", "hello"), None) == "hello"
    assert _comment_content(Comment("c", "u", "hello"), Reel(reel_id="v", caption="")) == "hello"
