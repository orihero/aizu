# Known Issues & Findings — Bug Ledger

A running log of real bugs, gotchas, and their resolutions so we don't rediscover them.
Grouped by area. Each entry: **Symptom** (what you observe) → **Root cause** → **Fix** →
**How to avoid / detect**. Add new entries at the top of the relevant section; never delete
(strike through if superseded).

> Sister docs: [`feedback-ui-mistakes.md`](./feedback-ui-mistakes.md) (UI-specific slip-ups),
> [`docs/ops/desktop-packaging.md`](../docs/ops/desktop-packaging.md) (build steps),
> [`docs/prd/distributed-workers-BUILD-PLAN.md`](../docs/prd/distributed-workers-BUILD-PLAN.md).

---

## A. Desktop worker app (Tauri 2 + PyInstaller freeze)

### A12. Three rounds of guard could not hold an invariant the DIRECTORY holds for free
**Symptom:** The underlying defect never changed: open a warmed Chrome profile with the *other*
browser brand and every cookie in it is **deleted** (A9). What changed three times was the
guard, and each version failed in front of an operator. Round 1: after upgrading, "Launch warmed
Chrome" refused on **every box that had ever launched Chrome through the app**, permanently, and
named a way out that had no UI behind it. Round 2 fixed that and mis-detected Chrome for Testing
on Linux/Windows, i.e. *vouched* for the crossing. Round 3 shipped the wizard's declaration —
`[ Chrome ] [ Chrome for Testing ]` — the operator clicked their answer, the marker was written,
and the next launch **refused again**: `resolve_chrome_binary` never read the marker, so it
re-resolved Chrome for Testing regardless. The button was a dead end from the commit that added
it.
**Root cause:** Two layers, and only the first one is a bug.
1. *The real defect.* On macOS the cookie-encryption key lives in a **branded** Keychain item:
   Playwright's Chrome for Testing reads `Chromium Safe Storage`, system Google Chrome writes
   `Chrome Safe Storage`. The wrong key does not fail softly — Chrome **deletes** every row it
   cannot decrypt instead of quarantining it, and the move-aside/snapshot machinery that would
   have saved the profile (`DowngradeManager`) is `#if BUILDFLAG(IS_WIN)`; on macOS nothing is
   set aside. Measured on a **clone** of a warmed profile: 18 cookies → 0, live Instagram
   `sessionid` included, unreadable by any browser afterwards. The controlled half of the
   experiment ruled out the version gap as the cause: the *same* system Chrome 151.0.7922.138
   relaunched against the clone with `--use-mock-keychain` (identical build, deliberately wrong
   key) lost exactly as much.
2. *The design.* We kept trying to make **two brands share one directory safely**. Nothing
   inside a Chrome profile records which brand wrote it, so that goal requires: a marker file we
   invent, a decision table with a row nobody can resolve (used profile, no marker), a refusal,
   an operator declaration to escape the refusal, a UI to ask the question, and the identical
   contract implemented three times in Rust, Python and bash. Every one of those is a place to
   be wrong, and every round was wrong in a new one. The guard grew; the invariant never got
   safer.
**Fix:** Make the **directory a function of the brand**. `AIZU_CHROME_PROFILE` names a *base*;
every launch site opens `profile_dir_for(base, brand) = <base>/<brand>` —
`~/.aizu-cft-profile/chrome-for-testing` or `~/.aizu-cft-profile/chrome`. The path **is** the
ownership record, so two brands can never open one directory *by construction*: nothing to mark,
nothing to police, no refusal, no declaration, no question anyone can answer wrong. `brand_of`
survives unchanged in all three languages — it now **chooses** a directory instead of policing
one (and resolves symlinks first, since a wrapper or a `google-chrome` symlink into Playwright's
cache is how a CfT binary reaches a launch site under another name). **Deleted**, not deprecated:
the `.aizu-browser-brand` marker read *and* write, `BrandDecision`, the refusal error variants,
the declaration commands, the wizard's brand block and its buttons, the "use a fresh profile
directory" command, the bash guard and its decision table, and all of their tests. The one
judgement call left is the **legacy profile** — a base that itself holds a `Default/`, warmed
before this change by an unknown brand. It is never opened, moved, renamed, copied, backed up or
deleted; each launch site surfaces it once, informationally and never blocking, and prints
**both** candidate destination paths so an operator who knows which browser warmed it can move it
themselves. Nothing guesses. That guess is what the whole arc was about.
**How to avoid/detect:** The general lesson, and it is the point of this entry: **when a guard
needs a marker file, a decision table, a UI and a three-language contract to hold an invariant
up, stop hardening the guard and change the shape so the invariant cannot be violated.** An
invariant that is structural needs no tests for its refusal path, no migration for boxes that
already exist, and cannot drift between implementations — `profile_dir_for` is four lines in each
of the three languages and it deleted far more than it added in all three. Two concrete detectors that would each have caught a round
of this: (1) before shipping a guard, ask what it does on a box that **already exists** — round
1's answer was "refuses forever"; (2) for every affordance you put in front of a user, grep for
the **reader** of the value it writes. The declaration button wrote `.aizu-browser-brand`;
grepping that constant's readers would have shown the only one was the guard — never
`resolve_chrome_binary`, which is what actually decides the browser — so the click could not
change the outcome it promised to change. That is the
**sixth** time this ledger records a fix landing at a layer no user reaches (B4, E7, F-10a,
F-10b, `warm_chrome.sh` in A11c, and now the declaration button); G3 counted four, A11 made it
five. The recurrence is the finding.

### A13. One setting, two spellings — a preflight row that watched a directory nothing warms
**Symptom:** The worker preflight's profile row reported on `~/.aizu-chrome-profile` while the
launcher the docs hand operators (`engine/scripts/warm_chrome.sh`) warms `~/.aizu-cft-profile`.
On every box in existence the row was therefore about an empty or absent directory — it could
never see the profile that was actually at risk, and it read as "checked, fine".
**Root cause:** The same setting acquired two names and two defaults, each introduced in the
component that needed it: `AIZU_CHROME_PROFILE_DIR` (worker, default `~/.aizu-chrome-profile`)
vs `AIZU_CHROME_PROFILE` (shell, default `~/.aizu-cft-profile`). Different variable **and**
different default, so nothing collides loudly — the two halves simply describe different disks.
`CHROME_BIN` (desktop shell, `warm_chrome.sh`) vs `AIZU_CHROME_BINARY` (worker) is the same split
one layer over, and under A12 it is no longer cosmetic: the binary now **chooses the profile
directory**, so an operator who pins one browser in one process and gets the other in the next
lands in a different profile dir and sees a box that is silently signed out.
**Fix:** One name and one default repo-wide: **`AIZU_CHROME_PROFILE`**, default
**`~/.aizu-cft-profile`** (the path that exists on real boxes and the one `.env.example` already
documented); `AIZU_CHROME_PROFILE_DIR` is gone. **`AIZU_CHROME_BINARY`** is canonical for the
binary, with `CHROME_BIN` still read as a fallback — it is what every runbook and the desktop
shell have said for months and dropping it would silently move an operator's pinned browser. The
table lives in `docs/ops/desktop-packaging.md` §3.1 so the next component has one place to copy
from.
**How to avoid/detect:** A check whose subject is a filesystem path must **print the path it
inspected**, and a reviewer should be able to grep the repo for something that *writes* that
path. If no component in the tree ever creates it, the check is watching a directory nothing
warms and its green means nothing. More bluntly: two env vars whose names differ only by a
suffix are one setting until proven otherwise — grep for `AIZU_[A-Z_]*CHROME[A-Z_]*` across
`engine/`, `desktop/` and `scripts/` before adding the third.

### A11. The brand guard's FIRST version bricked every existing box — and missed a third launch site
**Symptom:** Three separate failures from one round of work, all found in review before shipping:
(a) after upgrading, the desktop app's "Launch warmed Chrome" refused on **every box that had
ever launched Chrome through the app**, permanently — the refusal named a way out ("point this
browser at a DIFFERENT profile directory") that had no UI behind it (`grep -n profile
desktop/ui/main.js` → 0 hits); (b) on Linux and Windows the guard would happily stamp
Playwright's Chrome-for-Testing as `chrome`, i.e. *vouch* for exactly the cross-brand launch it
exists to prevent; (c) `engine/scripts/warm_chrome.sh` — the launch site the docs actually hand
operators — had no guard at all, so on the machine this was written on the documented command
would have opened Chrome-for-Testing against a system-Chrome-warmed `~/.aizu-cft-profile` and
deleted its 18 live cookies (A9).
**Root cause:** The guard itself (`<profile>/.aizu-browser-brand`, decision table in A9's
resolution) is right; each defect is a hole around it.
(a) The marker is written *after* the guard passes (`chrome_manager.rs`'s
`write_brand_marker`), so nothing ever backfills one. The app's default profile is
`<app data>/chrome-profile`, which on any box in service already holds a `Default/` — the
"used, unmarked ⇒ refuse" row, forever, with no affordance to change the profile dir.
(b) `brand_of` tested for the substring `chrome for testing`, which appears **only** in
Playwright's macOS `.app` bundle. Read off the installed driver's own `EXECUTABLE_PATHS`
(`playwright/driver/package/lib/coreBundle.js`): chromium is `chrome-linux64/chrome`
(linux-x64), `chrome-linux/chrome` (linux-arm64), `chrome-win64\chrome.exe` (win-x64). A
"detector" that is correct on the developer's OS and inverted elsewhere is worse than none:
it writes a marker that later launches *trust*.
(c) The first pass guarded the two launch sites we were editing (Rust shell, Python worker) and
documented the danger in `warm_chrome.sh`'s header — 200 lines above a launch that still had no
check. This is the **fifth** time the ledger records the lesson G3 already counted four for four
(B4, E7, F-10a, F-10b): **a fix is not done at the layer you edited — it is done at the layer a
user reaches**, and the layer an operator reaches for a warmed browser is this shell script.
**Fix:** (a) **The operator declares the brand.** On an unmarked-but-used profile the wizard's
Chrome step asks which browser warmed it and offers `[ Chrome ] [ Chrome for Testing ]
[ Use a fresh profile directory ]`; the first two write the marker, the third points
`chrome_profile_dir` at a new empty dir and leaves the old profile untouched (that third choice
is also the affordance the refusal copy was already promising). The question states the cost of
a wrong answer — every saved login in that profile, unrecoverably. The app never guesses.
(b) `brand_of` gains a second rule, identical in Rust, Python and bash: any path **segment**
matching `^chromium(_headless_shell)?-[0-9]+$` is Chrome for Testing. That is Playwright's
browsers-cache directory and it holds on every platform (verified against the real
`~/Library/Caches/ms-playwright`: `chromium-1234`, `chromium_headless_shell-1234`).
(c) `warm_chrome.sh` implements the whole contract — same filename, same tokens, same table —
and refuses in a terminal-appropriate way: it prints the exact `printf 'chrome\n' > …` command
to declare the brand, or the `AIZU_CHROME_PROFILE=…` invocation for a fresh dir, and touches
nothing itself. The guard runs *before* `ensure_port_free`, so a launch we are going to refuse
never first asks a healthy browser to quit. Same round, same file: `ensure_port_free`'s refusal
stopped offering "leave it running, the engine can use it exactly as it is" — it is reachable
only when `connect_over_cdp` was **rejected** or when nothing answered `/json/version` at all,
so that sentence was false in both branches; it now says which of the two happened.
**How to avoid/detect:** A guard that can only refuse is not a safe default — before shipping
one, ask what it does on a box that **already exists**, and make sure the way out it names is
reachable from where the operator is standing (a UI control, or a command they can paste). When
provenance is genuinely unknowable, the honest move is to ask the human and say what a wrong
answer costs, not to guess quietly. Detection rule for the brand contract: it lives in three
languages, so exercise all three — the bash half is testable without a browser
(`AIZU_WARM_CHROME_LIB=1 source engine/scripts/warm_chrome.sh` defines the functions and
returns before `main`; drive them against throwaway dirs and a scratch port, never 9333). Live
proof it earns its keep: on the box this was written on `~/.aizu-cft-profile` holds a
`Default/`, has no marker, and was warmed by **system** Chrome — while `resolve_chrome()`
prefers Chrome for Testing. The documented command used to walk straight into A9 there; it now
refuses and explains.
**Superseded by A12:** every mechanism above — the marker, the decision table, the refusal, the
operator declaration and its wizard buttons — has been deleted. The profile directory is now
derived from the brand (`<base>/<brand>`), so there is no shared directory left to police. Only
`brand_of` and its two rules survive, and only to pick the directory. Keep this entry for the
holes it names: they are what a marker-and-refusal design costs, and (a) and the round-3
declaration button are two of the six sightings of the "fix at a layer no user reaches" pattern.

### A8. `cdp_probe` has NEVER attached, in any build — a permanent FALSE RED on a healthy box
**Symptom:** On a box whose warmed Chrome is perfectly healthy — `/json/version` 200, the engine
attaching over CDP right now — the desktop app's Chrome step reports the degraded-browser dead end
and tells the operator to "quit that Chrome COMPLETELY and relaunch". Doing so changes nothing,
because the next launch is reported degraded too. Reproduces wherever `venv_python()` resolves,
i.e. `cargo tauri dev` from `desktop/src-tauri` (which reaches `../../engine/.venv/bin/python`).
Shipped in 361cf97 and wrong from the first commit.
**Root cause:** `chrome_manager.rs`'s `cdp_probe` builds the Python it shells out to with a
`format!` whose lines end in `\n\`. In Rust a backslash at end-of-line strips the newline **and
every leading whitespace character on the continuation line**, so the `\n` is the only newline
that survives and the block indentation is deleted outright. The emitted program is
`try:` followed by a column-0 body, and the interpreter dies before touching Chrome:
`IndentationError: expected an indented block after 'try' statement on line 4`. Compiled the
literal in isolation and ran what it emits — that error is the whole story. The interpreter
*runs* and exits non-zero, which is exactly the branch `status_within` maps to `Some(false)` ⇒
`CdpProbe::Rejected`. So the carefully-built tri-state of A7 (no interpreter ⇒ `Unknown`, never
`Rejected` — "could not verify is not fine") collapsed the other way: with an interpreter present
the probe could only ever return `Rejected`, and `Rejected` is sticky (`degraded_launch`).
**Fix:** Build the script so the emitted text is what you read in the source — explicit `\n`
separators, a raw/`concat!` literal, or a here-doc-style const — never `format!` line
continuations for whitespace-significant text.
**How to avoid/detect:** The generated program must **compile**. A unit test that renders the
script and feeds it to `python -c 'import sys; compile(sys.stdin.read(), "p", "exec")'` (skipped
when no interpreter is available) fails on today's literal and passes on the fix, and it costs
nothing to run. The general trap: `\` at EOL inside a Rust string is not a line continuation that
preserves your source layout — it eats the indentation you can see. Any embedded Python, YAML,
Makefile or shell heredoc built that way is silently mangled. And a probe whose *failure* branch
is the alarming one must be proven to reach its success branch at least once against a known-good
target, or "always alarming" reads exactly like "correctly detecting a problem".

### A9. Launching a different browser BRAND against a warmed profile DESTROYS its logins (macOS)
**Symptom:** A warmed profile that had live Instagram / LinkedIn / X sessions comes back logged
OUT of everything after one launch with the "other" Chrome. The cookie DB is not corrupt and not
locked — it is **empty**. No later launch of either browser brings the sessions back; the only
recovery is logging in again by hand on every platform.
**Root cause:** On macOS, Chromium's cookie encryption key lives in a Keychain item whose name is
**branded**: Playwright's Chrome for Testing reads `Chromium Safe Storage`, system Google Chrome
writes `Chrome Safe Storage`. Point one brand at a profile the other warmed and every
`v10`-encrypted value decrypts with the wrong key — and Chrome **deletes** the undecryptable rows
rather than quarantining them, so the loss happens on the first launch and is permanent.
Measured on a **clone** of a warmed profile: 18 cookies → 0, the live Instagram `sessionid`
included, unreadable by any browser afterwards.
The version downgrade is **not** the cause, and this was the controlled part of the experiment:
the *same* system Chrome 151.0.7922.138 relaunched against the clone with `--use-mock-keychain`
(identical build, deliberately wrong key) produced the identical total loss. Chrome's
`DowngradeManager` move-aside/snapshot machinery — the thing that renames a too-new `User Data`
out of the way instead of eating it — is `#if BUILDFLAG(IS_WIN)`; on macOS nothing is set aside.
**Honest limit:** the un-mocked Chrome-for-Testing-against-a-Chrome-warmed-profile case was
**inferred, not executed**. Reading a foreign Keychain item can raise a blocking GUI prompt, and
this ran on the operator's live machine next to a 1.4 GB warmed profile; `--use-mock-keychain`
reproduces the same wrong-key condition without touching the Keychain, so the mechanism is proven
and only that one link is by inference.
**Fix:** Treat a profile dir as bound to the brand that warmed it, forever. `warm_chrome.sh`
documents it as gotcha 2 and reminds you on every launch against an existing profile;
`docs/ops/desktop-packaging.md` §3 carries the same warning for `chrome_profile_dir`. If a
profile was warmed by system Chrome, keep opening it with system Chrome (`CHROME_BIN=…`);
otherwise warm a FRESH dir with Chrome for Testing.
**How to avoid/detect:** There is nothing to detect at runtime — **nothing inside the profile
records which brand wrote it**, and `Last Version` records only a version number, which the
experiment above proves is the wrong discriminator. So this is a *convention* enforced by
documentation and defaults, not a check. The live trap to watch: the default
`chrome_profile_dir`/`AIZU_CHROME_PROFILE` is `~/.aizu-cft-profile`, a *name* that promises CfT
but on a hand-warmed box may hold a system-Chrome profile — and the setup wizard's
"Download browser" → "Launch warmed Chrome" sequence would then wipe it. Never test a
brand/profile pairing on the real profile: clone it (`cp -R`) and burn the clone, which is how
this was measured.
~~**Superseded in part by A11:** "a convention, not a check" held only until we wrote the record
ourselves. Nothing *inside* a Chrome profile still names its brand, but every launch site now
stamps and consults `<profile>/.aizu-browser-brand`, so the crossing is refused rather than
merely documented — including in `warm_chrome.sh`, which is where this trap was live.~~
**Superseded by A12:** there is no marker and no refusal any more. Nothing inside a Chrome
profile names its brand, so we stopped needing that fact: the directory is *derived* from the
brand (`<base>/chrome`, `<base>/chrome-for-testing`), and the crossing became unreachable rather
than detected. The mechanism recorded above is still exactly right, and it is still why no
component may ever open, move or "repair" a profile whose provenance it does not know.

### A10. `warm_chrome.sh` killed a warmed browser it did not launch
**Symptom:** Running `engine/scripts/warm_chrome.sh` while any Chrome held the CDP port
SIGKILLed it — including the operator's warmed, logged-in browser, and including one that was
attaching perfectly well but happened to fail the script's probe (no venv Playwright, a slow
start, a 2s curl timeout). Whatever that browser had not flushed went with it.
**Root cause:** `free_port()` did `pkill -f "remote-debugging-port=${PORT}"`, then `kill -9` on
every pid `lsof` reported for the port. It was written for a real failure (a degraded Chrome that
survived a single best-effort `pkill` kept serving 9333 and the engine reconnected to the same
broken instance — validated live 2026-07-01), but it identified its target by *port*, which is
the one thing the browser we want to keep and the browser we want gone have in common. That
inverts the invariant everything else in the system holds: the engine is "a passive observer",
and the desktop `chrome_manager` only ever ADOPTS a running Chrome. On the box this was found on,
the process on 9333 was a 1.4 GB profile with a live Instagram session.
**Fix:** `free_port()` is gone; `ensure_port_free()` replaces it and **signals nothing it cannot
prove it launched**. The script records its child's pid at launch, and reclaims the port only
when there is exactly one holder, its pid matches that record, AND its cmdline still carries our
exact `--remote-debugging-port` + `--user-data-dir` signature — pid files are stale-able and
cmdlines are forgeable by hand, so both are required. Even then it sends **SIGTERM** (Chrome's
graceful shutdown flushes cookie and session state), never `-9`. Every other case prints the
holder — pid, user, start time, command — plus what the CDP probe just said about it, and exits 1
for the human to decide.
**How to avoid/detect:** "Free the port" is never a safe primitive when the thing on the port is
irreplaceable state; the safe primitive is "prove it is mine, else explain and stop". Exercise it
with a scratch port and a process you launched yourself, never against the live 9333 — the old
code kills a fake holder on 9444 in one shot, the new code refuses with a description of it, and
that pair is the whole regression test. A lost pid file (TMPDIR is reaped) only costs the
automatic reclaim, i.e. it fails toward refusing, which is the correct direction.

