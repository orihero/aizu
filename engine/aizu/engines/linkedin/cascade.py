"""LinkedIn decision cascade — copy-first relevance + comment matching.

Relevance gate:  post copy → carousel/image on-screen text (vision/OCR, when copy
                 is thin) → escalate-if-unsure → cloud.
Comment match:   local pre-filter → local scoring → escalate-if-unsure → cloud.

Each engine owns its own scoring (the locked per-engine-scoring decision), so the
small escalate/coerce/thin-copy helpers are intentionally local rather than shared.
The structure mirrors Instagram's vision-capable cascade; the difference is that a
campaign that omits its prompts falls back to LinkedIn-shaped defaults.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Callable, Optional

from ...core.config import Campaign
from ...core.feed import Comment, Reel
from ...core.logsetup import get_logger
from ...core.router import Decision, Router
from .prompts import LINKEDIN_MATCH, LINKEDIN_RELEVANCE

log = get_logger(__name__)

# Below this many word-chars the copy carries too little signal to judge on its
# own — read the attached carousel/document/image instead.
THIN_COPY_MIN_CHARS = 6


def _is_thin_copy(copy: str) -> bool:
    if not copy:
        return True
    stripped = re.sub(r"[#@]\w+", " ", copy)            # drop hashtag/mention tokens
    return len(re.findall(r"\w", stripped)) < THIN_COPY_MIN_CHARS


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
    """Match-stage content: the comment, plus the post it replies to (context for
    extraction). Intent is still judged from the comment."""
    if reel is None or not (reel.caption or reel.ocr_text):
        return comment.text
    parts = ["POST BEING COMMENTED ON (authored by):"]
    if reel.author:
        parts.append(f"author: {reel.author}")
    if reel.caption:
        parts.append(f"copy: {reel.caption}")
    if reel.ocr_text:
        parts.append(f"on-screen text: {reel.ocr_text}")
    parts.append(f"\nCOMMENT TO JUDGE:\n{comment.text}")
    return "\n".join(parts)


def _match_instruction(campaign: Campaign, fields: list[str]) -> str:
    instr = f"{campaign.match_def}\n\nEXTRACT FIELDS:\n{campaign.extract_def}"
    if fields:
        skeleton = "{" + ", ".join(f'"{f}": null' for f in fields) + "}"
        instr += (
            "\n\nOUTPUT CONTRACT: the \"extracted\" object in your JSON reply MUST "
            "contain EXACTLY these keys and no others. Fill a key from the comment "
            "(or the post context when it legitimately applies), else use null — "
            f"never omit a key and never add keys:\n{skeleton}")
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


class LinkedInCascade:
    def __init__(self, router: Router, campaign: Campaign,
                 session_id: Optional[str] = None):
        self.router = router
        self.campaign = campaign
        self.session_id = session_id
        self.escalations = 0

    # ---- relevance gate ----
    def gate_post(self, reel: Reel, frame_b64=None,
                  capture_fn: Optional[Callable[[], list[str]]] = None) -> RelevanceResult:
        """Post copy → carousel/image text (vision) → escalate-if-unsure (PRD §6).

        Frames come from ``frame_b64`` (str/list), ``reel.on_screen_frames``, or a
        lazy ``capture_fn()`` invoked ONLY when vision is needed. When the copy is
        thin/empty, vision is the PRIMARY judgment — a no-text post must not produce
        a confident reject."""
        cid = self.campaign.campaign_id
        relevance_system = self.campaign.relevance_prompt or LINKEDIN_RELEVANCE
        vision_system = self.campaign.vision_prompt or LINKEDIN_RELEVANCE

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

        if _is_thin_copy(reel.caption):
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
                content=f"[ESCALATED]\nCOPY:\n{reel.caption}",
                campaign_id=cid, stage="relevance", session_id=self.session_id,
                system=relevance_system)

        relevant = d.score >= 0.5 if d.label not in {"relevant", "irrelevant"} \
            else d.label == "relevant"
        log.debug("linkedin relevance post=%s relevant=%s score=%.2f conf=%.2f "
                  "vision=%s esc=%s", reel.reel_id, relevant, d.score, d.confidence,
                  used_vision, escalated)
        return RelevanceResult(relevant=relevant, decision=d,
                               used_vision=used_vision, escalated=escalated)

    # ---- comment match scoring ----
    def score_comment(self, comment: Comment,
                      reel: Optional[Reel] = None) -> MatchResult:
        cid = self.campaign.campaign_id
        fields = self.campaign.extract_fields()
        instr = _match_instruction(self.campaign, fields)
        content = _comment_content(comment, reel)
        system = self.campaign.match_prompt or LINKEDIN_MATCH
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
        log.debug("linkedin match comment=%s match=%s score=%.2f esc=%s",
                  comment.comment_id, is_match, d.score, escalated)
        return MatchResult(is_match=is_match, decision=d, escalated=escalated)
