# Account Warming System — Implementation PRD

**Status:** Build spec (v1) · **Author:** Lead architect (synthesis) · **Date:** 2026-06-29
**Schema target:** v11 (current `SCHEMA_VERSION = 10`, store.py:31)
**Scope:** Managed-pool account warming for account-backed platforms — **X + LinkedIn first**, designed to extend to **Instagram**. Telegram is deferred (no write/keep-alive surface — §11, gap #5).

> This PRD synthesizes five verified design dimensions (lifecycle/data-model, engine session-mode, warmth score, pool/scheduler, panel UI, infra) and adopts every verifier correction. Where dimensions contradicted each other, the resolution is stated inline under **RESOLVED**.

---

## 0. Cross-dimension contradictions — resolved up front

These three conflicts recurred across dimensions. They are decided here once and every section below conforms.

| # | Contradiction | RESOLUTION |
|---|---------------|------------|
| **C1** | LOCKED decisions say warming **performs light writes** (follow/like/connect). ENGINE MAP convention #11 + several dimension drafts said warming is **pure observation**. | **Warming WRITES.** The LOCKED decision governs. ENGINE MAP convention #11 is **incorrect and superseded**. Warming performs bounded, ramped engagement via `store.log_action`. The warmth `network` component therefore has a real data source. Read-only **harvest** is preserved by structural isolation (§4). |
| **C2** | Several dimensions read `health_flags`, `sessions`, and `actions` "scoped to an account" but **no account_id linkage exists** on those tables. | Add **`account_id INTEGER`** (nullable) to `sessions` and `health_flags` and `actions` via the self-heal `_add_column_if_missing` idiom (additive, not a PK reshape — matches the repo's own org_id/run_id precedent). This is the canonical join path. **BUT** the additive idiom cannot relax the existing `sessions.campaign_id NOT NULL` (store.py:160) and `actions.campaign_id NOT NULL` (store.py:205), and a pool warming session backs an *account*, not a campaign — it has no `campaign_id`. **RESOLVED via sentinel, not migration (D1):** warming sessions/actions are stamped with a reserved per-org sentinel campaign id `__warming__:<org_id>` (a TEXT value, satisfies NOT NULL with zero schema reshape). The sentinel is never a real campaign, is filtered out of all harvest/campaign queries, and `account_id` (not `campaign_id`) is the warmth join key throughout §5. (§3.2, §3.3) |
| **C3** | Warmth was described as both **persisted** (`accounts.warmth_score` column) and **computed-on-read** (panel constraint). | **Computed-on-read only.** No `warmth_score` column. The account row persists only **lifecycle `state`** + **ramp state** (`ramp_day`, `warmth_floor`). The 0–100 score is derived fresh on every panel read. (§5) |

Other resolved minor conflicts:
- **Naming:** the harvest/warming axis is `engine_mode` (NOT `mode` — `mode` already means `dry`/`live` in cli/runner/server). (§4)
- **Migration idiom:** column adds use `_add_column_if_missing(...)` in the open path, **not** a `_migrate_to_v11()` data-fold method. New tables use `CREATE TABLE IF NOT EXISTS` in the SCHEMA block. (§3)
- **MIN vs derived warmth:** per-campaign warmth = the score of its **resolved backing account** (pool model), with campaign-session density as a secondary term — not a blind `MIN` across many accounts (which wedges a campaign on one cold account). Reassign-on-cool prevents the wedge. (§5, §6)

---

## 1. Problem & goals

**Problem.** The harvest engine is read-only and halts on challenges, so it can only run against accounts that are already "warm" (trusted by the platform). Today there is no system to take a freshly-logged-in account from cold → harvest-ready, nor to keep it warm. Cold accounts get challenged/banned the moment harvest touches them.

**Warming is per-ACCOUNT, not per-campaign.** The durable warming entity is the account. Campaigns *consume* a warmed account from an org-level pool. A campaign's surfaced warmth is derived from its backing account.

**Goals (v1):**
- Automate the **ramp** (cold → harvest-ready) and **upkeep** (sustain warmth) of a managed pool of account-backed identities.
- Surface a **0–100% Warmth Score** per campaign and gate harvest on it.
- Keep harvest **structurally read-only** while warming performs deliberate light writes.
- Ship **X + LinkedIn**; generalize cleanly to **Instagram**. (Telegram deferred — §11.)

**Non-goals (v1):**
- **Account creation** is OUT — an operator manually creates + logs in N accounts once. Warming automates ramp + upkeep from a logged-in account onward.
- **YouTube + Reddit** are OUT — API-key/quota platforms with no account to warm. They never get an `accounts` row.
- **BYO accounts** — noted alternative only (see §9).

---

## 2. Locked decisions & open decisions

### Locked (design within these)
1. **Managed pool** — we own and warm the accounts.
2. **Generic framework** across account-backed platforms; **X + LinkedIn first**, extend to Instagram. `WARMABLE_PLATFORMS = {x, linkedin, instagram}` in v1. YouTube + Reddit explicitly out; **Telegram deferred** (no warming surface — §11).
3. **Account creation out of v1**; warming automates ramp + upkeep only.
4. **Engagement-for-warming is a deliberate policy shift** — harvest stays read-only (likes/follows are no-ops, halts on challenge); warming performs light writes. Must be cleanly isolated.
5. **Warmth Score** is a 0–100% composite per campaign, derived from the backing account. Default gate target = 40% ("good to continue").

### OPEN — for the product owner (flagged, not decided here)

| ID | Decision | Recommendation | Impact if unset |
|----|----------|----------------|-----------------|
| **O1** | **Gate semantics:** single 40% vs two-tier (≥40 gentle / ≥70 full). | Two-tier. | Ship data for both; selector is parameterized. |
| **O2** | **Hard-block vs warn-but-allow** below the gate. | Hard-block <40, warn 40–69. | One-line component branch either way. |
| **O3** | Run when the only pool account is `cooling`/`flagged`? | Hard-block + alert; offer reassign. | §6 reassign-on-cool covers the common case. |
| **O4** | Per-account concurrent-Chrome ceiling + CDP port scheme. | Static `BASE_PORT + ordinal`, cap N/host. | §8. |
| **O5** | `cooling` auto-entered (engine) or operator-only. | Auto on soft-flag streak. | §4 reconcile. |
| **O6** | Ramp numbers (caps/stage, dwell windows, act-probability, FULL_AGE_DAYS=21, TARGET_CONNECTS, penalty windows). | Conservative starts in §5. | Provisional; tune in P1. **Must be tuned so the day-14 reachable max score ≥ `gateFull` (70)** — see §10 consistency note: with FULL_AGE_DAYS=21 the `age` component alone is capped at 0.67 by day 14, so the day-14 ceiling depends entirely on near-full ramp/network/trust. If the KPI proves unreachable, raise the day-14 target or lower FULL_AGE_DAYS, not silently. |
| **O7** | Circuit-breaker trip threshold (N halt-flags / window). | N=3 within 24h, per (org, platform). | §9. |
| **O8** | Trend storage: recompute-as-of-T-7d vs `warmth_snapshots` table. | Defer trend to fast-follow; ship base-only or omit. | §5, §7. |

---

## 3. Account lifecycle & data model (schema v11)

### 3.1 Lifecycle state machine
States (persisted on `accounts.state`; transitions logged append-only):

```
provisioned  -> warming
warming      -> ready | flagged
ready        -> active | cooling | warming | flagged
active       -> ready | cooling | flagged
cooling      -> warming | ready | flagged
flagged      -> warming        (operator-cleared only)
```

- `provisioned` — operator logged in once; no warming yet.
- `warming` — ramp running; not harvest-eligible.
- `ready` — warmth gate satisfied; harvest-eligible; upkeep continues.
- `active` — currently backing live harvest campaign(s).
- `cooling` — backed off (rate concern / post-incident), warming-only.
- `flagged` — checkpoint/challenge/login-drift detected; **hard-blocked from BOTH harvest and warming** until an operator clears it.

`HARVEST_ELIGIBLE = {ready, active}`; `WARMING_ELIGIBLE = all − {flagged}`. The pure guard `can_transition(from, to) -> bool` lives in new `core/accounts.py`, mirrored TS-side later. Eligibility is the coarse persisted gate; the warmth **score** is the fine-grained derived number (§5). Consistent by construction: `state in {ready, active}` ⟺ score cleared the gate at last transition.

### 3.2 Schema delta (v11 — additive only)

Bump `SCHEMA_VERSION = 11` (store.py:31) with the comment-trail convention.

**New tables** (in the SCHEMA block via `CREATE TABLE IF NOT EXISTS` — the exact v10 `run_events` precedent; fresh DBs build them, existing DBs gain them on open, **no `_migrate_to_vN` code**):

```sql
-- accounts: first-class, per-(org, platform). Identity columns immutable; state/ramp/detail mutate in place.
CREATE TABLE IF NOT EXISTS accounts (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id       INTEGER NOT NULL,
  platform     TEXT NOT NULL,            -- x | linkedin | instagram | telegram
  username     TEXT NOT NULL,
  state        TEXT NOT NULL DEFAULT 'provisioned',
  profile_dir  TEXT,                     -- CDP: Chrome --user-data-dir; NULL for API platforms (telegram)
  cdp_port     INTEGER,                  -- per-account debug port (BASE_PORT + ordinal); NULL for API
  fingerprint  TEXT,                     -- JSON, written once at provision, never mutated
  ramp_day     INTEGER NOT NULL DEFAULT 0,
  warmth_floor REAL NOT NULL DEFAULT 0,  -- ramp curve target for ramp_day
  consecutive_flag_count INTEGER NOT NULL DEFAULT 0,
  last_warmed_at REAL,
  last_active_at REAL,
  cooling_until  REAL,
  detail       TEXT,                     -- JSON {login_status, checkpoint, last_activity_kind, cookie_backup_at}
  added_at     REAL NOT NULL,            -- subsystem onboarding time (see §3.5)
  updated_at   REAL NOT NULL,
  UNIQUE(org_id, platform, username),
  UNIQUE(cdp_port)
);
CREATE INDEX IF NOT EXISTS idx_accounts_org ON accounts(org_id, platform);

-- append-only audit of lifecycle transitions (lead_status_changes idiom)
CREATE TABLE IF NOT EXISTS account_state_changes (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL,
  org_id     INTEGER NOT NULL,
  from_state TEXT,
  to_state   TEXT NOT NULL,
  reason     TEXT,                       -- 'warmth_gate_passed' | 'checkpoint_detected' | ...
  session_id TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_account_changes_acct ON account_state_changes(account_id, id);

-- campaign -> account assignment (pool model). No row => default pool pick; row => pin.
CREATE TABLE IF NOT EXISTS campaign_accounts (
  campaign_id TEXT NOT NULL,
  org_id      INTEGER NOT NULL,
  platform    TEXT NOT NULL,
  account_id  INTEGER NOT NULL,
  pinned      INTEGER NOT NULL DEFAULT 0,
  assigned_at REAL NOT NULL,
  PRIMARY KEY (campaign_id, platform)
);

-- per-account encrypted secrets (Fernet via existing SecretCipher). NEW table chosen over
-- adding account_id to integration_secrets (that PK is (org_id, platform) -> would be a reshape).
CREATE TABLE IF NOT EXISTS account_secrets (
  org_id     INTEGER NOT NULL,
  platform   TEXT NOT NULL,
  account_id INTEGER NOT NULL,
  enc_blob   TEXT NOT NULL,              -- Fernet(JSON): telegram MTProto session, or cookie-backup
  updated_at REAL NOT NULL,
  PRIMARY KEY (org_id, platform, account_id)
);
```

**Column adds to existing tables** (self-heal idiom — unconditional, idempotent `_add_column_if_missing(c, table, coldef)` in the open path, mirroring v10 run_id/org_id self-heal at store.py ~551-556; **NOT** a `_migrate_to_v11` method):

```python
_add_column_if_missing(c, "sessions",     "engine_mode TEXT NOT NULL DEFAULT 'harvest'")
_add_column_if_missing(c, "sessions",     "account_id INTEGER")            -- nullable; warming stamps it
_add_column_if_missing(c, "health_flags", "account_id INTEGER")            -- nullable; account-keyed flags
_add_column_if_missing(c, "actions",      "account_id INTEGER")            -- nullable; warming writes stamp it
```

> `engine_mode` (not `mode`) avoids the dry/live collision. `account_id` on `sessions`/`health_flags`/`actions` is the **C2** join path that makes per-account warmth and the IntegrationsPanel tile actually queryable in SQL. `actions.action_type` is extended (data only) to include `connect` for LinkedIn.

**Sentinel campaign id for pool sessions/actions (D1, resolves C2 NOT NULL).** `sessions.campaign_id` and `actions.campaign_id` are both `NOT NULL` (store.py:160, 205) and the additive `_add_column_if_missing` idiom **cannot** relax a NOT NULL constraint without a table rebuild (out of scope — we keep migrations additive). A pool warming session has no campaign. **Resolution:** a reserved sentinel TEXT value satisfies the constraint with zero reshape:

```python
WARMING_SENTINEL_CAMPAIGN = lambda org_id: f"__warming__:{org_id}"   # core/accounts.py
```

- The sentinel is **never** inserted into `campaign_meta` and is **never** returned by `list_campaigns`/`build_campaigns`/panel queries (all of which already join `campaign_meta` or filter known ids; add an explicit `campaign_id NOT LIKE '__warming__:%'` guard to the few raw-`sessions`/`actions` scans the panel uses for harvest stats so warming rows never pollute harvest counters).
> - `org_for_campaign(sentinel)` would return NULL (no `campaign_meta` row), which is exactly why warming paths pass `org_id`/`account_id` **explicitly** to `start_session`/`log_action`/`raise_flag` (§3.3) rather than relying on campaign→org derivation.
> - All warmth signals join on `account_id` (§5.2), **never** on the sentinel `campaign_id` — the sentinel exists only to satisfy NOT NULL.

**No change** to `campaign_meta` (warmth is computed-on-read), `integration_secrets`, or any PK or NOT NULL constraint.

### 3.3 New / changed Store methods
```
add_account(org_id, platform, username, *, profile_dir=None, cdp_port=None, fingerprint=None) -> int
get_account(account_id) -> Optional[dict]
list_accounts(org_id, platform=None, state=None) -> list[dict]            # WHERE org_id=?
update_account_lifecycle(account_id, to_state, *, reason, session_id=None, **fields) -> dict   # _tx: writes accounts.state + account_state_changes row iff can_transition else InvalidAccountTransition
update_account_ramp(account_id, *, ramp_day, warmth_floor, last_warmed_at=None) -> dict
resolve_account_for_campaign(campaign_id, platform) -> Optional[dict]     # pool pick; ASSERTS account.org_id == org_for_campaign(campaign_id)
assign_account(campaign_id, platform, account_id, *, pinned=False) -> None  # SAME cross-org assert
warmth_for_campaign(campaign_id, *, now) -> WarmthScore                   # §5, computed-on-read
account_warmth(org_id, platform, account_id, *, now) -> WarmthScore       # IntegrationsPanel
account_health_by_platform(org_id, *, now) -> dict[str, dict]
resolve_flag(flag_id, org_id, *, to_state=None) -> bool                   # SOLE writer of health_flags.resolved_at; flag-resolve + optional lifecycle transition in ONE _tx
raise_flag(kind, severity, detail='', *, campaign_id=None, session_id=None, org_id=None, account_id=None)  # EXTENDED: org_id/account_id now explicit
start_session(session_id, campaign_id, platform='instagram', *, run_id=None, org_id=None, engine_mode='harvest', account_id=None)  # EXTENDED: stamps sessions.engine_mode + account_id; campaign_id is the §3.2 sentinel for warming
log_action(campaign_id, action_type, *, reel_id=None, target, succeeded, session_id=None, account_id=None)  # EXTENDED: reel_id now optional (warming follow/connect has none); account_id stamped; campaign_id is the §3.2 sentinel for warming
put_account_secret / get_account_secret(org_id, platform, account_id) -> ...   # Fernet via SecretCipher
```

**`start_session` is a real signature change (resolves gap #1).** Today `start_session(session_id, campaign_id, platform, *, run_id, org_id)` (store.py:976) takes neither `engine_mode` nor `account_id`, and `sessions.campaign_id` is NOT NULL. Warming calls it as `start_session(sid, WARMING_SENTINEL_CAMPAIGN(org_id), platform, engine_mode='warming', account_id=acct_id, org_id=org_id)` — passing `org_id` explicitly (the sentinel campaign has no `campaign_meta` row, so the existing `org_for_campaign` fallback would return NULL). The INSERT gains the `engine_mode`/`account_id` columns. Harvest call sites are unchanged (defaults preserve today's behavior).

**`log_action` is a real signature change (resolves gap #2).** Today `log_action(campaign_id, action_type, *, reel_id, target, succeeded, session_id)` (store.py:1069) makes both `campaign_id` and `reel_id` mandatory; `actions.campaign_id` is NOT NULL. Warming follows/connects have **no campaign and no reel**. We make `reel_id` optional (the column is already nullable, store.py:207) and add `account_id`; warming calls `log_action(WARMING_SENTINEL_CAMPAIGN(org_id), 'follow', reel_id=None, target=<handle>, succeeded=..., account_id=acct_id, session_id=...)`. Without this the `network` warmth component (§5.2) — the signal that justifies the entire "warming WRITES" policy (C1) — has **no viable insert path** and is permanently 0.

**Cross-org integrity (verifier #7/#L):** `resolve_account_for_campaign` and `assign_account` MUST assert `account.org_id == store.org_for_campaign(campaign_id)` before writing `campaign_accounts`; reject otherwise. Read-scoping alone is insufficient. `resolve_account_for_campaign` returns **None (not an error)** for non-warmable platforms or when no pool account exists — `warmth_for_campaign` short-circuits to the §5.8 neutral default on None (resolves gap #6).

**`raise_flag` is a real signature change** (verifier #1, infra-#2): today it derives `org_id` only from `campaign_id` via `org_for_campaign`. A pool warming session backs an account, not a campaign, so it has no real `campaign_id` → org would land NULL and the panel banner would never surface the flag. We add explicit optional `org_id` and `account_id` params; account-keyed warming flags pass them directly.

**`resolve_flag` transaction boundary (resolves gap #7):** `resolve_flag` is the SOLE writer of `health_flags.resolved_at`. It MUST, in a **single `_tx`**: (1) verify the flag's `org_id` matches the caller's `org_id` (reject cross-org), (2) set `resolved_at`, and (3) when `to_state` is given, apply the lifecycle transition (`flagged → warming|ready`) via the same `update_account_lifecycle` write — so an operator can never resolve the flag yet leave the account stuck in `flagged`. Flag-resolve and lifecycle transition are atomic or neither happens.

### 3.4 Chrome profile → account mapping
One Chrome profile per account (a profile holds at most one logged-in identity per domain). `accounts.profile_dir` + `accounts.cdp_port` are the stable binding: `${AIZU_PROFILE_ROOT}/${platform}/${account_id}` on port `BASE_PORT(9333) + ordinal`. The engine **never launches** Chrome (cdp.py:108) — it attaches over CDP to the operator-warmed instance. v1 back-compat: if `accounts` is empty for an org/platform, synthesize ONE legacy account row pointing at the existing default profile (idempotent via the UNIQUE constraint).

### 3.5 `added_at` provenance (verifier high)
Account creation is out of v1, so `added_at` = **subsystem-onboarding time**, NOT true account age. The age component (§5) is therefore labeled **"time under warming management,"** not "account age," so it is not mislabeled. If the operator can supply a real genesis date at `warm-register`, capture it into `detail.genesis_at` and prefer it.

---

## 4. Warming-session engine mode (ramp + read-only isolation)

### 4.1 Dispatch — do NOT forward `engine_mode` into harvest engines (verifier critical)
The harvest call site stays **byte-for-byte unchanged** — the strongest guarantee read-only harvest survives:

```python
def run_engine_session(*, campaign, store, router, feed, soul, pacer,
                       run_id=None, lead_target=None, engine_mode="harvest"):
    if engine_mode == "warming":
        from .engines.warming.session import run_warming_session
        return run_warming_session(campaign=campaign, store=store, router=router,
                                   feed=feed, soul=soul, pacer=pacer, run_id=run_id)
    run_session = select_engine(campaign.platform)          # existing fan-out, verbatim
    return run_session(campaign=campaign, store=store, router=router, feed=feed,
                       soul=soul, pacer=pacer, run_id=run_id, lead_target=lead_target)
```

The six harvest `run_session` signatures are **not touched**. Only `run_warming_session` is new.

### 4.2 Mode trigger end-to-end (verifier medium)
- `Campaign` gains `engine_mode: str = "harvest"` (validated `{harvest, warming}`), sourced from the campaign brief JSON (`campaign_briefs` detail evolves freely), round-tripped by `campaign_to_brief`.
- CLI gains `--engine-mode {harvest,warming}` (default harvest); CLI override wins.
- `cli._run_one` threads `engine_mode` into `run_engine_session`.
- `cli._run_session_loop` branches on `engine_mode == "warming"` **before** the `_SINGLE_PASS_PLATFORMS` check so X/LinkedIn warming is single-pass even though they normally loop to a lead target:
  `single_pass = (engine_mode == "warming") or (campaign.platform in _SINGLE_PASS_PLATFORMS)`
- `cli._build_run_io`: when warming, force home-feed-only config (`include_home_feed=True`, empty seeds) **and** construct the warming Pacer with an account-tz clock **and `enforce_daytime` forced on**: `Pacer(PacingConfig(enforce_daytime=True), clock=lambda: datetime.now(ZoneInfo(account.fingerprint.timezone_id)))`. **Forcing `enforce_daytime=True` is mandatory, not cosmetic (resolves gap #3):** `enforce_daytime` is resolved at `PacingConfig` construction from `_enforce_daytime_default()` reading `AIZU_IGNORE_DAYTIME` (pacing.py:32, 39). Overriding only `clock` leaves the env opt-out fully active — and live runs set `AIZU_IGNORE_DAYTIME=1`, so warming would write at 3am. An explicit `PacingConfig(enforce_daytime=...)` always wins over the env (pacing.py:23), so this is the actual mechanism that enforces §9.3. `clock` is already a Pacer constructor param (pacing.py:45); harvest keeps both defaults, so the harvest daytime guard is **not** modified (infra-#3).
- Kill-switch (env + org settings) is checked **here, before `build_feed`** (verifier medium), so a disabled warming campaign never attaches a live Chrome.

### 4.3 Engagement isolation (C1 — warming WRITES; harvest does not)
The leak being sealed is **Instagram-only** (X/LinkedIn already hardcode `likes=0/follows=0`). Concrete plan:

- **Harvest** forces `enable_actions=False`. Instagram's `_maybe_like/_maybe_follow` keep their summary keys (`likes`/`follows`) but are **forced to 0** in harvest. ActionPolicy is moved into the warming package (re-point `tests/test_actions.py`); the shipped `config/campaign.md` `enable_actions: true` is flipped.
- **Warming** is the ONLY writer. `WarmingActionExecutor` (constructed only inside `WarmingSession`) is the sole caller of feed write helpers and the sole path that calls `store.log_action(...)`.
- **Isolation is a single-caller convention + a test guard** (verifier medium — NOT "structurally unreachable," since write helpers remain public on the shared `FeedSource`). `tests/test_warming_isolation.py` greps that no `engines/*/session.py` except `warming/` references `like_reel`/`follow_author`/`connect`, and asserts harvest mode forces `enable_actions=False`.
- Warming NEVER calls `upsert_match` / `add_to_watchlist` / `log_spend` / `router.score`. Zero ML inference.

### 4.4 Ramp as data (`engines/warming/ramp.py`)
Account-age-in-days (from `accounts.added_at`) → `ActionBudget`. Pure data, `min()`-clamped by a per-platform `PLATFORM_CAPS` row.

| ramp_day | stage | likes | follows | connects | dwell windows | read_only |
|----------|-------|-------|---------|----------|---------------|-----------|
| 0–3 | observe | 0 | 0 | 0 | 2 | **yes** |
| 4–7 | light | 2 | 0 | 1* | 3 | no |
| 8–14 | ramp | 4 | 1 | 2 | 3 | no |
| 15+ | sustain | 6 | 2 | 3 | 4 | no |

\*LinkedIn `connect` is high-commitment/hard-rate-limited — O6 may defer connects to the `ramp` stage.

### 4.5 WarmingSession loop
Mirrors `XSession` shape (`_emit` phase `'warming'`, `_flush`, `start_session(WARMING_SENTINEL_CAMPAIGN(org_id), platform, engine_mode='warming', account_id=..., org_id=...)` — §3.2/§3.3, HaltSession fold into `halt_reason`). Body = `budget.dwell_windows` dwell windows: observe a small home-feed slice (`window_observe_items` cap), then `executor.maybe_act(item)` under budget + `act_probability` + human pacing. Halts on: daytime-closed, empty-interception canary (`empty_interception_halt=2`, stricter than harvest's 5), or action-block/challenge → raises HaltSession + writes `account_warmth` halt flag.

### 4.6 v1 ramp reality (verifier high — stated plainly)
Because account registration ships in v1 (it is a hard prerequisite — see P0/P1), the `accounts` row is created at `warm-register`. Age accrues from onboarding, so **a freshly-registered account starts in `observe` and ramps over days**, performing useful writes only from day 4. This is intended. v1 is NOT observation-only — the registration path ships in the same release (resolves the "dead code" risk).

### 4.7 Counters persistence (verifier low)
`update_counters` writes a fixed harvest column list and does **not** persist warming counters. Warming counters (`dwell_windows`, `warming_likes/follows/connects`, `checkpoint_flags`) persist via **`accounts.detail` JSON + `health_flags(kind='account_warmth')`** only. The shared fields (`reels_seen` etc.) stay 0 for warming.

### 4.8 Scheduling
Host OS cron / systemd-timer (NOT the session-bound `CronCreate`). A `scripts/warm_accounts.sh` enumerates warming-eligible campaigns and loops the CLI inside the daytime window. The CLI daytime guard is the real safety net.

---

## 5. Warmth Score (read-model)

**Computed on every read, never a column (C3).** Lives in pure `core/warmth.py` (functions) consumed by `store.warmth_for_campaign`. Returns the score **plus a per-component breakdown** for auditability.

### 5.1 Formula

```
base   = Σ_over_components ( normalize(raw) × platform_weight[component] )      # weights sum to 100
score  = round( base × penaltyFactor )                                          # 0..100
```

### 5.2 Components, sources, normalization (every signal is captured data)

| Component | Raw signal | Source (account_id-joined) | Normalize |
|-----------|-----------|----------------------------|-----------|
| **age** ("time under management") | days since `accounts.added_at` | `accounts` | `min(1, days / FULL_AGE_DAYS)`, FULL_AGE_DAYS=21 |
| **ramp** | warming consistency | count `sessions WHERE engine_mode='warming' AND account_id=? AND status='completed'` in 14d vs daily cadence; longest-gap penalty | `completed_days / 14` |
| **network** | engagement depth (C1) | `actions WHERE action_type IN ('follow','connect') AND succeeded=1 AND account_id=?`, cumulative | `min(1, successful / TARGET_CONNECTS)` |
| **profile** | login health | `accounts.detail.{login_status, checkpoint_detected}` (derivable from interception health) | fraction truthy of expected login keys |
| **trust** | absence of recent friction | `health_flags WHERE account_id=? AND resolved_at IS NULL AND kind IN WARMING_CHALLENGE_KINDS` in 14d; soft −X, halt −Y | `1 − min(1, weighted_open_load)` |

> `profile_complete` is **dropped** (verifier medium): pure-observe warming does not navigate the profile page, so we cannot capture it. The `profile` component uses only login/checkpoint signals from interception health.

### 5.3 Penalty (multiplicative — a fresh challenge craters the score)
Anchored to a single shared constant `config.WARMING_CHALLENGE_KINDS` imported by BOTH the warming engine's `raise_flag` calls and `compute_penalty`. The newest unresolved challenge flag wins:

| Condition | penaltyFactor | reason |
|-----------|---------------|--------|
| checkpoint/challenge < 24h | 0.10 | `challenge_fresh` |
| checkpoint/challenge 24–72h | 0.35 | `challenge_recent` |
| rate_limit < 6h | 0.50 | `rate_limited` → status `throttled` |
| login_drift / session_expired | 0.40 | `login_drift` |
| none | 1.00 | — |

> Until the warming engine actually raises the newer kinds, scope penalty to kinds that exist today (`checkpoint`, `empty_interception`); `arkose`/`rate_limit`/`login_drift`/`session_expired` are added to `WARMING_CHALLENGE_KINDS` **and** raised by the warming engine in the same release (verifier high).
> **Self-heal:** decay is by flag **age** past the window AND by `resolved_at` being set — and `resolve_flag` (the SOLE writer of `resolved_at`, §3.3) makes the resolved path real (verifier high). Both warmth queries filter `resolved_at`.

### 5.4 Status labels
`throttled` if `rate_limited`; else `< 40` → **cold**, `< 70` → **warming**, else **ready/full**. (`throttled` is distinct from cold so the operator knows it's transient.)

### 5.5 Platform weights (CONFIG, sum to 100)
```
x        : age 15, ramp 30, network 20, profile 10, trust 25
linkedin : age 20, ramp 20, network 30, profile 20, trust 10
_default : age 20, ramp 25, network 20, profile 15, trust 20   # Instagram (Telegram deferred — needs its own row, see §11/gap #5)
```
`WARMABLE_PLATFORMS = {x, linkedin, instagram}` in v1. Telegram/YouTube/Reddit are NOT warmable: `add_account` rejects them, `resolve_account_for_campaign` returns None, and their campaigns fall through to the §5.8 neutral default — they never touch the weights table.

### 5.6 Cross-endpoint identity (verifier medium)
`warmth_for_campaign(cid, now=<request-scoped now>)` with a fixed **TASHKENT** timezone (matching panel bucketing). The dashboard period filter does NOT apply to warmth windows. Single producer — `panel_org` calls the same method, never re-derives — so `/api/state`, `/api/campaigns`, `/api/dashboard` return byte-identical scores.

### 5.7 Trend (verifier medium / O8)
Trend-via-recompute is faithful only on the **additive base** (monotone-reconstructable from insert-only tables), not the penalty-adjusted score (penalty tiers are relative to wall-clock now). **v1 recommendation:** ship base-only trend OR defer `trend`/`etaHours` entirely (Zod `.catch([])`/`.catch(null)` makes this a clean staged rollout). Do not schematize a per-day series the engine cannot fill. **`etaHours` has no producer in v1** — the field ships in the payload/Zod schema (§7.2/§7.3) but the engine cannot fill it (it would require projecting the ramp curve against current cadence); it is always `null` until a fast-follow adds the producer. The schema field exists only so the rollout is additive (`.catch(null)`); reviewers should not look for an engine producer.

### 5.8 Backward-compat default
No account row / no warming sessions → `score=50, status='warming', base=50, penaltyFactor=1.0, components=[]`. Neutral, never blocking. YouTube/Reddit campaigns hit this default and are never gated.

---

## 6. Pool / lifecycle manager + scheduler

### 6.1 Two halves of a tick
A new `aizu warm-tick` CLI subcommand (cron-fired ~every 27 min, hours 8–20):
- **(A) Session driving** — run ONE warming session per selected account; touches Chrome; must hold the run slot.
- **(B) Lifecycle reconciliation** — pure `warming/lifecycle.py::reconcile(accounts, now, cfg) -> list[Transition]` over `accounts` + `health_flags` + `sessions`; fast, no Chrome; returns immutable transitions applied via `update_account_lifecycle`. **`reconcile` is the sole writer of `accounts.consecutive_flag_count`:** it increments the count on a soft-flag streak and resets it to 0 on a clean tick (a successful warming session with no new flag). The O5 "auto-enter `cooling` on soft-flag streak" decision keys off this counter crossing its threshold — both the increment and the resulting transition are emitted by `reconcile`/`update_account_lifecycle`, never elsewhere.

### 6.2 Coexistence with RunManager — REAL cross-process exclusion (verifier critical)
Warming and harvest both drive the single warmed Chrome and **must be mutually exclusive**. RunManager's lock is in-memory in the panel process — a cron `warm-tick` is a separate OS process and cannot see it. Therefore:

- **Build an OS advisory lock** (`fcntl.flock` on `<db>.warmlock`) acquired **non-blocking at process start** by `cmd_run`, `cmd_run_all`, AND `warm-run`, released on exit. Harvest acquires and proceeds; warming acquires and, on `EWOULDBLOCK`, **exits 0 (yields)**. Retrofitting `cmd_run`/`cmd_run_all` to take the flock is **REQUIRED**, not optional. SQLite `busy_timeout` protects DB writes but NOT the Chrome/CDP resource — the flock is mandatory.
- **The panel-spawned path IS covered by the `cmd_run` flock (resolves gap #9, verified).** The panel's RunManager (`runner.py`) spawns a child process whose argv is `[..., "run", "--config", ...]` (runner.py:122) and `subprocess.Popen` (runner.py:137) — i.e. it dispatches into the CLI `run` subcommand → `cmd_run`. So putting the flock in `cmd_run` automatically holds it for panel-launched harvest; there is **no separate spawn entry point** to retrofit. The in-server `try_launch_warming` wrapper is a secondary convenience only and does NOT "solve" coexistence — the flock in `cmd_run`/`cmd_run_all`/`warm-run` does.
- **Harvest always wins; warming yields** (revenue-bearing harvest is never blocked by upkeep).

### 6.3 Warm-ahead + target selection
Per (org, platform): `deficit = max(0, demand + ready_buffer − supply_ready)` (`ready_buffer` default 2). Priority within `--max-sessions` (default 3): (1) `warming` near the gate + stalest, (2) `ready` overdue for upkeep, (3) `registered` when `deficit > 0`, (4) `cooling` past `cooling_until`. Stalest-first keeps cadence human.

### 6.4 Assignment & reassign-on-cool (resolves MIN-wedge)
Per-campaign warmth = score of its **resolved backing account** (the pinned or pool-picked account), not a blind MIN across many. When an assigned account → `cooling`/`flagged`, `reconcile` **unassigns it and pulls a `ready` buffer account in** (warm-ahead maintains the buffer). One bad account therefore does not permanently zero a campaign; operators are alerted if no buffer exists (O3).

### 6.5 Provisioning (creation out of v1)
`aizu warm-register --org N --platform x --username u --chrome-profile <dir> [--proxy <url>]`: attaches over CDP and **confirms the authenticated identity** — navigate to the account/profile surface and assert the logged-in username matches `--username` (verifier medium; `feed.healthy()` is an empty-interception canary and cannot prove login). Then `add_account(state='provisioned')`. CLI-only in v1; panel wrapper later.

---

## 7. Panel UI + API contract chain

### 7.1 Where warmth is injected
`panel.py::_build_campaigns()` (~524) + `_draft_campaign()` (~562) call `store.warmth_for_campaign(cid, now)`. `panel_org.build_campaigns_org` delegates to `_build_campaigns` verbatim → identical across endpoints.

### 7.2 Campaign card payload (server-authoritative gate — verifier high)
The campaigns-page payload has **no CONFIG block**, so the client cannot fetch the threshold there. **Ship the verdict + thresholds WITH the campaign** so the client never needs CONFIG:

```ts
warmth: {
  score:    number,                                  // 0-100
  state:    'warming'|'ready'|'full'|'throttled',    // server-computed
  gateMin:  number,                                  // travels with the campaign
  gateFull: number,
  meetsGate: boolean,                                // server verdict; client re-derives score>=gateMin as the trust check
  components: { age:number; ramp:number; network:number; profile:number; trust:number },
  trend:    number[],                                // base-only; may be [] in v1
  etaHours: number | null,                           // may be null in v1
  checkedAt: string                                  // ISO, "as of" stamp
}
```

### 7.3 Zod boundary (`panelState.ts` campaignSchema, after `cpl`)
Every sub-field `.catch`es (pre-warmth payloads still validate):
```ts
warmth: z.object({
  score: z.number().min(0).max(100).catch(50),
  state: z.enum(['warming','ready','full','throttled']).catch('warming'),
  gateMin: z.number().catch(40), gateFull: z.number().catch(70),
  meetsGate: z.boolean().catch(true),
  components: z.object({ age:z.number().catch(0), ramp:z.number().catch(0),
    network:z.number().catch(0), profile:z.number().catch(0), trust:z.number().catch(0) }).catch({age:0,ramp:0,network:0,profile:0,trust:0}),
  trend: z.array(z.number()).catch([]),
  etaHours: z.number().nullable().catch(null),
  checkedAt: z.string().catch('—'),
}).catch({ /* neutral 50/'warming'/meetsGate:true */ })
```
`endpoints.ts` — no edit (payload schemas reference `campaignSchema`). `panelRepository.ts`/`httpPanelRepository.ts` — no change (types re-infer). New `fetchAccounts()`/`resolveChallenge()` added for the Settings tile + resolve loop.

### 7.4 Selectors (`shared/selectors/campaigns.ts`)
```ts
export const isWarmEnough = (c: Campaign) => c.warmth.meetsGate;   // server verdict, re-derivable
export const selectCampaignIsRunnable = (c, run) =>
  isRunnable(c) && isWarmEnough(c) && !selectIsAnyRunActive(run);  // isRunnable (brief) checked FIRST
```
Pill renders `c.warmth.state` directly (single source of truth); `warmthTier()` kept only as a tested fallback for pre-warmth payloads.

### 7.5 Components
- **CampaignCard.tsx** (RunButton ~131): warm enough → green Run; not warm → disabled `❄ Warming — not ready` pill + tooltip deep-linking Settings → Integrations. Headline gains `<WarmthBadge>` (score% + tier pill + sparkline); breakdown in a popover. Hard-block vs warn is O2 (one-line branch).
- **IntegrationsPanel.tsx**: extend the existing `integrationSchema` with optional `warmth`/`lastActivity`/`flagState` (one per-platform array — do NOT add a parallel `ACCOUNT_HEALTH` array) + a per-account "Resolve" CTA.

### 7.6 New endpoints
- `GET /api/accounts?platform=` → org-scoped pool view.
- `POST /api/accounts {platform, username}` → record a provisioned account.
- `POST /api/campaign/account {campaignId, accountId, pinned}` → assign/pin (cross-org assert).
- `POST /api/account/{id}/challenge/resolve {flagId}` → `resolve_flag` + flip `state`→`warming`/`ready`.

### 7.7 ASCII badge mock
```
+------------------------------------------------------+
| Tashkent Construction Leadgen           [ Paused ]    |
|  Warmth  38%   (• Warming)   ▁▂▂▃▃▂▄▄▅▅▄▆▆▅           |
|  Age ████░ Ramp ███░ Network ██░ Profile ███ Trust ██ |
|  Spent $4.20/$20    Leads 12    CPL $0.35             |
|  --------------------------------------------------   |
|                        [ ❄ Warming — not ready ]      |  <- disabled
+------------------------------------------------------+
   tooltip: "Account warmth is 38%, below the 40%
   threshold (reason: still warming). Check
   Settings → Integrations."

ready/full:  Warmth 82%  (• Full volume)  ▃▄▅▅▆▆▇▇██   [ ⚡ Run ]
```

---

## 8. Infra (proxy / fingerprint / challenge ops)

### 8.1 Identity triple — fixed per account for its lifetime
`account_id ⇒ (residential sticky proxy, stable fingerprint, profile_dir, cdp_port)`. Never shared, never datacenter, never engine-rotated (operator re-provision only). Proxy creds stored encrypted in `account_secrets` (Fernet/`SecretCipher`, `AIZU_SECRET_KEY`). `put_account_proxy` validates residential-not-datacenter at the boundary (fail fast).

### 8.2 Proxy is a LAUNCH-time concern, NOT attach-time (infra critical)
Over `connect_over_cdp` Playwright attaches to Chrome's **existing** context and **cannot** set a proxy or proxy-auth. Therefore:
- `--proxy-server=<scheme>://host:port` passed by `warm_chrome_pool.sh` at Chrome launch (per account).
- Proxy **auth** via a **generated per-profile proxy-auth Chrome extension** loaded at launch (`--load-extension`) — chosen for v1 over a local auth-injecting relay (no extra long-lived process per account, no port management). The extension is generated by `warm_chrome_pool.sh` from the encrypted proxy creds and lives under the account's `profile_dir`. NOT via Playwright at attach (the attached context cannot set proxy auth). A local relay remains the documented fallback if a platform rejects MV3 proxy extensions.
- `WarmingFeed.attach()` override does ONLY the fingerprint `add_init_script` (a JS-surface init script IS settable on the attached page); it does NOT touch the proxy.
- A **proxy-auth 407 canary** is evaluated BEFORE challenge classification (a 407 looks like empty-interception otherwise).

### 8.3 Fingerprint / timezone / proxy-country coherence
`fingerprint` JSON (UA, accept_language, timezone_id, viewport, platform, webgl). `timezone_id` aligned to proxy country. The warming Pacer's daytime guard uses `clock=lambda: datetime.now(ZoneInfo(timezone_id))` (§4.2) so warming runs at the account's local hours, not the host's.

### 8.4 `warm_chrome_pool.sh`
Reads the roster (`python -m aizu.warming.roster --org N` → `account_id port profile_dir proxy_url`), launches one Chrome-for-Testing per account on its port + profile + proxy, reusing `resolve_chrome()` + per-port `cdp_attaches()` probe. Single-host v1 assumption (O4); `host_id` deferred.

### 8.5 Challenge handling (alert → manual resolve → resume)
WarmingFeed classifies in order: (1) 407 → `proxy_misconfig` flag (no warmth penalty), (2) known challenge URL/DOM via per-platform `_challenge_hints()` → `HaltSession('challenge:<kind>')` + `raise_flag(kind='account_challenge', severity='halt', org_id=..., account_id=...)` + screenshot ref + `state→flagged`, (3) otherwise benign quiet → soft note. The halt flag (now correctly org-stamped via the extended `raise_flag`) surfaces in the panel banner. Operator solves in the live Chrome window, clicks Resolve → `resolve_flag` (sole `resolved_at` writer) → `state→warming/ready`; next run resumes.

---

## 9. Risk, safety & kill-switch

### 9.1 Three-layer kill-switch
1. **Env hard-stop** `AIZU_WARMING_ENABLED` (default OFF), checked in **cli before `build_feed`** so a disabled campaign never attaches Chrome (verifier medium).
2. **Per-org / per-platform DB switch** via the existing settings overlay: `warmingEnabled: bool`, `warmingDisabledPlatforms: string[]`.
3. **Auto circuit breaker**: N `account_warmth` halt flags for an (org, platform) within a window → `warming_killswitch` flag; warming refuses to start for that platform until operator-resolved (O7). Per-platform scoping reads `platform` from flag `detail` (or `account_id`→`accounts.platform`) via a small `warming_flags(org_id, platform, kind)` helper — `open_flags` does not filter by platform today (verifier high).

`warming_gate(store, *, org_id, platform) -> Optional[str]` runs first; non-None short-circuits to a uniform `halt_reason`. The kill-switch must be **re-checked each dwell window** so a mid-session flip halts in flight, not just the next run.

### 9.2 Read-only-harvest invariant (honest statement)
Harvest is read-only by **config-default + test guard**, not pure structure — the Instagram engine ships a real (gated-off) write path. The guard test asserts harvest forces `enable_actions=False`; warming is the sole `log_action` caller (§4.3).

### 9.3 Daytime guard for writes
Warming WRITE actions gate on `pacer.is_daytime()` and **must not** inherit the `AIZU_IGNORE_DAYTIME` opt-out — a test bypass must never widen the production write window. **Mechanism (not just policy):** the warming Pacer is constructed as `Pacer(PacingConfig(enforce_daytime=True), clock=…)` in `_build_run_io` (§4.2). Because an explicit `PacingConfig(enforce_daytime=…)` overrides the env default (pacing.py:23), `AIZU_IGNORE_DAYTIME=1` present in the inherited env (as live runs set it) cannot widen the warming write window. The guard test asserts a warming Pacer reports `is_daytime()==False` at 3am **even with `AIZU_IGNORE_DAYTIME=1` set**.

### 9.4 Cost model
Per-warmed-account/month = residential sticky proxy + ops minutes + amortized acquisition. Warming does zero ML inference → LLM spend ≈ $0. Proxy cost rolls into the existing `spend_log` (`stage='warming_proxy'`) under the per-campaign ceiling. One IP per account (shared IP = fingerprint-collision ban risk; operational invariant, no code guard in v1).

### 9.5 Residual risks
Account loss from over-engagement (mitigated by allowlist + caps + audit ledger + daytime); N-Chrome RAM/IP cost caps pool size per host; detection arms-race (X doc_id rotation, LinkedIn checkpoints) is perpetual ops surfaced in the panel tile; MIN-wedge mitigated by reassign-on-cool (§6.4).

---

## 10. Phased rollout & acceptance criteria

| Phase | Scope | Acceptance criteria |
|-------|-------|---------------------|
| **P0 — Observe + scoring + schema** | v11 schema (all tables + column adds), `warm-register`, warmth read-model, panel badge + `isWarmEnough` selector, kill-switch plumbing (default OFF), WarmingFeed dwell-only (writes still no-op). | Warmth computed per-account from session/flag/age; identical across all 3 endpoints; **zero write actions emitted** (assert via `actions`/run_events); `warm-register` confirms authenticated identity; flock taken by cmd_run/cmd_run_all/warm-run; harvest paths byte-for-byte unchanged (harvest test suite green). |
| **P1 — X writes behind allowlist** | Enable warming writes on **X** (env on + org setting on + per-account `detail.write_allowlist`). Ramp caps (≤3 follow, ≤5 like/session, daytime-only). reassign-on-cool live. | Warmth measurably rises on enrolled accounts; flag rate ≤ baseline; **every write has an `actions` row with `account_id` + `engine_mode='warming'`**; kill-switch verified to halt **mid-session**; isolation guard test passes (no harvest session.py references write helpers; harvest forces enable_actions=False). |
| **P2 — LinkedIn write surface + gate tier** | **Build `linkedin/cdp.py` write helpers from scratch** (`send_connect` incl. add-note/how-do-you-know modal handling, optional `like`) — LinkedIn has zero write surface today (§11). Wire LinkedIn `WarmingPolicy` + `_challenge_hints()`. Promote to two-tier gate per O1/O2. | LinkedIn `send_connect` lands a real connection request behind ramp caps with `actions` rows (`action_type='connect'`, `account_id`, `engine_mode='warming'`); connect-modal variants handled or safely aborted (never silently mis-clicked); both platforms warming concurrently, per-org isolated, flock-serialized against harvest; circuit breaker auto-trips under induced challenge storm; cross-org assignment rejection covered by test. |
| **P3 — Maintenance autopilot** | Scheduled upkeep sustains `active` accounts; warm-ahead buffer held. | Accounts hold ≥70% warmth over a 30-day window without manual intervention; ops-minutes/account trends down; time-to-harvest-ready < 14 days on managed accounts. |

**Success KPIs:** time-to-ready (<14d), flag/ban rate per platform/30d (primary safety KPI), harvest uplift (matches-per-session on ≥70% vs cold accounts), gate accuracy (runs started above gate that still halted on challenge — low validates the score).

> **KPI ↔ normalization consistency check (gap #8).** The "<14d to ready" and "≥70% warmth" KPIs are **not** independent of the §5.2 constants and were drafted separately. With `FULL_AGE_DAYS=21`, the `age` component is capped at `14/21 ≈ 0.67` on day 14, contributing only ~10 pts (X, weight 15) to ~13 pts (default, weight 20) of the 100-pt score. Reaching ≥70 by day 14 therefore requires `ramp`+`network`+`trust` to be **near-maximal** — feasible but tight, and impossible if O6 caps connects/likes too low. **Action:** when O6 is set, compute the day-14 reachable maximum per platform and confirm it ≥ gateFull(70); if not, adjust FULL_AGE_DAYS or the day-14 KPI target. Do not ship the two numbers as independent givens.

---

## 11. Generalization to Instagram (Telegram deferred)

Platform-agnostic by construction. Only two things fork per platform:
1. A **`WarmingPolicy`** subclass (candidate actions + caps) behind `policy_for(platform)`, **plus that platform's CDP feed write helpers — and these are NOT uniformly free:**
   - **Instagram:** `like_reel`/`follow_author` already exist (engines/instagram/cdp.py). Its warming policy is nearly free once the harvest write path is gated off.
   - **LinkedIn (gap #4 — the bulk of P2, not a free subclass):** `engines/linkedin/cdp.py` has **only read methods** (`open_reel`, `fetch_comments`, `_open_comment_thread`) and the session hardcodes `likes:0, follows:0` (engines/linkedin/session.py:264). There is **no `connect`/`like`/`follow` helper at all.** The `connect` action in the ramp table (§4.4) and the entire P2 acceptance criterion depend on **net-new CDP automation of LinkedIn's connect flow** (find profile → click Connect → handle the "add a note" / "how do you know" modals → confirm) — the riskiest, most checkpoint-prone write on the platform. Building `linkedin/cdp.py` write helpers (`send_connect`, optional `like`) from scratch is **the bulk of P2**, not a policy subclass; P2 acceptance (§10) is re-scoped accordingly.
2. A per-platform **`_challenge_hints()`** hook (mirrors `_url_hints()`).

Everything else — `accounts`/`account_state_changes`/`campaign_accounts` schema, the lifecycle state machine + transition guard, warmth scoring (`_default` weight row = Instagram; X/LinkedIn have explicit rows; only `WARMABLE_PLATFORMS` get scored at all), the flock, the resolve loop, warm-ahead, the panel contract — is shared verbatim.

**Credential fork:** X/LinkedIn/Instagram are managed-CDP (`profile_dir` + `cdp_port`, no per-org secret). **YouTube + Reddit** never get an `accounts` row (enforced by a `WARMABLE_PLATFORMS` check in `add_account`); their campaigns hit the neutral 50/'warming' default and are never gated. `resolve_account_for_campaign` returns None for them and `warmth_for_campaign` short-circuits to §5.8 (gap #6).

**Telegram warming is DEFERRED / research (gap #5 — does NOT generalize verbatim).** The earlier claim that Telegram warming is "session keep-alive over Telethon, reusing the same penalty/weights" does **not** hold against the code: `engines/telegram/feed.py` exposes only read ports (`iter_channel_messages`, `iter_replies`); `TelethonClient`/`TelegramBotClient` have **no keep-alive or write method**, there is no checkpoint DOM, and Telegram has no session-trust "cold→warm" arc the way CDP platforms do. As written, a Telegram `accounts` row would compute warmth from empty `actions`/`sessions` and sit at a meaningless score. **v1 decision (D2):** Telegram is **out of the warmable set in v1** alongside YouTube/Reddit (its campaigns hit the §5.8 neutral default). Promoting it later requires research + net-new design: (a) define a concrete keep-alive action and a write/keep-alive surface on `TelethonClient`, and (b) a **`_telegram` weight row** scoped to the components that even apply — likely `age` + `ramp` only, with `network`/`profile`/`trust` marked N/A (not silently zeroed against `_default`). Until then the `_default` weight row is for **Instagram only**. Telegram still reuses the `accounts` table shape, MTProto-session-in-`account_secrets` credential slot, and resolve endpoint **if/when** promoted — but the warmth model is not free.

---

## 12. Known gaps / follow-ups (medium/low — not build-blocking)

Closed inline above: gaps #1–#4 (blocking), #5 Telegram defer, #6 non-warmable short-circuit, #7 `resolve_flag` tx boundary, #8 KPI↔normalization check, #9 panel flock coverage, plus `etaHours` no-producer, `consecutive_flag_count` writer, and §8.2 proxy-auth mechanism. Remaining items, deliberately deferred:

1. **D1 sentinel vs. true nullable `campaign_id` (revisit if it leaks).** v1 uses the `__warming__:<org_id>` sentinel to satisfy the existing NOT NULL without a table rebuild. The cleaner long-term shape is a real nullable `campaign_id` on `sessions`/`actions`, which requires a one-time `_migrate_to_vN` table rebuild (out of the additive v11 scope). Acceptable for v1 **only if** every harvest/campaign read excludes the sentinel (tracked by a `tests/test_warming_sentinel_isolation.py` that asserts no panel/campaign query returns a `__warming__:` row). If a sentinel row ever leaks into harvest stats, promote to the nullable-column rebuild.
2. **Proxy-auth extension hardening.** The generated MV3 proxy-auth extension (§8.2) is the v1 mechanism; the local-relay fallback is documented but unbuilt. If a platform flags the extension, the relay must be built then — not pre-built (YAGNI).
3. **`host_id` / multi-host pool.** §8.4 assumes a single host (O4). Multi-host roster sharding (`host_id` column + per-host port ranges) is deferred until pool size exceeds one host's RAM/IP budget.
4. **Trend producer + `etaHours`.** Base-only trend may ship in v1 (§5.7); the `etaHours` projector has no producer and stays `null` until a fast-follow. O8 governs whether trend is recompute-on-read or a `warmth_snapshots` table.
5. **BYO-accounts path** (§1 non-goal) remains a noted alternative only; no schema or flow in v1.
6. **`profile_complete` component** stays dropped (§5.2) — pure-observe warming never navigates the profile page, so the signal is uncapturable; revisit only if a profile-visit action is added to the ramp.
