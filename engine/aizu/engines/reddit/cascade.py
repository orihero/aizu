"""Reddit decision cascade — text-only relevance + comment matching.

Standalone (NOT the Instagram cascade): Reddit is text-first, so there is no
vision tier and ``classify_image`` is never called in v1. Relevance is judged on
the submission's title + selftext; a comment is matched with the submission as
context ("SUBMISSION BEING COMMENTED ON") and is scored identically whether it is
a top-level reply or a deeply-nested one. Escalate-if-unsure re-runs the text call.

Each engine owns its own scoring (the locked per-engine-scoring decision), so the
small escalate/coerce helpers are intentionally local rather than shared.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

from ...core.config import Campaign
from ...core.feed import Comment, Reel
from ...core.logsetup import get_logger
from ...core.router import Decision, Router
from .prompts import REDDIT_MATCH, REDDIT_RELEVANCE

log = get_logger(__name__)


@dataclass
class RelevanceResult:
    relevant: bool
    decision: Decision
    escalated: bool


@dataclass
class MatchResult:
    is_match: bool
    decision: Decision
    escalated: bool


def _comment_content(comment: Comment, reel: Optional[Reel]) -> str:
    """Match-stage content: the comment, plus the submission it replies to.

    The submission context (title + selftext) lets the extractor fill fields the
    bare comment omits — the commenter's intent is still judged from the comment.
    """
    if reel is None or not reel.caption:
        return comment.text
    parts = ["SUBMISSION BEING COMMENTED ON (posted by the author):"]
    if reel.author:
        parts.append(f"author: {reel.author}")
    if reel.caption:
        parts.append(f"title + selftext: {reel.caption}")
    parts.append(f"\nCOMMENT TO JUDGE:\n{comment.text}")
    return "\n".join(parts)


def _match_instruction(campaign: Campaign, fields: list[str]) -> str:
    """Match brief: match definition + extract prose + an explicit key contract."""
    instr = f"{campaign.match_def}\n\nEXTRACT FIELDS:\n{campaign.extract_def}"
    if fields:
        skeleton = "{" + ", ".join(f'"{f}": null' for f in fields) + "}"
        instr += (
            "\n\nOUTPUT CONTRACT: the \"extracted\" object in your JSON reply MUST "
            "contain EXACTLY these keys and no others. Fill a key from the comment "
            "(or the submission context when it legitimately applies), else use null "
            f"— never omit a key and never add keys:\n{skeleton}")
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


class RedditCascade:
    def __init__(self, router: Router, campaign: Campaign,
                 session_id: Optional[str] = None):
        self.router = router
        self.campaign = campaign
        self.session_id = session_id
        self.escalations = 0

    def gate_submission(self, reel: Reel) -> RelevanceResult:
        """Relevance on title + selftext (text only; vision deferred in v1)."""
        cid = self.campaign.campaign_id
        system = self.campaign.relevance_prompt or REDDIT_RELEVANCE
        d = self.router.classify_text(
            instruction=self.campaign.relevance_def, content=reel.caption,
            campaign_id=cid, stage="relevance", session_id=self.session_id,
            system=system)
        escalated = False
        if _unsure(d, self.campaign):
            self.escalations += 1
            escalated = True
            d = self.router.classify_text(
                instruction=self.campaign.relevance_def,
                content=f"[ESCALATED]\nTITLE+SELFTEXT:\n{reel.caption}",
                campaign_id=cid, stage="relevance", session_id=self.session_id,
                system=system)
        relevant = d.score >= 0.5 if d.label not in {"relevant", "irrelevant"} \
            else d.label == "relevant"
        log.debug("reddit relevance submission=%s relevant=%s score=%.2f conf=%.2f esc=%s",
                  reel.reel_id, relevant, d.score, d.confidence, escalated)
        return RelevanceResult(relevant=relevant, decision=d, escalated=escalated)

    def score_comment(self, comment: Comment,
                      reel: Optional[Reel] = None) -> MatchResult:
        cid = self.campaign.campaign_id
        fields = self.campaign.extract_fields()
        instr = _match_instruction(self.campaign, fields)
        content = _comment_content(comment, reel)
        system = self.campaign.match_prompt or REDDIT_MATCH
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
        log.debug("reddit match comment=%s match=%s score=%.2f esc=%s",
                  comment.comment_id, is_match, d.score, escalated)
        return MatchResult(is_match=is_match, decision=d, escalated=escalated)
