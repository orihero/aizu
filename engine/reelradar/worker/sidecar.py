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
"""
from __future__ import annotations

import platform
import random
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

import httpx

from ..core.logsetup import get_logger
from ..core.store import MAX_SYNC_LEADS, Store
from ..secrets import SecretCipherError
from . import DEFAULT_HEARTBEAT_INTERVAL_SEC, job_runner, single_flight
from .config import JobSpec, JobSpecError, WorkerConfig
from .control_state import CurrentJobInfo
from .job_runner import CampaignNotFound, SoulMissing
from .lease_client import LeaseClient
from .token_store import TokenStore

log = get_logger("reelradar.worker.sidecar")

# Lease-miss backoff: jittered exponential, capped, to avoid a thundering herd on the
# single-writer dispatch DB (BUILD-PLAN risk #5).
_BACKOFF_BASE_SEC = 0.5
_BACKOFF_CAP_SEC = 30.0
_HEARTBEAT_FAIL_LIMIT = 3
# Floor on the heartbeat cadence so a misconfigured/compromised dispatch can't drive a
# busy-loop (security review H2). The real cadence (≈20s) comes from register.
_HEARTBEAT_MIN_SEC = 5.0

# Halt reasons that mean "don't bother retrying this job soon" (poison) vs. transient.
_POISON_HALTS = ("needs reconnect", "unsupported", "not yet implemented")

# How long a finished/crashed job's supervisor rendezvous files (spec/result/per-job
# log) are kept before the startup sweep reclaims them — long enough for an operator to
# inspect yesterday's failed job's log, short enough that disk stays bounded.
_JOB_FILE_RETENTION_SEC = 7 * 24 * 60 * 60


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
                 db_path: Optional[str] = None, run_id: Optional[str] = None):
        super().__init__(name=f"heartbeat-{job_id}", daemon=True)
        self._client = client
        self._worker_id = worker_id
        self._job_id = job_id
        self._interval = _clamp_interval(interval)
        self._controls = controls
        self._db_path = db_path
        self._run_id = run_id
        self._stop = threading.Event()

    def run(self) -> None:
        failures = 0
        # Own connection for reading local run_events (created in THIS thread).
        events_store = self._open_events_store()
        cursor = 0
        try:
            while not self._stop.wait(self._interval):
                body = {"workerId": self._worker_id, "jobId": self._job_id}
                batch, max_id = self._collect_run_events(events_store, cursor)
                if self._run_id:
                    body["runId"] = self._run_id
                if batch:
                    body["runEvents"] = batch
                res = self._client.heartbeat(self._job_id, body)
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
        self._stop.set()


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
                 stop_leasing: threading.Event):
        super().__init__(name="presence", daemon=True)
        self._client = client
        self._worker_id = worker_id
        self._interval = _clamp_interval(interval)
        self._current_sessions = current_sessions   # callable → live load at beat time
        self._stop_leasing = stop_leasing
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                res = self._client.presence({
                    "workerId": self._worker_id,
                    "currentSessions": self._current_sessions(),
                    "timestamp": time.time(),
                })
            except Exception as e:  # noqa: BLE001 — presence is best-effort, never crash
                log.warning("presence heartbeat raised (continuing): %s", e)
                continue
            if not res.ok:
                # A failure only de-noises the log — it must NOT stop leasing (a flaky
                # network is not a drain signal; LOCKED #9 keeps presence non-coercive).
                log.warning("presence heartbeat failed (continuing): %s", res.error)
                continue
            apply_presence_flags(self._stop_leasing, res)

    def stop(self) -> None:
        self._stop.set()


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
                 sleep: Callable[[float], None] = time.sleep):
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
        # The live job's identity + its Controls, guarded by the SAME _active_jobs_lock
        # (one lock per related-state cluster). The control surface reads current_job and
        # signals the active job's halt through these — never a bare cross-thread touch.
        self._current_job: Optional["CurrentJobInfo"] = None
        self._active_controls: Optional[Controls] = None
        self._control_server = None  # ThreadingHTTPServer when the surface is enabled
        self._control_thread: Optional[threading.Thread] = None

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

    # --- lifecycle -------------------------------------------------------------

    def run(self, *, max_iterations: Optional[int] = None) -> None:
        """Run the pull loop. ``max_iterations`` bounds it for tests; ``None`` runs
        until a drain flag is seen."""
        self._startup_sweep()
        # Control surface FIRST — the local desktop UI must reflect THIS box's state even
        # when the cloud dispatch is unreachable or registration hasn't succeeded yet
        # (otherwise switching dispatch = a silent "disconnected" with no local feedback).
        self.start_control_surface()
        if not self._register():
            log.error("Worker registration failed — control surface stays up; retrying")
            # Stay alive and retry on the presence cadence so a transient cloud outage or a
            # just-switched dispatch recovers WITHOUT a full process restart. Bounded by
            # max_iterations for tests.
            self._await_registration(max_iterations=max_iterations)
        if self._worker_id is None:
            return  # never registered (stopped or test-bounded) — nothing to lease
        self._loop(max_iterations=max_iterations)

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
            self._sleep(_backoff(attempt))
            attempt += 1
            if self._register():
                return

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
            "os": f"{platform.system()} {platform.release()}",
            "agentVersion": self._cfg.agent_version,
            "capabilities": list(self._cfg.capabilities),
        }
        res = self._client.register(body)
        if not res.ok or not isinstance(res.data, dict):
            log.error("register failed: %s", res.error)
            return False
        data = res.data
        self._worker_id = data.get("workerId") or self._cfg.machine_id
        self._heartbeat_interval = _clamp_interval(
            data.get("heartbeatIntervalSec", DEFAULT_HEARTBEAT_INTERVAL_SEC))
        token = data.get("token")
        if token:
            self._tokens.save(token)
            self._client = self._client.with_token(token)
        log.success("Worker registered · id=%s heartbeat=%ss",
                    self._worker_id, self._heartbeat_interval)
        # Start the worker-level presence keepalive once, right after register. It
        # runs for the worker's whole lifetime (idle between jobs included). Use the
        # register-returned (clamped) cadence so server/worker presence cannot drift.
        # A presence-start failure must never block entering the lease loop.
        try:
            self._presence_thread = _PresenceThread(
                self._client,
                self._worker_id or self._cfg.machine_id,
                self._heartbeat_interval,
                lambda: self.active_jobs,
                self._stop_leasing,
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
            res = self._client.lease({
                "workerId": self._worker_id,
                "capabilities": list(self._cfg.capabilities),
                "leasePollTimeoutSec": self._cfg.lease_poll_timeout_sec,
            })
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
                              db_path=self._cfg.db_path, run_id=job.run_id)
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
        try:
            # Thread the ALREADY-EXISTING halt Event into the supervisor so a heartbeat/
            # operator halt terminates the child mid-run (the post-call halt branch below
            # then nacks with controls.halt_reason — halt always wins).
            summary = job_runner.run_one_job(
                self._store, job, cfg=self._cfg, halt=controls.halt)
        except CampaignNotFound:
            self._nack(job, "campaign_not_found")
            return
        except SoulMissing:
            self._nack(job, "soul_missing")
            return
        except ValueError as e:  # malformed brief from resolve_campaign
            log.error("Job %s campaign brief malformed: %s", job.id, e)
            self._nack(job, "campaign_malformed")
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
            self._nack(job, "error")
            return

        # A halt signalled by the heartbeat thread wins (operator kill / dispatch gone).
        if controls.halt.is_set():
            self._nack(job, controls.halt_reason or "halted")
            return
        halt_reason = summary.get("halt_reason")
        if halt_reason:
            # A daytime/quiet-hours halt carries a "try again at" the engine computed;
            # forward it so dispatch defers the requeue rather than backing off blind
            # (BUILD-PLAN C7). Other halts fall back to dispatch-side backoff.
            self._nack(job, halt_reason, poison=_is_poison(halt_reason),
                       retry_after_at=summary.get("retry_after_at"))
            return
        self._ack(job, summary)

    # --- result posting --------------------------------------------------------

    def _ack(self, job: JobSpec, summary: dict) -> None:
        leads = self._collect_leads(summary)
        body = {"jobId": job.id, "summary": summary}
        if leads:
            body["leads"] = leads
        res = self._client.ack(job.id, body)
        if res.ok:
            log.success("Job %s done · matches=%s · leads_synced=%d",
                        job.id, summary.get("matches"), len(leads))
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

    def _nack(self, job: JobSpec, reason: str, *, poison: bool = False,
              retry_after_at: Optional[float] = None) -> None:
        body = {"jobId": job.id, "reason": reason, "poison": poison}
        if retry_after_at is not None:
            body["retryAfterAt"] = retry_after_at
        res = self._client.nack(job.id, body)
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
            base = str(self._cfg.state_dir / "logs" / "reelradar.log")
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
            generated_at=time.time())

    def pause(self) -> None:
        self._s.pause()

    def resume(self) -> None:
        self._s.resume()

    def stop_current_job(self) -> bool:
        return self._s.request_stop_current_job()

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
    sidecar = Sidecar(cfg)
    try:
        sidecar.run()
    finally:
        sidecar.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
