"""Run ONE leased job as a SUPERVISED CHILD PROCESS (BUILD-PLAN §2.2 + Phase 6).

Two layers live here:

  - ``_execute_job`` — the in-process body: resolve the brief + soul, stamp a run_id,
    drive ``cli._run_session_loop`` (the exact CLI ``aizu run`` seam), clean up the
    pause sentinel. This is byte-for-byte what the worker has always done; it now runs
    INSIDE the child process (``job_child``), and directly in unit tests.
  - ``run_one_job`` — the Phase-6 SUPERVISOR: it launches ``python -m
    aizu.worker.job_child`` as a killable child, then polls the child while watching
    the sidecar's ``halt`` Event and a wall-clock deadline. An operator/heartbeat halt
    (or a wedged-past-deadline child) is now a TRUE mid-run hard-stop — SIGTERM, grace,
    then SIGKILL — instead of the old in-process model that could only honor a halt at
    the job boundary. The child rendezvous with the parent through the SAME on-disk
    SQLite DB (WAL, exactly as today) plus a small JSON result file.

The single-flight lock (one account ↔ one box ↔ one live job) is UNCHANGED: it is
acquired/released by ``sidecar._handle_lease`` around the whole supervised lifetime,
so a hard-killed child can never leak it. See ``docs/prd/distributed-workers-BUILD-PLAN.md``
Phase 6a.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

from .. import cli
from ..core.config import Soul, resolve_campaign
from ..core.logsetup import get_logger
from . import (JOB_RESULT_KIND_CAMPAIGN_MALFORMED, JOB_RESULT_KIND_CAMPAIGN_NOT_FOUND,
               JOB_RESULT_KIND_ERROR, JOB_RESULT_KIND_SOUL_MISSING)
from ._atomic_io import atomic_write_json, pid_alive
from .config import JobSpec, WorkerConfig

log = get_logger("aizu.worker.job_runner")

# Mirror RunManager._pause_path so the engine (consumer) and the worker (producer)
# derive the SAME sentinel path: run-<run_id>.pause (runner.py:296).
_PAUSE_SUFFIX = ".pause"
# A baked soul carries no source file; use a sentinel path (Soul.path is required but
# only informational at run time).
_BAKED_SOUL_PATH = Path("<baked:job-spec>")

# Guards the process-wide os.environ mutation in _run_env. In the supervised-subprocess
# model each child is its own process with its own os.environ, so this is no longer
# LOAD-BEARING for cross-job isolation — it is belt-and-suspenders that turns a
# hypothetical in-process reentry (a bug) into a loud, test-time error (code review M-C).
_RUN_ENV_GUARD = threading.Lock()

# --- supervisor tuning (named, no magic numbers) ---------------------------------
# Let the child's own session teardown run before escalating to SIGKILL.
SIGTERM_GRACE_SEC = 15.0
# Child-liveness + halt-check granularity.
SUPERVISOR_POLL_SEC = 0.5
# Minutes of slack beyond the job's effective duration cap before the supervisor treats
# a still-alive child as WEDGED (a stuck Playwright/CDP attach must never hold a lease
# forever — a residual gap the old in-process model had, now closed).
TIMEOUT_GRACE_MIN = 2

# On-disk rendezvous dirs under state_dir. Parent owns their whole lifecycle (writes the
# spec, reads the result, deletes both) — mirrors the pause-file ownership convention.
_SPEC_DIRNAME = "job-specs"
_RESULT_DIRNAME = "job-results"
_JOB_LOG_DIRNAME = "logs"
_JOB_LOG_PREFIX = "job-"
_JOB_LOG_SUFFIX = ".log"

# Bound the tail of the child's stdout/stderr log we surface in a ChildCrashed so a
# crash reason is diagnosable without shipping an unbounded (possibly secret-bearing,
# though redacted) blob into an exception string.
_CRASH_LOG_TAIL_BYTES = 4096

# Fail-fast CDP pre-check (Fix B, 2026-07-03). Before spawning the child we cheaply probe
# that the warmed Chrome is actually attachable. Without this a bad/unreachable/logged-out
# Chrome makes the engine's connect_over_cdp hang ~180s inside the child, then crash and
# get re-leased — wasting minutes and prolonging the campaign's "in flight" block. The
# probe is HARD-bounded (a wedged attach must never wedge the worker): it runs on its own
# thread we abandon on timeout, so the worst case is one leaked short-lived thread + driver.
CDP_PROBE_TIMEOUT_SEC = 8.0
# The distinct nack reason surfaced when the probe fails. NOT a poison token
# (sidecar._POISON_HALTS) → the sidecar requeues it with backoff exactly like any other
# transient failure, via the existing summary halt_reason mapping (no new terminal path).
CDP_UNREACHABLE_REASON = "cdp_unreachable"


def _default_cdp_probe(cdp_url: str) -> bool:
    """Best-effort, HARD-BOUNDED reachability check for the warmed Chrome. Returns True
    iff we can attach over CDP within :data:`CDP_PROBE_TIMEOUT_SEC` and see at least one
    browser context. Mirrors the engine's real attach (``connect_over_cdp(url,
    no_defaults=True)`` — cdp.py) so a Chrome that would fail the real attach fails here
    too, cheaply and fast.

    The attach can itself block far longer than our budget, so we run it on a daemon
    thread and JOIN with a timeout — if it does not finish in time we return False and
    let the abandoned thread (and its driver) die on its own. This guarantees the probe
    can never hang the worker (the whole point of Fix B)."""
    result: dict[str, bool] = {"ok": False}

    def _attempt() -> None:
        pw = None
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            browser = pw.chromium.connect_over_cdp(cdp_url, no_defaults=True)
            result["ok"] = bool(browser.contexts)
        except Exception as e:  # noqa: BLE001 — any failure means "not reachable"
            log.warning("CDP probe failed for %s: %s", cdp_url, e)
        finally:
            if pw is not None:
                try:
                    pw.stop()
                except Exception:  # noqa: BLE001 — teardown must not mask the result
                    pass

    t = threading.Thread(target=_attempt, name="cdp-probe", daemon=True)
    t.start()
    t.join(CDP_PROBE_TIMEOUT_SEC)
    if t.is_alive():
        log.warning("CDP probe for %s did not return within %.0fs — treating as "
                    "unreachable", cdp_url, CDP_PROBE_TIMEOUT_SEC)
        return False
    return result["ok"]


class CampaignNotFound(Exception):
    """The job's campaign_id resolves to no runnable brief — hard nack, not a retry."""


