# Distributed CDP Workers + Superadmin Fleet Console — BUILD PLAN

**Status:** Build-ready (final) · **Date:** 2026-06-30
**Cross-reference:** [`docs/prd/distributed-workers-PRD.md`](./distributed-workers-PRD.md)
**Author:** Lead engineer (folds in three adversarial review passes: seam accuracy, BOLA/token security, DB/leasing feasibility)

This plan supersedes the draft. Every CRITICAL and HIGH review finding is folded in; cheap MEDIUMs are folded in inline; remaining MEDIUM/LOW items are tracked as in-phase tasks. **The schema namespace is corrected from v13 → v14** (v13 is already consumed by billing subscriptions).

---

## 1. Reality-check vs PRD

The PRD is directionally right (PULL model, ship-the-engine, local CDP). It is wrong or under-specified in seven load-bearing places. The corrections below are verified against the code at the cited lines.

### 1.1 CONFIRMED by the code (PRD is correct)

- **PULL fits the engine's shape.** `dispatch.run_engine_session(*, campaign, store, router, feed, soul, pacer, run_id, lead_target, engine_mode)` is **keyword-only** and receives a **pre-attached** feed; the caller owns feed construction/teardown (`dispatch.py:171-200`, `cli.py:302-322`). The engine is built to run beside its browser.
- **CDP attaches to a LOCAL Chrome** — `connect_over_cdp('http://127.0.0.1:9222')` (`core/cdp.py`). Ship-the-engine is sound; remote CDP would be wrong.
- **Feed must be released between sessions.** `_close_feed` is guarded — it no-ops on a FakeFeed and swallows teardown errors so cleanup never masks the run result (`cli.py:325-336`). A second live `sync_playwright().start()` while the first is live crashes; **every sidecar job builds a fresh feed and tears it down in `finally`.**
- **run_events streaming already exists**, keyed on `run_id`, emitted only when `run_id` is set (`engines/instagram/session.py:102-127`). The sidecar gets the activity feed for free by generating a `run_id` and passing it through dispatch — visible in the existing `/api/run/activity` drawer.
- **Migrations are additive + self-healing** — `CREATE TABLE IF NOT EXISTS` in the SCHEMA block + `_add_column_if_missing`; crash-safe replay keyed on legacy-table existence (`core/store.py:611-720`). New tables follow this pattern with no bespoke migration.
- **Secrets via Fernet** keyed on `REELRADAR_SECRET_KEY`; tokens hashed at rest (`core/secrets.py`, `auth.hash_session_token`, `core/store.py` auth_sessions). Per-worker bearer tokens reuse this exactly.
- **HTTP layer is declarative route-dispatch** with a two-layer gate (`server.py:1068-1099`), `{ok,data,error}` envelope, per-route body caps, BOLA-safe 404-not-403 via `_campaign_in_org` (`server.py:1164-1167`). New `worker_routes`/`admin_routes` dicts slot in beside `auth_routes`/`protected_routes`.

### 1.2 CONTRADICTED by the code — corrections that change the build

