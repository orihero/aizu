#!/usr/bin/env bash
# Launch the warmed, logged-in Chrome the engine attaches to over CDP.
#
# The engine NEVER launches its own browser (config/soul.md: "a passive observer
# attached to a real, warmed, logged-in Chrome"). It connects to an already-running
# Chrome via the DevTools protocol at AIZU_CDP_URL (engine/.env → 127.0.0.1:9333).
#
# Three gotchas, all validated live:
#
#   1. Default profile: Chrome refuses --remote-debugging-port on the DEFAULT profile,
#      and your everyday Chrome already owns it. So we bind a dedicated --user-data-dir.
#
#   2. A profile is bound to ONE browser BRAND — crossing them DESTROYS its logins
#      (validated live 2026-08-18, ledger A9). On macOS, Chrome for Testing reads the
#      Keychain item "Chromium Safe Storage"; system Google Chrome writes "Chrome Safe
#      Storage". Open a profile with the brand that did not warm it and cookie decryption
#      fails — and Chrome DELETES the undecryptable rows rather than quarantining them.
#      Proven on a CLONE of a warmed profile: 18 cookies → 0, the live Instagram sessionid
#      among them, unrecoverable by any browser afterwards. The version gap is NOT the
#      cause: the same system Chrome 151.0.7922.138 run against the clone with
#      --use-mock-keychain (identical build, wrong key) produced the identical total loss,
#      and Chrome's downgrade move-aside/snapshot machinery is #if BUILDFLAG(IS_WIN) — on
#      macOS nothing is set aside.
#
#      So the profile DIRECTORY is a function of the brand: AIZU_CHROME_PROFILE names a
#      BASE, and we launch into <base>/chrome-for-testing or <base>/chrome depending on
#      which binary we resolved. The path IS the ownership record — two brands can never
#      open one directory, so there is nothing to mark, nothing to police, no refusal and
#      no question anybody can answer wrong. Three earlier rounds tried a marker file plus
#      a decision table plus an operator declaration and each one shipped a new hole
#      (ledger A12); the derivation is four lines and cannot be violated.
#      profile_dir_for/brand_of are a FIXED CONTRACT shared with the other two launch
#      sites — desktop/src-tauri/src/chrome_manager.rs and aizu/worker/chrome_manager.py —
#      so all three land in the same directory for the same browser.
#
#   3. Chrome 149 broke connect_over_cdp — real history, NOT current behaviour. Regular
#      Chrome 149 removed the CDP "browser context management" surface
#      (Browser.setDownloadBehavior), so connect_over_cdp died right after the websocket
#      connected with "Browser context management is not supported" (see aizu/cdp.py:attach),
#      and pointing the engine at Playwright's own build was the only way through. Measured
#      again on 2026-08-18 and that is no longer true: system Google Chrome 151.0.7922.138
#      attaches, Chrome for Testing 151.0.7922.34 attaches, and a read-only
#      Target.getBrowserContexts against the LIVE system Chrome returned
#      {'browserContextIds': [], 'defaultBrowserContextId': '…'} with no error.
#      Chrome for Testing stays the default anyway, for a different reason than it was
#      chosen: it is the build the installed Playwright ships with, so its protocol surface
#      matches the client by construction and cannot auto-update out from under us — which
#      is exactly what 149 did. One measured build on one OS is not a licence to switch the
#      default to system Chrome.
set -euo pipefail

PORT="${AIZU_CDP_PORT:-9333}"
# The profile BASE, not the profile: the launch dir is <base>/<brand> (gotcha 2). One name,
# one default, repo-wide — AIZU_CHROME_PROFILE and $HOME/.aizu-cft-profile — because the
# worker preflight used to watch AIZU_CHROME_PROFILE_DIR/~/.aizu-chrome-profile, a directory
# nothing in the repo ever warms, so its profile row reported on an empty dir forever (A12).
# Log into each managed-CDP platform ONCE in the window this opens (instagram.com,
# linkedin.com, x.com); the sessions persist in the brand dir across launches. See
# docs/architecture/engines.md §9.
PROFILE_BASE="${AIZU_CHROME_PROFILE:-$HOME/.aizu-cft-profile}"
# Derived in main() once the binary is resolved. Declared here so the helpers below can be
# sourced and exercised under `set -u` without a launch.
PROFILE=""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="${SCRIPT_DIR}/../.venv/bin/python"
# Where we record the pid of the browser WE launch, so a later run can tell it apart from a
# browser the operator launched (see ensure_port_free). Best effort by design: TMPDIR gets
# reaped, and a missing file only costs us the automatic reclaim, which fails toward refusing.
PID_FILE="${TMPDIR:-/tmp}/aizu-warm-chrome-${PORT}.pid"

