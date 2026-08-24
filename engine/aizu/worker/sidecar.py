"""The worker pull loop (BUILD-PLAN §2.6).

    sweep orphans → register → loop { lease → single-flight → run (heartbeat thread) → ack/nack }

PULL only: long-poll the dispatch ``/lease``, run ONE job locally, post the result.
Control flags (``drain``/``halt``/``updateRequired``) ride the heartbeat *response*.

Halt model (Phase 6 supervised-subprocess): the leased job runs in a KILLABLE child
process (``job_runner.run_one_job`` supervises ``job_child``). A ``halt`` — operator
stop, a dispatch halt flag, or 3 failed heartbeats — now force-terminates the engine
MID-RUN (SIGTERM → grace → SIGKILL), not merely at the job boundary; the supervisor
also self-terminates a child that outlives its duration cap. ``drain`` still finishes
the current job then stops leasing. The single-flight lock is held by ``_handle_lease``
around the whole supervised lifetime, so a hard-killed child never leaks it.

Revocation model (ledger B10): a dispatch **401** on ANY worker-plane call may mean this
box's bearer token is dead — revoked from the panel, expired, or unknown to the dispatch it
is pointed at. Every 401 goes through ``_note_unauthorized``, which applies three guards
before anything irreversible happens, because destroying a box's only credential on a
single observation turns any transient server-side or ingress fault into a fleet-wide,
hand-repaired outage (adversarial review of the first cut):

1. **Whose token was refused.** If the persisted token has changed since the failing call
   presented its bearer, another process rotated the shared credential — the 401 is stale,
   so the box ADOPTS the stored token and carries on instead of deleting a valid one.
2. **How many in a row.** Only ``_UNAUTHORIZED_CONFIRM_LIMIT`` CONSECUTIVE 401s (any
   non-401 outcome resets the count) are taken as revocation.
3. **How LONG they have gone on.** A count on its own is worth whatever the retry cadence
   makes it worth, and the lease loop's `_backoff` is SUB-SECOND for its first attempts —
   so "three strikes" was, measured, **2.5 seconds** of wall clock. A ~9-second bridge
   restart on an empty DB therefore disenrolled a healthy box and cost an operator visit.
   The streak must ALSO have lasted ``_UNAUTHORIZED_CONFIRM_WINDOW_SEC`` (minutes), and
   every 401 retry is spaced by ``_unauthorized_retry_floor`` so those minutes are real
   elapsed time rather than a tight spin. A genuinely revoked box 401s forever and confirms
   a few minutes in; a bridge restarted against an empty or not-yet-mounted database —
   which fails closed to 401 for a perfectly valid token — heals long before that.

Launch preflight (ledger F9/F10/F12, B6): before this box takes any work it answers "can
this machine actually run a job?" — see ``preflight.py``. The report is refreshed AFTER the
control surface binds and BEFORE register, so a box with a red preflight and an unreachable
cloud still tells its operator WHY. Everything that touches the network at launch sits
behind that bind — F10 CDP port resolution included, since it probes two ports with real
socket timeouts (3.0s + 1.5s) and used to run in ``main()``, i.e. before a Sidecar (and
therefore before anything that could bind) existed at all. A FATAL failure never exits the
process and never refuses registration; it withholds CAPABILITIES (the box registers
honestly as "online, can take nothing") and parks the LEASE loop in
``_park_for_preflight``, re-probing every 30s until it heals. The park is reachable from
the loop as well as from launch: the report can turn blocking MID-LIFE (an operator's
manual re-check while Chrome restarts), and a box that noticed that only at launch would
withhold its capabilities on the next register and then lease nothing for the rest of the
process's life. Two independent stay-alive loops therefore exist in one process, and their
precedence is fixed: **re-enrolment (B10) is checked first and always wins** — revocation is
terminal until a human acts, preflight is self-healing, and getting that backwards either
spins a revoked box on preflight rechecks forever or makes a merely-misconfigured box log
the misleading re-enrolment CRITICAL.

:attr:`Result.is_unauthorized` supplies a fourth guard at the boundary: a 401 without the
dispatch's own envelope (a proxy's HTML) is never counted at all.

A CONFIRMED revocation is then answered exactly once, by ``_on_auth_revoked``: CLEAR the
persisted token (through :class:`TokenStore`, so the keyring backend is cleared too, never
a hard-coded path), log at CRITICAL naming the operator action, stop leasing, stop the
presence beat, and park the process with its control surface still serving. No IMMEDIATE
re-register: while ``AIZU_WORKER_LEGACY_BOOTSTRAP_ENABLED`` is on, a box that re-registered
itself off the shared bootstrap secret would resurrect the identity an operator just
revoked (B8) — the server refuses that (a legacy bootstrap register can no longer clear
`revoked_at`), and the parked box additionally re-probes only on the slow reminder cadence
so a genuinely server-side cause self-heals without a human. A transport error, timeout or
5xx keeps its existing retry/backoff behaviour, because turning a flaky network into a
bricked box is the worse bug.
"""
from __future__ import annotations

import platform
import random
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Optional

import httpx

from ..core.config import PER_ORG_CREDENTIAL_PLATFORMS
from ..core.logsetup import get_logger
from ..core.store import MAX_SYNC_LEADS, MAX_SYNC_SPEND_ROWS, Store
from ..secrets import SecretCipherError
from . import DEFAULT_HEARTBEAT_INTERVAL_SEC, job_runner, preflight, single_flight
from .config import JobSpec, JobSpecError, WorkerConfig
from .control_state import CurrentJobInfo
from .preflight import PreflightReport
from .job_runner import CampaignNotFound, SoulMissing
from .lease_client import LeaseClient
from .token_store import TokenStore

log = get_logger("aizu.worker.sidecar")

# Lease-miss backoff: jittered exponential, capped, to avoid a thundering herd on the
# single-writer dispatch DB (BUILD-PLAN risk #5).
_BACKOFF_BASE_SEC = 0.5
_BACKOFF_CAP_SEC = 30.0
_HEARTBEAT_FAIL_LIMIT = 3
# Floor on the heartbeat cadence so a misconfigured/compromised dispatch can't drive a
# busy-loop (security review H2). The real cadence (≈20s) comes from register.
_HEARTBEAT_MIN_SEC = 5.0

# Halt reasons that mean "don't bother retrying this job soon" (poison) vs. transient.
# `spend_cap` (job_runner.SPEND_CAP_REASON) is poison because spend only ever grows: a
# job refused for zero headroom would be refused identically on every remaining attempt,
# so retrying just burns leases before dead-lettering anyway.
_POISON_HALTS = ("needs reconnect", "unsupported", "not yet implemented", "spend_cap")

# Distinct nack reason for a failed credential fetch (SECURITY REVIEW CRITICAL/HIGH —
# the worker now pulls its per-org credential fresh instead of the server baking it into
# the job spec). Deliberately NOT one of _POISON_HALTS: the fetch can fail for a
# transient reason (a lease that expired in the gap since leasing, a dispatch blip), so
# this nacks like any other transient failure (requeue with backoff) rather than
# poisoning the job. Named explicitly so a failure here surfaces as ITS OWN, diagnosable
# halt kind instead of falling through to cli.py's generic "needs YOUTUBE_API_KEY in the
# environment" error, which matches no known halt kind and would silently retry to
# dead-letter.
CREDENTIAL_FETCH_FAILED_REASON = "credential_fetch_failed"

# How long a finished/crashed job's supervisor rendezvous files (spec/result/per-job
# log) are kept before the startup sweep reclaims them — long enough for an operator to
# inspect yesterday's failed job's log, short enough that disk stays bounded.
_JOB_FILE_RETENTION_SEC = 7 * 24 * 60 * 60

# --- revocation (ledger B10) --------------------------------------------------------
# The CONCRETE operator action, spelled out in the fatal log line — a box that says only
# "unauthorized" sends its operator hunting through docs. Mirrors the B8 runbook: the
# stored token is already gone by the time this prints, so all that is left is to hand the
# box a fresh enrolment token and restart it.
REENROLMENT_ACTION = (
    "mint a fresh per-worker enrolment token in the panel (Fleet → Add worker), put it in "
    "AIZU_WORKER_BOOTSTRAP_TOKEN (or the desktop app's dispatch-token secret) on this box, "
    "then restart the worker")
# How often the parked (revoked) process re-states the fatal line, so an operator who
# attaches to the logs late still sees WHY the box is idle instead of a silent hang. The
# same tick re-probes registration once (see park_for_reenrolment): cheap enough at one
# request per 5 min, and the only way a purely server-side cause recovers unattended.
_REENROLMENT_REMINDER_SEC = 300.0
# How many CONSECUTIVE dispatch 401s confirm a revocation. One is not proof: the bridge's
# worker auth fails closed, so a restart against an empty/unmounted DB, a restored
# snapshot, or a mistyped --db answers 401 to a perfectly valid token for as long as the
# blip lasts. A revoked box 401s forever and confirms within a couple of intervals; a blip
# clears first. Any non-401 outcome resets the count (see `_note_unauthorized`).
_UNAUTHORIZED_CONFIRM_LIMIT = 3
# ...and how long that streak must have LASTED. A count alone is not a duration: the lease
# loop's `_backoff` starts at ~0.5s, so three consecutive 401s were reachable in 2.5s of
# wall clock — measured — and a ~9s bridge restart on an empty DB was enough to delete a
# healthy box's only credential. Because the recovery for a per-worker enrolment token is a
# hand-minted token and an operator visit, the confirmation is bounded in TIME as well as
# in count: the FIRST 401 of the streak must be at least this old before the token is
# destroyed. Minutes, deliberately — no realistic server-side blip (restart, failover,
# volume mount, snapshot restore) outlasts it, while a genuinely revoked box simply parks a
# few minutes later than before, which costs nothing.
_UNAUTHORIZED_CONFIRM_WINDOW_SEC = 300.0
# Minimum spacing between two calls while a 401 streak is open, and the ceiling that
# spacing backs off to. This is the other half of the time bound: a window is meaningless
# if the retries spin sub-second inside it, and hammering a dispatch that is refusing us is
# pointless anyway. Applied as a FLOOR over the ordinary jittered `_backoff`, so nothing
# else about the loop's pacing changes.
_UNAUTHORIZED_RETRY_MIN_SEC = 30.0
_UNAUTHORIZED_RETRY_CAP_SEC = 120.0


@dataclass
class Controls:
    """Cross-thread control state set by the heartbeat thread, read by the loop.

    ``threading.Event`` instances are the mutable coordination primitives; the loop
    only reads them, the heartbeat thread only sets them."""

    drain: threading.Event = field(default_factory=threading.Event)
    halt: threading.Event = field(default_factory=threading.Event)
    update_required: threading.Event = field(default_factory=threading.Event)
    halt_reason: Optional[str] = None
    _reason_lock: threading.Lock = field(default_factory=threading.Lock)

    def signal_halt(self, reason: str) -> None:
        """FIRST caller to halt wins the reason — the heartbeat thread and the control
        surface can both call this; a lock keeps the first (most meaningful) reason from
        being silently clobbered by a later last-writer (review LOW #9)."""
        with self._reason_lock:
            if self.halt.is_set():
                return
            self.halt_reason = reason
            self.halt.set()


# How many new run_events this box ships per heartbeat. Bounded so a burst never blows
# the worker body cap (server re-caps at MAX_RUN_EVENTS_SYNC); the rest page next beat.
_RUN_EVENTS_PER_BEAT = 100

