"""Session orchestrator (PRD §7, §9, §10, §11).

Walks the feed behind the FeedSource interface, runs the cascade against the
active brief, persists matches + state, and enforces the three failure tiers:

  - auto-skip + continue:  a single reel/comment parse fails (transient).
  - soft flag + continue:  feed exhaustion/drift, spend cap, cloud degraded.
  - halt + alert human:    login expired, checkpoint, empty-interception canary.

Counters and the tired-feed flag are written to SQLite as the session runs, so
the panel sees live progress and a killed run resumes from state.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..base import SOFT_HALT_KINDS, HaltSession, session_crash_guard
from .actions import ActionPolicy
from .cascade import Cascade
from ...core.config import Campaign, Soul
from ...core.matching import compute_found_by
from ...core.feed import FeedSource, Reel
from ...core.logsetup import SUCCESS_LEVEL, get_logger
from ...core.pacing import Pacer
from ...core.pause import pause_file_from_env, wait_while_paused
from ...core.router import Router, env_flag
from ...core.store import SessionCounters, Store
from ...core.transcribe import NullTranscriber, Transcriber

log = get_logger(__name__)

# Map the activity-feed level vocabulary to stdlib logging levels (console+file).
_LEVEL_MAP = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "success": SUCCESS_LEVEL,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}




@dataclass
class SessionConfig:
    tired_feed_ratio: float = 0.6     # already-seen skips / reels seen → flag
    tired_feed_min_reels: int = 10    # don't judge the ratio on tiny samples
    empty_interception_halt: int = 5  # consecutive empty fetches → halt (canary)
    watchlist_ttl_days: float = 10.0
    # Outer wall-clock backstop for the BROWSER block only (open_reel + comment
    # fetch/scoring). Even with per-call Playwright timeouts, a pathological chain
    # of slow-but-not-timed-out calls could stack; this stops a reel that has
    # already burned the budget in open_reel from also paying for the comment
    # stage. The clock is anchored AFTER the cascade gate — classification time
    # (vision / STT / video / escalation) is deliberately not charged to it, see
    # the re-anchor in _run_after_start. On breach the reel is SKIPPED (warn) and
    # the walk continues — NEVER halt.
    per_reel_seconds: float = 90.0


# Flood safety-net for the live activity feed: cap narrative events per session. The
# coarse milestone taxonomy stays far below this; it only guards a pathological run.
MAX_SESSION_EVENTS = 500

# FIX B: a healthy run scanning ordinary (irrelevant) reels emits no relevance/match
# event, so the panel's live feed goes quiet and its stall banner fires (frontend
# STALL_THRESHOLD_SEC=120) even though the walk is fine. Emit a time-throttled
# progress heartbeat on the common per-reel path — comfortably under that threshold.
PROGRESS_EVENT_INTERVAL_SEC = 45.0

# FIX P0: stable, cross-engine flag kind for a reel the cascade judged RELEVANT that
# never reached comment scoring. store.mark_seen has already run by then and
# seen_reels has NO TTL (store.is_seen is a bare existence check and nothing in
# aizu/ ever DELETEs from it), so such a reel is blacklisted for that campaign
# forever — a silent, permanent lead loss. The 2026-08-19 live Instagram run
# reported status=completed / matches=0 / ZERO health flags while having discarded
# reel DFdnoSsgWBk (relevant=True, score 0.85, confidence 0.90). This flag is what
# makes that combination impossible again; keep the string identical in the
# linkedin and x engines so one panel/ops query covers all three.
RELEVANT_REEL_DISCARDED_FLAG = "relevant_reel_discarded"


class _HeartbeatRouter:
    """Router facade that bumps the session heartbeat every time a model call
    RETURNS — i.e. on every real verdict, success or failure.

    Why a facade rather than a hook inside the cascade: ONE ``gate_reel`` chains up
    to five model calls (caption → vision → STT → video → escalation) before it
    returns a single verdict, and ``score_comment`` chains up to two. Bumping only
    where the cascade returns therefore multiplies whatever budget the router
    allows one call by five — and on 2026-08-20 that product killed
    job-2099fb29e88b five times over with "stalled: no activity for over 180s"
    (session_watchdog.STALL_TIMEOUT_SEC). Tapping the router instead bounds the gap
    to ONE bounded model call for every engine, and the two sibling engines
    (linkedin, x) keep their own private cascade copies that need not learn a
    watchdog exists.

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


