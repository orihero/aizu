"""Run launcher for the bridge server (PRD §5 revision, v5).

The bridge server may now *spawn* the engine — one campaign or all live ones —
as a background subprocess in response to `POST /api/run`. This module owns that
lifecycle:

  - `build_argv`     pure mapping from a RunSpec to a `aizu` CLI argv.
  - `default_spawner`  the real subprocess launch (injectable for tests).
  - `RunManager`     a single-run lock, a daemon monitor thread that records the
                     exit result, and a JSON-ready `status()` for `/api/state`.

Only ONE run executes at a time (a live run drives one real browser/account, so
concurrency would clash). SQLite remains the durable record of what a run did;
RunManager state is best-effort in-memory and is lost on a server restart.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Optional

from .core.logsetup import get_logger

log = get_logger(__name__)

# How many finished runs to keep for the panel's "recent" list.
RECENT_LIMIT = 10
# Bytes of the tail log to scan for a one-line summary on exit. Generous on
# purpose: a crash's exception line sits BELOW its frames but the anchoring
# "Traceback (...)" header sits ABOVE them, so a tail too small to hold the whole
# block silently dropped the header and degraded a real crash to a bare "exit 1"
# (observed live: a 4.3 KB CDP-failure log vs the old 4 KB window).
_LOG_TAIL_BYTES = 64 * 1024
# How much of the tail to echo to the console when a run fails, so the operator
# sees the actual stack trace inline without opening the file.
_ERROR_DETAIL_BYTES = 16 * 1024

# A column-0 line that looks like a Python exception, e.g.
# ``playwright._impl._errors.Error: …`` or ``ValueError: …``. Used as a fallback
# when the "Traceback" header has scrolled out of the scanned window entirely.
_EXCEPTION_LINE_RE = re.compile(
    r"^\S.*?(?:Error|Exception|Interrupt|Timeout|SystemExit)(?:\b|:)")

VALID_RUN_MODES = ("dry", "live")
VALID_RUN_SCOPES = ("campaign", "all")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RunSpec:
    """What to launch. `campaign_id` is None for a batch ('all') run. `org_id` scopes
    a batch run to one org's live campaigns (v7 multi-tenancy)."""
    scope: str            # 'campaign' | 'all'
    campaign_id: Optional[str]
    mode: str             # 'dry' | 'live'
    org_id: Optional[int] = None
    # Live lead target: keep running back-to-back sessions until N leads (matched
    # comments) are found, then stop (campaign scope only). None → one session.
    target_leads: Optional[int] = None
    # Optional wall-clock safety cap (minutes): stop once it elapses even if the lead
    # target hasn't been reached (campaign scope only). On its own it is the legacy
    # timed run. None → no time cap.
    duration_minutes: Optional[int] = None
    # v12: who launched this run — 'manual' (operator via /api/run) or 'scheduled'
    # (the ScheduleManager daemon). Metadata only (never changes the argv); threaded
    # into run history so "why did this run?" is answerable.
    launch_source: str = "manual"


@dataclass(frozen=True)
class ActiveRun:
    run_id: str
    scope: str
    campaign_id: Optional[str]
    mode: str
    started_at: str       # ISO-8601
    pid: int              # server-internal; not exposed on the wire
    org_id: Optional[int] = None  # v7: which org launched it (for per-org status)
    launch_source: str = "manual"  # v12: 'manual' | 'scheduled'


@dataclass(frozen=True)
class RunResult:
    run_id: str
    scope: str
    campaign_id: Optional[str]
    mode: str
    started_at: str
    finished_at: Optional[str]
    outcome: str          # 'ok' | 'error' | 'aborted'
    summary: str
    org_id: Optional[int] = None  # v7: owning org (for per-org status filtering)
    # Multi-platform fan-out (G1): the engine's per-channel breakdown and classified
    # halt kind, lifted from its JSON summary for the panel. None for a single-platform
    # or batch run (which print neither key).
    per_platform: Optional[dict] = None
    halt_kind: Optional[str] = None
    launch_source: str = "manual"  # v12: 'manual' | 'scheduled'


# A spawner takes (argv, cwd, env, log_path) and returns a process-like object
# exposing `.pid`, `.wait() -> int` and `.returncode`. Injectable so tests never
# spawn a real process.
Spawner = Callable[[list[str], Path, dict, Path], Any]