class SoulMissing(Exception):
    """Neither a baked soul_text nor a local soul.md — hard nack."""


class ChildCrashed(Exception):
    """The job child died without a parseable result and no halt was requested — the
    sidecar's generic ``except Exception`` maps this to a soft nack('error')."""

    def __init__(self, returncode: Optional[int], log_tail: str = ""):
        self.returncode = returncode
        self.log_tail = log_tail
        super().__init__(f"job child crashed (rc={returncode})")


# kind → exception class, so the supervisor maps a child's unrunnable-job result back to
# the SAME exception the in-process path raised (sidecar's except-chain is unchanged).
_KIND_TO_EXC = {
    JOB_RESULT_KIND_CAMPAIGN_NOT_FOUND: CampaignNotFound,
    JOB_RESULT_KIND_SOUL_MISSING: SoulMissing,
    JOB_RESULT_KIND_CAMPAIGN_MALFORMED: ValueError,
    JOB_RESULT_KIND_ERROR: RuntimeError,
}


# --- the supervisor (parent side) ------------------------------------------------

def run_one_job(store, job: JobSpec, *, cfg: WorkerConfig, halt: threading.Event,
                popen: Callable[..., "subprocess.Popen"] = subprocess.Popen,
                sleep: Callable[[float], None] = time.sleep,
                monotonic: Callable[[], float] = time.monotonic,
                cdp_probe: Callable[[str], bool] = _default_cdp_probe) -> dict:
    """Supervise one leased job in a killable child process. Returns the engine summary
    dict (augmented with ``run_id``/``job_id``) on clean completion; raises
    :class:`CampaignNotFound`/:class:`SoulMissing`/``ValueError`` for an unrunnable job
    (mapped from the child's result), or :class:`ChildCrashed` for an unexplained death.

    On a ``halt`` (operator/heartbeat) the child is SIGTERM→SIGKILL'd mid-run and this
    returns promptly — the sidecar's post-call ``if controls.halt.is_set()`` branch then
    nacks with the operator reason (halt wins, exactly as before). A child that outlives
    its duration cap + grace is self-terminated with ``halt_reason='worker_timeout'``.

    FAIL-FAST CDP (Fix B): before spawning the child we probe that the warmed Chrome is
    attachable. If it is NOT, we return a fast summary carrying
    ``halt_reason='cdp_unreachable'`` WITHOUT spawning — the sidecar's existing
    halt_reason branch then nacks with that (non-poison) reason so dispatch requeues with
    backoff, exactly as for any other transient failure. This avoids the ~180s
    connect_over_cdp hang + crash-retry that otherwise prolongs the campaign block.

    ``popen``/``sleep``/``monotonic``/``cdp_probe`` are injected (mirroring ``Sidecar``'s
    ``sleep`` seam) so the whole supervisor is unit-testable with a fake process — no real
    spawn and no real browser.
    """
    run_id = job.run_id or uuid.uuid4().hex[:12]
    if not cdp_probe(cfg.cdp_url):
        # The warmed Chrome is unreachable/logged-out/bad. Do NOT spawn the child (it
        # would hang ~180s then crash). Return a fast, requeue-with-backoff nack via the
        # existing halt_reason mapping — no new terminal path.
        log.warning("Job %s pre-flight CDP probe failed (%s) — skipping spawn, nacking "
                    "%s", job.id, cfg.cdp_url, CDP_UNREACHABLE_REASON)
        return {"run_id": run_id, "job_id": job.id,
                "halt_reason": CDP_UNREACHABLE_REASON}
    spec_path = _spec_path(cfg.state_dir, job.id)
    result_path = _result_path(cfg.state_dir, job.id)
    job_log = _job_log_path(cfg.state_dir, job.id)

    _write_spec(spec_path, result_path, job, cfg, run_id)
    log.info("Supervising job %s · campaign=%s platform=%s run_id=%s target=%s · log=%s",
             job.id, job.campaign_id, job.platform, run_id, job.target_leads, job_log)

    pid_path = _pid_path(cfg.state_dir, job.id)
    log_fh = None
    proc = None
    try:
        job_log.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(job_log, "ab")  # append: a re-leased job's attempts accrete
        proc = popen(
            [sys.executable, "-m", "aizu.worker.job_child", "--spec-file",
             str(spec_path)],
            stdout=log_fh, stderr=subprocess.STDOUT,
            env=_child_env(run_id, cfg),
        )
        _write_pidfile(pid_path, proc.pid)
        outcome = _supervise(proc, job=job, cfg=cfg, run_id=run_id, halt=halt,
                             job_log=job_log, sleep=sleep, monotonic=monotonic)
        if outcome is not None:
            return outcome  # halt/timeout — child already terminated
        return _read_and_map_result(result_path, job=job, run_id=run_id,
                                    returncode=proc.returncode, job_log=job_log)
    finally:
        # LOCKED-invariant safety: guarantee no live child outlives run_one_job on ANY
        # exit path — a normal return, a mapped-nack raise, OR an unexpected exception
        # (e.g. KeyboardInterrupt / an OSError from poll/sleep) escaping _supervise while
        # the child is still alive. Without this, an orphaned child would keep driving the
        # warmed account with no lease and no lock, and a later lease could launch a SECOND
        # child against the same account (review CRITICAL).
        if proc is not None and _maybe_alive(proc):
            log.warning("Job %s left a live child on an unexpected exit — terminating",
                        job.id)
            _terminate(proc, sleep=sleep)
        _cleanup(log_fh, spec_path, result_path, pid_path)