class Session:
    def __init__(self, *, store: Store, router: Router, feed: FeedSource,
                 soul: Soul, campaign: Campaign, pacer: Optional[Pacer] = None,
                 cfg: Optional[SessionConfig] = None,
                 session_id: Optional[str] = None,
                 run_id: Optional[str] = None,
                 lead_target: Optional[int] = None,
                 pause_file: Optional[Path] = None,
                 pause_sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 transcriber: Optional[Transcriber] = None):
        self.store = store
        self.router = router
        self.feed = feed
        self.soul = soul
        self.campaign = campaign
        self.pacer = pacer or Pacer()
        self.cfg = cfg or SessionConfig()
        self.session_id = session_id or uuid.uuid4().hex[:12]
        # v10: the RunManager run that spawned this session (None for a direct CLI or
        # unit run). When set, meaningful steps are streamed to the panel's live feed.
        self.run_id = run_id
        # v12: cooperative pause sentinel (RunManager-launched runs only; a direct CLI
        # run leaves this None and pausing is inert). The sleep is injectable for tests.
        self.pause_file = pause_file if pause_file is not None else pause_file_from_env()
        self._pause_sleep = pause_sleep
        # Monotonic source of truth for the per-reel wall-clock backstop; injectable
        # so tests drive the deadline deterministically without real sleeping.
        self._clock = clock
        # Stop this session as soon as it has found this many matched comments (the
        # per-session reel budget still bounds it; the run loop spawns more sessions
        # to reach a larger target). None → walk the reel budget regardless of leads.
        self.lead_target = lead_target
        self._event_seq = 0
        # FIX B: wall-clock of the last scan-progress heartbeat (throttle anchor).
        # Reset to the run's start time in _run_after_start so the first heartbeat
        # fires ~PROGRESS_EVENT_INTERVAL_SEC in, not instantly after "Run started".
        self._last_progress_emit = 0.0
        self.counters = SessionCounters()
        self.cascade = Cascade(_HeartbeatRouter(router, self._touch),
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
        self.actions = ActionPolicy.from_campaign(campaign)
        self._empty_streak = 0
        # Uzbek STT (Instagram-only, campaign-gated — see cascade.py's gate_reel).
        # A real Transcriber is only ever handed in by run_session() below, and only
        # after the campaign gate already checked out and the model was warm-loaded
        # outside any per-reel deadline. _stt_wired short-circuits the lambda in
        # _run_after_start so a NullTranscriber never even gets a capture_audio call.
        self.transcriber = transcriber or NullTranscriber()
        self._stt_wired = not isinstance(self.transcriber, NullTranscriber)
        # Video-analysis tier (Instagram-only, campaign-gated in cascade.py). Wired
        # only when the campaign opted in AND the global kill-switch is on, so a
        # non-opted-in session never even builds the lazy capture lambda.
        self._va_wired = (getattr(campaign, "enable_video_analysis", False)
                          and env_flag("AIZU_VIDEO_ANALYSIS_ENABLED"))

    # ---- helpers ----
    def _touch(self) -> None:
        """Fine-grained progress heartbeat: bump sessions.last_activity_at ONLY.

        The watchdog kills any running session whose heartbeat is >180s stale, and
        before 2026-08-20 the only bump inside a reel came from ``_flush()`` at the
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
        """Bump the heartbeat and hand `value` straight back — for tagging a lazy
        acquisition callback (frame capture, STT, video sampling) as progress. Those
        run INSIDE the cascade gate and are not model calls, so the router facade
        never sees them; a 60s ffmpeg download between two verdicts would otherwise
        be invisible to the watchdog."""
        self._touch()
        return value

    def _flush(self) -> None:
        self.counters.escalations = self.cascade.escalations
        self.counters.transcriptions = self.cascade.transcriptions
        self.counters.video_analyses = self.cascade.video_analyses
        self.counters.spend_usd = self.store.total_spend(self.campaign.campaign_id)
        self.store.update_counters(self.session_id, self.counters)

    def _emit(self, phase: str, level: str, message: str,
              detail: Optional[dict] = None) -> None:
        """Record one narrative step to BOTH sinks: the colorful console/file log
        (always, so direct CLI and unit runs are visible too) and the run activity
        feed (the panel's live drawer) — the latter only when the panel launched
        this run (run_id set), capped per session as a flood safety-net. Defensive:
        store.emit_run_event swallows DB errors and the json.dumps is guarded, so
        this never disturbs the run loop."""
        # Sink 1: console + file — every meaningful step, regardless of run_id.
        # Floats are rounded for readability here only; the DB blob below stays raw.
        ctx = (" · " + " ".join(
            f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}"
            for k, v in detail.items())) if detail else ""
        log.log(_LEVEL_MAP.get(level, logging.INFO), "[%s] %s%s", phase, message, ctx)
        # Sink 2: panel live feed (DB) — only for a panel-launched run.
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

    def _check_pause(self) -> None:
        """At a between-reels checkpoint, idle while the run is paused (cooperative —
        no half-processed reel, no open DB write). Emits exactly one paused/resumed
        step to the activity feed so the panel reflects the state."""
        wait_while_paused(
            self.pause_file,
            on_enter=lambda: self._emit("lifecycle", "warn", "Run paused by operator"),
            on_exit=lambda: self._emit("lifecycle", "info", "Run resumed"),
            sleep=self._pause_sleep)

    def _maybe_emit_progress(self) -> None:
        """FIX B: throttled scan-progress heartbeat on the COMMON per-reel path (every
        reel counted toward reels_seen, relevant or not). Keeps the panel's live feed
        warm during a long stretch of irrelevant reels so its stall banner never fires
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
                   f"Scanning — {c.reels_seen} reels seen, {c.relevance_passes} relevant",
                   {"reelsSeen": c.reels_seen, "relevancePasses": c.relevance_passes,
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
                    "feed tapped out / drifting — re-steer on mobile",
                    campaign_id=self.campaign.campaign_id, session_id=self.session_id)
                self._emit("feed_walk", "warn", "Feed tapping out — re-steer suggested",
                           {"ratio": round(ratio, 2)})

    def _halt(self, reason: str, kind) -> None:
        """Raise HaltSession, first escalating the self-healing cooldown (gap #1)
        for a SOFT kind — action_block / canary, see engines/base.py's
        SOFT_HALT_KINDS. A HARD kind (checkpoint/login) or `daytime` (its own
        wall-clock gate, checked before this even runs) never touches the cooldown
        table — Store.record_soft_halt is a no-op unless `kind` is in that set, so
        those keep the original human-gated HaltSession behavior untouched."""
        if kind in SOFT_HALT_KINDS:
            self.store.record_soft_halt(self.campaign.campaign_id,
                                        self.campaign.platform, kind)
        raise HaltSession(reason, kind=kind)

    def _act(self, action_type: str, reel: "Reel", do_action, target: Optional[str]) -> None:
        """Perform a rate-limited engagement action, log it, and halt on any
        action-block. `do_action` returns True on success."""
        cid = self.campaign.campaign_id
        ok = False
        try:
            ok = bool(do_action(reel))
        except Exception as e:  # noqa: BLE001 — an action must never crash the run
            self.store.raise_flag("action_error", "soft", f"{action_type}: {e}",
                                  campaign_id=cid, session_id=self.session_id)
            self._emit("engage", "warn", f"{action_type.capitalize()} failed",
                       {"reelId": reel.reel_id})
        self.store.log_action(cid, action_type, reel_id=reel.reel_id, target=target,
                              succeeded=ok, session_id=self.session_id)
        # Like/follow are bounded CDP calls, but open_reel -> like -> action-block
        # probe -> fetch_comments used to be one unbroken silence; the action ran
        # and was logged, so this bump is progress, not a ticker.
        self._touch()
        if ok:
            if action_type == "like":
                self.actions.record_like()
                self.counters.likes += 1
                self._emit("engage", "success", f"Liked reel {reel.reel_id}",
                           {"reelId": reel.reel_id})
            else:
                self.actions.record_follow()
                self.counters.follows += 1
                self._emit("engage", "success", f"Followed @{target}",
                           {"target": target})
        # An action-block halts the session (soul.md) but is SOFT/rate-limit-shaped —
        # self._halt escalates the cooldown so it self-heals instead of waiting on a
        # human (gap #1).
        if self.feed.detect_action_block():
            self._halt("action-block detected after " + action_type,
                       kind="action_block")

    def _flag_relevant_discard(self, reel: "Reel", reason: str) -> None:
        """FIX P0: record that a reel which PASSED relevance never reached comment
        scoring. This is not cosmetic: mark_seen already ran, seen_reels has no TTL,
        and nothing ever deletes from it — so the reel is unreachable for this
        campaign for good. Soft severity (the walk carries on), but it guarantees a
        run can never again finish 'completed, 0 leads, no health flags' after
        destroying a confirmed hit."""
        self.store.raise_flag(
            RELEVANT_REEL_DISCARDED_FLAG, "soft",
            f"reel {reel.reel_id}: passed relevance but was skipped before comment "
            f"scoring ({reason}) — it is marked seen and will never be revisited",
            campaign_id=self.campaign.campaign_id, session_id=self.session_id)

    def _reel_deadline_breached(self, reel: "Reel", started: float) -> bool:
        """True once a reel's BROWSER block (open_reel + comment fetch) has exceeded
        per_reel_seconds. `started` is anchored after the cascade gate, so slow
        classification never spends this budget — that inversion is what silently
        discarded relevant reels before FIX P0. The outer backstop behind the
        per-call Playwright timeouts: a pathological stack of slow-but-not-timed-out
        calls can't wedge the walk. On breach it emits a warn step; the caller SKIPS
        the remaining browser work and continues — this NEVER halts the run."""
        if self._clock() - started <= self.cfg.per_reel_seconds:
            return False
        self._emit("feed_walk", "warn",
                   f"Reel {reel.reel_id} exceeded per-reel deadline — skipped",
                   {"reelId": reel.reel_id})
        return True

    def _transcribe_reel(self, reel: "Reel") -> Optional[str]:
        """capture_audio (feed-side fetch+ffmpeg, unverified against live IG — fails
        soft to None) → local KotibAI STT. Never raises. Cleans up the wav file
        regardless of outcome so a per-reel temp file never accumulates across a
        long-running session."""
        wav_path = self.feed.capture_audio(reel)
        if not wav_path:
            return None
        try:
            result = self.transcriber.transcribe(wav_path)
            if result is None:
                self.store.raise_flag(
                    "stt_failed", "soft",
                    f"reel {reel.reel_id}: transcription unavailable",
                    campaign_id=self.campaign.campaign_id, session_id=self.session_id)
                return None
            return result.text
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass

    def _analyze_video_reel(self, reel: "Reel") -> list[str]:
        """capture_video_frames (feed-side download + ffmpeg frame sampling,
        unverified against live IG — fails soft to []) → base64 frames for the
        video-analysis tier, with ``reel.frame_timestamps`` set as a side effect.
        Never raises; the feed cleans up its own temp files (frames are in memory)."""
        try:
            return self.feed.capture_video_frames(reel) or []
        except Exception as e:  # noqa: BLE001 — unverified acquisition path, never wedge
            self.store.raise_flag(
                "video_analysis_failed", "soft",
                f"reel {reel.reel_id}: frame capture unavailable",
                campaign_id=self.campaign.campaign_id, session_id=self.session_id)
            log.debug("video frame capture failed · reel=%s err=%s", reel.reel_id, e)
            return []

    def _maybe_like(self, reel: "Reel") -> None:
        if self.actions.can_like():
            self._act("like", reel, self.feed.like_reel, reel.reel_id)

    def _maybe_follow(self, reel: "Reel") -> None:
        if self.actions.can_follow():
            self._act("follow", reel, self.feed.follow_author, reel.author or None)

    def _process_comments(self, reel: "Reel") -> int:
        reel_id = reel.reel_id
        cid = self.campaign.campaign_id
        plat = self.campaign.platform
        cursor = self.store.get_cursor(cid, reel_id, platform=plat)
        comments, new_cursor = self.feed.fetch_comments(reel_id, cursor)
        # The comment fetch is a browser/network round-trip with no bump of its own;
        # without this the gap runs from the pre-block _flush all the way to the
        # first comment's verdict.
        self._touch()

        # empty-interception canary (PRD §9 tier 3) — SOFT/auto-resumable (gap #1).
        if not self.feed.healthy():
            self._empty_streak += 1
            if self._empty_streak >= self.cfg.empty_interception_halt:
                self._halt("empty interception for N reels — endpoint drift",
                          kind="canary")
        else:
            self._empty_streak = 0

        if comments:
            self._emit("comments", "info", f"Scoring {len(comments)} comments",
                       {"reelId": reel_id, "count": len(comments)})

        found = 0
        for comment in comments:
            try:
                # The reel rides along so extraction can read the advertiser's
                # caption / on-screen text (the brief-defined fields).
                res = self.cascade.score_comment(comment, reel)
            except Exception as e:  # noqa: BLE001 — auto-skip a single failure
                self.store.raise_flag("parse_skip", "soft", f"comment {comment.comment_id}: {e}",
                                      campaign_id=cid, session_id=self.session_id)
                log.debug("[comments] Skipped comment %s — %s", comment.comment_id, e)
                continue
            finally:
                # One bump per comment, on the skip path too (a failed score burned
                # the same wall-clock). This loop is N model calls deep and used to
                # bump nothing until the whole reel finished — with 20 comments on a
                # reel that is the second way one reel outlived the 180s watchdog.
                # `finally` runs before the `continue` above takes effect.
                self._touch()
            self.counters.comments_scored += 1
            if res.is_match:
                d = res.decision
                self.store.upsert_match(
                    campaign_id=cid, reel_id=reel_id, comment_id=comment.comment_id,
                    username=comment.username, text=comment.text, lang=comment.lang,
                    score=d.score, reason=d.reason, extracted=d.extracted, tier=d.tier,
                    session_id=self.session_id, platform=plat,
                    found_by_models=compute_found_by(d.model, d.comparisons,
                                                     self.campaign.threshold),
                    initial_status="needs_review" if res.needs_review else "new")
                found += 1
                self._emit("comments", "success",
                           f"Match: @{comment.username} (score {d.score:.2f})",
                           {"username": comment.username, "score": d.score,
                            "tier": d.tier, "reelId": reel_id})
        self.store.set_cursor(cid, reel_id, new_cursor, platform=plat)
        if found:
            self.store.add_to_watchlist(cid, reel_id, ttl_days=self.cfg.watchlist_ttl_days,
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
                # v12: cooperative pause checkpoint — idle here (between reels) while
                # the operator has the run paused, then carry on from the same cursor.
                self._check_pause()
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

                try:
                    # Frames captured lazily (only if the gate needs vision); audio
                    # only captured+transcribed lazily too, and only when this
                    # session actually has a real (non-Null) transcriber wired —
                    # cascade.py's own campaign-gate check still decides per-reel.
                    # Each lazy tier is wrapped in _touched: capture/STT/ffmpeg run
                    # inside the gate and are not model calls, so only this makes
                    # them visible to the watchdog (see _touched).
                    gate = self.cascade.gate_reel(
                        reel,
                        capture_fn=lambda r=reel: self._touched(
                            self.feed.capture_frames(r)),
                        transcribe_fn=(lambda r=reel: self._touched(
                            self._transcribe_reel(r)))
                        if self._stt_wired else None,
                        video_analyze_fn=(lambda r=reel: self._touched(
                            self._analyze_video_reel(r)))
                        if self._va_wired else None)
                except Exception as e:  # noqa: BLE001 — auto-skip transient
                    self.store.mark_seen(cid, reel.reel_id, relevant=None,
                                         author=reel.author or None, caption=reel.caption or None,
                                         source=reel.source or None,
                                         author_id=reel.author_id or None,
                                         platform=plat)
                    self.store.raise_flag("parse_skip", "soft", f"reel {reel.reel_id}: {e}",
                                          campaign_id=cid, session_id=self.session_id)
                    self._emit("feed_walk", "warn",
                               f"Skipped reel {reel.reel_id} — parse error",
                               {"reelId": reel.reel_id})
                    self._flush()
                    continue

                self.store.mark_seen(cid, reel.reel_id, relevant=gate.relevant,
                                     author=reel.author or None, caption=reel.caption or None,
                                     ocr_text=reel.ocr_text or None,
                                     transcript=reel.transcript or None,
                                     transcript_lang=("uz" if reel.transcript else None),
                                     video_analyzed=(True if gate.used_video else None),
                                     video_analysis_summary=(
                                         json.dumps(reel.video_analysis, ensure_ascii=False)
                                         if reel.video_analysis else None),
                                     source=reel.source or None,
                                     author_id=reel.author_id or None,
                                     platform=plat)

                # FIX P0: anchor the wall-clock backstop HERE, after the gate result
                # is persisted — it used to start before cascade.gate_reel(), so a
                # reel whose CLASSIFICATION overran per_reel_seconds was skipped
                # before open_reel ever ran. Since mark_seen had already written the
                # row and seen_reels has no TTL, that permanently blacklisted the
                # reel. Live on 2026-08-19: DFdnoSsgWBk classified relevant (0.85 /
                # conf 0.90) and was discarded in the same log second, and the
                # escalation path that produces those slow classifications fires on
                # exactly the borderline-but-genuine content most likely to be a
                # lead. The budget now bounds only the browser work, which is what
                # cfg.per_reel_seconds has always claimed to bound.
                reel_start = self._clock()
                # Heartbeat between the two now-serialized expensive stages. The
                # watchdog halts any session whose last_activity_at is >180s stale
                # (session_watchdog.py), and last_activity_at is bumped ONLY by
                # _flush() -> update_counters, which the loop otherwise reaches just
                # once per reel, at the very end. Before the re-anchor a slow reel
                # cost dwell+G; now it costs dwell+G+O+C, and the shakedown log
                # already had a 186s gap on the 159s-vision reel. Without this the
                # fix would trade "one reel silently lost" for "whole session halted
                # as stalled" — strictly worse, and on exactly the escalated reels
                # most likely to be leads.
                self._flush()
                # Bound once, read twice: the post-block diagnostic below must not
                # re-emit the same warn for a breach this branch already reported.
                breached = False

                if gate.relevant:
                    # Counted for EVERY reel the gate judged relevant, before any
                    # later skip can drop it: relevance_passes must never disagree
                    # with seen_reels.relevant. The live run reported
                    # relevance_passes=0 while seen_reels held a relevant=1 row,
                    # which is what made the loss invisible in the summary.
                    self.counters.relevance_passes += 1
                    self._emit("relevance", "success",
                               f"Relevant ✓ — @{reel.author or reel.reel_id}",
                               {"reelId": reel.reel_id, "author": reel.author})
                    # Open the reel full-screen — comments + Like/Follow controls
                    # only exist there, not on hashtag/account grid pages. If the
                    # permalink doesn't resolve (deleted/unavailable reel), reading
                    # the page would attribute someone else's comments to it.
                    opened = self.feed.open_reel(reel)
                    # Permalink navigation is the longest unguarded step inside the
                    # browser block: per_reel_seconds is only read AFTER it returns,
                    # so a 150s nav would sit between the pre-block _flush and the
                    # next bump with nothing in between. Progress, not a timer — it
                    # fires because the navigation finished.
                    self._touch()
                    if not opened:
                        self.store.raise_flag(
                            "reel_unavailable", "soft",
                            f"reel {reel.reel_id}: permalink did not open — skipped",
                            campaign_id=cid, session_id=self.session_id)
                        self._flag_relevant_discard(reel, "permalink did not open")
                        self._emit("feed_walk", "warn",
                                   f"Reel {reel.reel_id} unavailable — skipped",
                                   {"reelId": reel.reel_id})
                    elif (breached := self._reel_deadline_breached(reel, reel_start)):
                        # The only real interruption point inside the browser block:
                        # open_reel alone burned the whole budget, so don't also pay
                        # for the comment fetch + scoring. This keeps the backstop's
                        # original job (a stack of slow-but-not-timed-out calls can't
                        # wedge the walk) while charging it to browser work only. The
                        # reel is still lost, so it is flagged rather than silent.
                        self._flag_relevant_discard(
                            reel, f"open_reel exceeded the {self.cfg.per_reel_seconds:.0f}s "
                                  "browser budget")
                    else:
                        self._maybe_like(reel)                   # like on relevance (opt-in)
                        found = self._process_comments(reel)
                        self.counters.matches += found
                        if found > 0:
                            self._emit("comments", "success",
                                       f"{found} match(es) on reel {reel.reel_id}",
                                       {"reelId": reel.reel_id, "found": found})
                            self._maybe_follow(reel)             # follow only on a real lead

                # Wall-clock backstop AFTER the block too: if open_reel/comment fetch
                # burned the budget, log it so a chronically slow reel is diagnosable
                # (the work is already done here; the warn is the load-bearing signal).
                # Skipped when the in-block checkpoint already reported this breach —
                # re-evaluating the same (reel, reel_start) emitted the warn twice and
                # burned two of the 500-event per-run budget on one reel.
                if not breached:
                    self._reel_deadline_breached(reel, reel_start)

                self._check_tired_feed()
                self._flush()
                self.pacer.between_reels()

            self._flush()
            self.store.prune_watchlist(cid)
            self.store.end_session(self.session_id, "completed")
            # Gap #1: a clean completion resets any cooldown escalation from a prior
            # SOFT halt — the next one starts back at attempt 1 instead of continuing
            # a stale streak. A no-op (DELETE affecting 0 rows) when there was none.
            self.store.clear_cooldown(cid, plat)
            self._emit("lifecycle", "success",
                       f"Run completed — {self.counters.matches} match(es), "
                       f"${self.counters.spend_usd:.4f}",
                       {"matches": self.counters.matches,
                        "spendUsd": self.counters.spend_usd})
        except HaltSession as h:
            halt_reason = h.reason
            self.store.raise_flag("halt", "halt", halt_reason,
                                  campaign_id=cid, session_id=self.session_id)
            if h.kind in ("login", "checkpoint"):
                # A distinct, stable kind (not just the generic "halt") so a
                # consumer like the agent-readiness check can query health_flags
                # directly instead of parsing the free-form halt detail text.
                self.store.raise_flag(f"instagram_{h.kind}_required", "halt", halt_reason,
                                      campaign_id=cid, session_id=self.session_id)
            self._emit("halt", "error", f"Halted: {halt_reason}",
                       {"reason": halt_reason})
            self._flush()
            self.store.end_session(self.session_id, "halted", halt_reason)
            raise

        return {
            "session_id": self.session_id,
            "reels_seen": self.counters.reels_seen,
            "relevance_passes": self.counters.relevance_passes,
            "matches": self.counters.matches,
            "escalations": self.counters.escalations,
            "already_seen_skips": self.counters.already_seen_skips,
            "spend_usd": self.counters.spend_usd,
            "feed_health_flag": self.counters.feed_health_flag,
            "likes": self.counters.likes,
            "follows": self.counters.follows,
            "halt_reason": halt_reason,
            "halt_kind": None,            # success path never halts; halt re-raises
        }


def run_session(*, campaign, store, router, feed, soul, pacer,
                session_id=None, run_id=None, lead_target=None) -> dict:
    """Drive ONE Instagram discovery session and return its summary dict.

    The control plane (dispatch) builds the (router, feed, pacer) triple and calls
    this. A HaltSession (daytime window closed, action-block, login/checkpoint,
    empty interception) is folded into the summary as ``halt_reason`` — the caller
    never sees the exception, so the run-loop aggregation stays uniform across
    engines. Any other exception propagates to the control plane unchanged.

    Uzbek STT (Instagram-only): when this campaign is gated on for it
    (``enable_stt`` AND "uz" in ``language_mix``), build the real transcriber and
    warm-load the KotibAI model HERE — before the Session/per-reel loop exists —
    so the (measured ~1.1s, but still not worth risking) cold GPU load never eats
    into any reel's per_reel_seconds deadline. A warm-load failure disables STT
    for this session only (soft-degrade, never blocks the run).
    """
    transcriber = None
    # getattr-defensive: some callers (e.g. test_halt_classification.py's
    # run_session(campaign=None, ...) contract test) legitimately pass no
    # campaign at all — this gate must not blow up before Session() even
    # gets a chance to run.
    stt_wanted = (getattr(campaign, "enable_stt", False)
                  and "uz" in {str(x).lower()
                               for x in getattr(campaign, "language_mix", [])})
    if stt_wanted:
        from ...core.transcribe import build_transcriber
        transcriber = build_transcriber()
        # build_transcriber() falls back to NullTranscriber (no .warm()) when the
        # global AIZU_STT_ENABLED kill-switch is off even though this campaign
        # asked for STT — hasattr guards that combination instead of assuming.
        if hasattr(transcriber, "warm"):
            try:
                transcriber.warm()   # cold GPU load happens HERE, not per-reel
            except RuntimeError as e:
                log.warning("STT warm-load failed, disabling for this session: %s", e)
                transcriber = None

    session = Session(store=store, router=router, feed=feed, soul=soul,
                      campaign=campaign, pacer=pacer, cfg=SessionConfig(),
                      session_id=session_id, run_id=run_id, lead_target=lead_target,
                      transcriber=transcriber)
    try:
        return session.run()
    except HaltSession as h:
        return {"session_id": session.session_id, "halt_reason": h.reason,
                "halt_kind": h.kind}