### A1. UI permanently "disconnected", all buttons dead — Tauri 2 bridge not wired
**Symptom:** The AIZU Worker window renders, but the badge is stuck on `disconnected`, WORKER
shows `—`, CHROME shows `unknown` (yellow), and no button (Pause/Resume/Stop/Restart/focus)
does anything. The Python backend (sidecar, control surface, register loop) is 100% healthy.
**Root cause:** TWO independent Tauri-2 wiring omissions, BOTH required:
1. `withGlobalTauri` was not set in `tauri.conf.json` → Tauri 2 does **not** inject
   `window.__TAURI__`, so `ui/main.js`'s `tauri.core.invoke` / `tauri.event.listen` silently
   fell back to no-ops (the JS defensively resolves them and never throws).
2. There was **no `capabilities/` file** (generated `capabilities.json` = `{}`). In Tauri 2,
   `event.listen` internally calls the core `plugin:event|listen` command, which is DENIED
   without a capability granting `core:event`. So the status listener never subscribed and the
   poller's (working) `emit`s landed nowhere → the staleness watchdog showed "disconnected".
**Fix:** `"app": { "withGlobalTauri": true }` in `tauri.conf.json` + create
`src-tauri/capabilities/default.json` with `{ "windows": ["main"], "permissions":
["core:default", "core:event:default"] }`. (App commands via `generate_handler!` do NOT need
ACL in Tauri 2 — only core/plugin commands do.)
**How to avoid/detect:** A standard `tauri init` scaffold ships `capabilities/default.json` and
you enable `withGlobalTauri` when using the global API without a bundler — a hand-written
scaffold can miss both. Fast triage: if the whole UI is inert, check (a) the built binary has
`__TAURI_INTERNALS__` strings (`strings <binary> | grep __TAURI_INTERNALS__`), (b) the built
`capabilities.json` is not `{}` (`cat src-tauri/target/*/build/*/out/capabilities.json`). To
prove where the break is, add a temporary `eprintln!` in the Rust poller — if it logs
`emitting` but the UI stays stale, the break is the frontend permission/global, not the poll.

### A13. The unified profile base orphaned every existing desktop box's logins
**Symptom:** After the per-brand profile split, a desktop box that had been running fine looks
signed-out. Its warmed sessions are intact on disk, at a path nothing opens any more.
**Root cause:** The shell defaulted to `<app data>/chrome-profile` while `warm_chrome.sh` used
`~/.aizu-cft-profile` and the worker preflight used a third spelling (`AIZU_CHROME_PROFILE_DIR` /
`~/.aizu-chrome-profile`) — so the preflight's profile row watched a directory nothing in the repo
ever warmed. Unifying on ONE name and ONE default (`AIZU_CHROME_PROFILE`, `~/.aizu-cft-profile`)
fixed that, and is load-bearing: `sidecar_supervisor.rs` exports the base to the child, so the
shell, its sidecar and the launcher all open the same directory. But changing a DEFAULT moves
every box that never set the value explicitly.
**Fix:** Keep the unified default — the split was the real bug — and SAY SO. `config.rs`
`former_default_chrome_profile_base()` remembers the old location purely to report it, and
`chrome_manager::former_default_profile_notice` tells the operator their sign-ins are there, that
nothing has been moved, and how to carry them over. It names BOTH brand destinations and picks
neither: the brand is as unknowable here as for any legacy profile. Boxes with an explicit
`chrome_profile_dir` in `config.toml` were never affected — `serde(alias)` still reads the old key.
**How to avoid/detect:** Changing the DEFAULT of a path is a silent migration for everyone who took
it. A moved default needs the same treatment as a deleted file: leave the old one alone, and tell
the operator where it went. Detect with: does anything still read the old location, even to report?

### A12b. Three near-misses in the per-brand split, all found by review, none by tests
- **The remedy text guessed the brand.** The Python legacy notice pre-typed the Chrome-for-Testing
  destination into its `mv` command — so the app DID guess, and published the guess to the fleet
  console as a preflight remedy. An operator whose profile was warmed by system Chrome would have
  pasted the exact wrong-brand open the redesign exists to make unreachable. It also moved the whole
  base rather than `Default/`, burying the per-brand dirs the launcher had just created.
- **The three languages disagreed on override PRECEDENCE.** bash read `AIZU_CHROME_BINARY` first;
  Python and Rust read `CHROME_BIN` first. With both set, the launcher warmed
  `<base>/chrome-for-testing` while the worker opened `<base>/chrome`. Since the binary now chooses
  the DIRECTORY, a precedence disagreement is a split-brain about which logins exist. Standardised
  on the namespaced name winning: `CHROME_BIN` is generic and other tooling sets it.
- **Distro Chromium was filed as `chrome`.** Debian/Ubuntu `chromium` seals cookies under its own
  keyring entry, and `/usr/bin/chromium` sits on the Linux fallback list right next to
  `/usr/bin/google-chrome` — so a two-token rule handed them one directory and wiped whichever
  warmed it first. It is now a third brand.
