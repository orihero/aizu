"""Reddit cascade — text-only relevance + comment matching, no vision tier.

Reddit is text-first in v1, so the cascade must NEVER call classify_image and must
use the submission (title+selftext) as comment context with a Reddit-shaped
scaffold ("SUBMISSION BEING COMMENTED ON"). Comments at any depth are scored the
same way (a deeply-nested reply is judged exactly like a top-level one).
"""
from reelradar.core.config import campaign_from_brief
from reelradar.core.feed import Comment, Reel
from reelradar.core.router import Decision
from reelradar.engines.reddit.cascade import RedditCascade, _comment_content


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
        is_lead = any(k in low for k in ("pricing", "+1", "demo", "price", "buy"))
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
    return campaign_from_brief("reddit-test", {
        "platform": "reddit", "threshold": 0.7,
        "relevance_def": "saas product submissions",
        "match_def": "a commenter asking to buy / price / contact",
        "extract_def": "- phone",
    })


def test_gate_submission_relevant_is_text_only():
    spy = SpyRouter()
    casc = RedditCascade(spy, _campaign())
    res = casc.gate_submission(
        Reel(reel_id="s1", caption="Acme app demo, free trial", author="dev"))
    assert res.relevant is True
    assert spy.image_calls == []                       # no vision tier on Reddit v1
    assert spy.text_calls[0]["stage"] == "relevance"


def test_gate_submission_irrelevant():
    spy = SpyRouter()
    res = RedditCascade(spy, _campaign()).gate_submission(
        Reel(reel_id="s2", caption="funny cat compilation", author="cats"))
    assert res.relevant is False
    assert spy.image_calls == []


def test_score_comment_match_coerces_to_declared_fields():
    spy = SpyRouter()
    casc = RedditCascade(spy, _campaign())
    reel = Reel(reel_id="s1", caption="Acme app demo", author="dev")
    res = casc.score_comment(
        Comment("s1/c1", "aziz", "How much is the Pro plan? pricing +1 415 555 0142"), reel)
    assert res.is_match is True
    # extract contract: exactly the declared keys (stray 'junk' dropped, no others)
    assert set(res.decision.extracted) == {"phone"}
    assert spy.image_calls == []


def test_score_deeply_nested_comment_matches_like_top_level():
    spy = SpyRouter()
    reel = Reel(reel_id="s1", caption="Acme app demo", author="dev")
    # is_reply=True marks a nested reply; it must score identically to a top-level one.
    res = RedditCascade(spy, _campaign()).score_comment(
        Comment("s1/c9", "deep", "price? buy +1", is_reply=True), reel)
    assert res.is_match is True


def test_score_comment_uses_submission_scaffold():
    spy = SpyRouter()
    reel = Reel(reel_id="s1", caption="Acme app demo", author="dev")
    RedditCascade(spy, _campaign()).score_comment(
        Comment("s1/c1", "aziz", "What is the pricing?"), reel)
    match_call = [c for c in spy.text_calls if c["stage"] == "match"][0]
    assert "SUBMISSION BEING COMMENTED ON" in match_call["content"]
    assert "VIDEO BEING COMMENTED ON" not in match_call["content"]


def test_comment_content_bare_when_no_caption():
    assert _comment_content(Comment("c", "u", "hello"), None) == "hello"
    assert _comment_content(Comment("c", "u", "hello"), Reel(reel_id="s", caption="")) == "hello"
