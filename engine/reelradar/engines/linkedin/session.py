"""LinkedIn session — a CDP, copy-first, read-only discovery loop.

Walks the warmed account's feed (plus seeded hashtags/people) behind the
FeedSource interface, gates each post on its copy (with a vision pass on
carousel/image text when the copy is thin), scores its comments, and persists
matches under ``platform='linkedin'`` via the shared Store.

Like Instagram it walks an algorithmic feed (so a run loops toward the lead
target across re-scrolls; it is NOT a single-pass deterministic source) and
enforces the three failure tiers — but it is READ-ONLY (no like/follow/react) and
the most conservatively paced of the family (PRD §10). Halts (daytime window
closed, checkpoint, empty-interception canary) raise ``HaltSession``; the
entrypoint folds them into ``halt_reason`` so the run-loop aggregation stays
uniform across engines.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from ..base import HaltSession, session_crash_guard
from .cascade import LinkedInCascade
from ...core.config import Campaign
from ...core.feed import FeedSource, Reel
from ...core.logsetup import SUCCESS_LEVEL, get_logger
from ...core.matching import compute_found_by
from ...core.pacing import Pacer
from ...core.router import Router
from ...core.store import SessionCounters, Store

log = get_logger(__name__)

_LEVEL_MAP = {
    "debug": logging.DEBUG, "info": logging.INFO, "success": SUCCESS_LEVEL,
    "warn": logging.WARNING, "warning": logging.WARNING, "error": logging.ERROR,
}

MAX_SESSION_EVENTS = 500

# FIX B: a healthy run scanning ordinary (irrelevant) posts emits no relevance/match
# event, so the panel's live feed goes quiet and its stall banner fires (frontend
# STALL_THRESHOLD_SEC=120) even though the walk is fine. Emit a time-throttled
# progress heartbeat on the common per-post path — comfortably under that threshold.
PROGRESS_EVENT_INTERVAL_SEC = 45.0


@dataclass
class SessionConfig:
    tired_feed_ratio: float = 0.6     # already-seen skips / posts seen → flag
    tired_feed_min_reels: int = 10    # don't judge the ratio on tiny samples
    empty_interception_halt: int = 5  # consecutive empty fetches → halt (canary)
    watchlist_ttl_days: float = 10.0
    # Outer wall-clock backstop for one post's processing block (open_reel + comment
    # fetch). On breach the post is SKIPPED (warn) and the walk continues — NEVER
    # halt. Behind the per-call Playwright timeouts as defense in depth.
    per_reel_seconds: float = 90.0


class LinkedInSession:
    def __init__(self, *, store: Store, router: Router, feed: FeedSource,
                 soul, campaign: Campaign, pacer: Optional[Pacer] = None,
                 cfg: Optional[SessionConfig] = None,
                 session_id: Optional[str] = None, run_id: Optional[str] = None,
                 lead_target: Optional[int] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.store = store
        self.router = router
        self.feed = feed
        self.soul = soul
        self.campaign = campaign
        self.pacer = pacer or Pacer()
        self.cfg = cfg or SessionConfig()
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.run_id = run_id
        self.lead_target = lead_target
        # Monotonic source of truth for the per-reel wall-clock backstop; injectable
        # so tests drive the deadline deterministically without real sleeping.
        self._clock = clock
        self._event_seq = 0
        # FIX B: wall-clock of the last scan-progress heartbeat (throttle anchor).
        # Reset to the run's start time in _run_after_start so the first heartbeat
        # fires ~PROGRESS_EVENT_INTERVAL_SEC in, not instantly after "Run started".
        self._last_progress_emit = 0.0
        self.counters = SessionCounters()
        self.cascade = LinkedInCascade(router, campaign, session_id=self.session_id)
        self._empty_streak = 0

    # ---- helpers ----
    def _flush(self) -> None:
        self.counters.escalations = self.cascade.escalations
        self.counters.spend_usd = self.store.total_spend(self.campaign.campaign_id)
        self.store.update_counters(self.session_id, self.counters)

    def _emit(self, phase: str, level: str, message: str,
              detail: Optional[dict] = None) -> None:
        ctx = (" · " + " ".join(
            f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}"
            for k, v in detail.items())) if detail else ""
        log.log(_LEVEL_MAP.get(level, logging.INFO), "[%s] %s%s", phase, message, ctx)
        if not self.run_id or self._event_seq >= MAX_SESSION_EVENTS:
            return
        self._event_seq += 1
        try:
            blob = json.dumps(detail, ensure_ascii=False) if detail else None
        except (TypeError, ValueError):
            blob = None
        self.store.emit_run_event(
            self.run_id, self._event_seq, phase, level, message,
            campaign_id=self.campaign.campaign_id, session_id=self.session_id,
            detail=blob)

    def _maybe_emit_progress(self) -> None:
        """FIX B: throttled scan-progress heartbeat on the COMMON per-post path (every
        post counted toward reels_seen, relevant or not). Keeps the panel's live feed
        warm during a long stretch of irrelevant posts so its stall banner never fires
        on a healthy run. Only meaningful for a panel-launched run (run_id set); a
        direct CLI/unit run skips it so it never consumes the injected test clock."""
        if not self.run_id:
            return
        now = self._clock()
        if now - self._last_progress_emit < PROGRESS_EVENT_INTERVAL_SEC:
            return
        self._last_progress_emit = now
        c = self.counters
        self._emit("feed_walk", "info",
                   f"Scanning — {c.reels_seen} posts seen, {c.relevance_passes} relevant",
                   {"postsSeen": c.reels_seen, "relevancePasses": c.relevance_passes,
                    "matches": c.matches})

    def _check_tired_feed(self) -> None:
        c = self.counters
        if c.reels_seen >= self.cfg.tired_feed_min_reels:
            ratio = c.already_seen_skips / max(1, c.reels_seen)
            if ratio >= self.cfg.tired_feed_ratio and not c.feed_health_flag:
                c.feed_health_flag = True
                self.store.raise_flag(
                    "feed_health", "soft",
                    f"already-seen ratio {ratio:.0%} ≥ {self.cfg.tired_feed_ratio:.0%}; "
                    "feed tapped out / drifting — follow more / widen hashtags",
                    campaign_id=self.campaign.campaign_id, session_id=self.session_id)
                self._emit("feed_walk", "warn", "Feed tapping out — re-steer suggested",
                           {"ratio": round(ratio, 2)})

    def _reel_deadline_breached(self, reel: Reel, started: float) -> bool:
        """True once one post's processing has exceeded per_reel_seconds. The outer
        wall-clock backstop behind the per-call Playwright timeouts. On breach it
        emits a warn step; the caller SKIPS the post and continues — NEVER halts."""
        if self._clock() - started <= self.cfg.per_reel_seconds:
            return False
        self._emit("feed_walk", "warn",
                   f"Reel {reel.reel_id} exceeded per-reel deadline — skipped",
                   {"postId": reel.reel_id})
        return True

    def _process_comments(self, reel: Reel) -> int:
        reel_id = reel.reel_id
        cid = self.campaign.campaign_id
        plat = self.campaign.platform
        cursor = self.store.get_cursor(cid, reel_id, platform=plat)
        comments, new_cursor = self.feed.fetch_comments(reel_id, cursor)

        # empty-interception canary (PRD §9 tier 3)
        if not self.feed.healthy():
            self._empty_streak += 1
            if self._empty_streak >= self.cfg.empty_interception_halt:
                raise HaltSession("empty interception for N posts — Voyager endpoint drift",
                                  kind="canary")
        else:
            self._empty_streak = 0

        if comments:
            self._emit("comments", "info", f"Scoring {len(comments)} comments",
                       {"postId": reel_id, "count": len(comments)})
        found = 0
        for comment in comments:
            try:
                res = self.cascade.score_comment(comment, reel)
            except Exception as e:  # noqa: BLE001 — auto-skip a single failure
                self.store.raise_flag("parse_skip", "soft",
                                      f"comment {comment.comment_id}: {e}",
                                      campaign_id=cid, session_id=self.session_id)
                log.debug("[comments] skipped comment %s — %s", comment.comment_id, e)
                continue
            self.counters.comments_scored += 1
            if res.is_match:
                d = res.decision
                self.store.upsert_match(
                    campaign_id=cid, reel_id=reel_id, comment_id=comment.comment_id,
                    username=comment.username, text=comment.text, lang=comment.lang,
                    score=d.score, reason=d.reason, extracted=d.extracted, tier=d.tier,
                    session_id=self.session_id, platform=plat,
                    found_by_models=compute_found_by(d.model, d.comparisons,
                                                     self.campaign.threshold))
                found += 1
                self._emit("comments", "success",
                           f"Match: {comment.username} (score {d.score:.2f})",
                           {"username": comment.username, "score": d.score,
                            "tier": d.tier, "postId": reel_id})
        self.store.set_cursor(cid, reel_id, new_cursor, platform=plat)
        if found:
            self.store.add_to_watchlist(cid, reel_id,
                                        ttl_days=self.cfg.watchlist_ttl_days,
                                        platform=plat)
        return found

    # ---- main loop ----
    def run(self) -> dict:
        cid = self.campaign.campaign_id
        plat = self.campaign.platform
        self.store.start_session(self.session_id, cid, platform=plat, run_id=self.run_id)
        # From here on an unexpected exception (e.g. Chrome/CDP closing mid-run)
        # would otherwise escape before any end_session call, leaving the row stuck
        # at 'running' forever. The guard closes it as 'halted' with a crash reason;
        # the normal-completion and HaltSession end_session calls stay authoritative.
        with session_crash_guard(self.store, self.session_id):
            return self._run_after_start(cid, plat)

    def _run_after_start(self, cid: str, plat: str) -> dict:
        self._emit("lifecycle", "info", f"Run started — campaign {cid} ({plat})",
                   {"campaignId": cid, "platform": plat})
        # FIX B: anchor the progress throttle at the run start so the first heartbeat
        # fires ~PROGRESS_EVENT_INTERVAL_SEC in, not instantly (no dup after "started").
        # Only for a panel-launched run (run_id) — matches _maybe_emit_progress so a
        # direct CLI/unit run never consumes an injected test clock.
        if self.run_id:
            self._last_progress_emit = self._clock()

        if not self.pacer.is_daytime():
            self.store.end_session(self.session_id, "halted", "outside daytime window")
            self._emit("halt", "error", "Halted: outside daytime window")
            raise HaltSession("outside daytime window", kind="daytime")

        halt_reason: Optional[str] = None
        try:
            for reel in self.feed.walk():
                if self.counters.reels_seen >= self.pacer.reel_budget:
                    break
                if self.lead_target is not None and self.counters.matches >= self.lead_target:
                    break

                if self.store.is_seen(cid, reel.reel_id, platform=plat):
                    self.counters.already_seen_skips += 1
                    self.counters.reels_seen += 1
                    self._maybe_emit_progress()
                    self._check_tired_feed()
                    self._flush()
                    continue

                self.counters.reels_seen += 1
                self._maybe_emit_progress()
                self.pacer.dwell()

                # Start the per-reel wall-clock BEFORE the gate/scoring so the whole
                # expensive block (vision, open_reel nav, comment fetch) is bounded.
                reel_start = self._clock()

                try:
                    gate = self.cascade.gate_post(
                        reel, capture_fn=lambda r=reel: self.feed.capture_frames(r))
                except Exception as e:  # noqa: BLE001 — auto-skip transient
                    self.store.mark_seen(cid, reel.reel_id, relevant=None,
                                         author=reel.author or None,
                                         caption=reel.caption or None, platform=plat)
                    self.store.raise_flag("parse_skip", "soft",
                                          f"post {reel.reel_id}: {e}",
                                          campaign_id=cid, session_id=self.session_id)
                    self._emit("feed_walk", "warn",
                               f"Skipped post {reel.reel_id} — parse error",
                               {"postId": reel.reel_id})
                    self._flush()
                    continue

                self.store.mark_seen(cid, reel.reel_id, relevant=gate.relevant,
                                     author=reel.author or None,
                                     caption=reel.caption or None,
                                     ocr_text=reel.ocr_text or None, platform=plat)
                # Wall-clock backstop before the expensive open_reel + comment block.
                if self._reel_deadline_breached(reel, reel_start):
                    self._flush()
                    self.pacer.between_reels()
                    continue

                if gate.relevant:
                    self.counters.relevance_passes += 1
                    self._emit("relevance", "success",
                               f"Relevant ✓ — {reel.author or reel.reel_id}",
                               {"postId": reel.reel_id, "author": reel.author})
                    # Open the post full-screen — the comment thread + its Voyager
                    # call only fire there. A permalink that doesn't resolve would
                    # attribute someone else's comments to this post.
                    if not self.feed.open_reel(reel):
                        self.store.raise_flag(
                            "post_unavailable", "soft",
                            f"post {reel.reel_id}: permalink did not open — skipped",
                            campaign_id=cid, session_id=self.session_id)
                        self._emit("feed_walk", "warn",
                                   f"Post {reel.reel_id} unavailable — skipped",
                                   {"postId": reel.reel_id})
                    else:
                        found = self._process_comments(reel)
                        self.counters.matches += found
                        if found > 0:
                            self._emit("comments", "success",
                                       f"{found} match(es) on post {reel.reel_id}",
                                       {"postId": reel.reel_id, "found": found})

                # Wall-clock backstop AFTER the block too: log a chronically slow post.
                self._reel_deadline_breached(reel, reel_start)

                self._check_tired_feed()
                self._flush()
                self.pacer.between_reels()

            self._flush()
            self.store.prune_watchlist(cid)
            self.store.end_session(self.session_id, "completed")
            self._emit("lifecycle", "success",
                       f"Run completed — {self.counters.matches} match(es), "
                       f"${self.counters.spend_usd:.4f}",
                       {"matches": self.counters.matches,
                        "spendUsd": self.counters.spend_usd})
        except HaltSession as h:
            halt_reason = h.reason
            self.store.raise_flag("halt", "halt", halt_reason,
                                  campaign_id=cid, session_id=self.session_id)
            self._emit("halt", "error", f"Halted: {halt_reason}", {"reason": halt_reason})
            self._flush()
            self.store.end_session(self.session_id, "halted", halt_reason)
            raise

        c = self.counters
        return {
            "session_id": self.session_id,
            "reels_seen": c.reels_seen,
            "relevance_passes": c.relevance_passes,
            "matches": c.matches,
            "escalations": c.escalations,
            "already_seen_skips": c.already_seen_skips,
            "spend_usd": c.spend_usd,
            "feed_health_flag": c.feed_health_flag,
            "likes": 0,                  # LinkedIn engine is read-only
            "follows": 0,
            "halt_reason": halt_reason,
            "halt_kind": None,           # success path never halts; halt re-raises
        }


def run_session(*, campaign, store, router, feed, soul, pacer,
                session_id=None, run_id=None, lead_target=None) -> dict:
    """Engine entrypoint (see ``engines.base.EngineProtocol``). A HaltSession is
    folded into the summary as ``halt_reason``/``halt_kind`` so the run-loop
    aggregation stays uniform across engines."""
    session = LinkedInSession(store=store, router=router, feed=feed, soul=soul,
                              campaign=campaign, pacer=pacer,
                              session_id=session_id, run_id=run_id,
                              lead_target=lead_target)
    try:
        return session.run()
    except HaltSession as h:
        return {"session_id": session.session_id, "halt_reason": h.reason,
                "halt_kind": h.kind}