# How often a presence beat re-sends an UNCHANGED preflight report. The report rides a
# beat when it CHANGED — resending the same blob every ~20s would be pure noise on a box
# that has been green for a week — but never never: `record_worker_heartbeat` COALESCEs an
# omitted report, so a console could otherwise be showing a diagnosis from an unbounded
# time ago, which is the exact "looks healthy, is dead" confusion this work exists to end.
# Every 10th beat (~3.3 min at the default cadence) bounds that staleness cheaply.
_PREFLIGHT_RESEND_EVERY_BEATS = 10


class _HeartbeatThread(threading.Thread):
    """Posts a heartbeat every ``interval`` seconds while a job runs; folds the
    response control flags into ``controls``. Three consecutive failures are treated
    as a halt (the dispatch is unreachable; finish + nack rather than run blind).

    It also SHIPS the run's new run_events (the live activity feed) up on each beat: the
    engine writes them to this box's LOCAL store, invisible to the cloud until synced.
    A SEPARATE Store connection is opened inside ``run`` (same thread → sqlite's
    check_same_thread holds; WAL allows this read alongside the job thread's writes)."""

    def __init__(self, client: LeaseClient, worker_id: str, job_id: str,
                 interval: float, controls: Controls,
                 db_path: Optional[str] = None, run_id: Optional[str] = None,
                 on_unauthorized: Optional[
                     Callable[[str, Optional[str]], Optional[str]]] = None):
        super().__init__(name=f"heartbeat-{job_id}", daemon=True)
        self._client = client
        self._worker_id = worker_id
        self._job_id = job_id
        self._interval = _clamp_interval(interval)
        self._controls = controls
        self._db_path = db_path
        self._run_id = run_id
        # Ledger B10: reports a 401 up to the sidecar. Optional so the thread stays
        # constructible standalone (tests build it directly).
        self._on_unauthorized = on_unauthorized
        self._stop_event = threading.Event()

    def run(self) -> None:
        failures = 0
        # Own connection for reading local run_events (created in THIS thread).
        events_store = self._open_events_store()
        cursor = 0
        try:
            while not self._stop_event.wait(self._interval):
                body = {"workerId": self._worker_id, "jobId": self._job_id}
                batch, max_id = self._collect_run_events(events_store, cursor)
                if self._run_id:
                    body["runId"] = self._run_id
                if batch:
                    body["runEvents"] = batch
                res = self._client.heartbeat(self._job_id, body)
                # B10: a 401 here retires the box's token and stops it taking NEW work.
                # It deliberately does NOT signal this job's halt by itself — the running
                # child is left to the PRE-EXISTING three-strike rule below, which halts a
                # worker that cannot heartbeat because the server will reclaim its lease
                # and may re-dispatch the job elsewhere (running blind risks a double run).
                if res.is_unauthorized and self._on_unauthorized is not None:
                    adopted = self._on_unauthorized("job heartbeat", self._client.token)
                    if adopted:
                        # Another process rotated the shared credential mid-job — beat on
                        # with the live one rather than with a bearer already replaced.
                        self._client = self._client.with_token(adopted)
                failures = apply_heartbeat(self._controls, res, failures)
                # Advance the cursor only on a confirmed OK beat, so a failed post
                # re-ships the same events next time (at-least-once for the feed).
                if res.ok and batch:
                    cursor = max_id
                if self._controls.halt.is_set():
                    return  # dispatch unreachable or operator halt — stop beating
        finally:
            if events_store is not None:
                events_store.close()

    def _open_events_store(self):
        if not (self._db_path and self._run_id):
            return None
        try:
            return Store(self._db_path)
        except Exception as e:  # noqa: BLE001 — event sync is best-effort, never fatal
            log.warning("heartbeat could not open a store for run_events (%s)", e)
            return None

    def _collect_run_events(self, events_store, cursor: int) -> tuple:
        """Read local run_events newer than ``cursor`` → (wire batch, new cursor)."""
        if events_store is None or not self._run_id:
            return ([], cursor)
        try:
            rows = events_store.fetch_run_events(
                self._run_id, after_id=cursor, limit=_RUN_EVENTS_PER_BEAT)
        except Exception:  # noqa: BLE001 — a local read hiccup must not stop the beat
            log.warning("heartbeat run_events read failed (run=%s)", self._run_id,
                        exc_info=True)
            return ([], cursor)
        if not rows:
            return ([], cursor)
        batch = [{"seq": r["seq"], "phase": r["phase"], "level": r["level"],
                  "message": r["message"], "detail": r["detail"],
                  "sessionId": r["sessionId"], "createdAt": r["createdAt"]}
                 for r in rows]
        return (batch, max(r["id"] for r in rows))

    def stop(self) -> None:
        self._stop_event.set()


# Presence heartbeat is observational for LIVENESS: a failure is logged and retried,
# NEVER escalated to halt (LOCKED #9 — worker-level presence must not affect a RUNNING
# job). It DOES, however, relay a resolved drain/halt flag into `stop_leasing` so an
# idle box stops taking NEW work — that gates leasing only, never an in-flight run.
class _PresenceThread(threading.Thread):
    """Worker-LEVEL keepalive. Posts /api/worker/heartbeat every ``interval`` for the
    sidecar's whole lifetime (idle between jobs included), independent of the per-job
    ``_HeartbeatThread``. Best-effort; never raises. On a successful beat it folds the
    response's drain/halt into ``stop_leasing`` (gates NEW leasing only)."""

    def __init__(self, client: LeaseClient, worker_id: str, interval: float,
                 current_sessions: Callable[[], int],
                 stop_leasing: threading.Event,
                 on_unauthorized: Optional[
                     Callable[[str, Optional[str]], Optional[str]]] = None,
                 preflight_wire: Optional[
                     Callable[[], Optional[dict]]] = None):
        super().__init__(name="presence", daemon=True)
        self._client = client
        self._worker_id = worker_id
        self._interval = _clamp_interval(interval)
        self._current_sessions = current_sessions   # callable → live load at beat time
        self._stop_leasing = stop_leasing
        # Ledger B10: reports a 401 up to the sidecar. This is the ONE presence outcome
        # that is not merely observational — see run().
        self._on_unauthorized = on_unauthorized
        # Callable → the compact upstream preflight body (PreflightReport.to_upstream_wire)
        # or None. Optional so the thread stays constructible standalone (tests build it
        # directly), in which case no beat ever carries a report — exactly today's shape.
        self._preflight_wire = preflight_wire
        self._beats = 0
        self._last_preflight: Optional[dict] = None
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self._interval):
            payload = self._preflight_payload()
            try:
                body = {
                    "workerId": self._worker_id,
                    "currentSessions": self._current_sessions(),
                    "timestamp": time.time(),
                }
                if payload is not None:
                    body["preflight"] = payload
                res = self._client.presence(body)
            except Exception as e:  # noqa: BLE001 — presence is best-effort, never crash
                log.warning("presence heartbeat raised (continuing): %s", e)
                continue
            if not res.ok:
                # B10: a 401 is the ONE presence failure that is not observational — the
                # token is dead, not the network. It stops NEW leasing (which is exactly
                # what presence is already allowed to do) but still never touches a
                # RUNNING job, so LOCKED #9 holds.
                if res.is_unauthorized and self._on_unauthorized is not None:
                    adopted = self._on_unauthorized("presence heartbeat",
                                                    self._client.token)
                    if adopted:
                        # Another process rotated the shared credential — pick it up and
                        # keep beating rather than re-presenting a superseded bearer.
                        self._client = self._client.with_token(adopted)
                    elif self._stop_event.is_set():
                        # CONFIRMED revocation: `_on_auth_revoked` stopped this thread.
                        # Return NOW rather than loop — a parked box that kept beating
                        # would authenticate against the dispatch forever, silently
                        # (~4k rejected requests/day) while the UI reports it halted.
                        return
                    continue
                # Any other failure only de-noises the log — it must NOT stop leasing (a
                # flaky network is not a drain signal; LOCKED #9 keeps presence
                # non-coercive).
                log.warning("presence heartbeat failed (continuing): %s", res.error)
                continue
            if payload is not None:
                # Memoized only on an ACCEPTED beat: a report the dispatch never received
                # is not "sent", and marking it so would hide a changed diagnosis until
                # the next forced resend.
                self._last_preflight = payload
            apply_presence_flags(self._stop_leasing, res)

    def _preflight_payload(self) -> Optional[dict]:
        """This beat's ``preflight`` field, or None to omit it (§4.4 cadence). Omitted ⇒
        ``store.record_worker_heartbeat`` COALESCEs and keeps what is already stored, so
        omitting is always safe — it is never read as "no report".

        Called from THIS thread only, so ``_beats``/``_last_preflight`` need no lock."""
        if self._preflight_wire is None:
            return None
        self._beats += 1
        try:
            current = self._preflight_wire()
        except Exception as e:  # noqa: BLE001 — a diagnostic must never break the beat
            log.warning("could not read the preflight for a presence beat: %s", e)
            return None
        if current is None:
            return None      # the first preflight has not finished — say nothing yet
        if current != self._last_preflight:
            return current
        if self._beats % _PREFLIGHT_RESEND_EVERY_BEATS == 0:
            return current
        return None

    def stop(self) -> None:
        self._stop_event.set()


def apply_presence_flags(stop_leasing: threading.Event, res) -> None:
    """Fold one presence-heartbeat response into ``stop_leasing``. A resolved drain or
    halt sets it so the lease loop stops taking new jobs. Pure coordination logic (no
    threads/timing), so it is unit-tested directly. Only ever SETS the event — a later
    beat without the flag does not resume leasing (drain is a one-way door for this
    process; the box is meant to wind down)."""
    flags = res.data if isinstance(res.data, dict) else {}
    if flags.get("drain") or flags.get("halt"):
        stop_leasing.set()


def apply_heartbeat(controls: Controls, res, failures: int) -> int:
    """Fold one heartbeat response into ``controls``; return the running failure
    count. Pure coordination logic, unit-tested without threads or timing.

    Three consecutive failures are escalated to a halt (dispatch is unreachable, so
    the job finishes and is nacked rather than running blind, BUILD-PLAN risk #9)."""
    if not res.ok:
        failures += 1
        log.warning("heartbeat failed (%d/%d): %s",
                    failures, _HEARTBEAT_FAIL_LIMIT, res.error)
        if failures >= _HEARTBEAT_FAIL_LIMIT:
            controls.signal_halt("heartbeat_failed")
        return failures
    flags = res.data if isinstance(res.data, dict) else {}
    if flags.get("halt"):
        controls.signal_halt("halted")
    if flags.get("drain"):
        controls.drain.set()
    if flags.get("updateRequired"):
        controls.update_required.set()
    return 0


