"""The supervisor side of the Phase-6 model (job_runner.run_one_job).

A fake Popen + fake clock let us prove the mid-run hard-stop, crash mapping, timeout,
and clean-completion paths WITHOUT spawning a real process (mirrors the injected-sleep
seam Sidecar already uses). The child's on-disk result file is pre-written by the test
to stand in for what the real child would write.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from aizu.worker import job_runner
from aizu.worker.config import JobSpec, WorkerConfig
from aizu.worker.job_runner import ChildCrashed, CampaignNotFound, SoulMissing


def _job(**kw) -> JobSpec:
    base = dict(id="job-1", org_id=1, campaign_id="c-acme", platform="instagram",
                soul_text="soul", run_id="run-1")
    base.update(kw)
    return JobSpec(**base)


class FakePopen:
    """Simulates the child process. ``polls`` is the sequence poll() returns before the
    process is considered still-running (None); ``terminate_exits`` decides whether a
    SIGTERM stops it (else only kill() does)."""

    def __init__(self, *, polls=(None, 0), terminate_exits=True):
        self._polls = list(polls)
        self._i = 0
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.pid = 4242
        self._terminate_exits = terminate_exits

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if self._i < len(self._polls):
            v = self._polls[self._i]
            self._i += 1
            if v is not None:
                self.returncode = v
            return v
        return None  # never exits on its own

    def terminate(self):
        self.terminated = True
        if self._terminate_exits:
            self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


def _popen_factory(fake):
    captured = {}

    def popen(argv, **kw):
        captured["argv"] = argv
        captured["kw"] = kw
        return fake
    popen.captured = captured
    return popen


def _write_child_result(cfg: WorkerConfig, job: JobSpec, payload: dict) -> None:
    p = job_runner._result_path(cfg.state_dir, job.id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")


NEVER_TIMEOUT = lambda: 0.0          # monotonic that never reaches the deadline
NOOP_SLEEP = lambda _s: None
PROBE_OK = lambda _url: True         # CDP pre-check passes → proceed to spawn (Fix B)
PROBE_BAD = lambda _url: False       # CDP pre-check fails → fast nack, no spawn


def test_clean_completion_returns_summary_and_cleans_up(cfg: WorkerConfig):
    job = _job()
    _write_child_result(cfg, job, {"ok": True,
                                   "summary": {"matches": 5, "run_id": "run-1",
                                               "job_id": job.id}})
    fake = FakePopen(polls=(None, 0))
    popen = _popen_factory(fake)
    summary = job_runner.run_one_job(object(), job, cfg=cfg, halt=threading.Event(),
                                     popen=popen, sleep=NOOP_SLEEP,
                                     monotonic=NEVER_TIMEOUT, cdp_probe=PROBE_OK)
    assert summary["matches"] == 5
    # argv points at the child module; spec + result files cleaned up in finally.
    assert popen.captured["argv"][1:3] == ["-m", "aizu.worker.job_child"]
    assert not job_runner._spec_path(cfg.state_dir, job.id).exists()
    assert not job_runner._result_path(cfg.state_dir, job.id).exists()


# ----- Fix B: fail-fast CDP pre-check -------------------------------------------

def test_cdp_probe_failure_nacks_fast_without_spawning(cfg: WorkerConfig):
    """A failing CDP probe must NOT spawn the child (no ~180s connect_over_cdp hang) and
    must return the fast, requeue-with-backoff nack via halt_reason='cdp_unreachable'."""
    job = _job()
    popen = _popen_factory(FakePopen(polls=(0,)))
    summary = job_runner.run_one_job(object(), job, cfg=cfg, halt=threading.Event(),
                                     popen=popen, sleep=NOOP_SLEEP,
                                     monotonic=NEVER_TIMEOUT, cdp_probe=PROBE_BAD)
    assert summary["halt_reason"] == job_runner.CDP_UNREACHABLE_REASON
    assert summary["job_id"] == job.id
    assert "argv" not in popen.captured          # popen never called → no child spawned
    # No rendezvous files were written for a job we never launched.
    assert not job_runner._spec_path(cfg.state_dir, job.id).exists()


def test_api_platform_job_skips_the_cdp_preflight(cfg: WorkerConfig):
    """An API-only platform drives no browser, so a Chrome-less box must still run it —
    gating it on the CDP probe nacked it `cdp_unreachable` until it dead-lettered."""
    job = _job(platform="youtube")
    _write_child_result(cfg, job, {"ok": True,
                                   "summary": {"matches": 2, "run_id": "run-1",
                                               "job_id": job.id}})
    popen = _popen_factory(FakePopen(polls=(None, 0)))
    summary = job_runner.run_one_job(object(), job, cfg=cfg, halt=threading.Event(),
                                     popen=popen, sleep=NOOP_SLEEP,
                                     monotonic=NEVER_TIMEOUT, cdp_probe=PROBE_BAD)
    assert "halt_reason" not in summary
    assert summary["matches"] == 2
    assert popen.captured["argv"][1:3] == ["-m", "aizu.worker.job_child"]


# ----- B9 REVIEW FIX: fail-fast spend cap ---------------------------------------

class _SpendStore:
    """Just enough store for the cap math: this box's LOCAL total for the campaign."""

    def __init__(self, total: float = 0.0):
        self._total = total

    def total_spend(self, _campaign_id: str) -> float:
        return self._total