def _supervise(proc, *, job: JobSpec, cfg: WorkerConfig, run_id: str,
               halt: threading.Event, job_log: Path,
               sleep: Callable[[float], None],
               monotonic: Callable[[], float]) -> Optional[dict]:
    """Poll the child until it exits, a halt fires, the duration cap passes, or it STALLS
    (writes nothing to its job log for ``cfg.stall_timeout_sec``). Returns a summary dict
    when the supervisor itself stopped the child (halt → the sidecar's halt branch takes
    over; duration → ``halt_reason='worker_timeout'``; stall → ``halt_reason='worker_stall'``,
    which the sidecar nacks → requeues, so the retry resumes past the already-seen wedging
    reel), or ``None`` when the child exited on its own (caller reads the result file).

    The stall watchdog is the PREEMPTIVE backstop the engine's own cooperative per-reel
    deadline can't be: a blocked synchronous Playwright/CDP call never returns control to
    the loop that checks that deadline, so only killing the child from OUT here (the parent)
    can break a wedge. Log growth is the liveness signal — a healthy run appends relevance/
    CDP/heartbeat lines continuously; a wedged one goes silent. A value <= 0 disables it."""
    deadline = monotonic() + _effective_minutes(job, cfg) * 60 + TIMEOUT_GRACE_MIN * 60
    stall_sec = cfg.stall_timeout_sec
    last_size = _log_size(job_log)
    last_progress_at = monotonic()
    while True:
        if proc.poll() is not None:
            return None  # child exited on its own — read the result file
        if halt.is_set():
            _terminate(proc, sleep=sleep)
            log.warning("Job %s hard-stopped mid-run (halt)", job.id)
            # A bare summary — the sidecar checks controls.halt FIRST and nacks with the
            # operator's own halt_reason, so halt always wins over any value here.
            return {"run_id": run_id, "job_id": job.id, "halt_reason": "operator_stop"}
        if monotonic() >= deadline:
            _terminate(proc, sleep=sleep)
            log.warning("Job %s exceeded its duration cap — terminated (worker_timeout)",
                        job.id)
            return {"run_id": run_id, "job_id": job.id, "halt_reason": "worker_timeout"}
        if stall_sec > 0:
            size = _log_size(job_log)
            now = monotonic()
            if size != last_size:
                last_size = size
                last_progress_at = now
            elif now - last_progress_at >= stall_sec:
                _terminate(proc, sleep=sleep)
                log.warning("Job %s made no progress for %.0fs — terminated (worker_stall)",
                            job.id, stall_sec)
                return {"run_id": run_id, "job_id": job.id, "halt_reason": "worker_stall"}
        sleep(SUPERVISOR_POLL_SEC)