class Sidecar:
    """One worker process: registers, then pulls and runs jobs until drained."""

    def __init__(self, cfg: WorkerConfig, *,
                 client: Optional[LeaseClient] = None,
                 store: Optional[Store] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 preflight_fn: Optional[
                     Callable[[WorkerConfig], PreflightReport]] = None):
        self._cfg = cfg
        # Own the httpx.Client at the sidecar level so its connection pool is closed
        # exactly once — `with_token` hands out clones that share (don't own) it, so a
        # rebind never orphans an open pool (code review H-A).
        self._http: Optional[httpx.Client] = None
        if client is None:
            self._http = httpx.Client(timeout=cfg.request_timeout_sec)
            self._client = LeaseClient(cfg.dispatch_base_url, client=self._http)
        else:
            self._client = client
        self._tokens = TokenStore(cfg.state_dir)
        self._store = store or Store(cfg.db_path)
        self._sleep = sleep
        # MONOTONIC by default: the revocation confirmation is a duration, and a wall-clock
        # source would let an NTP step (or a laptop resuming from sleep) jump a 401 streak
        # straight past its window and brick a healthy box. Injectable purely so tests can
        # drive the multi-minute window as virtual time.
        self._clock = clock
        self._worker_id: Optional[str] = None
        self._heartbeat_interval = _clamp_interval(cfg.heartbeat_interval_sec)
        # Worker-LEVEL presence keepalive (separate from the per-job heartbeat).
        self._presence_thread: Optional[_PresenceThread] = None
        # Live load reported in presence beats; bumped around an in-flight job. The
        # loop thread mutates it inside the lock and the presence thread reads it via
        # `active_jobs` under the same lock — never a bare cross-thread int (code review
        # H-A / global immutability rule: no unguarded shared mutation).
        self._active_jobs_lock = threading.Lock()
        self._active_jobs: int = 0
        # Set by the presence thread when a resolved drain/halt flag arrives, read by
        # the lease loop to STOP LEASING NEW JOBS. It gates only new leasing — a running
        # job stays governed by its own job heartbeat (LOCKED #9: worker-level presence
        # must never interrupt an in-flight run). This is what lets an IDLE box react to
        # drain/halt instead of leasing until its next job's heartbeat carries the flag.
        self._stop_leasing = threading.Event()
        # Operator PAUSE (distinct from drain): a resumable "stop leasing NEW jobs" the
        # local control surface toggles. Unlike _stop_leasing (drain = terminal), the
        # loop IDLES while paused and resumes when cleared. Off by default → no behavior
        # change for the existing suite / a headless box.
        self._paused = threading.Event()
        # Ledger B10. Set (once) when the dispatch answered 401 on any worker-plane call:
        # the persisted token has been cleared and this box needs a fresh enrolment token
        # before it can work again. Terminal for the pull loop, but NOT for the process —
        # the control surface keeps serving so the desktop app can say why the box is idle
        # instead of showing a silent hang. The lock makes the clear-log-stop sequence
        # run exactly once even though the loop, presence and heartbeat threads can all
        # observe the same 401 concurrently.
        self._reenrolment_required = threading.Event()
        self._reenrolment_lock = threading.Lock()
        # Consecutive dispatch 401s (any route) and the monotonic timestamp of the FIRST
        # one in the current streak — the streak's count AND its age both have to clear
        # their thresholds before the token is destroyed. Guarded by _reenrolment_lock —
        # the lease loop, the presence thread and a job's heartbeat thread all report into
        # it. Reset together by `_note_authorized`.
        self._auth_failures = 0
        self._auth_streak_started_at: Optional[float] = None
        # The live job's identity + its Controls, guarded by the SAME _active_jobs_lock
        # (one lock per related-state cluster). The control surface reads current_job and
        # signals the active job's halt through these — never a bare cross-thread touch.
        self._current_job: Optional["CurrentJobInfo"] = None
        self._active_controls: Optional[Controls] = None
        self._control_server = None  # ThreadingHTTPServer when the surface is enabled
        self._control_thread: Optional[threading.Thread] = None
        # --- launch preflight (F9/F10/F12, B6) ---------------------------------------
        # The callable is injected so the whole suite (and the desktop's manual re-check)
        # can drive a scripted report with no browser, no keychain and no network. It
        # defaults to `self._default_preflight` rather than to `preflight.run_preflight`
        # itself because the real probes need something only a LIVE sidecar knows: whether
        # a job is running right now. Without that, `check_cdp_attachable` would open a
        # SECOND CDP client against a browser an in-flight run already owns — the exact
        # single-browser violation check_readiness is careful about.
        self._preflight_fn = preflight_fn or self._default_preflight
        # None means "the first run has not finished". Both UIs render that as 'checking…',
        # never as healthy. Guarded because the control surface, the presence beat and the
        # (detached) manual re-check all read it while the loop thread writes it.
        self._preflight: Optional[PreflightReport] = None
        self._preflight_lock = threading.Lock()
        # The in-flight detached re-check, so an operator leaning on the wizard's
        # "Re-check" button coalesces into ONE run instead of stacking N concurrent
        # Playwright attaches against the same Chrome.
        self._preflight_thread: Optional[threading.Thread] = None
        # What the LAST successful register actually advertised. Only ever read to decide
        # whether a mid-life block still has capabilities to WITHDRAW — `register_worker`
        # REPLACES capabilities and rotates worker_token_hash, so the withdrawal has to
        # fire on the transition and only on the transition (F9.1). Loop-thread only.
        self._registered_capabilities: list = []

    @property
    def active_jobs(self) -> int:
        """Thread-safe read of the in-flight job count (presence thread reads this)."""
        with self._active_jobs_lock:
            return self._active_jobs

    @property
    def current_job(self) -> Optional["CurrentJobInfo"]:
        """Thread-safe read of the live job's identity (control surface reads this).
        Returns the immutable snapshot object or None when idle."""
        with self._active_jobs_lock:
            return self._current_job

    @property
    def reenrolment_required(self) -> bool:
        """True once a dispatch 401 has retired this box's token (ledger B10). Read by the
        control surface (so the desktop app can SAY so) and by ``main`` (so the process
        parks instead of exiting and being crash-looped by its supervisor)."""
        return self._reenrolment_required.is_set()

    def _note_unauthorized(self, call: str,
                           presented: Optional[str] = None) -> Optional[str]:
        """Fold ONE dispatch 401 into this box's revocation state (ledger B10). The ONLY
        route to ``_on_auth_revoked`` — nothing else may destroy the credential.

        A 401 is not proof of revocation on its own, so three guards stand in front of the
        irreversible act (see the module docstring for the full rationale):

        * ``presented`` is the bearer the FAILING call actually sent. If the token store
          now holds a different one, another process (a second sidecar sharing this
          ``AIZU_WORKER_STATE``, or a restart that overlapped a long-poll) rotated the
          credential and THIS 401 is stale. Adopt the stored token and keep working —
          clearing here would delete the other process's brand-new valid token.
        * ``_UNAUTHORIZED_CONFIRM_LIMIT`` consecutive 401s are required. Any non-401
          outcome resets the count via ``_note_authorized``.
        * The streak must ALSO span ``_UNAUTHORIZED_CONFIRM_WINDOW_SEC``. Count without
          duration is what let a 2.5-second blip brick a box: the retry cadence, not the
          threshold, decided how much wall clock "three strikes" was worth. Callers space
          their 401 retries by ``_unauthorized_retry_floor`` so the window is spent waiting,
          not spinning.

        Returns the token to ADOPT when the credential was rotated underneath us (callers
        holding their own client rebind to it), else None. Callers that must know whether
        the box halted read ``reenrolment_required`` — it is set before this returns."""
        with self._reenrolment_lock:
            if self._reenrolment_required.is_set():
                return None  # already halted — one 401 storm, one clear, one log line
            stored = self._peek_token()
            if stored and presented and stored != presented:
                self._auth_failures = 0
                self._auth_streak_started_at = None
                self._client = self._client.with_token(stored)
                log.warning(
                    "dispatch refused a STALE worker token on %s (HTTP 401) — the "
                    "persisted credential has since been rotated (another worker process "
                    "shares this state dir); adopting it instead of clearing", call)
                return stored
            now = self._clock()
            if self._auth_failures == 0 or self._auth_streak_started_at is None:
                self._auth_streak_started_at = now
            self._auth_failures += 1
            failures = self._auth_failures
            elapsed = max(0.0, now - self._auth_streak_started_at)
            if (failures < _UNAUTHORIZED_CONFIRM_LIMIT
                    or elapsed < _UNAUTHORIZED_CONFIRM_WINDOW_SEC):
                log.warning(
                    "dispatch refused this worker's token on %s (HTTP 401, %d/%d over "
                    "%.0fs of %.0fs) — treating as TRANSIENT until it is BOTH repeated "
                    "and sustained; the token is kept and the call retried (a bridge on "
                    "an empty/rolled-back DB 401s a valid token too)",
                    call, failures, _UNAUTHORIZED_CONFIRM_LIMIT,
                    elapsed, _UNAUTHORIZED_CONFIRM_WINDOW_SEC)
                return None
        # Outside the lock: `_on_auth_revoked` takes it itself (and is idempotent, so a
        # second thread arriving here at the same instant is harmless).
        self._on_auth_revoked(call)
        return None

    def _note_authorized(self) -> None:
        """Reset the consecutive-401 count. Called on any dispatch outcome that was NOT a
        401 — the counter means 'in a row', so a single accepted (or merely differently
        failing) call proves the box is not being rejected outright. Takes the lock
        unconditionally: the presence thread can be incrementing concurrently, and each
        caller is already one HTTP round-trip apart, so the contention is irrelevant."""
        with self._reenrolment_lock:
            self._auth_failures = 0
            self._auth_streak_started_at = None

    def _unauthorized_retry_floor(self) -> float:
        """Minimum delay before the NEXT call while a 401 streak is open (0.0 when there is
        none). Applied by the retry sites as a floor over the ordinary jittered backoff.

        Two jobs: it makes ``_UNAUTHORIZED_CONFIRM_WINDOW_SEC`` a real wait instead of a
        spin — the count guard alone was worth 2.5s of wall clock because `_backoff` is
        sub-second early on — and it stops a box that is being refused from hammering a
        dispatch that has nothing else to say. Backs off with the streak so a longer
        outage costs proportionally fewer requests, capped so recovery stays prompt."""
        with self._reenrolment_lock:
            failures = self._auth_failures
        if failures <= 0:
            return 0.0
        ceiling = min(_UNAUTHORIZED_RETRY_CAP_SEC,
                      _UNAUTHORIZED_RETRY_MIN_SEC * (2 ** (failures - 1)))
        return random.uniform(_UNAUTHORIZED_RETRY_MIN_SEC,
                              max(_UNAUTHORIZED_RETRY_MIN_SEC, ceiling))

    def _peek_token(self) -> Optional[str]:
        """Read the persisted token without the repair side effects of
        ``_load_token_safely`` — a 401 must never itself clear an unreadable token."""
        try:
            return self._tokens.load()
        except Exception:  # noqa: BLE001 — unreadable is "unknown", never "clear it"
            return None

    def _on_auth_revoked(self, call: str) -> None:
        """Answer a CONFIRMED dispatch revocation (ledger B10) — reached only through
        ``_note_unauthorized``, which supplies the corroboration.

        Clears the persisted token THROUGH the token store (so the keyring backend is
        cleared as well; never an unlink of a hard-coded path), logs at CRITICAL with the
        concrete operator action, and stops the pull loop by way of the existing
        ``_stop_leasing`` gate — which every loop already checks, and which leaves an
        in-flight job alone.

        It deliberately does NOT re-register on the spot: the shared bootstrap secret is
        very likely still valid on this box (AIZU_WORKER_LEGACY_BOOTSTRAP_ENABLED defaults
        ON), so an immediate re-register would hand the box a brand-new working identity
        seconds after an operator revoked it. The server now refuses that outright (a
        legacy bootstrap register can no longer clear a revoked row), and the parked
        process re-probes only on the slow reminder cadence — see
        ``park_for_reenrolment``."""
        with self._reenrolment_lock:
            if self._reenrolment_required.is_set():
                return  # already handled — one 401 storm, one log line, one clear
            self._reenrolment_required.set()
            try:
                self._tokens.clear()
                cleared = True
            except Exception as e:  # noqa: BLE001 — a keyring clear can fail; still halt
                cleared = False
                log.error("could not clear the persisted worker token: %s", e)
            # `critical`, not `error`: this is the one worker-plane condition no retry can
            # resolve, and the box does nothing further until a human acts.
            log.critical(
                "Dispatch REJECTED this worker's token on %s (HTTP 401) SUSTAINED for "
                "over %.0fs — the box is revoked or its token expired. Stored token %s; "
                "NOT re-registering (that would resurrect a revoked box off the shared "
                "bootstrap secret). Pull loop stopped — to bring this box back: %s",
                call, _UNAUTHORIZED_CONFIRM_WINDOW_SEC,
                "cleared" if cleared else "COULD NOT BE CLEARED — remove it by hand",
                REENROLMENT_ACTION)
            self._stop_leasing.set()
            # The box will never lease again, so liveness reporting has no consumer: stop
            # the presence beat too. SET only, never join — the caller may BE the presence
            # thread. Without this a parked box keeps authenticating against the dispatch
            # forever (one rejected POST per interval, ~4k/day) while every UI reports it
            # halted; a later successful re-register starts a fresh thread anyway.
            presence = self._presence_thread
            if presence is not None:
                presence.stop()

    def pause(self) -> None:
        """Operator pause — stop leasing NEW jobs; a running job is untouched. Resumable."""
        self._paused.set()

    def resume(self) -> None:
        """Clear an operator pause so the loop leases again."""
        self._paused.clear()

    def request_stop_current_job(self) -> bool:
        """Signal the live job to HARD-STOP. With the supervised-subprocess model this
        sets the same halt Event the supervisor polls, so the child is terminated mid-run
        (the sidecar then nacks it 'operator_stop'). Returns False when nothing is live."""
        with self._active_jobs_lock:
            controls = self._active_controls
        if controls is None:
            return False
        controls.signal_halt("operator_stop")
        return True

    # --- launch preflight (F9/F10/F12, B6) -------------------------------------

    def _default_preflight(self, cfg: WorkerConfig) -> PreflightReport:
        """The real preflight with THIS box's live state wired into its probes.

        ``run_active`` is the only probe a bare `run_preflight(cfg)` cannot supply, and it
        is load-bearing: `check_browser` must SKIP while a job is running rather than open
        a second connect_over_cdp against the browser that run already owns (the
        single-browser invariant `check_readiness` enforces on the panel side). A manual
        re-check from the desktop UI mid-job is exactly the case that would violate it."""
        return preflight.run_preflight(
            cfg, probes=preflight.Probes(run_active=lambda: self.active_jobs > 0))

    @property
    def preflight(self) -> Optional[PreflightReport]:
        """The last report, or None while the first run is still in flight."""
        with self._preflight_lock:
            return self._preflight

    def preflight_wire(self) -> Optional[dict]:
        """The control-surface `/status` shape (full detail + remedy — it never leaves the
        box), or None when no report exists yet."""
        report = self.preflight
        return report.to_wire() if report is not None else None

    def preflight_upstream_wire(self) -> Optional[dict]:
        """The compact shape that rides the register/presence body (§5.1), or None when
        there is no report yet or it would blow MAX_UPSTREAM_BYTES."""
        report = self.preflight
        return report.to_upstream_wire() if report is not None else None

    def _refresh_preflight(self) -> PreflightReport:
        """Run the preflight once and publish it. NEVER raises.

        ``run_preflight`` already guards every individual check, so reaching the except
        arm means the COMPOSITION (or an injected callable) blew up. That is answered with
        `error_report`, a warn-only report — so even a bug in this machinery leaves the box
        leasing at full capabilities instead of parking a healthy machine. Deliberately
        does NOT log: the callers decide (launch logs once, the park tick logs every 30s,
        a manual re-check logs the operator's own action)."""
        previous = self.preflight
        try:
            report = self._preflight_fn(self._cfg)
        except Exception as e:  # noqa: BLE001 — our own bug is a warning, never a park
            log.warning("the launch preflight itself raised %s — the worker continues "
                        "UNBLOCKED", type(e).__name__, exc_info=True)
            # Carry the previously-observed enforcement state forward when we have one, so
            # a break-glass box does not silently start claiming enforcement is on.
            report = preflight.error_report(
                e, enforced=previous.enforced if previous is not None else True)
        with self._preflight_lock:
            self._preflight = report
        return report

    def _preflight_blocking(self) -> bool:
        """True iff a FATAL check actually failed and enforcement is on. A report that has
        not run yet is NOT blocking — the box is mid-launch, not condemned."""
        report = self.preflight
        return report is not None and report.blocking

    def _effective_capabilities(self) -> list:
        """What this box advertises to dispatch RIGHT NOW.

        Capability withholding is the actual enforcement (§3.1): the box still registers,
        still heartbeats and still serves its control surface, but `store.register_worker`
        REPLACES capabilities — so an empty list means `_job_capability_covers` can never
        match it and `fleet_readiness` refuses to count it. All-or-nothing, not per-platform
        masking: a mixed instagram+youtube box with Chrome down goes fully dark rather than
        quietly advertising half of itself, because the operator's remedy is identical
        either way and a reviewer only has one rule to hold."""
        if self._preflight_blocking():
            return []
        return list(self._cfg.capabilities)

    def request_preflight(self) -> bool:
        """Operator-triggered re-check (control surface ``runPreflight``). Detached and
        COALESCED — always 'accepted', never 'completed': a full pass can take ~9.5s,
        far past the desktop client's 3s timeout, and the operator's feedback is the next
        1500ms `/status` poll. Coalescing matters because the wizard's Sign-in step polls
        this while a human logs in; stacking attaches would put several CDP clients on one
        Chrome, which is the failure mode `check_browser` refuses to create itself."""
        with self._preflight_lock:
            running = self._preflight_thread
            if running is not None and running.is_alive():
                return True
            thread = threading.Thread(target=self._preflight_worker,
                                      name="preflight", daemon=True)
            self._preflight_thread = thread
        thread.start()   # started OUTSIDE the lock — the body takes it too
        return True

    def _preflight_worker(self) -> None:
        try:
            self._refresh_preflight().log_to(log)
        except Exception as e:  # noqa: BLE001 — a detached thread must never crash silently
            log.warning("the detached preflight re-check failed: %s", e)

    def open_login_tab(self, platform: str) -> bool:
        """Open (or focus) a login tab for ``platform`` in the warmed Chrome so the human
        sitting at this box can sign in — the LOCAL half of the handoff
        ``/api/agent/launch-login`` has always promised in distributed mode and nothing
        delivered (the warmed Chrome is on the worker PC; there is no inbound path to it
        from the cloud). Detached → 'accepted', same contract as `request_preflight`.

        There is no completion signal by design: the operator finishes the login and any
        2FA in the real Chrome window and the NEXT preflight reads the cookie jar. The
        wizard reads the browser, never an "I'm done" button."""
        threading.Thread(target=self._login_tab_worker, args=(platform,),
                         name=f"login-tab-{platform}", daemon=True).start()
        return True

    def _login_tab_worker(self, platform: str) -> None:
        if self.active_jobs > 0:
            # Same single-browser invariant as check_browser: a live run owns the one CDP
            # connection this architecture allows. Navigating its browser mid-harvest is
            # worse than making the operator wait for the job to finish.
            log.warning("not opening a %s login tab — a job is running and owns the "
                        "warmed Chrome; retry when the box is idle", platform)
            return
        try:
            from .. import readiness
            opened = readiness.open_login_tab(self._cfg.cdp_url, platform=platform)
        except Exception as e:  # noqa: BLE001 — a detached command never crashes a thread
            log.warning("could not open a %s login tab: %s", platform, e)
            return
        if opened:
            log.info("Opened a %s login tab in the warmed Chrome at %s — finish the login "
                     "(and any 2FA) there; the next preflight reads the session",
                     platform, self._cfg.cdp_url)
        else:
            log.warning("could not open a %s login tab at %s — is the warmed Chrome "
                        "running and attachable?", platform, self._cfg.cdp_url)

    # --- lifecycle -------------------------------------------------------------

    def run(self, *, max_iterations: Optional[int] = None) -> None:
        """Run the pull loop. ``max_iterations`` bounds it for tests; ``None`` runs
        until a drain flag is seen."""
        self._startup_sweep()
        # Control surface FIRST — the local desktop UI must reflect THIS box's state even
        # when the cloud dispatch is unreachable or registration hasn't succeeded yet
        # (otherwise switching dispatch = a silent "disconnected" with no local feedback).
        self.start_control_surface()
        # F10 port adoption, BEHIND the bind. An unpinned box whose warmed Chrome came up
        # on the sibling port adopts it here (with a logged receipt the drift check then
        # shows), so it works unattended instead of parking over a port literal; a box that
        # PINNED AIZU_CDP_URL is never overridden — it gets a named fatal instead.
        # It ran in `main()` before the Sidecar object existed, which put up to 4.5s of
        # blocking socket probes (3.0s + 1.5s) in front of the control surface — precisely
        # the launch window in which the desktop app has nothing to show but a refused
        # connection, on precisely the box (Chrome down) whose operator most needs the
        # reason. Still BEFORE the preflight, so the checks read the adopted URL.
        self._cfg = _resolve_cdp_url(self._cfg)
        # Preflight AFTER the surface binds (so the very first /status poll the desktop
        # makes already carries a report, and a box with a red preflight AND an unreachable
        # cloud still tells its operator why) and BEFORE register (so registration carries
        # HONEST capabilities on the very first call, instead of advertising work this box
        # cannot do and dead-lettering five leases proving it).
        self._refresh_preflight().log_to(log)
        if not self._register():
            if self._reenrolment_required.is_set():
                return  # revoked (B10) — _on_auth_revoked already logged the action
            log.error("Worker registration failed — control surface stays up; retrying")
            # Stay alive and retry on the presence cadence so a transient cloud outage or a
            # just-switched dispatch recovers WITHOUT a full process restart. Bounded by
            # max_iterations for tests.
            self._await_registration(max_iterations=max_iterations)
        if self._worker_id is None:
            # Never registered. When the preflight is BLOCKING that is very likely the
            # reason (an unwritable state dir cannot even mint a machine id), so park and
            # keep re-checking instead of exiting: returning here would hand a supervisor
            # a clean rc=0 for what is really a config problem, which F8 measured as
            # "never restarted again". Parking keeps the control surface serving, so the
            # operator can still read the failing check and its remedy, and the box
            # registers itself the moment they fix it.
            if not self._preflight_blocking():
                return  # stopped or test-bounded — nothing to lease
            if not self._park_for_preflight(max_waits=max_iterations):
                return
        if self._preflight_blocking():
            # Registered, visible, honest — and holding no capabilities, so nothing can be
            # leased to it. Park until it heals rather than exit: F8 measured that a
            # sidecar returning rc=0 makes a supervisor treat a config problem as a clean
            # shutdown and never restart, and F9.1 that crash-on-fatal rotates
            # worker_token_hash every supervisor iteration.
            if not self._park_for_preflight(max_waits=max_iterations):
                return
        self._loop(max_iterations=max_iterations)

    def _park_for_preflight(self, *, max_waits: Optional[int] = None) -> bool:
        """Stay alive with a BLOCKING preflight, re-probing every
        ``preflight.RECHECK_INTERVAL_SEC``. Returns True once the report cleared AND the
        box re-registered with its real capabilities (the caller then leases); False when
        it was stopped, revoked, or bounded out by ``max_waits``.

        The worst outcome of a WRONG fatal is therefore a 30-second pause, not an operator
        visit — which is the whole reason fatal is allowed to exist on machines nobody can
        SSH into. The cadence is flat on purpose (a genuinely broken box repeats its ERROR
        block forever): the log IS the diagnostic on a headless PC.

        ``max_waits`` bounds it for tests; ``None`` parks until the process is signalled."""
        waits = 0
        while max_waits is None or waits < max_waits:
            waits += 1
            # B10 FIRST, always (§3.4). Revocation is terminal until a human acts and
            # preflight is self-healing; getting the precedence backwards either spins a
            # revoked box on preflight rechecks forever or makes a merely-misconfigured box
            # log the misleading re-enrolment CRITICAL. `main()`'s park loop takes over.
            if self._reenrolment_required.is_set():
                return False
            if self._stop_leasing.is_set():
                return False
            try:
                self._sleep(preflight.RECHECK_INTERVAL_SEC)
            except KeyboardInterrupt:  # Ctrl-C / supervisor stop — leave cleanly
                return False
            # Safe ONLY here: parking means nothing is leasing and no job is live, so
            # re-pointing the box at a Chrome that came up on the sibling port cannot pull
            # the browser out from under a running engine. WorkerConfig is frozen, so this
            # swaps one immutable object for another — a concurrent reader (the control
            # surface reads _cfg.cdp_url) sees the old or the new one, never a torn one.
            self._cfg = _resolve_cdp_url(self._cfg)
            report = self._refresh_preflight()
            report.log_to(log)
            if report.blocking:
                continue
            # Re-register ONLY on the blocking→clear transition, never every tick:
            # `register_worker` rotates worker_token_hash, and a Chrome that restarts in a
            # loop must not rotate this box's credential every 30 seconds.
            if self._register():
                log.success("Preflight cleared — re-registered with this box's real "
                            "capabilities and resuming work")
                return True
            log.error("Preflight cleared but re-register failed — staying parked so the "
                      "fleet never sees capabilities it cannot honour; retrying")
        return False

    def _withdraw_and_park(self, *, max_waits: Optional[int] = None) -> bool:
        """The MID-LIFE half of the park: the report turned blocking after this box was
        already registered and working. Withdraw the capabilities it is still advertising,
        then take the same self-healing ``_park_for_preflight`` loop the launch path takes.
        Returns exactly what that park returns.

        `_effective_capabilities` is read at REGISTER time and nowhere else, so before this
        existed a report that went blocking mid-life (an operator's manual re-check landing
        while Chrome restarts) changed NOTHING until the next register — and if that
        register happened to be the B10 re-probe's, the box came back with an empty
        capability set and then leased nothing for the rest of the process's life, with no
        tick anywhere that would ever look at the report again. A one-way door, healed only
        by a restart on a machine nobody can SSH into.

        The withdrawal is one register on the clear→blocking TRANSITION only, mirroring the
        park's own blocking→clear register: doing it every tick is the F9.1 token-rotation
        pathology, and not doing it at all leaves `fleet_readiness` answering ready:true for
        a box that has quietly stopped leasing — the "looks healthy, is dead" lie this whole
        feature exists to end."""
        if self._registered_capabilities:
            log.warning(
                "The launch preflight went BLOCKING while this box was working — "
                "withdrawing the capabilities it is advertising and parking until it "
                "heals (nothing exits; the control surface keeps serving the reason)")
            self._register()   # never raises; its own log names the failing checks
        return self._park_for_preflight(max_waits=max_waits)

    def _await_registration(self, *, max_iterations: Optional[int]) -> None:
        """Keep the control surface serving while retrying register with jittered backoff
        until it succeeds or the process is stopped (drain/halt via ``_stop_leasing``).
        ``_worker_id`` is set by a successful ``_register``; the caller checks it."""
        attempt = 0
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            iterations += 1
            if self._stop_leasing.is_set():
                return
            # Same floor as the lease loop: while register is answering 401 the retries are
            # spaced by minutes, so `_UNAUTHORIZED_CONFIRM_WINDOW_SEC` measures a real
            # sustained outage. A non-401 registration failure keeps its old fast backoff.
            self._sleep(max(_backoff(attempt), self._unauthorized_retry_floor()))
            attempt += 1
            if self._register():
                return

    def park_for_reenrolment(self, *, max_waits: Optional[int] = None) -> bool:
        """Stay alive after a revocation halt (ledger B10), re-probing registration once
        per reminder tick. Returns True iff a probe re-registered — the caller then
        resumes leasing (``resume_leasing``); False means the park was interrupted.

        The process deliberately does NOT exit: a desktop/systemd supervisor treats a dead
        sidecar as a crash and restarts it, which would just re-run the same doomed
        register in a loop. Parking instead keeps the local control surface serving — so
        the operator sees ``reenrolmentRequired`` and the reason in the app rather than a
        silent hang — and re-states the fatal line periodically for whoever tails the log
        later.

        The re-probe is what keeps parking from being a one-way door for a fault that was
        never this box's: a bridge that answered 401 because its DB was mid-restore starts
        accepting again, and the box comes back by itself instead of waiting for someone
        to notice. It cannot resurrect a genuinely revoked box — the server refuses a
        legacy bootstrap register for a revoked row, and an enrolment token is single-use —
        so a real revocation simply re-parks, once per 5 minutes.

        ``max_waits`` bounds it for tests; ``None`` parks until the process is signalled."""
        waits = 0
        while max_waits is None or waits < max_waits:
            waits += 1
            log.critical("Worker is HALTED pending re-enrolment — %s", REENROLMENT_ACTION)
            try:
                self._sleep(_REENROLMENT_REMINDER_SEC)
            except KeyboardInterrupt:  # Ctrl-C / supervisor stop — leave cleanly
                return False
            if self._reprobe_registration():
                return True
        return False

    def _reprobe_registration(self) -> bool:
        """One registration attempt from the parked state. Lifts the halt for the duration
        of the probe (``_register`` refuses to present a credential otherwise) and puts it
        straight back — with the 401 counter reset, so a still-revoked box re-parks on the
        very next rejection rather than needing another full confirmation run."""
        with self._reenrolment_lock:
            self._auth_failures = 0
            self._auth_streak_started_at = None
            self._reenrolment_required.clear()
            self._stop_leasing.clear()
        if self._register():
            log.success("Dispatch accepted this worker again — resuming normal work "
                        "(the earlier 401s were not a revocation after all)")
            return True
        with self._reenrolment_lock:
            self._reenrolment_required.set()
            self._stop_leasing.set()
        return False

    def resume_leasing(self, *, max_iterations: Optional[int] = None) -> None:
        """Re-enter the pull loop after ``park_for_reenrolment`` re-registered this box.
        Distinct from ``run()``, which also sweeps orphans and binds the control surface —
        both already done once for this process."""
        self._loop(max_iterations=max_iterations)

    def _startup_sweep(self) -> None:
        # First: kill any job child a PRIOR sidecar crash orphaned (still alive with no
        # supervisor/lease). Must run BEFORE we lease, so we never race a zombie child for
        # the same account (the one-account/one-box/one-live-job invariant).
        job_runner.reap_orphan_children(self._cfg.state_dir)
        # A still-running job refreshes nothing, so reclaim only what is older than a
        # full worst-case job plus a margin — never a live run's sentinel/lock.
        max_age = self._cfg.max_job_minutes * 60 + 2 * self._heartbeat_interval
        job_runner.sweep_orphan_pause_files(self._cfg.state_dir, max_age_sec=max_age)
        single_flight.sweep_stale(self._cfg.state_dir, max_age_sec=max_age)
        # Supervisor rendezvous files (spec/result/per-job log) a crashed sidecar could
        # leave. Kept a bit longer than a run's liveness window so a just-finished job's
        # log survives for operator postmortem, but still bounded so disk can't grow.
        job_runner.sweep_orphan_job_files(
            self._cfg.state_dir, max_age_sec=_JOB_FILE_RETENTION_SEC)

    def _register(self) -> bool:
        """Register this box. NEVER raises — a failure is always a False return.

        The guard is not defensive padding: `cfg.machine_id` WRITES `machine-id` into the
        state dir, so an unwritable `AIZU_WORKER_STATE` raises PermissionError right here,
        several frames below `run()`. That is the exact crash `state_dir_writable` exists
        to pre-empt, and before this guard the preflight would correctly name the problem
        and its remedy and then the process would die anyway on the very next line —
        taking the control surface (the only thing that could show an operator that
        report) down with it, and handing a supervisor a crash-loop. Everything a fatal
        check reports must end in a park, never in an exit (F8/F9.1)."""
        try:
            return self._register_unguarded()
        except Exception as e:  # noqa: BLE001 — a raising register must PARK, never exit
            log.error(
                "Registration attempt raised %s: %s — the control surface stays up and "
                "the box re-checks on the preflight cadence. This is almost always the "
                "condition the preflight already named above; fix that and it recovers "
                "with no restart.", type(e).__name__, e)
            return False

    def _register_unguarded(self) -> bool:
        existing = self._load_token_safely()
        # Token precedence for the register call's bearer: a persisted per-worker token
        # (re-register / rotate) wins; otherwise the shared bootstrap secret authorises
        # a first register (the real server requires SOME valid bearer — the no-auth
        # stub hid this). The bootstrap secret is used transiently and never persisted.
        if existing:
            self._client = self._client.with_token(existing)
        elif self._cfg.bootstrap_token:
            self._client = self._client.with_token(self._cfg.bootstrap_token)
        else:
            log.warning("No stored worker token and no bootstrap token configured — "
                        "first register will be rejected by an authenticated dispatch")
        body = {
            "machineId": self._cfg.machine_id,
            "displayName": platform.node(),
            # `workers.host` was NULL for every box in the fleet because this body never
            # carried the field the server has always accepted. It is not purely cosmetic:
            # the B8 cutover's completion query is `SELECT id, host FROM workers WHERE
            # revoked_at IS NULL AND enrolment_scope_kind IS NULL`, and a column of NULLs
            # tells the operator which boxes are outstanding but not WHICH MACHINES to go
            # re-enrol.
            "host": _host_name(),
            "os": f"{platform.system()} {platform.release()}",
            "agentVersion": self._cfg.agent_version,
            # NOT cfg.capabilities: a blocking preflight registers this box HONESTLY as
            # "online, can take nothing" (§3.1). An unregistered box is invisible to both
            # the admin and the operator (F12 — nobody can SSH into these PCs), so the
            # right answer is always to appear and say why, never to hide.
            "capabilities": self._effective_capabilities(),
        }
        # Diagnostic only, and dropped by an older server that does not whitelist the key
        # (§5.3: nothing on the auth or lease path may ever branch on it). This is what
        # puts the real provisioning cause in front of an admin who cannot reach the box.
        report = self.preflight
        upstream = report.to_upstream_wire() if report is not None else None
        if upstream is not None:
            body["preflight"] = upstream
        if report is not None and report.blocking:
            failing = ", ".join(c.id for c in report.blocking_checks())
            log.error("Registering with NO capabilities — the launch preflight is "
                      "blocking on: %s. The box stays online and visible, takes no work, "
                      "and re-registers itself the moment the check clears.", failing)
        res = self._client.register(body)
        if not res.ok or not isinstance(res.data, dict):
            # B10: a 401 here is terminal, not transient — either the persisted token was
            # revoked (so the re-register branch is dead) or the enrolment/bootstrap
            # credential this box holds is not accepted (so first-register is dead).
            # Retrying either forever is exactly the silent brick this closes.
            if res.is_unauthorized:
                self._note_unauthorized("register", presented=self._client.token)
                return False
            self._note_authorized()
            log.error("register failed: %s", res.error)
            return False
        self._note_authorized()
        data = res.data
        self._worker_id = data.get("workerId") or self._cfg.machine_id
        self._heartbeat_interval = _clamp_interval(
            data.get("heartbeatIntervalSec", DEFAULT_HEARTBEAT_INTERVAL_SEC))
        token = data.get("token")
        if token:
            # Belt-and-braces over `check_token_persistence` (F9.1): even if the preflight
            # is wrong, was bypassed, or the backend broke since it ran, a failed save must
            # degrade to "this box re-registers on every start" rather than the OBSERVED
            # failure — a crash raised here, AFTER the server already minted the identity,
            # leaving a fleet row reading 'online' over a dead process while a supervisor
            # crash-loop rotated worker_token_hash every iteration. The token is still
            # adopted in memory, so THIS process works normally.
            try:
                self._tokens.save(token)
            except Exception as e:  # noqa: BLE001 — never crash after the identity is minted
                log.error("Could not persist this box's worker token (%s: %s) — it will "
                          "re-register (and be re-minted) on every start. Fix token "
                          "persistence: see the preflight's token_persistence check.",
                          type(e).__name__, e)
            self._client = self._client.with_token(token)
        # What the fleet now believes this box can do. `_withdraw_and_park` reads it to
        # tell a mid-life block that still has something to withdraw from one that has
        # already withdrawn (or never advertised anything).
        self._registered_capabilities = list(body["capabilities"])
        log.success("Worker registered · id=%s heartbeat=%ss",
                    self._worker_id, self._heartbeat_interval)
        # Start the worker-level presence keepalive, right after register. It runs for
        # the worker's whole lifetime (idle between jobs included). Use the
        # register-returned (clamped) cadence so server/worker presence cannot drift.
        # A presence-start failure must never block entering the lease loop.
        #
        # REPLACE, never stack. `_register` is no longer a once-per-process call: the
        # preflight park re-registers on its blocking→clear transition, `_withdraw_and_park`
        # on the opposite one, and the B10 re-probe on every reminder tick — and each
        # success used to start ANOTHER presence thread while the previous one kept beating
        # with a superseded client, so a box whose Chrome flapped multiplied its presence
        # traffic and `close()` only ever stopped the last of them. SET only, never join:
        # this runs on the loop thread and the old beat may be mid-request; it is a daemon
        # and `wait()` returns the moment the event is set.
        previous = self._presence_thread
        if previous is not None:
            previous.stop()
        try:
            self._presence_thread = _PresenceThread(
                self._client,
                self._worker_id or self._cfg.machine_id,
                self._heartbeat_interval,
                lambda: self.active_jobs,
                self._stop_leasing,
                on_unauthorized=self._note_unauthorized,
                preflight_wire=self.preflight_upstream_wire,
            )
            self._presence_thread.start()
        except Exception as e:  # noqa: BLE001 — presence is best-effort; loop still runs
            log.warning("could not start presence heartbeat (continuing): %s", e)
            self._presence_thread = None
        return True

    def _load_token_safely(self) -> Optional[str]:
        """Load the persisted token, recovering from a corrupt/partial blob by
        clearing it and re-registering fresh rather than crashing every start
        (security review L2)."""
        try:
            return self._tokens.load()
        except SecretCipherError as e:
            log.warning("Stored worker token unreadable (%s) — clearing and "
                        "re-registering", e)
            self._tokens.clear()
            return None

    # --- the loop --------------------------------------------------------------

    def _loop(self, *, max_iterations: Optional[int]) -> None:
        attempt = 0
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            iterations += 1
            # B10: checked BEFORE the shared _stop_leasing gate below so a revoked box
            # reports the real reason instead of masquerading as a drain — the two exit
            # for very different operator actions.
            if self._reenrolment_required.is_set():
                log.error("Worker token revoked — lease loop stopped (re-enrolment "
                          "required; see the CRITICAL line above)")
                return
            # An idle/draining box reacts to a presence-carried drain/halt WITHOUT
            # waiting for a job to run: stop leasing new work and exit the loop.
            if self._stop_leasing.is_set():
                log.info("Drain/halt signalled via presence — stopping the lease loop")
                return
            # Operator pause: idle without leasing, and WITHOUT exiting (resumable). A
            # running job is unaffected — this only gates taking NEW work.
            if self._paused.is_set():
                self._sleep(_backoff(1))
                continue
            # The preflight, re-consulted on EVERY iteration — which also means on every
            # ENTRY into this loop, `resume_leasing()` after a re-enrolment park included.
            # The report is not static: `request_preflight` (the wizard's Re-check button,
            # the desktop's `runPreflight`) republishes it from a detached thread at any
            # moment, and until this check existed nothing downstream of launch ever looked
            # at it again — so a box whose Chrome was restarting during a re-check kept
            # leasing on a blocking report, and the next register (the B10 re-probe's)
            # advertised nothing, permanently. Checked AFTER pause (an operator's explicit
            # idle is cheaper than a park and resumes without waiting for a heal) and after
            # B10 (§3.4: revocation is terminal, preflight is self-healing).
            if self._preflight_blocking():
                if not self._withdraw_and_park(max_waits=max_iterations):
                    return
                attempt = 0      # healed — start the lease backoff from scratch
                continue
            res = self._client.lease({
                "workerId": self._worker_id,
                "capabilities": list(self._cfg.capabilities),
                "leasePollTimeoutSec": self._cfg.lease_poll_timeout_sec,
            })
            if res.is_unauthorized:
                # B10: our bearer was refused for the lease itself. `_note_unauthorized`
                # decides whether that is REVOCATION (retire the token and stop) or an
                # unconfirmed/stale 401 — in which case this backs off exactly like the
                # transient failures below rather than bricking the box on one response.
                self._note_unauthorized("lease", presented=self._client.token)
                if self._reenrolment_required.is_set():
                    return
                attempt += 1
                # Floored, not the bare `_backoff`: an unconfirmed 401 must be re-probed on
                # the SLOW cadence, both so the confirmation window is genuine elapsed time
                # and so a refusing dispatch is not hammered twice a second.
                self._sleep(max(_backoff(attempt), self._unauthorized_retry_floor()))
                continue
            self._note_authorized()
            if not res.ok or res.is_empty:
                attempt += 1
                self._sleep(_backoff(attempt))
                continue
            stop, did_work = self._handle_lease(res.data)
            if stop:
                log.info("Drain signalled — stopping the lease loop")
                return
            if did_work:
                attempt = 0
            else:
                # Lock-busy skip (account already running on this box): back off rather
                # than re-lease in a tight loop and hammer dispatch (code review M-B).
                attempt += 1
                self._sleep(_backoff(attempt))

    def _handle_lease(self, payload) -> tuple:
        """Process one leased payload. Returns ``(stop_loop, did_work)``: ``stop_loop``
        is True on a drain signal; ``did_work`` is True when a job was actually run or
        rejected (vs. skipped because the account's single-flight lock was held)."""
        job_obj = payload.get("job") if isinstance(payload, dict) else None
        try:
            job = JobSpec.from_payload(job_obj)
        except JobSpecError as e:
            # We may not have a job id to nack against; log full detail, send a code.
            jid = (job_obj or {}).get("id") if isinstance(job_obj, dict) else None
            log.error("Rejecting malformed leased job (%s): %s", jid, e)
            if jid:
                self._client.nack(jid, {"jobId": jid, "reason": "invalid_spec"})
            return (False, True)

        # Assign the run_id ONCE here (single source of truth): a legacy job with no
        # cloud run_id gets one now, so the control-surface current_job, the per-run log
        # path, and the supervisor's child all key on the SAME id (review MEDIUM #7).
        if not job.run_id:
            job = replace(job, run_id=uuid.uuid4().hex[:12])

        lock = single_flight.try_acquire(self._cfg.state_dir, job.lock_key())
        if lock is None:
            return (False, False)  # account busy on this box; re-leased later
        controls = Controls()
        hb = _HeartbeatThread(self._client, self._worker_id or "", job.id,
                              self._heartbeat_interval, controls,
                              db_path=self._cfg.db_path, run_id=job.run_id,
                              on_unauthorized=self._note_unauthorized)
        try:
            with self._active_jobs_lock:
                self._active_jobs += 1  # presence beats now report this box as busy
                self._current_job = self._current_job_info(job)
                self._active_controls = controls  # control surface can hard-stop it
            hb.start()
            self._run_and_report(job, controls)
        finally:
            with self._active_jobs_lock:
                self._active_jobs = max(0, self._active_jobs - 1)
                self._current_job = None
                self._active_controls = None
            hb.stop()
            # Join longer than a heartbeat's own HTTP timeout so a beat in flight when
            # stop() fires cannot outlive the join and post for an already-done job
            # (code review H-B).
            hb.join(timeout=self._cfg.request_timeout_sec + self._heartbeat_interval + 1)
            lock.release()
        return (controls.drain.is_set(), True)

    def _run_and_report(self, job: JobSpec, controls: Controls) -> None:
        job, cred_err = self._fetch_job_credential(job)
        if cred_err is not None:
            self._nack(job, cred_err)  # nothing ran → no spend cursor, nothing to sync
            return
        # B9: high-water mark of this CAMPAIGN's local spend_log, taken immediately
        # before the run (and parked per run_id, so a reclaimed attempt's dollars are not
        # lost — see _spend_cursor), so every report path below can ship exactly this
        # attempt's delta to the cloud.
        cursor = self._spend_cursor(job)
        try:
            # Thread the ALREADY-EXISTING halt Event into the supervisor so a heartbeat/
            # operator halt terminates the child mid-run (the post-call halt branch below
            # then nacks with controls.halt_reason — halt always wins).
            summary = job_runner.run_one_job(
                self._store, job, cfg=self._cfg, halt=controls.halt)
        except CampaignNotFound:
            self._nack(job, "campaign_not_found", spend_cursor=cursor)
            return
        except SoulMissing:
            self._nack(job, "soul_missing", spend_cursor=cursor)
            return
        except ValueError as e:  # malformed brief from resolve_campaign
            log.error("Job %s campaign brief malformed: %s", job.id, e)
            self._nack(job, "campaign_malformed", spend_cursor=cursor)
            return
        except Exception as e:  # noqa: BLE001 — any run error → nack, never crash the loop
            # Full detail goes to the (redacted) local log; the outbound reason is a
            # fixed code so a secret-bearing exception string never leaves the box
            # (security review M1). For a child crash, surface the captured log tail via
            # the message (%s) so the RedactingFilter scrubs it before it hits a sink.
            tail = getattr(e, "log_tail", "")
            rc = getattr(e, "returncode", None)
            log.error("Job %s crashed (rc=%s): %s%s", job.id, rc, e,
                      f"\n--- child log tail ---\n{tail}" if tail else "", exc_info=True)
            self._nack(job, "error", spend_cursor=cursor)
            return

        # A halt signalled by the heartbeat thread wins (operator kill / dispatch gone).
        if controls.halt.is_set():
            self._nack(job, controls.halt_reason or "halted", spend_cursor=cursor)
            return
        halt_reason = summary.get("halt_reason")
        if halt_reason:
            # A daytime/quiet-hours halt carries a "try again at" the engine computed;
            # forward it so dispatch defers the requeue rather than backing off blind
            # (BUILD-PLAN C7). Other halts fall back to dispatch-side backoff.
            self._nack(job, halt_reason, poison=_is_poison(halt_reason),
                       retry_after_at=summary.get("retry_after_at"),
                       spend_cursor=cursor)
            return
        self._ack(job, summary, spend_cursor=cursor)

    def _fetch_job_credential(self, job: JobSpec) -> tuple[JobSpec, Optional[str]]:
        """Decrypt-on-demand credential fetch at job start (SECURITY REVIEW CRITICAL/
        HIGH): the server no longer bakes a decrypted per-org secret into the job spec
        or lease response, so a job on a per-org-credentialed platform must pull its own
        fresh here, scoped to the lease this box currently holds (POST
        /api/worker/jobs/{id}/credential — 404s for anyone but the lease holder).

        A CDP platform (instagram/linkedin/x) needs no API key — it drives the warmed
        browser instead — so it's excluded from PER_ORG_CREDENTIAL_PLATFORMS and this
        never even makes the call, mirroring cli._resolve_platform_credentials' own
        platform gate.

        Returns ``(job, None)`` on success — `job` unchanged for a platform that needs no
        credential, or `dataclasses.replace`d with the fetched value (which may itself be
        None: the org simply hasn't connected this platform yet, a legitimate case
        cli.py's baked→local-store→env fallback chain already handles, NOT an error).
        Returns ``(job, CREDENTIAL_FETCH_FAILED_REASON)`` ONLY when the fetch ITSELF
        failed (bad bearer, lease lost/expired, transport/500, or a malformed envelope)
        for a platform that DOES need a credential — the caller nacks with that distinct,
        diagnosable reason rather than silently proceeding into a confusing downstream
        'needs YOUTUBE_API_KEY in the environment' error that matches no known halt kind
        and would otherwise dead-letter after a few blind retries."""
        if job.platform not in PER_ORG_CREDENTIAL_PLATFORMS:
            return job, None
        res = self._client.credential(job.id)
        if not res.ok:
            # B10: a 401 here is about the BEARER, not the lease (a lost/foreign lease is
            # a 404). Retire the token — but still nack this job transiently rather than
            # poisoning it, since the job itself is blameless and another box can run it.
            if res.is_unauthorized:
                self._note_unauthorized("credential fetch",
                                        presented=self._client.token)
            log.error("credential fetch failed for job %s (platform=%s): %s",
                      job.id, job.platform, res.error)
            return job, CREDENTIAL_FETCH_FAILED_REASON
        data = res.data if isinstance(res.data, dict) else {}
        credential = data.get("credential")
        if credential is not None and not isinstance(credential, dict):
            log.error("credential fetch for job %s returned a non-object credential "
                      "(%s) — treating as a failed fetch", job.id,
                      type(credential).__name__)
            return job, CREDENTIAL_FETCH_FAILED_REASON
        return replace(job, platform_credentials=credential), None

    # --- result posting --------------------------------------------------------

    def _ack(self, job: JobSpec, summary: dict,
             spend_cursor: Optional[int] = None) -> None:
        leads = self._collect_leads(summary)
        body = {"jobId": job.id, "summary": summary}
        if leads:
            body["leads"] = leads
        reported = self._attach_spend(body, job, spend_cursor)
        res = self._client.ack(job.id, body)
        if res.is_unauthorized:
            # B10: the job already ran and its result is lost to the cloud (the server
            # will reclaim the lease and requeue). Nothing to do for THIS job, but the
            # box must still report the 401 so a CONFIRMED revocation retires its token.
            self._note_unauthorized("ack", presented=self._client.token)
        if res.ok and reported:
            self._clear_spend_cursor(job)  # the cloud banked this run's spend
        if res.ok:
            log.success("Job %s done · matches=%s · leads_synced=%d · spend_rows=%d",
                        job.id, summary.get("matches"), len(leads),
                        len(body.get("spend", [])))
        else:
            log.warning("ack for %s failed: %s", job.id, res.error)

    def _collect_leads(self, summary: dict) -> list[dict]:
        """Read the leads this job captured from the LOCAL store — by run_id, across
        ALL of the run's sessions (a target-leads run loops many) — and map them to
        the ack sync DTO. Capped + logged so a big harvest never blows the ack body;
        the cloud re-caps + forces the job's own campaign on write. A read failure is
        swallowed (the job is done regardless — never block the ack on the sync)."""
        run_id = summary.get("run_id")
        if not run_id:
            return []
        try:
            rows = self._store.matches_for_run(run_id)
        except Exception:  # noqa: BLE001 — a local read hiccup must not block the ack
            log.warning("could not read captured leads for run %s", run_id, exc_info=True)
            return []
        if len(rows) > MAX_SYNC_LEADS:
            log.warning("run %s captured %d leads; syncing the first %d (excess deferred)",
                        run_id, len(rows), MAX_SYNC_LEADS)
        return [_lead_dto(r) for r in rows[:MAX_SYNC_LEADS]]

    def _spend_cursor_path(self, job: JobSpec) -> Optional[Path]:
        """Where this run's spend cursor is parked between attempts, or None for a job
        with no run_id (nothing stable to key it on)."""
        if not job.run_id:
            return None
        return self._cfg.state_dir / f"run-{job.run_id}{job_runner.SPEND_CURSOR_SUFFIX}"

    def _spend_cursor(self, job: JobSpec) -> Optional[int]:
        """This attempt's local spend_log high-water mark (B9), PERSISTED per run_id
        alongside the run's pause sentinel. None on a read failure — the same swallow as
        `_collect_leads`: a bookkeeping hiccup must never stop a job from running or
        reporting.

        Persisted, not re-taken per lease, because an attempt can end WITHOUT an ack or
        a nack: if the sidecar dies mid-run, `Store.reclaim_offline_jobs` requeues the
        job PINNED to this same box, and a fresh cursor would already sit past the rows
        that attempt wrote — so those dollars would never reach the cloud at all, and the
        headroom they represent would later be handed to another box, breaking the very
        fleet-wide ceiling B9 exists to enforce. Reusing the original mark makes the
        retry report attempt 1's spend together with its own. The file is deleted the
        moment dispatch ACCEPTS an ack/nack (`_clear_spend_cursor`), so a legitimately
        re-queued next attempt starts from a fresh mark and nothing is double-reported;
        `job_runner.sweep_orphan_job_files` reaps any that leak."""
        path = self._spend_cursor_path(job)
        if path is not None:
            try:
                parked = path.read_text(encoding="utf-8").strip()
            except OSError:
                parked = ""
            if parked:
                try:
                    cursor = int(parked)
                except ValueError:
                    log.warning("ignoring a garbled spend cursor at %s", path)
                else:
                    log.info("Resuming the spend cursor for run %s at id=%d (a prior "
                             "attempt ended without an ack/nack)", job.run_id, cursor)
                    return cursor
        try:
            cursor = self._store.max_spend_id(job.campaign_id)
        except Exception:  # noqa: BLE001 — accounting is best-effort
            log.warning("could not read the local spend cursor", exc_info=True)
            return None
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(cursor), encoding="utf-8")
            except OSError as e:  # noqa: BLE001 — degrade to today's per-attempt cursor
                log.warning("could not park the spend cursor at %s: %s", path, e)
        return cursor

    def _clear_spend_cursor(self, job: JobSpec) -> None:
        """Drop this run's parked cursor once the cloud has ACCEPTED its spend (see
        `_spend_cursor`). Only ever called on an accepted ack/nack: a rejected report
        means the delta is still unbanked, so the mark must survive for the retry."""
        path = self._spend_cursor_path(job)
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as e:  # noqa: BLE001 — the sweep reaps it later
            log.warning("could not clear the spend cursor at %s: %s", path, e)

    def _collect_spend(self, job: JobSpec, cursor: Optional[int]) -> Optional[list[dict]]:
        """This attempt's spend delta, rolled up per (stage, model), for the ack/nack
        body (B9). ``None`` means "could not be determined" (no cursor was taken, or the
        read failed) as opposed to ``[]`` — "determined, and there was none". The caller
        needs that distinction: only a DETERMINED delta may retire the parked cursor.

        The run_id is passed so a CONCURRENT job for the same campaign on another
        platform (a different `lock_key`, so the box's single-flight lock does not
        serialise them) cannot have its rows counted into this report as well — the
        cloud `spend_log` has no unique key, so that double insert would inflate the
        campaign's spend and halve its effective cap."""
        if cursor is None:
            return None
        try:
            rows = self._store.spend_since(job.campaign_id, cursor, job.run_id)
        except Exception:  # noqa: BLE001 — never block the report on the spend read
            log.warning("could not read spend for campaign %s", job.campaign_id,
                        exc_info=True)
            return None
        if len(rows) > MAX_SYNC_SPEND_ROWS:
            log.warning("run for campaign %s produced %d spend rows; syncing the "
                        "first %d (excess dropped)", job.campaign_id, len(rows),
                        MAX_SYNC_SPEND_ROWS)
        return rows[:MAX_SYNC_SPEND_ROWS]

    def _attach_spend(self, body: dict, job: JobSpec,
                      cursor: Optional[int]) -> bool:
        """Attach the B9 spend rollup + this box's database identity to a report body.
        Returns True iff this report FULLY accounts for the attempt's spend — i.e. the
        delta was actually determined AND attributed to a database. Only then may the
        caller retire the parked cursor; otherwise the money is still unbanked and the
        mark has to survive for the next attempt.

        `dbId` is what lets the cloud tell "this worker writes to a DIFFERENT database"
        from "this worker's db_path IS the cloud DB" (the common same-box dev/desktop
        topology, since AIZU_DB defaults to the same `aizu.db` filename). spend_log has
        no unique key, so rolling up in the shared case would DOUBLE the campaign's
        spend. If the identity can't be read we therefore ship NO spend at all — an
        unattributed row is worse than a missing one."""
        db_id = None
        try:
            db_id = self._store.database_id()
        except Exception:  # noqa: BLE001
            log.warning("could not read the local database id — skipping spend sync",
                        exc_info=True)
            return False
        body["dbId"] = db_id
        spend = self._collect_spend(job, cursor)
        if spend:
            body["spend"] = spend
        return spend is not None

    def _nack(self, job: JobSpec, reason: str, *, poison: bool = False,
              retry_after_at: Optional[float] = None,
              spend_cursor: Optional[int] = None) -> None:
        body = {"jobId": job.id, "reason": reason, "poison": poison}
        if retry_after_at is not None:
            body["retryAfterAt"] = retry_after_at
        # A crashed/halted attempt still spent real money, and a requeue is UNPINNED —
        # attempt 2 may land on a box with no record of it. Roll it up here too (B9).
        # A report that carries no determined delta (e.g. the credential-fetch nack,
        # which fires BEFORE the run and so has no cursor) must NOT retire the mark: a
        # PRIOR attempt of this same run may have parked one holding real, unbanked money.
        reported = self._attach_spend(body, job, spend_cursor)
        res = self._client.nack(job.id, body)
        if res.is_unauthorized:
            # B10 — same as _ack: report it; a CONFIRMED revocation retires the token.
            self._note_unauthorized("nack", presented=self._client.token)
        if res.ok and reported:
            self._clear_spend_cursor(job)  # the cloud banked this attempt's spend
        # A nack always means the job did NOT succeed — log at warning so it is not
        # lost in an info-filtered view, even when dispatch accepts it (code review L-A).
        log.warning("Job %s nacked (poison=%s, dispatch_ok=%s): %s",
                    job.id, poison, res.ok, reason)

    def _current_job_info(self, job: JobSpec) -> CurrentJobInfo:
        """Build the immutable current-job snapshot the control surface reports. The log
        path is the per-run file the child writes under the box's state dir (run_id known
        for cloud jobs; None for a legacy job whose run_id the supervisor generates)."""
        from ..core.logsetup import run_log_path
        log_path = None
        if job.run_id:
            base = str(self._cfg.state_dir / "logs" / "aizu.log")
            resolved = run_log_path(job.run_id, log_file=base)
            log_path = str(resolved) if resolved else None
        return CurrentJobInfo(
            job_id=job.id, campaign_id=job.campaign_id, platform=job.platform,
            status="running", run_id=job.run_id, log_file_path=log_path)

    def start_control_surface(self) -> None:
        """Start the loopback-only control surface on a daemon thread IF enabled. Called
        after register (worker_id known). A start failure must never block the loop."""
        if not self._cfg.control_surface_enabled:
            return
        try:
            from .control_surface import ControlSurfaceConfig, start_control_surface
            cfg = ControlSurfaceConfig(
                auth_token=self._cfg.control_surface_token or "",
                port=self._cfg.control_surface_port)
            self._control_server = start_control_surface(cfg, SidecarControlSource(self))
            self._control_thread = threading.Thread(
                target=self._control_server.serve_forever, name="control-surface",
                daemon=True)
            self._control_thread.start()
        except Exception as e:  # noqa: BLE001 — control surface is best-effort
            log.warning("could not start control surface (continuing headless): %s", e)
            self._control_server = None

    def _stop_control_surface(self) -> None:
        if self._control_server is not None:
            try:
                self._control_server.shutdown()
                self._control_server.server_close()
            except Exception as e:  # noqa: BLE001
                log.warning("control surface shutdown raised: %s", e)
            self._control_server = None

    def close(self) -> None:
        # Stop the control surface first (it reads sidecar state we're about to tear down).
        self._stop_control_surface()
        # Stop + join the presence thread FIRST so it can't post against a client
        # whose connection pool we're about to close (LOCKED #9 ordering).
        if self._presence_thread is not None:
            self._presence_thread.stop()
            self._presence_thread.join(
                timeout=self._cfg.request_timeout_sec
                + self._heartbeat_interval + 1)
            self._presence_thread = None
        self._client.close()
        if self._http is not None:
            self._http.close()
        self._store.close()


