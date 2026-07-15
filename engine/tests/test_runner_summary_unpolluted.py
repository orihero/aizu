"""Regression guard for the logging landmine.

A panel-triggered run is a subprocess whose stdout+stderr are redirected into one
logfile that ``runner._extract_json`` / ``_summarize`` parse for the run's JSON
summary. If the new colorful logging leaked onto that stream, the summary would
silently degrade to ``exit 0``. This spawns a real dry run in spawned mode
(``REELRADAR_RUN_ID`` set) exactly like RunManager does, and asserts:

  1. the merged stdout/stderr still yields the summary (matches=3), and
  2. the rich activity went to the per-run app logfile instead — proving the
     console handler was correctly suppressed in spawned mode.
"""
import os
import subprocess
import sys
from pathlib import Path

from reelradar.runner import _extract_json, _summarize

ENGINE_ROOT = Path(__file__).resolve().parent.parent


def test_spawned_run_logs_do_not_pollute_parsed_summary(tmp_path):
    db = tmp_path / "rr.db"
    logdir = tmp_path / "logs"
    run_id = "testrun01"

    env = dict(os.environ)
    env["REELRADAR_RUN_ID"] = run_id                 # → spawned mode (file-only logs)
    env["REELRADAR_LOG_FILE"] = str(logdir / "reelradar.log")
    env["REELRADAR_LOG_LEVEL"] = "DEBUG"

    # Mimic default_spawner: capture stdout AND stderr into one per-run logfile.
    merged = tmp_path / "merged.log"
    with open(merged, "ab") as fh:
        proc = subprocess.run(
            [sys.executable, "-m", "reelradar.cli", "--db", str(db),
             "run", "--dry-run", "--config", str(ENGINE_ROOT / "config")],
            cwd=str(ENGINE_ROOT), env=env, stdout=fh, stderr=subprocess.STDOUT)

    assert proc.returncode == 0

    # 1. The stream the panel parses must still surface the summary.
    obj = _extract_json(merged.read_text())
    assert obj is not None, "summary JSON not recoverable from the run stream"
    assert obj.get("matches") == 3
    assert _summarize(0, merged).startswith("matches 3")

    # 2. The narrative logs went to the per-run app logfile, not the parsed stream.
    app_log = logdir / f"run-{run_id}.log"
    assert app_log.exists(), "per-run app logfile was not created in spawned mode"
    body = app_log.read_text()
    assert "Run completed" in body
    # And those rich lines must NOT have leaked onto the parsed stream.
    assert "Run completed" not in merged.read_text()
