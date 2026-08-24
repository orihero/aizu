"""YouTube session — a standalone, text-only discovery loop.

Deliberately NOT the Instagram Session. The Data API is a stateless, deterministic
source of videos + comment threads, so this loop drops every Instagram-reel
mechanic: no vision/OCR, no like/follow engagement, no action-block or
empty-interception halts, no tired-feed already-seen ratio, no daytime guard, no
human-pacing reel budget or dwell. It walks the feed's videos, gates each on
title+description text, scores its comments, and persists matches under
``platform='youtube'`` via the shared Store.

The ``pacer`` argument is accepted for a uniform engine signature but is unused
(the API needs no human pacing). Halts do not occur on this platform, so the
summary's ``halt_reason`` is always None.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from ...core.feed import FeedSource, Reel
from ...core.logsetup import SUCCESS_LEVEL, get_logger
from ...core.matching import compute_found_by
from ...core.store import SessionCounters, Store
from ..base import session_crash_guard
from .cascade import YouTubeCascade
from .feed import YouTubeApiError

log = get_logger(__name__)

_LEVEL_MAP = {
    "debug": logging.DEBUG, "info": logging.INFO, "success": SUCCESS_LEVEL,
    "warn": logging.WARNING, "warning": logging.WARNING, "error": logging.ERROR,
}

# Flood safety-net for the live activity feed (mirrors the Instagram session).
MAX_SESSION_EVENTS = 500
WATCHLIST_TTL_DAYS = 10.0


class YouTubeSession:
    def __init__(self, *, store: Store, router, feed: FeedSource, soul, campaign,
                 session_id: Optional[str] = None, run_id: Optional[str] = None,
                 lead_target: Optional[int] = None):
        self.store = store
        self.router = router
        self.feed = feed
        self.soul = soul
        self.campaign = campaign
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.run_id = run_id
        self.lead_target = lead_target
        self.counters = SessionCounters()
        self.cascade = YouTubeCascade(router, campaign, session_id=self.session_id)
        self._event_seq = 0

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

    def _process_comments(self, reel: Reel) -> int:
        reel_id = reel.reel_id
        cid = self.campaign.campaign_id
        plat = self.campaign.platform
        cursor = self.store.get_cursor(cid, reel_id, platform=plat)
        comments, new_cursor = self.feed.fetch_comments(reel_id, cursor)
        if comments:
            self._emit("comments", "info", f"Scoring {len(comments)} comments",
                       {"videoId": reel_id, "count": len(comments)})
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
                           f"Match: @{comment.username} (score {d.score:.2f})",
                           {"username": comment.username, "score": d.score,
                            "tier": d.tier, "videoId": reel_id})
        self.store.set_cursor(cid, reel_id, new_cursor, platform=plat)
        if found:
            self.store.add_to_watchlist(cid, reel_id, ttl_days=WATCHLIST_TTL_DAYS,
            platform=plat)
        return found

    # ---- main loop ----
    def run(self) -> dict:
        cid = self.campaign.campaign_id
        plat = self.campaign.platform
        self.store.start_session(self.session_id, cid, platform=plat, run_id=self.run_id)
        # A generic exception (not the handled YouTubeApiError) would escape before
        # end_session runs, leaving the row stuck at 'running'. The guard closes it
        # as 'halted' with a crash reason; the after-loop end_session stays
        # authoritative on the normal / API-rate-limit paths.
        with session_crash_guard(self.store, self.session_id):
            return self._run_after_start(cid, plat)

    def _run_after_start(self, cid: str, plat: str) -> dict:
        self._emit("lifecycle", "info", f"Run started — campaign {cid} ({plat})",
                   {"campaignId": cid, "platform": plat})

        halt_reason: Optional[str] = None
        try:
            for reel in self.feed.walk():
                if self.lead_target is not None and self.counters.matches >= self.lead_target:
                    break
                if self.store.is_seen(cid, reel.reel_id, platform=plat):
                    self.counters.already_seen_skips += 1
                    self.counters.reels_seen += 1
                    self._flush()
                    continue
                self.counters.reels_seen += 1
                try:
                    gate = self.cascade.gate_video(reel)
                except Exception as e:  # noqa: BLE001 — auto-skip transient
                    self.store.mark_seen(cid, reel.reel_id, relevant=None,
                                         author=reel.author or None,
                                         caption=reel.caption or None,
                                         source=reel.source or None,
                                         author_id=reel.author_id or None,
                                         platform=plat)
                    self.store.raise_flag("parse_skip", "soft", f"video {reel.reel_id}: {e}",
                                          campaign_id=cid, session_id=self.session_id)
                    self._emit("feed_walk", "warn",
                               f"Skipped video {reel.reel_id} — parse error",
                               {"videoId": reel.reel_id})
                    self._flush()
                    continue
                self.store.mark_seen(cid, reel.reel_id, relevant=gate.relevant,
                                     author=reel.author or None,
                                     caption=reel.caption or None,
                                     source=reel.source or None,
                                     author_id=reel.author_id or None,
                                     platform=plat)
                if gate.relevant:
                    self.counters.relevance_passes += 1
                    self._emit("relevance", "success",
                               f"Relevant ✓ — {reel.author or reel.reel_id}",
                               {"videoId": reel.reel_id, "channel": reel.author})
                    found = self._process_comments(reel)
                    self.counters.matches += found
                    if found:
                        self._emit("comments", "success",
                                   f"{found} match(es) on video {reel.reel_id}",
                                   {"videoId": reel.reel_id, "found": found})
                self._flush()
        except YouTubeApiError as e:
            # Persistent rate-limit / quota (after retries): stop this run cleanly so
            # the back-to-back loop stops hammering the API. Leads already found are
            # persisted; the summary carries halt_reason so the CLI loop stops too.
            halt_reason = str(e)
            self.store.raise_flag("youtube_api", "halt", halt_reason,
                                  campaign_id=cid, session_id=self.session_id)
            self._emit("halt", "error", f"Halted: {halt_reason}", {"status": e.status})

        self._flush()
        self.store.prune_watchlist(cid)
        self.store.end_session(self.session_id,
                               "halted" if halt_reason else "completed", halt_reason)
        if halt_reason:
            self._emit("lifecycle", "error",
                       f"Run halted after {self.counters.matches} match(es) — {halt_reason}",
                       {"matches": self.counters.matches})
        else:
            self._emit("lifecycle", "success",
                       f"Run completed — {self.counters.matches} match(es), "
                       f"${self.counters.spend_usd:.4f}",
                       {"matches": self.counters.matches,
                        "spendUsd": self.counters.spend_usd})

        c = self.counters
        return {
            "session_id": self.session_id,
            "reels_seen": c.reels_seen,
            "relevance_passes": c.relevance_passes,
            "matches": c.matches,
            "escalations": c.escalations,
            "already_seen_skips": c.already_seen_skips,
            "spend_usd": c.spend_usd,
            "feed_health_flag": False,   # no algorithmic feed → never tires
            "likes": 0,                  # YouTube engine is read-only
            "follows": 0,
            "halt_reason": halt_reason,  # set on persistent API rate-limit / quota
            "halt_kind": None,           # API platform — never poisons the CDP browser
        }


def run_session(*, campaign, store, router, feed, soul, pacer,
                session_id=None, run_id=None, lead_target=None) -> dict:
    """Engine entrypoint (see ``engines.base.EngineProtocol``). ``pacer`` is
    accepted for signature uniformity and intentionally unused on YouTube."""
    return YouTubeSession(store=store, router=router, feed=feed, soul=soul,
                          campaign=campaign, session_id=session_id, run_id=run_id,
                          lead_target=lead_target).run()
