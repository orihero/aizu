# Multi-Platform Campaigns — Implementation Plan

**Status:** Approved design, ready to build · **Date:** 2026-06-29
**Goal:** Let ONE campaign discover leads across MULTIPLE social platforms (e.g. Instagram + Telegram + Reddit) under a single brief, scoring model, lead target, and dashboard.

---

## TL;DR

The storage layer is **already multi-platform**. Every discovery table (`matches`,
`sessions`, `seen_reels`, `watchlist`, `comment_cursors`) keys on
`(campaign_id, platform, …)` and the brief is a free-form JSON blob
(`campaign_briefs.brief TEXT`). So there is **no table DDL / data migration** — the
single-platform assumption lives entirely above the DB: `Campaign.platform` (one
string), `_run_session_loop` (one engine session per run), and the API/UI
(one `<select>` + one seed block).

The change is: a campaign gains an optional list of **channels** (platform + its own
seeds), the CLI loop **fans out one engine session per channel sequentially** inside a
single subprocess, and the UI lets you add/remove platforms. Existing single-platform
campaigns and the file campaign are untouched (empty channel list = today's behavior).

### Locked product decisions

| # | Decision | Choice |
|---|----------|--------|
| D1 | **Lead-target apportionment** | Every channel runs **≥1 discovery pass**; the campaign-total target only gates *extra looping within a channel*. No platform gets starved. |
| D2 | **Multiple CDP platforms in one campaign** | **Allowed** (IG + X + LinkedIn run sequentially). UI **warns** that the warmed Chrome must be logged into every CDP site; save-time connection check (best-effort). |
| D3 | **Halt isolation** | Halts are **classified** (`daytime / action_block / checkpoint / login / canary`). Only *account-level* halts skip remaining CDP peers. A nighttime `daytime` halt ends the run cleanly (`halted-daytime`) and never poisons peers or the batch. API-platform halts never poison anyone. |

---

## 1. Data model

### New: `ChannelSpec` (`engine/aizu/core/config.py`, above `Campaign`)

```python
@dataclass(frozen=True)
class ChannelSpec:
    platform: str                          # one of SUPPORTED_PLATFORMS
    seed_hashtags: tuple[str, ...] = ()
    seed_accounts: tuple[str, ...] = ()
    seed_channels: tuple[str, ...] = ()
    include_home_feed: bool = True         # per-channel, seed-aware default
```

### `Campaign` gains exactly one field

```python
channels: tuple[ChannelSpec, ...] = field(default_factory=tuple)   # () = legacy single-platform
```

The existing scalar fields (`platform`, `seed_hashtags`, `seed_accounts`,
`seed_channels`, `include_home_feed`) are **kept verbatim** and remain authoritative
whenever `channels` is empty.

### Per-platform seed mapping (unchanged from the engine's current convention)

| field | IG / LinkedIn / X | YouTube | Telegram | Reddit |
|---|---|---|---|---|
| `seed_hashtags` | hashtags | search queries | — | — |
| `seed_accounts` | accounts | channel ids (fallback) | — | — |
| `seed_channels` | — | channel ids (primary) | channels | subreddits |

Each channel owns its own three seed pools, so Reddit's subreddits never bleed into
Telegram's channel field.

### Round-trip (`config.py`)

- **`campaign_from_brief`** — after the existing scalar parse, read an optional
  `channels` array via a new `_channel_spec_from_dict(c)` that validates `platform`
  against `SUPPORTED_PLATFORMS`, drops unknown platforms, and resolves
  `include_home_feed` **per channel** from *that channel's own seeds*. Absent key →
  `channels=()`, identical to today.
- **`campaign_to_brief`** — emit `channels` **only for ≥2 channels** (see §6 / M1).
  A single-channel campaign collapses back to the flat scalar shape, so single-platform
  briefs stay byte-for-byte as they are today (no churn).
- **`load_campaign`** (file campaign) — unchanged; permanently single-platform.

### `_resolve_home_feed` fix (critique H1)

`_resolve_home_feed` currently keys only on `seed_hashtags` + `seed_accounts`. Add
`seed_channels` so a channel-only platform (Reddit subreddits, Telegram channels) does
not get a spurious `include_home_feed=True`:

```python
return not (seed_hashtags or seed_accounts or seed_channels)
```

### DB: `SCHEMA_VERSION` 10 → 11 (`core/store.py:31`) — **marker only, no DDL**

`channels` lives inside the existing `campaign_briefs.brief` JSON; all leads/sessions
already partition by the `platform` column. The bump is a version marker so a v10→v11
reopen doesn't re-run earlier rename migrations. The activity-feed platform tagging
(H4) needs **no schema change** — see §4.

---

## 2. Run orchestration (`engine/aizu/cli.py`, `engines/base.py`)

**Invariant preserved:** one subprocess per run (`runner.py` `build_argv` unchanged),
one `AIZU_RUN_ID`, one `RunManager` lock, **strictly sequential** channels (forced
by the single warmed Chrome + the `sync_playwright().start()` re-entry guard).
`dispatch.run_engine_session` / `select_engine` and every engine are called **once per
channel, unchanged.**

### Structured halts (critique C1 / decision D3) — `engines/base.py`

`HaltSession` gains a structured kind so the loop can reason about it instead of
string-matching free text:

```python
HaltKind = Literal["daytime", "action_block", "checkpoint", "login", "canary", "unknown"]

class HaltSession(Exception):
    def __init__(self, reason: str, kind: HaltKind = "unknown"):
        super().__init__(reason); self.reason = reason; self.kind = kind
```

Update every `raise HaltSession(...)` across engines to pass `kind=` (daytime guard →
`"daytime"`, action-block → `"action_block"`, empty-interception canary → `"canary"`,
etc.). Engines fold the kind into the summary alongside `halt_reason` (new
`halt_kind` key). `SUMMARY_KEYS` gains `halt_kind`.

Account-poisoning kinds: `{"action_block", "checkpoint", "login", "canary"}`. A
`daytime` halt ends the whole run cleanly (the clock won't improve on this machine) and
poisons nothing.

### New helpers (`cli.py`)

```python
_CDP_PLATFORMS = {"instagram", "linkedin", "x"}          # shared warmed Chrome
_POISON_HALT_KINDS = {"action_block", "checkpoint", "login", "canary"}

def _effective_channels(c: Campaign) -> list[ChannelSpec]:
    if c.channels:
        return list(c.channels)
    return [ChannelSpec(c.platform, tuple(c.seed_hashtags), tuple(c.seed_accounts),
                        tuple(c.seed_channels), c.include_home_feed)]

def _campaign_with_channel(c: Campaign, ch: ChannelSpec) -> Campaign:
    # Fresh lists + a copied knobs dict so two synthetic channels NEVER alias
    # mutable state (critique C2). channels=() → engine sees a normal campaign.
    return dataclasses.replace(
        c, platform=ch.platform,
        seed_hashtags=list(ch.seed_hashtags), seed_accounts=list(ch.seed_accounts),
        seed_channels=list(ch.seed_channels), include_home_feed=ch.include_home_feed,
        knobs=dict(c.knobs), channels=())
```

Downstream code (`_build_run_io`, `_resolve_platform_credentials`,
`dispatch.select_engine`, `store.start_session(platform=…)`) sees a normal
single-platform `Campaign`. **No change to `dispatch.py`, the engines, or the store API.**

### `_run_session_loop` refactor

Lift the existing single-channel body (single-pass guard, the `--target-leads`
back-to-back loop, `_AGG_COUNTERS`) into `_run_one_channel(ch_campaign, …, remaining,
deadline)` returning a per-channel sub-summary. The outer loop (decisions D1 + D3):

```python
channels = _effective_channels(campaign)
agg = {k: 0 for k in _AGG_COUNTERS}
agg.update(sessions=0, spend_usd=0.0, per_platform={}, halt_reason=None, halt_kind=None)
target   = getattr(args, "target_leads", None)
duration = getattr(args, "duration_minutes", None)
deadline = time.monotonic() + duration*60 if duration else None
cdp_poisoned = False

for ch in channels:
    # D1: target only gates EXTRA looping inside a channel, never skips a channel's
    # first pass. We pass remaining so an already-satisfied campaign does a cheap
    # single pass rather than skipping the platform entirely. Deadline still hard-stops.
    if deadline is not None and time.monotonic() >= deadline:
        break
    if cdp_poisoned and ch.platform in _CDP_PLATFORMS:
        agg["per_platform"][ch.platform] = {"skipped": "cdp_poisoned"}; continue
    remaining = max(0, target - agg["matches"]) if target is not None else None
    try:
        sub = _run_one_channel(_campaign_with_channel(campaign, ch), store, soul, args,
                               remaining=remaining, deadline=deadline)   # ≥1 pass guaranteed
    except (RuntimeError, NotImplementedError) as e:                     # M3: never lose prior leads
        agg["per_platform"][ch.platform] = {"error": str(e)}
        if ch.platform in _CDP_PLATFORMS:
            cdp_poisoned = True
        continue
    agg["sessions"] += sub["sessions"]; agg["spend_usd"] += sub["spend_usd"]
    for k in _AGG_COUNTERS: agg[k] += sub.get(k, 0)
    agg["per_platform"][ch.platform] = sub
    if sub.get("halt_reason"):
        agg["halt_reason"], agg["halt_kind"] = sub["halt_reason"], sub.get("halt_kind")
        if sub.get("halt_kind") == "daytime":
            break                                                        # D3: end run, poison nothing
        if ch.platform in _CDP_PLATFORMS and sub.get("halt_kind") in _POISON_HALT_KINDS:
            cdp_poisoned = True
```

- **Single-pass** (`_SINGLE_PASS_PLATFORMS`) is checked **per channel** inside
  `_run_one_channel`, so a mixed campaign runs Instagram-looping + Telegram-single-pass
  in one run.
- `_close_feed` already runs in `_run_one`'s `finally`, so each channel's Playwright
  driver is torn down before the next channel attaches.
- The run summary carries a **`per_platform` breakdown** so the panel distinguishes
  "0 new leads (already-seen)" from "halted" from "skipped" from "completed".

### `cmd_run_all` batch halt (critique H3 / decision D3)

Today *any* halt aborts the whole batch. Refine: only a **poisoning CDP halt** sets the
batch-wide flag; API-platform halts and `daytime` halts are recorded per-campaign and
the batch continues. **Sort `_live_campaigns` so CDP campaigns are grouped** (avoid a
CDP campaign re-attaching Chrome right after a continued API campaign, then hitting a
stale poisoned state). Test `test_run_all_cdp_campaign_after_continued_api_halt`.

---

## 3. Lead attribution & target semantics

### Attribution — zero schema change

`_campaign_with_channel` fills `campaign.platform = ch.platform`, so the existing
`store.start_session(platform=…)` and `upsert_match(platform=…)` stamp the correct
per-channel platform. N channels → N `sessions` rows, each correctly attributed; one
`run_id` spans them all.

### Target = campaign-total sum of per-platform match rows (critique M4)

`--target-leads N` is the **aggregate** across channels. **Document explicitly** that a
"lead" is a per-platform match row: the same human commenting on both an IG reel and a
Telegram post counts as 2 (the dedup PK is `(campaign_id, platform, comment_id)` —
cross-platform human dedup is not possible today). The RunDrawer surfaces per-platform
counts so the operator understands the number. D1 guarantees every channel runs ≥1 pass
before the total caps further looping.

---

## 4. Live activity feed (critique H4) — no schema change

`run_events` has no `platform` column, but it has `session_id`, and `sessions` carries
`platform`. The `/api/run/activity` endpoint **joins `run_events.session_id →
sessions.platform`** to tag each streamed event with its platform. (Alternative: an
additive `platform` column populated at emit time — avoid for v1 to keep the migration
empty.) The RunActivityDrawer renders a per-platform tag on each line.

---

## 5. API changes (`server.py`, `panel.py`)

- **`_brief_to_snake`** (`server.py`): add a `channels` branch translating each entry via
  a new `_channel_to_snake` reusing the existing key maps; drop entries whose `platform
  ∉ SUPPORTED_PLATFORMS`. Flat keys stay for backward compat.
- **Merge semantics (critique M2):** add `channels` to `_BRIEF_KEYS` but **NOT** to
  `_BRIEF_BLANK_DROP_KEYS`. Sentinel contract: `channels` **absent = no change**;
  `channels: []` = **clear to single-platform**. Atomic list replace under the shallow
  `{**base, **incoming}` merge.
- **Validation** delegates to `campaign_from_brief`; its `ValueError` already becomes a
  400 at the existing try/except.
- **`/api/campaign/generate`**: optional — let the prompt infer a `channels` array
  (e.g. a SaaS tool → IG + Telegram). Low priority (§7 open item).
- **`panel.py`**: add `_all_campaign_platforms(brief)` → emit a `platforms` list on each
  campaign card (KEEP the existing single `platform` key = first channel, for the legacy
  badge). Edit-form serializer (`_brief_form_from_stored`) emits `channels`.
- **`runner.py`: no change** — `build_argv` still emits `run --campaign <id>`; the child
  resolves channels from the brief and fans out internally. The 409 single-run lock is
  intact.

---

## 6. UI changes (`admin-panel/src`)

- **`shared/schemas/panelState.ts`**: add `channelEntrySchema`; add
  `channels: z.array(channelEntrySchema).optional().catch(undefined)` to the brief form
  schema; add `platforms: z.array(z.string()).catch(undefined)` to the campaign schema
  (use `.catch(undefined)` not `[]` so a parse miss falls back intentionally — L2).
  Flat fields keep their `.catch` coercions → old payloads still parse.
- **`features/campaigns/useCampaignForm.ts`**: add `ChannelEntry` + `channels` to
  `CampaignFormState`. `toInput()` is dual-path: ≥1 channel → emit `channels`; 0 →
  emit the flat brief. **On save, a single-channel form collapses to scalar + overwrites
  the scalar seed fields from `channels[0]`** so scalar/channel data never drift
  (critique M1). `hasRequiredSeeds` validates each entry's `requireAnyOf` independently.
- **`features/campaigns/CampaignForm.tsx`**: extract a `PlatformChannels` component —
  one collapsible section per channel (platform header + per-channel `SeedFields`), with
  "Add platform" / remove (×) controls and **confirm-before-discard** on remove. When
  `channels` is empty, render today's single `<select>` + `SeedFields` unchanged.
  **Warn when >1 CDP platform is added** (decision D2): the warmed Chrome must be logged
  into each (`features/leads/PlatformChip.tsx` + `platformLabel.ts` already supply
  labels/colors).
- **`features/campaigns/CampaignCard.tsx`**: render one `PlatformChip` per
  `campaign.platforms` (fallback `[campaign.platform]`), wrapped in a `flex-wrap` row so
  the budget bar/sparkline below don't break.
- **`RunDrawer.tsx`** / **`useLeadFilters.ts`**: unchanged for v1 (campaign-total target;
  RunDrawer additionally shows per-platform counts in the result). Multi-select lead
  filter is a future enhancement.

---

## 7. Phased task list (each phase gated green before merge)

| Phase | Scope | Key files | Test gate |
|---|---|---|---|
| **1. Engine data model** | `ChannelSpec`, `Campaign.channels`, per-channel `_resolve_home_feed` (+seed_channels), brief round-trip both shapes, `_effective_channels` / `_campaign_with_channel` (identity-isolated). No execution change. | `core/config.py`, `cli.py` | unit: multi-channel from/to brief, single-channel collapses to scalar, fallback, **identity separation** (`a.seed_hashtags is not b.seed_hashtags`, `a.knobs is not b.knobs`), home-feed per channel incl. channels-only |
| **2. Halt classification + CLI fan-out** | `HaltKind` on `HaltSession` + all raise sites + `SUMMARY_KEYS`; `_run_one_channel`; outer per-channel loop (D1/D3); per-channel try/except (M3); `cmd_run_all` CDP-only contagion + CDP-grouped ordering (H3) | `engines/base.py`, all `engines/*/session.py`, `cli.py` | dry-run integration: sequential channels, total target caps extra looping but every channel runs ≥1 pass, daytime-halt ends run without poisoning peers, action-block poisons remaining CDP only, API-halt preserves prior leads, single-pass per channel, run-all CDP-after-continued-API |
| **3. Activity feed platform tag** | `/api/run/activity` joins `session_id → sessions.platform`; `SCHEMA_VERSION`→11 marker | `server.py`, `core/store.py` | integration: multi-channel events carry platform; v11 reopen is a no-op |
| **4. Server brief wire-format** | `_channel_to_snake`, `_brief_to_snake` channels branch + invalid-platform drop, sentinel merge (M2) | `server.py`, `panel.py` | integration: channels round-trip, invalid dropped, absent=no-change vs `[]`=clear, card `platforms` + legacy fallback |
| **5. Frontend schema + form state** | `channelEntrySchema`, dual-path `toInput`, single-channel collapse + scalar overwrite, multi-channel `hasRequiredSeeds` | `panelState.ts`, `useCampaignForm.ts` | Vitest: dual-path, collapse, `.catch` on old payloads |
| **6. Frontend UI** | `PlatformChannels` add/remove + confirm + >1-CDP warning; multi-chip card | `CampaignForm.tsx`, `CampaignCard.tsx` | RTL: add/remove, confirm-on-discard, multi-CDP warning, multi-chip + legacy render |
| **7. E2E + docs** | end-to-end IG+Telegram campaign; update `docs/architecture/engines.md` (fan-out + multi-CDP warming) | `admin-panel/e2e/`, docs | E2E: create IG+Telegram, brief round-trips, card shows 2 chips, dry-run yields per-platform sessions |

> **Phase ordering note (L1):** Phase 2 ships engine fan-out before Phase 4 lets the
> server persist a multi-channel brief; Phase 2's tests hand-construct multi-channel
> `Campaign` objects, which is fine, but Phases 2–4 aren't independently end-to-end
> testable until 4 lands.

---

## 8. Backward compatibility

- DB briefs without `channels` → `channels=()` → synthetic single `ChannelSpec` →
  identical behavior.
- File campaign (`config/campaign.md`) → permanently single-platform.
- `campaign.platform` accessor unchanged; always equals the running channel inside the
  loop.
- `campaign_to_brief` omits `channels` for ≤1 channel → edited single-platform campaigns
  write a v10-shaped blob (no churn). A brief becomes multi-channel only when a 2nd
  channel is saved.
- Wire / Zod flat keys kept with `.catch`; `channels` optional.
- `RunManager` / activity feed / 409 semantics untouched.

---

## 9. Open questions (v2 / product)

1. **Per-channel lead targets** — guaranteed minimums per platform ("≥10 Telegram AND
   ≥25 Instagram"). Needs optional `ChannelSpec.target_leads` + per-channel counters +
   RunDrawer inputs.
2. **Channel ordering UI** — D1 guarantees each channel a pass; should operators be able
   to reorder which platform absorbs the looping remainder first?
3. **Save-time warmed-Chrome pre-flight** (D2) — should the server hard-block saving a
   live multi-CDP campaign unless each CDP platform is "connected", rather than relying
   on the runtime canary?
4. **Cross-platform human dedup** — should "lead target" mean distinct humans rather than
   distinct match rows? Not possible without an author-identity join across platforms.
5. **AI generation** — should `/api/campaign/generate` infer a `channels` array?
6. **Activity feed grouping** — per-channel section headers vs a flat per-platform-tagged
   chronological stream.