class SidecarControlSource:
    """Adapts a live :class:`Sidecar` to the control surface's ``ControlSurfaceSource``
    port. The ONLY thing that reaches into sidecar internals for the surface, so the
    dependency stays one-way (sidecar never imports control_surface). Reads current_job
    + the active controls under the sidecar's own lock; never exposes a secret."""

    def __init__(self, sidecar: "Sidecar"):
        self._s = sidecar

    def get_status(self):
        from .control_state import StatusSnapshot
        from . import chrome_probe
        cj = self._s.current_job
        with self._s._active_jobs_lock:
            controls = self._s._active_controls
        halt = controls.halt.is_set() if controls else False
        halt_reason = controls.halt_reason if controls else None
        update_required = controls.update_required.is_set() if controls else False
        chrome = chrome_probe.cdp_status(self._s._cfg.cdp_url)
        return StatusSnapshot(
            worker_id=self._s._worker_id,
            accounts=tuple(_accounts_health(self._s._cfg.capabilities, cj)),
            current_job=cj,
            drain=self._s._stop_leasing.is_set(),
            halt=halt, halt_reason=halt_reason, update_required=update_required,
            chrome=chrome, paused=self._s._paused.is_set(),
            reenrolment_required=self._s.reenrolment_required,
            preflight=self._s.preflight_wire(),
            generated_at=time.time())

    def pause(self) -> None:
        self._s.pause()

    def resume(self) -> None:
        self._s.resume()

    def stop_current_job(self) -> bool:
        return self._s.request_stop_current_job()

    # Both of these MUST exist here, not merely on the Protocol: `control_surface._dispatch`
    # already routes `runPreflight`/`openLoginTab` to them through `_detach`, which wraps
    # the call in `_swallow` — so while this adapter lacked them, a real sidecar answered
    # `200 {"accepted": true}` and then did NOTHING, the AttributeError eaten on a daemon
    # thread. A test asserting them on a FAKE source would not have caught that.
    def run_preflight(self) -> bool:
        return self._s.request_preflight()

    def open_login_tab(self, platform: str) -> bool:
        return self._s.open_login_tab(platform)

    def focus_warmed_chrome(self) -> bool:
        """Bring the warmed Chrome to the front for a 2FA/captcha challenge. macOS is
        real (osascript activate — no process ownership needed); other OSes are a logged
        no-op pending real-hardware focus strategies (chrome_manager owns those seams)."""
        from .chrome_manager import _macos_focus, _unsupported_focus
        return {"Darwin": _macos_focus}.get(platform.system(), _unsupported_focus)()


