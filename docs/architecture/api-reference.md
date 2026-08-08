# Aizu Bridge Server — HTTP API Reference

The bridge server is a stdlib-only `ThreadingHTTPServer` (default `http://127.0.0.1:8765`) that serves the static marketing landing, the built React panel, and a JSON API under `/api/*`. All routing is hand-dispatched in `engine/aizu/server.py` inside `PanelHandler.do_GET` / `do_POST` / `do_OPTIONS` — there is no framework and no decorator-based route table.

> `engine/aizu/core/router.py` is the **LLM model router**, not an HTTP router — it defines no `/api/*` routes.

## Base URL & prefix

- Base URL: `http://127.0.0.1:8765` (loopback by default).
- All API paths are under the `/api` prefix. `/` and `/index.html` serve the static marketing landing (`dist/index.html`). `/app`, `/app/`, and any unknown non-API path under `/app/` fall back to the SPA shell (`dist/app/index.html`, client-side hash routing). Any other unknown non-API path falls back to the landing. Real static files under `/assets`, `/landing`, the favicon, etc. serve directly.
- Unknown `/api/*` paths return a JSON `404 {"ok": false, "error": "unknown endpoint"}` rather than the SPA shell.

## Response envelopes

Two shapes, verified per endpoint:

- **Enveloped** (most endpoints, via `_send_json`, `server.py:1553`): `{"ok": bool, "data": ..., "error": string|null}`.
- **Raw** (no envelope, via `_send_json_body`): the per-page GET reads `/api/state`, `/api/dashboard`, `/api/campaigns`, `/api/reports`, `/api/settings` send the builder dict directly. `/api/leads` is the exception among the page reads — it **is** enveloped, because its pagination metadata has no natural top-level record key.

## Auth model — three separate planes (never share identity)

| Plane | Credential | Resolver | Notes |
|---|---|---|---|
| **Org session** | `rr_session` HttpOnly cookie (`SESSION_COOKIE`, `server.py:129`) | `_current_user` (`server.py:1789`) | TTL 30 days (`auth.py:36`). |
| **Worker** | `Authorization: Bearer <token>` | `_current_worker` (`server.py:1871`) | Token TTL ~1 year; revocation, not expiry, is the off-switch (`store.py:849`). |
| **Superadmin** | `rr_admin_session` cookie + IP-allowlist + TOTP MFA | `_current_admin` (`server.py:1920`) | Fails closed on the IP-allowlist first. |

### Org-route RBAC gate (`do_POST`, `server.py:1745`)