def _log_size(job_log: Path) -> int:
    """Byte size of the child's job log, or 0 if it doesn't exist yet. Never raises —
    a stat error must not crash the supervisor (it just reads as no-growth this tick)."""
    try:
        return job_log.stat().st_size
    except OSError:
        return 0


def _terminate(proc, *, sleep: Callable[[float], None]) -> None:
    """SIGTERM the child, give it ``SIGTERM_GRACE_SEC`` to exit, then SIGKILL. Never
    raises past here — teardown must not mask the run outcome (mirrors Lock.release)."""
    try:
        proc.terminate()
    except Exception as e:  # noqa: BLE001 — child may already be gone
        log.warning("terminate() raised (continuing to kill): %s", e)
    waited = 0.0
    while waited < SIGTERM_GRACE_SEC:
        try:
            if proc.poll() is not None:
                return
        except Exception:  # noqa: BLE001 — can't determine liveness → escalate to kill
            break
        sleep(SUPERVISOR_POLL_SEC)
        waited += SUPERVISOR_POLL_SEC
    try:
        proc.kill()
    except Exception as e:  # noqa: BLE001 — best-effort escalation
        log.warning("kill() raised: %s", e)


def _maybe_alive(proc) -> bool:
    """Best-effort liveness. If poll() itself raises (e.g. a reap race), we cannot be sure
    the child is dead, so assume ALIVE and let the caller terminate — never leave a
    possibly-live child behind."""
    try:
        return proc.poll() is None
    except Exception:  # noqa: BLE001
        return True