def _accounts_health(capabilities, current_job) -> list:
    """Map the box's registered capabilities to per-account health. An account is 'busy'
    when a job is running on its platform, else 'idle'. Tolerant of both list-shaped
    ([org, platform, handle]) and dict-shaped capability entries."""
    busy_platform = current_job.platform if current_job else None
    out = []
    for cap in capabilities or ():
        org_id, plat, handle = _unpack_capability(cap)
        if not plat:
            continue
        status = "busy" if plat == busy_platform else "idle"
        out.append(_account_health(org_id, plat, handle, status))
    return out


def _unpack_capability(cap):
    if isinstance(cap, dict):
        return (cap.get("orgId", cap.get("org_id")), cap.get("platform"),
                cap.get("accountHandle", cap.get("account_handle")))
    if isinstance(cap, (list, tuple)) and len(cap) >= 2:
        org_id = cap[0]
        plat = cap[1]
        handle = cap[2] if len(cap) >= 3 else None
        return (org_id, plat, handle)
    return (None, None, None)


def _account_health(org_id, platform_name, handle, status):
    from .control_state import AccountHealth
    return AccountHealth(org_id=org_id, platform=platform_name,
                         account_handle=handle, status=status)


def _clamp_interval(interval) -> float:
    """Clamp a heartbeat cadence to the safe minimum (security review H2)."""
    try:
        value = float(interval)
    except (TypeError, ValueError):
        return _HEARTBEAT_MIN_SEC
    return max(_HEARTBEAT_MIN_SEC, value)


