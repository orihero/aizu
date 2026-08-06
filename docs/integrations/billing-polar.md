# Polar.sh Billing Integration — Design Document (CORRECTED, build-ready)

> **Status:** Design only — not yet implemented. Implementation is deferred to later phases.
> **Author:** Design session 2026-06-25. **Corrected:** 2026-06-30 (verification + critique pass).
> **Scope of this doc:** the full design + execution plan so a fresh implementer (human or
> agent) can build it cold against the *current* codebase. No billing code has been written yet.
>
> This revision folds in a code-verification pass (every stale path/line/version corrected
> against real source) and a correctness critique (Polar API field names, webhook signing,
> entitlement edge cases, and a real provider-agnostic seam). **Part A** is a corrections
> punch-list of what the prior draft got wrong. **Part B** is the full revised plan.

---

# PART A — Corrections Punch-List

Every stale/wrong/missing claim from the prior draft → corrected value, grouped by section.
Line/path/version anchors below were verified against the live tree on 2026-06-30.

## §3 / general — paths & module placement

| Prior claim | Corrected value |
|---|---|
| `store.py` lives at `engine/aizu/store.py` (§3 header, §3.2) | **`engine/aizu/core/store.py`** — shared cross-cutting modules (`store.py`, `logsetup.py`) live under `core/`. |
| Server/panel/rbac under `engine/aizu/` (implied generic) | Confirmed at **package root**: `engine/aizu/{server.py, panel.py, panel_org.py, rbac.py, secrets.py}`. `billing.py` correctly belongs here at the root (panel/server cross-cut), **not** under `core/` and **not** under `engines/{platform}/`. |
| Codebase is one flat package | It is a **`core/` + `engines/{platform}` split with ONE shared DB/panel.** Billing is org-scoped and platform-agnostic; it touches `organizations`, a new `subscriptions` table, and the shared `matches` table only. |

## §3.2 — schema version & store internals

| Prior claim | Corrected value |
|---|---|
| Schema bump **v10 → v11** for `subscriptions` | **v12 → v13.** `SCHEMA_VERSION = 12` already (`core/store.py:33`). v11 is already allocated to **account warming** (`accounts`, `account_state_changes`, `campaign_accounts`, `account_secrets`); v12 is campaign lifecycle controls. Next free version is **13**. |
| `SCHEMA_VERSION` bump "at line 31" | Constant is at **`core/store.py:33`** (line 31 is blank). |
| (intent) self-healing `_init_schema` + `CREATE TABLE IF NOT EXISTS` | **Confirmed correct.** `_init_schema` at `core/store.py:586` runs `executescript(SCHEMA)` (line 611); `SCHEMA` constant begins line 103; `SCHEMA_VERSION` row written via `INSERT OR IGNORE` + `UPDATE` at lines 688–692. `_add_column_if_missing` helper at line 716 for column-add migrations (not needed here — table is net-new). |
| `set_integration_secret` is the upsert template | **Confirmed** at `core/store.py:2171–2184`: `INSERT … ON CONFLICT(org_id, platform) DO UPDATE SET … = excluded.…`; uses `now = time.time()` for `updated_at` (line ~2176) and Fernet `_cipher()` for the blob. Mirror this for `upsert_subscription` (PK `org_id`). |
| `matches` table / `idx_matches_org` exist for per-org counting | **Confirmed.** `matches` at `core/store.py:113–131`, PK `(campaign_id, platform, comment_id)`, `org_id INTEGER` (nullable, v7 stamp) at line 115, `captured_at REAL` at line 128. `idx_matches_org` created at line 685; `idx_matches_time ON matches(campaign_id, captured_at)` at line 525. |

## §3.3 — server routes / enforcement line anchors

| Prior claim | Corrected value |
|---|---|
| `_handle_run` at **server.py:1405** | **`server.py:1768`.** (Line 1405 is inside `_handle_campaign_schedule`.) |
| `_ROUTE_ACTIONS` at **line 83** | **`server.py:110–129`.** (Line 83 is the `AUTH_LOGOUT_PATH` constant.) |
| `protected_routes` dict at **line 83** | **`server.py:967`** (range 967–986). |
| Path constants "near line 48" | Add `BILLING_*` constants alongside existing path constants near **server.py:54–84** (e.g. next to `RUN_PATH` at line 83). |
| Soft-enforcement insertion "after org_id, before launch" | **Confirmed location:** `org_id = user['orgId']` at `server.py:1779`; campaign validation 1780–1798; `run_manager.launch` at **1803**. Insert the cap/status check **between 1798 and 1803**. |
| Webhook "handled before the protected gate like AUTH_* routes" | **Confirmed pattern.** `do_POST` (server.py:950–1014): logout checked first (959) → `auth_routes` (987) → `protected_routes` + RBAC gate (988–1006). Webhook must be a **public** route handled **before** the protected gate (mirror the AUTH path at ~959–961); it must **not** appear in `protected_routes` or `_ROUTE_ACTIONS`. |
| `_send_json` / `_read_json_body` envelope | **Confirmed.** `_send_json(code, ok, data, error, extra_headers)` at `server.py:917–930`; `_read_json_body(max_bytes)` at 932–948. Follow `_handle_integration` (server.py:1635–1685) as the protected-handler template. |
| RBAC gate: `_ROUTE_ACTIONS.get(path)` → `rbac.can(...)` → 403 | **Confirmed** at server.py:1000–1006. `manage_billing` must be added to `_ROUTE_ACTIONS` for the checkout/portal POST routes. |
| Stdlib-only, no Polar/3rd-party SDK | **Confirmed.** server.py imports (18–50) are stdlib only (`json, logging, os, re, sqlite3, threading, time, http.*, pathlib, urllib.*`) + internal modules. Use `urllib.request` + `hmac`/`hashlib`/`base64`. |

## §3.4 — panel exposure (BIGGEST structural correction)

| Prior claim | Corrected value |
|---|---|
| Add `raw["BILLING"]` to `/api/state` `build_raw()` at **panel.py:672** under `view_settings` | **STALE on two counts.** (1) The architecture **evolved**: org-wide data is now served by **5 per-page endpoints** (`/api/dashboard`, `/api/campaigns`, `/api/leads`, `/api/reports`, `/api/settings`) via `panel_org.py` + `_serve_org_page`. `/api/state` (`panel.py:build_raw`) is now a single-campaign supplementary view. (2) The `view_settings` gate is at **panel.py:780–782**, not 672. |
| BILLING goes in `/api/state` | **BILLING must attach to `/api/settings`** → `build_settings_org` (`panel_org.py:311–329`, returns `{CONFIG, TEAM, INVITES, INTEGRATIONS}`). Add `_build_billing(store, org_id)` (in `panel.py`, mirroring `_build_integrations`) and call it from `build_settings_org`, gated by `rbac.can(role, "view_billing")`. |
| TEAM + INTEGRATIONS are both gated by `view_settings` | **Split.** `TEAM`/`INVITES` are gated by **`view_team`** (panel.py:777); `INTEGRATIONS` by **`view_settings`** (panel.py:780). BILLING should follow the **INTEGRATIONS** model (`view_settings` gate inside the builder). |
| GET billing needs a `_ROUTE_ACTIONS` entry | **No.** `_serve_org_page` (server.py:1954–1976 / route registration ~1892–1897) gates the whole `/api/settings` page by its action; the builder additionally prunes BILLING by `view_billing`. Only the **POST** checkout/portal routes need `_ROUTE_ACTIONS[... ] = "manage_billing"`. |