def test_no_spend_headroom_refuses_to_spawn(cfg: WorkerConfig):
    """REVIEW FIX. A REQUEUE never traverses cloud dispatch — `nack_job` puts the row
    straight back to `queued` — so a job that went over cap on attempt 1 is re-leased
    with `priorSpendUsd >= cap`. Before this branch the box computed an effective cap of
    exactly its local total, spawned anyway, and `router._spend_guard` failed on call
    one; `_degrade` does NOT stop a run, so the box held its warmed account for the whole
    duration cap emitting degraded stand-ins — once per remaining attempt."""
    job = _job(prior_spend_usd=cfg.spend_cap + 5.0)
    popen = _popen_factory(FakePopen(polls=(0,)))
    summary = job_runner.run_one_job(_SpendStore(0.0), job, cfg=cfg,
                                     halt=threading.Event(), popen=popen,
                                     sleep=NOOP_SLEEP, monotonic=NEVER_TIMEOUT,
                                     cdp_probe=PROBE_OK)
    assert summary["halt_reason"] == job_runner.SPEND_CAP_REASON
    assert summary["job_id"] == job.id
    assert "argv" not in popen.captured          # never spawned
    assert not job_runner._spec_path(cfg.state_dir, job.id).exists()


def test_no_spend_headroom_is_checked_before_the_cdp_probe(cfg: WorkerConfig):
    # An over-budget job must not even be diagnosed as a Chrome problem: `cdp_unreachable`
    # is a REQUEUE reason, so it would retry to dead-letter instead of stopping now.
    job = _job(prior_spend_usd=cfg.spend_cap)
    summary = job_runner.run_one_job(_SpendStore(0.0), job, cfg=cfg,
                                     halt=threading.Event(),
                                     popen=_popen_factory(FakePopen(polls=(0,))),
                                     sleep=NOOP_SLEEP, monotonic=NEVER_TIMEOUT,
                                     cdp_probe=PROBE_BAD)
    assert summary["halt_reason"] == job_runner.SPEND_CAP_REASON


def test_the_spend_cap_refusal_is_poison_so_it_dead_letters(cfg: WorkerConfig):
    # Spend only ever grows, so retrying can only reach the same verdict — the sidecar
    # must dead-letter it rather than burn the remaining attempts.
    from aizu.worker.sidecar import _is_poison
    assert _is_poison(job_runner.SPEND_CAP_REASON) is True


def test_headroom_left_still_spawns(cfg: WorkerConfig):
    job = _job(prior_spend_usd=cfg.spend_cap - 1.0)
    _write_child_result(cfg, job, {"ok": True,
                                   "summary": {"matches": 1, "run_id": "run-1",
                                               "job_id": job.id}})
    popen = _popen_factory(FakePopen(polls=(None, 0)))
    summary = job_runner.run_one_job(_SpendStore(0.0), job, cfg=cfg,
                                     halt=threading.Event(), popen=popen,
                                     sleep=NOOP_SLEEP, monotonic=NEVER_TIMEOUT,
                                     cdp_probe=PROBE_OK)
    assert summary["matches"] == 1
    assert popen.captured["argv"][1:3] == ["-m", "aizu.worker.job_child"]


