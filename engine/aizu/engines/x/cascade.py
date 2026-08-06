"""X (Twitter) decision cascade — text-first relevance + reply/quote matching.

Relevance gate:  post text → on-screen text (vision/OCR, image/video posts only) →
                 escalate-if-unsure → cloud. A text-only tweet is judged on text alone.
Comment match:   local pre-filter → local scoring → escalate-if-unsure → cloud.
                 Replies and quote-posts are scored identically (both are Comments).

Each engine owns its own scoring (the locked per-engine-scoring decision), so the
escalate/coerce/thin-text helpers are intentionally local. The structure mirrors
the other CDP cascades; X-specific differences are the prompts and that the two
match surfaces share one scoring path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Callable, Optional

from ...core.config import Campaign
from ...core.feed import Comment, Reel
from ...core.logsetup import get_logger
from ...core.router import Decision, Router
from .prompts import X_MATCH, X_RELEVANCE

log = get_logger(__name__)

# Below this many word-chars (after dropping #tags/@mentions) the post text carries
# too little signal — look at the image/video's on-screen text instead.
THIN_TEXT_MIN_CHARS = 6


def _is_thin_text(text: str) -> bool:
    if not text:
        return True
    stripped = re.sub(r"[#@]\w+", " ", text)
    return len(re.findall(r"\w", stripped)) < THIN_TEXT_MIN_CHARS


@dataclass
class RelevanceResult:
    relevant: bool
    decision: Decision
    used_vision: bool
    escalated: bool


@dataclass
class MatchResult:
    is_match: bool
    decision: Decision
    escalated: bool


def _comment_content(comment: Comment, reel: Optional[Reel]) -> str:
    """Match-stage content: the reply/quote, plus the post it responds to (context
    for extraction). Intent is still judged from the reply/quote itself."""
    if reel is None or not (reel.caption or reel.ocr_text):
        return comment.text
    parts = ["POST BEING COMMENTED ON (authored by):"]
    if reel.author:
        parts.append(f"author: @{reel.author}")
    if reel.caption:
        parts.append(f"text: {reel.caption}")
    if reel.ocr_text:
        parts.append(f"on-screen text: {reel.ocr_text}")
    surface = "quote-post" if not comment.is_reply else "reply"
    parts.append(f"\n{surface.upper()} TO JUDGE:\n{comment.text}")
    return "\n".join(parts)


def _match_instruction(campaign: Campaign, fields: list[str]) -> str:
    instr = f"{campaign.match_def}\n\nEXTRACT FIELDS:\n{campaign.extract_def}"
    if fields:
        skeleton = "{" + ", ".join(f'"{f}": null' for f in fields) + "}"
        instr += (
            "\n\nOUTPUT CONTRACT: the \"extracted\" object in your JSON reply MUST "
            "contain EXACTLY these keys and no others. Fill a key from the "
            "reply/quote (or the post context when it legitimately applies), else use "
            f"null — never omit a key and never add keys:\n{skeleton}")
    return instr


def _coerce_extracted(decision: Decision, fields: list[str]) -> Decision:
    """Force ``extracted`` to exactly the declared keys (new Decision, no mutation)."""
    if not fields:
        return decision
    src = decision.extracted if isinstance(decision.extracted, dict) else {}
    return replace(decision, extracted={f: src.get(f) for f in fields})


def _unsure(decision: Decision, campaign: Campaign) -> bool:
    if decision.tier == "degraded":
        return True
    lo, hi = campaign.escalate_band
    if lo <= decision.confidence <= hi:
        return True
    return abs(decision.score - campaign.threshold) < 0.05


class XCascade:
    def __init__(self, router: Router, campaign: Campaign,
                 session_id: Optional[str] = None):
        self.router = router
        self.campaign = campaign
        self.session_id = session_id
        self.escalations = 0

    # ---- relevance gate ----
    def gate_post(self, reel: Reel, frame_b64=None,
                  capture_fn: Optional[Callable[[], list[str]]] = None) -> RelevanceResult:
        """Post text → on-screen text (vision, image/video only) → escalate-if-unsure.

        A text-only tweet is judged on its text alone; vision runs only when the
        text is thin AND frames are available (i.e. an image/video post)."""
        cid = self.campaign.campaign_id
        relevance_system = self.campaign.relevance_prompt or X_RELEVANCE
        vision_system = self.campaign.vision_prompt or X_RELEVANCE

        def frames() -> list[str]:
            if frame_b64:
                return [frame_b64] if isinstance(frame_b64, str) else list(frame_b64)
            if reel.on_screen_frames:
                return list(reel.on_screen_frames)
            if capture_fn:
                got = capture_fn() or []
                return [got] if isinstance(got, str) else list(got)
            return []

        used_vision = False
        escalated = False

        if _is_thin_text(reel.caption):
            imgs = frames()
            if imgs:
                d = self.router.classify_image(
                    instruction=self.campaign.relevance_def, images_b64=imgs,
                    campaign_id=cid, stage="relevance", session_id=self.session_id,
                    system=vision_system)
                used_vision = True
            else:
                d = self.router.classify_text(
                    instruction=self.campaign.relevance_def, content=reel.caption,
                    campaign_id=cid, stage="relevance", session_id=self.session_id,
                    system=relevance_system)
        else:
            d = self.router.classify_text(
                instruction=self.campaign.relevance_def, content=reel.caption,
                campaign_id=cid, stage="relevance", session_id=self.session_id,
                system=relevance_system)
            if _unsure(d, self.campaign):
                imgs = frames()
                if imgs:
                    dv = self.router.classify_image(
                        instruction=self.campaign.relevance_def, images_b64=imgs,
                        campaign_id=cid, stage="relevance", session_id=self.session_id,
                        system=vision_system)
                    used_vision = True
                    if dv.confidence >= d.confidence:
                        d = dv

        if _unsure(d, self.campaign):
            self.escalations += 1
            escalated = True
            d = self.router.classify_text(
                instruction=self.campaign.relevance_def,
                content=f"[ESCALATED]\nTEXT:\n{reel.caption}",
                campaign_id=cid, stage="relevance", session_id=self.session_id,
                system=relevance_system)

        relevant = d.score >= 0.5 if d.label not in {"relevant", "irrelevant"} \
            else d.label == "relevant"
        log.debug("x relevance post=%s relevant=%s score=%.2f conf=%.2f vision=%s esc=%s",
                  reel.reel_id, relevant, d.score, d.confidence, used_vision, escalated)
        return RelevanceResult(relevant=relevant, decision=d,
                               used_vision=used_vision, escalated=escalated)

    # ---- match scoring (replies AND quote-posts, identical path) ----
    def score_comment(self, comment: Comment,
                      reel: Optional[Reel] = None) -> MatchResult:
        cid = self.campaign.campaign_id
        fields = self.campaign.extract_fields()
        instr = _match_instruction(self.campaign, fields)
        content = _comment_content(comment, reel)
        system = self.campaign.match_prompt or X_MATCH
        d = self.router.classify_text(
            instruction=instr, content=content, campaign_id=cid, stage="match",
            session_id=self.session_id, system=system)
        escalated = False
        if _unsure(d, self.campaign):
            self.escalations += 1
            escalated = True
            d = self.router.classify_text(
                instruction=instr, content=f"[ESCALATED]\n{content}",
                campaign_id=cid, stage="match", session_id=self.session_id,
                system=system)
        d = _coerce_extracted(d, fields)
        is_match = d.score >= self.campaign.threshold
        log.debug("x match comment=%s match=%s score=%.2f reply=%s esc=%s",
                  comment.comment_id, is_match, d.score, comment.is_reply, escalated)
        return MatchResult(is_match=is_match, decision=d, escalated=escalated)