# What the pre-launch probe learned about whatever holds ${PORT}. ensure_port_free's refusal
# reads it, because the two ways it can be reached need two different pieces of advice and
# neither of them is "leave it running" (see the refusal itself).
#   no-cdp        nothing answered /json/version at all
#   cdp-rejected  it answered, but connect_over_cdp was rejected
PORT_STATE="no-cdp"

# ─── Brand → directory derivation (gotcha 2) ──────────────────────────────────────────
# FIXED CONTRACT, shared with desktop/src-tauri/src/chrome_manager.rs and
# aizu/worker/chrome_manager.py: same brand tokens, same brand_of rules, same
# <base>/<brand> layout. Drift here and two launch sites warm two different directories
# for the same browser, which costs an operator a re-login (never their cookies — that is
# the point of deriving the path).
BRAND_CFT="chrome-for-testing"
BRAND_CHROME="chrome"
BRAND_CHROMIUM="chromium"
# A base dir that itself holds a Default/ is a profile from BEFORE this change, warmed by an
# unknown brand. "Has a browser used this dir" is a directory STAT and nothing more: reading,
# copying or opening the cookie DB to find out would risk the very thing we are protecting.
PROFILE_USED_DIR="Default"

# Follow symlinks before classifying. A wrapper script or a symlink into Playwright's cache
# is exactly how a Chrome-for-Testing binary sneaks past a path check — and a launch we
# mislabel lands in the OTHER brand's directory, which looks to the operator like every
# session silently vanished. Non-existent paths pass through unchanged (brand_of is called
# on operator-supplied strings in tests and in error paths).
resolve_path() {
  local p="${1:-}" target d b hops=0
  [[ -n "$p" ]] || return 0
  while [[ -L "$p" && "$hops" -lt 20 ]]; do
    target="$(readlink "$p" 2>/dev/null || true)"
    [[ -n "$target" ]] || break
    case "$target" in
      /*) p="$target" ;;
      *)  p="$(dirname "$p")/$target" ;;
    esac
    hops=$((hops + 1))
  done
  d="$(dirname "$p")"
  b="$(basename "$p")"
  if [[ -d "$d" ]]; then
    d="$(cd "$d" 2>/dev/null && pwd -P || printf '%s' "$d")"
  fi
  printf '%s/%s\n' "${d%/}" "$b"
}

# brand_of <binary path> → "chrome-for-testing" | "chrome"
#
# Rule 1 (the "chrome for testing" substring) fires on macOS only: that string is the name of
# Playwright's .app bundle. The SAME Chrome-for-Testing build installs as chrome-linux64/chrome
# (linux-x64), chrome-linux/chrome (linux-arm64) and chrome-win64\chrome.exe (win-x64) — read
# off the installed driver's own EXECUTABLE_PATHS table, playwright/driver/package/lib/
# coreBundle.js — so a substring check ALONE would label CfT "chrome" on those platforms and
# send it into the system-Chrome directory. Rule 2 catches them: every one of those paths sits
# under Playwright's browsers cache, whose directory is chromium-<build> /
# chromium_headless_shell-<build> on every platform (verified against the real
# ~/Library/Caches/ms-playwright).
brand_of() {
  local p seg
  # resolve first, then lowercase and normalise Windows separators so one segment rule
  # covers every platform
  p="$(printf '%s' "$(resolve_path "${1:-}")" | tr '\\' '/' | tr '[:upper:]' '[:lower:]')"
  case "$p" in
    *"chrome for testing"*) printf '%s\n' "$BRAND_CFT"; return 0 ;;
  esac
  while IFS= read -r seg; do
    if [[ "$seg" =~ ^chromium(_headless_shell)?-[0-9]+$ ]]; then
      printf '%s\n' "$BRAND_CFT"; return 0
    fi
  done <<< "$(printf '%s' "$p" | tr '/' '\n')"
  # Rule 3 (Linux): distro Chromium is a THIRD brand with its own keyring entry, not a
  # spelling of Chrome. Filing both under "chrome" would hand /usr/bin/chromium and
  # /usr/bin/google-chrome one directory and wipe whichever warmed it first.
  local leaf="${p##*/}"
  leaf="${leaf%.exe}"
  case "$leaf" in
    chromium|chromium-browser) printf '%s\n' "$BRAND_CHROMIUM"; return 0 ;;
  esac
  printf '%s\n' "$BRAND_CHROME"
}

