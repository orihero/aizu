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
engine couldn't attach, so it did no work but still completed cleanly.
**Fix / prerequisite:** A run needs a **healthy warmed Chrome, logged into the target platform,
on the CDP port** (9333 live), plus provider creds (`OPENROUTER_API_KEY`) in the worker's env.
This is the standing "live exit gate." See [engine-live-run notes] and CDP gotchas below (D3).
**How to avoid/detect:** Before blaming the pipeline, verify CDP attach works:
a real `connect_over_cdp('http://127.0.0.1:9333')` must succeed — HTTP 200 on `/json/version`
is NOT sufficient. Consider surfacing a non-zero `halt_reason` when Chrome can't attach so a
0-result run isn't silently reported as success.

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

### B8. Cross-tenant credential reach via the shared bootstrap token (OPEN)
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
**Fix direction (not done):** per-worker, single-use, revocable enrolment tokens instead of one
shared fleet token — so a worker's org scope is server-assigned at enrolment rather than
self-declared. Do NOT "fix" it by rejecting `cap_org=None`; that breaks the shipped desktop app,
which registers pool-wide by default (B1/B2).

### B9. Spend cap silently resets per worker box (OPEN)
**Symptom:** A campaign with a `--spend-cap` can spend up to the full cap again each time its
job lands on a different worker box.
**Root cause:** `core/router.py` checks `self.store.total_spend(campaign_id) >= self.spend_cap_usd`
against the **local** `spend_log` table. A fleet job is unpinned (pool-wide capability), so box B
has no rows for a campaign previously run on box A or on the server, sees $0, and permits a full
fresh budget. The cap is effectively per-box, not per-campaign.
**Fix direction (not done):** either roll spend up to the cloud on ack (alongside the existing
lead sync-back, which already proves the pattern) and gate against the authoritative total, or
ship the campaign's already-spent total in the job spec so the box starts from the right number.

---

## C. Local dev environment & deployment wiring

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
- **Shared fleet bootstrap token** (B8): one secret for the whole fleet, so any box holding it can
  lease any org's job and now reach that org's decrypted credential. Needs per-worker, revocable
  enrolment tokens.
- **Spend cap is per-box, not per-campaign** (B9): `total_spend` sums the local `spend_log`, so an
  unpinned fleet job gets a fresh full budget on each new box.
- **Live exit gate** (B6): a real `target_leads>=1` run producing leads on a warmed, logged-in
  Chrome has not been verified end-to-end on a worker box.
- **Windows/Linux packaging**: `.exe`/`.msi`/NSIS need a Windows host; code-signing/notarization
  (Apple Developer ID; Windows Authenticode/EV) unresolved; ad-hoc/unsigned chosen for the
  managed fleet (Gatekeeper `rejected` is expected — strip quarantine / distribute out-of-band).
