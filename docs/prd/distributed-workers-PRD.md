# PRD — Distributed CDP Workers + Superadmin Fleet Console

**Status:** Draft for review · **Date:** 2026-06-30 · **Owner:** founder
**Related:** `engine-live-run`, `multi-tenancy-rbac`, `multi-tenant-connections`, `separate-per-platform-engines`, `warming-system-p0`, `campaign-lifecycle-controls`
**Research basis:** workflow `wf_06956d1a-ca6` (architecture decision brief, grounded in codebase via `file:line`)

---

## 1. Problem

The engine currently runs **on the same machine as the warmed Chrome browser** it drives — `dispatch.run_engine_session()` receives an already-attached `feed`, and `core/cdp.py` attaches Playwright to a local Chrome on `127.0.0.1:9222/9333`. We want to run the **control plane in the cloud** (no browser) while the **actual browser work runs on independent worker PCs** that hold warmed, logged-in accounts. We also need a **superadmin console** to see every company, their campaigns/leads/statuses, and how many workers are connected and healthy.

## 2. Core decision (locked)

**PULL, not push.** Ship the engine to each worker PC; it drives its **local** Chrome and **pulls jobs** from the cloud over **outbound HTTPS 443**. The cloud never drives CDP remotely.

Rationale (from research):
- CDP is hundreds-of-round-trips per action; over WAN RTT it becomes slow and flaky. Bandwidth doesn't help — it's round-trip *count* × latency.
- The engine is already built to live beside the browser (`dispatch.run_engine_session()` takes a pre-attached browser; the IG session holds one live page handle for the whole run and can't be detached mid-session).
- Warmed, stateful sessions punish push (a dropped tunnel orphans a live login). Residential NAT means only outbound 443 is reliable.

**Tailscale** is adopted **only as an out-of-band ops plane** (SSH/RDP/log access to workers), never as the job channel.

## 3. Confirmed product decisions

| # | Decision | Value |
|---|---|---|
| 1 | Worker ownership | **You operate them** (managed back-office fleet — single trust domain) |
| 2 | Pooling | **Shared, centrally-operated pool**; jobs **capability-routed**; accounts pinned one-per-box |
| 3 | Superadmin power | **Full impersonation (read + write)** — requires MFA + IP-allowlist + append-only hash-chained audit |
| 4 | Proxies | **You supply & pin** a sticky residential/mobile proxy per account (secret via `secrets.py`/Fernet) |
| 5 | Concurrency/box | `max_sessions = min(hardware_ceiling, safety_cap)`; agent self-measures, operator overrides |
| 6 | Fleet size | Start with **1** worker, grow incrementally; ~**10** companies target |
| 7 | Worker OS | **Mac and Windows** (native-host install, Chrome on host GUI) |
| 8 | Offline policy | heartbeat 20s · `offline` after 2min silence · interrupted job requeues **to same box** (no cross-box failover) · alert if offline >5min |
| 9 | Worker form factor | **Full local desktop app** (GUI) on each box — own dashboard, logs, per-account login/checkpoint buttons, start/stop/pause; supervises the engine; auto-updates |

## 4. Goals / Non-goals

**Goals**
- Cloud control plane dispatches browser jobs to remote worker PCs; engine runs unchanged *on* the worker.
- Worker registry with heartbeat-derived presence; capability-aware job leasing with heartbeat-extended leases.
- Reuse existing seams: `run_events`/`/api/run/activity`, `sessions` + counter flush, org/RBAC, encrypted connections, kill-switch.
- Superadmin plane: cross-org read-all + audited full impersonation; live fleet view (polling, Recharts).
- Cross-platform (Mac/Windows) worker agent with supervised auto-update.

**Non-goals (this PRD)**
- No remote CDP driving. No cloud browser farm (buy-vs-build verdict: build orchestration, keep warmed boxes).
- No hot/standby same-account-on-two-boxes failover (optional future).
- No self-serve customer install (we operate the fleet).
- No change to engine decision logic / scoring / per-platform engines beyond making them runnable as a leased job.

## 5. Architecture

```
CLOUD CONTROL PLANE
  Existing: server.py /api/* gate · panel_org.py per-page · org/RBAC · secrets v8 · React panel
  NEW Dispatch service:   jobs table · POST /api/worker/lease (long-poll, SKIP LOCKED) ·
                          heartbeat/ack/nack · derives RunSpec from campaign_meta
  NEW Worker registry:    workers table · heartbeat-derived presence · fleet snapshot
  NEW Superadmin plane:   /api/admin/* (separate auth, MFA, IP-allowlist, audit)
        ▲ outbound HTTPS 443 only (NAT-proof)
WORKER PC  (managed; one per warmed account set)
  DESKTOP APP (Tauri shell + GUI) — supervisor + local UI:
    dashboard · per-account health · checkpoint/2FA prompt (focuses Chrome) ·
    start/stop/pause · log tail · capacity override · auto-update · run-at-login
   └─ supervises ENGINE SIDECAR (the shipped Python engine):
        pull loop: lease → run reelradar.cli run → heartbeat → ack/nack
        dispatch.run_engine_session() drives LOCAL Chrome via core/cdp.py (127.0.0.1)
        emits run_events locally → batches to cloud in heartbeat/ack
        holds warmed profile + pinned sticky proxy + fingerprint
    optional tailscaled for operator SSH/RDP only
```

**Data plane.** Down: job spec (campaign brief, target-leads, duration, platform, org-scoped connection handle). Up: heartbeat (load, free capacity, Chrome health, account warmth, ban signals, `run_events` delta) then final result (leads, counters, outcome). Coarse-grained, latency-tolerant — never per-CDP-call.

## 6. Data model (additive migrations, self-healing like v7/v11)

> Exact column types to follow existing schema conventions; this lists intent, not DDL.

**`workers` (schema ~v13)**
- `id` (stable machine fingerprint), `display_name`, `host`, `os`, `agent_version`
- `status` derived (not stored) from `last_heartbeat_at`: `online ≤2×`, `stale ≤4×`, `offline >8×` heartbeat interval
- `last_heartbeat_at`, `registered_at`, `max_sessions`, `current_sessions`
- `capabilities`: which `(org_id, platform, account_handle)` tuples this box can serve
- `worker_token_hash` (per-worker bearer, encrypted at rest), `revoked_at`

**`jobs` (schema ~v13)**
- `id`, `org_id`, `campaign_id`, `platform`, `required_account_handle`
- `spec` (RunSpec-shaped: target-leads, duration-minutes, brief ref)
- `status`: `queued → leased → running → done | failed | interrupted`
- `leased_by` (worker id), `lease_expires_at`, `attempts`, `max_attempts`, `dead_lettered_at`
- `result` (leads/counters/outcome), `session_id` (FK to existing `sessions`)

**`admin_audit_log` (schema ~v13)** — append-only, hash-chained
- `id`, `prev_hash`, `row_hash`, `acting_admin_id`, `action`, `target_org_id`, `target_user_id`, `target_resource`, `at`, `ip`, `user_agent`, `reason`, `impersonation_start`, `impersonation_end`

**`platform_admins` + `platform_admin_sessions`** — parallel to `users`/`auth_sessions` (v5), separate gate.

## 7. API contracts (described — not coded here)

**Worker plane** (bearer = per-worker token, gated like `/api/*`):
- `POST /api/worker/register` → bind worker to fleet; returns config + heartbeat interval.
- `POST /api/worker/lease` (long-poll) → returns one capability-matched `queued` job + lease token + `lease_expires_at`, or 204. Uses `SELECT … FOR UPDATE SKIP LOCKED` to avoid double-lease.
- `POST /api/worker/jobs/{id}/heartbeat` → extends `lease_expires_at`; carries load + `run_events` delta + account-health; **response carries control flags**: `drain`, `halt` (global/org/platform/worker), `update_required`.
- `POST /api/worker/jobs/{id}/ack` → final result; writes `sessions` row + flushes counters (reuse `store.update_counters`); marks `done`.
- `POST /api/worker/jobs/{id}/nack` → bounded retry → dead-letter.

**Superadmin plane** `/api/admin/*` (separate auth, MFA, IP-allowlist, every call audited):
- `GET /api/admin/orgs` · `/api/admin/orgs/{id}/{campaigns,leads}` — cross-org read-all aggregates.
- `GET /api/admin/fleet` — workers grouped by org, status computed server-side from `last_heartbeat_at`, literal last-seen age, current job, throughput, account-warmth, ban signals.
- `POST /api/admin/impersonate {org_id|user_id, reason}` / `POST /api/admin/impersonate/end` — writes effective principal into the **server-side superadmin session**; existing per-org endpoints serve unchanged. Exactly one place sets a foreign org/user; start+end audited.

## 8. Worker agent design

**Form factor: a full desktop application** (GUI) installed on each Mac/Windows box. The app is the supervisor + UI; the existing Python engine runs as a managed **sidecar process** underneath it.

- **Shell:** recommend **Tauri** over Electron — the app must *manage a separate warmed Chrome* (via CDP), so it should NOT bundle a second Chromium. Tauri (system webview + Rust shell) gives small signed binaries, a built-in updater, and a tray/login-item story on both OSes. (Electron is the fallback if the team prefers an all-JS stack.)
- **Engine runtime:** the existing engine packaged as a sidecar (PyInstaller/pex or pinned runtime), reusing `reelradar.cli run`, `dispatch.run_engine_session()`, `core/cdp.py connect_over_cdp(127.0.0.1)`. The desktop app spawns/supervises it and restarts on crash.
- **Pull loop:** app (or sidecar) registers → loop{ lease → run job locally → heartbeat-while-running → ack/nack } honoring `drain`/`halt`/`update_required`.
- **Local UI (the reason for a desktop app):** per-account health dashboard; **checkpoint/2FA/captcha handling** — when IG demands human interaction, surface a notification and a button that focuses the warmed Chrome window so the operator can solve it; start/stop/pause; live local log tail; connection/proxy status; current job + capacity.
- **Capacity:** self-measure RAM/cores → `hardware_ceiling`; apply `safety_cap`; report `max_sessions`; only lease what it can run (natural backpressure). Operator can override in the app.
- **Progress:** emit `run_events` locally (schema v10), batch up via heartbeat → flows into existing `/api/run/activity` feed and the cloud panel.
- **Stop/pause/kill:** flags in lease/heartbeat **response** (replacing the local pause-file as the *source of truth*); wire `halt` to the existing warming kill-switch; also operable from the local app.
- **Auto-update:** app reports `agent_version`; server can refuse leases to stale agents and signal `update_required`; the app self-updates via the Tauri/Electron updater pulling a **signed, notarized** build and relaunching.
- **Run-at-login + watchdog:** registers as a login item so a reboot brings the fleet back; a lightweight watchdog (OS login item + in-app supervision) keeps the engine sidecar alive.
- **Sticky identity:** box owns warmed profile + pinned proxy + fingerprint; jobs routed only to the box owning the matching account.

## 9. Leasing & safety (critical)

- **Lease deadline ≫ worst-case session length**, and **heartbeat-extended** — a job is never reassigned for being slow.
- **No cross-box failover:** because one account ↔ one box, a dead worker's job can't run elsewhere. On 2-min silence → worker `offline`, job → `interrupted`, **requeued to the same box** for when it returns; operator alerted if offline >5min.
- This makes catastrophic **double-actioning a live account impossible by construction**.

## 10. Superadmin & security

- **Separate auth plane**, never `OR role='superadmin'` in the data layer (BOLA factory). Org path stays fail-closed; the admin plane is the one sanctioned, audited bypass.
- **Full impersonation (read+write)** requires: MFA on the admin plane, IP-allowlist, explicit `reason`, start+end audit, off-hours/burst alerting.
- **Tenant filter** stays composite (`WHERE id=:id AND org_id=:effective_org`), enforced in a repository helper handlers can't forget.
- **Audit log** append-only + hash-chained, stored apart, ≥12-month retention.
- **Fleet view:** polling (not SSE) to match `useRunActivity`; Recharts; render literal last-seen age beside status dot.

## 11. Phases (shippable, ordered)

1. **Worker engine sidecar + local pull loop (stub dispatch).** Wrap `reelradar.cli run` in a headless pull-loop sidecar that long-polls a stub `/lease`, runs against local Chrome, posts result. Proves the engine runs off-cloud *before* building the desktop shell. *(reuse dispatch.py, cli.py, core/cdp.py)*
2. **Registry + heartbeat + presence.** `workers` table (v13), register/heartbeat, derived status, `/api/admin/fleet` snapshot.
3. **Real dispatch + leasing.** `jobs` table, `SKIP LOCKED` lease, heartbeat-extended visibility, ack/nack + dead-letter, capability-aware routing, `run_events` streamed into the existing feed.
4. **Lifecycle controls.** Drain, kill-switch (wire to existing), per-worker tokens (Fernet), auto-update gating, offline→interrupted→requeue policy + alerting.
5. **Superadmin plane.** `platform_admins` + separate gate + MFA + IP-allowlist, cross-org read views, full impersonation with audit, fleet dashboard (polling + Recharts).
6. **Desktop worker app + cross-OS packaging + Tailscale ops plane.** Wrap the Phase-1 sidecar in a Tauri (fallback Electron) desktop app: local dashboard, checkpoint/2FA handling that focuses the Chrome window, start/stop/pause, log tail, capacity override. Signed + notarized Mac/Windows installers, built-in auto-updater, run-at-login + watchdog. Tagged Tailscale enrollment + ACLs for operator SSH/RDP; anomaly alerting.

## 12. Success metrics

- A cloud-dispatched job runs end-to-end on a remote worker and lands leads + `run_events` in the panel, with zero remote-CDP traffic.
- Fleet view shows accurate online/stale/offline within one heartbeat interval after a state change.
- Zero double-actioned accounts under induced worker drops/partitions.
- One version flag drains + updates the fleet without aborting a live warmed session.
- Superadmin impersonation is fully reconstructable from the audit log.

## 13. Risks & unknowns

1. **Account bans dominate.** Keep CDP local, proxy+fingerprint pinned per account, one account per IP/box.
2. **Cross-OS packaging/auto-update** (Mac+Windows, host-GUI Chrome, warming + daytime-guard surviving packaging) — highest-effort unknown.
3. **Lease-visibility vs long sessions** — must stay heartbeat-extended and generous (a *safety* bug if wrong).
4. **Single-run lock today** (`runner.py:266-286`) assumes one active run/process; per-worker concurrency + per-account isolation is net-new.
5. **Superadmin = sanctioned BOLA bypass** — highest-value breach target; demands the separate plane + audit + repository-enforced filtering.

## 14. Open items to resolve during build

- Per-session RAM budget constant (measure a warmed Chrome on target hardware) to calibrate `hardware_ceiling`.
- Exact `safety_cap` per platform (anti-ban) — likely 1–3.
- Signed-build distribution + code-signing/notarization story for Mac/Windows installers (Apple Developer ID + notarization; Windows Authenticode / EV cert).
- **Desktop shell framework: Tauri (recommended) vs Electron** — confirm before Phase 6.
- MFA mechanism for the admin plane (TOTP vs WebAuthn).
