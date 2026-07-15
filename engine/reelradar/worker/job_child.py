"""Child-process entry point for one leased job (Phase 6 supervised-subprocess model).

Invoked by the ``job_runner`` supervisor as::

    python -m reelradar.worker.job_child --spec-file <path>

It reads the spec file the PARENT wrote (a validated JobSpec payload + a projected
WorkerConfig + the result-file path), opens its OWN Store on the shared SQLite DB (WAL
permits this), runs the job via ``job_runner._execute_job`` (the exact in-process body),
then writes a JSON result file the parent maps back to an ack/nack.

A single top-level guard wraps the ENTIRE body so EVERY exit path writes a result file —
an unrunnable job (campaign/soul/malformed) records its ``kind`` so the supervisor
re-raises the matching exception; any other exception records ``error``; a spec that
cannot even be parsed records ``spec_unreadable``. The outbound reason is a fixed code,
never a raw exception string (a secret-bearing message must not leave the box — the same
rule the sidecar's nack path already follows).

SIGTERM handling is DELIBERATELY absent: the supervisor's SIGTERM→SIGKILL is the hard-stop
mechanism, and default OS termination is correct here because (a) WAL SQLite needs no
clean close, (b) any pause sentinel this child created is an orphan already swept on the
next sidecar startup, and (c) the single-flight lock is owned/released by the PARENT, not
the child — so a killed child leaks nothing invariant-relevant. See BUILD-PLAN Phase 6a.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Optional

from . import (JOB_RESULT_KIND_CAMPAIGN_MALFORMED, JOB_RESULT_KIND_CAMPAIGN_NOT_FOUND,
               JOB_RESULT_KIND_ERROR, JOB_RESULT_KIND_SOUL_MISSING,
               JOB_RESULT_KIND_SPEC_UNREADABLE)
from .config import JobSpec, JobSpecError, WorkerConfig

# Exit codes: 0 = ran (result file's `ok` says success vs mapped-nack); 1 = a failure
# still recorded to the result file (spec unreadable, or a caught exception).
_EXIT_OK = 0
_EXIT_RECORDED_FAILURE = 1


def main(argv: Optional[list] = None) -> int:
    """Parse --spec-file, run the job, and ALWAYS write a result file. Returns an exit
    code the supervisor cross-checks against the result file's contents."""
    parser = argparse.ArgumentParser(prog="reelradar.worker.job_child")
    parser.add_argument("--spec-file", required=True)
    ns = parser.parse_args(argv)
    spec_path = Path(ns.spec_file)

    spec = _load_spec(spec_path)
    if spec is None:
        # We could not even learn the result-file path — nothing to write against.
        # Exit non-zero so the supervisor maps this to a crash (rare: the parent just
        # wrote this file, so an unreadable spec means a real FS problem).
        return _EXIT_RECORDED_FAILURE

    result_file = Path(spec["resultFile"])
    # Configure logging AFTER we know the spec is readable; REELRADAR_RUN_ID is already
    # in our env (the parent set it) so configure_logging writes the per-run log file.
    from .. import cli as _cli
    _cli._load_env()
    from ..core.logsetup import configure_logging
    configure_logging()

    try:
        job = JobSpec.from_payload(spec["job"])
        cfg = WorkerConfig.from_child_dict(spec["cfg"])
    except (JobSpecError, ValueError, KeyError, TypeError) as e:
        _write_result(result_file, ok=False, kind=JOB_RESULT_KIND_SPEC_UNREADABLE,
                      error=f"spec fields invalid: {type(e).__name__}")
        return _EXIT_RECORDED_FAILURE

    return _run_and_record(job, cfg, result_file)


def _run_and_record(job: JobSpec, cfg: WorkerConfig, result_file: Path) -> int:
    """Run the job in-process and record ok/summary or a mapped failure kind. Import
    job_runner lazily (it pulls in cli/engine deps) so a spec-parse failure above never
    pays that import cost."""
    from . import job_runner
    from ..core.store import Store

    store = None
    try:
        store = Store(cfg.db_path)
        summary = job_runner._execute_job(store, job, cfg=cfg)
        _write_result(result_file, ok=True, summary=summary)
        return _EXIT_OK
    except job_runner.CampaignNotFound:
        _write_result(result_file, ok=False, kind=JOB_RESULT_KIND_CAMPAIGN_NOT_FOUND,
                      error="campaign not found")
    except job_runner.SoulMissing:
        _write_result(result_file, ok=False, kind=JOB_RESULT_KIND_SOUL_MISSING,
                      error="soul missing")
    except ValueError:
        # A malformed brief from resolve_campaign — fixed code, not the raw message.
        _write_result(result_file, ok=False, kind=JOB_RESULT_KIND_CAMPAIGN_MALFORMED,
                      error="campaign brief malformed")
    except BaseException as e:  # noqa: BLE001 — EVERY path must record a result
        # Fixed outbound code so a secret-bearing exception string never leaves the box;
        # full detail is in the (redacted) per-run log the parent already captured.
        _write_result(result_file, ok=False, kind=JOB_RESULT_KIND_ERROR,
                      error=type(e).__name__)
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:  # noqa: BLE001 — WAL needs no clean close
                pass
    return _EXIT_RECORDED_FAILURE


def _load_spec(spec_path: Path) -> Optional[dict]:
    """Tolerant read of the parent-written spec file (validate the boundary). Returns
    None on any unreadable/garbled/ill-shaped file (the caller exits non-zero)."""
    try:
        data = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("resultFile"), str) or "job" not in data or "cfg" not in data:
        return None
    return data


def _write_result(result_file: Path, *, ok: bool, summary: Optional[dict] = None,
                  kind: Optional[str] = None, error: Optional[str] = None) -> None:
    """Atomically write the result JSON (tmp + os.replace) so the parent never reads a
    torn file even if SIGKILL lands mid-write."""
    payload: dict = {"ok": ok}
    if ok:
        payload["summary"] = summary or {}
    else:
        payload["kind"] = kind
        payload["error"] = error
    result_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = result_file.with_suffix(result_file.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, result_file)


if __name__ == "__main__":
    raise SystemExit(main())