## §3.5 / §4 — RBAC & frontend (all design-phase assumptions — these do not exist yet)

| Prior claim | Corrected value |
|---|---|
| `rbac.py` PERMISSIONS contains billing actions | **Missing.** PERMISSIONS at `rbac.py:34–53`. Add `'view_billing': frozenset({OWNER, ADMIN})` and `'manage_billing': frozenset({OWNER, ADMIN})` (mirror `view_settings`:40 read + `manage_member`:50 write patterns). |
| `roles.ts` has `view_billing`/`manage_billing` | **Missing.** `roles.ts:17–33` (Action type) + PERMISSIONS matrix (36–53). Add both for owner+admin. |
| `tabs.ts` has a `billing` tab | **Missing.** `tabs.ts:4` `SettingsTab = 'workspace' | 'team' | 'integrations'`; `TABS` array (line 13) has 3 entries. Add `'billing'`. |
| `SettingsRail.tsx` `TAB_ACTION['billing']` | **Missing.** `SettingsRail.tsx:21–25` has 3 keys; gating filter at line 34 (`can(role, TAB_ACTION[tab.key])`). Add `billing → view_billing`. |
| `SettingsPage.tsx` renders billing | `ActivePanel()` at SettingsPage.tsx:14–18 has team/integrations/default cases — **add a billing case**. |
| `panelStateSchema` has `BILLING` | **Missing.** `panelState.ts:619–639` (TEAM/INTEGRATIONS optional at 634–636). Add optional `BILLING`. |
| `settingsPayloadSchema` includes BILLING | **Missing.** `endpoints.ts:79–84` (`CONFIG/TEAM/INVITES/INTEGRATIONS`). Add optional `BILLING`. **This is the schema that matters** since BILLING ships on `/api/settings`. |
| `PanelRepository` has `openCheckout`/`openBillingPortal` | **Missing.** `panelRepository.ts` (~43 methods). Add both to the interface and to `httpPanelRepository.ts` (class at ~line 106) using the existing `postForData<T>` helper (httpPanelRepository.ts:465–496; copy the `createInvite` pattern at ~398). |
| `FakePanelRepository` records checkout/portal | **Confirmed extensible.** fakePanelRepository.ts:57–101 records all writes + `failNextWrite`. Add `checkoutCalls`/`portalCalls` arrays. |
| Shared `Button/Card/Badge/Field` exist | **Confirmed** (`shared/ui/{Button,Card,Badge}.tsx`, `features/settings/SettingsField.tsx`). |
| `/settings/:tab` dynamic, no router change | **Confirmed** (router.tsx:73–75; `resolveTab` tabs.ts:32). |
| `BillingPanel.tsx`, `useBillingMutations.ts` | **Do not exist** — both new files to create. |

## §3.1 / §5 / §9 — Polar API correctness (critique)