def build_argv(spec: RunSpec, python_exe: str, db_path: str,
               config_dir: str) -> list[str]:
    """Pure RunSpec → `aizu` CLI argv.

    `--db` is a global option and must precede the subcommand. A single-campaign
    run always uses `--campaign <id>` (the engine's resolver also accepts the file
    campaign's own id, so no special case is needed). Dry mode appends `--dry-run`.
    """
    argv = [python_exe, "-m", "aizu.cli", "--db", db_path]
    if spec.scope == "all":
        argv += ["run-all", "--config", config_dir]
        if spec.org_id is not None:        # scope the batch to this org's live campaigns
            argv += ["--org", str(spec.org_id)]
    else:
        argv += ["run", "--config", config_dir, "--campaign", spec.campaign_id or ""]
        if spec.target_leads:
            argv += ["--target-leads", str(spec.target_leads)]
        if spec.duration_minutes:
            argv += ["--duration-minutes", str(spec.duration_minutes)]
    if spec.mode == "dry":
        argv.append("--dry-run")
    return argv


def default_spawner(argv: list[str], cwd: Path, env: dict, log_path: Path) -> Any:
    """Launch the engine detached, streaming stdout+stderr to a per-run logfile."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "ab")
    try:
        return subprocess.Popen(argv, cwd=str(cwd), env=env,
                                stdout=fh, stderr=subprocess.STDOUT)
    finally:
        # The child holds its own dup of the fd; the parent's copy isn't needed.
        fh.close()


def _read_log_tail(log_path: Path) -> str:
    try:
        data = log_path.read_bytes()
    except OSError:
        return ""
    return data[-_LOG_TAIL_BYTES:].decode("utf-8", errors="replace").strip()


def _extract_json(text: str) -> Optional[dict]:
    """Tolerantly pull the last JSON object the engine printed (its run summary)."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _exception_line(text: str) -> Optional[str]:
    """Pull the exception line out of a Python traceback in `text`, or None.

    When a spawned run crashes (e.g. CDP `connect ECONNREFUSED 127.0.0.1:9333`),
    stdout/stderr ends in a traceback whose final frame is followed by a column-0
    ``module.path.SomeError: message`` line. The JSON/HALTED scans miss it, so the
    run used to summarize to a useless ``exit 1``. Returning this line is what lets
    the console's ✗ "Run finished" log and the panel's per-campaign last-run summary
    say *why* a run died. (`Call log:` lines that some libraries append after the
    exception are column-0 too, but the exception is the FIRST such line.)
    """
    lines = text.splitlines()
    headers = [i for i, ln in enumerate(lines)
               if ln.startswith("Traceback (most recent call last):")]
    if headers:
        for ln in lines[headers[-1] + 1:]:  # first column-0 line after the frames
            if ln and not ln[0].isspace():
                return ln.strip()[:200]
        return None
    # No header in the scanned window (a very long traceback, or one truncated by
    # log rotation): fall back to the LAST column-0 line that looks like an
    # exception. Bottom-up so trailing 'Call log:' noise can't mask the real line.
    for ln in reversed(lines):
        if ln and not ln[0].isspace() and _EXCEPTION_LINE_RE.match(ln):
            return ln.strip()[:200]
    return None


def _error_detail(log_path: Path) -> str:
    """The last traceback block (its header through the end) for echoing to the
    console when a run fails, so the operator sees the real stack trace inline.
    Falls back to the raw tail when no traceback header is present."""
    tail = _read_log_tail(log_path)[-_ERROR_DETAIL_BYTES:].strip()
    if not tail:
        return ""
    idx = tail.rfind("Traceback (most recent call last):")
    return tail[idx:].strip() if idx >= 0 else tail


def _extract_run_breakdown(log_path: Path) -> tuple[Optional[dict], Optional[str]]:
    """The engine's per-platform fan-out breakdown + classified halt kind from its
    JSON summary, for the panel (G1). Returns (per_platform, halt_kind), each None
    when absent (a single-platform or batch run prints neither)."""
    obj = _extract_json(_read_log_tail(log_path))
    if not isinstance(obj, dict):
        return None, None
    per = obj.get("per_platform")
    kind = obj.get("halt_kind")
    return (per if isinstance(per, dict) else None,
            kind if isinstance(kind, str) else None)


def _summarize(exit_code: int, log_path: Path) -> str:
    """Best-effort one-liner for the panel's recent list.

    The engine prints a single JSON summary; surface its headline counts. Fall back
    to a halt/error line, then the traceback's exception line, then the raw exit code.
    """
    tail = _read_log_tail(log_path)
    obj = _extract_json(tail)
    if obj is not None:
        if "ran" in obj:        # run-all batch summary
            return (f"ran {obj.get('ran')}, ok {obj.get('ok')}, "
                    f"halted {obj.get('halted')}")
        if "matches" in obj:    # single-session summary
            return f"matches {obj.get('matches')}, spend ${float(obj.get('spend_usd', 0)):.4f}"
    for line in reversed(tail.splitlines()):
        s = line.strip()
        if s.startswith(("HALTED:", "error:")):
            return s[:200]
    if exit_code != 0:          # a crash: surface the real reason, not just "exit 1"
        exc = _exception_line(tail)
        if exc:
            return exc
    return f"exit {exit_code}"


