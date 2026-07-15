#!/usr/bin/env bash
# Launch the warmed, logged-in Chrome the engine attaches to over CDP.
#
# The engine NEVER launches its own browser (config/soul.md: "a passive observer
# attached to a real, warmed, logged-in Chrome"). It connects to an already-running
# Chrome via the DevTools protocol at REELRADAR_CDP_URL (engine/.env → 127.0.0.1:9333).
#
# Two gotchas, both validated live:
#
#   1. Default profile: Chrome refuses --remote-debugging-port on the DEFAULT profile,
#      and your everyday Chrome already owns it. So we bind a dedicated --user-data-dir.
#
#   2. Chrome 149+ stable breaks connect_over_cdp (the blocker as of 2026-06-25):
#      regular Google Chrome 149 removed the CDP "browser context management" surface
#      (Browser.setDownloadBehavior), so Playwright's connect_over_cdp dies right after
#      the websocket connects with "Browser context management is not supported"
#      (see reelradar/cdp.py:attach). Playwright 1.60 is already the latest, so there is
#      no newer Playwright to fix it. The fix is to attach to Playwright's OWN bundled
#      "Chrome for Testing" build, whose CDP surface matches the installed Playwright.
#      This script resolves that binary automatically.
set -euo pipefail

PORT="${REELRADAR_CDP_PORT:-9333}"
# Chrome for Testing is a DIFFERENT (older) build than your system Chrome, so it gets a
# DEDICATED profile dir — a profile written by Chrome 149 cannot be reopened by CfT 148.
# Log into each managed-CDP platform ONCE in this window (instagram.com, linkedin.com,
# x.com); the sessions persist in this dir across launches. See docs/ENGINES.md §9.
PROFILE="${REELRADAR_CHROME_PROFILE:-$HOME/.reelradar-cft-profile}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="${SCRIPT_DIR}/../.venv/bin/python"

# Resolve the browser to launch:
#   1. CHROME_BIN override, if set.
#   2. Playwright's bundled Chrome for Testing (protocol-matched — the safe default).
#   3. System Google Chrome (last resort; known to fail connect_over_cdp on 149+).
resolve_chrome() {
  if [[ -n "${CHROME_BIN:-}" ]]; then
    echo "$CHROME_BIN"; return
  fi
  if [[ -x "$VENV_PY" ]]; then
    local cft
    cft="$("$VENV_PY" -c 'from playwright.sync_api import sync_playwright; p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()' 2>/dev/null || true)"
    if [[ -n "$cft" && -x "$cft" ]]; then
      echo "$cft"; return
    fi
  fi
  echo "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
}

CHROME="$(resolve_chrome)"

if [[ ! -x "$CHROME" ]]; then
  echo "✗ Chrome binary not found at: $CHROME" >&2
  echo "  Set CHROME_BIN=/path/to/chrome and retry." >&2
  exit 1
fi

# Does Playwright's connect_over_cdp actually succeed against the port? A bare
# /json/version 200 is NOT enough: a stale/long-running Chrome (even the right
# CfT build) can degrade into rejecting Browser.setDownloadBehavior with
# "Browser context management is not supported", which only surfaces on a real
# connect_over_cdp. Validated live 2026-06-29. Returns 0 only on a real attach.
cdp_attaches() {
  [[ -x "$VENV_PY" ]] || return 1
  "$VENV_PY" - "$PORT" <<'PY' >/dev/null 2>&1
import sys
from playwright.sync_api import sync_playwright
pw = sync_playwright().start()
try:
    # no_defaults=True mirrors how the engine attaches (cdp.py) — it skips the
    # Browser.setDownloadBehavior call CfT 148 rejects, so this probe reports the
    # port as usable exactly when the engine can actually attach to it.
    pw.chromium.connect_over_cdp(f"http://127.0.0.1:{sys.argv[1]}", no_defaults=True)
finally:
    pw.stop()
PY
}

# Actually free the port. A single best-effort `pkill` is NOT enough: it can miss the
# holder (timing/permission race), and the script would then relaunch a "fresh" Chrome
# that fails to bind, leaving the SAME degraded instance serving 9333 — the engine
# reconnects to it and hits the exact same rejection (identical browser GUID in the
# error). Validated live 2026-07-01. So: pkill by cmdline, then kill -9 the real port
# holder via lsof, then poll until the port is genuinely free before returning.
free_port() {
  pkill -f "remote-debugging-port=${PORT}" 2>/dev/null || true
  sleep 1
  local pids
  pids="$(lsof -ti "tcp:${PORT}" 2>/dev/null || true)"
  [[ -n "$pids" ]] && kill -9 $pids 2>/dev/null || true
  for _ in $(seq 1 20); do
    lsof -ti "tcp:${PORT}" >/dev/null 2>&1 || return 0
    sleep 0.5
  done
  echo "✗ Port ${PORT} is still held after kill — a process is stuck." >&2
  echo "  Holder(s): $(lsof -ti "tcp:${PORT}" 2>/dev/null | tr '\n' ' ')" >&2
  return 1
}

if curl -s -m 2 "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1; then
  if cdp_attaches; then
    echo "✓ CDP already up on http://127.0.0.1:${PORT} and connect_over_cdp attaches — nothing to do."
    echo "  NOTE: confirm you're logged into the platforms you'll run (instagram.com, linkedin.com, x.com)."
    exit 0
  fi
  echo "⚠ CDP answers on ${PORT} but connect_over_cdp is REJECTED ('Browser context"
  echo "  management is not supported'). This is a stale/incompatible Chrome holding the"
  echo "  port (a long-running CfT that degraded, or a system Chrome 149+ that lacks the"
  echo "  surface). Killing it and relaunching a fresh Chrome for Testing — your logins"
  echo "  persist in ${PROFILE}, so nothing is lost."
  free_port || exit 1
fi

echo "▶ Launching warmed Chrome for Testing · port=${PORT} profile=${PROFILE}"
echo "  binary: ${CHROME}"
"$CHROME" \
  --remote-debugging-port="${PORT}" \
  --user-data-dir="${PROFILE}" \
  --no-first-run \
  --no-default-browser-check \
  >/dev/null 2>&1 &

# Wait for a REAL attach before handing control back — a /json/version 200 alone is
# not enough (a degraded Chrome answers HTTP but still rejects connect_over_cdp), so we
# gate on cdp_attaches, the same probe the engine uses.
for _ in $(seq 1 20); do
  if curl -s -m 2 "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1 && cdp_attaches; then
    echo "✓ CDP is up and connect_over_cdp attaches: http://127.0.0.1:${PORT}"
    echo "  → If the window isn't logged into your platforms (instagram.com, linkedin.com, x.com), log in now, then run the engine."
    exit 0
  fi
  sleep 0.5
done

echo "✗ CDP did not come up (or connect_over_cdp did not attach) on port ${PORT} within 10s." >&2
echo "  Is another Chrome holding this profile? Close it, or pick a different profile dir." >&2
exit 1