def test_local_spend_the_cloud_never_saw_still_blocks(cfg: WorkerConfig):
    # The box holds spend the cloud never learned about (an attempt that died before its
    # ack/nack, re-pinned here by reclaim). `max(prior, local)` must count it.
    job = _job(prior_spend_usd=0.0)
    popen = _popen_factory(FakePopen(polls=(0,)))
    summary = job_runner.run_one_job(_SpendStore(cfg.spend_cap + 1.0), job, cfg=cfg,
                                     halt=threading.Event(), popen=popen,
                                     sleep=NOOP_SLEEP, monotonic=NEVER_TIMEOUT,
                                     cdp_probe=PROBE_OK)
    assert summary["halt_reason"] == job_runner.SPEND_CAP_REASON
    assert "argv" not in popen.captured


def test_an_uncapped_box_never_refuses_on_spend(cfg: WorkerConfig):
    import dataclasses
    cfg = dataclasses.replace(cfg, spend_cap=None)
    job = _job(prior_spend_usd=9999.0)
    _write_child_result(cfg, job, {"ok": True, "summary": {"matches": 0,
                                                           "job_id": job.id}})
    summary = job_runner.run_one_job(_SpendStore(9999.0), job, cfg=cfg,
                                     halt=threading.Event(),
                                     popen=_popen_factory(FakePopen(polls=(None, 0))),
                                     sleep=NOOP_SLEEP, monotonic=NEVER_TIMEOUT,
                                     cdp_probe=PROBE_OK)
    assert "halt_reason" not in summary


def test_cdp_probe_success_proceeds_to_spawn(cfg: WorkerConfig):
    """A passing probe leaves the existing happy path untouched — the child is spawned
    and its result returned."""
    job = _job()
    _write_child_result(cfg, job, {"ok": True,
                                   "summary": {"matches": 3, "run_id": "run-1",
                                               "job_id": job.id}})
    popen = _popen_factory(FakePopen(polls=(None, 0)))
    summary = job_runner.run_one_job(object(), job, cfg=cfg, halt=threading.Event(),
                                     popen=popen, sleep=NOOP_SLEEP,
                                     monotonic=NEVER_TIMEOUT, cdp_probe=PROBE_OK)
    assert summary["matches"] == 3
    assert popen.captured["argv"][1:3] == ["-m", "aizu.worker.job_child"]


def test_halt_hard_stops_child_mid_run(cfg: WorkerConfig):
    job = _job()
    halt = threading.Event()
    halt.set()  # operator halt already signalled
    fake = FakePopen(polls=(None,), terminate_exits=True)  # never exits on its own
    summary = job_runner.run_one_job(object(), job, cfg=cfg, halt=halt,
                                     popen=_popen_factory(fake), sleep=NOOP_SLEEP,
                                     monotonic=NEVER_TIMEOUT, cdp_probe=PROBE_OK)
    assert fake.terminated is True                       # SIGTERM sent mid-run
    assert summary["halt_reason"] == "operator_stop"


def test_halt_escalates_to_sigkill_when_sigterm_ignored(cfg: WorkerConfig):
    job = _job()
    halt = threading.Event()
    halt.set()
    fake = FakePopen(polls=(None,), terminate_exits=False)  # ignores SIGTERM
    job_runner.run_one_job(object(), job, cfg=cfg, halt=halt,
                           popen=_popen_factory(fake), sleep=NOOP_SLEEP,
                           monotonic=NEVER_TIMEOUT, cdp_probe=PROBE_OK)
    assert fake.terminated is True and fake.killed is True   # escalated to SIGKILL


def test_timeout_self_terminates_with_worker_timeout(cfg: WorkerConfig):
    job = _job(duration_minutes=1)
    times = iter([0.0] + [10_000.0] * 50)   # deadline calc then jump past it
    fake = FakePopen(polls=(None,), terminate_exits=True)
    summary = job_runner.run_one_job(object(), job, cfg=cfg, halt=threading.Event(),
                                     popen=_popen_factory(fake), sleep=NOOP_SLEEP,
                                     monotonic=lambda: next(times), cdp_probe=PROBE_OK)
    assert fake.terminated is True
    assert summary["halt_reason"] == "worker_timeout"


