"""The decision cascade (PRD §6, §11).

Relevance gate:  caption text → on-screen-text (vision/OCR) → escalate-if-unsure → cloud.
Comment match:   local pre-filter → local scoring → escalate-if-unsure → cloud.

"Escalate-if-unsure" = the verdict's confidence falls inside the campaign's
escalate band, OR the score sits near the threshold. Escalation re-runs the same
call site (the router decides local vs cloud per tier; in this cloud-only build
both passes hit cloud, but the structure and counters are intact for when a
local tier is added in front).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Callable, Optional

from ...core.config import Campaign
from ...core.feed import Comment, Reel
from ...core.logsetup import get_logger
from ...core.matching import corroboration_needs_review, ground_extracted
from ...core.router import Decision, Router, env_flag

log = get_logger(__name__)

# Below this many word-chars (after dropping #tags/@mentions) the caption carries
# too little signal to judge on its own — look at the video instead.
THIN_CAPTION_MIN_CHARS = 6


def _is_thin_caption(caption: str) -> bool:
    """True when the caption is empty / emoji-only / hashtag-or-mention-only /
    too short to judge relevance from text — the case where on-screen text in
    the video is the real signal."""
    if not caption:
        return True
    stripped = re.sub(r"[#@]\w+", " ", caption)         # drop hashtag/mention tokens
    return len(re.findall(r"\w", stripped)) < THIN_CAPTION_MIN_CHARS


@dataclass
class RelevanceResult:
    relevant: bool
    decision: Decision
    used_vision: bool
    escalated: bool
    used_stt: bool = False
    used_video: bool = False


@dataclass
class MatchResult:
    is_match: bool
    decision: Decision
    escalated: bool
    # Optional corroboration GATE (gap #4; Campaign.require_corroboration, off by
    # default) — True when a comparison model disagreed with/was inconclusive
    # about the primary verdict. The session loop demotes the persisted match to
    # `needs_review` instead of a hard accept when this is set.
    needs_review: bool = False


def _video_instruction(campaign: Campaign, reel: Reel) -> str:
    """The video-analysis prompt: relevance definition + a frame roster (FRAME n @
    Xs) so the model can localize on-screen signal in time, plus the spoken
    transcript when the STT tier already produced one (soliq's fusion folds the
    transcript into the same call). Timestamps come from ``reel.frame_timestamps``
    (set by capture_video_frames); absent → a bare frame count."""
    ts = reel.frame_timestamps or []
    if ts:
        roster = ", ".join(f"FRAME {i + 1} @ {t / 1000:.1f}s"
                           for i, t in enumerate(ts))
    else:
        roster = "the provided frames"
    instr = (f"{campaign.relevance_def}\n\nRead the on-screen text and visuals "
             f"across these real video frames ({roster}) and judge relevance.")
    if reel.transcript:
        instr += f"\n\nSPOKEN TEXT (from audio):\n{reel.transcript}"
    return instr


def _comment_content(comment: Comment, reel: Optional[Reel]) -> str:
    """Match-stage content: the comment, plus the reel it replies to.

    The reel context (the advertiser's caption + on-screen text) is what lets the
    extractor fill the brief-defined fields when the commenter only writes
    "how much?" — the comment alone rarely states them.
    """
    if reel is None or not (reel.caption or reel.ocr_text or reel.transcript):
        return comment.text
    parts = ["REEL BEING COMMENTED ON (posted by the author/advertiser):"]
    if reel.author:
        parts.append(f"author: {reel.author}")
    if reel.caption:
        parts.append(f"caption: {reel.caption}")
    if reel.ocr_text:
        parts.append(f"on-screen text: {reel.ocr_text}")
    if reel.transcript:
        parts.append(f"spoken text: {reel.transcript}")
    parts.append(f"\nCOMMENT TO JUDGE:\n{comment.text}")
    return "\n".join(parts)


def _match_instruction(campaign: Campaign, fields: list[str]) -> str:
    """The match-stage brief: match definition + extract prose + an explicit
    output contract built from the declared fields.

    The prose alone doesn't bind the model — a tuned system prompt enumerating a
    different fixed set, or the generic fallback that names no fields, would
    otherwise decide the keys. Listing the exact keys here (last, and as a JSON
    skeleton) makes the campaign's Extract input authoritative for every campaign.
    """
    instr = f"{campaign.match_def}\n\nEXTRACT FIELDS:\n{campaign.extract_def}"
    if fields:
        skeleton = "{" + ", ".join(f'"{f}": null' for f in fields) + "}"
        instr += (
            "\n\nOUTPUT CONTRACT: the \"extracted\" object in your JSON reply MUST "
            "contain EXACTLY these keys and no others. Fill a key from the comment "
            "(or the reel context when it legitimately applies), else use null — "
            f"never omit a key and never add keys:\n{skeleton}")
    return instr


def _coerce_extracted(decision: Decision, fields: list[str]) -> Decision:
    """Force the verdict's `extracted` to exactly the declared field keys.

    Drops any stray keys the model invented and back-fills missing ones with
    null, so what's stored on the lead always matches the campaign's Extract
    contract. Returns a new Decision (never mutates). No-op when no fields are
    declared (extraction stays unconstrained — legacy behaviour)."""
    if not fields:
        return decision
    src = decision.extracted if isinstance(decision.extracted, dict) else {}
    return replace(decision, extracted={f: src.get(f) for f in fields})


def _unsure(decision: Decision, campaign: Campaign) -> bool:
    # A degraded verdict (cloud failed / malformed response) is the opposite of
    # confident — it means "we don't know", so it must escalate/retry rather
    # than be read as a confident reject that silently drops the reel/comment.
    if decision.tier == "degraded":
        return True
    lo, hi = campaign.escalate_band
    if lo <= decision.confidence <= hi:
        return True
    # also unsure if the score straddles the decision threshold by a hair
    return abs(decision.score - campaign.threshold) < 0.05


class Cascade:
    def __init__(self, router: Router, campaign: Campaign,
                 session_id: Optional[str] = None):
        self.router = router
        self.campaign = campaign
        self.session_id = session_id
        self.escalations = 0
        self.transcriptions = 0  # reels sent through Uzbek STT (Instagram-only, gated)
        self.video_analyses = 0  # reels sent through the video-analysis tier (gated)

    # ---- relevance gate ----
    def gate_reel(self, reel: Reel, frame_b64=None,
                  capture_fn: Optional[Callable[[], list[str]]] = None,
                  transcribe_fn: Optional[Callable[[], Optional[str]]] = None,
                  video_analyze_fn: Optional[Callable[[], list[str]]] = None,
                  ) -> RelevanceResult:
        """Caption text → on-screen text (vision) → Uzbek STT → escalate-if-unsure (PRD §6).

        Frames come from `frame_b64` (str or list, e.g. FakeFeed), `reel.on_screen_frames`,
        or a lazy `capture_fn()` invoked ONLY when vision is needed (so we don't
        screenshot every reel). When the caption is thin/empty, vision is the
        PRIMARY judgment — a no-text caption must not produce a confident reject.

        `transcribe_fn`, when supplied, is a lazy Uzbek-STT tier invoked ONLY when
        the campaign is gated on for it (`campaign.enable_stt` + "uz" in
        `language_mix`) AND the verdict is still unsure after caption+vision — same
        cost-gating discipline as `capture_fn`. Instagram-only: no other engine's
        session module ever passes this.

        `video_analyze_fn`, when supplied, is a lazy video-analysis/fusion tier
        (download + real-frame sampling + multi-frame vision) invoked ONLY when the
        campaign is gated on for it (`campaign.enable_video_analysis` + the
        AIZU_VIDEO_ANALYSIS_ENABLED global kill-switch) AND the verdict is STILL
        unsure after caption+vision+STT — the most expensive tier, so it runs last,
        right before cloud escalation, and folds in `reel.transcript` (from the STT
        tier) via the frame roster. Instagram-only.
        """
        cid = self.campaign.campaign_id

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
        used_stt = False
        used_video = False
        escalated = False

        if _is_thin_caption(reel.caption):
            # Thin caption → judge from the video frames if we have any.
            imgs = frames()
            if imgs:
                d = self.router.classify_image(
                    instruction=self.campaign.relevance_def, images_b64=imgs,
                    campaign_id=cid, stage="relevance", session_id=self.session_id,
                    system=self.campaign.vision_prompt)
                used_vision = True
            else:
                d = self.router.classify_text(
                    instruction=self.campaign.relevance_def, content=reel.caption,
                    campaign_id=cid, stage="relevance", session_id=self.session_id,
                    system=self.campaign.relevance_prompt)
        else:
            d = self.router.classify_text(
                instruction=self.campaign.relevance_def, content=reel.caption,
                campaign_id=cid, stage="relevance", session_id=self.session_id,
                system=self.campaign.relevance_prompt)
            # on-screen text via vision when the caption verdict is unsure
            if _unsure(d, self.campaign):
                imgs = frames()
                if imgs:
                    dv = self.router.classify_image(
                        instruction=self.campaign.relevance_def, images_b64=imgs,
                        campaign_id=cid, stage="relevance", session_id=self.session_id,
                        system=self.campaign.vision_prompt)
                    used_vision = True
                    if dv.confidence >= d.confidence:   # take stronger-confidence verdict
                        d = dv

        # Uzbek STT tier — gated per-campaign (strict AND, no empty-language_mix
        # passthrough) AND per-reel (only when caption+vision left the verdict
        # unsure, same cost-gating as the vision tier above).
        stt_gate_ok = (self.campaign.enable_stt
                       and "uz" in {str(x).lower() for x in self.campaign.language_mix})
        if stt_gate_ok and transcribe_fn is not None and _unsure(d, self.campaign):
            text = transcribe_fn()
            if text:
                reel.transcript = text
                self.transcriptions += 1
                used_stt = True
                dt = self.router.classify_text(
                    instruction=self.campaign.relevance_def,
                    content=f"{reel.caption}\n\nSPOKEN TEXT (from audio):\n{text}",
                    campaign_id=cid, stage="relevance", session_id=self.session_id,
                    system=self.campaign.relevance_prompt)
                if dt.confidence >= d.confidence:   # take stronger-confidence verdict
                    d = dt

        # Video-analysis tier — gated per-campaign (enable_video_analysis) AND the
        # global AIZU_VIDEO_ANALYSIS_ENABLED kill-switch AND per-reel (only when the
        # cheaper caption+vision+STT tiers left the verdict unsure). Most expensive
        # tier (download + ffmpeg + multi-frame vision), so it runs last.
        video_gate_ok = (self.campaign.enable_video_analysis
                         and env_flag("AIZU_VIDEO_ANALYSIS_ENABLED"))
        if video_gate_ok and video_analyze_fn is not None and _unsure(d, self.campaign):
            frames = video_analyze_fn() or []
            if frames:
                self.video_analyses += 1
                used_video = True
                dvf = self.router.classify_image(
                    instruction=_video_instruction(self.campaign, reel),
                    images_b64=frames, campaign_id=cid, stage="video_analysis",
                    session_id=self.session_id, system=self.campaign.vision_prompt)
                # Cache the structured extras + take the stronger-confidence verdict.
                if isinstance(dvf.extracted, dict) and dvf.extracted:
                    reel.video_analysis = dvf.extracted
                if dvf.confidence >= d.confidence:
                    d = dvf

        # escalate to cloud if still unsure (router routes the tier)
        if _unsure(d, self.campaign):
            self.escalations += 1
            escalated = True
            log.debug("relevance escalating reel=%s conf=%.2f score=%.2f",
                      reel.reel_id, d.confidence, d.score)
            d = self.router.classify_text(
                instruction=self.campaign.relevance_def,
                content=f"[ESCALATED]\nCAPTION:\n{reel.caption}",
                campaign_id=cid, stage="relevance", session_id=self.session_id,
                system=self.campaign.relevance_prompt)

        relevant = d.score >= 0.5 if d.label not in {"relevant", "irrelevant"} \
            else d.label == "relevant"
        log.debug("relevance verdict reel=%s relevant=%s label=%s score=%.2f "
                  "conf=%.2f vision=%s stt=%s video=%s escalated=%s", reel.reel_id,
                  relevant, d.label, d.score, d.confidence, used_vision, used_stt,
                  used_video, escalated)
        return RelevanceResult(relevant=relevant, decision=d, used_vision=used_vision,
                               escalated=escalated, used_stt=used_stt,
                               used_video=used_video)

    # ---- comment match scoring ----
    def score_comment(self, comment: Comment,
                      reel: Optional[Reel] = None) -> MatchResult:
        cid = self.campaign.campaign_id
        fields = self.campaign.extract_fields()
        instr = _match_instruction(self.campaign, fields)
        content = _comment_content(comment, reel)
        d = self.router.classify_text(
            instruction=instr, content=content,
            campaign_id=cid, stage="match", session_id=self.session_id,
            system=self.campaign.match_prompt)
        escalated = False
        if _unsure(d, self.campaign):
            self.escalations += 1
            escalated = True
            log.debug("match escalating comment=%s conf=%.2f score=%.2f",
                      comment.comment_id, d.confidence, d.score)
            d = self.router.classify_text(
                instruction=instr,
                content=f"[ESCALATED]\n{content}",
                campaign_id=cid, stage="match", session_id=self.session_id,
                system=self.campaign.match_prompt)
        d = _coerce_extracted(d, fields)
        # Deterministic grounding check (gap #4): a hallucinated phone/email that
        # never appears in the comment/reel text the classifier actually saw is
        # dropped rather than trusted. Always on — no extra model call, no flag.
        d = replace(d, extracted=ground_extracted(d.extracted, content))
        is_match = d.score >= self.campaign.threshold
        # Optional corroboration GATE (gap #4): campaign-flagged, off by default.
        # A comparison-model disagreement/inconclusive demotes the match to
        # needs_review instead of a hard accept; agreement (or no comparison data
        # at all) leaves is_match untouched.
        needs_review = (self.campaign.require_corroboration and is_match
                        and corroboration_needs_review(d.score, d.comparisons,
                                                       self.campaign.threshold))
        log.debug("match verdict comment=%s match=%s score=%.2f conf=%.2f escalated=%s "
                  "needs_review=%s", comment.comment_id, is_match, d.score, d.confidence,
                  escalated, needs_review)
        return MatchResult(is_match=is_match, decision=d, escalated=escalated,
                           needs_review=needs_review)
