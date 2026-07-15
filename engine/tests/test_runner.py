"""Tests for runner.RunManager and build_argv — the single-run lock, the monitor
that records exits, and the pure argv mapping. No real subprocess is ever spawned;
a FakeSpawner returns a controllable FakeProc."""
import threading
import time

import pytest

from reelradar.runner import (RunManager, RunSpec, build_argv, _error_detail,
                              _summarize)


# ---- build_argv (pure) ----

def _idx(argv, token):
    return argv.index(token)


def test_build_argv_single_dry():
    argv = build_argv(RunSpec("campaign", "c1", "dry"), "py", "/db", "/cfg")
    assert argv == ["py", "-m", "reelradar.cli", "--db", "/db",
                    "run", "--config", "/cfg", "--campaign", "c1", "--dry-run"]


def test_build_argv_single_live_omits_dry_run():
    argv = build_argv(RunSpec("campaign", "c1", "live"), "py", "/db", "/cfg")
    assert "--dry-run" not in argv
    assert argv[-2:] == ["--campaign", "c1"]


def test_build_argv_all_dry_and_live():
    dry = build_argv(RunSpec("all", None, "dry"), "py", "/db", "/cfg")
    assert "run-all" in dry and dry[-1] == "--dry-run"
    live = build_argv(RunSpec("all", None, "live"), "py", "/db", "/cfg")
    assert "run-all" in live and "--dry-run" not in live


def test_build_argv_db_precedes_subcommand():
    for spec in (RunSpec("campaign", "c1", "dry"), RunSpec("all", None, "live")):
        argv = build_argv(spec, "py", "/db", "/cfg")
        sub = _idx(argv, "run") if "run" in argv else _idx(argv, "run-all")
        assert _idx(argv, "--db") < sub


def test_build_argv_includes_duration_for_campaign():
    argv = build_argv(RunSpec("campaign", "c1", "live", duration_minutes=120),
                      "py", "/db", "/cfg")
    i = _idx(argv, "--duration-minutes")
    assert argv[i + 1] == "120"


def test_build_argv_omits_duration_when_unset():
    argv = build_argv(RunSpec("campaign", "c1", "live"), "py", "/db", "/cfg")
    assert "--duration-minutes" not in argv


def test_build_argv_includes_target_leads_for_campaign():
    argv = build_argv(RunSpec("campaign", "c1", "live", target_leads=50),
                      "py", "/db", "/cfg")
    i = _idx(argv, "--target-leads")
    assert argv[i + 1] == "50"


def test_build_argv_omits_target_leads_when_unset():
    argv = build_argv(RunSpec("campaign", "c1", "live"), "py", "/db", "/cfg")
    assert "--target-leads" not in argv


def test_build_argv_target_leads_and_duration_together():
    argv = build_argv(RunSpec("campaign", "c1", "live", target_leads=25, duration_minutes=120),
                      "py", "/db", "/cfg")
    assert argv[_idx(argv, "--target-leads") + 1] == "25"
    assert argv[_idx(argv, "--duration-minutes") + 1] == "120"


# ---- FakeSpawner / FakeProc ----

class FakeProc:
    def __init__(self, returncode: int, gate: threading.Event | None):
        self.pid = 4242
        self.returncode = None
        self.terminated = False
        self._rc = returncode
        self._gate = gate

    def wait(self) -> int:
        if self._gate is not None:
            self._gate.wait(timeout=5)
        self.returncode = self._rc
        return self._rc

    def terminate(self) -> None:
        # Model a real terminate: unblock wait() so the monitor records the result.
        self.terminated = True
        self._rc = -15
        if self._gate is not None:
            self._gate.set()


class FakeSpawner:
    def __init__(self):
        self.calls = []
        self.next_returncode = 0
        self.next_gate: threading.Event | None = None

    def __call__(self, argv, cwd, env, log_path):
        self.calls.append({"argv": argv, "cwd": cwd, "env": env, "log_path": log_path})
        return FakeProc(self.next_returncode, self.next_gate)