For protected org routes the ladder is: `401` (no session) → `403` (no `orgId`) → `403` (role lacks the route's action per `_ROUTE_ACTIONS`, `server.py:181`). Per-op finer checks live inside the handlers. Roles (`rbac.py`): `owner`, `admin`, `member`, `viewer`. Assignable via invite/direct-add: `admin`, `member`, `viewer` (owner is established at signup or by ownership transfer only).

### CORS / CSRF

- Any non-loopback `Origin` on a POST → `403 "cross-origin request rejected"` (`server.py:1603`). Local origins are matched by `_LOCAL_ORIGIN_RE` (`server.py:229`), anchored to reject lookalikes like `http://127.0.0.1.evil.com`.
- Cookies are `HttpOnly; SameSite=Lax`.
- `OPTIONS` on any path returns `204` with CORS headers, only for local origins (`server.py:3880`). It is a preflight helper, not a data endpoint.

### Body caps

- Default `64 KB` (`MAX_BODY_BYTES`).
- Worker routes `1 MB` (`WORKER_MAX_BODY_BYTES`).
- Campaign generate / interview `8 MB` (`GENERATE_MAX_BODY_BYTES`).
- A missing / oversized / invalid body → `400`.

### Enumerated values (verified)

| Set | Values | Source |
|---|---|---|
| Platforms | `instagram, youtube, telegram, reddit, linkedin, x` | `core/config.py:24` |
| Lead statuses | `new, in_progress, interested, closed, couldnt_connect, archived` | `store.py:43` |
| Forced-reason statuses (require a note) | `closed, couldnt_connect, archived` | `store.py:47` |
| Campaign statuses | `live, paused, draft, ended` | `store.py:76` |
| Run modes | `dry, live` | `runner.py:53` |
| Assignable roles | `admin, member, viewer` | `rbac.py:31` |

---

## 1. Auth (org plane)

| Method | Path | Auth |
|---|---|---|
| POST | `/api/auth/signup` | Public |
| POST | `/api/auth/login` | Public (throttled) |
| POST | `/api/auth/logout` | Any |
| GET | `/api/auth/me` | Org session |
| GET | `/api/invite?token=<t>` | Public |

### POST `/api/auth/signup`
Create a new company (signer becomes `owner`) OR join an existing org via an invite token.
- **Body**: `{email, password}` required (email regex-validated; password 8–256 chars, `auth.py:39`). Either `inviteToken` (join existing org; company fields ignored) OR `companyName` required (+ optional `companyLogo`, `companyDescription`). Validated by `_validate_signup` (`server.py:1402`).
- **Response**: `200 {ok, data:{user:{id,email,role,orgId,impersonated,org:{...}}}}` + `Set-Cookie rr_session`. `409` if email exists; `400` invalid/expired invite or bad fields. Handler `server.py:1995`.

### POST `/api/auth/login`
Authenticate an org user. Throttled by `LoginThrottle` (per-email lockout → `429`).
- **Body**: `{email, password}` (`_validate_credentials`; password policy not enforced on login).
- **Response**: `200 {ok, data:{user}}` + `Set-Cookie`. `401 "invalid email or password"` (timing-safe dummy verify against `_DUMMY_PASSWORD_HASH`). Handler `server.py:2040`.

### POST `/api/auth/logout`
Delete the server session + clear the cookie. Handled before any gate.
- **Body**: none.
- **Response**: `200 {ok, data:{loggedOut:true}}` + cookie cleared. Handler `server.py:2081`.

### GET `/api/auth/me`
Current session identity. Org session (or active superadmin impersonation).
- **Response**: `200 {ok, data:{user}}`; `401` if not authenticated. Handler `server.py:2094`.

### GET `/api/invite?token=<t>`
Public invite-landing lookup (org branding + intended role before signup).
- **Query**: `token` required.
- **Response**: `200 {ok, data:{orgName, orgLogo, email, role, valid:true}}`; `400` missing token; `404` invalid/expired. Handler `server.py:2101`.

---

## 2. Per-page read endpoints (GET, org session)

All require an org session; each is gated by an RBAC view-action via `_gated_org_user` (401→403→role, `server.py:3990`). Pages built with `attach_run=true` fold in an in-memory `RUN` block scoped to the org (from `RunManager.status(org_id)`, attached verbatim).

| Method | Path | Role (view-action) | Envelope |
|---|---|---|---|
| GET | `/api/state?campaign=<id>` | session + orgId (pruned by role in builder) | Raw |
| GET | `/api/dashboard` | `view_dashboard` (owner/admin/viewer) | Raw + RUN |
| GET | `/api/campaigns` | `view_campaigns` (owner/admin/viewer) | Raw + RUN |
| GET | `/api/reports` | `view_reports` (owner/admin/viewer) | Raw |
| GET | `/api/settings` | `view_settings` (owner/admin) | Raw |
| GET | `/api/leads` | `view_leads` (owner/admin/member/viewer) | Enveloped |

### GET `/api/state?campaign=<id>`
Full single-campaign panel state. `?campaign=` selects one campaign (verified in-org, else 404); absent → org home campaign, or an empty state.
- **Response**: **RAW** dict from `build_raw` (`panel.py:770`): keys `CONFIG, CAMPAIGNS, SESSIONS, REELS, MATCHES, PLATFORMS, ESCALATION_LOG, ALERTS, HEALTH, SOUL, DASHBOARD, REPORTS`, plus `TEAM`+`INVITES` (view_team), `INTEGRATIONS` (view_settings), and `RUN` (in-memory). A `member` (leads-only role) gets a pruned `{CONFIG, CAMPAIGNS(stubs), MATCHES}`. `404` unknown campaign; `403` no org. Handler `server.py:4066`.

### GET `/api/dashboard`
- **Response**: RAW `{DASHBOARD, MATCHES, HEALTH, ALERTS, CONFIG}` + `RUN`. Builder `build_dashboard_org` (`panel_org.py:273`). `DASHBOARD` is keyed by period `today/week/month`, each with `leads, goal, cpl, conversion, channels, funnel, bestHour, activeCampaigns, topCampaigns, ticker, leadStatus, pipeline, teamActivity, needsAttention` (`panel.py:360`).

### GET `/api/campaigns`
- **Response**: RAW `{CAMPAIGNS, SESSIONS}` + `RUN`; each card enriched with `fleetRunId` (run_id of the most-recent active fleet job, else null, `server.py:264`). Builder `build_campaigns_org` (`panel_org.py:299`). Card shape: `id, name, goalType, status, platform, platforms, threshold, languages, extractFields, startedAt, brief, budgetCap, goalTarget, briefForm, spent, leads, cpl, spark, warmth` + lifecycle fields `archivedAt, pausedReason, scheduleEnabled, scheduleKind, scheduleDow, scheduleHour, scheduleMinute, scheduleTz, nextRunAt` (`panel.py:49`, `panel.py:630`).

### GET `/api/reports`
- **Response**: RAW `{REPORTS, HEALTH}`. `REPORTS` keyed by period, each `{labels, matchesByPlatform, cplTrend, spendByStage, platformRanking, perCampaign}` (`panel.py:418`).

### GET `/api/settings`
- **Response**: RAW `{CONFIG, TEAM, INVITES, INTEGRATIONS}`, plus `BILLING` if the role has `view_billing`. Builder `build_settings_org` (`panel_org.py:328`). Same path is POST-write in §6 — split by method.

### GET `/api/leads`
Org-wide, server-side filtered/sorted/paginated.
- **Query**: `page` (default 1), `pageSize` (default 50, max 200), `dir` (`asc`/`desc`, default `desc`), `q` (username/text substring), `status`, `platform`, `campaign` (scopes list + tiles to one campaign), `sort` (`capturedAt|score|username|platform|status`, default `capturedAt`). Parsed leniently (`_query_int` — a bad param falls back, never 400s).
- **Response**: **Enveloped** `{ok, data:{items, total, page, pageSize, stats, platforms, campaigns, CONFIG}}`. `stats` = `{total, counts{...}, won, escalated, labeled}`. Builder `build_leads_org` (`panel_org.py:409`); handler `server.py:4036`.

---

## 3. Leads / status writes (POST, org session)

| Method | Path | Role |
|---|---|---|
| POST | `/api/status` | `edit_leads` (owner/admin/member) |
| POST | `/api/status/bulk` | `bulk_edit_leads` (owner/admin) |
| POST | `/api/lead/note` | `edit_leads` (owner/admin/member) |

### POST `/api/status`
Set one lead's status.
- **Body**: `{campaignId, commentId, status}` required; `platform` optional (default `instagram`); `note` optional but **required** when `status` is a forced-reason status. Validated `_validate_status_request` (`server.py:342`).
- **Response**: `200 {ok, data:{commentId, status}}`; `404` unknown campaign (cross-org hidden) or no matching comment; `400` invalid status / missing forced reason. Handler `server.py:2121`.

### POST `/api/status/bulk`
Set status on up to 500 leads with one shared reason.
- **Body**: `{campaignId, status, items:[{commentId, platform?}], note?}`; `items` non-empty, ≤500; `note` required for a forced-reason status. `_validate_bulk_status` (`server.py:394`).
- **Response**: `200 {ok, data:{updated, missing:[commentId...], status}}` (partial misses are not an error). Handler `server.py:2150`.

### POST `/api/lead/note`
Create or delete a lead note.
- **Body**: `{op:"create", campaignId, commentId, body, platform?}` (body ≤4000 chars) OR `{op:"delete", noteId:<int>}`. `_validate_lead_note` (`server.py:430`).
- **Response**: create → `200 {ok, data:<note>}`; delete → `200 {ok, data:{noteId, op:"delete"}}`; `404` no note; `403 "only the note's author may delete it"`. Handler `server.py:2182`.

---

## 4. Campaigns (POST, org session)

All require `edit_campaigns` (owner/admin).

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/campaign` | Upsert campaign meta + brief |
| POST | `/api/campaign/generate` | AI-draft a campaign (8 MB cap) |
| POST | `/api/campaign/interview` | One round of the campaign interview (8 MB cap) |
| POST | `/api/campaign/archive` | Archive / un-archive |
| POST | `/api/campaign/schedule` | Arm / clear a recurring schedule |

### POST `/api/campaign`
Upsert campaign meta + brief (create or edit). The brief is **merged** over the stored/file brief, not replaced.
- **Body**: `{campaignId}` required; optional `status` (campaign-status set), `budgetCap` (≥0), `goalTarget` (≥0 int), `displayName`, `brief` (object; camelCase keys mapped to snake per `_BRIEF_KEYS`, `server.py:562`). `live`/`paused` route through pause semantics (`set_campaign_paused`). `_validate_campaign` (`server.py:457`).
- **Response**: `200 {ok, data:<meta>}` (meta gains `hasBrief:true` when a brief was stored); `404` cross-org campaign; `400` bad brief shape. Handler `server.py:2218`.

### POST `/api/campaign/generate`
AI-draft a campaign from product url / screenshot / description (+ optional interview transcript). Persists nothing — returns a draft to pre-fill the form. Body cap 8 MB.
- **Body**: at least one of `url` (http/https, ≤2048), `imageB64` (≤6 MB), `text` (≤8000), `productContext` (≤16000); optional `campaignIdHint`, `interview:[{question,answer}]` (≤30 pairs, ≤4000 chars each), `platforms:[...]` (≤6). `_validate_generate` (`server.py:722`).
- **Response**: `200 {ok, data:<flat draft>}`; `503` if `OPENROUTER_API_KEY` unset; `422` on `CampaignGenError.public`; `500 "generation failed"`. Handler `server.py:2360`.

### POST `/api/campaign/interview`
One round of the conversational campaign interview. Body cap 8 MB. Persists nothing.
- **Body**: same source fields as generate + `round` (positive int, clamped). `_validate_interview` (`server.py:764`).
- **Response**: `200 {ok, data:{done, questions, productContext, round}}`; `503`/`422`/`500` as above. Handler `server.py:2392`.

### POST `/api/campaign/archive`
Archive / un-archive. Archiving a live campaign first stops its run and parks it at `paused`.
- **Body**: `{campaignId, archived:bool}`. `_validate_campaign_archive` (`server.py:484`).
- **Response**: `200 {ok, data:{campaignId, archived}}`; `404` unknown campaign. Handler `server.py:2273`.

### POST `/api/campaign/schedule`
Arm/replace (enabled) or clear (disabled) a recurring schedule. Server computes `next_run_at`.
- **Body**: `{campaignId, enabled:bool}`; when enabled: `kind` ∈ `daily|weekdays|weekly` (`schedule.py:17`), `hour` 0–23, `minute` 0–59, `dow` 0–6 (weekly only), `tz` (`Asia/Tashkent` only), optional `targetLeads` 1–1000, `durationMinutes` 1–720. `_validate_campaign_schedule` (`server.py:514`).
- **Response**: `200 {ok, data:{campaignId, scheduleEnabled, nextRunAt}}`; `404` unknown campaign; `400` bad cadence. Handler `server.py:2316`.

---

## 5. Run control (POST/GET, org session)

All require `run_campaigns` (owner/admin). All `503` if run control is disabled (except the `/api/run` distributed-live path, which enqueues without a RunManager).

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/run` | Start a run (one campaign or all live) |
| POST | `/api/run/stop` | Stop the active run |
| POST | `/api/run/pause` | Pause the active run (idempotent) |
| POST | `/api/run/resume` | Resume the active run (idempotent) |
| GET | `/api/run/activity?runId=<id>&after=<cursor>` | Live activity feed for one run |

### POST `/api/run`
Start a run for one campaign or all live campaigns. In-process by default; distributed-backend live runs enqueue to the worker fleet.
- **Body**: exactly one of `{campaignId}` or `{all:true}`; `mode` ∈ `dry|live` (default `dry`); optional `targetLeadCount` (1–1000), `durationMinutes` (1–720). `_validate_run` (`server.py:1291`).
- **Billing gate** (`server.py:2884`): `402` if subscription status ∉ `active|trialing`, or `402` if the period lead cap is exhausted; remaining cap **clamps** the target.
- **Response (in-process)**: `202 {ok, data:{accepted:true, scope, campaignId, mode}}`; `409 "a run is already active"`; `400` not runnable.
- **Response (distributed)**: `202 {ok, data:{accepted:true, backend:"distributed", scope, jobs:[...], runId, runIds:[...], skipped:[{campaignId,reason}]}}`; `409` if no capable worker / nothing dispatched. Handler `server.py:2853`, fleet dispatch `server.py:2940`.

### POST `/api/run/stop`
- **Body**: none. **Response**: `200 {ok, data:{stopped:true}}`; `409 "no run is active"` (also covers another org's run). `server.py:3042`.

### POST `/api/run/pause`
- **Body**: none. **Response**: `200 {ok, data:{paused:true}}`; `409` if nothing active. Idempotent. `server.py:3056`.

### POST `/api/run/resume`
- **Body**: none. **Response**: `200 {ok, data:{paused:false}}`; `409` if nothing active. Idempotent. `server.py:3071`.

### GET `/api/run/activity?runId=<id>&after=<cursor>`
Live activity feed for one run (counters + narrative event stream + open flags), paged on a monotonic cursor. Ownership proven in-memory or via org-stamped DB rows.
- **Query**: `runId` required; `after` cursor (default 0).
- **Response**: `200 {ok, data:{runId, finished, fleetJob, counters:{reelsSeen,relevancePasses,commentsScored,matches,spendUsd,likes,follows}, events:[...], flags:[{kind,severity,detail}], cursor}}`; `400` missing runId; `404` unknown/foreign run. Counters aggregated by `_aggregate_run_counters` (`server.py:246`); handler `server.py:4109`.

---

## 6. Team / invites / org / settings (POST, org session)

| Method | Path | Role gate |
|---|---|---|
| POST | `/api/team` | `invite_member` floor + per-op checks |
| POST | `/api/invite` | `invite_member` (+ `can_assign_role` on create) |
| POST | `/api/org` | `edit_settings` |
| POST | `/api/settings` | `edit_settings` |

### POST `/api/team`
Manage real user accounts.
- **Role gate**: `invite_member` floor; finer per-op checks (`can_assign_role`, `can_manage_target`, last-owner guard).
- **Body**: `{op:"create", email, password, role}` (role ∈ assignable), OR `{op:"updateRole", userId, role}`, OR `{op:"remove", userId}`. `_validate_team` (`server.py:802`).
- **Response**: create → `200 {ok, data:{id, email, role}}`; updateRole/remove → `200 {ok, data:{userId, op}}`; `409` email exists; `403` role/authority violations; `404` no such teammate; `400` can't remove self / must keep one owner. Handler `server.py:2425`.

### POST `/api/invite`
Create a shareable invite link, or revoke one.
- **Role gate**: `invite_member`; create additionally checks `can_assign_role`. Rate-limited (`InviteThrottle`, 10/hour/actor → `429`).
- **Body**: `{op:"create", role, email?}` OR `{op:"revoke", id}`. `_validate_invite` (`server.py:839`).
- **Response**: create → `200 {ok, data:{token, role, email, path:"/signup?invite=<token>"}}` (token returned once); revoke → `200 {ok, data:{id, op:"revoke"}}`; `404` no such pending invite. Handler `server.py:2502`.

### POST `/api/org`
Edit the company profile.
- **Body**: optional `name` (non-blank, ≤200), `logo` (≤8192), `description` (≤2000) — at least one. `_validate_org` (`server.py:862`).
- **Response**: `200 {ok, data:{id, name, logo, description}}`; `404` org not found. Handler `server.py:2545`.

### POST `/api/settings`
Write per-org workspace settings.
- **Body**: `{settings:{...}}` — whitelisted keys only (`_SETTINGS_KEYS`, `server.py:221`): strings `productName, timezone`; numbers `matchThreshold, skipRatioThreshold, budgetCapUsd, canaryLimitReels, watchlistTtlDays`; object `pacing`. Unknown key → `400`. `_validate_settings` (`server.py:884`).
- **Response**: `200 {ok, data:<effective settings>}`. Handler `server.py:2570`.

---

## 7. Integrations (POST, org session)

All require `toggle_integration` (owner/admin).

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/integration` | Connect/disconnect/toggle a per-platform connection |
| POST | `/api/integration/telegram/start` | Telegram login step 1 (send code) |
| POST | `/api/integration/telegram/verify` | Telegram login step 2 (submit code / 2FA) |

### POST `/api/integration`
Connect/disconnect/toggle a per-platform connection. YouTube and Reddit self-serve credentials (validated live before storing, then encrypted).
- **Body**: `{platform}` (∈ supported); optional `connected:bool`, `detail:string`; `apiKey` (YouTube only); Reddit trio `clientId`+`clientSecret`+`userAgent` (all-or-nothing, Reddit only). `_validate_integration` (`server.py:903`). Live validation via `connections.validate_youtube_api_key` / `validate_reddit_credentials`.
- **Response**: `200 {ok, data:<integration row>}`; `400` bad credential (validation failed) or apiKey/reddit-fields on wrong platform; `500` on secret-cipher error (missing/invalid `AIZU_SECRET_KEY`). Handler `server.py:2588`.

### POST `/api/integration/telegram/start`
Telegram login wizard step 1 — send a login code to a phone.
- **Body**: `{phone}`.
- **Response**: `200 {ok, data:{token}}`; `503` if Telegram login not enabled; `400` bad phone. Handler `server.py:2660`.

### POST `/api/integration/telegram/verify`
Wizard step 2 — submit code (+ optional 2FA password). On success stores the session secret + marks connected.
- **Body**: `{token, code, password?}`. `_validate_telegram_verify` (`server.py:1263`).
- **Response**: `200 {ok, data:{needsPassword:true}}` (2FA required) or `{needsPassword:false}` (connected); `503`/`400`/`500`. Handler `server.py:2679`.

---

## 8. Billing

| Method | Path | Auth |
|---|---|---|
| POST | `/api/billing/checkout` | `manage_billing` (owner/admin) |
| POST | `/api/billing/portal` | `manage_billing` (owner/admin) |
| POST | `/api/billing/webhook` | **Public**, provider-signed (Polar) |

### POST `/api/billing/checkout`
- **Body**: `{tier, interval}` — tier ∈ `lite|starter|pro` (`SELF_SERVE_TIERS`), interval ∈ `month|year`. `_validate_billing_checkout` (`server.py:1431`).
- **Response**: `200 {ok, data:{checkoutUrl}}`; `503` billing not configured; `409` already has an active plan; `502` provider error. Handler `server.py:2736`.

### POST `/api/billing/portal`
- **Body**: none.
- **Response**: `200 {ok, data:{portalUrl, hasAccount}}`; `503`/`502`. Handler `server.py:2774`.

### POST `/api/billing/webhook`
**PUBLIC**, provider-signed. Verified on the raw bytes; handled before any session gate. Intentionally NOT in `_ROUTE_ACTIONS` (`server.py:199`).
- **Auth**: provider signature only (no session/role).
- **Body**: raw provider event bytes; signature in headers.
- **Response**: `200 {ok, data:{received:true}}` for any verified event (incl. unknown/ignored, to avoid retry storms); `401` invalid signature; `503` billing not configured; `400` missing/oversized body; `500` upsert failure. Only `subscription.*` events mutate state. Handler `server.py:2789`.

---

## 9. Worker plane (bearer token)

Bearer-gated, not cookie/RBAC. Handled before the org gate. First-register uses the shared `AIZU_WORKER_BOOTSTRAP_TOKEN` env; re-register presents the current token. Body cap 1 MB.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/worker/register` | Register / re-register a worker box |
| POST | `/api/worker/heartbeat` | Worker-level presence beat |
| POST | `/api/worker/lease` | Lease one job |
| POST | `/api/worker/jobs/{jobId}/{heartbeat\|ack\|nack}` | Job-scoped lifecycle |

### POST `/api/worker/register`
Register / re-register a worker box; mints a fresh plaintext token (hash stored), returned exactly once. Response body is suppressed from DEBUG logs (`server.py:1546`).
- **Auth**: existing bearer token (re-register) OR bootstrap secret (first register).
- **Body**: optional capped strings `machineId` (required on first register), `displayName, host, os, agentVersion`; `orgId:int|null`; `maxSessions:int` (default 1, ≤50); `capabilities:[[orgId|null, platform, accountHandle|null]]` (≤100). `_validate_worker_register` (`server.py:963`).
- **Response**: `200 {ok, data:{workerId, token, heartbeatIntervalSec}}`; `401` invalid/absent token + bootstrap; `400` missing machineId on first register. Handler `server.py:3086`.

### POST `/api/worker/heartbeat`
Worker-level presence beat; returns resolved OR-merged control flags.
- **Auth**: bearer token (identity; body workerId ignored).
- **Body**: optional `{currentSessions|load:int}`. `_validate_worker_heartbeat` (`server.py:1021`).
- **Response**: `200 {ok, data:{drain, halt, updateRequired}}`; `401` invalid/revoked token; `404` worker vanished mid-flight. Handler `server.py:3142`.

### POST `/api/worker/lease`
Lease one job (capabilities come from the registered row, not the body). Optional bounded long-poll (≤30 s).
- **Body**: optional `{leasePollTimeoutSec:number}` (clamped to 30). `_validate_worker_lease` (`server.py:1151`).
- **Response**: `200 {ok, data:{job, leaseExpiresAt}}` when a job leased, or `200 {ok, data:null}` on an empty queue (never 204); `401`. Handler `server.py:3507`.

### POST `/api/worker/jobs/{jobId}/{action}` — action ∈ `heartbeat|ack|nack`
Job-scoped lifecycle. URL job_id + bearer token are authoritative (one worker can't touch another's job). Route parsed by `_match_worker_job_route` (`server.py:1116`); dispatcher `server.py:3543`. All actions `401` on an invalid worker token.
- **`heartbeat`**: body optional `{runEvents:[...], runId?}`. Extends the lease; returns `200 {ok, data:{halt, drain, updateRequired, leaseExpiresAt}}` (a lost lease returns `halt:true`). `server.py:3559`.
- **`ack`**: body `{summary?:object, leads?:[...]}` (leads capped at 500, `MAX_SYNC_LEADS`). Returns `200 {ok, data:{recorded:bool}}`. `_validate_worker_ack` (`server.py:1186`); handler `server.py:3629`.
- **`nack`**: body `{reason (required), poison?:bool, retryAfterAt?:number}`. Returns `200 {ok, data:{recorded, outcome, retryAfterAt}}`. `_validate_worker_nack` (`server.py:1167`); handler `server.py:3648`.

---

## 10. Superadmin plane

Gated by `_current_admin` (IP-allowlist + `rr_admin_session` cookie + TOTP MFA). Env: `admin_auth.ADMIN_IP_ALLOWLIST_ENV`, `ADMIN_TRUSTED_PROXIES_ENV`. `_require_admin` returns `401 "platform admin authentication required"` when unauthenticated.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/admin/login` | Admin login (password + TOTP) |
| POST | `/api/admin/logout` | Admin logout |
| GET | `/api/admin/whoami` | Admin identity + impersonation state |
| POST | `/api/admin/impersonate` | Start impersonation |
| POST | `/api/admin/impersonate/end` | End impersonation |
| GET | `/api/admin/audit?limit=<n>` | List audit entries |
| GET | `/api/admin/audit/verify` | Verify audit hash-chain |
| GET | `/api/admin/orgs` | Cross-org index |
| GET | `/api/admin/orgs/{orgId}/{campaigns\|leads}` | Cross-org read of one org |
| GET | `/api/admin/fleet` | Worker fleet view |
| GET/POST | `/api/admin/control-flags` | List / set / clear control flags |
| POST | `/api/admin/jobs/enqueue` | Operator enqueue one job |
| POST | `/api/admin/workers/revoke` | Revoke a worker token |
| GET/POST | `/api/admin/execution-backend` | Read / set the run routing backend |
| GET/POST | `/api/admin/model-comparison` | Read / set the LLM fan-out switch |
| GET | `/api/admin/model-comparison/stats` | Model performance stats |

### POST `/api/admin/login`
- **Body**: `{email, password, totpCode}` (TOTP required, ≤16 chars). `_validate_admin_login` (`server.py:1356`).
- **Response**: `200 {ok, data:{admin:{id,email}}}` + `Set-Cookie rr_admin_session`; `403` off-allowlist; `429` throttled; `401 "invalid credentials"` (opaque — covers bad password/TOTP/replay/disabled); `500` MFA secret decrypt failure. The TOTP counter is consumed to prevent replay. Handler `server.py:3193`.

### POST `/api/admin/logout`
- **Response**: `200 {ok, data:{loggedOut:true}}` + cookie cleared; `403` off-allowlist. Handler `server.py:3275`.

### GET `/api/admin/whoami`
- **Response**: `200 {ok, data:{admin:{id, email, impersonating, effectiveOrgId, effectiveUserId, impersonationReason}}}`; `401`. Handler `server.py:3299`.

### POST `/api/admin/impersonate`
Start impersonation (stamps the effective principal on the admin session; audited, hash-chained).
- **Body**: exactly one of `{orgId}` or `{userId}` + required `reason` (≤500). `_validate_impersonate` (`server.py:1370`).
- **Response**: `200 {ok, data:{impersonating:{orgId, userId}}}`; `404` unknown target; `409` admin session lapsed. Handler `server.py:3322`.

### POST `/api/admin/impersonate/end`
- **Body**: none. **Response**: `200 {ok, data:{impersonating:null}}` (idempotent, audited). Handler `server.py:3371`.

### GET `/api/admin/audit?limit=<n>`
- **Response**: `200 {ok, data:{entries:[...]}}` (default limit 200). Handler `server.py:3411`.

### GET `/api/admin/audit/verify`
- **Response**: `200 {ok, data:<chain check result>}`. Handler `server.py:3396`.

### GET `/api/admin/orgs`
Cross-org index (member/campaign counts). Read-only, cross-tenant by design.
- **Response**: `200 {ok, data:{orgs:[{..., campaign_count}]}}`. Handler `server.py:3430`.

### GET `/api/admin/orgs/{orgId}/{campaigns|leads}`
Cross-org read of one org's campaigns or leads (reuses the org builders). Route parsed by `_match_admin_org_route` (`server.py:1132`).
- **campaigns**: `200 {ok, data:{campaigns:[{id, displayName, platform, status, createdAt, updatedAt, archived}]}}` (`panel_org.py:463`).
- **leads**: same query params as `/api/leads`; `200 {ok, data:{leads:[{commentId, campaignId, platform, username, text, capturedAt, status, score, reason, extracted, tier}], page, pageSize, total}}` (`panel_org.py:504`).
- `404` unknown org. Handler `server.py:3452`.

### GET `/api/admin/fleet`
- **Response**: `200 {ok, data:{workers:[...]}}`. Handler `server.py:3490`.

### GET/POST `/api/admin/control-flags`
- **GET**: `200 {ok, data:{flags:[...]}}` (`server.py:3736`).
- **POST**: set/clear a control flag. Body `{scope, scopeKey?, clear?:bool, drain?, halt?, updateRequired?, reason?}` — scope ∈ `global|org|platform|worker` (`store.py:852`); `scopeKey` required for non-global; either `clear:true` or ≥1 flag. `_validate_control_flags` (`server.py:1070`). Response `200 {ok, data:{cleared:n}}` or `{flag:<row>}`. Handler `server.py:3702`.

### POST `/api/admin/jobs/enqueue`
Operator enqueue one job. Rejects (400) a job no registered worker can serve.
- **Body**: `{campaignId, platform}` required; optional `orgId`, `requiredAccountHandle`, `engineMode` (`harvest|warming`, default harvest), `targetLeads`, `durationMinutes`, `soulText`, `jobId`. `_validate_enqueue` (`server.py:1210`).
- **Response**: `200 {ok, data:{job}}`; `400` no capable worker. Handler `server.py:3668`.

### POST `/api/admin/workers/revoke`
- **Body**: `{workerId}`. `_validate_worker_revoke` (`server.py:1106`).
- **Response**: `200 {ok, data:{revoked:bool}}`. Handler `server.py:3751`.

### GET/POST `/api/admin/execution-backend`
- **GET**: `200 {ok, data:{backend, options:["in_process","distributed"]}}` (`server.py:3773`).
- **POST**: body `{backend}` ∈ `in_process|distributed` (`store.py:790`); audited. Response `200 {ok, data:{backend}}`; `400` invalid. Handler `server.py:3789`.

### GET/POST `/api/admin/model-comparison`
- **GET**: `200 {ok, data:{enabled:bool, models:[...]}}` (models read from `MODEL_COMPARISON_MODELS` env, display-only) (`server.py:3818`).
- **POST**: body `{enabled:bool}`; audited. Response `200 {ok, data:{enabled}}`; `400` non-bool. Handler `server.py:3836`.

### GET `/api/admin/model-comparison/stats`
- **Response**: `200 {ok, data:{stats, recent}}` (recent limited to 200). Handler `server.py:3863`.

---

## Notes & known gaps

- **`OPTIONS`** on any path returns `204` with CORS headers only for local origins (`server.py:3880`) — a preflight helper, not a data endpoint.
- **Retired env** `AIZU_PLATFORM_ADMINS` (`PLATFORM_ADMINS_ENV`, `server.py:139`) is a named constant only — no code reads it (replaced by the real admin plane). Do not treat it as active.
- Detailed `data` field shapes for `<meta>`, `<integration row>`, `<note>`, worker `job`, control-flag `row`, fleet `workers`, and audit `entries` are produced by `core/store.py` methods (e.g. `upsert_campaign_meta`, `add_note`, `enqueue_job`, `list_workers`, `list_admin_audit`); only the wire keys the handlers/panel builders construct explicitly are documented here. Column-level shapes must be traced in `store.py` rather than inferred.
- The `RUN` block shape comes from `RunManager.status(org_id)` in `runner.py`; the server attaches it verbatim.
- The path-constants block (`server.py:64`–`127`) enumerates the complete set of `/api/*` routes; every one is dispatched in `do_GET`/`do_POST` above.

**Primary sources**: `engine/aizu/server.py` (routing + handlers), `panel.py` / `panel_org.py` (response builders), `rbac.py` (roles), `connections.py` (integration validation), `core/config.py` (platforms), `core/store.py` (status/campaign enums). `dispatch.py` (engine selection) and `core/router.py` (LLM router) have no HTTP surface.