_HOST_NAME_CACHE: Optional[str] = None


def _host_name() -> str:
    """This box's network name for `workers.host` (the register body's `host` field).

    `platform.node()` is the answer, upgraded to the FQDN ONLY when the FQDN is that same
    name plus a domain suffix (`worker-1` → `worker-1.fleet.internal`) — the one case where
    it tells an operator something the short name does not.

    That test is narrow on purpose. `socket.getfqdn()` resolves through the reverse-DNS
    path and is happy to return something strictly WORSE than what it started with: on a
    developer laptop here it answered
    `1.0.0.0.…​.ip6.arpa` (the loopback PTR name), and on a box with no reverse mapping it
    answers `localhost`. Either would have made `workers.host` less useful than the NULL it
    replaced. Cached because register retries on a backoff, and fully guarded because a
    box with a broken resolver must still register: an unidentifiable worker is a nuisance,
    an unregisterable one is an outage."""
    global _HOST_NAME_CACHE
    if _HOST_NAME_CACHE is None:
        node = (platform.node() or "").strip()
        try:
            fqdn = (socket.getfqdn() or "").strip()
        except Exception:  # noqa: BLE001 — resolver trouble is never fatal here
            fqdn = ""
        if node and fqdn.startswith(f"{node}."):
            _HOST_NAME_CACHE = fqdn
        else:
            _HOST_NAME_CACHE = node or fqdn or "unknown-host"
    return _HOST_NAME_CACHE