# The brand token as an operator would name the browser.
brand_label() {
  case "$1" in
    "$BRAND_CFT")    printf '%s\n' "Chrome for Testing" ;;
    "$BRAND_CHROME") printf '%s\n' "system Google Chrome" ;;
    *)               printf '%s\n' "$1" ;;
  esac
}

# profile_dir_for <base> <brand> → the directory that brand launches into. The whole guard,
# in one line: the path IS the ownership record, so no two brands can reach one directory.
profile_dir_for() {
  local base="${1%/}" brand="$2"
  printf '%s/%s\n' "$base" "$brand"
}

# The pre-derivation profile, if there is one. INFORMATIONAL ONLY and never blocking: whatever
# warmed <base>/Default still owns it, we cannot know which browser that was (that guess is
# precisely what the three earlier rounds got wrong), and moving it ourselves would be the
# same gamble with the operator's cookies. So we say it is there, say we left it alone, and
# print both destinations so the move is a copy-paste for the human who DOES know.
# Takes the derived dir as an argument rather than reading $PROFILE, so it can be exercised
# on throwaway dirs without a launch.
note_legacy_profile() {
  local base="${1%/}" derived="$2"
  [[ -d "$base/$PROFILE_USED_DIR" ]] || return 0
  echo "ℹ ${base} holds a ${PROFILE_USED_DIR}/ of its own — a profile warmed before Aizu"
  echo "  started deriving the directory from the browser brand. It has NOT been touched, and"
  echo "  nothing here will touch it: whichever browser warmed it still owns it, and opening it"
  echo "  with the other brand DELETES every cookie in it (ledger A9). This launch uses"
  echo "  ${derived} instead, which starts empty — so expect to sign in again there."
  echo "  If you KNOW which browser warmed the old one, move it in yourself — one of:"
  echo "    D='$(profile_dir_for "$base" "$BRAND_CHROME")'   # system Google Chrome warmed it"
  echo "    D='$(profile_dir_for "$base" "$BRAND_CFT")'   # Chrome for Testing warmed it"
  echo "    mkdir -p \"\$D\" && mv '${base}/${PROFILE_USED_DIR}' \"\$D/${PROFILE_USED_DIR}\""
  echo "    # on Windows also move '${base}/Local State' — the cookie key lives in it there;"
  echo "    # on macOS the key is in the Keychain, so ${PROFILE_USED_DIR}/ alone is enough."
  echo "  If you are not sure, leave it: a fresh sign-in costs you minutes, a wrong guess costs"
  echo "  you every saved login in it, unrecoverably."
}

# Resolve the browser to launch:
#   1. AIZU_CHROME_BINARY override, if set (CHROME_BIN is the legacy spelling and still works
#      — it is what every runbook and the desktop shell have said for months; the AIZU_-
#      prefixed name is the canonical one repo-wide).
#   2. Playwright's bundled Chrome for Testing (protocol-matched — the safe default).
#   3. System Google Chrome (last resort; it attaches fine on 151, but it is unpinned and
#      auto-updates independently of the installed Playwright — see gotcha 3).
resolve_chrome() {
  if [[ -n "${AIZU_CHROME_BINARY:-}" ]]; then
    echo "$AIZU_CHROME_BINARY"; return
  fi
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
    # Browser.setDownloadBehavior call a degraded browser rejects, so this probe reports
    # the port as usable exactly when the engine can actually attach to it.
    pw.chromium.connect_over_cdp(f"http://127.0.0.1:{sys.argv[1]}", no_defaults=True)
finally:
    pw.stop()
PY
}

port_holders() {
  lsof -ti "tcp:${PORT}" 2>/dev/null || true
}

# One line per holder — pid, user, start time, command — so the human can recognise it.
describe_holders() {
  local pid
  for pid in $1; do
    ps -o pid=,user=,lstart=,command= -p "$pid" 2>/dev/null | cut -c1-200 | sed 's/^ */    /'
  done
}