def _read_and_map_result(result_path: Path, *, job: JobSpec, run_id: str,
                         returncode: Optional[int], job_log: Path) -> dict:
    """Map a naturally-exited child's result file to a return value or a raised
    exception. A missing/garbled result with a non-zero exit is a crash."""
    result = _load_result(result_path)
    if result is None:
        raise ChildCrashed(returncode, _tail(job_log))
    if result.get("ok"):
        summary = result.get("summary")
        if isinstance(summary, dict):
            return summary
        raise ChildCrashed(returncode, "result ok but summary missing/not an object")
    kind = result.get("kind")
    exc_cls = _KIND_TO_EXC.get(kind, RuntimeError)
    reason = result.get("error") or kind or "unknown"
    raise exc_cls(f"job {job.id}: {reason}")


# --- spec / result on-disk contract ----------------------------------------------

def _write_spec(spec_path: Path, result_path: Path, job: JobSpec, cfg: WorkerConfig,
                run_id: str) -> None:
    payload = {**job.to_payload(), "runId": run_id}  # force the parent's chosen run_id
    atomic_write_json(spec_path, {
        "job": payload,
        "cfg": cfg.to_child_dict(),
        "resultFile": str(result_path),
    })


def _write_pidfile(pid_path: Path, pid: int) -> None:
    """Record the child PID so a startup reaper can kill an orphan a whole-sidecar crash
    left behind (the in-process finally handles the common case). Best-effort."""
    try:
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(pid), encoding="utf-8")
    except OSError as e:
        log.warning("could not write child pidfile: %s", e)