# ----- no-progress (stall) watchdog --------------------------------------------

def test_stall_self_terminates_with_worker_stall(cfg: WorkerConfig):
    """A child that writes NOTHING to its job log for stall_timeout_sec is killed with
    halt_reason='worker_stall' (the sidecar nacks → requeues). This is the preemptive
    backstop for a wedged sync-Playwright call the engine's own deadline can't break."""
    import dataclasses
    cfg = dataclasses.replace(cfg, stall_timeout_sec=5.0)
    job = _job()
    # deadline calc @0, last_progress init @0, then jump to 10s (< duration cap, > stall).
    times = iter([0.0, 0.0] + [10.0] * 50)
    fake = FakePopen(polls=(None,), terminate_exits=True)  # never exits on its own
    summary = job_runner.run_one_job(object(), job, cfg=cfg, halt=threading.Event(),
                                     popen=_popen_factory(fake), sleep=NOOP_SLEEP,
                                     monotonic=lambda: next(times), cdp_probe=PROBE_OK)
    assert fake.terminated is True
    assert summary["halt_reason"] == "worker_stall"


def test_no_stall_while_child_makes_progress(cfg: WorkerConfig):
    """When the job log keeps growing, the stall timer resets every tick and never fires —
    the child runs to its own clean exit even though wall-clock passes the stall threshold."""
    import dataclasses
    cfg = dataclasses.replace(cfg, stall_timeout_sec=5.0)
    job = _job()
    _write_child_result(cfg, job, {"ok": True,
                                   "summary": {"matches": 1, "run_id": "run-1",
                                               "job_id": job.id}})
    log_path = job_runner._job_log_path(cfg.state_dir, job.id)

    class GrowingLogPopen(FakePopen):
        def __init__(self):
            super().__init__(polls=(None, None, None, 0))

        def poll(self):
            # Simulate the child appending output each supervisor tick (progress).
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "ab") as fh:
                fh.write(b"progress line\n")
            return super().poll()

    fake = GrowingLogPopen()
    # Wall-clock advances 3s per call — well past stall=5 in aggregate, but growth resets it.
    t = iter([0.0, 0.0] + [3.0 * i for i in range(1, 60)])
    summary = job_runner.run_one_job(object(), job, cfg=cfg, halt=threading.Event(),
                                     popen=_popen_factory(fake), sleep=NOOP_SLEEP,
                                     monotonic=lambda: next(t), cdp_probe=PROBE_OK)
    assert fake.terminated is False           # never force-killed
    assert summary["matches"] == 1            # clean completion


def test_stall_watchdog_disabled_when_threshold_nonpositive(cfg: WorkerConfig):
    """stall_timeout_sec <= 0 disables the watchdog: a silent, never-exiting child is
    bounded ONLY by the duration cap, not stalled out."""
    import dataclasses
    cfg = dataclasses.replace(cfg, stall_timeout_sec=0.0)
    job = _job(duration_minutes=1)
    # No log growth; jump past stall-would-be but the duration cap (1min+grace) catches it.
    times = iter([0.0, 0.0] + [10_000.0] * 50)
    fake = FakePopen(polls=(None,), terminate_exits=True)
    summary = job_runner.run_one_job(object(), job, cfg=cfg, halt=threading.Event(),
                                     popen=_popen_factory(fake), sleep=NOOP_SLEEP,
                                     monotonic=lambda: next(times), cdp_probe=PROBE_OK)
    assert summary["halt_reason"] == "worker_timeout"   # duration cap, NOT stall


def test_campaign_not_found_result_reraises(cfg: WorkerConfig):
    job = _job()
    _write_child_result(cfg, job, {"ok": False, "kind": "campaign_not_found",
                                   "error": "x"})
    with pytest.raises(CampaignNotFound):
        job_runner.run_one_job(object(), job, cfg=cfg, halt=threading.Event(),
                               popen=_popen_factory(FakePopen(polls=(0,))),
                               sleep=NOOP_SLEEP, monotonic=NEVER_TIMEOUT, cdp_probe=PROBE_OK)