# TRUE only for a browser THIS SCRIPT launched and left behind. Both halves are required:
# the pid must be the one we recorded at launch (pids get reused, so the file alone proves
# nothing), and the process still holding it must carry our exact launch signature — same
# --remote-debugging-port AND same --user-data-dir (the cmdline alone proves nothing either,
# since an operator running the same command by hand produces a byte-identical one). That is
# as close to "ours" as a Unix process gets from the outside, and it is the only case in
# which this script signals anything.
is_our_launch() {
  local pid="$1" recorded cmd
  [[ -r "$PID_FILE" ]] || return 1
  recorded="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ "$recorded" == "$pid" ]] || return 1
  cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
  # Both matches are ANCHORED at the end of the argument (a space, or end of the command
  # line). An unanchored glob is a prefix match, and the per-brand layout made prefixes
  # collide: "<base>/chrome" is a prefix of "<base>/chrome-for-testing", so about to launch
  # system Chrome we would have recognised a RUNNING Chrome-for-Testing as "ours" and
  # SIGTERMed the operator's warmed browser. Ports collide the same way (933 vs 9333).
  _arg_is() { [[ "$cmd" == *"$1 "* || "$cmd" == *"$1" ]]; }
  _arg_is "--remote-debugging-port=${PORT}" || return 1
  _arg_is "--user-data-dir=${PROFILE}" || return 1
  return 0
}

# Make sure the port is ours to bind — WITHOUT killing anyone else's browser.
#
# This used to be free_port(): `pkill -f "remote-debugging-port=${PORT}"` and then `kill -9`
# on whatever lsof reported. That broke the invariant the rest of the system enforces in
# every other place — never kill a browser we did not launch (the desktop chrome_manager
# only ever ADOPTS a running Chrome; the engine is "a passive observer"). Whatever holds
# this port is by construction a warmed, logged-in browser: on the box this was written on,
# a 1.4 GB profile with a live Instagram session. A SIGKILL takes everything it has not
# flushed with it, and no relaunch brings the session back. So the only process we ever
# signal is one we can prove we launched, that one gets a polite SIGTERM (Chrome's graceful
# shutdown flushes cookie and session state) and never a -9, and every other case is
# described to the human, who decides. Ledger A10.
# Returns 0 when the port is free to bind, 1 when the caller must give up.
ensure_port_free() {
  local holders
  holders="$(port_holders)"
  if [[ -z "$holders" ]]; then
    return 0
  fi

  # Exactly one holder and we can prove we launched it → ask it to quit. More than one
  # holder means we cannot attribute the port at all, so we signal nothing.
  if [[ "$(echo "$holders" | wc -w | tr -d ' ')" == "1" ]] && is_our_launch "$holders"; then
    echo "  Port ${PORT} is held by the Chrome this script launched (pid ${holders}) — asking it to quit."
    kill -TERM "$holders" 2>/dev/null || true
    for _ in $(seq 1 20); do
      if [[ -z "$(port_holders)" ]]; then
        rm -f "$PID_FILE"
        return 0
      fi
      sleep 0.5
    done
    echo "✗ Port ${PORT} is still held 10s after SIGTERM — that Chrome is wedged." >&2
    echo "  Quit it from its own window (⌘Q), or kill it yourself once you are sure:" >&2
    describe_holders "$holders" >&2
    return 1
  fi

  echo "✗ Port ${PORT} is held by a process this script did not launch. Refusing to touch it." >&2
  describe_holders "$holders" >&2
  echo "  That is deliberate: the holder is almost certainly a warmed, logged-in browser," >&2
  echo "  and killing it costs you every session it has not flushed to disk." >&2
  # We only ever get here on a port the engine CANNOT use as it stands: either the attach
  # probe was rejected, or nothing spoke CDP at all (the healthy case exited 0 long before
  # this function ran). So "leave it running, the engine can use it" — which this refusal
  # used to offer — is false in both branches, and it is the branch-specific truth that tells
  # the operator which fix is theirs.
  case "$PORT_STATE" in
    cdp-rejected)
      echo "  It cannot simply be left alone either: connect_over_cdp was just REJECTED by that" >&2
      echo "  very browser, so the engine cannot drive it in the state it is in." >&2
      ;;
    *)
      echo "  It cannot simply be left alone either: nothing answered /json/version on ${PORT}" >&2
      echo "  within 2s, so whatever holds the port is not serving CDP right now — it may not" >&2
      echo "  even be a browser. The engine has nothing to attach to." >&2
      ;;
  esac
  echo "  Your call:" >&2
  echo "    · it IS your warmed Chrome → quit it from its OWN window (⌘Q on macOS). A clean" >&2
  echo "      quit flushes its cookies and session state, so its logins survive; then re-run" >&2
  echo "      this script. It relaunches ${PROFILE} on ${PORT} — if the warmed dir is a" >&2
  echo "      different one, pass AIZU_CHROME_PROFILE=/its/base so you get the same browser back." >&2
  echo "    · it is something else     → identify it from the line(s) above and stop it there," >&2
  echo "      or leave it and take another port: AIZU_CDP_PORT=9444 $0" >&2
  echo "    · you want a SECOND one    → AIZU_CDP_PORT=9444 AIZU_CHROME_PROFILE=~/.aizu-alt-profile $0" >&2
  echo "      (a second profile must be warmed — and logged into — on its own)." >&2
  return 1
}