def _resolve_cdp_url(cfg: WorkerConfig) -> WorkerConfig:
    """``preflight.resolve_cdp_url``, skipped entirely on an API-only box (F10).

    Resolution probes two ports with real socket timeouts (3.0s + 1.5s), and a box that
    advertises only youtube/telegram/reddit never opens a browser at all — so on that box
    the probe is 4.5 seconds of dead launch latency buying a field nothing reads. Gating
    on the same helper the CDP checks gate on keeps the two in lockstep."""
    if not preflight.cdp_platforms_advertised(cfg):
        return cfg
    return preflight.resolve_cdp_url(cfg)


def _backoff(attempt: int) -> float:
    """Jittered exponential backoff, capped (BUILD-PLAN risk #5)."""
    ceiling = min(_BACKOFF_CAP_SEC, _BACKOFF_BASE_SEC * (2 ** attempt))
    return random.uniform(_BACKOFF_BASE_SEC, max(_BACKOFF_BASE_SEC, ceiling))


def _is_poison(halt_reason: str) -> bool:
    low = halt_reason.lower()
    return any(token in low for token in _POISON_HALTS)


def _lead_dto(row: dict) -> dict:
    """Map a local `matches` row to the ack sync DTO (camelCase). The campaign is
    deliberately OMITTED — the cloud forces the job's own campaign on write, never
    trusting a client-supplied one (the BOLA guard lives server-side)."""
    return {
        "commentId": row.get("comment_id"),
        "reelId": row.get("reel_id"),
        "platform": row.get("platform"),
        "username": row.get("username"),
        "text": row.get("text"),
        "lang": row.get("lang"),
        "score": row.get("score"),
        "reason": row.get("reason"),
        "extracted": row.get("extracted"),
        "tier": row.get("tier"),
        "sessionId": row.get("session_id"),
        "capturedAt": row.get("captured_at"),
    }