def _resolve_run_python(engine_root: Path) -> str:
    """The interpreter used to spawn run children.

    Prefer the engine's own virtualenv (`engine/.venv`): it carries the live-run
    deps (notably Playwright). The panel may be launched under a bare system
    Python that lacks them — falling back to that interpreter makes every live
    run die at the "Playwright is not installed" guard while dry runs still pass
    (they need neither Playwright nor CDP). Use sys.executable only when no venv
    is present.
    """
    venv_python = engine_root / ".venv" / "bin" / "python"
    return str(venv_python) if venv_python.exists() else sys.executable


class RunManager:
    """Owns the single in-flight engine run and a short history of recent ones."""

    def __init__(self, *, db_path: str, config_dir: str, engine_root: str | Path,
                 log_dir: str | Path, spawner: Spawner = default_spawner,
                 python_exe: Optional[str] = None, recent_limit: int = RECENT_LIMIT):
        self._db_path = str(db_path)
        self._config_dir = str(config_dir)
        self._engine_root = Path(engine_root)
        self._log_dir = Path(log_dir)
        self._spawner = spawner
        self._python_exe = python_exe or _resolve_run_python(self._engine_root)
        self._lock = threading.Lock()
        self._active: Optional[ActiveRun] = None
        # The live child process for the active run, kept so stop() can terminate it.
        self._active_proc: Optional[Any] = None
        # run_id an operator asked to stop, so the monitor records 'aborted' (not 'error').
        self._aborting_run_id: Optional[str] = None
        self._recent: Deque[RunResult] = deque(maxlen=recent_limit)
        # The log dir is created lazily on the first launch (default_spawner), so
        # merely constructing a manager has no filesystem side effect.

    def _pause_path(self, run_id: str) -> Path:
        """The cooperative-pause sentinel for a run, keyed on run_id ONLY.

        v12: pause() creates this file; the engine polls it between reels and idles
        while it exists; resume()/the monitor delete it. NOTE the suffix is `.pause`
        and the key is run_id alone — NOT run-<id>-<scope> like the log path — so the
        producer (pause/resume/_child_env) and the consumer (the engine) derive the
        exact same path from one helper and can never drift."""
        return self._log_dir / f"run-{run_id}.pause"

    def _child_env(self, run_id: str, spec: RunSpec) -> dict:
        # Copy (don't alias) so OPENROUTER_API_KEY and model overrides inherit and
        # any future scrubbing has a single chokepoint. v10: stamp the run_id (and the
        # launching org) so the engine can correlate its run-activity events back to
        # this in-memory run — passed by env, not argv, so the CLI surface is unchanged.
        env = dict(os.environ)
        env["AIZU_RUN_ID"] = run_id
        if spec.org_id is not None:
            env["AIZU_ORG_ID"] = str(spec.org_id)
        # v12: tell the engine exactly which file to poll for a cooperative pause, so
        # the consumer never has to re-derive the path itself.
        env["AIZU_PAUSE_FILE"] = str(self._pause_path(run_id))
        return env

    def launch(self, spec: RunSpec) -> tuple[Optional[ActiveRun], Optional[str]]:
        """Start a run. Returns (ActiveRun, None) or (None, error_message).

        Rejects with "a run is already active" when one is in flight (→ 409) and
        with a "failed to start engine" message when the spawn itself fails (→ 500).
        """
        run_id = uuid.uuid4().hex[:12]
        log_path = self._log_dir / f"run-{run_id}-{spec.scope}.log"
        argv = build_argv(spec, self._python_exe, self._db_path, self._config_dir)
        with self._lock:
            if self._active is not None:
                log.warning("Run rejected · already active (id=%s)", self._active.run_id)
                return None, "a run is already active"
            try:
                proc = self._spawner(argv, self._engine_root,
                                     self._child_env(run_id, spec), log_path)
            except Exception as e:  # noqa: BLE001 — surface as a value, never crash the handler
                log.error("Failed to spawn run · id=%s scope=%s · %s", run_id, spec.scope, e)
                return None, f"failed to start engine: {e}"
            active = ActiveRun(run_id=run_id, scope=spec.scope,
                               campaign_id=spec.campaign_id, mode=spec.mode,
                               started_at=_now_iso(), pid=int(getattr(proc, "pid", -1)),
                               org_id=spec.org_id, launch_source=spec.launch_source)
            self._active = active
            self._active_proc = proc
        log.info("Spawned run · id=%s scope=%s campaign=%s mode=%s pid=%s",
                 run_id, spec.scope, spec.campaign_id, spec.mode, active.pid)
        monitor = threading.Thread(target=self._monitor, args=(proc, active, log_path),
                                   daemon=True)
        monitor.start()
        return active, None

    def stop(self, org_id: Optional[int] = None) -> tuple[bool, Optional[str]]:
        """Terminate the in-flight run early. Returns (True, None) once the stop is
        requested, or (False, message) when nothing matching is active.

        org-scoped like status(): a tenant can neither stop nor learn about another
        org's run — a foreign run reads as "no run is active". The monitor thread
        observes the child exit and records the result with outcome 'aborted'.
        """
        with self._lock:
            active = self._active
            proc = self._active_proc
            if active is None or proc is None:
                return False, "no run is active"
            if org_id is not None and active.org_id != org_id:
                return False, "no run is active"
            self._aborting_run_id = active.run_id
        try:
            proc.terminate()
        except Exception as e:  # noqa: BLE001 — surface as a value, never crash the handler
            log.error("Failed to stop run · id=%s · %s", active.run_id, e)
            return False, f"failed to stop run: {e}"
        log.info("Run stop requested · id=%s", active.run_id)
        return True, None

    def stop_campaign(self, campaign_id: str,
                      org_id: Optional[int] = None) -> tuple[bool, Optional[str]]:
        """Stop the in-flight run ONLY if it is a campaign-scoped run for this exact
        campaign (and org). Used by archive-while-live so archiving one campaign never
        kills an unrelated single run or a whole 'all' batch. Returns (False, reason)
        when nothing matching is active — callers treat that as 'nothing to stop',
        not an error."""
        with self._lock:
            active = self._active
            if active is None:
                return False, "no run is active"
            if org_id is not None and active.org_id != org_id:
                return False, "no run is active"
            if active.scope != "campaign" or active.campaign_id != campaign_id:
                return False, "active run is not for this campaign"
        return self.stop(org_id)

    def pause(self, org_id: Optional[int] = None) -> tuple[bool, Optional[str]]:
        """Cooperatively pause the in-flight run by creating its pause sentinel — the
        engine idles between reels while it exists. org-scoped like stop(); a foreign
        run reads as "no run is active". Idempotent: pausing an already-paused run is
        a no-op success. Unlike stop(), the child keeps living, so resume() continues
        from where it idled."""
        with self._lock:
            active = self._active
            if active is None or self._active_proc is None:
                return False, "no run is active"
            if org_id is not None and active.org_id != org_id:
                return False, "no run is active"
            run_id = active.run_id
        try:
            self._pause_path(run_id).parent.mkdir(parents=True, exist_ok=True)
            self._pause_path(run_id).touch()
        except OSError as e:  # noqa: BLE001 — surface as a value, never crash the handler
            log.error("Failed to pause run · id=%s · %s", run_id, e)
            return False, f"failed to pause run: {e}"
        log.info("Run pause requested · id=%s", run_id)
        return True, None

    def resume(self, org_id: Optional[int] = None) -> tuple[bool, Optional[str]]:
        """Lift a cooperative pause by deleting the sentinel. org-scoped; idempotent
        (resuming a not-paused active run is a no-op success). 409 only when nothing
        of yours is active."""
        with self._lock:
            active = self._active
            if active is None or self._active_proc is None:
                return False, "no run is active"
            if org_id is not None and active.org_id != org_id:
                return False, "no run is active"
            run_id = active.run_id
        self._pause_path(run_id).unlink(missing_ok=True)
        log.info("Run resume requested · id=%s", run_id)
        return True, None

    def _is_paused(self, run_id: str) -> bool:
        return self._pause_path(run_id).exists()

    def sweep_orphan_pause_files(self) -> None:
        """Delete any *.pause sentinel with no matching active run. A killed-then-
        restarted server loses its in-memory active slot but a paused child's pause
        file survives on disk; without this sweep a fresh process could leave a frozen
        zombie polling a file no live code removes. Best-effort: never raises."""
        try:
            files = list(self._log_dir.glob("run-*.pause"))
        except OSError:
            return
        with self._lock:
            active_id = self._active.run_id if self._active is not None else None
        for f in files:
            run_id = f.stem[len("run-"):]  # "run-<id>.pause" → "<id>"
            if run_id != active_id:
                f.unlink(missing_ok=True)

    def _monitor(self, proc: Any, active: ActiveRun, log_path: Path) -> None:
        """Wait for the child, record a RunResult, and ALWAYS clear the active slot.

        Clearing the lock is in a `finally` so a bug in result-building can never
        wedge the manager into a permanent "busy" state.
        """
        exit_code = -1
        try:
            try:
                exit_code = int(proc.wait())
            except Exception:  # noqa: BLE001 — a broken wait still clears the lock
                exit_code = -1
            with self._lock:
                aborted = self._aborting_run_id == active.run_id
            per_platform, halt_kind = (
                (None, None) if aborted else _extract_run_breakdown(log_path))
            result = RunResult(
                run_id=active.run_id, scope=active.scope, campaign_id=active.campaign_id,
                mode=active.mode, started_at=active.started_at, finished_at=_now_iso(),
                outcome="aborted" if aborted else ("ok" if exit_code == 0 else "error"),
                summary="stopped by operator" if aborted else _summarize(exit_code, log_path),
                org_id=active.org_id, per_platform=per_platform, halt_kind=halt_kind,
                launch_source=active.launch_source)
        except Exception as e:  # noqa: BLE001 — never leave the lock stuck
            result = RunResult(
                run_id=active.run_id, scope=active.scope, campaign_id=active.campaign_id,
                mode=active.mode, started_at=active.started_at, finished_at=_now_iso(),
                outcome="error", summary=f"monitor error: {e}", org_id=active.org_id,
                launch_source=active.launch_source)
        finally:
            # v12: a run that exits while paused (e.g. it hit its duration deadline
            # mid-pause) would otherwise leave an orphaned sentinel and a UI stuck on
            # "Paused". Deleting it here reconciles the state — the next status() reads
            # un-paused and the recorded outcome is the real one. The engine also
            # self-deletes on a natural loop exit; this is the belt to that suspenders.
            self._pause_path(active.run_id).unlink(missing_ok=True)
            with self._lock:
                self._recent.appendleft(result)
                self._active = None
                self._active_proc = None
                if self._aborting_run_id == active.run_id:
                    self._aborting_run_id = None
            (log.success if result.outcome == "ok" else log.error)(
                "Run finished · id=%s outcome=%s · %s",
                result.run_id, result.outcome, result.summary)
            # On a crash, echo the full stack trace and point at the complete log
            # so the operator can see *why* it died without hunting for the file.
            if result.outcome == "error":
                detail = _error_detail(log_path)
                log.error("Run %s failed — full log: %s%s",
                          result.run_id, log_path,
                          ("\n" + detail) if detail else "")

    def is_active(self) -> bool:
        """True iff a run is currently in flight (any org) — the readiness gate's
        short-circuit: a live run already owns the ONE CDP connection this
        architecture allows, so nothing may attach a second Playwright client while
        this is true (see readiness.check_readiness's ``run_active`` param)."""
        with self._lock:
            return self._active is not None

    def status(self, org_id: Optional[int] = None) -> dict:
        """JSON-ready RUN block for /api/state (camelCase; pid stays internal).

        v7: when `org_id` is given, the active run and recent history are filtered to
        that org so /api/state never discloses another tenant's campaign ids or run
        cadence. `org_id=None` (CLI/internal) returns the unfiltered global view."""
        with self._lock:
            active = self._active
            recent = list(self._recent)
        if org_id is not None:
            if active is not None and active.org_id != org_id:
                active = None
            recent = [r for r in recent if r.org_id == org_id]
        return {
            "active": None if active is None else {
                "id": active.run_id,  # v10: lets the panel correlate the active run to
                                      # its activity feed (random hex; org-filtered above)
                "scope": active.scope,
                "campaignId": active.campaign_id,
                "mode": active.mode,
                "startedAt": active.started_at,
                # v12: derived from the pause sentinel's existence — the single source
                # of truth the engine itself polls, so the panel and engine never drift.
                "paused": self._is_paused(active.run_id),
                "launchSource": active.launch_source,   # v12: 'manual' | 'scheduled'
            },
            "recent": [{
                "id": r.run_id,
                "scope": r.scope,
                "campaignId": r.campaign_id,
                "mode": r.mode,
                "startedAt": r.started_at,
                "finishedAt": r.finished_at,
                "outcome": r.outcome,
                "summary": r.summary,
                "perPlatform": r.per_platform,   # G1: per-channel fan-out breakdown
                "haltKind": r.halt_kind,
                "launchSource": r.launch_source,   # v12: 'manual' | 'scheduled'
            } for r in recent],
        }