def _load_result(result_path: Path) -> Optional[dict]:
    """Tolerant read of the child's result file (never trust it blindly — llm-json-output
    rule for any text a separate process produced). Returns None on absent/garbled."""
    if not result_path.exists():
        return None
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("job result file unreadable: %s", result_path.name, exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def _child_env(run_id: str, cfg: WorkerConfig) -> dict:
    """The child inherits the sidecar's FULL environment (it needs AIZU_SECRET_KEY /
    OPENROUTER_API_KEY etc. for a live run) plus AIZU_RUN_ID (so the child's own
    configure_logging writes the per-run log the desktop shell tails, D4) and a
    state-dir-rooted base log path (so worker logs live under the box's state dir, not
    engine/logs, once packaged)."""
    return {
        **os.environ,
        "AIZU_RUN_ID": run_id,
        "AIZU_LOG_FILE": str(cfg.state_dir / _JOB_LOG_DIRNAME / "aizu.log"),
    }


def _cleanup(log_fh, spec_path: Path, result_path: Path, pid_path: Path) -> None:
    """Independent teardown steps — one failing must not skip the next (Sidecar.close
    pattern). The per-job log file itself is KEPT for postmortem/tail; the sweep bounds
    it by age."""
    if log_fh is not None:
        try:
            log_fh.close()
        except OSError as e:
            log.warning("closing job log handle failed: %s", e)
    _unlink_quietly(spec_path)
    _unlink_quietly(result_path)
    _unlink_quietly(pid_path)


# --- the in-process body (runs inside the child, and directly in tests) -----------

def _execute_job(store, job: JobSpec, *, cfg: WorkerConfig) -> dict:
    """Resolve and run one leased job IN THIS PROCESS. Returns the engine summary dict
    augmented with ``run_id``/``job_id``. Raises :class:`CampaignNotFound`/
    :class:`SoulMissing`/``ValueError`` for unrunnable jobs (the caller maps these to a
    hard nack); a *halt* is folded into the returned ``halt_reason`` and is NOT raised.

    This is the historical ``run_one_job`` body, unchanged — it now runs inside
    ``job_child`` under the supervisor, or directly in unit tests."""
    # resolve_campaign raises ValueError on a malformed brief (caller → nack
    # 'campaign_malformed'); returns None for an unknown id (→ CampaignNotFound).
    campaign = resolve_campaign(store, cfg.cfg_dir, job.campaign_id)
    if campaign is None:
        raise CampaignNotFound(job.campaign_id)

    soul = _resolve_soul(job, cfg.cfg_dir)
    # Prefer the cloud-assigned run_id (so the org drawer + the heartbeat run_events
    # sync all key on the SAME id); fall back to a local id for a legacy job.
    run_id = job.run_id or uuid.uuid4().hex[:12]
    pause_path = cfg.state_dir / f"run-{run_id}{_PAUSE_SUFFIX}"
    args = cfg.run_args(job)

    log.info("Running job %s · campaign=%s platform=%s run_id=%s target=%s",
             job.id, job.campaign_id, job.platform, run_id, job.target_leads)

    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    with _run_env(run_id, pause_path):
        try:
            summary = cli._run_session_loop(
                campaign=campaign, store=store, soul=soul, args=args)
        finally:
            # The pause-file is the engine's cooperative checkpoint; the worker owns
            # its whole lifecycle, so delete any sentinel this run created even if the
            # session raised (BUILD-PLAN C6).
            _unlink_quietly(pause_path)
    return {**summary, "run_id": run_id, "job_id": job.id}


def _resolve_soul(job: JobSpec, cfg_dir: Path) -> Soul:
    """Prefer the soul baked into the job spec (fail-fast in the cloud at enqueue,
    the chosen Phase-1 contract); else load the box-local soul.md; else SoulMissing."""
    if job.soul_text:
        return Soul(text=job.soul_text, path=_BAKED_SOUL_PATH)
    local = cfg_dir / "soul.md"
    if local.exists():
        return Soul(text=local.read_text(encoding="utf-8"), path=local)
    raise SoulMissing(
        f"job {job.id} has no soul_text and no {local} on this box")


@contextmanager
def _run_env(run_id: str, pause_path: Path) -> Iterator[None]:
    """Stamp AIZU_RUN_ID (so Session._emit streams run_events for this run) and
    AIZU_PAUSE_FILE (the cooperative-pause sentinel), restoring the prior values
    afterward so jobs never leak env into one another.

    The child runs exactly ONE job, so mutating these process-wide is safe; the guard
    below turns a hypothetical in-process reentry into a loud error (belt-and-suspenders,
    no longer load-bearing now that jobs run in their own process).
    """
    if not _RUN_ENV_GUARD.acquire(blocking=False):
        raise RuntimeError(
            "_run_env is already active in this process — jobs must run one at a time "
            "(one-account/one-box/one-live-job invariant); concurrent runs need a "
            "subprocess boundary")
    prev_run_id = os.environ.get("AIZU_RUN_ID")
    prev_pause = os.environ.get("AIZU_PAUSE_FILE")
    os.environ["AIZU_RUN_ID"] = run_id
    os.environ["AIZU_PAUSE_FILE"] = str(pause_path)
    try:
        yield
    finally:
        _restore_env("AIZU_RUN_ID", prev_run_id)
        _restore_env("AIZU_PAUSE_FILE", prev_pause)
        _RUN_ENV_GUARD.release()


def _restore_env(key: str, prev: Optional[str]) -> None:
    if prev is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = prev


# --- path helpers + sweeps --------------------------------------------------------

def _sanitize_id(raw: str) -> str:
    """Filesystem-safe path component (identical policy to single_flight's sanitizer):
    alnum or '-_' pass through, everything else → '_'. Prevents a malformed job_id from
    escaping the intended directory."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in raw)


def _spec_path(state_dir: Path, job_id: str) -> Path:
    return state_dir / _SPEC_DIRNAME / f"{_sanitize_id(job_id)}.json"


def _result_path(state_dir: Path, job_id: str) -> Path:
    return state_dir / _RESULT_DIRNAME / f"{_sanitize_id(job_id)}.json"


def _pid_path(state_dir: Path, job_id: str) -> Path:
    return state_dir / _SPEC_DIRNAME / f"{_sanitize_id(job_id)}.pid"


def _job_log_path(state_dir: Path, job_id: str) -> Path:
    return (state_dir / _JOB_LOG_DIRNAME
            / f"{_JOB_LOG_PREFIX}{_sanitize_id(job_id)}{_JOB_LOG_SUFFIX}")


def _effective_minutes(job: JobSpec, cfg: WorkerConfig) -> int:
    """The SAME duration the engine itself is given (cfg.run_args), so the supervisor's
    deadline never disagrees with the engine's own internal cap."""
    return job.duration_minutes or cfg.max_job_minutes


def _tail(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-_CRASH_LOG_TAIL_BYTES:].decode("utf-8", errors="replace")


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as e:  # noqa: BLE001 — cleanup must not mask the run outcome
        log.warning("Could not unlink %s: %s", path, e)


def sweep_orphan_pause_files(state_dir: Path, *, max_age_sec: float) -> int:
    """Delete ``run-*.pause`` sentinels older than ``max_age_sec`` (a crashed run's
    orphans, which would otherwise idle the NEXT run that reused the dir). Returns the
    count swept. Called once on sidecar startup (BUILD-PLAN C6, swept in Phase 1)."""
    if not state_dir.exists():
        return 0
    now = time.time()
    swept = 0
    for path in state_dir.glob(f"run-*{_PAUSE_SUFFIX}"):
        try:
            if now - path.stat().st_mtime > max_age_sec:
                path.unlink(missing_ok=True)
                swept += 1
                log.warning("Swept orphan pause sentinel %s", path.name)
        except OSError:
            continue
    return swept


def sweep_orphan_job_files(state_dir: Path, *, max_age_sec: float) -> int:
    """Delete stale supervisor rendezvous files a crashed SIDECAR (not just a crashed
    child) could leave: job-specs/*.json, job-results/*.json, and logs/job-*.log*. A
    leaked spec/result blocks nothing (the supervisor rewrites per job) but grows disk
    over a long-lived box's life; job logs are kept longer for operator postmortem, so
    the caller passes a longer age for those via a separate sweep if desired. Returns the
    total swept. Never raises (each unlink guarded)."""
    if not state_dir.exists():
        return 0
    now = time.time()
    swept = 0
    # The trailing '*' catches the atomic-write '.json.tmp' temp files a hard kill can
    # leave behind (which a bare '*.json' would miss and leak forever) and rotated logs.
    patterns = (f"{_SPEC_DIRNAME}/*.json*", f"{_RESULT_DIRNAME}/*.json*",
                f"{_SPEC_DIRNAME}/*.pid",
                f"{_JOB_LOG_DIRNAME}/{_JOB_LOG_PREFIX}*{_JOB_LOG_SUFFIX}*")
    for pattern in patterns:
        for path in state_dir.glob(pattern):
            try:
                if now - path.stat().st_mtime > max_age_sec:
                    path.unlink(missing_ok=True)
                    swept += 1
            except OSError:
                continue
    return swept


def reap_orphan_children(state_dir: Path) -> int:
    """Kill any child recorded in a pidfile that is STILL ALIVE at sidecar startup — it
    can only be an orphan (a healthy run removes its pidfile in the finally; a fresh
    sidecar has no active jobs). This closes the whole-sidecar-crash case the in-process
    finally cannot reach, protecting the one-account/one-box/one-live-job invariant. Never
    raises. Returns the number killed."""
    spec_dir = state_dir / _SPEC_DIRNAME
    if not spec_dir.exists():
        return 0
    killed = 0
    for pf in spec_dir.glob("*.pid"):
        try:
            pid = int(pf.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            _unlink_quietly(pf)
            continue
        if _pid_alive(pid):
            log.warning("Reaping orphaned job child pid=%d from a prior sidecar", pid)
            try:
                # SIGKILL on POSIX; Windows has no SIGKILL, so fall back to SIGTERM
                # (os.kill(pid, SIGTERM) → TerminateProcess — an unconditional kill).
                os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
                killed += 1
            except OSError as e:
                log.warning("could not kill orphan pid=%d: %s", pid, e)
        _unlink_quietly(pf)
    return killed


def _pid_alive(pid: int) -> bool:
    """True if a process with ``pid`` currently exists. Cross-platform (see
    ``_atomic_io.pid_alive``): POSIX probes with signal 0, Windows via OpenProcess.
    Ambiguous cases resolve to NOT-alive so orphan reaping never signals an
    unconfirmed pid."""
    return pid_alive(pid, unknown_is_alive=False)