def main(argv: Optional[list] = None) -> int:
    from .. import cli as _cli
    _cli._load_env()
    from ..core.logsetup import configure_logging
    configure_logging()
    cfg = WorkerConfig.from_env()
    # NOTE: F10 port resolution deliberately does NOT happen here. It probes two ports with
    # real socket timeouts, and here it would run before the Sidecar exists — so before the
    # control surface can bind, delaying the local UI by up to 4.5s on exactly the box
    # (Chrome down) whose operator needs it. `Sidecar.run` does it behind the bind instead.
    try:
        sidecar = Sidecar(cfg)
    except Exception as e:  # noqa: BLE001 — name the cause, then fail as before
        # The `db_writable` check that could not exist (§2.1): `Sidecar.__init__` opens
        # Store(cfg.db_path) EAGERLY, i.e. before the control surface can bind and before
        # any preflight could run, so an unwritable AIZU_DB kills the process with a bare
        # traceback a GUI operator never sees. One named CRITICAL is the whole fix
        # available without making `_store` lazy across ~17 construction sites.
        log.critical("The worker could not start: %s: %s. Check AIZU_DB (%s) and "
                     "AIZU_WORKER_STATE (%s) — both must be writable by this user.",
                     type(e).__name__, e, cfg.db_path, cfg.state_dir)
        raise
    try:
        sidecar.run()
        # B10: a revoked box parks (control surface still serving) instead of exiting,
        # so its supervisor does not crash-loop a process that can only fail. The park
        # re-probes registration on its reminder cadence, so a 401 that turned out to be
        # server-side resumes the pull loop here without anyone logging in.
        while sidecar.reenrolment_required:
            if not sidecar.park_for_reenrolment():
                break
            sidecar.resume_leasing()
    finally:
        sidecar.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