def test_soul_missing_result_reraises(cfg: WorkerConfig):
    job = _job()
    _write_child_result(cfg, job, {"ok": False, "kind": "soul_missing", "error": "x"})
    with pytest.raises(SoulMissing):
        job_runner.run_one_job(object(), job, cfg=cfg, halt=threading.Event(),
                               popen=_popen_factory(FakePopen(polls=(0,))),
                               sleep=NOOP_SLEEP, monotonic=NEVER_TIMEOUT, cdp_probe=PROBE_OK)


def test_malformed_result_reraises_valueerror(cfg: WorkerConfig):
    job = _job()
    _write_child_result(cfg, job, {"ok": False, "kind": "campaign_malformed",
                                   "error": "x"})
    with pytest.raises(ValueError):
        job_runner.run_one_job(object(), job, cfg=cfg, halt=threading.Event(),
                               popen=_popen_factory(FakePopen(polls=(0,))),
                               sleep=NOOP_SLEEP, monotonic=NEVER_TIMEOUT, cdp_probe=PROBE_OK)


def test_missing_result_file_is_a_crash(cfg: WorkerConfig):
    job = _job()
    # No result file written → child died without recording → ChildCrashed.
    fake = FakePopen(polls=(1,))  # non-zero exit
    with pytest.raises(ChildCrashed):
        job_runner.run_one_job(object(), job, cfg=cfg, halt=threading.Event(),
                               popen=_popen_factory(fake), sleep=NOOP_SLEEP,
                               monotonic=NEVER_TIMEOUT, cdp_probe=PROBE_OK)


def test_spec_file_written_with_run_id_forced(cfg: WorkerConfig):
    job = _job(run_id=None)   # legacy job with no cloud run_id
    _write_child_result(cfg, job, {"ok": True, "summary": {"job_id": job.id}})
    captured = {}

    def popen(argv, **kw):
        # Inspect the spec the parent wrote BEFORE the (fake) child would read it.
        spec = json.loads(job_runner._spec_path(cfg.state_dir, job.id).read_text())
        captured["spec"] = spec
        return FakePopen(polls=(0,))

    job_runner.run_one_job(object(), job, cfg=cfg, halt=threading.Event(),
                           popen=popen, sleep=NOOP_SLEEP, monotonic=NEVER_TIMEOUT, cdp_probe=PROBE_OK)
    # The supervisor generated a run_id and forced it into the child's spec.
    assert captured["spec"]["job"]["runId"]
    assert captured["spec"]["cfg"]["db_path"] == cfg.db_path


# ----- review fixes: guaranteed child termination + 0600 spec -----------------

def test_child_terminated_when_supervise_raises(cfg: WorkerConfig):
    """CRITICAL fix: an exception escaping _supervise (e.g. poll() raising) must still
    leave NO live child — run_one_job's finally terminates it."""
    job = _job()

    class ExplodingPopen(FakePopen):
        def poll(self):
            raise OSError("simulated reap-race")

    fake = ExplodingPopen(polls=(None,))
    with pytest.raises(OSError):
        job_runner.run_one_job(object(), job, cfg=cfg, halt=threading.Event(),
                               popen=_popen_factory(fake), sleep=NOOP_SLEEP,
                               monotonic=NEVER_TIMEOUT, cdp_probe=PROBE_OK)
    assert fake.terminated is True  # killed on the way out despite the exception


@pytest.mark.skipif(os.name != "posix",
                    reason="0600 mode bits are POSIX-only; Windows secures files via ACLs")
def test_spec_file_is_mode_0600(cfg: WorkerConfig):
    import stat
    job = _job()
    _write_child_result(cfg, job, {"ok": True, "summary": {"job_id": job.id}})
    seen = {}

    def popen(argv, **kw):
        p = job_runner._spec_path(cfg.state_dir, job.id)
        seen["mode"] = stat.S_IMODE(p.stat().st_mode)
        return FakePopen(polls=(0,))

    job_runner.run_one_job(object(), job, cfg=cfg, halt=threading.Event(),
                           popen=popen, sleep=NOOP_SLEEP, monotonic=NEVER_TIMEOUT, cdp_probe=PROBE_OK)
    assert seen["mode"] == 0o600


