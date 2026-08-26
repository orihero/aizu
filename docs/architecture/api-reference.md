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
| **Worker** | `Authorization: Bearer <token>` | `_current_worker` (`server.py:1871`) | Token TTL ~1 year; revocation, not expiry, is the off-switch (`store.py:849`). A rejected bearer is always `401`, and the sidecar answers it by clearing its token and halting for re-enrolment — see §9's 401 contract. |
| **Superadmin** | `rr_admin_session` cookie + IP-allowlist + TOTP MFA | `_current_admin` (`server.py:1920`) | Fails closed on the IP-allowlist first. |

### Org-route RBAC gate (`do_POST`, `server.py:1745`)

For protected org routes the ladder is: `401` (no session) → `403` (no `orgId`) → `403` (role lacks the route's action per `_ROUTE_ACTIONS`, `server.py:181`). Per-op finer checks live inside the handlers. One route is deliberately absent from that table: `POST /api/lead/reveal` does its own `reveal_lead` check inside the handler, because the table 403s before any handler runs and a *denied* reveal must still write its audit row. Roles (`rbac.py`): `owner`, `admin`, `member`, `viewer`. Assignable via invite/direct-add: `admin`, `member`, `viewer` (owner is established at signup or by ownership transfer only).

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
| CDP (browser-driven) platforms | `instagram, linkedin, x` | `core/config.py:CDP_PLATFORMS` |
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

> **Two things the customer plane deliberately does NOT return (v27), plus the KEY it renames (v28).** A lead's **identity** — no `username`, no comment `text`, no `reelId` on any lead, anywhere under `/api/*`; the ONE exception is `POST /api/lead/reveal` (§3), and it covers the **handle alone**: one lead per call, audited, metered against the plan's period lead allowance. The comment body and the post pointer have **no org-facing route at all**, audited or otherwise, and `reelId` is denied by the same rule as the words it points at — the post is public and prints the comment in plain sight, so a post link reinstates by redirection exactly what dropping `text` closes. A POINTER TO THE COMMENT IS THE COMMENT. The other withheld thing is the **narrative run event feed**, which `/api/run/activity` folds into scalars and answers as `events: []`. Nothing was deleted: the raw handle, comment and post pointer live on in `matches` and are served by `GET /api/admin/orgs/{id}/leads`, and the full event stream by `GET /api/admin/run/activity` (§10, superadmin + IP allowlist). A missing `username` on an unrevealed customer surface — and a missing comment on every customer surface — is the contract, not a bug. What is *not* redacted: `REELS`/post rows keep their `id`, `caption` and `author` — a scanned post is the product ("here is what the agent read"), and its author is the content creator, not the lead — so leave `_build_reels` alone. That is not a contradiction of the `reelId` rule: what turns a post into a pointer is the JOIN to a *specific lead's* comment, which is why the org-facing reel row also drops every watchlist-derived field (`newSinceLastPoll`, `expiresInDays`, a watchlisted `addedAt`) that would mark which posts produced leads. **v28 closed the hole that made all of the above cosmetic on four platforms** — the lead's own `commentId` used to be the platform id, with the post id inside it; it is now an opaque token. See the lead-key note below the table, which every write endpoint in §3 refers back to.

| Method | Path | Role (view-action) | Envelope |
|---|---|---|---|
| GET | `/api/state?campaign=<id>` | session + orgId (pruned by role in builder) | Raw |
| GET | `/api/dashboard` | `view_dashboard` (owner/admin/viewer) | Raw + RUN |
| GET | `/api/campaigns` | `view_campaigns` (owner/admin/viewer) | Raw + RUN |
| GET | `/api/reports` | `view_reports` (owner/admin/viewer) | Raw |
| GET | `/api/settings` | `view_settings` (owner/admin) | Raw |
| GET | `/api/leads` | `view_leads` (owner/admin/member/viewer) | Enveloped |

> **THE ORG-FACING LEAD KEY (v28) — defined once here, referenced by every endpoint below.** On a customer payload, `commentId` is **not** the platform's comment id. It is `matches.lead_token`: a random per-lead key (`secrets.token_urlsafe(12)`), UNIQUE across the whole table, minted on the row's first INSERT and **never rotated** on a re-poll or a worker sync-back (`upsert_match` deliberately omits it from the `ON CONFLICT DO UPDATE`), so a bookmarked drawer URL keeps working. The field kept the name `commentId` on purpose — renaming it would have churned every write path for no behavioural gain — so the name no longer says what the value is, and this paragraph does.
>
> Why it exists: the real `comment_id` is a **permalink** on four of six platforms. reddit and youtube compose it as `{reel_id}/{comment_id}`, telegram as `{channel/msg}/{reply_id}`, and x uses the reply's own tweet id — so the post id v27 withheld was a *prefix* of a key still shipped on every lead row, and the comment was one hand-built URL away. Instagram and LinkedIn were unaffected. The token carries no platform data on any of the six.
>
> **Every org-scoped lead write resolves it server-side** through `server._resolve_org_lead` (`server.py:3135`) → `Store.resolve_lead_token(org_id, token)`, which is org-scoped and **accepts the token ONLY**. Posting a raw comment id answers `404 "unknown lead"` — deliberately, not incidentally: a route honouring both keys would leave the permalink-bearing one working and make the whole change decorative. Resolution returns the same `None` for "no such token" and "not yours", so it is not a cross-tenant oracle; and since a token is 96 bits of randomness there is no longer a guessable `(campaign, comment)` pair to probe with in the first place.
>
> **Two deliberate asymmetries.** (1) The superadmin lead rows at `GET /api/admin/orgs/{id}/leads` (§10) still carry the **real** comment id — same `include_identity` split as `username`/`text` — so an admin row and a customer row for the same lead do **not** share a key; never carry one across the two planes. (2) The reveal's audit `target` and its period meter uid are still built from the **real** comment id server-side, so the cap and the audit trail keep pointing at the same lead across the v28 upgrade; that uid is never sent to a client.
>
> **Write responses echo the caller's token, never the resolved id.** An echo can only return what the customer already had, so it cannot become a second delivery route for the id the payload just dropped.

### GET `/api/state?campaign=<id>`
Full single-campaign panel state. `?campaign=` selects one campaign (verified in-org, else 404); absent → org home campaign, or an empty state.
- **Response**: **RAW** dict from `build_raw` (`panel.py:770`): keys `CONFIG, CAMPAIGNS, SESSIONS, REELS, MATCHES, PLATFORMS, ESCALATION_LOG, ALERTS, HEALTH, SOUL, DASHBOARD, REPORTS`, plus `TEAM`+`INVITES` (view_team), `INTEGRATIONS` (view_settings), and `RUN` (in-memory). A `member` (leads-only role) gets a pruned `{CONFIG, CAMPAIGNS(stubs), MATCHES}`. `404` unknown campaign; `403` no org. Handler `server.py:4066`.

### GET `/api/dashboard`
- **Response**: RAW `{DASHBOARD, MATCHES, HEALTH, ALERTS, CONFIG}` + `RUN`. Builder `build_dashboard_org` (`panel_org.py:273`). `DASHBOARD` is keyed by period `today/week/month`, each with `leads, goal, cpl, conversion, channels, funnel, bestHour, activeCampaigns, topCampaigns, ticker, leadStatus, pipeline, teamActivity, needsAttention` (`panel.py:360`). `ticker` rows are `{id, intent, platform, score, capturedAt}` — v27 replaced each row's `username` with the lead's `intent`, cut server-side at `TICKER_INTENT_CHARS` (80) on a word boundary so a client cannot widen it.

### GET `/api/campaigns`
- **Response**: RAW `{CAMPAIGNS, SESSIONS}` + `RUN`; each card enriched with `fleetRunId` (run_id of the most-recent active fleet job, else null, `server.py:264`). Builder `build_campaigns_org` (`panel_org.py:299`). Card shape: `id, name, goalType, status, platform, platforms, threshold, languages, extractFields, startedAt, brief, budgetCap, goalTarget, briefForm, spent, leads, cpl, spark, warmth` + lifecycle fields `archivedAt, pausedReason, scheduleEnabled, scheduleKind, scheduleDow, scheduleHour, scheduleMinute, scheduleTz, nextRunAt` (`panel.py:49`, `panel.py:630`).

### GET `/api/reports`
- **Response**: RAW `{REPORTS, HEALTH}`. `REPORTS` keyed by period, each `{labels, matchesByPlatform, cplTrend, spendByStage, platformRanking, perCampaign}` (`panel.py:418`).

### GET `/api/settings`
- **Response**: RAW `{CONFIG, TEAM, INVITES, INTEGRATIONS}`, plus `BILLING` if the role has `view_billing`. Builder `build_settings_org` (`panel_org.py:328`). Same path is POST-write in §6 — split by method.
- `BILLING` carries both entitlements: `leadCap`/`leadsUsed`/`usageRatio`/`nearLimit` and (v27) `campaignCap` (int or **null = unlimited**), `campaignsUsed` (non-archived campaigns, from `panel.org_campaign_count` — the same producer the create gate counts with), and `maxRunLeads` (the largest target one run may request = the org's resolved period cap, so a provisioned Scale org gets its override, not the catalogue's fail-closed 0). Each row of the `tiers` comparison grid gains `campaignCap` too. Gate on `campaignCap !== null`; a falsy check reads unlimited as zero and disables New Campaign for a paying org.

### GET `/api/leads`
Org-wide, server-side filtered/sorted/paginated.
- **Query**: `page` (default 1), `pageSize` (default 50, max 200), `dir` (`asc`/`desc`, default `desc`), `q` (substring over `intent` + `reason` + the `extracted` field VALUES — v27; the handle and comment are no longer in the payload, so searching them would silently match nothing), `status`, `platform`, `campaign` (scopes list + tiles to one campaign), `sort` (`capturedAt|score|intent|platform|status`, default `capturedAt`; `username` is still accepted and sorts every redacted row as `""`, so a stale bundle 200s instead of 500ing). Parsed leniently (`_query_int` — a bad param falls back, never 400s).
- **Response**: **Enveloped** `{ok, data:{items, total, page, pageSize, stats, platforms, campaigns, CONFIG}}`. `stats` = `{total, counts{...}, won, escalated, labeled}`. Builder `build_leads_org` (`panel_org.py:409`); handler `server.py:4036`.
- **Each item is redacted (v27, v28)**: `{id, commentId, campaignId, platform, sessionId, lang, intent, score, reason, extracted, status, escalated, escalationCost, capturedAt{date,time,ts}, statusBy, statusAt, statusHistory[], notes[]}` — **no `username`, no `text`, no `reelId`**, and `commentId` is the **opaque lead token**, not the platform's comment id (see the lead-key note above). `id` is `lead_uid(campaignId, platform, <token>)`, so it composes the token too. The post pointer goes with the comment, not with the product fields: the post is public and the comment is readable on it, so a `reelId` in a list payload is the whole redaction undone one URL at a time. `intent` is the one-line "what this person wants", derived at capture by `core.matching.derive_intent`; it is `""` for a pre-v27 row or when nothing could be derived honestly, and the panel renders a neutral placeholder for that, never a fallback to an identifier. `_build_matches`'s `include_identity` flag is what puts all three back, and only `build_admin_org_leads` (§10) sets it; the audited reveal below adds the handle to ONE lead and never the other two.

---

## 3. Leads / status writes (POST, org session)

| Method | Path | Role |
|---|---|---|
| POST | `/api/status` | `edit_leads` (owner/admin/member) |
| POST | `/api/status/bulk` | `bulk_edit_leads` (owner/admin) |
| POST | `/api/lead/note` | `edit_leads` (owner/admin/member) |
| POST | `/api/lead/reveal` | `reveal_lead` (owner/admin/member) — checked in the handler, **not** in `_ROUTE_ACTIONS` |

### POST `/api/status`
Set one lead's status.
- **Body**: `{campaignId, commentId, status}` required; `platform` optional (default `instagram`); `note` optional but **required** when `status` is a forced-reason status. Validated `_validate_status_request` (`server.py:342`). `commentId` is the **opaque lead token** off the row's own `commentId` field; a raw platform comment id is a `404`.
- **Response**: `200 {ok, data:{commentId, status}}` — `commentId` is the **caller's** token echoed back, never the id it resolved to; `404` unknown campaign (cross-org hidden), unresolvable token, or no matching comment; `400` invalid status / missing forced reason. Handler `server.py:3335`.

### POST `/api/status/bulk`
Set status on up to 500 leads with one shared reason.
- **Body**: `{campaignId, status, items:[{commentId, platform?}], note?}`; `items` non-empty, ≤500; `note` required for a forced-reason status. `_validate_bulk_status` (`server.py:394`). Each item's `commentId` is the **opaque lead token**, resolved per item.
- **Response**: `200 {ok, data:{updated, missing:[commentId...], status}}` (partial misses are not an error). An item whose token does not resolve joins `missing` rather than failing the batch, and `missing` lists the **tokens the caller sent**, so the client can match them against its own rows. Handler `server.py:3375`.

### POST `/api/lead/note`
Create or delete a lead note.
- **Body**: `{op:"create", campaignId, commentId, body, platform?}` (body ≤4000 chars) OR `{op:"delete", noteId:<int>}`. `_validate_lead_note` (`server.py:430`). On create, `commentId` is the **opaque lead token**; an unresolvable one is `404 "unknown lead"`. Delete takes no lead key at all — it is gated on authorship — so v28 does not touch that branch.
- **Response**: create → `200 {ok, data:<note>}`; delete → `200 {ok, data:{noteId, op:"delete"}}`; `404` no note / unknown lead; `403 "only the note's author may delete it"`. Handler `server.py:3417`. **Note the re-mask on create**: `Store.add_note` returns the comment id it was *given*, which is the real one the handler just resolved, so the handler overwrites `commentId` with the caller's token before sending. Overwrite rather than rebuild, so a future note column still reaches the panel — but this is the one response shape where a store dict passes through, and the mask is what keeps it from becoming a write-path route to the id the read path drops.

### POST `/api/lead/reveal`
v27 reveal-on-demand: hand back **one** lead's HANDLE, and audit the attempt. Leads are anonymized by default (§2), which also removed the customer's only way to *contact* one — this is the sanctioned way back, and it goes exactly that far. An org learns what a lead wants (`intent`, on every row) and who to reach out to (this handle); it never learns the words the person wrote, and it is never handed a link to the page where those words are printed.
- **Body**: `{campaignId, commentId}` required, `platform` optional (default `instagram`) — where `commentId` is the **opaque lead token** (v28), which is emphatically *not* the `matches` primary key any more: the PK is exactly what this endpoint refuses. `_validate_lead_reveal` (`server.py:1031`). There is **no list form**: no `commentIds`, no `status`/`limit`/`all`. A bulk path would quietly rebuild the export leak the redaction exists to close, so the shape refuses to widen rather than trusting a handler to cap it.
- **Auth**: authenticated org session + `rbac.can(role, "reveal_lead")` (owner/admin/member — a `viewer` is refused, being the read-only role). The role check runs inside `_handle_lead_reveal`, deliberately absent from `_ROUTE_ACTIONS`, because that table 403s before any handler runs and a **denied** reveal must still write its audit row.
- **Ownership**: the org comes from the SESSION, never from the body (BOLA). A lead that is not this org's, and one that does not exist at all, both answer the same `404 "unknown lead"` — a 403 would confirm the row exists and turn the endpoint into a cross-tenant existence oracle. A token that resolves to nothing is folded into the same answer: same `404`, same message, same `not_found` audit result. Since v28 that defence is structural as well as behavioural — the key is 96 bits of randomness, so there is no guessable `(campaign, comment)` pair left to enumerate.
- **Audit**: one `record_audit(org_id, actor, "reveal_lead", target=<lead uid>, detail={campaignId, platform, result})` row per call. **That `target` uid is built from the REAL comment id, not the token**, and never leaves the server: it is both the audit key and the period meter's key, so building it from the stored id is what keeps the cap and the trail pointing at the same lead across the v28 upgrade rather than silently granting every org a fresh allowance. One row per call, with `result` ∈ `denied|not_found|capped|revealed` — one row for every outcome, refusals included, because a denied or capped attempt is precisely the row that explains a support ticket and the row a scripted enumeration writes over and over. The denial is audited *before* the row is looked up, so what is recorded is that the actor asked, independent of whether the lead exists.
- **Plan cap**: the reveal is metered by the org's period lead allowance (`billing.TIERS` `lead_cap`; free 10 … scale = `subscriptions.lead_cap_override`), and over it the answer is `402` with the billing message. An uncapped per-lead endpoint is a bulk export with extra round-trips — a script walks the anonymized list and reveals every row — so the cap is what makes "one at a time" mean anything. The meter counts **distinct leads, not calls** (`Store.count_reveals_this_period`, reading the same audit rows `Store.reveal_audit_detail` writes): revealed data is never cached client-side, so reopening a drawer re-reveals, and metering calls would spend a Free org's whole allowance on one lead opened ten times. Re-revealing a lead already revealed this period is always free, and that check runs *before* the cap. The cap is also the LAST of the four gates, so a `402` always means "this really is your lead, your allowance is spent" — an org over its cap still cannot use the endpoint as an existence oracle.
- **Response**: `200 {ok, data:{id, commentId, platform, username}}` — the handle and nothing else, constructed field by field, never `dict(match)` minus keys, so a future `matches` column cannot ship to a customer the day it is added. `id` and `commentId` are composed from the **token the caller sent**, deliberately not from the audit/meter uid above; that is also what lets the drawer compare the answer's `id` against the row's own `id` before painting a handle on screen. `text` and `reelId` are absent **by contract**: the comment body is superadmin-only, and the post pointer is held to the same rule because a POINTER TO THE COMMENT IS THE COMMENT — the post it names is public and carries the words in plain sight, so shipping it would reinstate by redirection exactly what dropping `text` closes. (The earlier build returned both, arguing that a handle already unlocks the post so withholding the words was incoherent. That reasoning is retired: the promise is that an org learns *what* a lead wants and *who* to contact, never what they said.) `tests/test_lead_reveal.py` pins the key set exactly, because re-adding either key here is the single edit that undoes the policy. `400` bad body; `403` role refused; `404` unknown/foreign lead; `402` over the period cap. Reveal is a **READ** — it writes no status, no history row, no `updated_at`; the audit row is the only write.

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
- **Body**: `{campaignId}` required; optional `op` ∈ `create|edit`, `status` (campaign-status set), `budgetCap` (≥0), `goalTarget` (≥0 int), `displayName`, `brief` (object; camelCase keys mapped to snake per `_BRIEF_KEYS`, `server.py:562`). `live`/`paused` route through pause semantics (`set_campaign_paused`). `_validate_campaign` (`server.py:457`).
- **Which row it targets** (`_resolve_campaign_target`): a campaign the caller's org already owns is edited in place. An **edit** of anything else — explicit `op:"edit"`, or a legacy payload carrying no brief — 404s for another org's campaign but is allowed for an *unregistered* id (the file-backed `config/campaign.md` campaign has no DB row until its first write; the write stamps it to the caller's org). A **create** — explicit `op:"create"`, or a legacy brief-carrying payload — always allocates a key in the caller's own namespace, `o<orgId>.<requestedId>`, *even when the bare id is free*: two tenants can each hold `q4-outbound`, and since the allocated key is a pure function of the caller's own org it can never reveal whether another tenant holds that name. Clients must therefore read the real id back off the response, not assume the id they asked for. Ids in that reserved `o<digits>.` shape may only be named by the org that owns them (otherwise the namespace would be squattable, since `campaign_meta.campaign_id` is still one global PK).
- **Plan campaign cap** (v27): a **create** — explicit `op:"create"`, or the legacy brief-carrying payload with no `op`, i.e. the same predicate `_resolve_campaign_target` uses — is refused with `402 "Plan limit reached (N campaigns on <Display Name>). Upgrade to add more campaigns."` once the org holds `billing.tier_campaign_cap(tier)` non-archived campaigns (free 1, lite 3, starter/pro/scale unlimited). Checked after the target id is resolved and before the first write. Archived campaigns do not count, so archiving frees a slot; an **edit** is never blocked, whatever the plan. Reads the same `Store.get_subscription` choke point as the run gate, so the settings meter and enforcement cannot disagree.
- **Response**: `200 {ok, data:<meta>}` (meta gains `hasBrief:true` when a brief was stored); `402` plan campaign cap (above); `409 {code:"campaign_exists"}` when a create would land on a campaign the caller already has (refused, never overwritten — `matches` is keyed on `campaign_id`, so a clobber would also inherit the first campaign's leads); `404` cross-org campaign; `400` bad brief shape. Handler `server.py:2218`.

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
| GET | `/api/run/activity?runId=<id>&after=<cursor>` | Live run progress — scalars only, no event feed (v27) |

### POST `/api/run`
Start a run for one campaign or all live campaigns. In-process by default; distributed-backend live runs enqueue to the worker fleet.
- **Body**: exactly one of `{campaignId}` or `{all:true}`; `mode` ∈ `dry|live` (default `dry`); optional `targetLeadCount` (1–1000), `durationMinutes` (1–720). `_validate_run` (`server.py:1291`).
- **Billing gate** (`server.py:2884`): `402` if subscription status ∉ `active|trialing`, or `402` if the period lead cap is exhausted (`remaining = sub["lead_cap"] − count_leads_this_period(...) <= 0`); otherwise `remaining` **clamps** the target — an absent `targetLeadCount` becomes `remaining`, a stated one becomes `min(requested, remaining)`.
- **The clamp is a SOFT bound, not a ceiling** (v27). The engine's stop condition is `counters.matches >= lead_target` on PER-SESSION counters, incremented once per post after a whole comment batch, so one reel with 13 qualifying comments overshoots a target of 10 without ever testing in between (10 targeted, 15 delivered, measured). The HARD enforcement is this gate at the *period* boundary: the next run start 402s once the allowance is spent, so an overshoot self-corrects by shortening the next run. Copy must say "up to N leads per run"; "exactly N" is false.
- **Readiness gate** (in-process **live** runs only): `409` with its OWN shape — `{error:"agent_not_ready", detail, readiness}` (not the `{ok,data,error}` envelope) — when the agent that would execute the run isn't ready. Narrow by design: a `dry` run walks a fake feed, and an API-only campaign (youtube/reddit/telegram) never touches the shared browser, so neither is gated. A campaign with an Instagram channel additionally requires `instagram == "logged_in"`; other CDP channels (linkedin/x) only require CDP reachability. A probe failure allows the run rather than blocking it.
- **Response (in-process)**: `202 {ok, data:{accepted:true, scope, campaignId, mode, targetLeads, maxRunLeads, leadsRemaining}}`; `409 "a run is already active"`; `400` not runnable.
- **Response (distributed)**: `202 {ok, data:{accepted:true, backend:"distributed", scope, jobs:[...], runId, runIds:[...], skipped:[{campaignId,reason}], targetLeads, maxRunLeads, leadsRemaining}}`; `409` if no capable worker / nothing dispatched. Handler `server.py:2853`, fleet dispatch `server.py:2940`.
- The three plan-bound fields (v27) are identical on both paths and are the **surfacing** of the clamp above: `targetLeads` is what the run was actually started with (a silently clamped target is visible rather than a run that quietly stops early), `maxRunLeads` = `billing.tier_max_run_leads(tier)` (= the period allowance), `leadsRemaining` = what was left when the run started. On the distributed path `targetLeads` is the whole clamped budget, which the dispatcher then SPLITS across the enqueued jobs.

### POST `/api/run/stop`
- **Body**: none. **Response**: `200 {ok, data:{stopped:true}}`; `409 "no run is active"` (also covers another org's run). `server.py:3042`.

### POST `/api/run/pause`
- **Body**: none. **Response**: `200 {ok, data:{paused:true}}`; `409` if nothing active. Idempotent. `server.py:3056`.

### POST `/api/run/resume`
- **Body**: none. **Response**: `200 {ok, data:{paused:false}}`; `409` if nothing active. Idempotent. `server.py:3071`.

### GET `/api/run/activity?runId=<id>&after=<cursor>`
Live **progress** for one run — counters, redaction-safe scalars, `finished`, the fleet job, and open health flags. **No narrative events (v27).** Ownership proven in-memory or via org-stamped DB rows.
- **Query**: `runId` required; `after` accepted and **inert** (see `cursor` below). A junk value falls back to 0, never 400s.
- **Response**: `200 {ok, data:{runId, finished, fleetJob, counters:{reelsSeen,relevancePasses,commentsScored,matches,spendUsd,likes,follows}, phase, leadsFound, itemsScanned, relevantFound, lastEventAt, targetLeads, events:[], eventsRedacted:true, flags:[{kind,severity,detail}], cursor}}`; `400` missing runId; `404` unknown/foreign run. Counters aggregated by `_aggregate_run_counters`, the scalars by `_aggregate_run_progress`; handler `_serve_run_activity`.
- **`events` is always `[]` and `eventsRedacted` always `true`.** The key (and `cursor` with it) survives so the panel's poll contract and `after` plumbing are unchanged. `cursor` echoes `after` and never advances — there is nothing to page. The full feed is superadmin-only (`GET /api/admin/run/activity`, §10). A *filtered* feed was rejected as the wrong shape: a `comments/success` detail is literally `{username, score, tier, reelId}` and a `relevance` one `{reelId, author}`, so it would ship exactly the rows being hidden and trust a client-side filter — and the next detail key an engine invents would ride along for free. The scalars are CONSTRUCTED, never an event row with keys deleted. `message`, `detail`, `sessionId`, `campaignId` never appear here.
- **The scalars are folded out of `run_events`, which for a fleet run is the ONLY live signal.** Session counters and captured `matches` both travel in the job ACK body (the sidecar collects leads from the *worker's* local store), so the cloud reads zero for both until ack, while events land on the ~45s job heartbeat. Every aggregate is a max/sum over a growing prefix of the run's own events (read from id 0, capped at `RUN_ACTIVITY_AGGREGATE_EVENTS` = 2000), so all of them are monotonic — a progress number must never fall back down mid-run.
  - `leadsFound` — `comments`/`success` events deduped on `(item id, username)` before counting (`run_events` is append-only per attempt while the store dedupes on comment id, so a retry re-emits), `max`ed against the per-post roll-up and against the authoritative `matches_for_run` count, which is what makes it exact once the run acks. The item id is resolved through an allow-list (`reelId|postId|submissionId|messageId|videoId`) — deduping on `reelId` alone would collapse every non-Instagram run into one bucket.
  - `itemsScanned` / `relevantFound` — `feed_walk` details taken as the highest-`seq` per `session_id` and then SUMMED (one run id spans many sessions), `max`ed against the ack-time counters. `detail.matches` is deliberately never read: it lags the per-comment success events and was observed at 0 against 15 of them.
  - `phase` — the latest event's phase through an explicit allow-list (`lifecycle→starting`, `feed_walk|relevance→searching`, `comments|engage→qualifying`, `halt→stopped`), plus `done`/`failed` from run/job state; an unknown phase degrades to `working`, never a raw internal stage name. Zero events on a live run is `starting`, not "nothing found".
  - `lastEventAt` — `MAX(created_at)`. A timestamp is not a log; it is the liveness beat that keeps the panel's stall banner honest.
  - `targetLeads` — the plan-clamped target, so the panel can show "7 of 10 leads". Only a fleet job carries it durably (off `job.spec.target_leads`); for an in-process run it is `null` here and reaches the panel in the `POST /api/run` 202 instead.
  - `counters.reelsSeen/relevancePasses/matches` are overwritten with the same three aggregates rather than shipped alongside them — a payload carrying "0 reels scanned" next to "searching · 40 scanned" would just make the panel pick a side.
- **`flags` stay.** They drive the "fix your agent" UX and are a state, not a log.
- **Known limitation, present it honestly, do not paper over it.** A dead-lettered job never acks, so its leads stay in the worker's local SQLite and the org's `matches` rows stay 0 forever — `leadsFound` collapses to the event estimate permanently and must not be reset or recomputed to zero when the job flips to `failed`; it is the only record that run will ever have. Spend, by contrast, *is* banked on nack, so such a run reads "$X spent, 0 delivered". A companion `leadsDelivered` (the real row count, so the two can be shown as what they are) is specified but **not yet in the payload**.
- `fleetJob` is `null` for an in-process run, else `{jobId, status, lastEventAt, leaseExpiresAt, reason, attempts, maxAttempts}`. `reason` is the worker's nack code (`cdp_unreachable`, `worker_timeout`, `worker_stall`, `credential_fetch_failed`, `campaign_not_found`, `soul_missing`, `campaign_malformed`, `error`, …) or the acked summary's `halt_reason`, capped at 200 chars, `null` when unknown — key the failed/succeeded wording off `status`, never off `reason` being present.

---

## 5b. Agent readiness (org session)

"Can a live run start right now?" — polled by the panel's global `AgentReadinessBanner` every 60s and reused as the `POST /api/run` gate above. Both answer a **raw** dict (no envelope).

| Method | Path | Role gate |
|---|---|---|
| GET | `/api/agent/readiness[?refresh=1][&campaign=<id>]` | any org session |
| POST | `/api/agent/launch-login` | `fix_agent` (owner/admin) |

What "ready" measures follows the superadmin execution-backend switch:

| Backend | Probed | Ready when |
|---|---|---|
| `in_process` | this box's warmed Chrome (`readiness.check_readiness`) | CDP answers **and** the Instagram session is logged in |
| `distributed` | the worker fleet (`readiness.fleet_readiness`) | ≥1 non-revoked worker is `online` — the cloud has no browser of its own, so probing local CDP would say nothing true |

### GET `/api/agent/readiness`
- **Query**: `refresh=1` forces a live probe past the ≤60s server-side cache. `campaign=<id>` narrows the `distributed` answer to the platforms that campaign actually needs, so a tenant whose only online box advertises `youtube` gets `ready:false` for an Instagram run instead of a green banner and a job nothing can lease; `detail` then names the scope (`"… for instagram"`). An unknown, blank or other-org campaign id is ignored — the endpoint degrades to the unscoped answer and never errors, because this backs a 60s banner poll where a stale id must cost the scope, not the verdict. Ignored in `in_process` mode, which probes the one local browser.
- **Response**: `200 {ready, cdp:"ok"|"unreachable", instagram:"logged_in"|"logged_out"|"unknown", checkedAt, cdpUrl, detail, backend}`; `401` anonymous. In `distributed` mode `instagram` stays `"unknown"` — a box's login state never rides the presence heartbeat — so clients should render `detail`, not the enums.
- A live run holds the one CDP connection this architecture allows, so an in-process check never attaches a second Playwright client mid-run: it serves the last-known snapshot instead.

### POST `/api/agent/launch-login`
Open (or focus) a Chrome tab on instagram.com so a human can sign the warmed browser back in.
- **Body**: `{}`. **Response**: `200 {launched, readiness}` (`launched` is best-effort — `false` with a still-unready snapshot is a normal answer, not an error); `409 {error:"run_active", detail}` while a run is in flight; `500 {error, detail}` if Chrome itself couldn't be started; `403` without `fix_agent`.
- In `distributed` mode this is a no-op `200 {launched:false, ...}`: the warmed Chrome lives on the worker PC and is signed in from that box's own desktop app.

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

Bearer-gated, not cookie/RBAC. Handled before the org gate. Body cap 1 MB.

**Enrolment (v22 — supersedes the shared bootstrap token).** A box's FIRST register presents, in the same bearer slot, a per-worker, single-use, admin-minted **enrolment token** (`worker_enrolment_tokens`, minted from the panel's Fleet page). It is tried first and always wins; its server-assigned scope (`org` or `pool`) is stamped on the worker row as `workers.enrolment_scope_kind` and CLAMPS the `orgId`/`capabilities` written on that call **and on every later re-register**. Only if redemption fails does the server fall back to the shared `AIZU_WORKER_BOOTSTRAP_TOKEN` — a DEPRECATED path gated by `AIZU_WORKER_LEGACY_BOOTSTRAP_ENABLED` (default ON so upgrade day is a no-op; an EMPTY value parses as OFF). A legacy-enrolled box stores `enrolment_scope_kind = NULL` and stays fully self-declaring forever, so `SELECT id, host FROM workers WHERE revoked_at IS NULL AND enrolment_scope_kind IS NULL` must be empty before that flag is flipped off (ledger B8). A **re-register** presents the box's current worker token, not an enrolment token.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/worker/register` | Register / re-register a worker box |
| POST | `/api/worker/heartbeat` | Worker-level presence beat |
| POST | `/api/worker/lease` | Lease one job |
| POST | `/api/worker/jobs/{jobId}/{heartbeat\|ack\|nack\|credential}` | Job-scoped lifecycle |

**401 contract — what a rejected bearer means and what the box does (ledger B10).** Every route here answers `401` — and ONLY `401` — when the presented token is invalid, revoked (`workers.revoked_at`), or past its ~1-year `token_expires_at`; everything else stays inside the `{ok, data, error}` envelope. A store FAILURE behind that gate (DB locked, mid-restore, not yet mounted) answers `503 worker authentication is temporarily unavailable`, NOT `401`: the auth lookup fails closed either way, but a server fault must not be indistinguishable from a revocation, or one bad bridge restart would tell an entire fleet of valid tokens that they were revoked.

That single status is the sidecar's revocation signal, keyed on the code and never on the message text (which differs per route) — but a 401 alone is **not** acted on. `worker/sidecar.py::_note_unauthorized` requires three things before anything irreversible happens: (1) the 401 must carry the dispatch's own `{ok:false, …}` envelope, so a reverse proxy's HTML/empty-bodied 401 (nginx basic-auth, an SSO rule mid-rollout, a captive portal) stays transient; (2) the refused bearer must still be the one in the box's token store, so a 401 for a credential another process already rotated adopts that credential instead of deleting it; (3) it must repeat `_UNAUTHORIZED_CONFIRM_LIMIT` (3) times CONSECUTIVELY across any of register / presence heartbeat / lease / job heartbeat / ack / nack / credential, with any non-401 outcome resetting the count.

On a CONFIRMED revocation `_on_auth_revoked` does exactly five things, once: CLEARS the persisted token through `TokenStore` (file or keyring backend), logs at CRITICAL naming the operator action, stops the pull loop, stops the presence beat (a parked box must be inert, not authenticate forever), and parks the process with its local control surface still serving (status `controls.reenrolmentRequired: true`, so the desktop app shows "re-enrolment required" instead of an idle-looking box). The parked process re-probes register once per 5-minute reminder tick and resumes leasing if the dispatch accepts it again — the recovery path for a 401 that was really a server-side fault. It never re-registers faster than that, and it cannot resurrect itself: **a legacy shared-bootstrap register is refused outright for a worker whose row is revoked** (`401 worker is revoked; re-enrol it with a per-worker enrolment token`), so `register_worker`'s `revoked_at = NULL` can no longer undo an operator's Revoke on the next reboot / watchdog relaunch / desktop "Restart worker". Revocation is durable regardless of what the box does, without waiting for the `AIZU_WORKER_LEGACY_BOOTSTRAP_ENABLED=0` cutover. Recovery is an operator action: mint a fresh single-use enrolment token, put it on the box, restart — redeeming it clears `revoked_at`. A non-401 failure (transport, timeout, 5xx, a 4xx about the body) is transient and keeps its existing retry/backoff: a flaky network must never brick a box.

### POST `/api/worker/register`
Register / re-register a worker box; mints a fresh plaintext token (hash stored), returned exactly once. Response body is suppressed from DEBUG logs (`server.py:1546`).
- **Auth**: existing bearer token (re-register) OR a single-use enrolment token, falling back to the deprecated shared bootstrap secret (first register) — see the enrolment note above.
- **Body**: optional capped strings `machineId` (required on first register), `displayName, host, os, agentVersion`; `orgId:int|null`; `maxSessions:int` (default 1, ≤50); `capabilities:[[orgId|null, platform, accountHandle|null]]` (≤100); optional `preflight` (v23, see below). `_validate_worker_register` (`server.py:963`).
- **Response**: `200 {ok, data:{workerId, token, heartbeatIntervalSec}}`; `401` when the bearer is neither a live worker token, nor a redeemable enrolment token, nor (if still enabled) the bootstrap secret — three consecutive such rejections halt the box (see the 401 contract above); `401 worker is revoked; re-enrol it with a per-worker enrolment token` when the shared bootstrap secret is presented for an ALREADY-REVOKED `machineId` (ledger B10 — the row stays revoked); `503` when the revocation check itself could not run; `400` missing machineId on first register. Handler `server.py:3086`.

### POST `/api/worker/heartbeat`
Worker-level presence beat; returns resolved OR-merged control flags.
- **Auth**: bearer token (identity; body workerId ignored).
- **Body**: optional `{currentSessions|load:int}`, optional `preflight` (v23, see below). `_validate_worker_heartbeat` (`server.py:1021`).
- **Response**: `200 {ok, data:{drain, halt, updateRequired}}`; `401` invalid/revoked token; `503` auth store unavailable; `404` worker vanished mid-flight. Handler `server.py:3142`.

### The `preflight` field (v23 — worker launch self-check, ledger F9/F10/F12)
Every failure this carries produces a box that reads **perfectly healthy** in the fleet console and cannot work: no `AIZU_SECRET_KEY` so the minted token cannot be persisted (F9.1), no `AIZU_WORKER_PLATFORMS` so it registers with `capabilities: []` and can never be leased to (F9.2), no `OPENROUTER_API_KEY` so every live job dead-letters at attempt 5 (F9.3), Chrome on 9333 while the sidecar probes 9222 (F10), or a Chrome that answers `/json/version` while refusing a DevTools attach (B6/D3). Nobody can SSH into these PCs (F12), so the box's own self-check is the only channel that gets the cause to an admin.

- **Shape** — exactly `PreflightReport.to_upstream_wire()` (`worker/preflight.py`):
  ```json
  {"ok": false, "blocking": true, "enforced": true, "ranAt": 1786800000.12,
   "failed": [{"id": "token_persistence", "severity": "fatal", "status": "fail", "detail": "…"},
              {"id": "login.instagram", "severity": "warn", "status": "unknown", "detail": "skipped — CDP endpoint is unreachable"}]}
  ```
  `failed[]` carries every check whose status is not `pass`/`skip`. `title` and `remedy` deliberately never ride the wire — they are UI copy the fleet console resolves client-side from the `id`, which halves the body and keeps operator-facing text under our control rather than a worker's.

  `status` DOES ride along, because `failed[]` mixes `fail` ("we checked, it is broken") with `unknown` ("we could not check at all"), and those need different operator copy. This is not a corner case: the commonest red state of all is Chrome being down, which marks every `login.*` row `unknown` — and remedy-by-`id` alone would render that as "not signed in", sending an admin to fix a login that was never the problem. A row from an older sidecar that omits `status` degrades to `fail`, the reading that never under-reports a problem.
- **Cadence**: always on `register` (including the re-register a box does when its preflight heals); on `heartbeat` only when the summary CHANGED or every 10th beat (~3.3 min). An omitted field means "unchanged" — `store.record_worker_heartbeat` COALESCEs it.
- **Validation** — `_validate_preflight_summary` (`server.py`) is tolerant, TOTAL, and **never** an error return. Non-dict ⇒ dropped. Rows are kept only for a known check id (or `login.<platform>` for a `CDP_PLATFORMS` platform) with severity ∈ `{fatal, warn}`; a bad row is dropped individually, not the whole report. Caps: 16 rows, 200-char `detail`, 8192 bytes total; over budget ⇒ the field is dropped. (The byte budget is sized so a *maximally* verbose report — 16 rows at the full detail length — still fits, since going over drops the report WHOLE and the box with the most to say would otherwise be the one that said nothing. It is still under 1% of the 1 MiB `WORKER_MAX_BODY_BYTES` the endpoint accepts.) **A malformed or oversized blob is never a `400`** (B9 rule: a diagnostic hint must never be the reason a workable box cannot register), and nothing on the auth or lease path may ever branch on it.
- **Enforcement lives on the BOX, not here.** A blocking preflight parks the sidecar's lease loop and makes it register with `capabilities: []`; the server stores what it is told. That means `capabilities: []` now means both "misconfigured" and "parked by preflight" — the `preflight` block is the only thing that distinguishes them.
- `detail` is **worker-authored text rendered in the superadmin console**. Render as text, never markup (E1/E2/F18).

### POST `/api/worker/lease`
Lease one job (capabilities come from the registered row, not the body). Optional bounded long-poll (≤30 s).
- **Body**: optional `{leasePollTimeoutSec:number}` (clamped to 30). `_validate_worker_lease` (`server.py:1262`).
- **Response**: `200 {ok, data:{job, leaseExpiresAt}}` when a job leased, or `200 {ok, data:null}` on an empty queue (never 204); `401`. Handler `server.py:3966`.
- `job` is the whitelist in `store._job_row_to_lease`: `{id, orgId, campaignId, platform, requiredAccountHandle, targetLeads, durationMinutes, engineMode, soulText, campaignBrief, runId, priorSpendUsd, leaseExpiresAt}`. `platformCredentials` is deliberately absent — a worker pulls its own job's credential from the lease-holder-gated `credential` action below. Anything baked into `jobs.spec` but missing from this dict never reaches a genuinely remote worker (B4).
- `priorSpendUsd` (B9) is the campaign's CLOUD-side `SUM(spend_log.usd)`, resolved LIVE at lease time on the same transaction as the claim — never baked at enqueue, since a queued job may sit while other boxes spend against the same campaign. `0.0` when the campaign has no recorded spend. The box subtracts it from its own `AIZU_SPEND_CAP` (`job_runner._effective_spend_cap`) so one ceiling holds across the whole fleet instead of silently resetting per machine. An older worker binary ignores the key (`JobSpec.from_payload` drops unknown keys) and simply behaves as before.

### POST `/api/worker/jobs/{jobId}/{action}` — action ∈ `heartbeat|ack|nack`
Job-scoped lifecycle. URL job_id + bearer token are authoritative (one worker can't touch another's job). Route parsed by `_match_worker_job_route` (`server.py:1227`); dispatcher `server.py:4002`. All actions `401` on an invalid worker token.
- **`heartbeat`**: body optional `{runEvents:[...], runId?}`. Extends the lease; returns `200 {ok, data:{halt, drain, updateRequired, leaseExpiresAt}}` (a lost lease returns `halt:true`). `server.py:4021`.
- **`ack`**: body `{summary?:object, leads?:[...], spend?:[...], dbId?:string}` (leads capped at 500, `MAX_SYNC_LEADS`; spend capped at 50, `MAX_SYNC_SPEND_ROWS`). Returns `200 {ok, data:{recorded:bool}}`. `_validate_worker_ack` (`server.py:1333`); handler `server.py:4091`.
- **`nack`**: body `{reason (required), poison?:bool, retryAfterAt?:number, spend?:[...], dbId?:string}`. Returns `200 {ok, data:{recorded, outcome, retryAfterAt}}`. `_validate_worker_nack` (`server.py:1305`); handler `server.py:4112`.

**`spend` / `dbId` on `ack` + `nack` (B9 fleet spend roll-up).** The only writer of spend is `router._record` on the BOX, into whichever DB that process opened — so before this the cloud `spend_log` never saw a single fleet dollar, the panel showed `spent` $0 / `cpl` `null` for fleet-run campaigns, and every box's cap restarted at $0.

- `spend` is this ATTEMPT's delta, rolled up per `(stage, model)`: `[{stage:string, model:string|null, usd:number, at:number}]`. The worker takes a per-campaign `spend_log.id` high-water mark immediately before the run (`store.max_spend_id(campaignId)`) and ships `store.spend_since(campaignId, cursor, runId)` — an id cursor, not a `run_id` join, because a requeued attempt reuses its `run_id`. `at` is the group's EARLIEST `created_at`, clamped server-side to `min(at, now)`, so a run that spanned midnight is not all bucketed on the ack day by `spend_by_day`. Do NOT confuse this with `summary.spend_usd`, which is the campaign's LIFETIME local total summed once per session.
- The cursor is PARKED on disk per `run_id` (`<state_dir>/run-<runId>.spend-cursor`) and only deleted once dispatch ACCEPTS the ack/nack. An attempt can end with neither — the sidecar dies and `reclaim_offline_jobs` requeues the job pinned to the same box — and a freshly-taken mark would already sit past that attempt's rows, losing those dollars from the cloud permanently and handing the phantom headroom to another box later.
- `spend_since`'s `runId` argument EXCLUDES rows that provably belong to a different run. `JobSpec.lock_key()` is `org-platform-account`, i.e. per PLATFORM, not per campaign, so two jobs for one campaign on different platforms are NOT serialised by the box's single-flight lock; their id windows would otherwise each contain the other's rows, and since the cloud `spend_log` has no unique key both reports get inserted. Rows with no `session_id`, and rows from a PRIOR ATTEMPT of this same run, are always kept.
- Server side each row is FORCED under the job's own campaign/org (the same BOLA guard as `leads`); non-finite / zero / negative / malformed rows are dropped, never raised on; a missing `stage` defaults to `'fleet'`. Written inside the SAME transaction as the ack/nack, so exactly-once rides the existing `leased_by` ownership check — a replayed ack writes nothing. `store._sync_acked_spend`.
- **`nack` carries spend too, and must**: every failure route (child crash, CDP probe failure, halt, operator stop, timeout, stall) funnels through it, and a nack REQUEUE is unpinned — attempt 2 can land on a box with no record of the money attempt 1 spent.
- `dbId` is the reporting box's database identity (`store.database_id`, a `uuid4().hex` persisted in `platform_settings.db_id`). It exists solely so the server can SKIP the roll-up when the worker's `db_path` IS this database — the default same-box topology, since `AIZU_DB` defaults to the same `aizu.db` filename the bridge uses. `spend_log` has an AUTOINCREMENT PK and no unique key, so unlike the idempotent lead sync a second insert would DOUBLE the campaign's spend and trip its cap at half the budget. A worker that cannot read its own `dbId` ships no `spend` at all.

**Enqueue-time skip (best-effort only).** `_dispatch_run_to_fleet` skips a campaign whose `store.total_spend` already meets the ceiling, with `skipped: [{campaignId, reason: "spend cap reached ($25.00 spent of $20.00)"}]`; a single-campaign run turns that into a `409 run not dispatched: <reason>`.

The ceiling comes from `_fleet_spend_cap_usd()`, which reads `AIZU_SPEND_CAP` **from the bridge process** and returns `null` when it is unset/non-numeric/non-positive — in which case NO campaign is skipped. That asymmetry is deliberate: `AIZU_SPEND_CAP` is a WORKER-plane variable, so on a hosted split deployment the bridge does not have it. A hard-coded fallback would make the cloud enforce a ceiling no box uses, and because `total_spend` is a lifetime sum that never resets, any long-lived campaign would eventually `409` forever with no operator control able to lift it.

Enforcement is therefore box-side and authoritative: `job_runner._effective_spend_cap` re-bases the box's own cap against `priorSpendUsd`, and `job_runner.run_one_job` refuses to spawn at all when there is zero headroom, returning `halt_reason: "spend_cap"` — a POISON reason (spend only grows, so retrying cannot help), so the job dead-letters instead of burning its remaining attempts. This is also the only enforcement a REQUEUE sees, since `nack_job` returns a job straight to `queued` without going through dispatch. `campaign_meta.budget_cap` remains display-only and is NOT wired into the run path.

---

## 10. Superadmin plane

Gated by `_current_admin` (IP-allowlist + `rr_admin_session` cookie + TOTP MFA). Env: `admin_auth.ADMIN_IP_ALLOWLIST_ENV`, `ADMIN_TRUSTED_PROXIES_ENV`. `_require_admin` returns `401 "platform admin authentication required"` when unauthenticated.

There is no signup and no password-reset **route** here on purpose — an emailed reset link would be a second, unaudited way into the highest-privilege surface. Both live in the out-of-band CLI (`python -m aizu.admin_bootstrap`, needs shell + DB access): bare to mint an admin, `--reset-password` to re-set an existing one's password, which keeps the TOTP enrolment, revokes that admin's live sessions, and appends `admin.password.reset` to the hash-chained audit log.

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
| GET | `/api/admin/orgs/{orgId}/{campaigns\|leads\|runs}` | Cross-org read of one org |
| GET | `/api/admin/run/activity?runId=<id>` | Full narrative feed for one run (v27) |
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

### GET `/api/admin/orgs/{orgId}/{campaigns|leads|runs}`
Cross-org read of one org's campaigns, leads or runs (reuses the org builders). Route parsed by `_match_admin_org_route` (`server.py:1132`); the subresource allow-list is the whole gate on that half of the path, so adding a name to it is exactly as load-bearing as writing the handler.
- **campaigns**: `200 {ok, data:{campaigns:[{id, displayName, platform, status, createdAt, updatedAt, archived}]}}` (`panel_org.py:463`).
- **leads**: same query params as `/api/leads`; `200 {ok, data:{leads:[{commentId, campaignId, platform, username, text, intent, capturedAt, status, score, reason, extracted, tier}], page, pageSize, total}}` (`panel_org.py:504`). **This is the one surface that still carries a lead's WORDS**: `build_admin_org_leads` is the only caller passing `include_identity=True`, and it shows the handle, the raw comment, and the derived `intent` side by side so an operator can check the summary is honest. Be precise about which half of "identity" is exclusive to it: the **handle** is reachable org-side too, one lead at a time, through the audited and metered `POST /api/lead/reveal` (§3) — but the comment `text` is superadmin-only with no org-facing route at all, and so is `reelId` (carried on the `include_identity=True` rows themselves, not projected into this route's payload by `_admin_lead_row`), because the post it points at prints those words in public. `q` here therefore spans the handle and comment text too. **v28 added a fourth field to that exclusive list, and it is easy to miss because the key name did not change**: `commentId` on these rows is the REAL platform comment id, while the same key on every customer payload is the opaque `lead_token`. So an admin row and a customer row for the same lead compose two different `lead_uid`s, and a key taken from here will 404 against any org-scoped write in §3 — which is correct, since those routes accept the token only.
- **runs** (v27): `200 {ok, data:{runs:[{runId, campaignId, campaignName, mode, status, platforms, startedAt, finishedAt, sessions, leads}]}}`, newest first, capped at `ADMIN_ORG_RUNS_LIMIT` (50) — the picker for the narrative feed below. There is no `runs` table: a run IS the set of `sessions` sharing a `run_id`, folded by `_build_admin_org_runs`, with the org's *active fleet jobs* merged in as started-but-empty runs (a fleet run has no cloud session rows until its job acks, and the run an operator most wants to inspect is the one running right now). `mode` is `live`/`dry` only for runs this process still remembers in `RunManager.status(None)`, else `null` — `sessions` records no mode, so an older run says so rather than guessing.
- `404` unknown org. Handler `server.py:3452`.

### GET `/api/admin/run/activity?runId=<id>&after=<cursor>`
v27: the **full** narrative feed for one run — messages, details, identities — i.e. the rows `/api/run/activity` no longer hands an org. The only route on the server that still emits event text, hence the admin gate.
- **Cross-tenant and NOT org-scoped, by design**: a superadmin picks a run off `/api/admin/orgs/{id}/runs` and inspects it whoever owns it. Same posture as the other Phase 5d read views — read-only, no impersonation, no audit row (only writes and impersonation are audited on this plane).
- **Query**: `runId` required (`400` without it); `after` is a real cursor here and does page. A junk value falls back to 0.
- **Response**: `200 {ok, data:{runId, finished, counters:{...}, events:[...], flags:[{kind,severity,detail}], cursor}}`. An **unknown run answers an empty feed, not 404** — there is no tenant boundary left to protect, and a fleet run that has not yet heartbeated its first event is exactly the run an operator is trying to watch. Handler `_handle_admin_run_activity`.

### GET `/api/admin/fleet`
- **Response**: `200 {ok, data:{workers:[...]}}` — straight pass-through of `store.list_workers()`, no re-serialization. Handler `server.py:3490`.
- Each worker: `{id, orgId, displayName, host, os, agentVersion, maxSessions, currentSessions, capabilities, registeredAt, lastHeartbeatAt, lastSeenAgeSec, status, revokedAt, currentJob, preflight}`.
- `preflight` (v23) is the box's last reported launch self-check, in the shape above, or `null` when it has never reported one (a pre-v23 sidecar — render that as "unknown", never as healthy). `readiness.fleet_readiness` reads the same field: an online worker whose report is `blocking` does not count towards the tenant's readiness banner.

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
- The path-constants block near the top of `server.py` (from `STATE_PATH` down through the `ADMIN_*` constants) enumerates the complete set of `/api/*` routes; every one is dispatched in `do_GET`/`do_POST` above.

**Primary sources**: `engine/aizu/server.py` (routing + handlers), `panel.py` / `panel_org.py` (response builders), `rbac.py` (roles), `connections.py` (integration validation), `core/config.py` (platforms), `core/store.py` (status/campaign enums). `dispatch.py` (engine selection) and `core/router.py` (LLM router) have no HTTP surface.