# ─── main ─────────────────────────────────────────────────────────────────────────────
# Everything above is definitions only, so the derivation can be sourced and exercised on
# scratch ports and throwaway profile dirs without launching a browser:
#   AIZU_WARM_CHROME_LIB=1 source engine/scripts/warm_chrome.sh
# That seam exists because the alternative — testing a brand/profile pairing by running the
# script for real — is precisely the experiment that costs an operator their logins (A9).
if [[ -n "${AIZU_WARM_CHROME_LIB:-}" ]]; then
  return 0 2>/dev/null || exit 0
fi

CHROME="$(resolve_chrome)"

if [[ ! -x "$CHROME" ]]; then
  echo "✗ Chrome binary not found at: $CHROME" >&2
  echo "  Set AIZU_CHROME_BINARY=/path/to/chrome and retry." >&2
  exit 1
fi

# The profile is a function of the brand — derive it before anything else reports a path.
BRAND="$(brand_of "$CHROME")"
PROFILE="$(profile_dir_for "$PROFILE_BASE" "$BRAND")"

if curl -s -m 2 "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1; then
  if cdp_attaches; then
    echo "✓ CDP already up on http://127.0.0.1:${PORT} and connect_over_cdp attaches — nothing to do."
    echo "  NOTE: confirm you're logged into the platforms you'll run (instagram.com, linkedin.com, x.com)."
    exit 0
  fi
  PORT_STATE="cdp-rejected"
  echo "⚠ CDP answers on ${PORT} but connect_over_cdp is REJECTED ('Browser context"
  echo "  management is not supported'). Something the engine cannot drive holds the port:"
  echo "  a long-running browser that degraded, or a build whose CDP surface does not match"
  echo "  the installed Playwright. It has to go before a usable Chrome can bind ${PORT}."
fi

# The port must be free before we launch — including when nothing answered /json/version at
# all (a non-CDP process on the port would otherwise let Chrome fail to bind in silence).
ensure_port_free || exit 1

# Name the brand we actually resolved, not the one the default suggests: the override and the
# system-Chrome fall-through both land here too, and "Launching warmed Chrome for Testing"
# over a system-Chrome launch is the exact confusion gotcha 2 is about.
echo "▶ Launching warmed $(brand_label "$BRAND") · port=${PORT} profile=${PROFILE}"
echo "  binary: ${CHROME}"
echo "  profile dir is derived: ${PROFILE_BASE}/${BRAND} — one directory per browser brand,"
echo "  so the other brand can never open this one (gotcha 2)."
note_legacy_profile "$PROFILE_BASE" "$PROFILE"
mkdir -p "$PROFILE"
"$CHROME" \
  --remote-debugging-port="${PORT}" \
  --user-data-dir="${PROFILE}" \
  --no-first-run \
  --no-default-browser-check \
  >/dev/null 2>&1 &
echo "$!" > "$PID_FILE" 2>/dev/null || true

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
echo "  Is another Chrome already holding ${PROFILE}? A profile dir can only be open in one" >&2
echo "  browser at a time — quit that window, or point AIZU_CHROME_PROFILE at another base." >&2
exit 1