| Prior claim | Corrected value |
|---|---|
| Checkout body uses `customer_external_id=str(org_id)` (§3.1, §2) | **WRONG field name (critical).** Polar uses **`external_customer_id`** on both `POST /v1/checkouts/` and `POST /v1/customer-sessions/`. `customer_external_id` → 422; the org link never forms and webhooks can't resolve the org. Use `external_customer_id=str(org_id)` everywhere; keep `metadata={"orgId": org_id}` as a backstop only. |
| `verify_webhook` HMAC key derivation (unspecified) | **Key = raw secret bytes**, not base64-decoded. Polar's `whsec_…` secret: strip the `whsec_` prefix, use the resulting string's UTF-8 bytes directly as the HMAC-SHA256 key. (Polar's SDK base64-*encodes* the secret then the lib decodes it back — net effect = raw UTF-8 bytes. Naively base64-*decoding* `whsec_…` yields the wrong key and **all** signatures fail.) Pin with a real-sandbox signature test vector. |
| `webhook-signature` compared as one whole header | **Header can hold MULTIPLE space-separated `v1,<sig>` tokens** (secret rotation). Split on spaces, strip the `v1,` prefix from each, and `hmac.compare_digest` against **any** token. Never compare the whole header. |
| Handled events: created/updated/active/canceled/revoked + order.* | Add **`subscription.uncanceled`** (clears `cancel_at_period_end`, sets active). Better: derive **all** state (status, cancel flag, period end) from the subscription object on **every** `subscription.*` event rather than per-type branching. Consider `order.refunded` for revoke-on-refund. |
| Response field handling / base URLs | **Confirmed correct — do not change.** Checkout returns `url`; customer-session returns `customer_portal_url`; sandbox `https://sandbox-api.polar.sh`, prod `https://api.polar.sh`; `products` (array of product UUIDs), `success_url`, `metadata`, `customer_email` are all valid current fields. |

## §3.2 / §3.3 — entitlement logic (critique)

| Prior claim | Corrected value |
|---|---|
| `count_leads_this_period(org_id, since)` "monthly" — `since` undefined | **Period anchor must be explicit.** Anchor to the **subscription billing period** (persist `current_period_start`, set from the webhook; or derive `current_period_end − interval`). For Free orgs (no Polar period), fall back to **calendar-month UTC** using the **same `TZ_SQL_SHIFT`** convention `store.py` already uses for day/hour bucketing. `leadsUsed` in `/api/settings.BILLING` MUST use the identical `since` so the UI meter matches enforcement. **(Open decision — see Part B §Open decisions.)** |
| Count rows `WHERE org_id=?` | `matches.org_id` is **nullable** and is `NULL` for orphan/unregistered campaigns. `WHERE org_id=? AND captured_at>=?` correctly excludes NULLs — make that explicit and **log/alert on NULL-org match volume**; add a test that a NULL-org match never counts toward another org's cap. |
| Cap meters "leads" but TIERS also has "run allowance"; enforcement blocks runs | **Two half-specified meters.** Pick ONE: **leads = rows in `matches` scoped by org_id in the period.** **Delete the `run allowance` field** from TIERS. Document burst behavior: the gate blocks *starting* a run when `leadsUsed >= cap`, so the last allowed run may overshoot — accept + document overshoot (no mid-run kill in v1). |
| Block only `past_due`/`canceled` | **Use a closed-set ALLOW-list:** allow only `{active, trialing}` to run (subject to cap); block everything else (`past_due, canceled, unpaid, incomplete, paused`, or any unrecognized status) at 402. Fails safe for new Polar statuses. |
| Webhook idempotency "ignore stale events using period/timestamp ordering" | The upsert as drafted is **unconditional**, so nothing drops stale events; `webhook-timestamp` is delivery time, not state-effective time. Persist `last_event_ts` (Polar `subscription.modified_at`) and apply the update only `WHERE excluded.event_ts > subscriptions.last_event_ts`. Treat `revoked`/`canceled` as terminal for the period. Use `webhook-id` only for exact-redelivery dedup. |
| `get_subscription` returns FREE default | **Confirmed intent; make it the single choke point.** Both `_handle_run` and `_build_billing` must call `Store.get_subscription(org_id)` → `{tier:'free', status:'active', lead_cap:10, current_period_end:None}`. Never let `None` reach the cap comparison (`None >= cap` raises). |

## §9 — risks/notes & logging

| Prior claim | Corrected value |
|---|---|
| Polar token/secret "covered by existing RedactingFilter; add patterns if needed" | **Not covered today.** `core/logsetup.py`: `_SECRET_ENV_VARS` (line 102) = `('OPENROUTER_API_KEY','AIZU_SECRET_KEY','TELEGRAM_API_HASH')`; `RedactingFilter` at 115–138 snapshots these at `configure_logging()` time. **Add `POLAR_ACCESS_TOKEN` and `POLAR_WEBHOOK_SECRET`** to `_SECRET_ENV_VARS`. |
| `test_billing.py` is the integration-test home | `test_multitenancy_server.py` (engine/tests/, in-process server + ephemeral port `serve(port=0)`, `_StubRunManager`, `_req/_post/_get` helpers) is the **integration** home — **extend it**. `test_billing.py` is a **UNIT** test file for `billing.py` (signature verify, parse_event, TIERS mapping). |
| "Provider-agnostic seam" (one line in §9) | **Under-designed.** Define a real `BillingProvider` Protocol with `PolarClient` as first impl; normalize payloads to ONE canonical event; server holds a `PROVIDERS` registry and never names `PolarClient`. `verify_webhook`/`parse_event` must be **per-provider methods**, not shared free functions (PayTechUZ rails are not Standard-Webhooks). Add a `provider` column + neutral `provider_subscription_id`/`provider_customer_id` to the table now. (See Part B §Backend.) |

---

# PART B — Updated Build Plan (build-ready)

A fresh implementer can build from this section alone. All paths/lines/versions are corrected
and verified against the tree on 2026-06-30.

## 1. Context & Motivation

Aizu/AIZU needs paid subscriptions, but **Stripe does not onboard Uzbekistan-based
sellers**, where the business is based. The chosen replacement is **[Polar.sh](https://polar.sh)**,
a **Merchant-of-Record (MoR)** that:

- Onboards UZ sellers and pays out via **Stripe Connect Express** to a local bank account
  (individual or business; KYC required).
- Acts as the legal seller, so **Polar** calculates, collects, and remits global sales tax /
  VAT — Aizu never registers for tax in foreign jurisdictions.
- Supports subscriptions, one-time, and (later) usage-based billing via one API.

This document designs a **billing layer** over the existing multi-tenant panel: customer orgs
subscribe to a tier through Polar's hosted checkout; Polar webhooks keep our DB's subscription
state in sync; the run path softly enforces the active plan.

### Decisions locked during design
| Decision | Choice |
|---|---|
| Plan structure | **Free $0 (default) + self-serve Lite $9.99 / Starter $24.99 / Pro $149 (monthly+annual) + sales-led Scale (per-deal cap)** — see §5 |
| Scope (v1) | **Subscriptions only.** Metered/usage billing is a later phase |
| Enforcement | **Soft** — block *starting new runs* when status ∉ {active, trialing} **or** over the period lead cap; all reads/exports keep working |
| Polar account | Already exists; wire it up (**sandbox first**, then production) |
| Metering unit | **Leads** = rows in the shared `matches` table scoped by `org_id` within the billing period. (No separate "run allowance" meter.) |

### Two-rail bigger picture
Polar covers **international** customers. For **local Uzbek** customers paying in UZS with
Humo/Uzcard, a second rail (Payme/Click/Uzum via **PayTechUZ**) is the eventual plan, behind
the same billing abstraction. **This document covers the Polar rail only**, but the seam is
designed real (a `BillingProvider` Protocol + a canonical event + a `provider` column) so
PayTechUZ becomes a new class + a registry entry with **zero `server.py` edits**.

---

## 2. Key Architectural Decision

Billing is **NOT** a per-org `integration_secrets` connection. That pattern is for each
customer org connecting *their own* third-party account. Billing is the opposite:

> **Aizu (the vendor) has ONE Polar account.** Each customer org is a *customer* inside
> that single Polar account.

Therefore:

- **Polar credentials are platform-level**, configured via **environment variables** (same tier
  as `AIZU_SECRET_KEY`) — *not* per-org, *not* in `integration_secrets`.
- **Per-org we store only the resulting subscription state** in a new `subscriptions` table.
- The link between a Polar customer and our org is carried as **`external_customer_id = str(org_id)`**
  (NOT `customer_external_id` — see Part A) and mirrored in checkout `metadata.orgId` as a
  backstop, so inbound webhooks resolve the owning org deterministically via
  `external_customer_id`. `org_id` is `organizations.id` — an `INTEGER PRIMARY KEY AUTOINCREMENT`
  surrogate (`core/store.py:386`), stable and non-reused, ideal as an external id.
- **One active subscription per org across all rails** is an invariant: an org pays via Polar
  *or* PayTechUZ, not both. The `subscriptions` row carries a `provider` column to record which.

---

## 3. Backend Design

The panel backend is a **stdlib-only** `ThreadingHTTPServer` (`engine/aizu/server.py`) over
SQLite (`engine/aizu/core/store.py`), with a `{ok, data, error}` JSON envelope,
HttpOnly-cookie sessions, and a per-route RBAC gate. The only third-party dependency is
`cryptography` (Fernet). **Do not add the Polar SDK** — use `urllib.request` + `hmac`/`hashlib`/`base64`.

The codebase is a **`core/` + `engines/{platform}` split with ONE shared DB/panel**. Billing is
org-scoped and platform-agnostic; `billing.py` belongs at the package **root** alongside
`server.py`/`panel.py`/`rbac.py` (a panel/server cross-cut, not engine-loop code).

### 3.0 Provider-agnostic seam (design this first)

In `engine/aizu/billing.py`, define a `BillingProvider` Protocol; `PolarClient` is its
first concrete impl. Minimum surface:

```python
class BillingProvider(Protocol):
    def create_checkout(self, tier: str, org_id: int, email: str, success_url: str) -> CheckoutResult: ...
    def create_portal(self, org_id: int) -> PortalResult: ...           # may return a typed "no account yet"
    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> bool: ...
    def parse_event(self, raw_body: bytes) -> CanonicalBillingEvent | ParseError: ...
```

- `parse_event` normalizes provider payloads into ONE internal shape so the
  webhook→`upsert_subscription` reconciliation is written ONCE and never sees a Polar (or
  Payme/Click/Uzum) field name:
  ```python
  CanonicalBillingEvent = {
      "org_id": int,                    # resolved from external_customer_id (fallback metadata.orgId)
      "provider": str,                  # "polar"
      "tier": str,                      # mapped from product id
      "status": str,                    # active|trialing|past_due|canceled|...
      "cancel_at_period_end": bool,
      "current_period_end": float | None,
      "current_period_start": float | None,
      "provider_subscription_id": str | None,
      "provider_customer_id": str | None,
      "event_ts": float,                # subscription.modified_at — monotonic ordering key
      "event_id": str,                  # webhook-id — exact-redelivery dedup
  }
  ```
- `server.py` holds a registry `PROVIDERS = {"polar": PolarClient.from_env()}` and resolves the
  provider per request/route. It must **never reference `PolarClient` by name**.
- `verify_webhook` / `parse_event` are **per-provider methods** (NOT shared free functions):
  PayTechUZ rails are callback/redirect with their own signature schemes, not Standard Webhooks.

### 3.1 `billing.py` (new, at package root)

- **`PolarConfig.from_env()`** — reads + validates at the boundary (model it on
  `secrets.py:SecretCipher.from_env()`, lines 67–75: classmethod, fail-fast, clear errors):
  - `POLAR_ACCESS_TOKEN`
  - `POLAR_WEBHOOK_SECRET` (the `whsec_…` value)
  - `POLAR_SERVER` = `sandbox` | `production` → base URL `https://sandbox-api.polar.sh` / `https://api.polar.sh`
  - `POLAR_PRODUCTS` = JSON map keyed by `tier` then `interval`, e.g.
    `{ "lite": {"month":"<id>","year":"<id>"}, "starter": {"month":"<id>","year":"<id>"},
    "pro": {"month":"<id>","year":"<id>"} }`
    (**Scale is sales-led — no self-serve product id**; see §5). Parse this behind a tolerant,
    never-throw boundary per the llm-json-output rule; validate that each self-serve tier has both
    intervals present at startup (fail-fast).
- **`PolarClient.create_checkout(tier, interval, org_id, email, success_url)`** →
  `POST /v1/checkouts/` with `products=[POLAR_PRODUCTS[tier][interval]]`,
  **`external_customer_id=str(org_id)`**, `customer_email`,
  `success_url`, `metadata={"orgId": org_id}`. Returns hosted-checkout `data["url"]`. Polar
  upserts the customer by `external_customer_id`, so re-running checkout for the same org reuses
  the same Polar customer (idempotent). Treat `customer_email` as prefill only.
- **`PolarClient.create_portal(org_id)`** → `POST /v1/customer-sessions/` body
  `{"external_customer_id": str(org_id)}` → returns `data["customer_portal_url"]`. The
  Polar-hosted portal handles manage/upgrade/cancel/invoices — **we build no invoice UI**. For a
  Free org with no Polar customer yet, return a typed **"no billing account yet"** result, NOT a
  500, so the panel "Manage billing" button degrades cleanly.
- **`PolarClient.verify_webhook(raw_body, headers)`** — **Standard Webhooks** scheme:
  - HMAC-SHA256 over `f"{webhook_id}.{webhook_timestamp}.{raw_body}"`, base64-encoded.
  - **Key derivation:** strip any `whsec_` prefix from the secret, then use the resulting
    string's **UTF-8 bytes directly** as the HMAC key. Do **not** base64-decode the secret.
  - **Header parsing:** split `webhook-signature` on spaces; for each token strip the leading
    `v1,`; accept if the computed signature matches **any** token via `hmac.compare_digest`.
  - Timestamp-skew check. Verify on the **raw bytes before** JSON parsing.
- **`PolarClient.parse_event(raw_body)`** — tolerant, never-throw JSON boundary; returns a
  `CanonicalBillingEvent` or an error value, never an uncaught raise. Derive **all** state from
  the subscription object on every `subscription.*` event (don't branch per type).
- **`TIERS`** — the entitlement catalogue: `tier → {lead_cap, display_name, prices:{month, year}}`.
  **No "run allowance" field.** `FREE` is the **implicit default**. Confirmed (§5): `free` $0/cap 10,
  `lite` $9.99/$99·cap 50, `starter` $24.99/$249·cap 250, `pro` **$149/$1,490·cap 2,000**,
  `scale` custom/cap **per-deal** (the catalogue default is a placeholder; the real cap lives in
  `subscriptions.lead_cap` set when the deal is provisioned — see §3.2). The `lead_cap` is
  **interval-independent** — only price/renewal differ by interval. **No tier uses a `None` cap.**

**Handled webhook events:** `subscription.created`, `.updated`, `.active`, `.canceled`,
`.revoked`, **`.uncanceled`**, plus `order.created`/`order.paid` (and optionally
`order.refunded`). Unknown types → ack 200 and ignore. Practically, route every `subscription.*`
through the same state-derivation path.

### 3.2 `core/store.py` — new table + schema bump **v12 → v13**

`SCHEMA_VERSION` is at **`core/store.py:33`** (currently `12`). Bump to **13**. Add the table to
the `SCHEMA` constant (so fresh DBs get it via `executescript`, line 611) as
`CREATE TABLE IF NOT EXISTS`, placed near the other tables (~line 527, after `run_events`). The
version-bump path (`INSERT OR IGNORE` + `UPDATE` at 688–692) is idempotent; the existing v7→v12
one-shot migrations are not re-run destructively. No `_add_column_if_missing` migration is needed
(net-new table). v13 comment: `'v13: billing subscriptions (Polar + provider-agnostic, soft run-cap)'`.

```sql
CREATE TABLE IF NOT EXISTS subscriptions (
    org_id                   INTEGER PRIMARY KEY,        -- one active sub per org across all rails
    provider                 TEXT NOT NULL DEFAULT 'polar',
    tier                     TEXT NOT NULL DEFAULT 'free',   -- free|lite|starter|pro|scale
    interval                 TEXT,                           -- month|year (NULL for free); from checkout/webhook
    lead_cap_override        INTEGER,                        -- NULL = use TIERS[tier].lead_cap; set per-deal for scale
    status                   TEXT NOT NULL DEFAULT 'active', -- active|trialing|past_due|canceled|...
    provider_subscription_id TEXT,                       -- provider-neutral (was polar_subscription_id)
    provider_customer_id     TEXT,                       -- provider-neutral (was polar_customer_id)
    current_period_start     REAL,                       -- set from webhook; anchors the cap window
    current_period_end       REAL,
    cancel_at_period_end     INTEGER NOT NULL DEFAULT 0,
    last_event_ts            REAL NOT NULL DEFAULT 0,     -- monotonic ordering (subscription.modified_at)
    updated_at               REAL NOT NULL
);
```

`org_id` is the PK (already an index), so no extra index is required for the current design.

New `Store` methods (immutable upserts, mirroring `set_integration_secret` at
`core/store.py:2171–2184`):

- **`get_subscription(org_id) -> dict`** — returns the **FREE default** when no row exists:
  `{tier:'free', status:'active', lead_cap:10, current_period_end:None, provider:None}`. Never
  returns `None`. This is the single choke point for both enforcement and the panel. **Effective
  cap** = `row.lead_cap_override` if set, else `TIERS[tier].lead_cap` — resolve it here so callers
  always read one `lead_cap`. Scale rows carry the negotiated number in `lead_cap_override`.
- **`upsert_subscription(org_id, **fields)`** — `INSERT … ON CONFLICT(org_id) DO UPDATE SET …`,
  but **conditional on monotonic ordering**: apply only `WHERE excluded.last_event_ts >
  subscriptions.last_event_ts`. Treat `revoked`/`canceled` as terminal precedence for the
  period. Use `now = time.time()` for `updated_at` (mirror line ~2176). Use `webhook-id` (event
  id) for exact-redelivery dedup only.
- **`period_since(org_id) -> float`** *(CONFIRMED anchor — billing period)* — the cap window start.
  For a paid org, return the persisted **`current_period_start`** (set from the webhook). For a Free
  org (no Polar period), fall back to the **start of the current calendar month in UTC**, computed
  with the same `TZ_SQL_SHIFT` convention `store.py` already uses for day/hour bucketing. Both
  enforcement and the `leadsUsed` meter MUST call this single function so the UI never disagrees
  with the gate.
- **`count_leads_this_period(org_id, since) -> int`** — `SELECT COUNT(*) FROM matches WHERE
  org_id=? AND captured_at >= ?` with `since = period_since(org_id)`. **Meter = ALL surfaced
  matches** (CONFIRMED 2026-06-30): **no status predicate** — rejected/archived leads still count
  (the engine work/value is in surfacing them; keeps billing predictable and `leadsUsed` monotonic
  within a period). **No platform predicate** either — the cap aggregates every
  rail/engine for the org (add a one-line comment asserting this). NULL-org rows are correctly
  excluded; separately log/alert on NULL-org match volume. `captured_at` is set on first insert
  and the `ON CONFLICT DO UPDATE` deliberately does NOT touch it (`core/store.py:888–906`), so a
  lead's period membership is its first-capture time and is stable across re-scores — add a
  regression test asserting re-upsert does not move/double-count.

### 3.3 `server.py` — routes, gate, enforcement

- **Path constants** (near server.py:54–84, beside existing path constants):
  ```python
  BILLING_CHECKOUT_PATH = "/api/billing/checkout"
  BILLING_PORTAL_PATH   = "/api/billing/portal"
  BILLING_WEBHOOK_PATH  = "/api/billing/webhook"   # public; provider-routed in handler
  ```
- **Webhook = PUBLIC.** In `do_POST` (server.py:950–1014), handle it **before** the protected
  gate, mirroring the AUTH path checked at ~959. It must **not** be in `protected_routes` or
  `_ROUTE_ACTIONS`. Handler flow:
  1. resolve provider (today only `polar`; design as `/api/billing/webhook` for Polar, but call
     through the provider object, not `PolarClient` statics);
  2. read the **raw** body;
  3. `provider.verify_webhook(raw_body, headers)` → **401** on failure;
  4. `provider.parse_event(raw_body)` → `CanonicalBillingEvent`;
  5. resolve org via `external_customer_id` (fallback `metadata.orgId`); reject an event whose
     provider ≠ the org's stored `provider` (single-active-rail invariant);
  6. `upsert_subscription(...)` (monotonic; stale events dropped);
  7. return **200** on any verified event (even unknown types — avoid retry storms).
- **Checkout + portal = PROTECTED (POST).** Add to `protected_routes` (server.py:967) and to
  `_ROUTE_ACTIONS` (server.py:110–129) under **`manage_billing`**. Handlers follow the
  `_handle_integration` template (server.py:1635–1685) using `_read_json_body` (932–948),
  `_validate_*`, and `_send_json` (917–930):
  - `_handle_billing_checkout(payload)` → `{tier, interval}` (tier ∈ `lite`/`starter`/`pro`;
    interval ∈ `month`/`year`; reject `free`/`scale` and unknown values) → `{checkoutUrl}`
  - `_handle_billing_portal(_payload)` → `{portalUrl}` (or a typed "no billing account yet")
- **Soft enforcement + run clamp** in `_handle_run` — insert **between server.py:1798 and 1803**
  (after campaign validation, before `run_manager.launch`). `org_id` is already resolved at
  server.py:1779. Logic:
  ```python
  sub = store.get_subscription(org_id)              # never None; lead_cap already effective
  if sub["status"] not in ("active", "trialing"):   # closed-set allow-list, fails safe
      → HTTP 402
  cap = sub["lead_cap"]                              # always an int now (no tier uses None)
  since = store.period_since(org_id)                 # billing-period anchor (CONFIRMED), free→cal-month UTC
  used = store.count_leads_this_period(org_id, since)
  remaining = cap - used
  if remaining <= 0:
      → HTTP 402                                     # nothing left this period
  # CLAMP (CONFIRMED 2026-06-30): a run can never exceed the plan.
  payload["target_leads"] = min(payload.get("target_leads", DEFAULT_TARGET), remaining)
  ```
  402 message must be actionable: include the cap and reset date (period end), e.g.
  `"Plan limit reached (250 leads). Resets {date}. Upgrade to keep running."` When a run is clamped
  (started with `remaining < requested`), surface a one-line notice in the run/activity feed so the
  user knows the run stopped at the plan limit, not on its own target. Reads, leads, and exports
  remain unaffected; there is **no mid-run kill** — the clamp bounds the run's target up front so
  the period total lands at/under `cap`.

### 3.4 `panel.py` / `panel_org.py` — expose `BILLING` on `/api/settings`

Org-wide data is served by the 5 per-page endpoints via `_serve_org_page` (server.py routes
~1892–1897; `_serve_org_page` 1954–1976). Billing is org-wide, so it ships on **`/api/settings`**,
NOT `/api/state`.

- Add **`_build_billing(store, org_id) -> dict`** in `panel.py` (mirror `_build_integrations`):
  returns `{tier, interval, status, periodEnd, cancelAtPeriodEnd, leadCap, leadsUsed, usageRatio,
  nearLimit, tiers}` (`tiers` carries each tier's `prices:{month,year}` for the comparison grid).
  `leadsUsed` MUST use the **same `since`** (`period_since`) as enforcement (§3.2). Include a
  soft-warn `nearLimit` flag (~80%) so the UI can warn before the wall.
- Call it from **`build_settings_org`** (`panel_org.py:311–329`, which returns
  `{CONFIG, TEAM, INVITES, INTEGRATIONS}`) gated by `rbac.can(role, "view_billing")` — following
  the **INTEGRATIONS** model (gated in the builder, not via `_ROUTE_ACTIONS`). The `/api/settings`
  page itself is already action-gated by `_serve_org_page`.

### 3.5 `rbac.py` — new permissions

Add to `PERMISSIONS` (`rbac.py:34–53`), both owner/admin (billing is sensitive):
```python
"view_billing":   frozenset({OWNER, ADMIN}),
"manage_billing": frozenset({OWNER, ADMIN}),
```
Mirror in the frontend `roles.ts`. `view_billing` gates the builder; `manage_billing` gates the
checkout/portal POST routes via `_ROUTE_ACTIONS`.

### 3.6 `core/logsetup.py` — redact Polar secrets

Add `POLAR_ACCESS_TOKEN` and `POLAR_WEBHOOK_SECRET` to `_SECRET_ENV_VARS`
(`core/logsetup.py:102`) so `RedactingFilter` (115–138) snapshots and masks them at
`configure_logging()` time.

### Backend files touched
`engine/aizu/billing.py` *(new)* · `core/store.py` · `server.py` · `panel.py` ·
`panel_org.py` · `rbac.py` · `core/logsetup.py` · `engine/tests/test_billing.py` *(new, unit)* ·
extend `engine/tests/test_multitenancy_server.py` *(integration)*.

---

## 4. Frontend Design (`admin-panel/src/`)

Stack: React 19 + TS + Tailwind 4 + Vite + React Query + React Router v7 (hash routing) + Zod,
with a `PanelRepository` interface (Http + Fake impls) and a never-throw `Result<T>` boundary.
Billing slots into the existing **Settings tab** pattern — **no router changes** (`/settings/:tab`
is dynamic, router.tsx:73–75). Billing data arrives on **`/api/settings`** (the `SettingsPayload`),
not `/api/state`. Implement RBAC first to avoid rail gating issues.

- **`shared/auth/roles.ts`** *(do first)* — add `view_billing`/`manage_billing` to the `Action`
  type (17–33) and PERMISSIONS matrix (36–53) for owner + admin.
- **`features/settings/tabs.ts`** — add `'billing'` to `SettingsTab` (line 4) + the `TABS` array
  (line 13; Lucide `CreditCard` icon).
- **`features/settings/SettingsRail.tsx`** — `TAB_ACTION['billing'] = 'view_billing'` (the
  record at 21–25; gating filter at line 34 hides it for non-owner/admin).
- **`features/settings/BillingPanel.tsx`** *(new)* — current plan + status + renewal date, a
  tier-comparison grid (**5 tiers**: Free / Lite / Starter / Pro / Scale), a **monthly⇄annual
  toggle** (showing "~2 months free" on annual), a usage meter (with the `nearLimit` warn state),
  and a **Manage billing** button (→ Polar portal). Follow the WorkspacePanel layout (`Card` >
  header/body) with shared `Button`/`Badge`/`Field`/`Toggle`. Per-tier CTA: **Lite/Starter/Pro →
  "Upgrade"** (self-serve checkout, passing the selected `interval`); **Scale → "Talk to sales"**
  (mailto/contact, **no** checkout call), showing "Custom" not a price; **Free → no CTA**.
- **`features/settings/SettingsPage.tsx`** — add a `billing` case to `ActivePanel()` (14–18).
- **`shared/schemas/panelState.ts`** — add a `billingSchema` (tier, interval, status, periodEnd,
  cancelAtPeriodEnd, leadCap, leadsUsed, usageRatio, nearLimit, tiers — each tier carrying
  `prices:{month,year}`) and a `.optional()`
  `BILLING` key (panelState.ts:619–639; role-pruned like TEAM/INTEGRATIONS).
- **`shared/api/endpoints.ts`** — add optional `BILLING` to `settingsPayloadSchema` (79–84). **This
  is the schema that carries billing**, since it ships on `/api/settings`.
- **`shared/types/domain.ts`** — `Billing` type inferred from the schema.
- **`shared/api/panelRepository.ts`** + **`httpPanelRepository.ts`** — add
  `openCheckout(tier, interval)` → `POST /api/billing/checkout {tier, interval}` and
  `openBillingPortal()` → `POST /api/billing/portal`, both returning a URL via the existing
  `postForData<T>` helper (httpPanelRepository.ts:465–496; copy the `createInvite` pattern ~398).
- **`shared/hooks/useBillingMutations.ts`** *(new)* — `useOpenCheckout`, `useOpenBillingPortal`;
  on success `window.location.assign(url)`.
- **`useSettings()` hook** — no change; `BILLING` auto-populates once the backend adds it to
  `/api/settings`.
- **Tests/fixtures** — extend `FakePanelRepository` (fakePanelRepository.ts:57–101) with
  `checkoutCalls`/`portalCalls` arrays; add a `buildBilling()` fixture (fixtures.ts);
  `BillingPanel.test.tsx`; extend `SettingsRbac.test.tsx` to assert billing visibility is
  owner/admin only.

### Data flow (checkout)
```
User picks interval (monthly/annual) + clicks Upgrade (BillingPanel)
  → useOpenCheckout(tier, interval).mutate()
  → repository.openCheckout(tier, interval)  → POST /api/billing/checkout {tier, interval}
  → backend PolarClient.create_checkout(tier, interval, ...)  → { checkoutUrl }
  → window.location.assign(checkoutUrl)        (Polar hosted checkout)
  → user pays on Polar
  → Polar webhook → POST /api/billing/webhook → verify → parse → upsert_subscription
  → user returns via success_url
  → next /api/settings includes BILLING.status = "active"
  → React Query refetch → BillingPanel re-renders; blocked runs now allowed
```

### Frontend files touched
`features/settings/{tabs.ts,SettingsRail.tsx,SettingsPage.tsx,BillingPanel.tsx}` ·
`shared/schemas/panelState.ts` · `shared/api/{endpoints.ts,panelRepository.ts,httpPanelRepository.ts}` ·
`shared/types/domain.ts` · `shared/hooks/useBillingMutations.ts` · `shared/auth/roles.ts` ·
`test/{fakePanelRepository.ts,fixtures.ts}` · `features/settings/{BillingPanel.test.tsx,SettingsRbac.test.tsx}`.

---

## 5. Tier / Entitlement Defaults (CONFIRMED 2026-06-30)

| Tier | Monthly | Annual (~2 mo free) | Billing path | Period lead cap | Notes |
|------|--------:|--------------------:|--------------|----------------:|-------|
| Free | **$0** | — | — (implicit default, no checkout, no Polar customer) | **10** | Default for every new org |
| Lite | **$9.99/mo** | **$99/yr** | Polar self-serve hosted checkout | **50** | Entry on-ramp; softens the Free→Starter jump |
| Starter | **$24.99/mo** | **$249/yr** | Polar self-serve hosted checkout | **250** | |
| Pro | **$149/mo** | **$1,490/yr** | Polar self-serve hosted checkout | **2,000** | |
| Scale | **Custom — "Talk to sales"** | — | **Sales-led; no self-serve checkout** | **Per-deal cap (negotiated)** | UI shows a *Contact sales* CTA; cap stored on the subscription row, enforced + alerted like other tiers (§3.2) |

Implied price-per-lead: Lite ~$0.20, Starter ~$0.10, Pro ~$0.075 (volume discount preserved —
Pro keeps Starter's tier-over-tier discount despite the higher price).
Caps live in `billing.TIERS` and are surfaced via `/api/settings.BILLING`. Lite/Starter/Pro
products are created in Polar **with both a monthly and an annual price** (one product, two
prices, or two product ids per tier — whichever Polar's dashboard yields); **Scale is not a
self-serve Polar product** — provisioned per deal.

**Annual / `interval`:** every paid tier carries an `interval` (`month` | `year`). The chosen
interval flows checkout → webhook → the `interval` column (§3.2) → `/api/settings.BILLING` so the
UI can show "/mo" vs "/yr" and the renewal date. The lead **cap does not change with interval** —
annual just changes price and renewal cadence, not entitlement.

### Scale = sales-led: design consequences
1. **No self-serve checkout for Scale.** `create_checkout` is only ever called with
   `lite`/`starter`/`pro`. `POLAR_PRODUCTS` only needs those three (each month+year).
2. **Scale has a per-deal numeric cap, NOT `None`** (CONFIRMED 2026-06-30). The negotiated lead
   allowance is stored in `subscriptions.lead_cap` (per-row override; see §3.2) and enforced
   exactly like other tiers, with a soft `nearLimit` alert at ~80%. This surfaces abuse against the
   actual deal rather than leaving Scale unbounded. (`None` cap is no longer used by any tier.)
3. **UI renders a "Contact sales" CTA** for Scale (mailto/form), not Upgrade → checkout.

---

## 6. Inputs Needed Before Implementation (Polar account exists)

1. **Sandbox access token** → `POLAR_ACCESS_TOKEN` (build/test first).
2. **Webhook signing secret** (`whsec_…`) → `POLAR_WEBHOOK_SECRET`, from a sandbox webhook
   endpoint pointed at `<panel-url>/api/billing/webhook`.
3. **Product IDs** for **Lite, Starter, Pro — each with a monthly AND an annual price**
   (Lite $9.99/$99 · Starter $24.99/$249 · **Pro $149/$1,490**), sandbox → `POLAR_PRODUCTS` nested
   map (`tier → {month, year}`). Create the **three** self-serve subscription products (each with two
   prices/intervals) in the Polar **sandbox** dashboard. **Scale has no self-serve product** — its
   per-deal `lead_cap_override` is set manually on the subscription row when provisioned.
4. ~~Confirm tier names, caps, prices~~ — **DONE (2026-06-30, §5).**
5. **A real Polar sandbox webhook delivery** (id + timestamp + body + `webhook-signature` header)
   captured to pin the `verify_webhook` encoding direction in a unit test.
6. Later: **production** equivalents of 1–3 when flipping `POLAR_SERVER=production`.

> `AIZU_SECRET_KEY` already exists and is unrelated — Polar creds are separate env vars.

---

## 7. Build Phases (test-gated)

1. **Provider seam + backend core.** Define `BillingProvider` Protocol, `CanonicalBillingEvent`,
   `PolarConfig.from_env()` (nested `POLAR_PRODUCTS` tier→interval, both-intervals fail-fast),
   `PolarClient` (checkout(tier,interval)/portal/verify_webhook/parse_event), `TIERS` (5 tiers incl.
   `lite`, interval-independent caps). Add the `subscriptions` table (+ `interval`,
   `current_period_start` columns) + **v12→v13** migration + `get_subscription`/
   `upsert_subscription` (monotonic)/`period_since`/`count_leads_this_period`. Add Polar env vars to
   `_SECRET_ENV_VARS`.
   **Gate:** `pytest tests/test_billing.py` — signature accept/reject (real sandbox vector +
   rotation multi-sig header), tolerant parse, tier→cap map (incl. lite + month/year), FREE default,
   monotonic upsert drops a stale event, NULL-org match never counts, `period_since` billing-period
   vs free-cal-month.
2. **Backend wiring.** `PROVIDERS` registry; public webhook route (verify→parse→resolve org→
   upsert, 200/401); protected checkout/portal handlers + `manage_billing` in `_ROUTE_ACTIONS`;
   `_handle_run` soft enforcement (1798–1803, closed-set allow-list + cap); `_build_billing` on
   `/api/settings` via `build_settings_org` gated by `view_billing`; RBAC perms.
   **Gate (extend `test_multitenancy_server.py`):** signed webhook upserts a row; bad signature →
   401; checkout/portal → URL for owner/admin, 403 for member/viewer; over-cap/non-active org →
   `POST /api/run` 402, active org 200; `/api/settings.BILLING` present for owner/admin, absent
   for viewer; revoked-then-stale-active leaves org canceled.
3. **Frontend.** roles.ts → tabs/rail → BillingPanel → schemas (panelState + endpoints) →
   repository (Http + Fake) → hooks → fixtures/tests. **Gate:** `npm test`, coverage ≥ 80%,
   `SettingsRbac` billing visibility owner/admin only.
4. **Sandbox end-to-end.** Real Polar sandbox checkout → webhook → state flips active → a
   previously-blocked run starts. Then document production cutover (`POLAR_SERVER=production` +
   prod token/secret/products) and the UZ-payout verification (§9).

---

## 8. Verification Strategy

- **Backend unit** (`cd engine && pytest tests/test_billing.py`): Standard-Webhooks accept/reject
  pinned to a **real sandbox** vector; multi-signature (rotation) header acceptance; malformed-body
  tolerance; tier→cap mapping; FREE default; monotonic upsert; NULL-org exclusion.
- **Backend integration** (extend `tests/test_multitenancy_server.py`; in-process server +
  ephemeral port, `_StubRunManager`, `_req/_post/_get`):
  - signed webhook → `subscriptions` upsert; bad signature → 401; stale out-of-order event dropped.
  - `POST /api/billing/checkout`/`/portal` → URL for owner/admin; 403 for member/viewer.
  - over-cap / non-active org → `POST /api/run` **402**; active org → 200; new org (no row) → Free,
    runs until 10 leads then 402.
  - **clamp:** org with `used=8, cap=10` requesting `target_leads=25` launches with `target_leads`
    clamped to **2**; Scale row with `lead_cap_override` enforces that number, not a tier default.
  - `/api/settings.BILLING` present for owner/admin, absent for viewer.
- **Frontend** (`cd admin-panel && npm test`): `BillingPanel` renders plan/status/usage; Upgrade
  calls `openCheckout(tier)`; Manage calls `openBillingPortal()` (via `FakePanelRepository`);
  Scale shows "Talk to sales", no checkout call. Coverage ≥ 80%.
- **Sandbox manual:** run the panel, Upgrade → complete Polar **sandbox** checkout → webhook lands,
  tier flips active in Settings → Billing, previously-blocked run starts.

---

## 9. Risks & Notes

- **UZ payout must be confirmed with Polar** *in writing* before production: that the UZ
  entity/bank receives payouts via Stripe Connect Express (Uzbekistan is listed as supported, but
  country-listing ≠ confirmed UZS deposit). Verify (a) Stripe Connect Express accepts the UZ
  account, (b) payout currency/threshold/fees, (c) KYC tier. Sandbox build/test is unblocked
  regardless. **This caveat is blocking for production cutover only.**
- **Webhook signing is the #1 silent-failure risk** — wrong key derivation rejects every
  legitimate webhook. Use raw secret bytes (Part A), parse multi-sig headers, pin to a real
  sandbox vector. Stdlib-only hand-rolled path MUST be test-pinned.
- **Webhook idempotency + ordering** — `webhook-id` for exact-redelivery dedup; `last_event_ts`
  (subscription `modified_at`) for monotonic ordering so a delayed `updated(active)` can't
  re-activate a `revoked` org.
- **Implicit Free default** — orgs with no `subscriptions` row read as Free via `get_subscription`
  (status `active`, cap 10); no backfill needed. Never let `None` reach the cap comparison.
- **NULL-org leads** — orphan-campaign matches have `org_id = NULL` and cannot count toward any
  cap; this is correct but must be logged/alerted, and a test must assert they never count toward
  another org's cap.
- **No secrets in logs** — `POLAR_ACCESS_TOKEN` + `POLAR_WEBHOOK_SECRET` added to
  `core/logsetup.py:_SECRET_ENV_VARS`.
- **Lead cap ≠ infra cost (monitor).** The cap bounds *value delivered* (leads surfaced), not the
  *cost to produce* them. On the shared-pool distributed-worker model, an org can burn many
  low-yield runs (proxy/compute/account-warming cost) while staying under its lead cap. v1 accepts
  this; watch cost-per-lead per org and consider a future fair-use run/time guard if abuse appears.
- **Provider-agnostic seam** — `BillingProvider` Protocol + canonical event + `provider` column +
  `PROVIDERS` registry; PayTechUZ becomes a new class + registry entry, no `server.py` edit. The
  public webhook handler calls through the provider object, never `PolarClient` statics; a
  per-provider path (`/api/billing/webhook/<provider>`) can be adopted when the second rail lands.

---

## Decisions resolved 2026-06-30 (folded into the plan above)

1. **Cap period anchor → BILLING PERIOD.** Persist `current_period_start` from the webhook; Free
   orgs fall back to calendar-month UTC via `TZ_SQL_SHIFT`. One `period_since(org_id)` function
   drives BOTH enforcement and the displayed `leadsUsed` (§3.2, §3.3).
2. **Annual pricing → YES, now.** Lite $99/yr · Starter $249/yr · Pro $1,490/yr (~2 months free).
   `interval` (`month`|`year`) added to the schema, TIERS, `POLAR_PRODUCTS`, checkout, and UI from
   day one; cap is interval-independent (§3.1, §3.2, §5).
3. **Overage UX → HARD BLOCK + 80% WARNING.** v1 = HTTP 402 on *starting* a run at cap, plus a soft
   `nearLimit` flag (~80%) and an actionable message incl. reset date. Last allowed run may
   overshoot; no mid-run kill in v1 (§3.3, §3.4).
4. **Free→Starter on-ramp → ADD LITE TIER.** New self-serve **Lite** $9.99/mo ($99/yr) / 50 leads
   between Free and Starter (§5).
5. **Overshoot → CLAMP RUN TO REMAINING.** `_handle_run` clamps `target_leads` to `cap − used` so a
   run can never exceed the plan (§3.3), with a clamp notice in the activity feed. No mid-run kill.
6. **Lead meter → ALL SURFACED MATCHES.** Rejected/archived leads still count; no status predicate
   (§3.2). Simple, predictable, monotonic-within-period.
7. **Scale fair-use → PER-DEAL CAP ON THE ROW.** Negotiated allowance in `subscriptions.
   lead_cap_override`, enforced like other tiers + `nearLimit` alert at ~80% (§3.2, §5). No tier
   uses a `None` cap anymore.
8. **Pro pricing → $149/mo ($1,490/yr) / 2,000 leads.** Raised from $74.99/1,000 to better capture
   B2B willingness-to-pay while preserving Pro's $0.075/lead volume discount over Starter (§5).
9. **Mid-period change → UPGRADE IMMEDIATE, DOWNGRADE AT PERIOD END.** An upgrade raises the
   effective cap immediately (they paid more); a downgrade only lowers it at `current_period_end`
   (driven off Polar's scheduled-change webhook / `cancel_at_period_end`), so an org is never 402'd
   for leads already paid for at the higher tier. Persist the scheduled tier and apply it when the
   period rolls.

**No open decisions remain — the plan is decision-complete and build-ready.**

---

## 10. References

- Polar payout accounts — https://polar.sh/docs/finance/accounts
- Polar payouts (fees / thresholds / timing) — https://polar.sh/docs/features/finance/payouts
- Polar supported countries — https://polar.sh/docs/merchant-of-record/supported-countries
- Polar webhooks (Standard Webhooks spec) — https://polar.sh/docs/integrate/webhooks/endpoints
- Polar checkouts API (`external_customer_id`) — https://polar.sh/docs/api-reference/checkouts
- Polar customer sessions API — https://polar.sh/docs/api-reference/customer-sessions
- PayTechUZ (future local rail) — https://pay-tech.uz/en/ · https://github.com/paytechuz/paytechuz
