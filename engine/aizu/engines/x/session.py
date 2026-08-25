"""X (Twitter) session — a CDP, text-first, read-only discovery loop.

Walks the warmed account's For You feed + Search + Lists behind the FeedSource
interface, gates each post on its text (vision pass on image/video posts when the
text is thin), scores its replies AND quote-posts, and persists matches under
``platform='x'`` via the shared Store.

Like Instagram it walks an algorithmic feed (so a run loops toward the lead target
across re-scrolls; NOT a single-pass deterministic source) and enforces the three
failure tiers — but it is READ-ONLY and the most conservatively paced of the family
(PRD §10). Two X-specific spine pieces:

  - **read-budget** — cumulative post views vs the account's daily read-view
    ceiling; soft-flag and stop BEFORE the hard "rate limit exceeded" lockout (which
    locks the account ~24h). v1 enforces a conservative per-session view cap.
  - **quote-vs-reply** — each match records its surface in ``extracted["surface"]``
    ("reply"|"quote") so a quote-post lead is captured exactly like a replier with
    no new column (PRD §5, §8).

Halts (daytime window closed, Arkose/checkpoint, empty-interception canary) raise
``HaltSession``; the entrypoint folds them into ``halt_reason``.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from ..base import HaltSession, session_crash_guard
from .cascade import XCascade
from ...core.config import Campaign
from ...core.feed import FeedSource, Reel
from ...core.logsetup import SUCCESS_LEVEL, get_logger
from ...core.matching import compute_found_by, derive_intent
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

# FIX P0: stable, cross-engine flag kind for a post the cascade judged RELEVANT that
# never reached reply/quote scoring. store.mark_seen has already run by then and
# seen_reels has NO TTL (store.is_seen is a bare existence check and nothing in
# aizu/ ever DELETEs from it), so such a post is blacklisted for that campaign
# forever — a silent, permanent lead loss. The 2026-08-19 live Instagram shakedown
# reported status=completed / matches=0 / ZERO health flags while having discarded a
# confirmed on-campaign reel; this engine runs the same loop. Keep the string
# identical to the instagram and linkedin engines so one panel/ops query covers all
# three.
RELEVANT_REEL_DISCARDED_FLAG = "relevant_reel_discarded"


class _HeartbeatRouter:
    """Router facade that bumps the session heartbeat every time a model call
    RETURNS — i.e. on every real verdict, success or failure.

    Why a facade rather than a hook inside the cascade: ONE ``gate_post`` chains up
    to three model calls (copy → vision → escalation) before it returns a single
    verdict, and ``score_comment`` chains up to two. Bumping only where the cascade
    returns therefore multiplies whatever budget the router allows one call by
    three — and on 2026-08-20 that product killed the fleet's first real campaign
    (job-2099fb29e88b) five times over with "stalled: no activity for over 180s"
    (session_watchdog.STALL_TIMEOUT_SEC). Tapping the router bounds the gap to ONE
    bounded model call, and this engine's private cascade copy need not learn that
    a watchdog exists. Kept identical in the instagram and linkedin engines.

    This is a PROGRESS signal, not a liveness ticker: nothing fires unless a model
    call actually completed, so a session wedged inside a call still goes quiet and
    is still halted. ``finally`` (not a post-return bump) so a call that burned its
    whole budget and then raised still counts the wall-clock it spent — the loop
    survives that case and moves on to the next comment.
    """

    # The facade's OWN state. Everything else — reads and WRITES — belongs to the
    # wrapped router.
    _OWN_ATTRS = ("_router", "_on_verdict")

    def __init__(self, router, on_verdict: Callable[[], None]):
        self._router = router
        self._on_verdict = on_verdict

    def __getattr__(self, name):        # everything else passes straight through
        return getattr(self._router, name)

    def __setattr__(self, name, value):
        """Writes pass through too, or the facade silently swallows configuration.

        A read-only proxy is a trap here: `setattr(router, "x", v)` on the facade
        binds `x` on the FACADE, while every router method still reads `self.x` on
        the WRAPPED object and sees the old value. The write appears to work — the
        caller can read it straight back — and the effect never reaches the code
        that matters. Measured: setting `default_threshold` through the facade left
        the real router's at None, so `_classify_text_with_comparison` would have
        gone on writing NULL into `model_comparison_log.agreed`, which is the exact
        bug that setting it exists to fix. This repo has SEVEN recorded sightings of
        a fix that was correct where it was written and inert where it was read
        (ledger B4, E7, F10a, F10b, A11, A12, and the CDP wedge attribution); a
        write-swallowing proxy in the middle of the router is how you get an eighth.
        """
        if name in self._OWN_ATTRS:
            object.__setattr__(self, name, value)
        else:
            setattr(self._router, name, value)

    def classify_text(self, *args, **kwargs):
        try:
            return self._router.classify_text(*args, **kwargs)
        finally:
            self._on_verdict()

    def classify_image(self, *args, **kwargs):
        try:
            return self._router.classify_image(*args, **kwargs)
        finally:
            self._on_verdict()


@dataclass
class SessionConfig:
    tired_feed_ratio: float = 0.6
    tired_feed_min_reels: int = 10
    empty_interception_halt: int = 5
    watchlist_ttl_days: float = 10.0
    # Read-budget (PRD §7, §10): stop well before the daily read-view lockout.
    # Conservative per-session view cap; the true daily ceiling spans sessions and
    # is a store-backed refinement.
    read_view_soft_cap: int = 500
    # Outer wall-clock backstop for the BROWSER block only (open_reel + reply/quote
    # fetch/scoring); anchored AFTER the cascade gate so classification time is not
    # charged to it (see the re-anchor in _run_after_start). On breach the remaining
    # browser work is SKIPPED (warn) and the walk continues — NEVER halt. Behind the
    # per-call Playwright timeouts as defense in depth.
    per_reel_seconds: float = 90.0


class XSession:
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
        self.cascade = XCascade(_HeartbeatRouter(router, self._touch),
                                campaign, session_id=self.session_id)
        # The feed's OWN heartbeat. `_HeartbeatRouter` covers the model calls and
        # the loop below covers everything it can see, but the interval BETWEEN two
        # `walk()` yields belongs entirely to the feed: nav + landed/login probes +
        # up to four scroll rounds per source, repeated for every unproductive
        # source in the brief, with nothing to bump `last_activity_at`. That is
        # unbounded in the number of sources; the six redirecting hashtag pages on
        # the live 2026-08-19 Instagram brief are what it looks like in practice.
        # Duck-typed on purpose: FakeFeed and every non-CDP feed simply lack the
        # attribute and stay exactly as they were.
        if hasattr(feed, "on_progress"):
            feed.on_progress = self._touch
        self._empty_streak = 0
        self._read_budget_flagged = False

    # ---- helpers ----
    def _touch(self) -> None:
        """Fine-grained progress heartbeat: bump sessions.last_activity_at ONLY.

        The watchdog kills any running session whose heartbeat is >180s stale, and
        before 2026-08-20 the only bump inside a post came from ``_flush()`` at the
        end of it (plus the one added between the gate and the browser block). One
        slow cascade gate or one long comment-scoring loop therefore emitted nothing
        for its whole duration, and the fleet's first real campaign dead-lettered at
        attempt 5 with reason=worker_stall. Called after each unit of REAL progress —
        a model verdict, a comment scored, a permalink opened — never on a timer.

        Never raises: it runs inside the router facade's ``finally``, where an
        exception would mask the model error the loop is about to handle. A DB that
        is genuinely broken still surfaces on the next ``_flush()``."""
        try:
            self.store.touch_session(self.session_id)
        except Exception:  # noqa: BLE001 — a heartbeat must never break a run
            log.debug("heartbeat bump failed · session=%s", self.session_id,
                      exc_info=True)

    def _touched(self, value):
        """Bump the heartbeat and hand `value` straight back — for tagging the lazy
        frame-capture callback as progress. It runs INSIDE the cascade gate and is
        not a model call, so the router facade never sees it; a slow screenshot
        between two verdicts would otherwise be invisible to the watchdog."""
        self._touch()
        return value

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
                    "feed tapped out / drifting — widen Lists / refresh saved searches",
                    campaign_id=self.campaign.campaign_id, session_id=self.session_id)
                self._emit("feed_walk", "warn", "Feed tapping out — re-steer suggested",
                           {"ratio": round(ratio, 2)})

    def _read_budget_exhausted(self) -> bool:
        """Soft-flag once and signal stop when the per-session view cap is hit —
        BEFORE the hard daily lockout (PRD §9 soft-flag tier)."""
        if self.counters.reels_seen < self.cfg.read_view_soft_cap:
            return False
        if not self._read_budget_flagged:
            self._read_budget_flagged = True
            self.store.raise_flag(
                "read_budget", "soft",
                f"approaching daily read-view cap ({self.counters.reels_seen} views) — "
                "stopping before the hard rate-limit lockout",
                campaign_id=self.campaign.campaign_id, session_id=self.session_id)
            self._emit("feed_walk", "warn",
                       "Read-budget cap approached — stopping before lockout",
                       {"views": self.counters.reels_seen})
        return True

    def _flag_relevant_discard(self, reel: Reel, reason: str) -> None:
        """FIX P0: record that a post which PASSED relevance never reached reply/quote
        scoring. Not cosmetic: mark_seen already ran, seen_reels has no TTL, and
        nothing ever deletes from it — so the post is unreachable for this campaign
        for good. Soft severity (the walk carries on), but it guarantees a run can
        never again finish 'completed, 0 leads, no health flags' after destroying a
        confirmed hit."""
        self.store.raise_flag(
            RELEVANT_REEL_DISCARDED_FLAG, "soft",
            f"post {reel.reel_id}: passed relevance but was skipped before reply/quote "
            f"scoring ({reason}) — it is marked seen and will never be revisited",
            campaign_id=self.campaign.campaign_id, session_id=self.session_id)

    def _reel_deadline_breached(self, reel: Reel, started: float) -> bool:
        """True once one post's BROWSER block (open_reel + reply/quote fetch) has
        exceeded per_reel_seconds. `started` is anchored after the cascade gate, so
        slow classification never spends this budget — that inversion is what
        silently discarded relevant posts before FIX P0. The outer wall-clock
        backstop behind the per-call Playwright timeouts. On breach it emits a warn
        step; the caller SKIPS the remaining browser work and continues — NEVER
        halts."""
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
        # The comment fetch is a browser/network round-trip with no bump of its own;
        # without this the gap runs from the pre-block _flush all the way to the
        # first comment's verdict.
        self._touch()

        # empty-interception canary (PRD §9 tier 3) — load-bearing on X (doc_id drift)
        if not self.feed.healthy():
            self._empty_streak += 1
            if self._empty_streak >= self.cfg.empty_interception_halt:
                raise HaltSession("empty interception for N posts — GraphQL doc_id/features drift",
                                  kind="canary")
        else:
            self._empty_streak = 0

        if comments:
            self._emit("comments", "info", f"Scoring {len(comments)} replies/quotes",
                       {"postId": reel_id, "count": len(comments)})
        found = 0
        # Defect A — per-batch overshoot. The caller raises `self.counters.matches`
        # ONCE for this whole batch, so the lead-target guard at the top of the per-
        # post walk cannot fire mid-batch and the run overshoots by up to the
        # size of the batch. `self.counters.matches + found` is the live running total;
        # `self.counters.matches` alone is the PRE-batch value and still overshoots.
        stopped_early = False
        for comment in comments:
            try:
                res = self.cascade.score_comment(comment, reel)
            except Exception as e:  # noqa: BLE001 — auto-skip a single failure
                self.store.raise_flag("parse_skip", "soft",
                                      f"comment {comment.comment_id}: {e}",
                                      campaign_id=cid, session_id=self.session_id)
                log.debug("[comments] skipped comment %s — %s", comment.comment_id, e)
                continue
            finally:
                # One bump per comment, on the skip path too (a failed score burned
                # the same wall-clock). This loop is N model calls deep and used to
                # bump nothing until the whole post finished — with 20 comments that
                # is the second way one post outlived the 180s watchdog. `finally`
                # runs before the `continue` above takes effect.
                self._touch()
            self.counters.comments_scored += 1
            if res.is_match:
                d = res.decision
                # Tag the surface so a quote-post lead is captured exactly like a
                # replier — no new column (PRD §8). New dict, never mutate.
                surface = "reply" if comment.is_reply else "quote"
                extracted = {**(d.extracted or {}), "surface": surface}
                # v27 redaction: `intent` is the ONLY lead text the org ever
                # sees — username and raw comment stop at the superadmin plane
                # from here. `getattr` because a Decision carries the model's own
                # `intent` only when the MATCH prompt asked for one (a
                # campaign-authored prompt never does); derive_intent composes the
                # line from the grounded fields plus the post text when it didn't.
                # Deliberately `d.extracted`, not the surface-tagged dict above:
                # our own bookkeeping key must never surface to a customer as
                # "Interested in surface reply".
                self.store.upsert_match(
                    campaign_id=cid, reel_id=reel_id, comment_id=comment.comment_id,
                    username=comment.username, text=comment.text, lang=comment.lang,
                    score=d.score, reason=d.reason, extracted=extracted, tier=d.tier,
                    intent=derive_intent(getattr(d, "intent", None),
                                         extracted=d.extracted,
                                         post_caption=reel.caption,
                                         comment_text=comment.text),
                    session_id=self.session_id, platform=plat,
                    found_by_models=compute_found_by(d.model, d.comparisons,
                                                     self.campaign.threshold))
                found += 1
                self._emit("comments", "success",
                           f"Match ({surface}): @{comment.username} (score {d.score:.2f})",
                           {"username": comment.username, "score": d.score,
                            "tier": d.tier, "surface": surface, "postId": reel_id})
                if (self.lead_target is not None
                        and self.counters.matches + found >= self.lead_target):
                    stopped_early = True
                    break
        if not stopped_early:
            # A mid-batch stop leaves the TAIL of this page unscored, and `new_cursor`
            # covers the WHOLE fetched page — advancing it would mark items we never
            # read as consumed and lose them for good. An unread item is UNKNOWN,
            # never "not a lead".
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
                # A new item arrived from the feed — real progress, and the
                # first bump after the walk's own scroll/fetch, which runs INSIDE
                # this generator between two iterations and bumped nothing.
                self._touch()
                if self.counters.reels_seen >= self.pacer.reel_budget:
                    break
                if self.lead_target is not None and self.counters.matches >= self.lead_target:
                    break
                if self._read_budget_exhausted():
                    break  # soft stop, well before the hard daily lockout

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

                try:
                    # capture_fn is wrapped in _touched: the screenshot runs
                    # inside the gate and is not a model call, so only this makes it
                    # visible to the watchdog (see _touched).
                    gate = self.cascade.gate_post(
                        reel, capture_fn=lambda r=reel: self._touched(
                            self.feed.capture_frames(r)))
                except Exception as e:  # noqa: BLE001 — auto-skip transient
                    self.store.mark_seen(cid, reel.reel_id, relevant=None,
                                         author=reel.author or None,
                                         caption=reel.caption or None,
                                         source=reel.source or None,
                                         author_id=reel.author_id or None,
                                         platform=plat)
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
                                     ocr_text=reel.ocr_text or None,
                                     source=reel.source or None,
                                     author_id=reel.author_id or None,
                                     platform=plat)

                # FIX P0: anchor the wall-clock backstop HERE, after the gate result
                # is persisted — it used to start before cascade.gate_post(), so a
                # post whose CLASSIFICATION overran per_reel_seconds was skipped
                # before open_reel ever ran. Since mark_seen had already written the
                # row and seen_reels has no TTL, that permanently blacklisted the
                # post. Proven live on Instagram (2026-08-19, reel DFdnoSsgWBk
                # classified relevant 0.85 / conf 0.90 and discarded in the same log
                # second); this engine is a copy of that loop. The budget now bounds
                # only the browser work, which is what cfg.per_reel_seconds claims.
                reel_start = self._clock()
                # Heartbeat between the two now-serialized expensive stages. The
                # watchdog halts any session whose last_activity_at is >180s stale
                # (session_watchdog.py), and last_activity_at is bumped ONLY by
                # _flush() -> update_counters, which the loop otherwise reaches just
                # once per post, at the very end. Before the re-anchor a slow post
                # cost dwell+G; now it costs dwell+G+O+C. Without this the fix would
                # trade "one post silently lost" for "whole session halted as
                # stalled" — strictly worse, and on exactly the escalated posts most
                # likely to be leads.
                self._flush()
                # Bound once, read twice: the post-block diagnostic below must not
                # re-emit the same warn for a breach this branch already reported.
                breached = False

                if gate.relevant:
                    # Counted for EVERY post the gate judged relevant, before any
                    # later skip can drop it: relevance_passes must never disagree
                    # with seen_reels.relevant — that disagreement is what made the
                    # live loss invisible in the run summary.
                    self.counters.relevance_passes += 1
                    self._emit("relevance", "success",
                               f"Relevant ✓ — @{reel.author or reel.reel_id}",
                               {"postId": reel.reel_id, "author": reel.author})
                    opened = self.feed.open_reel(reel)
                    # Permalink navigation is the longest unguarded step inside the
                    # browser block: per_reel_seconds is only read AFTER it returns,
                    # so a 150s nav would sit between the pre-block _flush and the
                    # next bump with nothing in between. Progress, not a timer — it
                    # fires because the navigation finished.
                    self._touch()
                    if not opened:
                        self.store.raise_flag(
                            "post_unavailable", "soft",
                            f"post {reel.reel_id}: permalink did not open — skipped",
                            campaign_id=cid, session_id=self.session_id)
                        self._flag_relevant_discard(reel, "permalink did not open")
                        self._emit("feed_walk", "warn",
                                   f"Post {reel.reel_id} unavailable — skipped",
                                   {"postId": reel.reel_id})
                    elif (breached := self._reel_deadline_breached(reel, reel_start)):
                        # The only real interruption point inside the browser block:
                        # open_reel alone burned the whole budget, so don't also pay
                        # for the reply/quote fetch + scoring. Keeps the backstop's
                        # original job (a stack of slow-but-not-timed-out calls can't
                        # wedge the walk) while charging it to browser work only. The
                        # post is still lost, so it is flagged rather than silent.
                        self._flag_relevant_discard(
                            reel, f"open_reel exceeded the {self.cfg.per_reel_seconds:.0f}s "
                                  "browser budget")
                    else:
                        found = self._process_comments(reel)
                        self.counters.matches += found
                        if found > 0:
                            self._emit("comments", "success",
                                       f"{found} match(es) on post {reel.reel_id}",
                                       {"postId": reel.reel_id, "found": found})

                # Wall-clock backstop AFTER the block too: log a chronically slow post.
                # Skipped when the in-block checkpoint already reported this breach —
                # re-evaluating the same (reel, reel_start) emitted the warn twice.
                if not breached:
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
            "likes": 0,                  # X engine is read-only
            "follows": 0,
            "halt_reason": halt_reason,
            "halt_kind": None,           # success path never halts; halt re-raises
        }


def run_session(*, campaign, store, router, feed, soul, pacer,
                session_id=None, run_id=None, lead_target=None) -> dict:
    """Engine entrypoint (see ``engines.base.EngineProtocol``). A HaltSession is
    folded into the summary as ``halt_reason``/``halt_kind`` so the run-loop
    aggregation stays uniform across engines."""
    session = XSession(store=store, router=router, feed=feed, soul=soul,
                       campaign=campaign, pacer=pacer, session_id=session_id,
                       run_id=run_id, lead_target=lead_target)
    try:
        return session.run()
    except HaltSession as h:
        return {"session_id": session.session_id, "halt_reason": h.reason,
                "halt_kind": h.kind}