def _manager(spawner, tmp_path):
    return RunManager(db_path="/db", config_dir="/cfg", engine_root=tmp_path,
                      log_dir=tmp_path / "logs", spawner=spawner, python_exe="py")


def _wait_idle(mgr, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if mgr.status()["active"] is None:
            return
        time.sleep(0.02)
    raise AssertionError("run never went idle")


def test_lock_rejects_second_launch(tmp_path):
    spawner = FakeSpawner()
    gate = threading.Event()
    spawner.next_gate = gate
    mgr = _manager(spawner, tmp_path)

    active, err = mgr.launch(RunSpec("all", None, "dry"))
    assert active is not None and err is None
    assert mgr.status()["active"]["scope"] == "all"

    # Second launch while the first proc is still blocked → rejected.
    active2, err2 = mgr.launch(RunSpec("campaign", "c1", "dry"))
    assert active2 is None
    assert err2 == "a run is already active"

    gate.set()                  # release the first proc
    _wait_idle(mgr)
    recent = mgr.status()["recent"]
    assert recent and recent[0]["outcome"] == "ok"


def test_monitor_records_nonzero_exit_as_error(tmp_path):
    spawner = FakeSpawner()
    spawner.next_returncode = 2
    mgr = _manager(spawner, tmp_path)
    mgr.launch(RunSpec("campaign", "c1", "live"))
    _wait_idle(mgr)
    rec = mgr.status()["recent"][0]
    assert rec["outcome"] == "error"
    assert rec["mode"] == "live" and rec["campaignId"] == "c1"


def test_status_is_org_scoped(tmp_path):
    """v7: status(org_id) must not disclose another org's runs (RUN block leak)."""
    spawner = FakeSpawner()
    mgr = _manager(spawner, tmp_path)
    mgr.launch(RunSpec("campaign", "ca", "dry", org_id=1))
    _wait_idle(mgr)
    mgr.launch(RunSpec("campaign", "cb", "dry", org_id=2))
    _wait_idle(mgr)
    assert [r["campaignId"] for r in mgr.status(org_id=1)["recent"]] == ["ca"]
    assert [r["campaignId"] for r in mgr.status(org_id=2)["recent"]] == ["cb"]
    # Unfiltered (CLI/internal) still sees the whole history.
    assert {r["campaignId"] for r in mgr.status()["recent"]} == {"ca", "cb"}


def test_status_active_is_org_scoped(tmp_path):
    spawner = FakeSpawner()
    gate = threading.Event()
    spawner.next_gate = gate
    mgr = _manager(spawner, tmp_path)
    mgr.launch(RunSpec("campaign", "ca", "dry", org_id=1))
    assert mgr.status(org_id=1)["active"]["campaignId"] == "ca"
    assert mgr.status(org_id=2)["active"] is None  # org 2 sees nothing
    gate.set()
    _wait_idle(mgr)


def test_stop_campaign_stops_only_matching_campaign(tmp_path):
    """v12: archive-while-live stops the run only when it is a campaign-scoped run
    for that exact campaign."""
    spawner = FakeSpawner()
    gate = threading.Event()
    spawner.next_gate = gate
    mgr = _manager(spawner, tmp_path)
    mgr.launch(RunSpec("campaign", "ca", "live", org_id=1))
    # A different campaign's archive must NOT stop this run.
    ok, _ = mgr.stop_campaign("cb", org_id=1)
    assert ok is False
    assert mgr.status(org_id=1)["active"]["campaignId"] == "ca"
    # The matching campaign's archive stops it.
    ok, err = mgr.stop_campaign("ca", org_id=1)
    assert ok is True and err is None
    _wait_idle(mgr)
    assert mgr.status(org_id=1)["recent"][0]["outcome"] == "aborted"


def test_stop_campaign_org_scoped_and_skips_batch(tmp_path):
    spawner = FakeSpawner()
    gate = threading.Event()
    spawner.next_gate = gate
    mgr = _manager(spawner, tmp_path)
    # A foreign-org campaign run is invisible.
    mgr.launch(RunSpec("campaign", "ca", "live", org_id=1))
    assert mgr.stop_campaign("ca", org_id=2)[0] is False
    gate.set()
    _wait_idle(mgr)
    # An 'all' batch run is never stopped by archiving one campaign.
    gate2 = threading.Event()
    spawner.next_gate = gate2
    mgr.launch(RunSpec("all", None, "live", org_id=1))
    assert mgr.stop_campaign("ca", org_id=1)[0] is False
    gate2.set()
    _wait_idle(mgr)


def test_stop_campaign_when_idle(tmp_path):
    mgr = _manager(FakeSpawner(), tmp_path)
    assert mgr.stop_campaign("ca", org_id=1) == (False, "no run is active")


# ---- v12 pause / resume ----

def test_pause_resume_round_trip_on_one_path(tmp_path):
    """pause() and resume() act on the SAME path the child env exposes — keyed on
    run_id only (asserts no -<scope> suffix drift between producer and consumer)."""
    spawner = FakeSpawner()
    gate = threading.Event()
    spawner.next_gate = gate
    mgr = _manager(spawner, tmp_path)
    active, _ = mgr.launch(RunSpec("campaign", "ca", "live", org_id=1))

    # The env the child got points at exactly the file pause() creates.
    pause_env = spawner.calls[0]["env"]["REELRADAR_PAUSE_FILE"]
    assert mgr.pause(org_id=1) == (True, None)
    assert pause_env == str(mgr._pause_path(active.run_id))
    assert mgr.status(org_id=1)["active"]["paused"] is True

    assert mgr.resume(org_id=1) == (True, None)
    assert mgr.status(org_id=1)["active"]["paused"] is False
    gate.set()
    _wait_idle(mgr)


def test_pause_is_org_scoped(tmp_path):
    spawner = FakeSpawner()
    gate = threading.Event()
    spawner.next_gate = gate
    mgr = _manager(spawner, tmp_path)
    mgr.launch(RunSpec("campaign", "ca", "live", org_id=1))
    assert mgr.pause(org_id=2) == (False, "no run is active")   # foreign org
    assert mgr.status(org_id=1)["active"]["paused"] is False
    gate.set()
    _wait_idle(mgr)


def test_pause_resume_idempotent_and_409_when_idle(tmp_path):
    spawner = FakeSpawner()
    gate = threading.Event()
    spawner.next_gate = gate
    mgr = _manager(spawner, tmp_path)
    mgr.launch(RunSpec("campaign", "ca", "live", org_id=1))
    assert mgr.pause(org_id=1) == (True, None)
    assert mgr.pause(org_id=1) == (True, None)      # re-pause is a no-op success
    assert mgr.resume(org_id=1) == (True, None)
    assert mgr.resume(org_id=1) == (True, None)      # re-resume too
    gate.set()
    _wait_idle(mgr)
    # Nothing active → 409 signal for both.
    assert mgr.pause(org_id=1) == (False, "no run is active")
    assert mgr.resume(org_id=1) == (False, "no run is active")


def test_monitor_deletes_pause_file_on_exit(tmp_path):
    """A run that exits while paused must not leave an orphaned sentinel (else the UI
    sticks on 'Paused'). The monitor deletes it on child exit."""
    spawner = FakeSpawner()
    gate = threading.Event()
    spawner.next_gate = gate
    mgr = _manager(spawner, tmp_path)
    active, _ = mgr.launch(RunSpec("campaign", "ca", "live", org_id=1))
    mgr.pause(org_id=1)
    pause_path = mgr._pause_path(active.run_id)
    assert pause_path.exists()
    gate.set()                       # child exits while still paused
    _wait_idle(mgr)
    assert not pause_path.exists()   # reconciled


def test_sweep_orphan_pause_files(tmp_path):
    mgr = _manager(FakeSpawner(), tmp_path)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    orphan = tmp_path / "logs" / "run-deadbeef.pause"
    orphan.touch()
    mgr.sweep_orphan_pause_files()
    assert not orphan.exists()


def test_launch_passes_env_and_argv(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sentinel-key")
    spawner = FakeSpawner()
    mgr = _manager(spawner, tmp_path)
    mgr.launch(RunSpec("campaign", "c1", "dry"))
    _wait_idle(mgr)
    call = spawner.calls[0]
    assert call["env"].get("OPENROUTER_API_KEY") == "sentinel-key"
    assert "--campaign" in call["argv"] and "c1" in call["argv"]


def test_launch_injects_run_id_and_org_into_env(tmp_path):
    """v10: the engine subprocess gets REELRADAR_RUN_ID (and the launching org) so it
    can correlate its run-activity events back to this run. run_id matches status()."""
    spawner = FakeSpawner()
    mgr = _manager(spawner, tmp_path)
    active, _ = mgr.launch(RunSpec("campaign", "c1", "dry", org_id=7))
    _wait_idle(mgr)
    env = spawner.calls[0]["env"]
    assert env["REELRADAR_RUN_ID"] == active.run_id
    assert env["REELRADAR_ORG_ID"] == "7"


def test_launch_omits_org_env_when_unset(tmp_path):
    spawner = FakeSpawner()
    mgr = _manager(spawner, tmp_path)
    mgr.launch(RunSpec("all", None, "dry"))   # no org_id
    _wait_idle(mgr)
    env = spawner.calls[0]["env"]
    assert "REELRADAR_RUN_ID" in env
    assert "REELRADAR_ORG_ID" not in env


def test_status_active_includes_run_id(tmp_path):
    spawner = FakeSpawner()
    gate = threading.Event()
    spawner.next_gate = gate
    mgr = _manager(spawner, tmp_path)
    active, _ = mgr.launch(RunSpec("campaign", "c1", "dry", org_id=1))
    assert mgr.status(org_id=1)["active"]["id"] == active.run_id
    gate.set()
    _wait_idle(mgr)


# ---- stop() ----

def test_stop_terminates_and_records_aborted(tmp_path):
    spawner = FakeSpawner()
    gate = threading.Event()          # keep the run "in flight" until stop terminates it
    spawner.next_gate = gate
    mgr = _manager(spawner, tmp_path)
    mgr.launch(RunSpec("campaign", "c1", "live", org_id=1))

    stopped, err = mgr.stop(org_id=1)
    assert stopped is True and err is None

    _wait_idle(mgr)
    rec = mgr.status(org_id=1)["recent"][0]
    assert rec["outcome"] == "aborted"
    assert rec["summary"] == "stopped by operator"


def test_stop_when_idle_returns_false(tmp_path):
    mgr = _manager(FakeSpawner(), tmp_path)
    stopped, err = mgr.stop()
    assert stopped is False
    assert err == "no run is active"


def test_stop_is_org_scoped(tmp_path):
    """A tenant can't stop (or learn about) another org's run."""
    spawner = FakeSpawner()
    gate = threading.Event()
    spawner.next_gate = gate
    mgr = _manager(spawner, tmp_path)
    mgr.launch(RunSpec("campaign", "c1", "live", org_id=1))

    stopped, err = mgr.stop(org_id=2)       # different org
    assert stopped is False and err == "no run is active"

    gate.set()                              # let the real run finish cleanly
    _wait_idle(mgr)
    assert mgr.status(org_id=1)["recent"][0]["outcome"] == "ok"


# ---- _summarize (failure surfacing) ----

# A real spawned-run crash: stdout/stderr is a Python traceback (no JSON, and the
# exception line starts with the module path, not "HALTED:"/"error:") — the old
# code fell through to a useless "exit 1". This is the exact CDP failure operators hit.
_CDP_TRACEBACK = """\
Traceback (most recent call last):
  File "<frozen runpy>", line 198, in _run_module_as_main
  File "/engine/reelradar/cli.py", line 106, in _build_run_io
    feed = build_feed(campaign.platform, cdp_url=args.cdp_url)
  File "/engine/reelradar/cdp.py", line 113, in attach
    self._browser = self._pw.chromium.connect_over_cdp(self.cfg.cdp_url)
playwright._impl._errors.Error: BrowserType.connect_over_cdp: connect ECONNREFUSED 127.0.0.1:9333
Call log:
  - <ws preparing> retrieving websocket url from http://127.0.0.1:9333
"""


def test_summarize_surfaces_exception_from_crash_traceback(tmp_path):
    """A crashed run must report WHY, not a bare 'exit 1'."""
    log_path = tmp_path / "run-x-campaign.log"
    log_path.write_text(_CDP_TRACEBACK, encoding="utf-8")

    summary = _summarize(1, log_path)

    assert summary == (
        "playwright._impl._errors.Error: BrowserType.connect_over_cdp: "
        "connect ECONNREFUSED 127.0.0.1:9333"
    )


def test_summarize_prefers_json_summary_over_traceback(tmp_path):
    """A clean single-session JSON summary still wins (unchanged behaviour)."""
    log_path = tmp_path / "run-x-campaign.log"
    log_path.write_text('{"matches": 3, "spend_usd": 0.0125}', encoding="utf-8")

    assert _summarize(0, log_path) == "matches 3, spend $0.0125"


def test_summarize_prefers_halted_line(tmp_path):
    """An explicit HALTED line is surfaced verbatim (unchanged behaviour)."""
    log_path = tmp_path / "run-x-campaign.log"
    log_path.write_text("HALTED: outside daytime window\n", encoding="utf-8")

    assert _summarize(1, log_path) == "HALTED: outside daytime window"


def test_summarize_falls_back_to_exit_code_without_traceback(tmp_path):
    """No JSON, no HALTED, no traceback → the exit code is the only signal."""
    log_path = tmp_path / "run-x-campaign.log"
    log_path.write_text("some unstructured noise\nmore noise\n", encoding="utf-8")

    assert _summarize(1, log_path) == "exit 1"


def test_summarize_surfaces_exception_when_header_scrolls_out_of_window(tmp_path):
    """Regression for the live failure: a crash log larger than the scan window
    put the 'Traceback' header above it, so the header-anchored scan found nothing
    and degraded a real CDP error to 'exit 1'. The exception-line fallback must
    still surface WHY the run died."""
    log_path = tmp_path / "run-x-campaign.log"
    # > 64 KiB of indented frame lines between the header (top) and the exception
    # (bottom), so the tail window cannot contain the header.
    filler = '  File "x.py", line 1, in f\n    do_thing()\n' * 4000
    exc = "playwright._impl._errors.Error: BrowserType.connect_over_cdp: boom"
    log_path.write_text("Traceback (most recent call last):\n" + filler + exc + "\n",
                        encoding="utf-8")

    assert _summarize(1, log_path) == exc


def test_error_detail_returns_only_the_traceback_block(tmp_path):
    """The console dump on failure carries the stack trace from its header down,
    not the unrelated output that preceded it."""
    log_path = tmp_path / "run-x-campaign.log"
    log_path.write_text("earlier unrelated output\n" + _CDP_TRACEBACK, encoding="utf-8")

    detail = _error_detail(log_path)

    assert detail.startswith("Traceback (most recent call last):")
    assert "connect ECONNREFUSED 127.0.0.1:9333" in detail
    assert "earlier unrelated output" not in detail
