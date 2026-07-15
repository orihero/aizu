# Multi-Platform Campaigns — Executable Build Plan

**Companion to** `multi-platform-campaign-PRD.md` · **Date:** 2026-06-29 · **Status:** ready to build

Six phases turn a single-platform campaign into a per-channel fan-out. This doc is the
**execution** layer: line-accurate `file:symbol:action` checklists, a canonical
shared-contract appendix (the single source of truth), a dependency DAG, named test
gates, and a risk register — produced from a per-phase deep-read of the live code and a
cross-phase contract reconciliation.

**Build order:** `1 → (2 ∥ 3) → 4 → 5 → 6`. Phase 3 is independent and may start immediately.

### Reconciliation outcomes already baked into the checklists
- **M6 / L1 / M3** — double-ownership resolved. **Phase 1** solely owns the new `cli.py` helpers + constants; **Phase 5** solely owns the `useCampaignForm.ts` model + logic; **Phase 6** is UI-only and consumes the Phase-5 schema verbatim.
- **M1** — tolerant schema. `channelEntrySchema` seed arrays use `.catch([])`; the "reject missing array" test is replaced by a "degrade to `[]`" test.
- **M2** — names disambiguated. Form-layer type is `ChannelFormEntry` everywhere; `ChannelEntry` is the wire type only.
- **L2** — collapse boundary fixed at **≥2**. `toInput()` and `campaign_to_brief` emit a `channels` key only at length ≥ 2; ≤1 collapses to a flat brief.
- **G1** — `perPlatform` serialization owned by **Phase 4** (maps the run summary's `per_platform`/`halt_kind` into the panel JSON as camelCase).

---

## 1. Canonical shared-contract appendix (C1–C7)

The only authoritative definition. Where a phase spec disagreed with itself or a sibling,
the checklists below already state this reconciled target.

### C1 — Channel entry, three representations (clean bijection)

| Layer | Owner | Shape |
|---|---|---|
| **Python `ChannelSpec`** (in-memory) | P1 | frozen dataclass: `platform:str`, `seed_hashtags/seed_accounts/seed_channels: tuple[str,...]=()`, `include_home_feed:bool=True` |
| **Python brief** (snake, stored JSON) | P1 emit, P4 store | `{platform, seed_hashtags[], seed_accounts[], seed_channels[], include_home_feed}`. Seed arrays + `include_home_feed` **optional on read** (absent → seed-aware default); `_channel_to_snake` **omits absent** keys; `campaign_to_brief` always emits all 5. |
| **UI brief** (camel, wire) | P4 serializers, P5 schema | `{platform, seedHashtags[], seedAccounts[], seedChannels[], includeHomeFeed?}` |
| **Form-state entry** | P5/P6 | `{platform, seedHashtags, seedAccounts, seedChannels}` — comma **strings**, no `includeHomeFeed` (derived) |

Canonical Zod `channelEntrySchema`: `platform: z.string()`, seed arrays `z.array(z.string()).catch([])`, `includeHomeFeed: z.boolean().optional()`.

### C2 — `channels` field / collapse rule

| Aspect | Canonical | Owner |
|---|---|---|
| `Campaign.channels` | `tuple[ChannelSpec,...] = field(default_factory=tuple)`; `()` ≡ legacy single-platform (tuple keeps the dataclass hashable — never `default_factory=list`) | P1 |
| `campaign_to_brief` emit | emit `channels` **only when `len ≥ 2`**; 0/1 collapse to flat scalars (and when `len==1`, source flat scalars from `channels[0]` — **M4**) | P1 |
| `toInput()` emit | emit `brief.channels` **only when `form.channels.length ≥ 2`**; ≤1 → flat brief, no `channels` key | P5 |
| `campaign_from_brief` parse | absent/empty → `channels=()`; invalid-platform entries silently dropped | P1 |

### C3 — Merge sentinel (`channels` in POST /api/campaign)

| Incoming `brief.channels` | Server behavior | Owner |
|---|---|---|
| **absent** | key not emitted → merge preserves stored → **no change** | P4 |
| **`[]`** | emitted as `[]` → overwrite → **clear to single-platform** | P4 |
| **`[…valid…]`** | atomic list replace | P4 |
| all-invalid entries | filtered to `[]` → treated as clear | P4 |

### C4 — HaltKind ↔ poison set ↔ raise sites ↔ summary

| Aspect | Canonical | Owner |
|---|---|---|
| `HaltKind` | `Literal["daytime","action_block","checkpoint","login","canary","unknown"]` | P2 |
| `_POISON_HALT_KINDS` | `frozenset({"action_block","checkpoint","login","canary"})` — `daytime`/`unknown`/`None` never poison | P1+P2 |
| `_CDP_PLATFORMS` | `frozenset({"instagram","linkedin","x"})` (py) · `Set(['instagram','x','linkedin'])` (ts) | P1+P2+P6 |
| Raise sites that emit | `action_block` (ig `_act`), `canary` (ig/x/linkedin `_process_comments`), `daytime` (ig/x/linkedin `run`). **`checkpoint`/`login` are reserved enum+poison members, never raised in scope (G3)** | P2 |
| Summary key | every `run_session()` returns `halt_kind: str\|None` — 12th `SUMMARY_KEYS` entry; non-halting engines (TG/YT/Reddit) → `None` | P2 |

### C5 — Run summary dict (`_run_session_loop`) + `perPlatform` serialization

`_AGG_COUNTERS` (int) · `sessions` (int) · `spend_usd` (float, 6dp) · `halt_reason` (str\|None) · `halt_kind` (str\|None) · `per_platform` (`dict[str,dict]` — sub-summary / `{skipped:"cdp_poisoned"}` / `{error:str}`) · `target_leads` (int when set).

**G1 fix — serialization owner = Phase 4.** The engine produces `per_platform`/`halt_kind`; Phase 4 maps them onto the panel run-result / per-page JSON as camelCase `perPlatform` + `haltKind`. Until that lands, P6's per-platform display has no data source.

### C6 — Card `platforms` ↔ `campaignSchema.platforms` ↔ CampaignCard

| Aspect | Canonical | Owner |
|---|---|---|
| Card field | every card has both `platform:str` (primary/first, unchanged) and `platforms:string[]` | P4 |
| Zod schema | `platforms: z.array(z.string()).optional().catch(undefined)` — **both** `.optional()` and `.catch(undefined)`; **never `.catch([])`** (M3) | P5 |
| Render | `(campaign.platforms ?? [campaign.platform]).map(PlatformChip)` | P6 |

Rationale for `undefined` not `[]`: the card must distinguish "server sent no `platforms` key" (fallback to `[platform]`) from "server sent an empty list".

### C7 — Activity-feed RunEvent + SCHEMA_VERSION

- Event wire shape: all v10 keys **plus `platform: str\|null`**, resolved by `LEFT JOIN sessions s ON s.session_id = re.session_id` in `Store.fetch_run_events`. Null when the session row is missing/pruned. **Org scoping stays on `re.org_id`, never `s.org_id`.**
- `SCHEMA_VERSION = 11` — marker only, **no DDL**, no new `_init_schema` branch for `prior==10`. `run_events` and `sessions` are byte-for-byte identical after Phase 3.

---

## 2. Dependency DAG & build order

```
        ┌─────────────┐
        │ 1 Data model│──────────────┐
        │ + CLI helpers│             │
        └──────┬───────┘             │
               │                     ▼
      ┌────────┴────────┐      ┌───────────┐
      ▼                 ▼      │ 3 Activity │  (independent — start day one)
┌───────────┐   (server-only) │  platform  │
│ 2 Halt +  │                 │    tag     │
│  fan-out  │────────┐        └─────┬──────┘
└───────────┘        ▼              │
              ┌───────────────┐     │
              │ 4 Server wire │     │
              │  + perPlatform│     │
              └───────┬───────┘     │
                      ▼             │
              ┌───────────────┐     │
              │ 5 FE schema   │     │
              │  + form state │     │
              └───────┬───────┘     │
                      ▼             ▼
              ┌───────────────────────┐
              │ 6 FE UI (form + card) │
              └───────────┬───────────┘
                          ▼
                  7 E2E (out of scope here)
```

| Phase | Hard prerequisite | Unblocks | Parallelizable with |
|---|---|---|---|
| **1** Data model + CLI helpers | — | 2, 4 | 3 |
| **2** Halt classification + fan-out | 1 | 4 (`perPlatform`), 3 (per-channel sessions) | 3 · 4 (server-only parts) |
| **3** Activity platform tag | — | 6, 7 | 1 · 2 · 4 · 5 |
| **4** Server brief wire-format | 1 (`c.channels`), 2 (`per_platform`) | 5, 6 | 3 · 5 (unit-only) |
| **5** FE schema + form state | 4 (integration); none for units | 6 | 3 · 4 (server) |
| **6** FE UI | 5 | 7 | — |

---

## 3. Phase 1 — the first slice (start here)

Safest opening move: **zero execution-path change.** Acceptance criterion —
`_run_session_loop`, `cmd_run`, `cmd_run_all`, `_run_one` are untouched. Every change is
additive data-model or pure helper, fully unit-covered.

Ordered steps (each compiles + tests green before the next):

1. `core/config.py` — widen `_resolve_home_feed` to a 4th param `seed_channels`; update **both** call sites (`load_campaign:253`, `campaign_from_brief:300`). → `test_resolve_home_feed_off_when_only_seed_channels`
2. Add `ChannelSpec` frozen dataclass (between `Soul` end and `Campaign`). → `test_channel_spec_from_dict_valid` / `_invalid_platform_returns_none` / `_explicit_home_feed_override`
3. Add `_channel_spec_from_dict` (after `_resolve_home_feed`); add `Campaign.channels = field(default_factory=tuple)`.
4. Wire `campaign_from_brief` channels parse + `campaign_to_brief` emission with the ≥2 collapse and the **M4** `len==1` scalar-from-`channels[0]` rule. → the seven `test_campaign_{from,to}_brief_*` incl. `_json_serializable`
5. `cli.py` — add `import dataclasses`; extend the config import with `ChannelSpec`; add `_CDP_PLATFORMS` + `_POISON_HALT_KINDS` frozensets; add `_effective_channels` + `_campaign_with_channel`. → the seven `test_effective_channels_*` / `test_campaign_with_channel_*`

**Pre-merge guard:** `grep -n "_resolve_home_feed(" reelradar/core/config.py` must return **exactly 3 lines** (def + 2 call sites). A missed call site is a runtime `TypeError`.

---

## 4. Phases in build order

### PHASE 1 — Engine data model + CLI helpers · deps: none

**`reelradar/core/config.py`**
- `_resolve_home_feed` · **modify** — add 4th param `seed_channels: list[str]`; `return not (seed_hashtags or seed_accounts or seed_channels)`; update call sites `:253`, `:300`.
- `ChannelSpec` · **add** — frozen dataclass, tuple seed fields default `()`, `include_home_feed:bool=True`; above `Campaign`.
- `_channel_spec_from_dict` · **add** — drops non-`SUPPORTED_PLATFORMS` entries (→ `None`); resolves `include_home_feed` seed-aware when key absent.
- `Campaign.channels` · **add** — `tuple[ChannelSpec,...] = field(default_factory=tuple)`.
- `campaign_from_brief` · **modify** — parse `brief["channels"]` via comprehension over `_channel_spec_from_dict`; pass `seed_channels` as the new 4th arg to `_resolve_home_feed`; add `channels=channels`.
- `campaign_to_brief` · **modify** — emit `channels` only when `len ≥ 2` with `list()` seeds (no tuples); **M4:** when `len==1`, source flat scalars from `channels[0]`.

**`reelradar/cli.py`** *(M6: P1 is sole owner of these)*
- imports · **modify** — `import dataclasses`; extend `from .core.config import …` with `ChannelSpec`.
- `_CDP_PLATFORMS` / `_POISON_HALT_KINDS` · **add** — frozensets after `_PER_ORG_CREDENTIAL_PLATFORMS` (≈`:122`).
- `_effective_channels` · **add** — returns a **new list each call**; synthesizes one `ChannelSpec` from scalars when `channels==()`.
- `_campaign_with_channel` · **add** — `dataclasses.replace(...)` with fresh `list()` seeds + `dict(c.knobs)` + `channels=()` (**C2 identity isolation**).

**Test gate** — `tests/test_config.py`: `test_resolve_home_feed_off_when_only_seed_channels`, `test_channel_spec_from_dict_{valid,invalid_platform_returns_none,explicit_home_feed_override}`, `test_campaign_from_brief_{multi_channel_round_trip,channels_absent_yields_empty_tuple,invalid_platform_in_channels_dropped}`, `test_campaign_to_brief_{omits_channels_for_zero_channels,omits_channels_for_single_channel,emits_channels_for_two_or_more,channels_are_json_serializable}`. `tests/test_cli_run_loop.py`: `test_effective_channels_{legacy_scalar_produces_single_spec,multi_channel_returns_stored_list,returns_new_list_each_call}`, `test_campaign_with_channel_{seed_lists_are_not_aliased,knobs_are_not_aliased,channels_cleared,does_not_mutate_base}`.

**GO** when: all existing config + run-loop tests pass unmodified; `campaign_from_brief('x',{})` → `channels=()`; a seed_channels-only brief → `include_home_feed=False`; `campaign_to_brief` emits `channels` only at ≥2 and JSON-serializes; two `_campaign_with_channel` calls share no mutable state; grep shows exactly 3 `_resolve_home_feed(` lines. **NO-GO** if any execution path changed.

---

### PHASE 2 — Halt classification + CLI fan-out loop · deps: 1

**`reelradar/engines/base.py`**
- `HaltKind` · **add** — `Literal[...]` (import `Literal`); `HaltSession.__init__` · **modify** — `(reason, kind="unknown")`, store `self.kind`; `SUMMARY_KEYS` · **modify** — append `"halt_kind"` (now 12 keys).

**Per-engine `session.py` (7 raise sites + summaries)**
- `instagram/session.py` — `_act:161` → `kind="action_block"`; `_process_comments:182` → `"canary"`; `run:231` daytime → `"daytime"`; capture `halt_kind=h.kind` + emit in both summary dicts.
- `x/session.py` — `_process_comments:150` → `"canary"`; `run:202` → `"daytime"`; `halt_kind` in summary + `run_session` return.
- `linkedin/session.py` — `_process_comments:121` → `"canary"`; `run:169` → `"daytime"`; `halt_kind` in summary + return.
- `youtube / telegram / reddit session.py` — no raise sites; add `"halt_kind": None` to each summary dict.

**`reelradar/cli.py`** *(M6: P1 helpers are consumed, not re-added)*
- `_run_one_channel` · **add** — always runs ≥1 pass (**D1**); no loop for `_SINGLE_PASS_PLATFORMS`; immutable `agg[k]=agg[k]+…`; returns sub-summary with `sessions/spend_usd/halt_reason/halt_kind`.
- `_run_session_loop` · **modify** — full rewrite to channel fan-out over `_effective_channels`; spread-only dict updates; `per_platform` with sub-summary / `{skipped:"cdp_poisoned"}` / `{error:str}`; daytime breaks cleanly (no poison); CDP poison kinds set `cdp_poisoned`.
- `_live_campaigns` · **modify** — stable sort CDP campaigns before API-platform campaigns.
- `cmd_run_all` · **modify** (**G2**) — rename batch flag to `cdp_poisoned`; only a poison-kind CDP halt sets it; daytime + API halts record & continue; skip reason `"cdp batch halted"`; result entries carry `haltKind`.

**Existing-test updates (do not delete)** — `test_run_all.py::test_run_all_halt_stops_batch` → change halt to `action_block`. `test_cli_run_loop.py` full-dict halt tests → switch to `.get()` assertions.

**Test gate** — `tests/test_halt_classification.py` (new) + `tests/test_cli_fan_out.py` (new, 20 cases incl. `test_daytime_halt_ends_run_without_poisoning_api_peers`, `test_action_block_poisons_remaining_cdp_only`, `test_api_halt_does_not_poison_cdp_peers`, `test_channel_runtime_error_preserves_prior_leads`, `test_run_all_cdp_campaign_after_continued_api_halt`, `test_live_campaigns_cdp_ordered_before_api`).

**GO** when: all 7 raise sites carry `kind=`; `SUMMARY_KEYS` has exactly 12 with `halt_kind` last; every engine returns `halt_kind`; daytime breaks without poisoning; a CDP poison-kind skips remaining CDP only; a channel `RuntimeError` preserves prior leads + continues; `cmd_run_all` poisons on CDP-kind only; the two existing tests are updated (not deleted). **NO-GO** until Phase 1 merged + green.

---

### PHASE 3 — Activity-feed platform tag + SCHEMA_VERSION 10→11 · deps: none (parallel)

**`reelradar/core/store.py`**
- `SCHEMA_VERSION` · **modify** — `10 → 11`; append `"; v11: activity-feed platform tag (join-only, no DDL)"`. No new `_init_schema` branch.
- `Store.fetch_run_events` · **modify** — `LEFT JOIN sessions s ON s.session_id = re.session_id`, select `s.platform AS platform`; org filter stays on `re.org_id`; returned dict gains `"platform": str\|None`.

**`server.py` / tests**
- `_serve_run_activity` — no code change; `platform` passes through verbatim.
- `test_server.py::_seed_run_activity` · **modify** — stamp `start_session("sess-seed", cid, "instagram", run_id=run_id)`.

**Test gate** — `tests/test_store.py`: `test_fetch_run_events_returns_platform_from_session` (new), `test_emit_and_fetch_run_events_cursor` (extended: null platform when no session), `test_schema_version_is_11`, `test_v10_self_heal_on_upgrading_db`. `tests/test_server.py`: `test_run_activity_returns_events_and_counters` (extended), `test_run_activity_multi_platform_events_carry_correct_platform` (new).

**GO** when: every `/api/run/activity` event has a `platform` key (string when session resolves, else null); `SCHEMA_VERSION==11`; a v10 DB reopens with zero DDL; org scoping stays on `re.org_id`; pre-existing activity tests pass. Grep test files for a hardcoded `'10'` before merge.

---

### PHASE 4 — Server brief wire-format + merge sentinel + perPlatform · deps: 1, 2

**`reelradar/server.py`**
- `_BRIEF_KEYS` · **modify** — add `'channels': 'channels'` (now 15 pairs); **not** in `_BRIEF_BLANK_DROP_KEYS`.
- `_channel_to_snake` · **add** — non-dict / non-`SUPPORTED_PLATFORMS` → `None`; camel→snake seed coercion (strip blanks); **omit absent** optional seed + `includeHomeFeed` keys (reuses `_to_bool`).
- `_brief_to_snake` · **modify** — channels branch in the loop (`if snake=='channels': … continue`); absent → not emitted (no-change); `[]` / all-invalid → emit `[]` (clear). **C3.**
- `_handle_campaign` · **modify** — comment only documenting the shallow-merge sentinel; no logic change.
- run-result serialization · **add** (**G1**) — surface `per_platform → perPlatform` and `halt_kind → haltKind` in the panel JSON (RunManager outcome / per-page response).

**`reelradar/panel.py`** *(M5: hard-gate on P1; access `c.channels` directly, no getattr guard)*
- `_all_campaign_platforms` · **add** — non-empty channels → each platform; else `[brief.get('platform','instagram')]`.
- `_draft_campaign` · **modify** — add `'platforms': _all_campaign_platforms(stored_brief or {})`.
- `_build_campaigns` · **modify** — primary card adds `'platforms': [ch.platform for ch in campaign.channels] if campaign.channels else [campaign.platform]`.
- `_brief_form_from_stored` · **modify** — emit camelCase `channels` (`[]` when absent).
- `_brief_form_from_campaign` · **modify** — emit `channels` from `c.channels` (`[]` when empty).

**Test gate** — `tests/test_server.py`: `test_channel_to_snake_{valid_entry,invalid_platform_returns_none,absent_optional_fields}`, `test_brief_to_snake_channels_{branch_translates_and_drops_invalid,absent_key_not_emitted,empty_list_emitted_as_empty,all_invalid_emits_empty}`, `test_campaign_channels_{round_trip_via_api,absent_is_no_change,empty_list_clears_stored}`. `tests/test_panel.py`: `test_draft_campaign_card_has_platforms_list`, `test_single_platform_card_platforms_list_fallback`, `test_all_campaign_platforms_from_channels`, `test_brief_form_from_stored_{emits_channels,empty_channels_emits_empty_list}`.

**GO** when: invalid-platform entries silently dropped (no 400); absent = no-change, `[]` = clear, list = atomic replace; every card carries both `platform` and `platforms`; both brief-form serializers emit a `channels` array; `perPlatform` + `haltKind` appear in the run JSON (G1); the listed existing tests pass unmodified. **NO-GO** until Phase 1 (`c.channels`) and Phase 2 (`per_platform`) merged.

---

### PHASE 5 — Frontend schema + form state · deps: 4 (integration)

**`admin-panel/src/shared/schemas/panelState.ts`**
- `channelEntrySchema` · **add** — `platform: z.string()`; seeds `z.array(z.string()).catch([])` (**M1**); `includeHomeFeed: z.boolean().optional()`.
- `campaignBriefFormSchema` · **modify** — `channels: z.array(channelEntrySchema).optional().catch(undefined)`.
- `campaignSchema` · **modify** — `platforms: z.array(z.string()).optional().catch(undefined)` (**M3** — never `.catch([])`).

**`domain.ts` + `useCampaignForm.ts`** *(M2 naming · P5 sole owner of the hook model)*
- `domain.ts: ChannelEntry` · **add** — `z.infer<typeof channelEntrySchema>` (wire type; arrays).
- `useCampaignForm.ts: ChannelFormEntry` · **add** — form interface, comma-string seeds, no `includeHomeFeed`. **Named `ChannelFormEntry`, not `ChannelEntry`** (M2).
- `CampaignFormState` · **modify** — `readonly channels: readonly ChannelFormEntry[]`; `INITIAL_STATE.channels = []`.
- `hasRequiredSeeds` · **modify** — multi-entry: **every** channel independently satisfies its `requireAnyOf`; flat path only when `channels.length===0`.
- `toInput` · **modify** — dual path; emit `channels` only at length **≥ 2** + overwrite scalars from `channels[0]`; ≤1 → flat brief, no `channels` key (**L2**).

**Test gate** — `panelState.test.ts`: well-formed + optional `includeHomeFeed`; **missing seed array degrades to `[]`** (replaces old reject test — M1); old payload → `channels: undefined`; malformed → `undefined`; `platforms` absent/malformed → `undefined` (not `[]`). `useCampaignForm.test.ts` (new): single-platform emits no `channels`; ≥2 emits array + scalars from `[0]`; single-entry collapses with **no** `channels` key (L2); `hasRequiredSeeds` single & multi; `INITIAL_STATE.channels === []`.

**GO** when: seed arrays use `.catch([])` and the M1 degrade test passes; form type is `ChannelFormEntry` and `ChannelEntry` is wire-only; `platforms` degrades to `undefined`; `toInput` emits `channels` only at ≥2; per-entry `hasRequiredSeeds` fails the whole form on any bad channel; existing `panelState.test.ts` passes. **NO-GO** if any restatement names the form type `ChannelEntry` or sets `.catch([])` on `platforms`.

---

### PHASE 6 — Frontend UI (PlatformChannels, multi-chip card, >1-CDP warning) · deps: 5

**`admin-panel/src/features/campaigns/`** *(L1: hook model is P5's; P6 is UI-only)*
- `CampaignForm.tsx: SeedFields` · **modify** — parameterize to `{platform, values, onChange}`; `htmlFor` id uses the **channel index** (`seed-${index}-${field.key}`) not platform, to avoid duplicate-platform id collisions (**L3**).
- `CampaignForm.tsx: PlatformChannels` · **add** — add/remove rows, confirm-before-discard `Modal` (skipped when seeds empty), **>1-CDP `role="alert"` banner**; `CDP_PLATFORMS = new Set(['instagram','x','linkedin'])` matching engine C4.
- `CampaignForm.tsx: body` · **modify** — legacy `<select>` + flat `SeedFields` render only when `channels.length===0`; threshold input stays visible in both modes; `PlatformChannels` always mounted.
- `CampaignCard.tsx` · **modify** — replace inline `<span>` with a `flex-wrap` row of `PlatformChip` over `(campaign.platforms ?? [campaign.platform])` (**C6**); drop now-unused `platformColor` import.

**Consumer responsibility (spec risk):** the edit page's seed builder must convert `briefForm.channels` (wire `string[]`) into `ChannelFormEntry[]` (comma strings) when constructing `CampaignFormSeed.channels`, or the edit form silently drops a multi-channel brief to `[]` on first save. Verify `e.preventDefault()` on the in-`<summary>` remove button stops the `<details>` toggle in jsdom.

**Test gate** — `CampaignForm.test.tsx` (new, RTL): add a 2nd channel; remove empty channel (no dialog); confirm-dialog on removing a seeded channel; `role="alert"` for instagram+x and instagram+linkedin, clears when CDP count drops to 1; per-channel SeedFields show platform-appropriate fields; legacy render intact; `CampaignCard` renders one `PlatformChip` per `platforms` entry; falls back to `[platform]` when undefined.

**GO** when: empty-channels mode is byte-for-byte today's UI; "Add platform" swaps to per-channel sections; confirm-on-discard works; the CDP banner appears/clears correctly; the card renders multi-chip with the `?? [platform]` fallback; the inline-color span is gone; no TypeScript errors. **NO-GO** until Phase 5 merged + green.

---

## 5. Cross-cutting risk register

Severity · issue · closed by.

**Critical**
- **M1** `channelEntrySchema` required-array vs `.catch([])` contradiction → **P5**: canonical `.catch([])`; delete reject test, add degrade-to-`[]` test.
- **M2** Form type name collision (`ChannelEntry` form vs wire) → **P5**: form type is `ChannelFormEntry`; `ChannelEntry` = wire `z.infer` only.
- **M3** `campaignSchema.platforms` cardinality drift → **P5**: canonical `.optional().catch(undefined)`; P6 consumes, never redefines.

**High**
- **G1** `per_platform` defined + consumed but never serialized → **P4**: map `per_platform→perPlatform`, `halt_kind→haltKind` in the panel JSON.
- **G2** `cmd_run_all` flag semantics + unimplemented `haltKind` chip → **P2+P6**: rename batch flag to `cdp_poisoned` + reconcile accounting; implement or drop the dashboard chip (tracked P6 task).
- **G3** `checkpoint`/`login` are enum+poison members never raised → **documented (C4)**: forward-compat only; no code change.

**Medium**
- **M4** 1-channel collapse loses an explicit `include_home_feed` override → **P1**: when `len==1`, source flat scalars (incl. home-feed) from `channels[0]`.
- **M5** Phase 4 reads `c.channels` before Phase 1 may ship → **sequencing**: hard-gate P4 on P1 green CI; access `c.channels` directly (no dead getattr guard).
- **M6 / L1** Double-ownership of `cli.py` helpers & `useCampaignForm.ts` model → **P1 / P5 own**; downstream phases consume.
- **M7** No per-channel home-feed override in the multi-channel UI → **documented (P4/P6)**: engine re-derives seed-aware; intended for v1.
- **L2** `toInput` collapse boundary (≥1 vs ≥2) → **P5**: canonical ≥2 emits / ≤1 flat; fix P5 `toInput` + its inverted test.

**Low**
- **L3** `SeedFields` `htmlFor` id collides for two same-platform channels → **P6**: `seed-${index}-${field.key}`.
- **P2-disp** `HaltSession` must fold into the summary at the dispatch layer → **verify in review**: confirm `dispatch.run_engine_session` folds X/LinkedIn's re-raised halt into the summary, not an escaping exception.
- **P3-join** `LEFT JOIN` adds one index lookup per event row → **accepted** (negligible with `idx_sessions_run`); panel must render `platform=null` gracefully.
- **P3-ver** Hardcoded `'10'` schema-version string in some test → **grep before merge**; reference `str(SCHEMA_VERSION)`.

---

*Source of truth: the Canonical Contract Appendix (C1–C7). Build order: `1 → (2 ∥ 3) → 4 → 5 → 6`, Phase 3 free to start immediately. Line anchors verified against the live `config.py`, `cli.py`, engine session files, `store.py`, `server.py`, `panel.py`, and `admin-panel/src`.*
