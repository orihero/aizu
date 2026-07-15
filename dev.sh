#!/usr/bin/env bash
#
# dev.sh — start every app for local development with one command.
#
# Launches, with prefixed/interleaved logs, and tears all of them down on Ctrl+C:
#   1. bridge  — the engine's JSON API (reelradar) on 127.0.0.1:8765, via
#                scripts/dev_panel.py (auto-restarts on engine/reelradar/*.py edits)
#   2. panel   — the admin-panel Vite dev server on http://localhost:5173,
#                which proxies /api -> the bridge
#
# The engine *run* itself (the live Instagram crawl) is NOT started here — it is
# launched from the panel's Run button or `reelradar.cli ... run`. This script
# only brings up the two long-lived dev servers you need to use the panel.
#
# Usage:
#   ./dev.sh                 # bridge on :8765, panel on :5173
#   BRIDGE_PORT=8770 ./dev.sh
#   DB=other.db ./dev.sh
#
set -euo pipefail

# --- Configuration (override via environment) --------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_DIR="$REPO_ROOT/engine"
PANEL_DIR="$REPO_ROOT/admin-panel"
VENV_PYTHON="$ENGINE_DIR/.venv/bin/python"

BRIDGE_PORT="${BRIDGE_PORT:-8765}"
BRIDGE_HOST="${BRIDGE_HOST:-127.0.0.1}"
PANEL_PORT="${PANEL_PORT:-5173}"
DB="${DB:-reelradar.db}"

# --- Output helpers ----------------------------------------------------------
COLOR_BRIDGE='\033[0;36m'  # cyan
COLOR_PANEL='\033[0;35m'   # magenta
COLOR_INFO='\033[0;32m'    # green
COLOR_ERR='\033[0;31m'     # red
COLOR_RESET='\033[0m'

info()  { printf "${COLOR_INFO}[dev]${COLOR_RESET} %s\n" "$*"; }
fail()  { printf "${COLOR_ERR}[dev] error:${COLOR_RESET} %s\n" "$*" >&2; exit 1; }

# --- Preflight checks --------------------------------------------------------
[ -x "$VENV_PYTHON" ] || fail "engine venv not found at $VENV_PYTHON — create it (python -m venv engine/.venv && engine/.venv/bin/pip install -e engine)"
[ -d "$PANEL_DIR/node_modules" ] || fail "panel deps not installed — run: (cd admin-panel && npm install)"
[ -x "$PANEL_DIR/node_modules/.bin/vite" ] || fail "vite binary missing — run: (cd admin-panel && npm install)"

# --- Free any port still held by a previous run ------------------------------
# The #1 cause of "OSError: [Errno 48] Address already in use" here is a stale
# bridge/panel from an earlier ./dev.sh that didn't tear down cleanly (crash,
# SIGKILL, closed terminal). Kill whatever is bound to our ports before we try
# to bind them ourselves.
free_port() {
  local port="$1" label="$2" pids
  pids="$(lsof -ti "tcp:$port" -sTCP:LISTEN 2>/dev/null || true)"
  [ -n "$pids" ] || return 0
  info "port $port ($label) held by stale process(es) [$(echo "$pids" | tr '\n' ' ')] — killing"
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
  sleep 1
  pids="$(lsof -ti "tcp:$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "$pids" ]; then
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
    sleep 1
  fi
}

free_port "$BRIDGE_PORT" bridge
free_port "$PANEL_PORT" panel

# --- Process management ------------------------------------------------------
BRIDGE_PID=""
PANEL_PID=""

# Recursively signal a process and all its descendants, leaves first. Each
# server runs behind a sed pipe (a subshell) and itself spawns children —
# dev_panel.py spawns the panel server, vite spawns esbuild — so a single-level
# pkill is not enough; we must walk the whole tree.
kill_descendants() {
  local pid="$1" signal="$2" child
  [ -n "$pid" ] || return 0
  for child in $(pgrep -P "$pid" 2>/dev/null); do
    kill_descendants "$child" "$signal"
  done
  kill -"$signal" "$pid" 2>/dev/null || true
}

cleanup() {
  trap - INT TERM EXIT
  info "shutting down…"
  kill_descendants "$PANEL_PID" TERM
  kill_descendants "$BRIDGE_PID" TERM
  sleep 1
  # Anything that ignored TERM gets a hard KILL so no port is left held.
  kill_descendants "$PANEL_PID" KILL
  kill_descendants "$BRIDGE_PID" KILL
  wait 2>/dev/null || true
  info "stopped."
}
trap cleanup INT TERM EXIT

# --- Launch ------------------------------------------------------------------
info "starting bridge (engine API) on $BRIDGE_HOST:$BRIDGE_PORT  db=$DB"
(
  cd "$ENGINE_DIR"
  "$VENV_PYTHON" scripts/dev_panel.py \
    --db "$DB" --host "$BRIDGE_HOST" --port "$BRIDGE_PORT" 2>&1 \
    | sed "s/^/$(printf "${COLOR_BRIDGE}[bridge]${COLOR_RESET} ")/"
) &
BRIDGE_PID=$!

info "starting panel (Vite dev server) — proxies /api -> :$BRIDGE_PORT"
(
  cd "$PANEL_DIR"
  node_modules/.bin/vite 2>&1 \
    | sed "s/^/$(printf "${COLOR_PANEL}[panel]${COLOR_RESET} ")/"
) &
PANEL_PID=$!

info "both servers up — open the URL the panel prints below. Ctrl+C to stop everything."

# Block while both children live; the moment either dies, fall through so the
# EXIT trap (cleanup) stops the survivor. Polling keeps this portable to the
# macOS system bash 3.2, which lacks `wait -n`.
while kill -0 "$BRIDGE_PID" 2>/dev/null && kill -0 "$PANEL_PID" 2>/dev/null; do
  sleep 1
done
