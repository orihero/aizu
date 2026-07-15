"""Deterministic offline router for tests and dry runs.

No network. Decides via simple keyword heuristics so the full loop can run
end-to-end without an API key or a live Instagram session. Confidence is set
to land some cases inside the escalate band so escalation paths are exercised.

The heuristics mirror the SHIPPED DEFAULT (SaaS) campaign — they are an offline
stand-in, not the source of truth. A live run uses the real model + campaign
brief; this only needs to be plausible enough to exercise the loop.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .router import Decision

# Generic international phone: a leading "+" and 8+ digits, or a long digit run.
_PHONE = re.compile(r"(\+?\d[\d\s\-]{7,}\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_INTENT_WORDS = {
    "buy": ["buy", "purchase", "sign up", "signup"],
    "subscribe": ["subscribe", "subscription"],
    "trial": ["trial", "free trial", "try it"],
    "demo": ["demo", "walkthrough", "book a"],
    "pricing": ["price", "pricing", "cost", "plan", "plans", "how much", "quote", "per seat"],
    "inquire": ["integrate", "integration", "feature", "support", "onboard", "does it", "can it"],
}
_RELEVANCE_WORDS = ["app", "saas", "software", "tool", "dashboard", "integration",
                    "pricing", "plan", "trial", "demo", "feature", "workflow",
                    "productivity", "subscription", "platform", "onboarding"]

# Mirrors the production prompt's sourcing rules: the comment segment is the only
# legitimate source for the lead's contact details and intent (a phone/email in
# the reel-context block is the company's own, never the lead's).
_COMMENT_MARKER = "COMMENT TO JUDGE:"


def _hits(text: str, words: list[str]) -> int:
    t = text.lower()
    return sum(1 for w in words if w in t)


class MockRouter:
    def __init__(self, store=None):
        self.store = store

    def classify_text(self, *, instruction: str, content: str, campaign_id: str,
                      stage: str, session_id: Optional[str] = None,
                      system: Optional[str] = None,
                      threshold: Optional[float] = None) -> Decision:
        # `threshold` is accepted for signature parity with OpenRouterRouter (it
        # drives model-comparison agreement logging there) — unused offline.
        if stage == "relevance":
            n = _hits(content, _RELEVANCE_WORDS)
            score = min(1.0, 0.25 * n)
            conf = 0.9 if n >= 2 or n == 0 else 0.55  # 1 hit => unsure band
            label = "relevant" if score >= 0.5 else "irrelevant"
            return Decision(label=label, score=score, confidence=conf,
                            reason=f"{n} relevance cue(s)", tier="local")
        # match scoring — intent + contact judged on the COMMENT segment only.
        comment_part = content.split(_COMMENT_MARKER)[-1]
        intent = None
        for tag, words in _INTENT_WORDS.items():
            if _hits(comment_part, words):
                intent = tag
                break
        phone_m = _PHONE.search(comment_part)
        email_m = _EMAIL.search(comment_part)
        score = 0.0
        if intent:
            score += 0.55
        if phone_m or email_m:
            score += 0.35
        if _hits(content, _RELEVANCE_WORDS):
            score += 0.15
        score = min(1.0, score)
        conf = 0.85 if (intent and (phone_m or email_m)) or score == 0 else 0.55
        extracted: dict[str, Any] = {}
        if score > 0:
            extracted = {
                "phone": phone_m.group(0).strip() if phone_m else None,
                "email": email_m.group(0).strip().lower() if email_m else None,
                "intent": intent or "inquire",
            }
        label = "match" if score >= 0.5 else "no"
        return Decision(label=label, score=score, confidence=conf,
                        reason=f"intent={intent}, phone={bool(phone_m)}, email={bool(email_m)}",
                        extracted=extracted, tier="local")

    def classify_image(self, *, instruction: str, images_b64: list[str],
                       campaign_id: str, stage: str, session_id: Optional[str] = None,
                       system: Optional[str] = None) -> Decision:
        # Offline: pretend the frame text matched moderately.
        return Decision(label="relevant", score=0.6, confidence=0.7,
                        reason="mock OCR", tier="local")

    def transcribe(self, *, audio_path: str, campaign_id: str,
                   session_id: Optional[str] = None) -> Decision:
        raise NotImplementedError("v2")