**C1 — SCHEMA VERSION COLLISION (CRITICAL, review #1).** `SCHEMA_VERSION = 13` is **already consumed by billing subscriptions** (`core/store.py:33`; `subscriptions` table at `:533`). **All new tables in this plan are `v14`.** `SCHEMA_VERSION` bumps to `14` when the workers/jobs/admin tables land.

**C2 — `SELECT … FOR UPDATE SKIP LOCKED` DOES NOT EXIST IN SQLITE (CRITICAL, all three reviews).** The store is `sqlite3.connect(..., timeout=30.0)` + WAL + `busy_timeout=30000`, single-writer (`core/store.py:604-608`). The existing `_tx` opens a **deferred** transaction (`core/store.py:806-815`): it takes only a READ lock until the first write, so **two workers can both `SELECT … LIMIT 1` the same queued row before either upgrades to a write lock.** The `rowcount==0` guard catches the race *after* it happens — necessary but not sufficient on its own. **Correction: add a dedicated `_tx_immediate` that issues `BEGIN IMMEDIATE` (write lock at statement one), used ONLY by `lease_one_job`.** Deferred `_tx` stays correct for every other path.

**C3 — The single-run lock is NOT the obstacle the PRD implies (CRITICAL→HIGH).** `RunManager._lock` is a `threading.Lock` guarding in-memory `_active`/`_active_proc` in one process (`runner.py`). **Correction: the sidecar bypasses `RunManager` entirely** and calls `dispatch.run_engine_session` in-process. There is nothing to "break" cross-process. The real invariant — **one account ↔ one box ↔ one live job** — is enforced by (a) the DB lease, (b) a capability pin, and (c) a local single-flight file lock. Phase 6's desktop app supervises the **sidecar process** (option A), never a CLI subprocess and never a second RunManager.

**C4 — `resolve_campaign` lives in `core.config`, not `cli` (CRITICAL phrasing fix, review #1).** It is imported into `cli` (`cli.py:25`) and called positionally `resolve_campaign(store, cfg_dir, args.campaign)` (`cli.py:509`). It **returns `None` for an unknown campaign** and **raises `ValueError` on a malformed brief** (`core/config.py:483-503`). The sidecar must check `None` explicitly and hard-nack — an unchecked next line crashes with `AttributeError`.

**C5 — `soul` is loaded from a file, and the sidecar must prove it exists (HIGH).** `_run_one` does `soul = load_soul(cfg_dir / "soul.md")` (`cli.py:502`). dispatch **requires** `soul`. **Decision (recommended): bake `soul` into the job `spec` as a string at enqueue time** so a missing/file-drifted soul fails fast in the cloud, not at runtime on a customer box. Phase 1 supports both paths behind one resolver (`job_runner._resolve_soul`).

**C6 — Pause-file is per-`run_id`, not per-job (HIGH/MEDIUM).** `RunManager._pause_path` keys the sentinel on `run_id` only — `run-{run_id}.pause` (`runner.py:288-296`), polled via `REELRADAR_PAUSE_FILE` (`core/pause.py:20-31`). A requeued job gets a NEW `run_id`, so a stale file won't carry over but an orphan poisons the next run. **Correction:** control truth lives in **heartbeat-response flags** (`drain`/`halt`); the pause-file is ONLY the engine's cooperative checkpoint, owned by the sidecar (created on demand, deleted in `finally`), and **swept on startup in Phase 1** (not deferred to Phase 4).

**C7 — Daytime guard cannot be pre-filtered at lease time (CRITICAL, review #3).** Daytime is enforced inside the engine loop against the account timezone. The warming kill-switch is checked **before** `_build_run_io` (`cli.py:294`), but **harvest attaches Chrome before any daytime check**. **Correction:** let the engine halt naturally with `halt_reason='daytime'`; the sidecar nacks with a **`retry_after_at` timestamp** (engine returns "try again at"), and the lease/queue scan skips jobs whose `retry_after_at` is in the future. For *warming*, pre-filter via the existing kill-switch before leasing.

### 1.3 DB engine bottom line

SQLite, WAL, single writer, 30s busy_timeout. Leasing is correct via `BEGIN IMMEDIATE` + conditional UPDATE + `rowcount` check, but **all worker writes serialize through one writer**. At PRD scale (1 box → ~10 companies, `safety_cap` 1–3) this is a non-issue. **Do NOT migrate to Postgres now.** Keep lease/ack windows tiny (batch run_events into heartbeat/ack); jittered backoff on lease miss avoids a thundering herd; document a measured write-throughput ceiling as the Postgres migration trigger.

---

## 2. Phase 1 — Worker engine sidecar + local pull loop (against a STUB dispatch)

> **STATUS: SHIPPED 2026-06-30** — `engine/reelradar/worker/` (config, job_runner, lease_client, single_flight, token_store, sidecar) + `engine/tools/stub_dispatch.py` + `engine/tests/worker/` (65 tests; full suite 1075 green). Adversarially reviewed (code + security); all HIGH/MEDIUM/LOW findings folded in. **As-built deviations from the plan below:** (a) `job_runner` REUSES `cli._run_session_loop` rather than re-cloning `_run_one` (DRY; inherits future engine fixes); (b) single-flight uses an atomic `O_CREAT|O_EXCL` lock, no `filelock` dep; (c) **in-process halt limitation** — a leased run can't be force-killed mid-session, so `halt`/`drain` are honored at the JOB BOUNDARY bounded by the per-job duration cap; true mid-run hard-stop needs the supervised-subprocess model (deferred). **Not yet verified:** the live exit gate (a real `target_leads=1` run on a warmed Chrome) — needs a worker box; the real-HTTP stub integration test covers only the register→lease→ack wire contract.

**Goal:** prove the shipped engine runs off-cloud, drives local Chrome, and completes a leased job round-trip *before* any desktop shell, real jobs table, or registry. Cloud-side is a stub. **Exit criterion:** one live job (`target_leads=1`) on a warmed local Chrome produces leads + run_events in the existing panel drawer, with zero remote-CDP traffic (PRD success metric 1).

### 2.1 New files

```
engine/reelradar/worker/
  __init__.py
  sidecar.py            # the pull loop + orphan sweep + heartbeat thread
  lease_client.py       # tolerant HTTP client → typed Result, never raises
  job_runner.py         # resolve a job spec → run ONE job in-process; sweep helper
  config.py             # WorkerConfig (frozen dataclass, env-driven)
  single_flight.py      # local per-account file lock guarding the Chrome
  token_store.py        # secure token persistence (NOT deferred — see review #2 C2)
tests/worker/
  conftest.py           # fake dispatch server fixture; live_smoke marker/skip
  test_job_runner.py
  test_sidecar_loop.py
  test_lease_client.py
  test_single_flight.py
  test_token_store.py
tools/stub_dispatch.py  # standalone stdlib ThreadingHTTPServer (throwaway)
```

### 2.2 `job_runner.run_one_job(store, job_spec, *, cfg_dir, base_args)` — the in-process seam

Does exactly what `cli._run_one` (`cli.py:277-322`) does, minus argparse and minus `RunManager`:

1. **Resolve campaign** — `campaign = resolve_campaign(store, cfg_dir, job_spec.campaign_id)` (`core.config`, called like `cli.py:509`). **`if campaign is None: raise CampaignNotFound(job_spec.campaign_id)`** — caught by the loop as a hard nack `reason='campaign_not_found'` (C4). A `ValueError` from a malformed brief → nack `reason='campaign_malformed'`.
2. **Resolve soul** — `soul = job_runner._resolve_soul(job_spec, cfg_dir)`: prefer `job_spec.soul_text` (baked at enqueue, C5); else `load_soul(cfg_dir / "soul.md")` (`cli.py:502`); raise `SoulMissing` if neither → hard nack.
3. **Generate `run_id = uuid4().hex[:12]`**; set `os.environ["REELRADAR_RUN_ID"] = run_id` **before** the call so `Session._emit` routes to `store.emit_run_event` (`session.py:102-127`).
4. **Pause-file** — derive `pause_path = log_dir / f"run-{run_id}.pause"` (mirror `runner.py:296`); set `os.environ["REELRADAR_PAUSE_FILE"] = str(pause_path)` (`core/pause.py`). Do NOT create it; the engine/sidecar create it only on a `halt`/`pause` flag.
5. **Build IO** — `router, feed, pacer = cli._build_run_io(campaign, store, dry_run, base_args, engine_mode)` (`cli.py:92-123`). Reused verbatim — this is the seam.
6. **Run** — `summary = dispatch.run_engine_session(campaign=campaign, store=store, router=router, feed=feed, soul=soul, pacer=pacer, run_id=run_id, lead_target=job_spec.target_leads, engine_mode=job_spec.engine_mode)` (`dispatch.py:171`). Halts fold into `summary['halt_reason']`/`halt_kind`; dispatch never raises for a halt.
7. **`finally:`** `cli._close_feed(feed)` (C-confirmed guarded teardown, `cli.py:325-336`) **and** best-effort `pause_path.unlink(missing_ok=True)` (C6). Both run even if step 6 raises.
8. **Return** the summary dict: `{session_id, reels_seen, matches, escalations, spend_usd, halt_reason, halt_kind, retry_after_at?}`.

`job_runner.sweep_orphan_pause_files(log_dir)` — glob `run-*.pause`, unlink any with mtime older than `2× heartbeat_interval` (mirror `runner.py` sweep). Called by `sidecar` **before the lease loop starts** (C6, review #3 HIGH).

### 2.3 `single_flight.py` (review #1 HIGH)

- Lock path: `<log_dir>/single-flight-{org_id}-{platform}-{account}.lock`.
- Acquire: `filelock` with a short timeout (1s) + bounded retry; if held, the job is **skipped** (re-leased later), never double-attached.
- Startup sweep: delete locks with mtime `> 2× heartbeat_interval`.
- Failure mode: a crashed sidecar's stale lock is reclaimed by the startup sweep; never a manual delete in steady state.

### 2.4 `token_store.py` (review #2 C2 — NOT deferred)

- macOS Keychain / Windows Credential Manager / Linux secret-service when available; else a **Fernet-encrypted file, mode `0600`** in a locked dir (reuse `core/secrets.py` Fernet, keyed on `REELRADAR_SECRET_KEY`). Never plaintext, never logged.
- Phase 1 ships the encrypted-file backend at minimum; keychain backends land in Phase 6 packaging. Token recovery after a sidecar crash is tested here.

### 2.5 `tools/stub_dispatch.py` — endpoint contracts (review #1 MEDIUMs)

Standalone `ThreadingHTTPServer`, no auth, in-memory queue seeded from a JSON file. **All responses are HTTP 200 with the `{ok,data,error}` envelope** (HTTP 204 has no body — explicitly NOT used):

- `POST /api/worker/register` — req `{display_name?, os, agent_version, max_sessions?}` → `{ok, data:{workerId, heartbeatIntervalSec:20}}`. Schema documented as a comment for Phase 2/3 porting.
- `POST /api/worker/lease` — req `{workerId, capabilities}` → `{ok, data:{job:{id, orgId, campaignId, platform, requiredAccountHandle, targetLeads, durationMinutes, engineMode, soulText?}, leaseToken, leaseExpiresAt}}` on success; `{ok, data:null}` when the queue is empty **or after a long-poll timeout (30s)** — HTTP 200, never 204.
- `POST /api/worker/jobs/{id}/heartbeat` — req `{job_id, run_events_after_seq}` → `{ok, data:{drain:false, halt:false, updateRequired:false, leaseExpiresAt, runEventsAckedThroughSeq}}`.
- `POST /api/worker/jobs/{id}/ack` — req `{job_id, summary}` → marks done, logs result.
- `POST /api/worker/jobs/{id}/nack` — req `{job_id, reason, retryAfterAt?}` → requeues.

Replaced by real `server.py` routes in Phase 3; deleted then.

### 2.6 `sidecar.py` — the loop

```
sweep_orphan_pause_files(log_dir)
sweep_stale_single_flight_locks(log_dir)
workerId, interval = register()           # parse heartbeatIntervalSec, store + use it
loop while not draining:
  result = lease()                        # tolerant Result; null → jittered sleep, continue
  if result.is_empty: sleep(jitter(0.5, min(2**attempt, 30))); continue   # backoff (review #3)
  job = result.job
  if not single_flight.try_acquire(job): continue        # held → skip
  try:
      start heartbeat_thread(interval):   # POST heartbeat; read flags; buffer/flush run_events
          on flags.halt  → terminate run + delete pause-file; nack(reason='halted')
          on flags.drain → finish current; stop leasing after
          on 3 consecutive heartbeat FAILURES → treat as halt: tear down, nack('heartbeat_failed')
      summary = job_runner.run_one_job(store, job, cfg_dir=cfg, base_args=base)
      if summary.halt_reason == 'daytime':   nack(job, retry_after_at=summary.retry_after_at)
      elif summary.halt_reason in POISON:    nack(job, reason=summary.halt_reason, backoff=long)
      else:                                  ack(job, summary)
  except CampaignNotFound:  nack(job, reason='campaign_not_found')
  except SoulMissing:       nack(job, reason='soul_missing')
  except Exception as e:    nack(job, reason=str(e))
  finally:
      stop heartbeat_thread; single_flight.release(job)
      # pause-file already unlinked inside run_one_job's finally (C6)
```

`lease_client` applies a tolerant parse on every response (strip → parse → validate → typed `Result`), per the LLM-JSON-output discipline — a malformed dispatch reply never crashes the loop.

### 2.7 Phase 1 test plan (AAA)

- **`test_job_runner`**: dry-run job + `cli._sample_feed` → summary with `matches>=0`, run_events emitted to an in-memory store.
- **`test_job_runner::closes_feed_on_exception`**: monkeypatch `dispatch.run_engine_session` to raise → assert `_close_feed` called once **and** pause-file unlinked (C6, review #1 MEDIUM).
- **`test_job_runner::campaign_not_found`**: stub `resolve_campaign`→`None` → `CampaignNotFound` raised, no `AttributeError` (C4).
- **`test_job_runner::dispatch_signature_lock`** (review #1 HIGH): parametrized over every platform's `run_session` — assert dispatch returns a dict with `{session_id, halt_reason, halt_kind}`. Regression gate against future engine signature drift.
- **`test_lease_client`**: malformed JSON, truncated JSON, HTTP 500, and `data:null` each → typed `Result`, never raises.
- **`test_single_flight`**: (a) second thread on same `(org,platform,account)` skips; (b) stale lock present at startup → swept and replaced.
- **`test_token_store`**: round-trip persist/read; recovery after simulated crash; file mode `0600`.
- **`test_sidecar_loop`** (integration): `stub_dispatch` on an ephemeral port, one seeded **dry-run** job → assert register→lease→run→heartbeat≥1→ack sequence in the stub call log; a `sessions` row + `run_events` land in the test DB; loop sleep honors the returned `heartbeatIntervalSec`.
- **`test_sidecar_loop::orphan_pause_swept`**: pre-create a stale `run-*.pause`, start sidecar → asserted gone before first lease.
- **Manual live smoke** (`pytest -m live_smoke --live`, off-CI): conftest skips unless `--live`; requires `REELRADAR_SECRET_KEY`, warmed Chrome on `:9222`, test DB. One live job, `target_leads=1` → leads + run_events in the panel. **This is the Phase 1 exit gate.**

---

## 3. Phases 2–6 (task-level)

### Phase 2 — Registry + heartbeat + presence

> **STATUS: SHIPPED 2026-06-30** — `workers` table at **v14** (v13 = billing) + store methods, the bearer-gated worker plane (`POST /api/worker/register`, `POST /api/worker/heartbeat`) and `GET /api/admin/fleet` in `server.py`, plus a worker-level presence-heartbeat thread in the sidecar. Adversarially reviewed (code + security); fixes folded. **Full suite 1124 green (+49).** **As-built notes:** (a) the Phase-2 worker heartbeat is **worker-level presence** (`/api/worker/heartbeat`) — the job-scoped `/api/worker/jobs/{id}/heartbeat` + run_events buffering stay Phase 3 (no jobs table yet); (b) **derived status** resolves the PRD §6-vs-§8 inconsistency toward §8 (`online ≤2×`, `stale ≤6×`, `offline >6×` of a 20s interval = 2-min offline), not the §6 `>8×`; (c) `/api/admin/fleet` uses an **interim fail-closed `REELRADAR_PLATFORM_ADMINS` env allowlist** — Phase 5 replaces it with the real platform_admins plane + MFA + audit; (d) worker tokens issued once at register, hashed at rest via `auth.hash_session_token`, request-time revocation/expiry check.

- v14 `workers` table (§5). `store.register_worker`, `store.touch_worker_heartbeat`, `store.list_workers`.
- `worker_routes` dict in `server.py` beside `auth_routes` (`:1068`); **bearer-token gate** `_request_worker()` mirroring `_current_user` (`:1137`): parse `Authorization: Bearer`, hash, look up `worker_token_hash`, **reject if `revoked_at IS NOT NULL` or expired** (review #2 HIGH — revocation is checked at request time, not learned only at next heartbeat).
- `POST /api/worker/register` returns the worker token **exactly once** (not per heartbeat); sidecar persists it via `token_store`. Heartbeat sends `Bearer <token>` (server hashes + compares).
- `POST /api/worker/jobs/{id}/heartbeat` — carries `{job_id, run_events_after_seq, load, chrome_health}`; response `{drain, halt, updateRequired, leaseExpiresAt, runEventsAckedThroughSeq}`. **Run_events buffering** (review #1 HIGH): sidecar buffers in memory (cap 1000), advances the low-water mark on `runEventsAckedThroughSeq`; on heartbeat failure, retries next interval.
- **Derived presence** (never stored): `online ≤2× interval`, `stale ≤4×`, `offline >8×`, computed server-side from `last_heartbeat_at` (mirrors the run-activity derive-on-read pattern; PRD §6).
- `GET /api/admin/fleet` minimal snapshot (placeholder gate; hardened in Phase 5).
- Sidecar: replace stub register with the real call; derive `workerId` from a stable machine fingerprint.
- Tests: presence transitions at interval boundaries (frozen clock); heartbeat updates only the worker's own row; token revocation → next request 401.

### Phase 3 — Real dispatch + leasing (SQLite-correct)

> **STATUS: SHIPPED 2026-06-30** — `_tx_immediate` (`BEGIN IMMEDIATE`) + the v14 `jobs`
> table + store leasing methods (`enqueue_job`, `lease_one_job`, `extend_lease`,
> `ack_job`, `nack_job`, `count_capable_workers`, `get_job`) in `core/store.py`; the
> bearer-gated worker routes `POST /api/worker/lease` + `POST /api/worker/jobs/{id}/
> {heartbeat,ack,nack}` and the interim-allowlist-gated `POST /api/admin/jobs/enqueue`
> in `server.py`; the sidecar's `_nack` now forwards a daytime `retryAfterAt`, and
> `_register` presents the shared `bootstrap_token` on first register (the no-auth stub
> had hidden that the real plane requires a bearer). `tools/stub_dispatch.py` +
> `test_stub_integration.py` DELETED; replaced by `test_real_dispatch_integration.py`
> (sidecar vs. the real server over loopback). Tests: `test_jobs_store.py` (22, incl.
> the N-thread concurrent-double-lease race → exactly one winner) + `test_jobs_server.py`
> (11). Full suite **1160 green (+33)**. **As-built notes / deferred to later phases:**
> (a) ~~lead/match BODIES are NOT synced back to the cloud~~ **CLOSED 2026-07-01 — lead
> sync-back SHIPPED.** On ack the worker now reads the leads its job captured from its
> LOCAL store (`store.matches_for_run(run_id)` — joins matches→sessions on the run_id
> stamped on every session, so it collects across ALL of a target-leads run's looped
> sessions and a multi-channel fan-out) and ships them in the ack body as camelCase DTOs
> (`sidecar._collect_leads`/`_lead_dto`; campaign OMITTED). `store.ack_job(..., leads=)`
> upserts each into the cloud `matches` table via `_sync_acked_leads`, **FORCING
> `campaign_id = job.campaign_id`** (org stamped from that campaign) — the BOLA guard: a
> worker can't inject a lead into another campaign/org whatever it claims. Best-effort +
> idempotent (leads sync only on the job-done transition; re-ack is a no-op; `upsert_match`
> preserves human status + a supplied `captured_at` on re-poll). `_validate_worker_ack`
> accepts an optional `leads` array (drops non-objects, caps at `MAX_SYNC_LEADS=500`);
> `WORKER_MAX_BODY_BYTES` 256KB→1MB. Security-reviewed (BOLA + auth SOUND, no SQLi, no
> CRITICAL/HIGH); hardening folded in: untrusted-value coercion (`_lead_str`/`_lead_float`),
> per-field string cap (`MAX_LEAD_STR_LEN`), oversized-`extracted` drop
> (`MAX_EXTRACTED_BYTES`), skip+warn when a job's campaign resolves to no org. +16 tests
> (`tests/worker/test_lead_syncback.py` + server/sidecar cases); full engine suite **1276
> green**. ~~**Residual (MEDIUM, documented):** the job-done commit and the lead upserts are
> not one transaction — a worker-cloud crash in the gap marks the job done but loses that
> batch.~~ **CLOSED 2026-07-02 — sync-back is now ATOMIC.** `_sync_acked_leads` was refactored
> to take the ack transaction's cursor and run INSIDE the same `_tx` as the job-done `UPDATE`
> (the match INSERT is extracted into `_upsert_match_row(c, …)`, shared with `upsert_match`).
> Either the job is `done` AND every lead is committed, or the whole ack rolls back and the
> job stays `leased` for ReclaimManager to requeue (pinned to the original box) — no
> `done-but-leads-lost` window, so the follow-up "re-sync sweep" is unnecessary. A per-row
> write failure now PROPAGATES (rollback → retry) instead of being swallowed — safe because
> every field is sanitized before the write, so a failure is genuine DB/infra trouble, not
> bad data; malformed rows are still skipped by pre-write validation. The `sessions` mirror
> stays best-effort OUTSIDE the tx (observational; losing it never loses lead data). +2 tests
> (`test_lead_syncback.py`: atomic-rollback + one-transaction happy path); full suite **1294
> green**. run_events sync via
> the job heartbeat is still deferred. (b) ~~`/api/run`
> still runs IN-PROCESS via `RunManager`~~ **RESOLVED 2026-07-01 — the in-process RunManager
> and the worker fleet are now INTERCHANGEABLE via a superadmin switch.** New v16
> `platform_settings` KV table (`SCHEMA_VERSION` 15→16) holds `execution_backend` =
> `in_process` (default) | `distributed`; `store.execution_backend()` falls back to the safe
> default on a corrupt value. Admin plane: `GET/POST /api/admin/execution-backend`
> (`_require_admin`-gated; POST audited as `execution_backend.set`). `_handle_run` reads the
> backend after the billing gate + lead-cap clamp: a LIVE run in distributed mode routes to
> `_dispatch_run_to_fleet` (enqueue one job for scope=campaign; one per live campaign with a
> capable worker for scope=all, incapable ones reported as `skipped`, 409 if none) —
> **governing org users' Run button too**. DRY runs always stay in-process; the
> `run_manager is None` 503 guard moved to just before `run_manager.launch`. Admin panel:
> confirm-gated `ExecutionBackendCard` on the Fleet page. Suite **1291 engine / 396 FE green** (code-reviewed; 2 HIGH fixed).
> scope=all mirrors the CLI runnable predicate (`status='live'` AND not archived) and SPLITS
> the billing-clamped lead budget across dispatchable jobs via `_split_lead_budget` so the
> fleet total can't exceed the period cap (no N× multiplication); a per-campaign enqueue
> failure skips that one, never stranding the batch. Caveats: a rapid double-Run enqueues two
> job sets (fresh uuid, no cross-request dedup — as the in-process 'all' path also lacks); a
> fleet-routed run's live activity feed isn't wired to the org drawer yet (results arrive via
> ack + lead sync-back). (c) lease reclaims an
> EXPIRED lease directly at lease time; the proactive offline→interrupted→requeue sweep +
> pin-to-original-worker + alerting is Phase 4. (d) job heartbeat returns placeholder
> control flags (`drain`/`halt`/`updateRequired` all false unless the lease was lost,
> which returns `halt:true`); real control-flag source of truth is Phase 4. (e) run_events
> buffering through the job heartbeat is still deferred (part of the lead-sync-back gap).

- **`_tx_immediate` lands first** (C2): add `def _tx_immediate(self) -> Iterator[sqlite3.Cursor]` right after `_tx` (`core/store.py:~816`); it issues `self._conn.execute("BEGIN IMMEDIATE")` before `yield`, commits/rollbacks identically. Used **only** by `lease_one_job` and the heartbeat lease-extension UPDATE. Documented as a correctness trap.
- v14 `jobs` table (§5). `store.enqueue_job`, **`store.lease_one_job(worker_id, capabilities)`**: `BEGIN IMMEDIATE` → `SELECT … WHERE status='queued' AND (org,platform,account) IN capabilities AND (lease_expires_at IS NULL OR lease_expires_at < now) AND (retry_after_at IS NULL OR retry_after_at < now) ORDER BY created_at LIMIT 1` → `UPDATE … SET status='leased', leased_by=?, lease_expires_at=? WHERE id=? AND status='queued'` → commit; `rowcount==0` ⇒ retry/backoff.
- `POST /api/worker/lease` (long-poll loop with short sleep up to 30s, returns `data:null` on empty), `ack`, `nack` (`reason`, optional `retry_after_at`; bounded `attempts`/`max_attempts` → `dead_lettered_at`).
- **Enqueue validation** (review #2 MEDIUM): reject (400) a job whose `(org,platform,account)` has no registered worker capability — prevents orphaned jobs.
- **Lease extension math** (review #1 + #3 HIGH/MEDIUM): heartbeat extends `lease_expires_at = now + max(60s, heartbeat_interval × worst_case_multiplier)` **only while `status='running'`**; final on ack/nack/interrupted. Extension uses `_tx_immediate`; a contended-write failure is treated as transient (retry next interval), escalating to nack only after N consecutive failures — never reassigns a job for slowness.
- ack writes the `sessions` row and flushes counters via `store.update_counters` (reuse, don't reinvent).
- **Daytime/warming integration** (C7, review #1 MEDIUM): for `engine_mode='warming'`, `job_runner` checks `warming_control.warming_kill_reason` before `_build_run_io` and nacks `reason='warming disabled for platform'`; for harvest, the engine's natural daytime halt drives `retry_after_at`.
- **Backoff** (review #3): jittered exponential in `lease_client` — `random.uniform(0.5, min(2**attempt, 30))`.
- Replace `tools/stub_dispatch.py` with the real routes; delete it.
- Tests: **concurrent double-lease** (N threads, 1 job → exactly one winner, rest `null`); lease expiry → requeue; ack idempotency; nack backoff/dead-letter; warming-disabled nack; daytime `retry_after_at` skip.

### Phase 4 — Lifecycle controls

> **STATUS: SHIPPED 2026-07-01** — Control flags are now the source of truth, worker
> tokens are long-lived + revocable + version-gated, and stranded jobs are reclaimed.
> Full suite **1197 green (+37)**. What landed:
> - **Control flags (C6):** net-new v14 `control_flags` table (scope `global|org|platform|
>   worker`, self-heals via `CREATE IF NOT EXISTS`) + `store.set_control_flag`/`clear_
>   control_flags`/`list_control_flags`/`resolve_control_flags` (OR-merge across every
>   applicable scope). Both the WORKER-presence heartbeat and the JOB heartbeat now return
>   RESOLVED `{drain,halt,updateRequired}` instead of hardcoded false — the job heartbeat
>   resolves for global+job.org+job.platform+worker, presence for global+org+worker+its
>   capability platforms. Interim-allowlist-gated `POST /api/admin/control-flags` (set/
>   clear) + `GET /api/admin/control-flags` (list). `halt` also fails the in-engine warming
>   gate (`warming_control.warming_kill_reason` layer 3, defense in depth).
> - **Token lifetime + version gate + revoke:** register now stamps a 1-year TTL
>   (`WORKER_TOKEN_TTL_SEC`; revocation, not expiry, is the real off-switch — PRD §7);
>   `REELRADAR_MIN_AGENT_VERSION` gate sets `updateRequired` in both heartbeats
>   (`_agent_version_below`); interim-gated `POST /api/admin/workers/revoke`.
> - **Atomic offline→interrupted→requeue:** `jobs.pinned_worker_id` column (additive self-
>   heal) + `store.reclaim_offline_jobs` under `_tx_immediate` (reclaims ONLY expired-lease
>   jobs of OFFLINE workers → requeue PINNED to the original box with backoff, or dead-
>   letter when exhausted; raises a `worker_offline` health_flag past 5 min). `lease_one_job`
>   honors the pin (no cross-box failover) + clears it on lease. New `ReclaimManager` daemon
>   (30s tick) injected into `serve()` like `ScheduleManager` (default path only).
> - **As-built notes / deferred:** (a) the sidecar's PRESENCE thread still ignores flags by
>   design (LOCKED #9) — `drain`/`halt` act via the JOB heartbeat, so an IDLE worker keeps
>   leasing until its next job's heartbeat carries the flag (true mid-run hard-stop still
>   needs the Phase-6 supervised-subprocess model — halt is honored at the job boundary).
>   (b) A permanently-dead box's pinned job stays queued (never dead-letters via reclaim, as
>   reclaim only touches leased/running) — surfaced by the >5-min alert for manual reassign.
>   (c) Phase 5 still owns replacing the interim `REELRADAR_PLATFORM_ADMINS` gate on the new
>   admin routes with the real platform-admin plane + audit.

- Heartbeat-response **control flags** (`drain`, `halt`, `update_required`) as control source of truth (C6). `control_flags` storable per scope (`global|org|platform|worker`). `halt` → immediate engine/feed teardown + pause-file delete; `drain` → finish current, stop leasing.
- Wire `halt` to the existing warming kill-switch so a halted platform also fails the in-engine gate (defense in depth).
- Per-worker bearer tokens minted + revoked (`revoked_at`); **long TTL (1 year / until-revoked, NOT the 30-day human session TTL)**. Stale-agent version gate returns `update_required` (no silent failure).
- **Atomic offline→interrupted→requeue** (review #2 MEDIUM): a sweep in `_tx_immediate` — `SELECT jobs WHERE leased_by=? AND <worker offline >2min>` → `UPDATE status='interrupted', leased_by=NULL` in one tx; requeue **pinned to the original worker** (one account ↔ one box, no cross-box failover); alert if offline >5min.
- Sidecar startup orphan sweep already lives in Phase 1; Phase 4 adds the server-side reclaim path.
- Tests: induced worker drop mid-job → interrupted → requeued to same worker, never double-actioned (PRD success metric 3); heartbeat-failure→halt path; sweep race (crash → reclaim → old worker returns → no double-action).

### Phase 5 — Superadmin plane (review #2 CRITICAL — full spec required before start)

> **STATUS: BACKEND SHIPPED 2026-07-01 (5a–5d); React panel 5e DEFERRED.** Schema bumped
> **v14 → v15** (v14 consumed by workers). Full suite **1260 green (+60)**. Adversarially
> security-reviewed (no CRITICALs); every substantive finding folded in. What landed:
> - **5a — data-layer tenant isolation:** `store.campaign_in_org(campaign_id,
>   effective_org_id)` is the ONE composite tenant filter (fail-closed on a None org);
>   `server._campaign_in_org` + the raw request-boundary `org_for_campaign` uses now route
>   through it. Behaviour-preserving for org users; the seam impersonation threads the
>   effective org through.
> - **5b — admin auth plane:** new `reelradar/admin_auth.py` (stdlib TOTP RFC-6238 +
>   IP-allowlist fail-closed + admin session TTL); v15 `platform_admins` +
>   `platform_admin_sessions` + `admin_audit_log` + `admin_login_throttle` +
>   `admin_totp_used`; a SEPARATE `_current_admin` gate (IP-allowlist FIRST, then admin
>   cookie `rr_admin_session`) resolved before the org gate; `/api/admin/{login,logout,
>   whoami}`; PBKDF2 reuse + mandatory TOTP + DB-backed throttle; `python -m
>   reelradar.admin_bootstrap` seeds the first admin (MFA secret Fernet-encrypted).
> - **5c — impersonation + audit:** `/api/admin/impersonate{,/end}` stamp the effective
>   principal on the admin session; `_current_user` falls back to `_impersonated_user`
>   ONLY when a real org session is absent AND an effective principal is set — so existing
>   per-org endpoints serve the target org unchanged (an admin who has not started
>   impersonating has NO org identity; fail-closed). Every admin action is appended to a
>   SHA-256 hash-chained `admin_audit_log`; `GET /api/admin/audit{,/verify}`.
> - **5d — cross-org reads + real gate:** `GET /api/admin/orgs` + `/api/admin/orgs/{id}/
>   {campaigns,leads}` (read-only, reuse the org builders with the target org). The interim
>   `REELRADAR_PLATFORM_ADMINS` env allowlist gate on fleet/enqueue/control-flags/revoke is
>   REPLACED by the real `_require_admin`; the dead `_is_platform_admin` was removed.
> - **Security hardening (from review):** TOTP anti-replay (`claim_totp_counter` consumes
>   the matched step counter); `append_admin_audit` uses `_tx_immediate` (no forked chain
>   under concurrency); `verify_admin_audit_chain` streams (no unbounded fetchall);
>   impersonate returns 409 (no ghost audit) if the session lapsed; logout is IP-allowlist
>   gated + logs housekeeping failures; audit hash has a domain separator.
> - **As-built notes / deferred:** (a) `_client_ip` uses the transport peer, NOT
>   X-Forwarded-For (spoofable) — behind a reverse proxy the allowlist must list the proxy.
>   (b) MFA is TOTP-only (WebAuthn later, per plan).
>
> **5e — React `/admin` panel: SHIPPED 2026-07-01.** A SEPARATE frontend plane in
> `admin-panel/` (NOT org RBAC — its own `useAdminAuth`/`RequireSuper` gate bootstrapping
> `GET /api/admin/whoami`, its own `rr_admin_session` cookie, its own `AdminLayout` rail so
> org-scoping can't leak). Admin methods live on the existing `PanelRepository` interface but
> strictly on the `/api/admin/*` branch, behind the never-throw `Result` boundary + zod
> schemas (`shared/schemas/admin.ts`; snake_case `update_required`/audit rows/org index rows
> normalised to camel via `.transform`). Route subtree is a top-level sibling of the org tree
> in `router.tsx` (`/admin/login` under `RedirectIfAdmin`; fleet/orgs/orgs/:id/audit under
> `RequireSuper`→`AdminLayout`). Pages: **FleetPage** (worker table w/ derived presence
> badges + revoke-with-confirm + control-flags card set/clear across scopes + enqueue-job
> modal), **OrgsPage**+**OrgDetailPage** (cross-org index → read-only campaigns/leads +
> reason-gated impersonation), **AuditPage** (hash-chained log + "Verify chain" →
> intact/tamper banner). Global `ImpersonationBanner` in the layout w/ one-click End. 10 new
> tests (`features/admin/admin.test.tsx`) via a `renderAdmin` harness + `FakePanelRepository`
> admin seam; full FE suite **395 green**, tsc + eslint clean (bar 2 pre-existing unrelated
> tsc errors in `roles.test.ts`/`CampaignCard.lifecycle.test.tsx`). **Deferred:** (a) no nav
> link INTO `/admin` from the org app (reach it by URL, by design — plane separation);
> (b) impersonation is start/banner/end within the console — actually *driving* the org app
> under the impersonated identity needs the org `AuthProvider` to re-bootstrap (cross-plane
> hand-off is a follow-up); (c) fleet snapshot still unenriched (no per-worker current-job).

**Original spec (for reference / the 5e panel):**

- v14 `platform_admins` + `platform_admin_sessions` (parallel to `users`/`auth_sessions`). **Separate gate** — `admin_routes` dict resolved before the org session gate; **never `OR role='superadmin'` in the data layer** (PRD §10 BOLA rule).
- **Data-layer isolation (the load-bearing fix):** promote the static `_campaign_in_org` (`server.py:1164-1167`) to an instance method `store.campaign_in_org(cid, effective_org_id)`; **remove `store.org_for_campaign` from the public call sites** and route all 5+ raw uses (`server.py:1319,1345,1381,1419,1476,1518,1928`) through it. Audit every cross-org query method (`per_campaign_rollup`, `list_campaign_meta`, etc.) to inject `effective_org_id`. The composite tenant filter (`WHERE id=? AND org_id=:effective_org`) lives in one repository helper handlers can't forget.
- **Impersonation:** `POST /api/admin/impersonate {org_id|user_id, reason}` / `…/end` writes the **effective principal** into the server-side admin session (`effective_org_id`/`effective_user_id`); existing per-org endpoints serve unchanged; exactly one sanctioned place sets a foreign org.
- **MFA (TOTP for v1, WebAuthn later) + IP-allowlist** in the admin gate; admin `LoginThrottle` moved to DB so lockout survives restart.
- Cross-org reads: `GET /api/admin/orgs`, `…/orgs/{id}/{campaigns,leads}`, `/api/admin/fleet` (full snapshot: last-seen age, current job, throughput, account-warmth, ban signals).
- **`admin_audit_log`** append-only + **hash-chained** (review #2 HIGH — algorithm specified): `row_hash = SHA-256(prev_hash || json.dumps(row, sort_keys=True, separators=(',', ':')))`. A `GET /api/admin/audit/verify` endpoint walks the chain and reports the first mismatch; tampering appends an alert row. Every admin read/write audited incl. `ip`, `user_agent`, `reason`, impersonation start/end. ≥12-month retention; defensive emit (catch-and-log, never crash the request).
- React panel (`admin-panel/`): `/admin` subtree behind `RequireSuper` (mirror `RequireCan`); new `PanelRepository` methods (`fetchFleetStatus`, `fetchWorkerDetail`, `stopJob`) on a **separate `/api/admin/*` branch** (org-scoping must not leak in); `useFleetStatus` polling hook (copy `useRunStatus` cadence); `JobActivityFeed` reuses `RunActivityFeed`; Recharts via `useChartPalette`. Polling, not SSE.
- Tests: cross-org access → 404 not 403 (**with an actual breach attempt**); impersonation fully reconstructable from the audit chain; hash-chain tamper detection (inject corrupted row → `verify` flags it).

### Pre-Phase-6 gap closure — SHIPPED 2026-07-02

> All deferred cross-phase gaps closed before Phase 6. Backend **1322 green**, FE **404
> green**. Each gap TDD'd. What landed:
> - **A · Fleet run live activity feed (run_events sync):** run_id is assigned at ENQUEUE
>   (`_dispatch_run_to_fleet`), carried in the job `spec.run_id` → `JobSpec.run_id` → the
>   worker runs under it (not a locally-generated id) and the job heartbeat SHIPS new local
>   run_events each beat (`_HeartbeatThread` opens its OWN Store connection in-thread — WAL
>   allows the read alongside the job thread's writes). Server `_sync_job_run_events` →
>   `store.sync_run_events` inserts them under the JOB's run_id/org/campaign (FORCED, BOLA),
>   idempotent on `(run_id, session_id, seq)`. `/api/run` returns `runId`/`runIds`; the
>   RunDrawer polls the returned fleet runId so the org drawer shows the run live.
> - **B · Double-Run dedup:** `store.enqueue_job_deduped` (exists-check + insert in one
>   `_tx_immediate`, no TOCTOU) skips a campaign already `queued|leased|running`; the fleet
>   dispatch uses it (second click → 409, never a duplicate job set). Admin manual enqueue
>   stays un-deduped.
> - **C · Idle/draining worker stops leasing:** the PRESENCE heartbeat response's
>   drain/halt now sets a sidecar `stop_leasing` Event (`apply_presence_flags`) the lease
>   loop checks at the top — an idle box reacts without waiting for a job's heartbeat. Gates
>   NEW leasing only; a running job stays governed by its job heartbeat (LOCKED #9 intact).
>   Mid-run hard-stop still needs Phase 6's supervised-subprocess model.
> - **D · Pinned-to-dead-box dead-letter:** `reclaim_offline_jobs` gained a second pass —
>   a QUEUED job pinned to a box dark past `WORKER_PINNED_DEAD_LETTER_SEC` (1h, ≫ the 5-min
>   alert) is dead-lettered + alerted (never un-pinned/failed-over — the one-account↔one-box
>   invariant holds). Closes the forever-queued case.
> - **E · Trusted-proxy XFF:** `admin_auth.effective_client_ip` honours X-Forwarded-For
>   ONLY when the peer is a configured `REELRADAR_TRUSTED_PROXIES` proxy (rightmost
>   non-proxy hop); default (unset) ignores XFF = the prior transport-peer behaviour.
> - **F · Impersonation cross-plane hand-off:** `/api/auth/me` now reports
>   `impersonated:true` under an active impersonation (`_shape_user`; `id` may be null for
>   org-level). Admin console gained an "Open workspace" button (full-nav to `/` so the org
>   AuthProvider bootstraps under the impersonated identity); the org app shows an
>   `ImpersonationBanner` with "Exit" (ends impersonation + full-nav back to `/admin/orgs`).
> - **G · Fleet snapshot enrichment:** `list_workers` carries each box's `currentJob`
>   `{jobId,campaignId,platform,status,runId,leaseExpiresAt}` (one query, no N+1); the admin
>   FleetPage WorkerTable shows a "Running" column (campaign/platform/status, or idle).
>
> **STILL open (genuinely Phase-6 / hardware-blocked, NOT closeable here):** mid-run hard
> stop of a leased job (needs the supervised-subprocess model), and live exit-gate
> verification (a real `target_leads=1` run on a warmed Chrome on a worker box).

### Phase 6 — Desktop app (Tauri) + packaging + Tailscale ops

> **STATUS 2026-07-02 — Python CORE SHIPPED + reviewed; Tauri/packaging SCAFFOLDED (uncompiled).**
> The buildable, testable substance of Phase 6 landed and is green (engine **1405 passed**,
> was 1322). What shipped, TDD'd + adversarially reviewed (11 findings, all fixed incl. one
> CRITICAL):
> - **6-core-A · True mid-run hard-stop (supervised subprocess).** Each leased job now runs in a
>   KILLABLE child (`reelradar.worker.job_child`, `python -m …`) supervised by the new
>   `job_runner.run_one_job`: it writes a 0600 spec file, spawns the child, polls the shared
>   `Controls.halt` Event + a wall-clock deadline, and does SIGTERM→grace→SIGKILL — closing the
>   old "halt only at the job boundary" gap. `_execute_job` is the old in-process body verbatim
>   (runs inside the child; in-process tests repointed to it). A crash → `ChildCrashed`; an
>   unrunnable job → mapped exception via a JSON result file. `sidecar._run_and_report` passes
>   `halt=controls.halt`; a `finally` GUARANTEES no live child survives any exit path, and a
>   startup `reap_orphan_children` (child PID file + `os.kill` liveness) kills a whole-crash
>   orphan before leasing — the one-account↔one-box↔one-live-job invariant holds.
> - **6-core-B · Managed Chrome lifecycle** (`worker/chrome_manager.py`): idempotent
>   reconnect-or-launch via a REAL `connect_over_cdp` probe (HTTP 200 alone is insufficient),
>   never-spawn-a-second, never-kill-what-we-didn't-launch, cross-platform focus seam
>   (macOS real; Win/Linux stubs). Fully DI — no real Chrome in tests. Port policy (9222 vs
>   live 9333) left to the wiring layer.
> - **6-core-C · Loopback-only control surface** (`worker/control_surface.py` + `control_state.py`
>   + `chrome_probe.py`): opt-in (`REELRADAR_CONTROL_SURFACE=1` + `REELRADAR_CONTROL_TOKEN`),
>   127.0.0.1-only, Bearer-gated `GET /status` + `POST /command` (pause/resume/stopCurrentJob/
>   focusWarmedChrome). `stopCurrentJob` now = a REAL hard-stop (feeds `controls.halt`). Sidecar
>   gained lock-guarded `current_job` + `pause/resume`. Leak-safe wire DTO.
> - **6-core-D · Per-job logs**: fell out of 6-core-A — the child gets `REELRADAR_RUN_ID` so the
>   existing spawned-run `run-<run_id>.log` fires; added `logsetup.run_log_path` + an orphan-file
>   sweep. RedactingFilter now scrubs tracebacks (`exc_info`/`stack_info`) too.
> - **6-core-E · Keychain tokens**: `TokenStore` is a façade over a pluggable Fernet-file +
>   OPTIONAL `keyring` backend (`token_backends.py`, guarded import like Playwright);
>   `KeyringBackendError` subclasses `SecretCipherError` so sidecar needs ZERO change; env
>   `REELRADAR_TOKEN_BACKEND=keyring|file|auto`.
>
> **SCAFFOLDED, NOT COMPILED (no Rust/tauri-cli/pyinstaller in the dev sandbox):** `desktop/`
> (Tauri 2.x `src-tauri/` supervising the `reelradar-worker` console-script binary + managed
> Chrome + a thin UI talking to the control surface), `desktop/pyinstaller/sidecar.spec`,
> `pyproject.toml` `reelradar-worker` entry point, `docs/ops/desktop-packaging.md` (build steps +
> blocker list). First step for a toolchained engineer: `cargo check` + `pyinstaller --clean`.
>
> **STILL genuinely blocked (toolchain/account/hardware):** code-signing/notarization (Apple
> Developer ID; Windows Authenticode/EV), Tauri updater keypair + endpoint, real Windows/Linux
> `focus_window`, and the live warmed-Chrome exit-gate + warming/daytime-guard-survive-packaging
> verification on both OSes.

- **Tauri** (Rust + system webview — not Electron/bundled Chromium; the app *manages* a separate warmed Chrome via CDP) supervising the Phase-1 sidecar as a managed **child process** (C3 option A): restart-on-crash watchdog, run-at-login. **No RunManager, no CLI subprocess.**
- **Managed Chrome lifecycle** (review #3 LOW): Tauri starts a Chrome on launch; sidecar connects via CDP `:9222`; on app exit Tauri kills Chrome; on sidecar restart it reconnects to the existing Chrome (never spawns a second). Tested on macOS + Windows.
- Engine packaged as a sidecar binary (PyInstaller/pex, pinned runtime) reusing `reelradar.cli`/`dispatch`/`core/cdp`; **`soul.md` bundled** if not baked into job specs (C5).
- Local UI: per-account health, **checkpoint/2FA/captcha button that focuses the warmed Chrome window**, start/stop/pause, live log tail (per-job log files keyed on `job_id`/`run_id` — review #1 LOW on interleaved logs), proxy/connection status, capacity override.
- Token storage promoted to OS keychain backends (`token_store` from Phase 1).
- Signed + notarized **Mac (Developer ID + notarization)** and **Windows (Authenticode/EV)** installers; built-in Tauri updater pulling signed builds; `agent_version` wired to the Phase-4 update gate.
- Tagged Tailscale enrollment + ACLs for operator SSH/RDP only — **never the job channel** (PRD §2).
- Verify warming + daytime-guard survive packaging (PRD risk 2 — highest-effort unknown; test on both OSes early).

---

## 4. (reserved)

---

## 5. New schema (v14) intent

Additive self-healing: `CREATE TABLE IF NOT EXISTS` in the SCHEMA block + `_add_column_if_missing` for later additions; timestamps as **REAL epoch** (matching `sessions`/`run_events`); **`SCHEMA_VERSION → 14`**.

**`workers`**
- `id` TEXT PK (stable machine fingerprint), `org_id` INTEGER, `display_name`, `host`, `os`, `agent_version`
- `last_heartbeat_at` REAL, `registered_at` REAL, `max_sessions` INTEGER, `current_sessions` INTEGER
- `capabilities` TEXT (JSON array of `[org_id, platform, account_handle]`)
- `worker_token_hash` TEXT (SHA-256 at rest), `token_expires_at` REAL NULL, `revoked_at` REAL NULL
- *(status NOT stored — derived from `last_heartbeat_at`)*
- Index on `(org_id)`; capability lookups scan the JSON at PRD scale.

**`jobs`**
- `id` TEXT PK, `org_id` INTEGER, `campaign_id` TEXT, `platform` TEXT, `required_account_handle` TEXT
- `spec` TEXT (JSON: target_leads, duration_minutes, engine_mode, brief ref, **soul_text**)
- `status` TEXT CHECK in (`queued|leased|running|done|failed|interrupted`)
- `leased_by` TEXT NULL (→`workers.id`), `lease_expires_at` REAL NULL, `retry_after_at` REAL NULL
- `attempts` INTEGER DEFAULT 0, `max_attempts` INTEGER, `dead_lettered_at` REAL NULL
- `result` TEXT NULL (JSON), `session_id` TEXT NULL (→`sessions.session_id`)
- `created_at` REAL, `updated_at` REAL
- Index: `(status, required_account_handle)` for the lease SELECT; partial index on `status='queued'`.

**`admin_audit_log`** (append-only, hash-chained; distinct from v9 `audit_log`)
- `id` INTEGER PK AUTOINCREMENT, `prev_hash` TEXT, `row_hash` TEXT
- `acting_admin_id` INTEGER, `action` TEXT, `target_org_id` INTEGER NULL, `target_user_id` INTEGER NULL, `target_resource` TEXT NULL
- `at` REAL, `ip` TEXT, `user_agent` TEXT, `reason` TEXT, `impersonation_start` REAL NULL, `impersonation_end` REAL NULL

**`platform_admins`** (parallel to `users`)
- `id` INTEGER PK, `email` TEXT UNIQUE, `password_hash` TEXT (PBKDF2 self-describing), `mfa_secret` TEXT (Fernet-encrypted), `created_at`/`updated_at` REAL, `disabled_at` REAL NULL

**`platform_admin_sessions`** (parallel to `auth_sessions`)
- `token` TEXT PK (SHA-256 hash), `admin_id` INTEGER, `effective_org_id` INTEGER NULL, `effective_user_id` INTEGER NULL, `created_at`/`expires_at` REAL

---

## 6. Cross-cutting risks & mitigations

1. **Double-lease race in SQLite** (no SKIP LOCKED) → `_tx_immediate` (`BEGIN IMMEDIATE`) + conditional UPDATE + `rowcount` + jittered backoff; Phase-3 N-thread test asserts exactly one winner.
2. **Double-actioning a live account** (catastrophic) → one account ↔ one box (capability pin) + DB lease + local single-flight + atomic offline-reclaim pinned to original worker + no cross-box failover; lease never reassigns for slowness.
3. **Second-attach / feed reuse crash** → fresh feed per job, guarded `_close_feed` in `finally`, startup sweep of orphan pause-files + stale single-flight locks (all in Phase 1).
4. **Stale pause-file / control divergence** → control truth in heartbeat flags; pause-file is engine-cooperative only, owned by the sidecar, deleted in `finally`, swept on startup.
5. **SQLite single-writer contention / thundering herd** → tiny lease/ack windows, run_events batched into heartbeat, jittered backoff; Postgres only past a measured throughput ceiling (not now).
6. **Daytime/warming thrash** → engine halts naturally; daytime → `retry_after_at` skip; warming → pre-filter via kill-switch; surface `halt_reason` to the dashboard.
7. **Superadmin = sanctioned BOLA bypass** → separate auth plane, MFA + IP-allowlist, composite tenant filter in one repository helper, `org_for_campaign` removed from call sites, append-only hash-chained audit, breach-attempt test.
8. **Worker token leakage** → secure `token_store` (keychain / 0600 Fernet) from Phase 1, hash-at-rest, request-time revocation check, long TTL with explicit revoke, version gate.
9. **Heartbeat-thread silent death** → 3 consecutive failures = treat as `halt` (tear down, nack `heartbeat_failed`); lease-extension contention is transient-retry, not instant-nack.
10. **Schema skew on partial rollout** → version-gate `/api/worker/register`; stale workers get explicit `update_required`; v14 auto-heals like v7/v11.
11. **Interleaved logs across parallel jobs** → per-job log files keyed on `run_id`/`job_id`; desktop app separates worker logs from engine logs.

---

## 7. Remaining blocking decisions

Carried from PRD §14 plus items surfaced by the code review:

- **(C2, Phase 1/3)** `_tx_immediate` confirmed as net-new, NOT a reuse of deferred `_tx` — load-bearing for lease atomicity. *(Resolved in this plan; flag for sign-off.)*
- **(C5, Phase 1)** Bake `soul` into the job spec (fail-fast in cloud) vs. ship `soul.md` on the box — recommend **bake into spec**; confirm bandwidth is acceptable.
- **(NEW, Phase 4)** Worker-token lifetime — recommend **1 year / until-revoked** (NOT 30-day human TTL). Sign-off needed.
- **(NEW, Phase 3)** Lease `worst_case_multiplier` + worst-case session length per platform (drives `lease_expires_at`) — **measure empirically**.
- **(PRD)** Per-session RAM budget — measure a warmed Chrome on target hardware to calibrate `hardware_ceiling`.
- **(PRD)** `safety_cap` per platform (anti-ban), likely 1–3 — needed before Phase 4 capacity logic.
- **(PRD, Phase 5)** MFA mechanism — recommend **TOTP v1, WebAuthn later**.
- **(PRD, Phase 6)** Code-signing/notarization accounts (Apple Developer ID; Windows Authenticode/EV) — blocks packaging.
- **(PRD, Phase 6)** Tauri vs Electron — recommend **Tauri**; confirm before Phase 6.

---

**Key seams for the executor:** entrypoint `dispatch.run_engine_session` (`dispatch.py:171`); IO build `cli._build_run_io` (`cli.py:92`); single-job pattern `cli._run_one` (`cli.py:277-322`); guarded feed teardown `cli._close_feed` (`cli.py:325-336`); campaign resolution `core.config.resolve_campaign` (`config.py:483`, returns None / raises ValueError); soul `cli.load_soul(cfg_dir/"soul.md")` (`cli.py:502`); run_events emit (`engines/instagram/session.py:102-127`); pause-file key + env (`runner.py:288-296`, `core/pause.py:20-31`); migration pattern + `SCHEMA_VERSION=13→14` (`core/store.py:33,611-720`); tx helper to fork into `_tx_immediate` (`core/store.py:806-815`); route gate dicts + `_current_user` (`server.py:1068-1099,1137`); BOLA helper to promote + the `org_for_campaign` call sites to reroute (`server.py:1164-1167,1319,1345,1381,1419,1476,1518,1928`).
