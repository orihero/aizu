#!/usr/bin/env bash
# Launch the local dev panel + dispatch server (default :8765) with the worker bootstrap
# token loaded from the SAME 0600 secret file the AIZU Worker desktop app reads. Without
# this token in the server's env, a freshly launched worker cannot first-register and shows
# up as "disconnected" (register → 401). Sourcing it here keeps ONE source of truth: the
# dev menu writes dispatch-token.secret, and both the worker AND this server read it.
#
#   engine/scripts/dev_panel.sh                 # db=aizu.db, port=8765
#   engine/scripts/dev_panel.sh --port 8770     # any dev_panel.py flag is forwarded
#
# To rotate the token: set it once in the worker app's dev menu (which rewrites the secret
# file), then restart this server so it picks up the new value.
set -euo pipefail

ENGINE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SECRET="$HOME/Library/Application Support/com.aizu.workerdesktop/dispatch-token.secret"

if [[ -f "$SECRET" ]]; then
  AIZU_WORKER_BOOTSTRAP_TOKEN="$(cat "$SECRET")"
  export AIZU_WORKER_BOOTSTRAP_TOKEN
  echo "dev_panel: loaded worker bootstrap token from dispatch-token.secret"
else
  echo "dev_panel: WARN — $SECRET not found; worker first-register will be rejected (401)." >&2
  echo "           Set a bootstrap token in the AIZU Worker dev menu, then re-run this." >&2
fi

PY="$ENGINE_DIR/.venv/bin/python"
[[ -x "$PY" ]] || PY="python3"
cd "$ENGINE_DIR"
exec "$PY" scripts/dev_panel.py "$@"