**How to avoid/detect:** A "shared contract" across three languages is only shared if something
CHECKS it. Feed all three implementations the same fixture list and compare — 16 real path shapes
(read off Playwright's own `EXECUTABLE_PATHS` table, not invented) caught all of this in seconds.

### A6. Packaged app can never find a browser — frozen Playwright looks INSIDE the bundle
**Symptom:** On a packaged box the setup wizard's Chrome step cannot go green. The app launches
system Chrome, its own probe reports success, and the sidecar preflight then reports
`cdp_attachable: fail` with a remedy ("quit that Chrome completely and relaunch") that reproduces
the identical browser forever. Reproduces even on a dev machine whose
`~/Library/Caches/ms-playwright` is fully populated.
**Root cause:** THREE layers, each individually reasonable:
1. `chrome_manager.rs` resolved Chrome-for-Testing by shelling to a **dev-tree venv python**
   (`engine/.venv/bin/python`, resolved RELATIVE TO CWD). A packaged install has no such venv, and
   a Finder/LaunchAgent launch has cwd `/`. `CHROME_BIN` is no escape hatch either — a GUI launch
   inherits no shell profile and `config.toml` has no chrome-binary field.
2. Fall-through hit **system Chrome**, and Chrome 149+ answers `/json/version` with HTTP 200 while
   REJECTING `connect_over_cdp` — a browser the engine can never attach to. (True of 149; **no
   longer true on 151** — see the correction in D3. The layer-1 and layer-3 breakages are
   unaffected, and CfT-first is still right, for the pinning reason, not the attach reason.)
3. The obvious fix (ask the frozen sidecar, which carries Playwright inside it) is ALSO broken:
   `playwright/_impl/_transport.py` does `env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")` whenever
   `sys.frozen` is true, and the Node driver reads `"0"` as *browsers live inside the package* →
   `<bundle>/_internal/playwright/driver/package/.local-browsers/`. `sidecar.spec` copies the
   driver tree but **no browsers**, so that directory does not exist at all.
**Fix:** `aizu.worker.chrome_path` (a CLI the frozen binary dispatches via `run_sidecar.py`, same
argv trick as A2) prints Playwright's Chrome-for-Testing path; `chrome_manager.rs` asks the SIDECAR
first, dev venv second, system Chrome last. `run_sidecar.py` pins `PLAYWRIGHT_BROWSERS_PATH` (via
`setdefault`, so ops can still override) to the per-user cache the dev tree already uses, which
covers the probe AND the real sidecar since both enter through that shim. `--install` downloads the
browser with the bundled driver — no Python needed on the box — behind a wizard button.
**How to avoid/detect:** `cd / && <frozen-binary> -m aizu.worker.chrome_path` must print a path and
exit 0. Running it from the repo proves nothing: the dev venv is reachable there and masks the bug.

### A7. Two ways this fix silently un-fixed itself (both caught in review, both now guarded)
- **The degraded-Chrome note was erased on the next click.** `ensure_running`'s attach branches
  returned `degradation: None`. On a packaged box `cdp_probe` can ONLY return `Unknown` (no
  interpreter to probe with), so the second "Launch warmed Chrome" — or the next app start — wiped
  the footer note, painted the step green, and restored the exact green-UI/red-preflight dead end.
  The fact is now STICKY (`degraded_launch`), retired only by a real `Attached` probe or a healthy
  launch. **"Could not verify" is not "fine"** — see `attach_branch_degradation`.
- **A half-installed browser passed an existence check.** A killed download leaves ~322 MB and a
  52 KB launcher on disk; Playwright knows it is incomplete (it re-downloads with "Removing unused
  browser at …") but `os.path.exists` does not. `resolve()` now requires Playwright's own
  `INSTALLATION_COMPLETE` marker in the revision dir — and only rejects when it can positively
  identify that dir, so an unrecognised layout is accepted rather than falsely failed. The first
  cut matched `<anything>-<digits>` and declared a healthy browser broken because `pytest-188` was
  an ancestor of the test fixture.

### A2. Frozen job child killed instantly (rc=-9), job stuck `queued` forever
**Symptom:** A leased job crash-loops: `Job <id> crashed (rc=-9): job child crashed`,
`ChildCrashed`, nack, repeat until `failed` (attempts hit max). A re-Run then reports
"already running". Per-job log is 0 bytes. Only happens in the packaged/frozen app, not from
source.
**Root cause:** The Phase-6 supervisor spawns each job as
`[sys.executable, "-m", "aizu.worker.job_child", "--spec-file", X]`. Under a real Python
interpreter `-m` runs the module. But in a **PyInstaller frozen binary `sys.executable` is the
worker binary itself** and the bootloader IGNORES `-m` — so the "child" booted a SECOND sidecar
(which competes on the control-surface port and is reaped/killed → SIGKILL = rc=-9).
**Fix:** The frozen entry shim (`desktop/pyinstaller/run_sidecar.py`) now inspects argv: if it
starts with `-m aizu.worker.job_child`, it calls `job_child.main(argv[2:])` instead of the
sidecar. Source mode is unaffected (real `python -m`). Requires a **sidecar rebuild**.
**How to avoid/detect:** Any `subprocess([sys.executable, "-m", ...])` or `multiprocessing`
"spawn" is a landmine under PyInstaller — the frozen exe is not a Python. Route all self-re-exec
through an argv dispatcher in the frozen entry point, and TEST the packaged binary, not just
source (`<binary> -m the.module --help` should behave like the module, not boot the app).

### A3. `tauri build` alone produces a broken app (missing sidecar; broken codesign)
**Symptom:** After a bare `tauri build`, the app can't spawn the worker
(`sidecar spawn failed: aizu-worker: No such file or directory`); or `codesign --verify`
fails with "a sealed resource is missing or invalid".
**Root cause:** The PyInstaller onedir sidecar is embedded MANUALLY (Tauri's `bundle.resources`
can't ship the onedir tree — `_internal/…` Mach-O → "Not a directory"). `tauri build` doesn't do
that copy. Separately, LAUNCHING the app writes runtime files into the bundle and breaks the
code seal.
**Fix:** Always build via `desktop/scripts/build_macos.sh` (pyinstaller → tauri build → copy
sidecar into `Contents/Resources/sidecar/aizu-worker/` → `codesign --force --deep --sign -`
→ install to `/Applications`). If you launched the app then need a valid signature, **re-sign**
before verifying.
**How to avoid/detect:** Never ship a bare `tauri build` output. `codesign --verify --deep
--strict "<app>"` before distributing; confirm `Contents/Resources/sidecar/aizu-worker/
aizu-worker` exists.

### A4. Frozen sidecar: "no venv python found — cannot CDP-probe" (benign)
**Symptom:** Log line `[chrome] no venv python found — cannot CDP-probe`.
**Root cause:** The desktop Chrome-manager's CDP probe helper shells out to a venv Python that
doesn't exist inside a frozen bundle. It degrades gracefully (Chrome still attaches by other
means); not fatal.
**How to avoid/detect:** Low priority. If the probe is ever made load-bearing, port it to an
in-process check rather than a `python` subprocess (see A2 — no interpreter in a freeze).

### A5. App launch-crash: opens no window
**Symptom:** App "runs" (process exists) but shows no window and exits.
**Root cause (historical):** A `plugins.autostart` config block in `tauri.conf.json` made the
autostart plugin init fail ("invalid type: map, expected unit"). Autostart is configured in
Rust, not conf.
**Fix:** Removed the block; hardened first-run so `main` setup is non-fatal (window always opens;
Chrome+sidecar only start when `dispatch_base_url` is set).
**How to avoid/detect:** Diagnose a silent GUI exit by running
`".../Contents/MacOS/aizu-worker"` directly in a terminal to see the hidden stderr.

---

## B. Distributed workers — fleet dispatch, capabilities, execution

### B1. "run not dispatched: no capable worker" (fleet backend)
**Symptom:** With execution backend = distributed and a worker visibly ONLINE in the Fleet page,
a Run fails with `run not dispatched: no capable worker`.
**Root cause:** The fleet only enqueues to a worker whose declared **capability** matches the
campaign's `(platform, org)` (`count_capable_workers` / `_job_capability_covers`). The worker
had registered with **empty `[]` capabilities** — TWO reasons:
1. `WorkerConfig.from_env` had no capability source (`capabilities: () ` hardcoded), so any
   env/desktop-launched worker always declared nothing.
2. The desktop app didn't pass any capabilities to the sidecar.
**Fix:** `from_env` now parses `AIZU_WORKER_CAPABILITIES` (JSON `[[org,platform,handle],…]`)
or `AIZU_WORKER_PLATFORMS` (comma list / `all`) into pool-wide `[null, platform, null]`
caps. Desktop: `DesktopConfig.worker_platforms` (config.toml, default `"all"`) →
`sidecar_supervisor` sets `AIZU_WORKER_PLATFORMS`.
**How to avoid/detect:** Capabilities are OVERWRITTEN on every re-register, and a worker is only
dispatchable AFTER it declares them. Check `sqlite3 <db> "SELECT capabilities FROM workers"`. A
bare `from_env` worker still defaults to `()` — only the desktop path defaults to `all`.

### B2. Register rejected pool-wide capabilities (`accountHandle must be a non-empty string`)
**Symptom:** Worker log: `register failed: capability accountHandle must be a non-empty string`
(HTTP 400) once it started declaring `[null, "instagram", null]` capabilities.
**Root cause:** `_validate_worker_register` REQUIRED a non-empty `accountHandle`, contradicting
the lease matcher `_job_capability_covers`, which is explicitly built to treat `handle=None` as
**unpinned/pool-wide** and only requires an exact handle for an account-PINNED job. The fleet
dispatch even queries with `account_handle=None`. The validator was the outlier.
**Fix:** The validator now accepts `accountHandle = None` (blank → None); a non-null handle must
still be a non-empty string.
**How to avoid/detect:** When two layers share a data contract (here: register-validation vs
lease-matching), assert them against the SAME shape in tests. Added a
`test_register_accepts_pool_wide_capabilities_with_null_handle` regression.

### B3. "run not dispatched: already running" (double-Run dedup)
**Symptom:** A Run fails with `already running` even though nothing is visibly running.
**Root cause:** `enqueue_job_deduped` refuses if the campaign already has a job in
`queued|leased|running`. A job was stuck `queued` because it kept crash-looping (see A2) and
never reached a terminal state.
**Fix:** Resolving the underlying crash (A2) lets the job reach `done`/`failed` (terminal), which
no longer blocks a fresh Run.
**How to avoid/detect:** If "already running" appears, inspect the jobs table
(`SELECT id,status,attempts FROM jobs`). A job stuck `queued` with climbing `attempts` means the
worker is leasing-and-nacking it (look at WHY it nacks). Terminal states (`done`/`failed`/
`dead_lettered`) do not block dedup.

### B4. Job nacks `campaign_not_found` on a real, existing campaign
**Symptom:** Worker leases the job, runs the child, then nacks `campaign_not_found` for a
campaign that clearly exists in the panel.
**Root cause:** The worker resolves the campaign from ITS OWN DB (`AIZU_DB`, default =
app-data `com.aizu.workerdesktop/aizu.db`, empty), not the server's `engine/aizu.db`
where the brief lives. The job spec does NOT carry the campaign brief.
**Fix (local dev):** Set `db_path` in the worker's `config.toml` to the absolute
`engine/aizu.db` (the documented shared-DB local model).
~~**OPEN (real remote):** A worker on a different machine can't share the SQLite file — the brief
must be BAKED INTO THE JOB SPEC (like soul now is).~~ **CLOSED 2026-08-12 — the brief is now
baked.** `JobSpec.campaign_brief` (optional, defaults None) carries `campaign_to_brief(campaign)`;
`server._dispatch_run_to_fleet` bakes it at enqueue; `job_runner._resolve_campaign` PREFERS the
baked brief and falls back to the box-local `resolve_campaign`, so an already-queued job with no
baked brief still runs. A malformed baked brief raises `ValueError` from `campaign_from_brief`
exactly as a malformed DB brief does, flowing through the existing `campaign_malformed` nack with
no new mapping code. Size-capped at `MAX_CAMPAIGN_BRIEF_BYTES` (512 KiB vs a ~12.5 KB real brief),
enforced server-side pre-bake and again in `JobSpec.from_payload`.
**The trap that nearly shipped:** the first implementation passed 362 worker tests while being
completely INERT on the real wire — `store._job_row_to_lease` WHITELISTS which spec keys become
the lease response, and `campaignBrief` was never added to it. Every new test either constructed
`JobSpec` directly or read the raw `spec` DB column; none went through `POST /api/worker/lease`.
**Any new job-spec field must be added to `_job_row_to_lease`, and its test must hit the served
HTTP endpoint** — a test that would still pass with the fix reverted is worthless.
**How to avoid/detect:** Confirm the worker and server agree on the DB:
`ps -wwE -p <sidecar_pid> | tr ' ' '\n' | grep AIZU_DB` vs the server's `--db`.

### B5. Job nacks `soul_missing` (or would, on a remote box)
**Symptom:** Fleet-dispatched job can't find a soul and nacks; a worker with no local `soul.md`
can never run a fleet job.
**Root cause:** `_dispatch_run_to_fleet` baked only `{engine_mode, target_leads,
duration_minutes, run_id}` — NO `soul_text` (unlike the admin-enqueue path, which does). So the
worker had to rely on a box-local `soul.md`, which a remote box lacks.
**Fix:** The fleet dispatch now bakes `soul_text` from `load_soul(self.config_dir/"soul.md")`
into every job spec (BUILD-PLAN decision C5).
**How to avoid/detect:** Anything the engine needs at RUN time that isn't in the shared DB must
travel in the job spec for a remote worker (soul now does; the campaign brief still does not —
see B4).

### B6. Fleet run completes `done` but returns 0 leads
**Symptom:** Job reaches `done`, but `sessions=0, reels_seen=0, matches=0`.
**Root cause:** NOT a code bug — the managed Chrome on port 9333 was degraded:
`answers HTTP but rejects connect_over_cdp (stale/degraded Chrome or system Chrome 149+)`. The
engine couldn't attach, so it did no work but still completed cleanly. (The "system Chrome 149+"
half is history — see the D3 correction; a degraded/stale browser still does this.)
**Fix / prerequisite:** A run needs a **healthy warmed Chrome, logged into the target platform,
on the CDP port** (9333 live), plus provider creds (`OPENROUTER_API_KEY`) in the worker's env.
This is the standing "live exit gate." See [engine-live-run notes] and CDP gotchas below (D3).
**How to avoid/detect:** Before blaming the pipeline, verify CDP attach works:
a real `connect_over_cdp('http://127.0.0.1:9333')` must succeed — HTTP 200 on `/json/version`
is NOT sufficient.
**The "don't report a silent success" half is now DONE (2026-08-13)** — this entry's closing
recommendation, implemented by three independent pieces:
1. **Can't attach at all** → the worker probes CDP before spawning and returns
   `halt_reason='cdp_unreachable'` without spawning, so the job nacks with a real reason instead of
   hanging ~180s and crashing. Gated to `CDP_PLATFORMS`, since an API-only job (youtube/telegram/
   reddit) drives no browser and must still run on a Chrome-less box.
2. **Attaches, then wedges mid-run** → `HaltSession("cdp_call_wedged", kind="canary")` after
   `cfg.max_consecutive_wedged_calls` degraded calls (see D6). Without it, the restored deadline
   turned a frozen browser into a clean `completed` run with 0 leads — this entry's exact symptom,
   reintroduced by the fix for a different bug. Probed: `walk elapsed=23.59s reels=0 exception=None`.
3. **Operator visibility** → the run activity feed surfaces the fleet job's failure code, so a dead
   run reads "Failed on the fleet — the worker's Chrome could not be attached" rather than a blank
   "Finished on the fleet".
**Still open — the live exit gate itself:** nobody has yet driven a real `target_leads>=1` run to
leads on a warmed, logged-in Chrome on a worker box. That needs hardware and a warmed account, not
code.

### B7. Per-org platform credentials on a remote box — fetch, never bake
**Symptom:** A youtube/telegram/reddit fleet job on a genuinely remote worker resolves its
campaign fine (B4 is fixed) and then fails with `YouTube live run needs YOUTUBE_API_KEY in the
environment (.env)`. That message matches neither `cli._is_auth_error` nor sidecar's
`_POISON_HALTS`, so it nacks as a plain transient failure and retries identically until it
dead-letters.
**Root cause:** `cli._resolve_platform_credentials` → `store.org_for_campaign` reads local-only
tables (`campaign_meta`/`campaign_briefs`), empty on a remote box, so `org_id` is None and the
org's connected integration secret is never found. Baking the brief does not help — the secret
does not live in the brief.
**Fix (2026-08-12):** `POST /api/worker/jobs/{id}/credential` — a 4th job-scoped worker action
that decrypts `store.get_integration_secret` **fresh per request** and returns it in the response
body only. The sidecar fetches at job start (only for `PER_ORG_CREDENTIAL_PLATFORMS`; CDP
platforms drive a warmed browser and never fetch) and threads it onto the JobSpec via
`dataclasses.replace`, so the existing 0600 spec-file hand-off carries it to the killable child
unchanged. A fetch failure nacks with a distinct reason instead of the confusing downstream
error above.
**The rejected design — do not reintroduce it:** baking the decrypted secret into the job spec
at enqueue. `store.ack_job` updates only status/result/session_id/leased_by/lease_expires_at —
**it never scrubs `spec`** — and there is no DELETE or prune of the `jobs` table anywhere in
`store.py`. A baked secret therefore persists **forever, in plaintext**, in the `jobs.spec` TEXT
column, undoing the Fernet-at-rest protection in `core/secrets.py`.
**Authorization:** `store.get_leased_job_for_worker(job_id, worker_id)` —
`WHERE id=? AND leased_by=? AND status IN ('leased','running')`. Org and platform come from the
JOB ROW, never from the request. Deliberately does NOT touch `_job_capability_covers`: the
lease-holder check is strictly tighter than capability matching, so pool-wide `[null, platform,
null]` capabilities keep working (see B1/B2 — breaking those regresses every deployment).

### B8. Cross-tenant credential reach via the shared bootstrap token (MECHANISM SHIPPED — cutover still owed)
**Symptom:** None observed — a design exposure surfaced by security review, not a live bug.
**Root cause:** `AIZU_WORKER_BOOTSTRAP_TOKEN` is ONE shared secret for the whole fleet, and a
worker self-declares its capabilities at register. `_job_capability_covers` treats `cap_org=None`
as matching ANY org (deliberately — one managed box serves ~10 companies, PRD scale). So any box
holding the bootstrap token can register pool-wide, legitimately lease another org's job, become
its `leased_by`, and therefore pass B7's lease-holder check and receive that org's decrypted
credential.
**Assessment:** the gap is **pre-existing** — it long predates B7, which raises the stakes
(campaign metadata → a live credential, a full logged-in session for Telegram) without authoring
it. The bootstrap token IS the fleet's trust boundary today: anyone holding it could already
lease, run, and sync leads for any org.
**Fix (shipped in `Launch`, schema v22 + the v22.1 clamp-on-re-register follow-up):** per-worker,
single-use, admin-minted enrolment tokens — `worker_enrolment_tokens` (`store.py:639`), minted from
the panel (`MintEnrolmentTokenModal.tsx`), redeemed on first register. A redeemed token's scope is
stamped on the worker row as `workers.enrolment_scope_kind` (`store.py:621`) and CLAMPS what gets
written on that register **and on every later re-register** (`server.py:3490-3497`): `'org'` forces
`org_id` and every capability's `cap_org` to the token's org; `'pool'` is the deliberate multi-org
grant and leaves capabilities unclamped. The `cap_org=None` trap was correctly avoided — pool-wide
registration still works, so the shipped desktop app (B1/B2) is unaffected.
**Why this entry is not CLOSED — three things the shipped mechanism does not do by itself:**
1. **A legacy-enrolled worker is never clamped, ever.** The clamp is gated on
   `if enrolment_scope is not None` (`server.py:3490`), and a worker that first-registered via the
   shared bootstrap token has `enrolment_scope_kind IS NULL` forever. It keeps self-declaring its
   own `org_id` + pool-wide capabilities on every re-register, on a bearer token with a 1-year TTL
   that each re-register refreshes. `test_worker_server.py:396-421` pins exactly this. **So the
   exposure persists on every already-provisioned box until it is re-enrolled**, no matter what the
   feature flag says.
2. **Re-enrolling a box is not just "hand it the new token".** The sidecar presents a bootstrap /
   enrolment token ONLY when it holds no stored worker token (`sidecar.py:388-397`) — otherwise it
   takes the re-register branch and never calls redemption. The runbook must be: stop the sidecar →
   **delete the persisted token** (`<AIZU_WORKER_STATE>/worker-token.enc`, default
   `.worker-state/worker-token.enc`, or the `aizu-worker-token` keyring entry —
   `token_backends.py:33,37,67`) → set the enrolment token → restart. Reusing the same machineId is
   fine; `register_worker` UPSERTs the same row (`store.py:3199-3218`).
3. **The completion criterion is a DB query, not a log line.** `SELECT id, host FROM workers WHERE
   revoked_at IS NULL AND enrolment_scope_kind IS NULL` must be EMPTY before flipping
   `AIZU_WORKER_LEGACY_BOOTSTRAP_ENABLED=0`. Watching for the deprecation warning
   (`server.py:3470-3474`) is insufficient — a legacy box that only ever re-registers on its own
   bearer token never hits that branch and never logs it.
**Also note:** scope choice is what decides whether the cutover delivers real isolation. Minting
`'pool'` out of habit leaves the exposure materially unchanged — only now revocable and
attributable. And the flag parse (`server.py:3463-3466`) treats an EMPTY value as disabled, so
`AIZU_WORKER_LEGACY_BOOTSTRAP_ENABLED=` in a `.env` silently fails closed and can lock out an
un-enrolled box. That direction is safe, but surprising — set `1` or `0` explicitly.

### B10. Revoking a worker permanently bricks the box (no 401 recovery) — FIXED 2026-08-13
**Symptom:** A worker whose row is revoked server-side (or whose `workers` row vanished in a DB
reset — see C3) goes dead and never comes back, even after the operator re-issues credentials.
**Root cause:** There is NO 401 handling anywhere in the worker sidecar — `grep '401\|revoked'`
over `worker/sidecar.py` and `worker/lease_client.py` returns nothing. The box keeps presenting its
persisted bearer token forever, gets rejected forever, and never clears the token or falls back to
enrolment. This directly undercuts B8's "revocable" claim: revocation stops the box from working
but leaves no way to bring it back short of a manual token-file deletion (C3's workaround).
**Fix (2026-08-13, no schema change — SCHEMA_VERSION stays 22):** every worker-plane 401 funnels
into one idempotent `Sidecar._on_auth_revoked`, reached from all seven bearer-authenticated calls
(register, presence, lease, job heartbeat, ack, nack, credential). It clears the token through
`TokenStore` (so the keyring backend is cleared too, not an unlink of `worker-token.enc`), logs at
CRITICAL naming the operator action, stops leasing, and parks the process with the control surface
still serving so a supervisor/desktop shell does not crash-loop it. **No auto-re-register.**
Operators see `controls.reenrolmentRequired` on the status surface and a red strip + "revoked" pill
in the desktop app.
**Detection is deliberately narrow, because the failure mode of getting this wrong is worse than the
bug.** Three CRITICAL defects were caught in review and fixed; all three are ways a *transient*
condition could have bricked a healthy box, or ways revocation could have failed to stick:
1. **A 401 must come from the dispatch, not from anything in front of it.** Keying on the status
   code alone meant a reverse proxy's or captive portal's HTML-bodied 401 destroyed a valid token.
   `Result.is_unauthorized` now requires `status == 401` AND a body that really parsed to an
   envelope with a boolean `ok` that is false.
2. **One 401 is not proof.** The token is retired only after `_UNAUTHORIZED_CONFIRM_LIMIT` (3)
   CONSECUTIVE 401s across any route; any non-401 outcome resets the counter. Server-side companion:
   `_current_worker` now distinguishes "store raised" from "no such row" and answers **503**, not
   401, during a DB blip — a valid token was being told it was revoked.
3. **Revocation did not survive a restart, and the token clear is what re-opened the hole.** With no
   token, a restarted box takes the FIRST-register branch, where the shared bootstrap secret UPSERTs
   `revoked_at = NULL` and silently undoes the revocation. Closed server-side: `Store.is_worker_revoked`
   (`store.py:3495`), and the legacy-bootstrap branch of register now refuses a revoked worker with
   `401 worker is revoked; re-enrol it with a per-worker enrolment token`. An unknown machine still
   enrols normally, so C3's DB-reset recovery is preserved, and an admin-minted enrolment token
   still un-revokes. **This makes revocation durable without waiting for the B8 cutover.**
**A 401 mid-job does not kill the running child by itself** — the pre-existing three-strike heartbeat
rule decides its fate. A worker that cannot heartbeat has lost its lease claim and the server may
re-dispatch, so continuing blind risks the same job running on two boxes; and letting it "finish and
report" is hollow because the ack would 401 too.
**Also fixed:** the parked process no longer keeps a presence thread authenticating forever, and a
401 is compared against the bearer that was actually presented — if another process rotated the
token in a shared state dir, the stale process ADOPTS the new token instead of halting.

### B9. Spend cap silently resets per worker box — FIXED 2026-08-13
**Symptom:** A campaign with a `--spend-cap` can spend up to the full cap again each time its
job lands on a different worker box.
**Root cause:** `core/router.py` checks `self.store.total_spend(campaign_id) >= self.spend_cap_usd`
against the **local** `spend_log` table. A fleet job is unpinned (pool-wide capability), so box B
has no rows for a campaign previously run on box A or on the server, sees $0, and permits a full
fresh budget. The cap is effectively per-box, not per-campaign.
**Fix:** prior total resolved at LEASE time + delta rolled up on ack AND nack, riding the same
channel the lead sync-back already proved. No schema change (SCHEMA_VERSION stays 22).
- `priorSpendUsd` is resolved live inside `lease_one_job`'s `_tx_immediate` and emitted through
  `_job_row_to_lease`'s whitelist (`store.py:1152`) — the B4 trap, so its regression test hits the
  served `POST /api/worker/lease`, not the store layer (`test_jobs_server.py:216`).
- The box computes `effective_cap = local + max(0, cap - max(prior, local))`, so a campaign's
  budget is honoured across boxes instead of resetting.
- The sidecar reports the delta on BOTH ack and nack, so every graceful failure banks its spend.
**Four traps this walked into, all found by adversarial review with executed probes — do not
reintroduce them:**
1. **Same-DB double count.** `AIZU_DB` defaults to the same `aizu.db` filename the bridge uses, and
   the repo's own integration test wires worker and server to ONE file. In that topology the child
   already wrote the rows into the cloud `spend_log`, so the rollup INSERTed them a second time
   (`spend_log` has no unique key — it is not idempotent like `_upsert_match_row`). Guarded by
   `Store.database_id()`: the sidecar sends `dbId`, and the sync SKIPS entirely when it matches the
   server's own. Probed: $3 spent read back as $6 before the guard.
2. **A cloud-side cap does not exist.** `AIZU_SPEND_CAP` is a WORKER-plane variable — set on the
   boxes, absent on the bridge. The first cut had the bridge fall back to a hard-coded default and
   permanently refuse to dispatch an over-budget campaign nobody had capped. The enqueue-time skip
   now fails OPEN: `_fleet_spend_cap_usd()` returns None when the bridge has no explicit cap, and
   the skip only fires when a cap is genuinely known. **Authoritative enforcement lives on the box**,
   which is the only place that knows its own cap.
3. **A requeue never traverses dispatch.** `nack_job` puts the row straight back to `queued`, so the
   enqueue-time skip cannot see it and an over-cap job would re-lease and burn a full duration of
   degraded local stand-ins. The box now refuses BEFORE the CDP probe and before the spawn, with
   `halt_reason='spend_cap'` in `_POISON_HALTS` so it dead-letters instead of burning all 5 attempts.
4. **A reclaimed attempt lost its dollars.** The cursor was re-taken per attempt, so a SIGKILLed
   box's spend fell between the old mark and the retry's. The mark is now parked at
   `<state_dir>/run-<runId>.spend-cursor`, resumed on retry, and retired only on an ACCEPTED report.
**How to avoid/detect:** `total_spend` is a LIFETIME sum, not per-run — anything gating on it must
say which. And when a fix needs a value the control plane cannot know, fail OPEN and enforce where
the value actually lives; a guessed default turns a safety rail into an outage.

---

## C. Local dev environment & deployment wiring

### C0. Both default LLM model ids are DEAD on OpenRouter — masked locally by `engine/.env`
**Symptom:** Nothing, on any box that sets `OPENROUTER_TEXT_MODEL` /
`OPENROUTER_VISION_MODEL` — which `engine/.env` does, so local dev and the current fleet
are fine. On any box WITHOUT them (a fresh clone that copied `.env.example`, a new worker,
CI, a colleague's laptop), every LLM call sends a model id OpenRouter does not have.
**Root cause:** `openrouter/owl-alpha` and `nex-agi/nex-n2-pro:free` are both **absent**
from the live model listing. Verified twice on 2026-08-21 against
`GET https://openrouter.ai/api/v1/models` (200 OK, 419 models) — no id anywhere in the
listing contains "owl"; `nex-agi/nex-n2-pro` exists but the `:free` variant does not.
They were presumably live when written; model ids are not stable.

Five sites carry the literals, and the precedence chain means fixing one is not enough:
| Site | Note |
|---|---|
| `engine/aizu/core/router.py:336-337` | `_DEFAULT_TEXT_MODEL` / `_DEFAULT_VISION_MODEL` — the LAST resort in the chain |
| ~~`engine/aizu/cli.py:1013,1015`~~ | ~~passed as EXPLICIT args (`cli.py:134-135`), which **outrank** the router chain~~ — **done 2026-08-21**: both are `default=None`; this also restored `AIZU_TEXT_MODEL`, which the explicit pass had made unreachable on the CLI path |
| `engine/aizu/worker/config.py:299-300, 361-363, 416-417` | same shadowing for the whole fleet via `config.py:455-456` |
| ~~`engine/aizu/engines/warming/tg_relevance.py:32`~~ | ~~`DEFAULT_GATE_MODEL`, a 4th copy~~ — **done 2026-08-21**: replaced with `_default_gate_model()`, which resolves through the router's chain instead of holding a literal |
| ~~`engine/.env.example:20-21`~~ | ~~a fresh install copies the dead id~~ — **done 2026-08-21**: points at ids verified live that day, with prices and a re-check warning |

`router.py:366-370` resolves `text_model or AIZU_TEXT_MODEL or OPENROUTER_TEXT_MODEL or
_DEFAULT_*`, and an explicit argument always wins — so the CLI and worker hardcodes make
the router default unreachable on the two paths that matter, and they also make
`AIZU_TEXT_MODEL` (the local-Ollama knob) unreachable on the CLI path entirely.

**Fix:** Do NOT swap five literals. Delete the CLI and worker hardcodes (`default=None`)
so everything falls through to the router's single resolution chain, leaving exactly one
place a dead id can live. Then update that one place. Live candidates with exact per-1M
pricing from the same 2026-08-21 fetch: `qwen/qwen3-vl-32b-instruct` $0.104/$0.416
(text+image, 131k, response_format yes); `google/gemini-3.5-flash-lite` $0.300/$2.500;
`nex-agi/nex-n2-mini` $0.025/$0.100; `nex-agi/nex-n2-pro` $0.250/$1.000 (⚠️ response_format
NOT supported). Free and present: `dots-studio/dots-3-note-preview:free`,
`google/gemma-4-31b-it:free`, `nvidia/nemotron-nano-12b-v2-vl:free` (⚠️ last one:
response_format NOT supported).

⚠️ Before picking a VISION id: `router.classify_image` (`:834-880`) appears to have no
response_format param-rejection handling — the `_json_mode = False` latch-off and stripped
retry exist only in `classify_text` (`:707-725`) and `generate_json` (`:919-930`). A vision
model that rejects the param may fail hard rather than degrade. NOT independently verified;
check before choosing an id marked "response_format NOT supported".

**How to avoid / detect:** A model id is a third-party fact with a shelf life, exactly like
the API facts the Campaign Lab sheets date and mark ⚠️. There is no preflight row for it —
`worker/preflight.py` makes no outbound call at all (`check_llm_backend` is a pure env
predicate), so "the key is set" is checked and "the model exists" is not. An id-resolution
preflight row would be the first outbound probe in that module; that is a design decision,
not an oversight to fix casually.
**Status 2026-08-21:** three of the five sites are done (struck through above).
**Two remain, both owned by a concurrent session at the time of writing:**
`engine/aizu/core/router.py:336-337` and
`engine/aizu/worker/config.py:299-300, 361-363, 416-417`. Until the router
default is changed, a box with no env override still sends a dead id — the work
above removed the DUPLICATES, not the defect. The worker one matters most:
`config.py:455-456` passes the ids as explicit args, so changing the router
default alone will not help a worker box.

**Found:** 2026-08-21, by a Campaign Lab research fan-out that was looking at
something else entirely.


### C1. Worker shows "disconnected" / first-register 401 — bootstrap token mismatch
**Symptom:** Worker can't first-register (401), or shows disconnected in the Fleet page.
**Root cause:** The server needs the SAME `AIZU_WORKER_BOOTSTRAP_TOKEN` the worker presents
(from `~/Library/Application Support/com.aizu.workerdesktop/dispatch-token.secret`, written by
the app's dev menu).
**Fix:** Launch the server with `engine/scripts/dev_panel.sh` — it sources the token from that
same secret file (ONE source of truth). Bare `dev_panel.py` does not set it.
**How to avoid/detect:** Dispatch and panel are the SAME server (`server.py` serves
`/api/worker/*` and `/api/admin/*`). Point the worker at the panel port (8765). Worker + server
share `engine/aizu.db` in local dev.

### C2. "unknown endpoint" 404 on a route that exists
**Symptom:** `/api/worker/register` (or any newer route) returns `{"ok":false,"error":"unknown
endpoint"}` (HTTP 404) even though the code has it.
**Root cause:** A STALE server process on that port, predating the route (e.g. an old
`aizu.cli panel` on 8799, or a no-reload dev bridge). Requests hit the old code.
**How to avoid/detect:** `lsof -nP -iTCP:<port> -sTCP:LISTEN` and `ps -o lstart,command -p <pid>`
to spot a stale server; kill it and relaunch. Confirm exactly ONE listener on the port.

### C3. Stale worker token in Keychain after DB wipe
**Symptom:** Register says "invalid or revoked token" and the server has no bootstrap token.
**Root cause:** The worker's persisted token (Keychain `aizu-worker-token` or the encrypted
`worker-token.enc` file) survived a DB reset that dropped its `workers` row.
**Fix:** Clear it so the worker first-registers via bootstrap:
`security delete-generic-password -s aizu-worker-token` (keychain backend) or delete the
`worker-token.enc` in the worker state dir.
**Note:** `auto` token backend resolves to **file** (keyring is opt-in via
`AIZU_TOKEN_BACKEND=keyring`) — an unattended box must never risk a blocking Keychain prompt.

### C4. Direct SQL patches to shared registries are blocked / fragile
**Symptom:** A quick `UPDATE workers SET capabilities=...` to unblock is denied by the safety
classifier, and would be wiped on the next re-register anyway.
**Takeaway:** Fix the SOURCE (config/env/code path that produces the value), not the DB row.
Registry columns like `workers.capabilities` are UPSERT-overwritten on every re-register.

### C5. Entire panel Vitest suite red on Node ≥25 — vitest never copies jsdom's Storage
**Symptom:** 100% of the admin-panel suite fails — `Test Files 51 failed (51) / Tests 470 failed
(470)` — every failure identical: `TypeError: Cannot read properties of undefined (reading 'clear')`
from the global `afterEach` in `admin-panel/src/test/setup.ts:18`. Plus an `ExperimentalWarning:
localStorage is not available` on stderr. **CI stayed green the whole time.**
**Root cause:** vitest 3.2.6's `getWindowKeys` skips any jsdom-window key that already exists on the
Node global unless it is in a hardcoded allowlist — `if (k in global) return keysArray.includes(k)`
(`node_modules/vitest/dist/chunks/index.CmSc2RE5.js:250`). Neither `localStorage` nor
`sessionStorage` is in that list (it carries the `Storage` *constructor*, not the instances). Node
≥25 unflagged webstorage, so `'localStorage' in globalThis` is now true and jsdom's Storage is never
copied — `localStorage` stays Node's lazy accessor, which is `undefined` without
`--localstorage-file`. The jsdom env calls `populateGlobal` with no `additionalKeys`, so there is no
config knob. jsdom is a BYSTANDER (its `window.localStorage` works fine) and bumping vitest does not
help — `main` carries the identical filter. Upstream: vitest-dev/vitest#8757.
**Quieter companion bug:** pre-fix on Node ≥25, `sessionStorage` silently resolved to Node's
*process-global* Storage — a working-but-wrong object (`instanceof globalThis.Storage === false`)
that leaked across test files and was never cleared by the `afterEach`.
**Fix:** shim at the top of `admin-panel/src/test/setup.ts` — read the untouched WebIDL accessor
`Object.getOwnPropertyDescriptor(Document.prototype, 'defaultView')` (vitest shadows
`document.defaultView` with an own property pointing at the global) and re-point both storage globals
at the real jsdom window. A no-op on Node ≤24, where they are already the same object. Two things to
preserve if you edit it: **never bind the getter to a variable** (`@typescript-eslint/unbound-method`
errors under `strictTypeChecked` and reds `npm run lint` — which `tsc` does NOT catch), and never
swap in a hand-rolled polyfill (`src/shared/lib/storage.test.ts` spies on
`Storage.prototype.setItem/getItem`, which needs a genuine jsdom Storage). Delete the shim once
vitest adds both names to OTHER_KEYS.
**How to avoid/detect:** the real lesson is a **two-major local/CI Node drift with ZERO CI signal** —
CI pins Node 24 (`.github/workflows/ci-cd.yml:50`) while local dev ran 26, so a total local wipeout
was invisible to CI. A root `.nvmrc` (`24`) now pins nvm users to the CI Node.
`src/shared/lib/storage.test.ts` carries a named `describe('test environment')` guard asserting both
storages are `instanceof Storage` — `toBeInstanceOf` is the load-bearing part, since it also catches
the silent-substitution variant, not just the `undefined` one.

---

## D. General development findings (cross-cutting)

### D1. Parsing untrusted / model-generated text — always behind a tolerant boundary
Any parse of text you don't control (LLM output, third-party API, DB row) must: request the
provider's JSON/structured mode → strip fences → tolerant parse + repair → validate shape (not
just syntax) → return a typed `Result`, never let an exception escape. Never `as T`/unchecked
cast external data. The worker's `lease_client` already does this (a malformed dispatch reply
never crashes the loop) — mirror it for every new external boundary.

### D2. OpenRouter model churn
Free/alpha models disappear without notice (`openrouter/owl-alpha` 404'd → swapped to a
Nemotron free model; the worker default `text_model` still references owl-alpha in
`WorkerConfig` — override via `OPENROUTER_TEXT_MODEL`). Surface a dead-model error instead of
faking a result. Keep model IDs in env/config, not hardcoded in call sites.

### D3. Instagram/CDP live-run gotchas (port 9333)
Live runs attach to a LOCAL warmed Chrome via `connect_over_cdp('http://127.0.0.1:9333')` (NOT
9222). Gotchas: a "degraded" Chrome answers HTTP but rejects `connect_over_cdp` (system Chrome
149+ / already-attached / stale profile) → relaunch a clean warmed instance; the engine enforces
a daytime guard against the account timezone; harvest attaches Chrome before the daytime check;
warming is gated by a kill-switch. A dedicated `--user-data-dir` is required (Chrome refuses
`--remote-debugging-port` on the default profile).

**Correction — "system Chrome cannot be attached to" is stale as of Chrome 151 (measured
2026-08-18).** The 149 regression was real (Chrome 149 dropped the CDP browser-context-management
surface, so `connect_over_cdp` died right after the websocket with "Browser context management is
not supported"), and it is why CfT became the default. It is not current behaviour: **system
Google Chrome 151.0.7922.138 attaches**, **Chrome for Testing 151.0.7922.34 attaches**, and a
read-only `Target.getBrowserContexts` against the LIVE system Chrome returned
`{'browserContextIds': [], 'defaultBrowserContextId': '…'}` with no error. Chrome for Testing
remains the recommended default for a **different** reason than it was picked: it is the build the
installed Playwright ships with, so its protocol surface matches the client by construction and
cannot silently auto-update out from under us — which is exactly what 149 did. One build on one OS
is not a licence to switch the default to system Chrome. A rejected `connect_over_cdp` on 151 means
degraded/stale/already-attached, not "wrong brand", so the remedy is the same as ever: relaunch a
clean warmed instance — but see A10, you do not get to kill someone else's browser to do it, and
A9, you do not get to relaunch it with the other brand.

### D4. Schema migrations are additive + self-healing — mind the version namespace
New tables use `CREATE TABLE IF NOT EXISTS` in the SCHEMA block + `_add_column_if_missing`;
timestamps are REAL epoch. `SCHEMA_VERSION` is a shared counter — check the latest before
claiming a number (workers=v14, superadmin=v15, platform_settings=v16). A version collision
(the plan originally said v13, already taken by billing) silently breaks migration logic.

### D5. SQLite leasing has no `SELECT … FOR UPDATE SKIP LOCKED`
Concurrency-safe leasing uses `_tx_immediate` (`BEGIN IMMEDIATE` = write lock at statement one)
+ conditional `UPDATE … WHERE status='queued'` + `rowcount` check + jittered backoff. The
deferred `_tx` (read lock until first write) lets two workers SELECT the same row — use
`_tx_immediate` for anything that leases/claims. All worker writes serialize through one writer;
fine at PRD scale, revisit Postgres only past a measured throughput ceiling.

### D6. A test that HANGS is far worse than a test that fails — it wedges CI silently
**Symptom:** `python -m pytest` never terminates. Locally it looks like a stalled run ("1%" for
10+ minutes); in CI the job burns until GitHub's 6-hour timeout and only then fails, and because
`deploy` `needs: [engine, panel]`, **deploys are blocked** with no useful signal about why.
**Root cause:** `CDPFeedBase._call_bounded` (`core/cdp.py:232-256`) deliberately stopped enforcing
a deadline — routing Playwright's greenlet-based, thread-affine SYNC API through
`core.bounded.call_bounded` raised `greenlet.error: Cannot switch to a different thread` on 100% of
calls, so every scroll notch was skipped and the feed never advanced. Removing it was correct. What
was missed is that **five tests still assert the old contract by feeding in a page that blocks on
`threading.Event().wait()`**. With a deadline they returned and passed; with none they never return
at all: `tests/core/test_cdp.py` (3), `tests/core/test_human.py` (1),
`tests/engines/instagram/test_cdp_timeouts.py` (1).
**Fix, part 1 (2026-08-13):** all five marked `pytest.mark.skip` to unwedge CI while the real fix
was designed.
**Fix, part 2 — the deadline is BACK (2026-08-13), `core/pw_owner.py` (new, ~390 lines).** All five
tests are un-skipped and pass in 0.84s. The mechanism is a dedicated `PlaywrightOwner` daemon thread
with a work queue: **`attach()` itself runs on the owner**, so `sync_playwright().start()`,
`connect_over_cdp`, the `Page`, the `BrowserContext` and every ElementHandle are created on and bound
to that thread — it IS the owning thread. Only the WAIT crosses threads. That is the distinction the
original disaster missed: the rule is thread-AFFINITY, not thread-identity, and `readiness.py` was
already safely using `core/bounded.py` for exactly this reason (it starts its own `sync_playwright()`
INSIDE the daemon thread). An `OwnedPW` auto-dispatching proxy replaces what would have been a
~35-site hand migration across 5 files — every missed site would have raised `greenlet.error` inside
an `except Exception: return False`, i.e. silently.
**Verified live, not inferred:** against real headless Chrome 151 on `--remote-debugging-port=9444`,
a `SIGSTOP`'d browser makes `page.mouse.wheel` hang unboundedly (the D6 risk, observed); the same
call under a 1.0s queue deadline returns control at 1.002s; `SIGCONT` self-heals and the next call
succeeds in ~6ms.
**Three ways this fix nearly recreated the original catastrophe, all caught in review and fixed:**
1. **A wedged session finished as a clean, successful, zero-lead run** — probe:
   `walk elapsed=23.59s reels=0 healthy=False exception=None`. The deadline turns "hangs forever"
   into "every call degrades", and every degrade path is skip-and-continue, so the run ended
   `completed` with no halt reason and no flag: **ledger B6's exact complaint**. Now `PlaywrightOwner`
   counts a wedge streak (expiries AND the fast-fails behind them; any completed call resets) and
   `walk()` raises `HaltSession("cdp_call_wedged", kind="canary")` past
   `cfg.max_consecutive_wedged_calls` (default 3, 0 disables).
2. **The owner could migrate mid-session.** An unlatched `_ensure()` failure let the owner move from
   the caller thread to a new daemon thread — the forbidden move, mid-run.
3. **`attach()` leaked the node driver subprocess** on any failure after `sync_playwright().start()`,
   because the objects lived in a local closure. `stop()` is only legal on the owning thread, so the
   cleanup had to run there.
**The loud-failure guard:** `PlaywrightThreadAffinityError` deliberately subclasses **BaseException**
(`pw_owner.py:92`), so a thread-affinity regression escapes the `except Exception:` handlers that
made the first breakage invisible to a fully green suite. If this trap is ever re-entered, it fails
loudly instead of silently harvesting nothing.
**Rejected with evidence, do not retry:** a transport-level kill (nothing to kill — the acceptance
fakes block in pure Python with no CDP behind them, so it re-wedges CI) and prevent-and-detect via
`os._exit` (terminates the pytest process mid-test; a crashed session is not a green test).
**How to avoid/detect:** when you delete a guarantee, grep for the tests that assert it — a test
built around "hangs forever" input turns into an infinite hang, not a red X. Any test that fakes a
wedge should carry its own hard bound (`pytest-timeout`, or a real deadline inside the fake) so it
FAILS instead of hanging. Consider a job-level `timeout-minutes` on the CI engine step so a wedge
surfaces in minutes rather than hours.

---

## E. HTTP bridge, panel API & product surface — found by the LIVE SHAKEDOWN (2026-08-13)

> Ten agents booted the real bridge, the built panel and a real `aizu-worker` on isolated ports and
> drove the product over HTTP; every claimed defect was then independently reproduced from a clean
> slate before being accepted. **All of section E was invisible to a fully green 1,953-test suite.**
> The meta-lesson: unit tests prove a function's contract; only running the app proves the product's.

### E1. Unauthenticated remote DoS — the access log echoed the full request path
**Symptom:** a few hundred KB of long-URL requests froze the WHOLE bridge; a normal `/api/health` went
from 0.9 ms to 1157 ms. Measured curve: 1k=6ms, 8k=190ms, 16k=726ms, 32k=2894ms, 64k=11594ms.
**Root cause:** `log_request` handed the entire attacker-controlled `self.path` to the Rich console
handler; word-wrapping ONE unbroken token is quadratic, and Rich's renderer holds the GIL, so every
thread stalls. No auth needed to trigger it.
**Fix:** `_log_path()` bounds the path at every sink, plus a `LineCapFormatter` backstop on the
console handlers so no caller can make rendering superlinear. Capped PER LINE, so tracebacks keep all
frames; the file handler stays uncapped. After: the curve is flat (~1.5 ms at 64k) and the flood costs
68 ms instead of 2103 ms.
**How to avoid/detect:** any request-controlled string reaching a log sink is a DoS surface, and a
pretty-printing handler is a *computational* surface, not just an I/O one.

### E2. NaN/Infinity accepted at 200 permanently bricked an org's panel
**Symptom:** `POST /api/campaign` with `budgetCap: Infinity` → HTTP 200, stored. Thereafter
`/api/state` and `/api/campaigns` returned **200 with a body no JSON parser accepts** (`json.dumps`
emits bare `Infinity`/`NaN`, invalid per RFC 8259). The org's panel was dead with no in-app remedy.
**Root cause:** `_opt_number` validated type and `>= 0` but never `math.isfinite` — and **`NaN < 0` is
False**, so NaN sailed through the range check.
**Fix:** one shared `_finite_number()` boundary, AND `_json_bytes()` now serializes with
`allow_nan=False` so the response layer itself cannot emit invalid JSON — the class cannot recur
through a different door.
**How to avoid/detect:** every comparison-based range check has a NaN hole. Validate finiteness
explicitly, and make the serializer the last line of defence.

### E3. An out-of-range number killed the connection with NO response and leaked a traceback
**Symptom:** a 400-digit `budgetCap` → curl exit 52, HTTP 000, empty reply — the socket reset with no
HTTP response at all — plus a full traceback with absolute paths on stderr. A sibling case returned
500 leaking the SQLite driver's own message.
**Root cause:** validation ran BEFORE the handler's try-block, and `float(value)` raises
`OverflowError`, which escaped `do_POST` unguarded.
**Fix:** `_finite_number` catches it → clean 400; plus `_dispatch_guarded()` wraps all routing so any
unexpected exception becomes a generic 500 with detail to the LOG only, and a `send_response_only`
override prevents a second status line on an already-started response. 19 `error=str(e)` leak sites
swept.

### E4. Lead identity collided across campaigns — clicking one lead wrote to another
**Symptom:** an operator clicked a row in campaign A; the drawer opened campaign B's lead, and the
status write returned 200 having marked **B's** lead 'interested'.
**Root cause:** the panel payload flattened a lead to `"id": comment_id`, but real identity is the
composite PK `(campaign_id, platform, comment_id)`. The same commenter under two campaigns — or the
same id on two platforms — collapsed into one row.
**How to avoid/detect:** when a DB row has a composite key, any single-column "id" handed to a client
is a bug waiting for its second tenant. Check every flattening boundary between store and panel.

### E5. A colliding campaign CREATE silently destroyed the existing campaign
**Symptom:** creating a second campaign named the same as an existing one overwrote the first's brief,
and because `matches` is keyed on `(campaign_id, …)`, the first campaign's entire lead history
silently re-pointed to what the operator thought was a new campaign.
**Root cause:** CREATE and EDIT were the same endpoint with the same payload and no discriminator.
**Fix:** a create that would clobber an existing id is refused with `409 a campaign with this id
already exists`. **The trap during the fix:** the first attempt keyed the guard on an explicit
`op: "edit"` field — which the SHIPPED PANEL NEVER SENDS, so the bug was untouched on the real wire
while its regression test passed. Caught by the verification pass. Any create/edit discriminator MUST
be validated against the payload the actual client sends.

### E6. Campaign ids were a GLOBAL namespace across tenants
**Symptom:** org B creating an ordinarily-named campaign that org A already had got
`404 unknown campaign` **on a CREATE** — and could thereby probe which ids other tenants owned (a
cross-tenant existence oracle).
**Fix:** campaign identity is per-org. **The regression this introduced, caught in verification:** the
first fix let any tenant permanently lock every other tenant out of any campaign name by
pre-registering it — a cross-tenant denial-of-service *created by the fix for a cross-tenant leak*.
**How to avoid/detect:** when de-globalising a namespace, ask immediately what a hostile tenant can now
reserve.

### E7. The B6 fleet-failure fix was INERT on the wire (the B4 trap, one layer up)
**Symptom:** a dispatched run whose worker could not attach Chrome showed the operator NOTHING —
`/api/run/activity` returned `404 unknown run`.
**Root cause:** the handler 404s when there are no sessions AND no events — which is exactly the state
of a job that died before doing anything — and that check sat BEFORE the `fleetJob` block, using a
`job` row the handler had ALREADY FETCHED a few lines earlier.
**How to avoid/detect:** this is ledger B4 repeating in a new place. A fix is not shipped until a test
drives the SERVED endpoint in the exact failure state the fix exists for. "No rows yet" IS the state
that matters for a failure-reporting path.

### E8. The panel reported $0 spent for any campaign with spend but no leads
**Symptom:** $999 banked in `spend_log` rendered as `"spent": 0.0` on the campaign card, while the
SAME `/api/reports` payload summed that money under `spendByStage` — the payload contradicted itself.
**Root cause:** `per_campaign_rollup` built its rows `FROM matches … GROUP BY campaign_id`, so a
campaign with zero leads simply did not exist in the result, and the panel defaulted it to 0.
**How to avoid/detect:** a rollup driven off the *success* table silently reports zero for every
failure case — which is precisely when an operator needs the number.

### E9. B10's revocation halt was trigger-happy: a 9-second blip bricked a box
**Symptom:** restarting the bridge on an empty DB destroyed a worker's persisted token in NINE
SECONDS (401s at :19, :22, :28 → CRITICAL, token deleted, box parked pending a manual re-enrolment).
**Root cause:** the confirmation was COUNT-ONLY (`_UNAUTHORIZED_CONFIRM_LIMIT = 3`) while `_backoff`
starts sub-second, so "three strikes" was worth ~2.5 s of wall clock.
**Fix:** confirmation is now bounded in TIME as well as count — a sustained window (~5 min) with a
minimum spacing between 401 retries. A genuinely revoked box still halts, still never auto-re-registers,
and revocation still survives a restart.
**How to avoid/detect:** "N consecutive failures" is meaningless without knowing the retry interval.
Any destructive confirmation threshold must be expressed in time.

### E10. The production build publicly served the entire TypeScript source
**Symptom:** `GET /assets/app-*.js.map` → 200, ~2.1 MB, unauthenticated, with `sourcesContent` for all
265 sources — the complete proprietary frontend including the RBAC mirror. 7.5 MB of a 10 MB `dist`.
**Fix:** `sourcemap: 'hidden'` plus a Vite plugin that moves every `.map` out of `dist` into
`admin-panel/.vite/sourcemaps/`. Maps still exist for symbolicating a production stack trace; none is
ever served. Never copy them into a deployed `--panel-dir`.

### E11. Smaller live findings, all fixed
- A campaign create rejected with 400 still **committed a ghost row** (meta written before the brief
  was validated) — an un-runnable card with no brief.
- `GET /api/state` handed a `member` the org's full run history and spend, which every other route
  correctly 403s for that role.
- **HEAD bypassed all routing** (no `do_HEAD`): `/app/campaigns` GET 200 / HEAD 404, `/api/state` GET
  401 JSON / HEAD 404 HTML, violating two explicit in-code design comments.
- A nested unknown URL served the landing at 200, so its relative asset URLs answered HTML with a 200.
- Starting on a busy port dumped a raw `OSError` traceback and left a stray migrated DB behind.
- Session cookies carried no `Secure` flag on a 30-day credential.
- `Server: SimpleHTTP/0.6 Python/3.12.13` on every response.
- `workers.host` was always NULL — the sidecar never sent the field the server accepts.
- **CLAUDE.md documented a production start command that fails instantly**: `--db` is a TOP-LEVEL flag
  and must precede the subcommand. Every probe agent tripped on it.

---

## Cross-machine deployment gaps still OPEN (not yet fixed)

- **LinkedIn & X live endpoint capture unverified** (blocker for production; needs warmed
  accounts). The parsers are shape-based and the loop is proven on fixtures, but nobody has
  confirmed the live selectors/URLs actually fire interception. Capture real response bodies
  in DevTools (X: `HomeTimeline` / `SearchTimeline` / `ListLatestTimeline` / `TweetDetail` /
  Quotes; LinkedIn: Voyager feed + comments), drop them as fixtures, and confirm the parsers.
  X rotates `doc_id`s every ~2–4 weeks, so also confirm the empty-interception canary trips on
  drift. Full detail in
  [`docs/archive/handover-linkedin-x-2026-06-29.md`](../docs/archive/handover-linkedin-x-2026-06-29.md) §2A.
- ~~**Campaign brief not shipped to remote workers** (B4)~~ **CLOSED 2026-08-12** — baked into
  the job spec; per-org platform credentials now fetched on demand (B7). A remote box no longer
  needs the shared SQLite file to resolve a campaign or its credentials.
- **Shared fleet bootstrap token** (B8): the per-worker enrolment-token mechanism SHIPPED (v22), but
  the cutover has not run. Every box that first-registered on the shared token is still unclamped and
  self-declaring, and stays that way until its persisted token is deleted and it re-enrols. Gate on
  `SELECT id, host FROM workers WHERE revoked_at IS NULL AND enrolment_scope_kind IS NULL` returning
  empty, then set `AIZU_WORKER_LEGACY_BOOTSTRAP_ENABLED=0`.
- ~~**Revoking a worker bricks it** (B10)~~ **CLOSED 2026-08-13** — a confirmed 401 clears the token,
  halts loudly and parks; and a revoked box can no longer walk back in on the shared bootstrap
  secret, so revocation is durable even before the B8 cutover runs.
- ~~**Desktop Rust is uncompiled**~~ **CLOSED 2026-08-19** — the blocker was PATH, not tooling:
  `cargo` is installed at `~/.cargo/bin` and simply is not on the shell's default PATH, which is
  why several rounds recorded "no cargo on this build host". With it exported,
  `cargo check --all-targets` is clean (one dead-code warning, `errors.rs:43`) and `cargo test`
  passes **81 tests**, including the A12 brand contract and the A8 `the_probe_script_is_valid_python`
  regression. Re-check before packaging, but the standing debt is paid. Detection lesson: "command
  not found" is not "not installed" — check `~/.cargo/bin`, `/opt/homebrew/bin` and `/usr/local/bin`
  before recording a tooling gap that then persists across commits.
- ~~**Spend cap is per-box, not per-campaign** (B9)~~ **CLOSED 2026-08-13** — prior total resolved at
  lease time + delta rolled up on ack and nack; the box enforces, the cloud's enqueue-time skip fails
  open. Residual: an attempt on a permanently-dead box that never acks or nacks is never banked.
- ~~**Live exit gate** (B6)~~ **CLOSED 2026-08-21** (section I) — a panel-authored campaign,
  dispatched to a WORKER and run against a warmed, logged-in Chrome, produced real leads:
  37 reels → 27 relevant → 26 comments scored → 2 matches (`@lead-A` «Цена?» 0.78,
  `@lead-B` «Можно узнать цену такого ремонта» 0.82), with 0 permalink bounces, 0 owner
  wedges and three consecutive sessions completing. The whole chain — discover, gate, open the
  right reel, intercept comments, score, write a lead — now runs end to end on the fleet path.
  Earlier framing of this gate, kept because the narrowing steps are what made it closeable:
  still open, but materially narrowed on 2026-08-19 (section H). Two live
  runs on a warmed, logged-in Chrome proved every stage up to and including comment scoring —
  run 2 fetched and scored **12 real comments** on a live reel, the first time `_process_comments`
  has ever executed in this repo. What remains unproven is only the last step: no comment has yet
  scored >=0.70, so `matches` has never received a row. The blocker is now content/model, not code
  path — the shipped brief hunts SaaS buyers while the reels reaching it are construction/PM.
- **Windows/Linux packaging**: `.exe`/`.msi`/NSIS need a Windows host; code-signing/notarization
  (Apple Developer ID; Windows Authenticode/EV) unresolved; ad-hoc/unsigned chosen for the
  managed fleet (Gatekeeper `rejected` is expected — strip quarantine / distribute out-of-band).

---

## F. Second live shakedown — 25 agents, two rounds (2026-08-14)

> Round 1 (16 agents) drove all 56 `/api/*` routes, a real `aizu-worker` sidecar, the CLI and the
> production static serving on isolated ports — **48 verified findings, 1 refuted**. A completeness
> critic then showed round 1 had never once opened a *pre-existing* database, never run two writers
> against one file, and never abused the socket layer. Round 2 (9 agents) closed exactly those
> classes — **17 more findings, 3 refuted**. Every claim was re-run from a clean slate by an
> independent verifier before being accepted here.
>
> **Both suites were fully green throughout (2026 engine + 487 panel).** Section E's meta-lesson
> repeats and hardens: unit tests prove a function's contract, running the app proves the product's,
> and only *abusing* the app proves it under load.

### F1. `/api/state` and `/api/dashboard` are unbounded full-table dumps — the panel congestion-collapses at ~6 readers
**Symptom (measured, reader-only ramp, 100k-lead org, zero writers):**
`clients=1 p50 2.41s → 2 p50 5.46s → 4 p50 11.28s → 6 p50 24.16s (2/18 over 30s) → 8 p50 63.13s,
max 123s, 24/24 over 30s`. Throughput *falls* 4.6x from 1 to 8 clients; 8x the clients costs 26x the
p50. Every request still returned 200 — the bridge never fails, it just stops being usable.
**Root cause:** both endpoints embed EVERY lead in the org. 47 MB at 100k leads, 239 MB at 500k. The
panel polls them on a timer from every open tab (`refetchInterval` in `useDashboard.ts` /
`useCampaigns.ts`), so N tabs = N concurrent multi-megabyte serializations.
**Not the cause — checked explicitly:** writers are innocent. Re-running the ramp with 1, 4 and 16
concurrent external SQL writers on the same file changed latency by less than noise (clients=4: 11.28s
with 0 writers, 8.20s with 16). WAL delivers real writer/reader isolation.
**A diagnosis the first prober got wrong, and the verifier corrected by measuring:** the collapse was
attributed to the per-request `Store()` open (a write txn, see F3). That accounts for ~0.27s of a 60s
request. Fixing only it leaves the collapse essentially unchanged. **Cap/paginate the two endpoints —
that is the fix that matters.**
**Reproduced by hand, independently of any agent** (100k seeded leads, own bridge, own harness):
`/api/state` **60.7 MB**, `/api/dashboard` **60.7 MB**, `/api/leads` 0.0 MB (correctly paginated — only
those two endpoints are unbounded). Ramp: `conc=1 p50 2.26s → 2 p50 4.36s → 4 p50 8.20s → 6 p50 16.32s,
max 32.65s`, throughput 0.44 → 0.30 req/s. `conc=8` did not finish 16 requests in 4 minutes. Bridge RSS
peaked at **1.2 GB**. The very first (cold) `/api/state` took **68.9s**, then 2.26s warm — so the first
operator to open the panel after a restart waits over a minute.
**Effective ceiling today:** ~5 simultaneous panel users per bridge process, on a product sold as a
multi-tenant operator panel. Whether that is fatal depends on real org size, which nobody has measured
— at 5k leads this is a medium; at 500k it is terminal. **Measure your actual p95 org size before
pricing the fix.**

### F2. No socket read/idle timeout anywhere — slow-loris pins one unbounded thread per connection
`BaseHTTPRequestHandler.timeout is None`, and `ThreadingHTTPServer` spawns one unbounded thread per
connection. Measured: 2000 half-open sockets pinned 2004 threads indefinitely; saturation ceiling
~12288 threads (recoverable). A single ~200 MB RSS high-water is retained after a large burst —
bounded by peak concurrency, not a per-request leak, so not a creeping leak.
**Consequence:** the bridge is safe **only** behind a fully-buffering reverse proxy. Either set a
socket timeout and a connection cap, or make the proxy a documented hard requirement rather than a
deployment suggestion.
**Genuinely hardened, do not re-test:** 100 KB request line → 414; 5000 headers → 431; 10 MB header →
431; unknown method → 501; a lying 10 MB `Content-Length` rejected before a byte is read; 500 abrupt
mid-response RSTs → zero tracebacks. The parse layer is fine. Only *blocking reads* are soft.

### F3. `Store.__init__` writes on every request, so a >30s write lock hangs pure READS and then lies about why
`_init_schema()` runs `CREATE INDEX IF NOT EXISTS …` plus an unconditional
`UPDATE meta SET value=? WHERE key='schema_version'` on **every single open**, and the bridge builds a
fresh `Store` per request. Holding `BEGIN EXCLUSIVE` externally for 35s (past `busy_timeout=30000`):
`GET /api/state → 500 in 31.86s`, `POST /api/campaign → 500 in 31.86s`, log shows
`OperationalError: database is locked` x4 then a generic `internal server error`. The client waits 32
seconds and is told nothing useful. The bridge recovers cleanly once the lock clears.
**Fix:** make `_init_schema` a no-op when the version already matches, and map lock timeouts to a fast,
named 503 instead of a slow, opaque 500.
**Unmeasured premise:** nobody demonstrated a real workload that holds a write lock >30s, so the
production frequency of this is unknown — but the failure *mode* is now known and it is bad.

### F4. `POST /api/status/bulk` is not atomic — two operators tear a batch and both are told it worked
40 trials, 25 leads each, barrier-released. 2 clients: 80/80 responses `200 {"updated":25,"missing":[]}`,
**15% of trials ended split across two statuses** (e.g. `{closed:12, interested:13}`). 6 clients: 20%
torn, some across three. An independent re-run measured 8%/10% — timing-dependent, same phenomenon.
**Root cause:** `server.py:2777-2784` loops `store.set_status(...)` per item, each its own transaction,
so two batches interleave lead-by-lead. `updated` counts rows *touched*, not rows that kept the
caller's value.
**Bounded honestly:** `lead_status_changes` stayed self-consistent in 80/80 trials, so nothing is
destroyed and history is recoverable. What is broken is the operator's belief — in the explicitly
supported multi-user triage workflow, with a false success response.

### F5. `POST /api/campaign` op=create is check-then-act across transactions
9% of 8-way races produce multiple 200s; **1.5% produce a row with one operator's display name and
another operator's brief.** Needs a UNIQUE constraint or a single `BEGIN IMMEDIATE` — i.e. exactly the
`_tx_immediate` discipline D5 already mandates for anything that leases. Note this is a *different*
defect from E5/E6 (which were about the id namespace), living in the same handler.

### F6. An archived or `ended` campaign is still runnable via `POST /api/run`
**Reproduced by hand, independently of any agent:** create → `POST /api/campaign/archive
{archived:true}` → 200 → `POST /api/run` → **202**, and 4 seconds later the campaign shows
`archivedAt` set AND `leads: 3`. Real rows, written to an archived campaign.
**Root cause:** `_handle_run` (`server.py:3617`) gates on `campaign is None or not owned` and nothing
else — no `archived_at`, no `status` — directly beneath a comment claiming *"A single-campaign run must
be runnable now."* Every OTHER path gates correctly (`RUNNABLE_SQL_PREDICATE` in
`due_scheduled_campaigns`, the fleet dispatcher, `scope='all'`), and `_handle_campaign_archive`'s own
docstring asserts "archived campaigns are runnable by no path." The docstring is false.
**Second half, worker side:** `lease_one_job` never joins `campaign_meta` and `_resolve_campaign`
prefers `job.campaign_brief` outright, so a campaign archived or paused *between enqueue and lease* is
handed out with its baked brief and runs anyway. Archive is not a kill switch on either side of the
fleet boundary.
**Third half:** an ordinary campaign save re-creates the `(archived, live)` state the archive handler
documents as unreachable.

### F7. A fleet run has no exit — it cannot be stopped, cancelled, or dead-lettered by anyone
Three independent holes that compose into "no way out":
1. `_handle_run_stop/pause/resume` (`server.py:3884/3898/3913`) dispatch only to `self.run_manager`,
   which knows nothing about fleet jobs. The tenant gets `409 no run is active` while
   `/api/run/activity` simultaneously reports `running`.
2. `count_capable_workers` (`store.py:3670`) filters `WHERE revoked_at IS NULL` only — never
   `last_heartbeat_at` — so `/api/run` returns **202 with zero live workers**. The job then sits
   `queued`, `pinned_worker_id=NULL`, `attempts=0` forever: `reclaim_offline_jobs`' two passes cover
   `status IN ('leased','running')` and `status='queued' AND pinned_worker_id IS NOT NULL`, and an
   **unpinned queued job matches neither**. Every retry then 409s "already running".
3. There is no tenant- or admin-reachable job-cancel route at all (every `/api` path enumerated).
**Related, same family:** `/api/admin/jobs/enqueue`'s capability precheck counts workers offline for
**30 days** as capable, so the guard that exists to prevent exactly this creates it instead.

### F8. The global halt/drain flag is a one-way door that kills the process with rc=0
`sidecar._loop` returns on `_stop_leasing` and `run()` falls straight through to `return 0`
(`sidecar.py:1420-1436`). So `{scope:global, halt:true}` **terminates every sidecar process**; clearing
the flag brings nothing back; and a supervisor reads rc=0 as a clean shutdown and will not restart.
Same at org/platform/worker scope. The fleet console shows halted boxes as `stale`→`offline`,
indistinguishable from a crash. Only the *re-enrolment* path parks-and-repolls correctly — that is the
shape the halt path needs. Neither the halt nor a token revocation writes an admin audit row.

### F9. Three provisioning traps that each produce a green box that cannot work
1. **`AIZU_SECRET_KEY` is absent from CLAUDE.md's worker-plane env list** but
   `FernetFileBackend._get_cipher` (`token_backends.py:73`) needs it to persist the token. First boot
   crashes *after* the server has already minted the identity — so the row reads `online` while the
   process is dead, and a supervisor crash-loop rotates `worker_token_hash` every iteration.
2. **`AIZU_WORKER_PLATFORMS` / `AIZU_WORKER_CAPABILITIES` are also absent from that list.**
   `_parse_capabilities_env` returns `()` when neither is set (`config.py:100`), so the box registers
   with `capabilities: []` and can never be leased to — **and `fleet_readiness` (`readiness.py:284`)
   counts online boxes without matching platform, so adding that useless box flips the tenant's
   readiness banner from an accurate `ready:false` to a false `ready:true`.**
3. **The box needs its own LLM credential.** `cfg.run_args` runs the engine in a child on the box;
   without `OPENROUTER_API_KEY` every live job fails at run setup and dead-letters at attempt 5. Also
   not in the env list.
**Fix shape:** a `WorkerConfig.from_env` preflight that refuses to register, loudly, naming the missing
variable — plus capability matching inside `fleet_readiness`.

### F10. The CDP port is ambiguous across the codebase — cheapest high-value fix on this list
`worker/config.py:269` defaults `cdp_url` to **9222** and CLAUDE.md agrees; `engine/scripts/warm_chrome.sh`
binds **9333** and `engines.md §9` documents 9333; `chrome_manager.py:19` says in-code *"Port note
(unresolved policy): the engine default is 9222 but every LIVE run in this repo has used 9333."*
**Consequence:** a box provisioned per the warming runbook launches Chrome on 9333 while the sidecar
probes 9222 → `cdp_unreachable` nack on every Instagram/LinkedIn/X job, on a box that looks perfect in
the fleet console. Pick one value; fix the loser in code, both docs and the warm script.

### F11. Re-enrolling a revoked box takes ~10.5 minutes and then lies about why it recovered
`_register` (`sidecar.py:741-749`) prefers the persisted token unconditionally, so a fresh enrolment
token is never presented until B10's confirmation (≥3 401s **and** ≥300s) deletes the file — measured
**10m29s**. Any single non-401 calls `_note_authorized` and restarts the 300s clock, so a flapping
bridge extends it indefinitely. Recovery then logs *"the earlier 401s were not a revocation after all"*
— false. `REENROLMENT_ACTION` (`sidecar.py:116`) never mentions deleting `worker-token.enc`, though
deleting it first makes the same restart register in 0.1s. The same 10.5-minute path fires for any
stale token (bridge restored from a snapshot, `--db` re-pointed).
**This is B10 working as designed colliding with B8's runbook.** The design is right; the precedence is
wrong: an explicitly-supplied enrolment token should beat a persisted one.

### F12. A crashed fleet job's real cause never leaves the box
`_read_and_map_result` (`job_runner.py:377-380`) raises `RuntimeError(f"job {id}: {reason}")`, so the
operator sees `Job <id> crashed (rc=None): RuntimeError` and the tenant sees `reason:"error", events:[]`.
The per-job postmortem file the crash line points at is **0 bytes**; the real message (`No LLM backend
configured…`) lives only in `.worker-state/logs/aizu.log` and `run-<runId>.log` on the box. On the
intended topology — operator PCs running the Tauri shell, which nobody can SSH into — that is
undiagnosable. Tail `run-<runId>.log`, which is already keyed by the runId the tenant holds.

### F13. Invite email binding is UI-only; offboarding does not revoke minted invites
`_handle_signup` passes the client-supplied email straight into `store.accept_invite`, which checks only
token_hash / accepted_at / expires_at and **never reads the invite's own `email` column** — while
`SignupPage.tsx:73` renders that field `disabled` + `readOnly`, so the product presents invites as
address-bound. Verified: an invite addressed to `bound@example.com` was redeemed by `attacker@evil.test`
as a full org **admin**. The invite is a pure bearer credential; a forwarded email or a pasted Slack link
is a tenant join. Separately, removing a teammate leaves the invites they minted redeemable.
(The raw token in the `GET /api/invite?token=` query string *is* redacted in the access log — that half
of the original claim was refuted.)

### F14. The admin audit log is not tamper-evident against truncation, and impersonation is invisible
`/api/admin/audit/verify` returns `ok:true` after the log is truncated at the tail **or wiped entirely** —
a hash chain verifies links between rows that exist, and says nothing about rows removed from the end.
Separately, writes performed while impersonating a tenant are stamped on the *tenant's own user* and
appear nowhere in the admin audit log: `POST /api/org {"name":"PWNED"}` under impersonation wrote no
admin row at all. Together these mean the audit trail cannot answer "what did the superadmin do."

### F15. The daytime write guard runs on the *box's* clock
`Pacer.is_daytime` (`pacing.py:60`) reads `datetime.now().hour` — worker-box local time, with no account
or org timezone anywhere in the path. A fleet spread across timezones halts a tenant's Instagram/
LinkedIn/X jobs at hours the tenant considers midday. `AIZU_IGNORE_DAYTIME` is a testing escape hatch and
is also not in the worker env list. Either thread the org timezone into `PacingConfig` or write down a
box-placement rule and enforce it at enrolment.

### F16. Dry runs write real leads and permanently burn the tenant's monthly plan cap
`_handle_run` checks `count_leads_this_period` and clamps the target for **every** mode, and dry runs
write real `matches` rows. Four dry runs exhaust the whole Free tier, and the only remedy is "wait for
next month". Severity was initially rated critical on a user journey that does not exist — `RunDrawer.tsx:163`
hardcodes `mode:'live'`, so there is no dry-run affordance in the shipped panel. **The lesson is the
error, not the bug:** a severity was assigned from an imagined UI and only a grep caught it.

### F17. Migration of a pre-existing database — mostly excellent, one sharp edge
**The good news, and it is substantial.** A genuine v21→v22 upgrade of a populated install (24 non-empty
tables, 2 orgs, 6 users, encrypted secrets) was **clean**: every row count and content hash preserved,
all 6 users logged in, the Fernet secret still decrypted, zero `*__legacy_v1` / `*__legacy_v6` leftovers,
and all 14 API responses byte-identical to a HEAD-native control after timestamp normalisation. The v7
multi-tenancy adoption is **genuinely self-healing** — SIGKILL at 4 points inside it, plus 3 inside the
v6 rename-aside, all recovered perfectly. The in-code claim at `store.py:1244` holds *for the path it
describes*.
**The sharp edge:** the structurally identical **v1→v2** platform reshape has no equivalent
existence-keyed retry. SIGKILL anywhere in it (5 interposed points + 4 plain wall-clock kills into a
1.73s migration of a 163 MB / 400,005-row DB) permanently strands every row: the first
`ALTER TABLE matches RENAME TO matches__legacy_v1` is durable across the kill, so on reopen `matches`
exists with a platform column, `_needs_platform_migration` returns False, the copy-forward never runs,
and `meta.schema_version` is stamped **22**. The install comes back up looking healthy and empty.
**Reachability:** almost certainly nil — this repo's earliest commit already declares v17+, so no
pre-v2 database plausibly exists in the field. Filed because the code is maintained and the failure is
silent-total. Give the v1→v2 copy the same existence-keyed retry the v7 path has, and add
`if prior > SCHEMA_VERSION: raise` (a newer-than-code DB currently opens silently and gets downstamped).
Also: upgrading a pre-v3 DB never runs the org adoption, so every surviving row is invisible to the panel.

### F18. Smaller confirmed items
- A >255-byte path segment returns 500 and Rich-renders a full traceback — unauthenticated remote
  log-flood. **This is E1's family resurfacing at a different entry point**; `_log_path()` bounded the
  access-log sink, not the error path.
- Archiving a lead makes `/api/dashboard` contradict `/api/leads` and `/api/reports` — three different
  counts for the same org.
- `/api/reports`' Today/Week/Month selector is inert for 4 of 6 tiles, and Today shows all-time spend
  next to today's leads.
- Login lockout is keyed by email alone, so any unauthenticated caller can lock out a known account at
  will; the superadmin plane has the same shape, so the platform's only admin can be locked out.
- A worker with no capabilities registers as a healthy "online" box (see F9.2).
- `/api/lead/note` accepts a `commentId` with no lead, and NUL/control characters in the body; a
  `noteId` past SQLite's INTEGER range 500s with a traceback.
- `POST /api/integration` accepts `connected:true` with no credential, so the panel shows Connected for
  an integration that has nothing behind it. (The *impact* claim — that runs then silently spend the
  operator's shared server-wide key — was never executed and remains unproven.)
- A signed billing webhook creates a `subscriptions` row for an org id that does not exist.
- `/api/admin/audit?limit=-1` and `limit=0` silently return exactly one entry.
- Minting an enrolment token for a nonexistent or negative orgId 500s with a stack trace.
- `aizu run` on the file brief writes leads with `org_id NULL` — unreachable from every panel view.
- An unwritable `--db` path dumps a raw sqlite3 traceback instead of the one-line `error:` every other
  CLI failure uses.
- `transfer_ownership` is declared in **both** RBAC matrices and implemented nowhere: org ownership is a
  permanent dead end.
- `GET /api/state` leaks the whole per-org settings block to member and viewer, who are 403'd on
  `GET /api/settings`.
- `_coerce_extract_def` collapses a comma-separated `extractDef` into one field named
  `handle_budget_city`.

### F19. What these two rounds PROVED works — do not re-test
- **The write path is not the bottleneck.** 32 parallel writer processes against the live bridge's file:
  zero errors, zero "database is locked", ~1.5M rows committed. 16 concurrent SQL writers moved HTTP
  latency less than noise. Nothing here argues for leaving SQLite.
- **The lease contract holds under real parallelism.** 6 racing worker processes, one queued job, 120
  barrier-released trials: **exactly one winner in 120/120, zero cross-leases.** D5's `_tx_immediate`
  discipline works as documented.
- **Transient DB faults self-heal.** Real ENOSPC and a read-only filesystem both 500 for the duration
  and return to 200 the instant the fault clears — same PID, no restart, verified over 45s of polling.
  An earlier "permanent wedge" claim was **refuted**; do not build a watchdog for it.
- **RBAC is in lockstep.** `rbac.py` and `roles.ts` were diffed action by action: same 19 actions, same
  role sets, same `canManageTarget`/`canAssignRole` semantics, no disagreement. The server gate was then
  proven empirically for all four roles across 18 endpoints and matched `rbac.py` exactly every time.
- **All ten never-driven success paths return 200/202 against real handler code**, including all four
  branches of `/api/agent/launch-login` (the one route nobody had ever touched) and campaign
  `generate`/`interview` against a local fake model, with a deliberately ragged reply.
- **The "Telegram wizard stores an unreadable credential" theory is disproven.** `TelegramLoginManager.verify`
  returns a dict and the full round-trip through the real getter reads back correctly. The bare-string
  failure mode exists only for a caller that does not exist.
- **Refuted outright, ignore them:** the cross-tenant credential reach filed as new (it is B8 verbatim),
  the permanent read-only wedge, `set_integration_secret` non-dict, `model_comparison_stats` NULL
  averaging, and the raw invite token reaching the access log.

### F20. Method notes worth keeping
- **The completeness critic paid for itself.** Round 1 was thorough and still had three whole classes at
  literally zero coverage — pre-existing databases, concurrent writers, the socket layer. Asking "what
  did nobody probe?" found more than adding an eighth prober would have.
- **Verifiers must RUN, not reason.** The verifier's most valuable act was re-measuring F1's *diagnosis*
  and finding it wrong (0.27s of a 60s request) while the *observation* was right. A confirmed symptom
  with a wrong cause is how you ship a fix that changes nothing.
- **Watch for confident impact paragraphs with no probe behind them.** Across round 1 the observations
  were strong and the impact claims were frequently pure code-reading — and two of them were flatly
  disproven by a verifier who bothered to look. Round 2 added a `measuredOrInferred` field per finding
  specifically to force that distinction; keep it.
- **A plausible number can prove nothing.** One finding's stated root cause rested on
  `pragma foreign_keys -> 0` read from the prober's *own throwaway connection* — a per-connection pragma,
  while `store.py:1197` sets `PRAGMA foreign_keys=ON` in `Store.__init__`. The real cause (no
  `FOREIGN KEY` clause on `accounts.org_id`) was different and only surfaced when someone dumped the schema.

---

## G. Worker launch preflight + first-run setup wizard (2026-08-14)

> Built because the shakedown showed a freshly-installed worker opens a dead dashboard, tells the
> operator nothing (the only diagnostic was an `eprintln` to stderr a GUI user never sees), and the
> sole configuration surface was a **hidden dev menu behind 7 taps on the brand logo**. Meanwhile
> `ChromeManager::ensure_running` proved *attachable*, never *logged in* — a blank Chrome passed.
> 25 agents across three workflows: design → judge → 5 builders → integrate → 2 adversarial
> reviewers → 5 repairers → integrate → verify.

### G1. The design decisions worth keeping
- **"Fatal is never terminal."** No preflight outcome exits the process — F8 already proved `rc=0`
  makes a supervisor treat a config error as a clean shutdown and never restart. Fatal withholds
  *capabilities and leasing*, re-probes every 30s, self-heals. A red box stays up and keeps saying why.
- **The wizard blocks; the launch preflight informs.** A human at the wizard may be held until Chrome
  is signed in. A 4am unattended relaunch must never be blocked by a login classifier nobody has
  validated against a live session (linkedin `li_at` / x `auth_token` are exactly that).
- **Fatal checks probe MECHANISMS, never env-var names.** Token persistence is a real save/load/clear
  through whichever backend is configured; the LLM check uses the identical predicate
  `cli._build_run_io` raises on. An env-name check is wrong in one direction on a keyring box and the
  other on a local-Ollama box.
- **The preflight's own bugs are warnings.** `run_preflight` never raises; a test asserts a *raising*
  preflight leaves the box leasing at FULL capability.
- **CDP port resolved: 9333 is canonical**, unified across code + both docs + the warm script in one
  landing. Not a silent flip: an explicit wrong pin is a named fatal
  (`Chrome is on 9222 but this worker is configured for 9333`), an *unset* port prefers 9333 but still
  adopts 9222 with a logged receipt. No box that worked before stops working.

### G2. Bugs the LIVE proof caught that no reviewer or spec did
- **`_register()` could raise, defeating the entire point of check #1.** With an unwritable state dir
  the preflight correctly reported `state_dir_writable` fatal *with its remedy* — and the process died
  on the very next line inside `cfg.machine_id`, taking down the only channel that could have shown an
  operator that report, and handing a supervisor a crash-loop. **The check named the problem and
  changed nothing**, because parking happened *after* register. Now `_register` is a never-raising
  guard and a blocking preflight with no worker id parks instead of returning.
- **The probe could destroy a live credential.** An unconditional `finally: clear()` on the token
  round-trip turned "set `AIZU_SECRET_KEY` back and it works" into a hand-minted enrolment token and an
  operator visit (B10). `FernetFileBackend.save()` resolves the cipher *before* opening any file, so a
  box that merely lost the key raises with its blob intact — and the cleanup then unlinked it.

### G3. B4 shipped a FOURTH time — one hop further out each round
`fleet_readiness` grew `platforms=` and `server.py:3650` never passed it, so the F9.2 fix was dead in
production. Repaired server-side and proven over HTTP with a real socket… and it was **still inert**,
because `AgentReadinessOptions` carried only `refresh` and no shipped client could send `?campaign=`.
The narrowing was reachable by curl and by test, and unreachable by the product.
**The pattern, now four for four:** B4 (job spec → lease whitelist), E7 (fix keyed on an `op`
discriminator the panel never sends), F-10a (server never passes the argument), F-10b (no client sends
the parameter). **A fix is not done at the layer you edited — it is done at the layer a user reaches.**
Closed by threading it to `RunDrawer`, the one component where a campaign is genuinely in scope; the
global banner has none and correctly stays unscoped.
**Now six for six** (A11, A12): `warm_chrome.sh` — the launch site the docs actually hand operators —
went a whole round without the guard the other two launch sites got, and the round after that shipped a
wizard button that wrote a marker `resolve_chrome_binary` never read. Both were *correct* code in a
place no user's path runs through. When you finish a fix, name the exact click or command a user makes
and follow it to your edit.

### G4. Cheap checks that repaid the whole exercise
- `tsc` passed and `npm run lint` failed — TS narrows through a boolean const, so a `?.` I wrote was
  redundant under `@typescript-eslint/no-unnecessary-condition`. **Second time this session** that
  type-aware lint caught what `tsc` could not (see C5). Always run both.
- A **revert-check** on every new test: temporarily undo the fix, confirm the test fails, restore.
  Done for the redaction tests (2 failed reverted / 16 passed restored). A test that passes either way
  is documentation, not a gate.
- The rule-5 leak nobody flagged for a round: `check_dispatch_credential` interpolated
  `cfg.dispatch_base_url` into a `detail` that rides `to_upstream_wire()` to the cloud — in the module
  whose docstring promises **no secret values in any wire field**. A URL with `user:pass@` leaked to
  the fleet console. `_redact_userinfo` now strips userinfo and keeps the host readable.

### G5. A verifier finding that was WRONG — check the loop under the explicit calls
The final verifier claimed a GUI-managed box "cannot declare warming-only at all" because
`sidecar_supervisor.rs` has no `cmd.env("AIZU_WORKER_WARMING_ONLY", …)`. False: `read_worker_secrets`
is a **generic passthrough of every key in `worker-secrets.env`**, not a whitelist, so any worker flag
can be set there. It read the explicit `cmd.env` calls and missed the loop directly beneath them.
A comment now says so, to stop the next reader adding a redundant per-key line on the same reasoning.
**Adversarial reviewers are high-value and still wrong sometimes — verify their claims too.**

### G6. A correction I made to my own repair
I first demoted the LLM check to warn when `AIZU_WARMING_ENABLED=1`, reasoning that promoting an
existing field box from amber to fatal would dark it overnight. **Reverted: the test was right and the
edit was wrong.** A box with no LLM backend that leases harvest jobs *is* broken — every live job
dead-letters at attempt 5 with the cause never leaving the machine, which is F9.3 verbatim. Parking it
loudly with a named remedy is the whole purpose. The message now names which of the two
confusingly-similar flags (`AIZU_WARMING_ENABLED` vs `AIZU_WORKER_WARMING_ONLY`) the operator wants.

### G7. Still owed
- **The desktop Rust has never been compiled.** No cargo/rustc on this machine and CI does not build it
  either. The wizard — the half that runs on an operator's PC with no terminal — is source no compiler
  has read. `cargo check && cargo test` in `desktop/src-tauri/` is the first thing to run.
- **A wedged attach now demotes to `unknown [warn]`** rather than parking (rule 6). Deliberate, but it
  means B6/D3's degraded-Chrome case splits: "answers HTTP then errors" parks, "answers HTTP then
  hangs" does not, and surfaces at job time instead.
- `import aizu.worker.sidecar` still pulls Playwright via `core/pacing → core/human → core/pw_owner`,
  which imports the real `TimeoutError` on purpose. The "cheap to import on an API-only box" goal is
  met for `config`, `preflight` and `cli`, not for the module that actually runs the preflight.
- **A green preflight means "nothing known-broken on this machine", NOT "this box can harvest."**
  B6's live exit gate is untouched.

---

## H. Third live shakedown — the FIRST warmed-Chrome harvest (2026-08-19)

Two runs against a real, logged-in Instagram session on system Google Chrome at
`127.0.0.1:9333`, campaign `acme-saas-leadgen`, free OpenRouter tier. Run 1 exposed the
defects below; run 2 ran with the fixes. **Run 2 executed `_process_comments` against live
Instagram for the first time in this repo's history** — 12 real comments on reel
`DXaiAECDLYZ` (@the_construction_mentor), each scored by the cloud model. B6's remaining
question is no longer "does the code path work" but "does a comment ever clear 0.70".

Run 1 → Run 2: reels 12 → 25, relevance_passes 0 → 1, comments_scored **0 → 12**,
matches 0 → 0, health_flags **0 → 3**.

### H1. The per-reel deadline destroyed precisely the reels that PASSED relevance
**Symptom:** A run reports `status=completed`, exit 0, `matches=0`, `relevance_passes=0` and
**zero health flags**, while `seen_reels` holds a row with `relevant=1`. Indistinguishable
from a dry feed. Live: reel `DFdnoSsgWBk` (@planuppro) scored relevant 0.85 / confidence 0.90
and was discarded **in the same log second**.
**Root cause:** `engines/instagram/session.py` anchored `reel_start` BEFORE
`cascade.gate_reel()` and enforced `per_reel_seconds=90` AFTER `store.mark_seen(...)` but
BEFORE `if gate.relevant:` — so classification spent the budget its own docstring said
bounds the BROWSER block, and the `continue` landed before `open_reel` and
`_process_comments`. The slow path IS the escalation path, which fires on
borderline-but-genuine content, so the guard preferentially destroyed the likeliest leads.
`counters.relevance_passes` sat after the skip, which is why the summary could not show the
loss. Identical shape in `linkedin/session.py` and `x/session.py`.
**Made permanent by:** `store.is_seen` (`core/store.py:1538`) is a bare existence check with
NO TTL and nothing in `aizu/` ever DELETEs from `seen_reels`. `mark_seen` had already run, so
the reel is blacklisted for that campaign forever. **Every skip path that runs after
`mark_seen` is destructive, not deferred.**
**Fix:** Re-anchor the clock after the gate so the budget bounds browser work only; keep a
real interruption point at the one seam inside the block (after `open_reel`, before the
comment stage); count `relevance_passes` for every gate-relevant reel; and raise a new
`relevant_reel_discarded` soft flag on every post-`mark_seen` loss path in all three engines.
**How to avoid/detect:** A budget must be anchored where the thing it claims to bound BEGINS
— if a guard's docstring names one stage and its clock starts at another, that is the bug.
And a summary counter that sits after a `continue` cannot report the loss it was added to
report: assert the counter against the persisted row (`relevance_passes` vs
`COUNT(seen_reels.relevant=1)`), which is a cheap invariant and was violated on the first
live run.

### H2. Fixing H1 nearly traded a lost reel for a halted session (caught in review)
**Symptom:** Would have presented on the NEXT live run as a mystery `halted: stalled` with no
obvious cause.
**Root cause:** `last_activity_at` is bumped ONLY by `_flush()` → `store.update_counters`,
which the feed loop reaches once per reel, at the very end. Re-anchoring serialized gate +
`open_reel` + comment scoring behind that single heartbeat, and `session_watchdog.py` halts
any running session idle >180s. Run 1's log already contained a **186s gap** on the
159s-vision reel — already past the threshold BEFORE the change added `O + C` on top.
**Fix:** `self._flush()` between the gate and the browser block in all three engines. Pinned
by a test that asserts by ORDER (`trace.index("flush") < trace.index("open_reel")`), because
the injected monotonic clock does not move the wall-clock `last_activity_at` records — a
timestamp assertion would have passed vacuously. Verified to fail with the `_flush()` removed
(`trace == ['open_reel', 'flush', 'flush']`).
**How to avoid/detect:** When a fix makes two expensive stages run in sequence where only one
ran before, check every watchdog/heartbeat whose threshold was justified against the OLD
shape. `session_watchdog.py`'s comment literally cited `per_reel_seconds` as its justification
— the constant the fix redefined.

### H3. A 200 with no usable verdict was a SILENT confident reject
**Symptom:** 3 of 12 reels in run 1 came back `label=unknown score=0.00 confidence=0.00`,
logged as `Cloud relevance ✓`, rejected for good behind the TTL-free watermark, no flag.
**Root cause:** `core/router.py` fed `_extract_json` (returns `{}` on anything unparseable)
into `_decision_from_payload`, which defaults a missing label to `unknown` and a missing score
to `0.00` with `tier="cloud"`. `cascade._unsure` then misses all three arms — tier is not
`degraded`, 0.00 is below `escalate_band` lo, and 0.00 is far from the 0.70 threshold — so it
is never re-asked. Distinct from the already-handled `malformed 200: no usable choices`, which
correctly rides the retry ladder.
**Fix:** An unparseable/verdictless 200 raises inside the retry path and, if it persists,
returns the degrade path so `tier=="degraded"` — which makes the cascade escalate and raises
`cloud_degraded`. Kept the word "malformed" in the message so `_looks_like_param_rejection`
still returns False and JSON mode is not latched off.
**REVIEW CAUGHT:** the first version of the guard read label and score as an **OR**, so
`{"label":"unknown","score":0.0}` — byte-for-byte the shape the live run logged — hit the
score branch, coerced `0.0`, returned True, and rebuilt the exact Decision the fix existed to
eliminate. A stated label is now authoritative; the score branch is reachable only when
`label` is absent entirely. **A guard written from a log line must be tested against that
literal log line**, not against a paraphrase of it.
**Confirmed live:** run 2 raised 3 `cloud_degraded` flags where run 1 was silent, one reading
`raw={"label":"{"label":"irreleva…` — the model emitting nested broken JSON.

### H4. One global untagged reel queue inverted per-source attribution
**Symptom:** Run 1 logged `.../acme.io/reels/ · yielded=12` while **zero** of the 12
`seen_reels` authors was `acme.io`; all were project-management accounts matching the FIRST
hashtag.
**Root cause:** `core/cdp.py` holds a single process-wide `_reel_queue`; `_enqueue_reel` never
saw which source was being walked, and `walk()` drained with `pop(0)` under whatever url it
currently named. Reels intercepted during a redirected source's nav+settle were queued, the
fast-skip `continue` fired before that source's drain loop, and they surfaced later under a
different source's name.
**Worse than cosmetic:** that accidental drain was the ONLY thing keeping hashtag discovery
alive. A brief with `seed_accounts: []` and `include_home_feed` false would intercept reels on
every tag page, skip each source before draining, harvest ZERO, and still report `completed`
with no health flag.
**Fix:** `Reel.source` stamped at interception from a `_current_source` published under the
existing `_queue_lock`; the redirect path sets `scrollable = False` instead of `continue`, so
a redirected source drains what it queued without paying the scroll budget; `yielded=` counts
own-source reels with `carried_over=` beside it; and `walk()` warns if it returns with reels
still queued. Live in run 2: `projectmanagement · yielded=12 · carried_over=0`, then
`productivitytools · yielded=0 · carried_over=12`.

### H5. `/explore/tags/<tag>/` redirects are INTERMITTENT, not permanent
Run 1: all six tag sources 302'd to `/explore/search/keyword/?q=%23<tag>`. Run 2, same URLs,
~2h later: the first served a real reels grid and yielded 12 on its own. **Do not generalise a
platform-behaviour claim from one run** — the first reading here ("six for six, the hashtag
path is dead") was wrong, and would have justified a large unnecessary rewrite of discovery.

### H6. What this shakedown PROVED works — do not re-test
- The A12 per-brand profile split, end to end and in anger: `brand=chrome` →
  `profile=<base>/chrome`, legacy notice correctly silent after the operator move, and
  **cookies 15 → 16 across two full runs** (gained `rur`; lost nothing). A9 did not fire.
- CDP attach, interception wiring, reel parsing, the relevance cascade, vision escalation
  (`vision=True frames=3`), comment fetch, comment scoring, and the spend ledger.
- `cargo check` + `cargo test` on the desktop shell: clean, 81 tests. The ledger's "Desktop
  Rust is uncompiled" gap was stale — `cargo` was installed at `~/.cargo/bin`, just not on the
  shell's PATH.
- **Both hardcoded model defaults are dead ids.** `openrouter/owl-alpha` and
  `nex-agi/nex-n2-pro:free` (`router._DEFAULT_TEXT_MODEL`/`_DEFAULT_VISION_MODEL`, and the
  values `CLAUDE.md` documents) both 404 against the live `/api/v1/models` listing. Any box
  without the `engine/.env` pins latches its cloud leg off on the first call. Ledger D2 again.
- `max_source_seconds` is NOT broken (an in-session claim that it was is retracted): the check
  runs at the top of every iteration and `consumer_seconds` deliberately excludes time the
  cascade holds the generator. Run 2's 41 minutes were **18.5 min of model latency across 43
  free-tier calls**, plus dwell and scrolling — not a wedge.

### H7. Still open after this shakedown
- **B6 is NOT closed.** No comment has yet scored ≥0.70, so `matches` has never received a
  row. Every stage upstream of that is now proven live. The remaining variable is content:
  the brief hunts SaaS buyers while the reels reaching it are construction/PM, whose
  commenters discuss methodology, not procurement.
- Reels destroyed by H1 before the fix stay destroyed — the TTL-free watermark has no repair
  path. Clearing `seen_reels` for a campaign is currently a manual `DELETE`.
- `router.classify_image` and `generate_json` (panel-facing, AI campaign generate/interview)
  have the same unguarded shape H3 fixed for `classify_text`.
- Free-tier viability: text p90 ~42s, one 159s vision call, and `usd=$0.0000` on every call
  means `_spend_guard` can never engage.

---

## I. The first leads — Tashkent renovation campaign, fleet-executed (2026-08-20/21)

The campaign was authored end-to-end **through the panel UI** (signup → org → campaign, all
three classifier prompts pasted into the Advanced section), dispatched to a **worker**, and run
against the warmed Chrome. It produced the repo's first leads. Four defects stood between "every
stage is implemented" and "a lead lands", and none of them was the one the logs blamed.

### I1. `/reel/<code>/` bounces off the swipeable surface — the biggest single funnel loss
**Symptom:** `CDP open_reel bounced off the permalink (swipeable surface?) · reel=X landed=.../reels/Y/`
on most reels of every run. The engine asks for reel X and lands on reel Y, so
`_open_reel_landing_check` correctly refuses to read comments there and skips the reel.
**Root cause:** Instagram serves the SINGULAR `/reel/<code>/` into its swipeable `/reels/` feed,
which restores its own scroll position and drops the requested code. Measured live on four real
codes: `/reel/` landed on a different reel **3 of 4**; `/p/<code>/` landed on the requested code
**4 of 4**.
**Cost, measured across one day of runs:** 65 reels passed relevance → 22 bounced, 24 more
"unavailable" → only 47 comment fetches, 24 comments scored **in total**. ~70% of everything the
campaign correctly identified as on-campaign never reached the classifier.
**Fix:** `REEL_PERMALINK = "https://www.instagram.com/p/{code}/"`. `_CODE_IN_PAGE_URL` already
matched `/p/`, so attribution was unaffected. After: **0 bounces**.
**How to avoid/detect:** A "skip" that is logged as a warning and returns False is invisible in
every summary — the run still completes, still reports success, just with fewer leads. Count
the skip paths against the relevance count; if the ratio is not close to 1, the funnel is
leaking upstream of anything the scores can show you.

### I2. Fixing I1 silently broke comment retrieval — the dialog-scoped scroller
**Symptom:** After the permalink fix, `new=0 total=0` on **23 of 23** comment fetches. The funnel
was finally open and nothing came through it.
**Root cause:** `_scroll_comment_dialog` scoped its scroller lookup to `div[role=dialog]`. That
was correct for the modal `/reel/` opened and wrong the moment the permalink moved: `/p/`
renders comments **inline**. Measured on a post with 12 comments: `hasDialog: false`, yet a
page-level scroller of `scrollHeight 2172` vs `clientHeight 382` holding the real comment list.
**The second cost, which matters more:** returning False is also the branch that falls through to
the humanised **mouse-scroll**, which is the path the owner-thread wedge (H-series) lives on. So
the dialog-only lookup both silenced comments AND fed the wedge.
**Fix:** `document.querySelector("div[role=dialog]") || document.body`. After: a single reel
returned `new=15 total=15`, 26 comments scored in one session, **0 wedges**.
**How to avoid/detect:** When a navigation target changes, re-derive every DOM assumption that
depended on the old surface. A selector scoped to a container that no longer exists fails
CLOSED and looks exactly like "there was nothing there".

### I3. The log names an operation it did not observe — four hypotheses refuted at real cost
**Symptom:** `CDP scroll wheel timed out — trying JS fallback`, dozens of times, followed by
`the owner thread is still inside the hung wheel call`.
**Root cause:** `pw_owner`'s FAST-FAIL raises the same `PlaywrightTimeout` class as a genuine
deadline expiry, so that line prints when `page.mouse.wheel` was **never dispatched to Chrome at
all**. The real hang was `page.mouse.move`, whose failure `HumanSim.mouse_move` swallowed with a
bare `except Exception: return` — no log, no trace. "the owner thread is still inside the hung
wheel call" is inference from `is_wedged()`, which knows only that SOME call is abandoned.
**What it cost:** four hypotheses tested against the live browser and refuted — the Playwright
API, Chrome state rot / version skew, the swipeable surface, and `response.json()` on the owner
thread — plus a lock-order deadlock theory refuted by measurement (`queue_lock locked=False`).
Every one was aimed at the wheel because the log named the wheel.
**How to avoid/detect:** **An operation named in an error message is an ATTRIBUTION, not an
observation** — unless the code can prove it observed that specific call. Before chasing the
named operation, ask what the code actually knows. And never swallow a bounded call's failure
silently: `_focus` now logs at debug for exactly this reason, because a silent swallow is what
hid the original defect for five dead-lettered attempts.

### I0. NEVER put a lead's handle or comment text in this ledger — the repo is PUBLIC
Leads are private individuals. Their Instagram handle plus the verbatim comment they wrote is
personal data, and `github.com/orihero/aizu` is a public repository whose `main` branch is also
the DEPLOY TRIGGER — so committing it publishes it. The first draft of section I named two real
commenters alongside their messages, scores and extracted intent; it was caught before any
commit and redacted to `@lead-A` / `@lead-B`. The technical argument never needed the handles:
the funnel counts and the score gap (0.05-0.40 vs 0.78/0.82) carry all of it. Same rule for
reel authors where the account is a person rather than a business, and for anything pasted out
of `matches.text`. If evidence genuinely requires a real example, put it in a local scratch
file, never in a tracked one.

### I4. The config was never the problem — check the funnel before the thresholds
When the run produced no leads it was natural to suspect a too-tight brief. The data said
otherwise: relevance passed **65 of 106** (scores piled at 0.90/0.92), and of the 24 comments
ever scored, 2 cleared 0.70 and exactly ONE sat in the 0.60-0.69 near-miss band. A tight
threshold produces a CLUSTER of near-misses; there wasn't one. The shortfall was throughput —
24 comments scored all day — caused by I1 and I2 upstream.
**After the fixes, same brief, same threshold:** 37 reels → 27 relevant → 26 comments scored →
**2 leads** in one session, with a clean gap between 0.40 and 0.78 and nothing stranded below
the threshold.
**Leads:** `@lead-A` «Цена?» (0.78) and `@lead-B` «Можно узнать цену такого ремонта»
(0.82), both extracted `intent: price`. The first is the market's canonical two-word lead that
the Match prompt deliberately protects ("brevity must lower CONFIDENCE, never SCORE") — evidence
that the vertical-specific prompt work paid off.
**How to avoid/detect:** Before tuning a rubric, count the funnel. A threshold can only be judged
against a sample that reached it.

### I5. Panel-authored campaigns cannot express two brief keys
The UI has no control for `escalate_band` or `enable_stt`, so a campaign created in the panel
silently takes the dataclass defaults — `[0.4, 0.75]` instead of the intended `[0.4, 0.65]`.
Confirmed in the dispatched job spec. `include_home_feed` is fine (the seed-aware default
resolves to False). Ship a FILE brief, or patch the stored `campaign_briefs` JSON, when those
two knobs matter.

### I6. Source order is fixed, and accounts-only is worse than hashtags
`_sources()` is hardcoded `home → hashtags → accounts` with no priority knob, and the other
session's `_source_seeds` labels sources BY POSITION, so reordering it breaks their ledger.
Clearing `seed_hashtags` to reach the curated accounts first was tried and was **much worse**:
account `/reels/` grids wedged almost immediately (2 wedges in ~4 minutes, sessions halting at
3 reels) where hashtag grids completed 4 sessions over ~60 minutes. Account grids remain
unusable until the residual wedge is understood; do not repeat this experiment expecting a
different result.
