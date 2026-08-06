#!/usr/bin/env python3
"""Dev runner for the panel bridge with auto-restart on Python edits.

The bridge (aizu.server) is a plain stdlib ThreadingHTTPServer with no
reloader: a running process holds the code it was launched with, so editing
aizu/*.py has *no effect* until the process restarts. This wrapper watches
the package for .py changes and restarts the `panel` subprocess automatically —
the same instant-feedback loop the Vite frontend already gets from HMR.

  python scripts/dev_panel.py                 # db=aizu.db, port=8765
  python scripts/dev_panel.py --port 8770
  .venv/bin/python scripts/dev_panel.py --db other.db

Stdlib only — no watchdog/entr install needed. Frontend HMR is independent; keep
`npm run dev` running under admin-panel as usual (it proxies /api here). A syntax
error in an edit makes the child exit; the runner waits for the next save and
retries, so a bad save never tears the loop down.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

POLL_INTERVAL_SECONDS = 1.0
GRACEFUL_SHUTDOWN_SECONDS = 5.0

ENGINE_ROOT = Path(__file__).resolve().parent.parent
WATCH_DIR = ENGINE_ROOT / "aizu"


def _snapshot() -> dict[Path, float]:
    """Map every watched .py file to its mtime; files that vanish mid-scan
    (editors swap via a temp file) simply drop out and reappear next poll."""
    snapshot: dict[Path, float] = {}
    for path in WATCH_DIR.rglob("*.py"):
        try:
            snapshot[path] = path.stat().st_mtime
        except OSError:
            continue
    return snapshot


def _build_command(args: argparse.Namespace) -> list[str]:
    # Mirror the documented launch line; sys.executable keeps us in the same
    # interpreter the runner was started with (venv or system).
    return [
        sys.executable, "-m", "aizu.cli",
        "--db", args.db,
        "panel",
        "--panel-dir", args.panel_dir,
        "--config", args.config,
        "--host", args.host,
        "--port", str(args.port),
    ]


def _start(command: list[str]) -> subprocess.Popen:
    print(f"[dev_panel] starting: {' '.join(command)}", flush=True)
    # cwd = engine root so relative --db / --panel-dir resolve as documented.
    return subprocess.Popen(command, cwd=ENGINE_ROOT)


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return  # already exited
    proc.terminate()
    try:
        proc.wait(timeout=GRACEFUL_SHUTDOWN_SECONDS)
    except subprocess.TimeoutExpired:
        print("[dev_panel] graceful stop timed out — killing", flush=True)
        proc.kill()
        proc.wait()


def _wait_for_change(baseline: dict[Path, float]) -> dict[Path, float]:
    """Block until the watched tree differs from baseline; return the new state."""
    while True:
        time.sleep(POLL_INTERVAL_SECONDS)
        current = _snapshot()
        if current != baseline:
            return current


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Auto-restarting dev runner for the panel bridge")
    parser.add_argument("--db", default="aizu.db")
    parser.add_argument("--panel-dir", default="../admin-panel/dist")
    parser.add_argument("--config", default="config")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    if not WATCH_DIR.is_dir():
        print(f"[dev_panel] error: package dir not found: {WATCH_DIR}", file=sys.stderr)
        return 1

    command = _build_command(args)
    proc = _start(command)
    fingerprint = _snapshot()
    print(f"[dev_panel] watching {WATCH_DIR} for *.py changes "
          f"({len(fingerprint)} files) — Ctrl+C to stop", flush=True)

    try:
        while True:
            time.sleep(POLL_INTERVAL_SECONDS)
            if proc.poll() is not None:
                # Child died on its own (commonly a syntax error in an edit).
                # Surface it, then wait for the next save and retry.
                print(f"[dev_panel] bridge exited (code {proc.returncode}); "
                      "waiting for a .py change to retry", flush=True)
                fingerprint = _wait_for_change(fingerprint)
                proc = _start(command)
                continue
            current = _snapshot()
            if current != fingerprint:
                print("[dev_panel] change detected — restarting bridge", flush=True)
                _stop(proc)
                proc = _start(command)
                fingerprint = current
    except KeyboardInterrupt:
        print("\n[dev_panel] stopping", flush=True)
        _stop(proc)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