def test_reap_orphan_children_kills_live_and_unlinks(cfg: WorkerConfig):
    import subprocess as sp
    import sys as _sys
    from aizu.worker import job_runner as jr
    # A real, harmless child we own; record its pid as if it were an orphan.
    proc = sp.Popen([_sys.executable, "-c", "import time; time.sleep(30)"])
    pid_path = jr._pid_path(cfg.state_dir, "job-orphan")
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(proc.pid), encoding="utf-8")
    try:
        killed = jr.reap_orphan_children(cfg.state_dir)
        assert killed == 1
        assert not pid_path.exists()
        assert proc.wait(timeout=5) is not None  # actually died
    finally:
        if proc.poll() is None:
            proc.kill()


def test_reap_orphan_children_ignores_dead_pid(cfg: WorkerConfig):
    from aizu.worker import job_runner as jr
    pid_path = jr._pid_path(cfg.state_dir, "job-dead")
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("999999", encoding="utf-8")   # almost certainly not alive
    assert jr.reap_orphan_children(cfg.state_dir) == 0
    assert not pid_path.exists()                        # stale pidfile cleaned up


def test_a_child_logging_only_to_the_run_log_is_not_mistaken_for_a_stall(cfg: WorkerConfig):
    """The 2026-08-20 fleet dead-letters. The child's stdout/stderr go to the job log, but
    the engine logs through core.logsetup, which writes `aizu.log` and the per-run
    `run-<run_id>.log` — never stdout. So the job log took a one-time Playwright/Node
    preamble and then never grew: 1491 bytes, byte-identical across two different jobs.
    Liveness was read off that file alone, so EVERY job looked wedged after exactly
    stall_timeout_sec however healthy it was — a hard ceiling that dead-lettered
    job-19eaf089da2e at 5/5 attempts *while it was actively producing leads*.

    Here the job log is written once and then frozen, exactly as in production, while the
    run log keeps growing. That must read as progress, not as a stall."""
    import dataclasses
    cfg = dataclasses.replace(cfg, stall_timeout_sec=5.0)
    job = _job()
    _write_child_result(cfg, job, {"ok": True,
                                   "summary": {"matches": 2, "run_id": "run-1",
                                               "job_id": job.id}})
    job_log = job_runner._job_log_path(cfg.state_dir, job.id)
    job_log.parent.mkdir(parents=True, exist_ok=True)
    job_log.write_bytes(b"node:events:487\n  throw er;  // one-time driver preamble\n")
    run_log = job_log.parent / "run-run-1.log"

    class RunLogOnlyPopen(FakePopen):
        def __init__(self):
            super().__init__(polls=(None, None, None, 0))

        def poll(self):
            # The engine appends to the RUN log only; the job log stays frozen.
            with open(run_log, "ab") as fh:
                fh.write(b"CDP walking source\n")
            return super().poll()

    fake = RunLogOnlyPopen()
    t = iter([0.0, 0.0] + [3.0 * i for i in range(1, 60)])
    summary = job_runner.run_one_job(object(), job, cfg=cfg, halt=threading.Event(),
                                     popen=_popen_factory(fake), sleep=NOOP_SLEEP,
                                     monotonic=lambda: next(t), cdp_probe=PROBE_OK)
    assert fake.terminated is False, "a healthy run was force-killed as a stall"
    assert summary.get("halt_reason") != "worker_stall"
    assert summary["matches"] == 2


def test_a_truly_silent_child_still_stalls_with_every_log_watched(cfg: WorkerConfig):
    """The widened signal must not blind the watchdog: when NOTHING grows — job log, run
    log or aizu.log — a wedged child is still killed. This is the half that keeps the
    preemptive backstop honest."""
    import dataclasses
    cfg = dataclasses.replace(cfg, stall_timeout_sec=5.0)
    job = _job()
    times = iter([0.0, 0.0] + [10.0] * 50)
    fake = FakePopen(polls=(None,), terminate_exits=True)
    summary = job_runner.run_one_job(object(), job, cfg=cfg, halt=threading.Event(),
                                     popen=_popen_factory(fake), sleep=NOOP_SLEEP,
                                     monotonic=lambda: next(times), cdp_probe=PROBE_OK)
    assert fake.terminated is True
    assert summary["halt_reason"] == "worker_stall"
