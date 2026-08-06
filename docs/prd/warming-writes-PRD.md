# Account Warming — Writes (Engagement) Implementation PRD

**Status:** Build spec (P1) · **Author:** Lead architect (synthesis) · **Date:** 2026-06-29
**Sequel to:** [warming-system-PRD.md](./warming-system-PRD.md) (P0, shipped — dwell-only)
**Scope:** The P1 *write* layer for warming — probabilistic, capped, randomized engagement on top of the shipped P0 dwell-only loop. **Instagram first** (like / save / follow / share-to-DM); **Telegram** as a second, paradigm-distinct surface (search / join / dwell / react).

> This PRD synthesizes six verified design sections (shared executor core, Instagram engagement, Telegram membership warming, data-model/config/panel, warmth-model impact, safety/rollout) and adopts every verifier correction. Where sections contradicted each other or the live code, the resolution is stated inline under **RESOLVED**.
>
> **Reality anchor (do not lose):** the shipped P0 is **dwell-only**. `WarmingSession` walks the feed, calls `pacer.dwell()`, records session metadata — and emits **ZERO** write actions. `tests/test_warming_isolation.py` currently *asserts* that `log_action` never appears in the warming path. **Everything in this PRD is unbuilt P1 work.** Code excerpts are design-target specifications, not documentation of existing code.

---

## 0. Cross-section contradictions — resolved up front

These conflicts recurred across the six sections (or between a section and the live code). They are decided here once; every section below conforms.

| # | Contradiction | RESOLUTION |
|---|---------------|------------|
| **X1** | Multiple sections assert "the executor is the **sole** caller of `store.log_action`." Reality: harvest (`engines/instagram/session.py`) already calls `log_action` for like/follow. | **Warming's executor is the sole *warming-path* caller of `log_action`.** It is NOT the only caller in the whole codebase — harvest also calls it. Isolation is by **structure + stamping**, not exclusivity: warming actions are stamped with `account_id` + the per-org **warming sentinel campaign** (`__warming__:<org_id>`, P0 §C2/D1); harvest actions carry `account_id=None`. The isolation test is updated (X4 below) to assert "`log_action(` appears only in `warming/action_executor.py` within the warming package," not "appears nowhere." |
| **X2** | Sections variously say warming is "strictly zero-ML" (P0 invariant) and "uses a cheap text relevance model." | **P1 warming is NO LONGER strictly zero-ML.** Exactly one ML touch is permitted: a **relevance gate via the cheap text model** — applied to every IG reel reaching `maybe_act`, and to **Telegram search hits only** (seeded channels skip it). It must NOT route through `router.score` / `gate_post` / `upsert_match` / `add_to_watchlist`, and must NOT call `log_spend`. Flagged **OPEN (O-relevance-model)** for product-owner sign-off on the per-item cheap-model spend. |
| **X3** | IG action-block halt is raised as `HaltSession("action_block", kind="account_challenge")`, but `action_block` is **not** a member of `WARMING_CHALLENGE_KINDS` (`warmth.py:38–40`: `checkpoint, empty_interception, account_challenge, arkose, rate_limit, login_drift, session_expired`). | The halt must map to a kind **already in** `WARMING_CHALLENGE_KINDS`. **RESOLVED:** raise `kind="account_challenge"` for an account-level action block (recommended), or `kind="rate_limit"` if the block is rate-based. Optionally add `action_block`/`peer_flood` as *new* kinds (§4.4 / §8.4) — but only as an additive frozenset extension, never as a fictional pre-existing member. Flagged **OPEN (O-halt-kind)**. |
| **X4** | The P0 isolation test asserts `log_action` must NOT appear in warming (`test_..._logs_no_engagement_actions_in_p0`). P1 needs warming to write. | **P1 flips exactly that one assertion.** Replace it with `test_log_action_only_in_action_executor` (grep: `log_action(` appears only in `warming/action_executor.py`). Harvest's own read-only test (`test_shipped_campaign_is_read_only_by_default`) stays unchanged and must still pass. The relaxation is surgical: one path opened, every other guard kept. |
| **X5** | Telegram is described as a full warmable platform, but it is **not** in `WARMABLE_PLATFORMS` (`accounts.py:50` = `{x, linkedin, instagram}`) and `PLATFORM_WEIGHTS` (`warmth.py:27–31`) has no `telegram` row. | **Telegram warming ships AFTER Instagram, gated on three additive prerequisites:** (a) add `"telegram"` to `WARMABLE_PLATFORMS`; (b) add a researched `PLATFORM_WEIGHTS["telegram"]` row (sums to 100); (c) build a Telegram warming executor + the three new TelethonClient write methods. Until all three land, Telegram stays on the `neutral_default()` path (score 50, never blocking). Telegram is **P1.3, explicitly later than the IG phases**. |
| **X6** | "Per-day caps" claimed enforceable today; but `store.action_counts()` (`store.py:1226`) buckets **per-session** and keys on `campaign_id`, and no per-account-per-day method exists. | **A new additive read method is required:** `action_counts_for_account_day(account_id, *, day, action_types)` (a.k.a. `action_counts_today`) — TZ-bucketed, keyed on `account_id`, grouped by `action_type`, mirroring the `spend_by_day` pattern. No DDL, no `SCHEMA_VERSION` bump. This makes caps robust across multiple sessions in the same day. |
| **X7** | New action types (`save`/`share`/`join`/`react`) described as "no schema change," but the base `CREATE TABLE actions` lacks even `account_id`. | **RESOLVED — truly additive, with one nuance:** `actions.account_id` was added in the **v11 migration** (`store.py:659`), not the base schema; P0 already provisions it. The four new `action_type` values are free-text strings (`log_action` inserts verbatim; no `CHECK` constraint exists or may be added). Only the schema *comment* (`store.py:221`) is edited. No migration. |

Other resolved minor points:
- **Construction site (RESOLVED).** The executor is constructed **only inside `WarmingSession.__init__`** — never in `dispatch.py`, `cli.py`, or harvest. `run_warming_session` gains no new parameter. (§3.1)
- **Delay distribution (RESOLVED).** Delays MUST be randomized and right-skewed (variable ~2–8s plus an occasional longer pause), **never a fixed constant** — a fixed cadence is itself an automation fingerprint. Reviewers flag any literal `time.sleep(constant)` in the executor. (§3.6, §7)
- **`network` keep-alive (RESOLVED).** The warmth `network` component survives via IG **follow** and TG **channels-joined** (`join`). `like`/`save`/`share`/`react` are deliberately **excluded** from the network query. (§5)

---

## 1. Problem & goals

**Problem.** P0 shipped a structurally-safe warming loop, but it only **observes** (dwells). Dwell alone is the strongest single algorithm signal and zero-risk, but it leaves the warmth `network` component with no live data source and gives the platform a thin behavioral profile. To actually ramp an account toward harvest-readiness we need bounded, human-shaped **engagement writes** — without weakening any P0 safety invariant and without touching read-only harvest.

**Goals (P1):**
- Add a single new write component — **`WarmingActionExecutor`** — constructed only inside `WarmingSession`, the sole warming-path caller of `log_action` and of feed write helpers.
- **Instagram:** relevance-gated, probabilistic, per-day-capped **like / save / follow / share-to-DM** layered on always-on dwell.
- **Telegram:** a paradigm-distinct **search → join → dwell → react** loop (no feed, no like/follow/share), heavily rate-limited.
- Keep the warmth `network` signal alive (IG follow / TG join), add a scoped Telegram weight row, and classify the new write-era failure modes (action-block / flood) into the penalty model.
- Preserve every P0 invariant: 3-layer kill-switch, **forced** daytime guard, warming allowlist, harvest stays read-only by config default + test guard.

**Non-goals (P1):**
- No message sends / DMs *as content* (IG share-to-DM is the sole DM surface, and it shares a reel, never composes a message; TG sends nothing).
- No operator-crankable caps — caps are engine-tuned and panel-read-only (a safety invariant).
- No new persisted warmth state — warmth stays computed-on-read (P0 §C3).
- Telegram weight-row tuning numbers are proposals pending sign-off; account creation remains out.

---

## 2. Locked vs OPEN decisions

### Locked (design within these)
1. **Everything sits on top of shipped P0.** Behind the 3-layer kill-switch (`AIZU_WARMING_ENABLED` default OFF + per-org `warmingEnabled` + per-org `warmingDisabledPlatforms`), the FORCED daytime guard (explicit `enforce_daytime=True` beats `AIZU_IGNORE_DAYTIME`), and the warming allowlist.
2. **Warming is the SOLE warming-path `store.log_action` caller** (harvest stays read-only by config-default + test guard). Harvest is byte-for-byte unchanged.
3. **`WarmingActionExecutor` is constructed ONLY inside `WarmingSession`.** `WarmingSession` is dwell-only today; the executor is the entire P1 addition.
4. **IG action set is exactly:** watch-to-completion (ALWAYS) + like (high) + save (medium) + follow (LOW + tiny cap) + share-to-DM (RARE + tightest cap). No more, no less. **Follow is KEPT** — it is the sole live source of the IG warmth `network` component.
5. **TG action set is exactly:** discover (seed_channels + keyword search) → relevance-gate **search hits only** → join (per-day capped) → dwell → emoji-react (occasional). No like/follow/share/sends.
6. **Per-day caps are engine-tuned in `ramp.py`** per stage (observe/light/ramp/sustain); panel shows them **READ-ONLY**; never operator-crankable (safety invariant).
7. **Delays are randomized / right-skewed, never a fixed constant.**
8. **`share_target` is an OPTIONAL per-campaign knob** (default = first contact in the IG share list).
9. **Phased rollout: Instagram first** (behind the allowlist, one internal org), Telegram later.

### RESOLVED — Instagram P1 build (product owner, 2026-06-29)

The Instagram warming-writes build is shipped. The product owner resolved the IG-relevant OPENs below with these binding values. **Telegram OPENs remain open** (the table that follows) — a separate follow-up build ships Telegram.

| ID | Resolution (Instagram, binding) |
|----|----------------------------------|
| **O6** | Per-relevant-reel action probabilities (rolled **independently**): like `0.70`, save `0.30`, follow `0.12`, share `0.05`. Per-DAY caps by ramp stage `observe(0–3) / light(4–7) / ramp(8–14) / sustain(15+)`: like `0/15/30/50`, save `0/8/15/25`, follow `0/1/3/5`, share `0/0/1/2`. Instagram `_PLATFORM_CAPS` ceilings: like `50`, save `25`, follow `5`, share `2`. `observe` stays fully read_only (all caps 0, no writes; explicit `saves=0, shares=0`). Never exposed to operators. |
| **O-relevance-model** | **REVISED for Instagram:** the IG relevance gate is a **cheap, ZERO-ML, NO-API-KEY heuristic** — case-insensitive token overlap of the reel's caption/author/ocr_text against the campaign's `seed_hashtags` / `seed_accounts` / `relevance_def`. Empty seeds ⇒ every reel relevant (home-feed warming). This **preserves P0's MockRouter wiring** (warming still needs no OPENROUTER key) and performs NO router scoring / `upsert_match` / `add_to_watchlist` / `log_spend`. The cheap-text-model LLM gate is **Telegram-only** and ships with the Telegram build. |
| **O-halt-kind** | An IG action-block during warming calls `store.raise_flag(kind="account_challenge", severity="halt", org_id, account_id)`, raises `HaltSession`, and transitions the backing account to `FLAGGED` via `update_account_lifecycle`. Uses the **existing** `account_challenge` kind — no new `WARMING_CHALLENGE_KINDS` member added in this build. |
| **O-share-ship** | **SHIP `share` in P1**, but it fires only at `ramp(8–14)+` stages (cap 0 below, and `p_share=0.0` below — double-barrel gated), at the lowest probability (`0.05`) and tightest cap (`≤2`). `save` ships unconditionally from `light`. |
| **O-share-target** | `Campaign.share_target: Optional[str] = None` (an IG handle). `share_reel` DMs that handle if set, else the FIRST contact in the share list. Round-trips through `load_campaign` / `campaign_from_brief` / `campaign_to_brief`. Optional panel field with a "set this for safest warming" hint (config + round-trip REQUIRED; panel field optional). |
| **O-dm-regex** | A **structural hook** lives in `detect_action_block()` for DM-share failure phrases, with a `TODO(O-dm-regex)` for the live-sampled regex. No phrases invented — the regex lands after the first live IG DM-block capture. |
| **O-mid-session-daytime** | **RESOLVED.** Re-check BOTH the daytime guard and `warming_kill_reason` at the top of every dwell window; if either closes, stop issuing further writes this run (finish any in-flight action; never abort mid-action). |

### RESOLVED — Telegram P1 build (product owner, 2026-06-30)

The Telegram warming-writes build is shipped. The product owner resolved the Telegram OPENs below with these binding values. **O7 (L3 circuit-breaker) remains deferred.**

| ID | Resolution |
|----|------------|
| **O-tg-weights** | **RESOLVED.** `WARMABLE_PLATFORMS` adds `"telegram"` → `{x, linkedin, instagram, telegram}`. `PLATFORM_WEIGHTS["telegram"]` = `{age:25, ramp:20, network:30, profile:10, trust:15}` (sum 100). `network` = channels-joined; `profile` = TG profile completeness (photo/bio/username) from `accounts.detail`; `ramp` = warming-session cadence; `trust` = flood-absence. Additive row — `x`/`linkedin`/`_default` unchanged. The P0 tests that asserted Telegram is non-warmable now use `youtube`/`reddit` as the rejected examples. |
| **O-tg-network-divisor** | **RESOLVED.** Per-platform `TARGET_NETWORK` lookup defaulting to 20: `telegram = 8`, `instagram`/default keep `TARGET_CONNECTS = 20`. The network warmth query is widened to `action_type IN ('follow','connect','join')` so TG `join` actions count (additive; IG behavior unchanged). |
| **O-tg-join-gap** | **RESOLVED — by caps, not a new `PacingConfig` field.** At most **ONE join per warming session** (joins are spread across sessions/days), under the per-day stage cap (joins `0/1/2/3` for observe/light/ramp/sustain). Combined with the right-skewed inter-action delay (`_delay`, never a fixed constant) and the forced daytime guard, this spaces joins far apart without a dedicated `join_gap` knob (YAGNI). React caps are `0/3/5/8` with `p_react = 0.4` on dwelled messages, under the per-day cap; `observe` stays read-only. |
| **O-tg-gate-router** | **RESOLVED — thin helper.** The TG relevance gate is a dedicated thin text helper (`tg_relevance.build_relevance_gate`), NOT the harvest `router`. LLM runs on **search hits ONLY**; operator-seeded `seed_channels` always pass (skip the gate). It performs no `router.score` / `upsert_match` / `add_to_watchlist` / `log_spend`. With no `OPENROUTER_API_KEY` it degrades to seeded-channels-only (keyword search + gate skipped) so warming still runs, and is fail-closed on any parse failure. |
| **O-relevance-model** (TG half) | **RESOLVED.** This is the single ML touch on the Telegram warming path — a cheap text-model gate over search-hit metadata only (seeded channels add zero ML cost). Same isolation guards as above; no `log_spend`. Telegram warming is therefore no longer strictly zero-ML, by design and bounded to search-hit vetting. |

#### Still OPEN — Telegram

| ID | Decision | Recommendation | Impact if unset |
|----|----------|----------------|-----------------|
| **O7** | **L3 circuit-breaker** (auto-trip per-org after N consecutive flags). | Defer past P1; a single block already halts + craters score. | No auto-cooldown across runs until built. |

---

## 3. The shared core — `WarmingActionExecutor` + extended ramp budget

This is the **single new write component** P1 introduces. It is **platform-agnostic core**: the IG action set (§4) and the TG action set (§7) plug into it. It turns the P0 dwell-only loop into a probabilistic, per-day-capped, randomized-delay engager — without touching harvest and without weakening the P0 isolation guard.

### 3.1 Where it slots (no harvest change, P0 guard preserved)

The P0 loop body (`aizu/engines/warming/session.py:158–164`) today:

```python
for reel in walker:
    self.reels_observed += 1
    observed += 1
    self.pacer.dwell()
    # P0: dwell-only. NO writes, NO router, NO match capture.
    if observed >= self.cfg.window_observe_items:
        break
```

P1 inserts exactly **one** call after `pacer.dwell()`:

```python
for reel in walker:
    self.reels_observed += 1
    observed += 1
    self.pacer.dwell()
    self._executor.maybe_act(reel, budget)   # P1: the ONLY new write surface
    if observed >= self.cfg.window_observe_items:
        break
```

The executor is built once in `WarmingSession.__init__` (alongside `self.pacer`, `session.py:70`) and never escapes the session:

```python
self._executor = WarmingActionExecutor(
    feed=self.feed, store=self.store,
    sentinel_campaign=accounts_lib.warming_sentinel_campaign(self.org_id),
    account=self.account, account_id=self._account_id(),
    session_id=self.session_id, pacer=self.pacer,
    platform=self.campaign.platform, campaign=self.campaign,
    relevance_gate=relevance_gate,   # platform-supplied; see §3.5
    rng=self.pacer.rng,              # share the session RNG for deterministic tests
)
```

**RESOLVED — construction site.** Constructed *inside* `WarmingSession`, never in `dispatch.py`/`cli.py`/harvest. `run_warming_session` (`session.py:220–233`) gains no new parameter. **Harvest is byte-for-byte unchanged** — warming routes early in `dispatch.run_engine_session` (`dispatch.py:187–192`), *before* `select_engine()`; the executor lives in `aizu/engines/warming/` and is never imported by harvest.

### 3.2 Extended `ActionBudget` (probabilities + per-day caps)

The P0 `ActionBudget` (`ramp.py:14–21`) is a frozen dataclass with integer count ceilings (`stage, likes, follows, connects, dwell_windows, read_only`). P1 extends it **additively** — every new field defaults to `0`/`0.0`, so existing call sites keep constructing it and the P0 schedule shape is unchanged:

```python
@dataclass(frozen=True)
class ActionBudget:
    # --- P0 (unchanged) ---
    stage: str
    likes: int
    follows: int
    connects: int
    dwell_windows: int
    read_only: bool
    # --- P1 Instagram per-day count ceilings ---
    saves: int = 0
    shares: int = 0
    # --- P1 Telegram per-day count ceilings ---
    joins: int = 0
    reacts: int = 0
    # --- P1 per-action fire probabilities, rolled per RELEVANT reel/item ---
    p_like: float = 0.0
    p_save: float = 0.0
    p_follow: float = 0.0
    p_share: float = 0.0
    p_join: float = 0.0
    p_react: float = 0.0
    # --- P1 right-skewed inter-action delay envelope (seconds) ---
    delay_min: float = 2.0
    delay_max: float = 8.0
    delay_long_p: float = 0.12    # prob. of an occasional longer "distraction" pause
    delay_long_max: float = 25.0  # ceiling of that longer pause
```

**Design rules:**
- **Watch-to-completion (dwell) is NOT in this table.** Dwell ALWAYS happens for every observed reel via the existing `pacer.dwell()` call (`session.py:161`). No probability, no cap — the zero-risk baseline, never gated by the executor.
- **Probabilities encode the risk tier:** `p_like` high, `p_save` medium, `p_follow` LOW, `p_share` RARE. Per-relevant-reel roll probabilities; the per-day caps are the hard backstop.
- **Caps are the safety invariant.** A probability can never exceed a cap: once the day's `likes` (etc.) are exhausted, the roll is skipped regardless of `p_like`.

Proposed `_STAGES` extension (**O6 — engine-tuned**; conservative starting point):

| Stage (days) | dwell | likes / p_like | saves / p_save | follows / p_follow | shares / p_share | read_only |
|---|---|---|---|---|---|---|
| observe (0–3) | 2 | 0 / 0.0 | 0 / 0.0 | 0 / 0.0 | 0 / 0.0 | **true** |
| light (4–7) | 3 | 2 / 0.40 | 1 / 0.15 | 0 / 0.0 | 0 / 0.0 | false |
| ramp (8–14) | 3 | 4 / 0.55 | 2 / 0.25 | 1 / 0.05 | 1 / 0.02 | false |
| sustain (15+) | 4 | 6 / 0.60 | 3 / 0.30 | 2 / 0.06 | 2 / 0.03 | false |

Telegram stage rows (§7.5): observe `joins=0/reacts=0`; light `joins=0/reacts=0`; ramp `joins=1/reacts=2`; sustain `joins=2/reacts=3`.

`budget_for_day(ramp_day, platform)` (`ramp.py:50–65`) keeps its signature and `min()`-clamp-by-cap behavior; it additionally clamps the new count fields against `_PLATFORM_CAPS` and carries the probability/delay fields through. The `observe` stage stays `read_only=True` and is **never widened by a cap** (existing guard `ramp.py:52–53`); the executor additionally treats `read_only` as a hard short-circuit (§3.3).

**`_PLATFORM_CAPS` extension (`ramp.py:36–40`):** Instagram caps `saves`/`shares` (`shares` tightest, proposed `≤2`, only from `ramp` stage onward — it is the highest-risk action). Telegram zeroes the IG actions and caps `joins`/`reacts`:

```python
# ramp.py _PLATFORM_CAPS — ADDITIVE "telegram" row
"telegram": ActionBudget("cap", likes=0, follows=0, connects=0,
                         dwell_windows=4, read_only=False,
                         joins=3, reacts=5),
```

So a fresh TG account joins **at most one channel/day** at light/ramp, ramping to a small sustain ceiling.

### 3.3 Executor contract

```python
class WarmingActionExecutor:
    """P1 warming write core. Constructed once per session INSIDE WarmingSession.
    Sole warming-path caller of feed write helpers and of store.log_action(...).
    Performs NO ML inference except the relevance gate (router/upsert_match/log_spend forbidden)."""

    def __init__(self, *, feed, store, sentinel_campaign: str,
                 account: dict, account_id: int, session_id: str,
                 pacer: Pacer, platform: str, campaign: Campaign,
                 relevance_gate: "RelevanceGate", rng: random.Random) -> None: ...

    def maybe_act(self, reel: Reel, budget: ActionBudget) -> None:
        """Evaluate ONE observed reel/item. Choreography (§3.4):
          0. Short-circuit if budget.read_only OR no remaining cap on any action.
          1. Relevance-gate the item (§3.5). Irrelevant -> return (dwell already happened).
          2. Randomized human delay (pacer) BEFORE the first action.
          3. For each platform action, independently roll its probability AND check
             its remaining per-day cap; collect the allowed actions.
          4. Fire each allowed action in turn, with a randomized right-skewed delay
             BETWEEN actions (§3.6). Each fire => store.log_action(...).
          5. On an action-block / flood signal, raise HaltSession (§4.4 / §8).
        Returns None; all effects are writes + logged actions."""
```

Field reads only: the executor touches `reel.reel_id` and `reel.author` (a plain string — `Reel` has fields `reel_id, caption, author, ocr_text, on_screen_frames, comments`; no `User` object, no platform-specific attrs). For IG share it also reads the campaign-level `share_target`. It **never** calls `router`, `upsert_match`, `add_to_watchlist`, `log_spend`, `score_comment`, or `gate_post`. The relevance gate (§3.5) is the one ML touch — platform-scoped, cheap text model — documented so the isolation test is updated, not silently violated.

### 3.4 Choreography per relevant reel/item (the agreed sequence)

```
maybe_act(reel, budget):
    if budget.read_only or _all_caps_exhausted(budget):    # observe stage / day done
        return
    if not relevance_gate.is_relevant(reel):               # §3.5; IG always, TG search-only
        return                                             # dwell already counted upstream

    _human_delay(budget)                                   # right-skewed pause BEFORE acting

    planned = []
    for action in _PLATFORM_ACTIONS[platform]:             # e.g. ('like','save','follow','share')
        if _remaining(action) <= 0:                        # per-day cap backstop
            continue
        if rng.random() < _prob(budget, action):           # INDEPENDENT roll per action
            planned.append(action)

    for i, action in enumerate(planned):
        if i > 0:
            _human_delay(budget)                            # right-skewed pause BETWEEN actions
        ok = _fire(action, reel)                            # feed.<helper>(...) ; may HaltSession
        store.log_action(sentinel_campaign, action,         # campaign_id first, action_type second
                         reel_id=_reel_scoped(action) and reel.reel_id or None,
                         target=_target(action, reel), succeeded=ok,
                         session_id=session_id, account_id=account_id)
        if ok:
            _record(action)                                 # decrement remaining cap on success
```

> `_PLATFORM_ACTIONS` is a **new** dict (does not exist yet) mapping platform → tuple of action-type strings, e.g. `{"instagram": ("like","save","follow","share"), "telegram": ("join","react")}`.

- **Independent rolls.** Each action's probability is rolled separately — a reel may get like + save but no follow. No "pick one" coupling.
- **`log_action` signature is exact:** `log_action(campaign_id, action_type, *, reel_id=None, target, succeeded, session_id=None, account_id=None)` — first arg `campaign_id` (the sentinel string), second `action_type` (string), `target` keyword-only and **required**.
- **Caps gate before the roll fires**; `_record` decrements only on a *successful* fire, so a failed like does not consume the day's like budget.
- **`read_only` short-circuit** means the observe stage performs zero writes even if a probability were misconfigured — defense in depth over cap=0.

### 3.5 Relevance gating (the one ML touch — RESOLVED per X2)

- **Instagram:** every reel reaching `maybe_act` is relevance-checked via the cheap text model so writes only land on *relevant* reels. Dwell is unconditional; **like/save/follow/share are relevance-gated.**
- **Telegram:** the gate applies to **search hits only.** Operator `seed_channels` are trusted and skip the check (§7.4); the executor receives a `relevance_gate` whose `is_relevant` returns `True` unconditionally for seeded sources.

**OPEN (O-relevance-model).** Warming is **no longer strictly zero-ML.** The gate routes via the cheap text model only. `tests/test_warming_isolation.py` is updated to (a) still forbid `router.score`/`upsert_match`/`log_spend`/`add_to_watchlist`, and (b) explicitly *allow* the one relevance-gate path.

### 3.6 Randomized right-skewed human delays (never a fixed constant)

A fixed cadence is itself an automation fingerprint, so delays MUST be variable and right-skewed. The executor reuses the session `Pacer` (`aizu/core/pacing.py`), which already injects a seedable `rng` (`pacing.py:48`) and `uniform`-samples (`dwell()` `pacing.py:64–67`, `between_reels()` `pacing.py:69–72`):

```python
def _human_delay(self, budget) -> float:
    # Right-skew: usually a short variable pause; occasionally a longer "distraction".
    if self.rng.random() < budget.delay_long_p:
        t = self.rng.uniform(budget.delay_max, budget.delay_long_max)
    else:
        t = self.rng.uniform(budget.delay_min, budget.delay_max)
    self.pacer._sleep(t)     # same injected sleep the Pacer uses (test-injectable)
    return t
```

- Typical pause ≈ `uniform(2, 8)` s with a ~12% chance of a longer `uniform(8, 25)` s pause.
- Sharing `Pacer.rng` lets the deterministic-seed test pattern cover the executor; sharing the injected `sleep` keeps tests instant.
- **NEVER** a fixed `time.sleep(constant)` in the executor — reviewers flag any literal sleep.

### 3.7 Per-day count enforcement (no schema reshape)

Caps are **per-account-per-day**, not per-session. P0's `store.action_counts()` (`store.py:1226–1237`) buckets per *session* only and keys on `campaign_id`, so the executor needs the day's running totals at construction time, seeded from the DB and decremented in memory as it fires.

**New store method (additive, no DDL)** — mirrors the TZ-bucketed `spend_by_day` pattern (`store.py:1779–1782`, `TZ_SQL_SHIFT = "+5 hours"` at `store.py:70`):

```python
def action_counts_for_account_day(
        self, account_id: int, *, day: str,
        action_types: Optional[Sequence[str]] = None) -> dict[str, int]:
    """Successful warming actions by this account on one local day, grouped by
    action_type. Used to seed the executor's remaining-cap counters. `day` is
    'YYYY-MM-DD'; keys on account_id, NEVER on the sentinel campaign_id."""
    q = ("SELECT action_type, COUNT(*) AS n FROM actions "
         "WHERE account_id=? AND succeeded=1 "
         f"AND strftime('%Y-%m-%d', datetime(created_at,'unixepoch','{TZ_SQL_SHIFT}'))=?")
    args: list[Any] = [account_id, day]
    if action_types:
        q += f" AND action_type IN ({','.join('?' for _ in action_types)})"
        args.extend(action_types)
    q += " GROUP BY action_type"
    rows = self._conn.execute(q, args).fetchall()
    return {r["action_type"]: r["n"] for r in rows}
```

The `actions` table already carries `account_id` (v11 migration, `store.py:659`) and `action_type` (free text; schema comment `store.py:216–225`). **No migration / no reshape** — purely a new read method plus new `action_type` string values.

Executor remaining-cap logic:

```python
# at __init__ (day computed from account.fingerprint.timezone_id; see O-tz below):
used = store.action_counts_for_account_day(account_id, day=local_day,
                                           action_types=_PLATFORM_ACTIONS[platform])
self._remaining = {a: max(0, _cap_for(budget, a) - used.get(a, 0)) for a in _PLATFORM_ACTIONS[platform]}
# per successful fire:
self._remaining[action] -= 1
```

This makes the per-day cap robust across **multiple warming sessions in the same day** — the second session sees the first's logged actions.

> **OPEN (O-tz):** the `+5 hours` Tashkent shift (`store.py:70`) is hardcoded — correct for current ops, not the true local day for accounts in other zones. For tightest per-account-local-day bucketing the caller computes `day` in Python from `account.fingerprint.timezone_id` (mirroring the daytime guard, `cli.py:153–159`) and passes it in. The signature already takes `day`, so this is a caller choice, not a schema issue.

---

## 4. INSTAGRAM section (watch / like / save / follow / share-to-DM)

> **Status:** entirely P1 / unbuilt. Shipped IG warming is dwell-only. This section specifies the IG action set that plugs into the §3 executor.

### 4.1 Action set

Each action is evaluated **independently per relevant reel** — one reel may trigger zero, one, or several. Probabilities and per-day caps are **engine-tuned in `ramp.py`** per stage (§3.2) and clamped by `_PLATFORM_CAPS["instagram"]`; the panel renders them **read-only** (never operator-crankable).

| Action | Probability | Daily cap | Risk | CDP helper | Status | `action_type` |
|--------|-------------|-----------|------|------------|--------|----------------|
| **Watch-to-completion (dwell)** | ALWAYS (p=1.0) | none | none | `Pacer.dwell()` (P0, wired) | exists | metadata only (not an `actions` row) |
| **Like** | high | `budget.likes` | low | `feed.like_reel(reel)` | exists (`cdp.py:241`) | `like` |
| **Save** | medium | `budget.saves` | low | `feed.save_reel(reel)` | **P1 NEW** | `save` |
| **Follow** | LOW | `budget.follows` | medium | `feed.follow_author(reel)` | exists (`cdp.py:250`) | `follow` |
| **Share to DM** | RARE | `budget.shares` | **highest** | `feed.share_reel(reel, target)` | **P1 NEW** | `share` |

**Dwell (always-on, zero-risk).** Strongest algorithm signal, no automation footprint. Not logged to `actions` — only recorded in `accounts.detail` as today.

**Follow keeps the warmth `network` signal alive.** `network = min(1.0, network_successes / TARGET_CONNECTS)` (`warmth.py:189`), where `network_successes` counts `action_type IN ('follow','connect')` (`store.py:1529–1533`). Dropping follow would zero the IG network component. It is therefore **KEPT** despite medium risk — gated to LOW probability + a tiny daily cap. (Note: in the live query both `follow` *and* `connect` count; on IG warming only `follow` will land, making it IG's sole live network source.)

### 4.2 Per-reel choreography

Fired by `WarmingActionExecutor.maybe_act(reel, budget)` at the §3.1 insertion point. Both `pacer.dwell()` and the executor's `_human_delay` draw from `random.uniform` — the executor MUST NOT introduce fixed `time.sleep(...)` calls (beyond CDP `settle_seconds`). The existing `_human_pause()` in `_click_centermost` (`cdp.py:253`) covers pre-click randomization. (Sequence per §3.4.)

**Non-exclusivity (RESOLVED per X1).** Both warming (P1) and harvest call `store.log_action()`. Harvest logs with `account_id=None`; warming logs with `account_id=<warming account_id>` + the sentinel campaign. Intentional (store.py:1214) and supports the warmth join. Neither is the codebase-wide "sole" caller — warming is the sole *warming-path* caller.

### 4.3 New CDP feed helpers (P1)

**`save_reel`** — mirrors `like_reel`: a single `_click_centermost` on the Save SVG's clickable ancestor. Low risk, no social footprint, no recipient resolution.

```python
def save_reel(self, reel: Reel) -> bool:
    """Save/bookmark the opened reel. Requires open_reel() first.
    Skips silently if already saved. No-op on read-only feeds."""
    return self._click_centermost(
        self._ipage,
        """() => [...document.querySelectorAll(
               "svg[aria-label='Save'], svg[aria-label='Add to Favorites']")]
             .map(s => s.closest("[role=button], button, div[tabindex='0']") || s)""")
```

*Fragility (LOW–MEDIUM):* `aria-label` text drifts across IG versions; no observable success signal (success inferred from a clean click only). **OPEN — selector drift:** re-verify against live IG before launch.

**`share_reel` + `share_target` resolution** — RARE, tightest cap, **highest-risk** IG action; a 3-step DOM choreography with no confirmation signal. Recipient resolved at call time: (1) the campaign's optional `share_target` username if set, else (2) the **FIRST contact** in IG's native DM share list (positional fallback). The panel SHOULD hint operators to set `share_target` explicitly.

```python
def share_reel(self, reel: Reel, share_target: Optional[str]) -> bool:
    """Share the opened reel via DM. If share_target is set, search+pick that
    username; else pick the FIRST contact. Returns True only if Send clicked
    without an error toast. No-op on read-only feeds."""
    page = self._ipage
    if page is None:
        return False
    try:
        # Step 1 — open share sheet
        if not self._click_centermost(page,
                """() => [...document.querySelectorAll("svg[aria-label='Share']")]
                     .map(s => s.closest("[role=button], button, div[tabindex='0']") || s)"""):
            return False
        time.sleep(self.cfg.settle_seconds)
        # Step 2 — resolve recipient
        if share_target:
            box = page.query_selector("input[placeholder*='Search'], input[type='text']")
            if not box:
                return False
            self._human_pause()
            box.type(share_target)
            time.sleep(self.cfg.settle_seconds)
        recipient = page.query_selector("[role=option], [role=button][tabindex='0']")
        if not recipient:
            return False
        self._human_pause(); recipient.click()
        time.sleep(self.cfg.settle_seconds)
        # Step 3 — send
        send = page.query_selector("button:has-text('Send'), [role=button]:has-text('Send')")
        if not send:
            return False
        self._human_pause(); send.click()
        time.sleep(self.cfg.settle_seconds)
        return not self.detect_action_block()
    except Exception:
        return False
```

*Fragility & anti-detection (VERY HIGH):* DOM-only, no interception fallback; no confirmation signal (`True` = "Send clicked + no action-block toast"); recipient ambiguity on positional pick; separate rate-limit surface (IG may action-block DM-send independently). **OPEN — extend `detect_action_block` regex** with DM phrases (e.g. `can't send|message couldn't be sent|spam`) once a live DM-block sample exists (O-dm-regex). RARE cap keeps absolute volume minimal. **Recommendation: gate `share` behind an explicit per-campaign opt-in** and ship like/save/follow first (O-share-ship).

### 4.4 Halting on action-block

Before/after a fire the executor consults `feed.detect_action_block()` (P0 `core/feed.py:73–75`; CDP impl `cdp.py:257–269` scanning "action blocked / try again later"). On a positive signal it raises `HaltSession`, caught by `WarmingSession.run`'s existing `except HaltSession` (`session.py:179–186`), which raises a `health_flag` and ends the session `halted`.

**RESOLVED per X3:** the halt maps to an existing `WARMING_CHALLENGE_KINDS` member — **`account_challenge`** for account-level blocks (recommended) or `rate_limit` if rate-based (O-halt-kind). `compute_penalty` (`warmth.py:138–160`) then craters the score (`PENALTY_CHALLENGE_FRESH=0.10`, 24h fresh / 72h recent). The executor only *raises* — it never persists or swallows.

---

## 5. (folded into §4/§6) — see warmth-model impact in §9 for IG `network` sourcing.

---

## 7. TELEGRAM section (search / join / dwell / react — a different paradigm)

> **Status:** P1 design, ships AFTER Instagram (X5). None of the methods/dataclasses/queries below exist yet. Telegram is NOT in `WARMABLE_PLATFORMS` today.

### 7.1 Why Telegram is a different paradigm

Instagram rolls per-action probabilities per relevant reel in an algorithmic feed. Telegram has **no algorithmic feed and no like/follow/share** surface.

| IG verb | TG equivalent | Why |
|---------|---------------|-----|
| watch-to-completion (dwell) | **dwell** = read channel history (`iter_channel_messages`) | presence without footprint |
| like (high prob) | **emoji-react** (occasional) | the only lightweight per-message engagement TG offers |
| follow (network signal) | **join channel** (per-day capped) | sole source of TG's `network`-analog (channels-joined) |
| save / share-to-DM | **— (omitted)** | no analog; do **not** build |

**RESOLVED:** TG warming = `search → relevance-gate-search-hits → join (capped) → dwell → react (occasional)`. No message sends, no DMs, no follows.

### 7.2 New `TelethonClient` write methods (P1)

The client is built on `telethon.sync.TelegramClient` (`feed.py:239`) — **synchronous**, so these are plain sync methods, consistent with existing `iter_*` methods. **Prerequisites:** the §3.2 `ActionBudget` `joins`/`reacts` fields; a new `TgChannel` dataclass (mirror `TgMessage` `feed.py:33–40`: `username, title, participants, is_channel`); a new **`TelegramWarmingPort`** Protocol (separate from the read-only `TelegramClientPort`, which stays untouched); and the §3.7 `action_counts_for_account_day` query.

**`search_channels`** — `messages.SearchGlobalRequest` (read-only RPC; the join is the write):

```python
def search_channels(self, query: str, limit: int = 10) -> list[TgChannel]:
    from telethon.tl.functions.messages import SearchGlobalRequest
    from telethon.tl.types import InputPeerEmpty, InputMessagesFilterEmpty
    from datetime import datetime
    res = self._client(SearchGlobalRequest(
        q=query, filter=InputMessagesFilterEmpty(),
        min_date=datetime.fromtimestamp(0), max_date=datetime.now(),
        offset_rate=0, offset_peer=InputPeerEmpty(), offset_id=0, limit=limit))
    out: list[TgChannel] = []
    for chat in getattr(res, "chats", []):
        uname = getattr(chat, "username", None)
        if not uname:
            continue
        out.append(TgChannel(username=f"@{uname}",
                             title=getattr(chat, "title", "") or "",
                             participants=getattr(chat, "participants_count", 0) or 0,
                             is_channel=bool(getattr(chat, "broadcast", False))))
    return out
```

**`join_channel`** — `channels.JoinChannelRequest` (THE write that grows channels-joined):

```python
def join_channel(self, channel: str) -> bool:
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.utils import get_input_channel
    input_ch = get_input_channel(self._client.get_input_entity(channel))
    return bool(self._client(JoinChannelRequest(input_ch)))
```

*Idempotency:* rejoining raises `UserAlreadyParticipantError` → treat as no-op success, **don't** spend join budget.

**`react_message`** — `messages.SendReactionRequest` (lightweight engagement, never a send):

```python
def react_message(self, channel: str, message_id: int, emoji: str) -> bool:
    from telethon.tl.functions.messages import SendReactionRequest
    from telethon.tl.types import ReactionEmoji
    from telethon.utils import get_input_peer
    peer = get_input_peer(self._client.get_input_entity(channel))
    return bool(self._client(SendReactionRequest(
        peer=peer, msg_id=message_id,
        reaction=[ReactionEmoji(emoticon=emoji)], add_to_recent=True)))
```

*Soft skip:* a disallowed emoji raises `ReactionInvalidError` → logged `succeeded=False`, continue (not a halt). React with a small random allowlist (`"👍","🔥","❤"`). **GAPs (verify on pinned Telethon):** `SearchGlobalRequest` ranks by message recency not channel quality; `participants_count`/`broadcast`/`emoticon` field names vary by version — `getattr(..., default)` guards degrade gracefully.

### 7.3 Discovery: `seed_channels` + keyword search

Two candidate sources per session:
1. **Operator-seeded** — `campaign.seed_channels` (`config.py:248`). **Trusted; skip the relevance gate.** Same seeds harvest walks.
2. **Keyword search hits** — `search_channels(q)` per keyword. **Untrusted; relevance-gated** (§7.4).

**RESOLVED — keyword source.** Reuse the campaign's existing relevance/seed prose (`campaign.seed_direction` + `campaign.goal`, `config.py:223–230`) split into keyword phrases. No new TG-keyword config knob in P1 (YAGNI).

### 7.4 LLM relevance gate — search hits only (cheap text model)

**RESOLVED, and the deliberate departure from P0 zero-ML (X2).** Search hits get one cheap text-model relevance call each; seeded channels skip it. This is the only inference any warming path performs.

- **Route via the cheap text model.** The warming `MockRouter` (`cli.py:126–143`) must be swapped for a real cheap-text router on the TG warming path, or a thin direct helper. **OPEN (O-tg-gate-router):** reuse `_build_warming_io`'s router slot vs. a dedicated `relevance_gate_text(name, desc, goal) -> bool` helper — **recommend the thin helper** (keeps `router.score`-forbidden guard intact).
- **Input is metadata only** — `title` + `username` + `participants` + campaign goal. No message-content fetch before joining (cheaper; avoids reading private content pre-join).

Prompt sketch (JSON mode, never-throw boundary per the llm-json-output rule): `{"relevant": bool}`, parsed JSON-mode → strip-fences → `jsonrepair` → validate → **fail-closed** (`relevant=False` ⇒ do not join on any parse failure). Cost ≈ 20–40 metadata-only cheap-model calls per session (pennies); seeded channels add zero ML cost. **No `log_spend`** (isolation guard). **OPEN:** if per-org TG gate cost needs visibility, add a separate non-`log_spend` counter rather than weakening the guard.

### 7.5 Per-day join pacing (engine-tuned, panel read-only)

Joins/reactions capped per stage in `ramp.py` (§3.2 stage rows + the `telegram` `_PLATFORM_CAPS` row, `joins≤3, reacts≤5`). The platform cap min-clamps the stage value → a fresh account joins at most one channel/day.

Choreography per warming session (in the §3 executor):

```pseudocode
budget = budget_for_day(ramp_day, "telegram")
if budget.read_only or (budget.joins + budget.reacts == 0):
    return                                            # observe stage: dwell only
joined_today = store.action_counts_for_account_day(account_id, day=local_day,
                                                    action_types=["join"]).get("join", 0)
for cand in discovery_candidates():                   # §7.3 order: seeded first
    if not pacer.is_daytime(): raise HaltSession("daytime")   # re-check each iter
    if joined_today >= budget.joins: break
    if cand.is_search_hit and not relevance_gate(cand): continue   # §7.4, seeded skip
    pacer.between_reels()                             # right-skewed delay, NOT fixed
    ok = client.join_channel(cand.username)           # may raise flood errors → §8
    store.log_action(sentinel_campaign, "join", reel_id=None,
                     target=cand.username, succeeded=ok,
                     session_id=session_id, account_id=account_id)
    if ok: joined_today += 1
    dwell_read_history(cand)                           # iter_channel_messages, no write
    maybe_react(cand, budget)                          # §7.5a, probabilistic
```

**Delays MUST be right-skewed / randomized, never fixed.** Reuse `Pacer.between_reels()` (`uniform`, `pacing.py:69–72`) plus an occasional longer pause. **GAP:** `between_min/max` (2–8s) are tuned for IG reel-scrolling; TG joins should be spaced **minutes** apart. **OPEN (O-tg-join-gap):** add a TG-specific `join_gap` (~60–300s right-skewed, engine-tuned, read-only) to `PacingConfig`. **Daytime guard is forced** (`enforce_daytime=True`, `cli.py:162`) and re-checked before every join/react.

**7.5a Emoji reactions (occasional):**

```pseudocode
def maybe_react(cand, budget):
    if reacts_today >= budget.reacts: return
    if random() > REACT_PROBABILITY: return            # occasional, not every channel
    msg = pick_recent_message(cand)
    emoji = rng.choice(REACT_ALLOWLIST)                # "👍","🔥","❤"
    ok = client.react_message(cand.username, msg.id, emoji)   # ReactionInvalid → soft skip
    store.log_action(sentinel_campaign, "react", reel_id=None,
                     target=cand.username, succeeded=ok,
                     session_id=session_id, account_id=account_id)
    if ok: reacts_today += 1
```

`join`/`react` are new `action_type` values; `target` carries the channel (`reel_id=None` — `log_action.reel_id` is optional). **`react` target shape (RESOLVED, with an OPEN):** log `target=channel` and do **not** persist `message_id` (warmth counts only `join`; reacts feed the activity feed + per-day caps, keyed on `account_id+action_type+day`). If per-message react auditing is later wanted, add a nullable `note TEXT` column (additive) — do not overload `reel_id`.

### 7.6 (flood handling) — see §8.

### 7.7 Telegram warmth weight row — see §9.

---

## 6. Data model, config knobs & panel surface

### 6.1 New `action_type` values (NO schema change)

`log_action` accepts any string for `action_type` and inserts it verbatim; the table comment `-- like / follow` (`store.py:221`) is the only thing documenting the vocabulary (no `CHECK` constraint exists, and **none may be added** — a `CHECK` would convert this to a schema reshape).

| Action | Platform | `action_type` | `reel_id` | `target` |
|--------|----------|---------------|-----------|----------|
| Like | IG | `like` (exists) | reel id | `reel.reel_id` |
| Save | IG | **`save`** (new) | reel id | `reel.reel_id` |
| Share to DM | IG | **`share`** (new) | reel id | resolved DM recipient handle |
| Follow | IG | `follow` (exists) | `None` | `reel.author` |
| Join | TG | **`join`** (new) | `None` | channel `@username` |
| React | TG | **`react`** (new) | `None` | channel `@username` |

**Edit:** update the comment at `store.py:221` to `-- like / follow / save / share (IG) | join / react (TG)` (documentation-only, no `SCHEMA_VERSION` bump). `actions.account_id` is already shipped (v11 migration `store.py:659`; not in the base `CREATE TABLE`, so fresh installs get it via the migration path).

### 6.2 `share_target` campaign knob (NEW field)

Per design: target = the campaign's optional `share_target` if set, else the first contact in the IG share list (resolved live). Add to the frozen `Campaign` dataclass after `max_follows_per_session` (`config.py:265`):

```python
# config.py — append to Campaign, frozen dataclass, additive:
    # Optional DM recipient for the IG warming `share` action. None => the share
    # helper falls back to the FIRST contact in the live IG share list. Operators
    # are nudged to set this for safety.
    share_target: Optional[str] = None
```

**Threading — THREE mirrored call sites** (all currently missing it):

| Function | Location | Add |
|----------|----------|-----|
| `load_campaign()` (yaml/`.md`) | `config.py:278–327` | `share_target=_norm_share_target(knobs.get("share_target"))` |
| `campaign_from_brief()` (DB → Campaign) | `config.py:330–387` | `share_target=_norm_share_target(brief.get("share_target"))` |
| `campaign_to_brief()` (Campaign → brief) | `config.py:390–448` | `"share_target": c.share_target,` |

**Validation** — new module-level helper:

```python
import re
_IG_HANDLE_RE = re.compile(r"^@?[A-Za-z0-9._]{1,30}$")

def _norm_share_target(raw: Any) -> Optional[str]:
    """Normalize the optional IG share recipient. None/'' => default (first DM
    contact, resolved live). Strips a leading '@'. Rejects anything not a plausible
    IG handle so a typo can't silently DM the wrong account."""
    if raw is None:
        return None
    s = str(raw).strip().lstrip("@")
    if not s:
        return None
    if not _IG_HANDLE_RE.match(s):
        raise ValueError(
            f"campaign.share_target {raw!r} is not a valid Instagram handle "
            f"(letters, digits, '.', '_'; max 30 chars)")
    return s
```

### 6.3 Per-day cap counting — see §3.7 (`action_counts_for_account_day`).

### 6.4 Warmth `network` keep-alive (additive query widening)

The `network` raw signal is `actions WHERE action_type IN ('follow','connect') AND succeeded=1 AND account_id=?` (`store.py:1529–1533`). For Telegram, widen the IN-set to add `'join'`:

```sql
AND action_type IN ('follow','connect','join')   -- additive, one-line
```

Safe and non-cross-contaminating: the query is `account_id`-scoped and an account is single-platform (`accounts.platform`), so a TG account has zero `follow`/`connect` rows and an IG account has zero `join` rows. `save`/`share`/`react` are **deliberately excluded** — they are dwell/engagement signals, not network-graph growth.

### 6.5 Panel surface (READ-ONLY caps + editable `share_target` hint)

1. **Engine-tuned caps are READ-ONLY (safety invariant).** Caps live in `ramp.py` `_STAGES`/`_PLATFORM_CAPS`/`budget_for_day()`. The panel **displays** the current stage + per-day budget (e.g. *"Stage: ramp (day 8–14) · today: likes 4, follows 1, saves 2, shares 1, joins 1, reacts 2"*) as **static text, not form inputs.** No code path may POST a cap override. **OPEN (eng):** add a read-only `{stage, budget:{...}, readOnly:true}` projection of `budget_for_day()` to the warming/settings read response. No write endpoint.
2. **`share_target` IS operator-editable, with a safety hint.** Rides the existing campaign create/edit form over `campaign_meta`, round-trips via `campaign_to_brief` ↔ `campaign_from_brief` (§6.2). Field: label **"Share-to-DM recipient (optional)"**, single text input, placeholder `@username`. Hint copy: *"If set, warming shares reels only to this account. Leave blank and the agent shares to your first DM contact — set a dedicated recipient for safety."* Client mirrors `_IG_HANDLE_RE`; the server `_norm_share_target` remains the authoritative fail-fast boundary.

### 6.6 Change classification summary

| Surface | File / symbol | Change | Type |
|---------|---------------|--------|------|
| `actions.action_type` vocab (`save`/`share`/`join`/`react`) | `store.py:221` comment | comment-only edit | additive, no version bump |
| `actions.account_id` | already shipped (v11 `store.py:659`) | none | done |
| `action_counts_for_account_day()` | `store.py` (~:1238, new) | new read method | additive, no DDL |
| `Campaign.share_target` | `config.py:265` | new frozen-dataclass field | additive |
| `_norm_share_target()` + 3 call sites | `config.py:278–448` | new helper + threading | additive |
| `ActionBudget` P1 fields | `ramp.py:14–21` | extend dataclass + `_STAGES`/`_PLATFORM_CAPS` | additive |
| `network` query: add `join` | `warmth.py`/`store.py:1532` | one-line IN() extension | additive |
| Panel read-only cap projection | warming/settings read endpoint | new read field | additive |
| Panel `share_target` field + hint | campaign create/edit form | text input + client validation | additive |

---

## 9. Warmth-model impact (network sourcing + Telegram weight row)

> **Invariant from P0:** warmth is **computed-on-read**, never a column (§C3). This section persists **no new score state** — every change targets either insert-only `actions`/`health_flags` rows the executor writes, or the pure derivation in `aizu/core/warmth.py`. The single producer stays `store.warmth_for_campaign(...)` → `warmth.compute(...)` (`store.py:1559`, `warmth.py:182`).

### 9.1 IG `network` component — source preparation

`network = min(1.0, network_successes / TARGET_CONNECTS)` (`TARGET_CONNECTS=20`, `warmth.py:18`), fed by the `IN ('follow','connect')` query. When the IG executor logs a successful `follow` — `log_action(sentinel_campaign, 'follow', reel_id=None, target=<author>, succeeded=True, account_id=<id>, session_id=<sid>)` — this query counts it. `like`/`save`/`share` do **not** feed `network` (by design; they raise `ramp`/`trust` indirectly via completed-session consistency).

### 9.2 TG sources `network` via the query widening (P1)

TG has no `follow`; its keep-alive write is **`join`**. Widen the network query to `IN ('follow','connect','join')` (§6.4). The `TARGET_CONNECTS=20` divisor is almost certainly too high for rate-limited TG joins — **OPEN (O-tg-network-divisor):** add per-platform `TARGET_NETWORK` (TG ≈ 8, IG keeps 20), a one-line tunable in `warmth.compute`.

### 9.3 NEW Telegram weight row (X5 prerequisite)

Telegram is NOT warmable today (`WARMABLE_PLATFORMS = {x, linkedin, instagram}`, `accounts.py:50`) and `weights_for()` funnels it to neutral. To promote it: **(a)** add `"telegram"` to `WARMABLE_PLATFORMS`; **(b)** add an explicit `PLATFORM_WEIGHTS["telegram"]` row — it must **not** fall through to `_default` (Instagram-shaped). The five components map onto TG explicitly (none silently zeroed):

| Component | TG applicability | Handling |
|-----------|------------------|----------|
| `age` | Applies (StringSession age) | keep, weighted |
| `ramp` | Applies (completed TG warming sessions in 14d) | keep, weighted highest — the cleanest, lowest-fingerprint TG signal |
| `network` | Repurposed → channels-joined (§9.2) | keep, weighted lower than IG |
| `profile` | Partial (binary connected/auth via `TelethonClient.connected()`, `feed.py:282`) | keep at a **small** weight |
| `trust` | Applies (TG raises flood flags, §8) | keep, weighted |

**OPEN (O-tg-weights)** — proposed defensible starting rows (each sums to 100):

```python
PLATFORM_WEIGHTS = {
    "x":        {"age": 15, "ramp": 30, "network": 20, "profile": 10, "trust": 25},
    "linkedin": {"age": 20, "ramp": 20, "network": 30, "profile": 20, "trust": 10},
    "telegram": {"age": 25, "ramp": 35, "network": 15, "profile": 5,  "trust": 20},  # P1.3
    "_default": {"age": 20, "ramp": 25, "network": 20, "profile": 15, "trust": 20},  # Instagram
}
```

Add a unit test asserting every row sums to 100. Promoting TG to warmable also requires the TG warming executor (X5) to exist and emit `join` actions; campaigns without one stay on `neutral_default()` (score 50, never blocking).

### 9.4 Write-era challenge kinds → penalty

P0 raises only read-era kinds. The two write-era failure surfaces must be classified. **Per X3, two routes are available:** map to existing kinds, or add new ones. If new kinds are added (additive frozenset extension only):

```python
WARMING_CHALLENGE_KINDS = frozenset({
    "checkpoint","empty_interception","account_challenge","arkose",
    "rate_limit","login_drift","session_expired",
    "action_block","peer_flood"})            # P1+: additive
_CHALLENGE_KINDS = frozenset({"checkpoint","empty_interception",
                              "account_challenge","arkose","action_block"})
_RATE_KINDS = frozenset({"rate_limit","peer_flood"})
```

| Kind | Raised by | Bucket | Factor | Window |
|------|-----------|--------|--------|--------|
| `action_block` (or `account_challenge`) | IG executor on `detect_action_block()` | `_CHALLENGE_KINDS` | `PENALTY_CHALLENGE_FRESH=0.10` → decays 0.35 | 24h/72h |
| `peer_flood` (or `rate_limit`) | TG executor on flood errors | `_RATE_KINDS` | `PENALTY_RATE_LIMITED=0.50`, `throttled=True` | 6h |

Routing rationale: action-block is a durable "you're acting too much" signal → crater to 10%, NOT `throttled`. Flood is transient/timed → 50% penalty + `throttled=True` ("back off and retry"). `compute_penalty` already filters on `kind IN WARMING_CHALLENGE_KINDS`, so adding the kinds is the complete derivation change — no query rewrite, no schema change. New flags simultaneously (a) reduce `trust` (open warming-challenge flags in 14d) and (b) apply the multiplicative penalty (the same double-count-by-design P0 has for checkpoints).

### 9.5 What does NOT change
- No persisted score / no new column. `WarmthScore` / `WarmthInputs` shapes unchanged; the only new data is more `actions`/`health_flags` rows + more `action_type`/`kind` values.
- `neutral_default()` still protects non-warmable platforms (score 50, `meets_gate=True`).
- Each weight row sums to 100 (enforced by a test).

---

## 8. Safety, anti-detection, kill-switch

> Nothing here loosens a P0 invariant; P1 *adds* per-action gating on top of the shipped dwell-only safety floor.

### 8.1 Anti-detection model

The threat is **automation fingerprinting** — fixed cadence, constant probability, or off-hours activity. Three properties defeat it:

**(a) Randomized, right-skewed delays — NEVER a fixed constant.** Reuse `Pacer.dwell()` (`uniform(3.0,30.0)`, `pacing.py:64–67`) and `Pacer.between_reels()` (`uniform(2.0,8.0)`, `pacing.py:69–72`), plus the executor's `_human_delay` right-skew (§3.6). **OPEN (O-AD1, RESOLVED approach):** the `between_reels` flat `uniform` is hardened with an optional `occasional_long_pause` roll, gated by a `PacingConfig` field defaulting OFF so harvest pacing is byte-unchanged. The flat baseline already beats a constant; the skew is a hardening follow-up — product owner confirms the long-pause probability/range.

**(b) Engine-tuned per-day caps — never operator-crankable (SAFETY INVARIANT).** Caps in `ramp.py` (`_STAGES`/`_PLATFORM_CAPS`/`budget_for_day`), keyed by stage and clamped by per-platform ceilings. Panel READ-ONLY. No write endpoint, brief key, or campaign knob raises a cap; `max_*_per_session` only *tightens* (min-clamp). `observe` always returns `read_only=True`, never widened → days 0–3 make ZERO writes.

**(c) Forced daytime guard.** `_warming_pacer()` (`cli.py:146–162`) constructs the Pacer with explicit `enforce_daytime=True`; `_enforce_daytime_default()` (`pacing.py:16–26`) makes an explicit value ALWAYS beat `AIZU_IGNORE_DAYTIME`, so a live-run off-hours opt-out CANNOT weaken warming. Clock localized to `account.fingerprint.timezone_id`. P0 checks `is_daytime()` once at session start (`session.py:134`); **P1 re-evaluates per dwell window** (O-mid-session-daytime) so a session crossing the boundary stops writing mid-run.

### 8.2 Kill-switch + daytime enforcement (per-window re-check)

P1 adds a pre-window guard at the top of each dwell window:

```python
reason = warming_kill_reason(store, org_id=org_id, platform=platform)   # warming_control.py:30
if reason:
    raise HaltSession(reason, kind="kill_switch")        # clean stop, no penalty flag
if not pacer.is_daytime():
    raise HaltSession("outside daytime window", kind="daytime")
```

`warming_kill_reason()` is contractually cheap (pure settings/flag queries, no Chrome/network, `warming_control.py:13–14`), so per-window re-checks cost nothing. Layers:

| Layer | Source | Gate |
|-------|--------|------|
| L1 env hard-stop | `AIZU_WARMING_ENABLED` (default OFF) | `warming_control.py:25–27` |
| L2 per-org switch | `settings.warmingEnabled is False` | `warming_control.py:42` |
| L2b per-platform | `platform in settings.warmingDisabledPlatforms` | `warming_control.py:44` |
| L3 circuit-breaker | auto-trip on N consecutive flags | **deferred (O7)** |

### 8.3 Action-block / flood → FLAGGED state

**Instagram** — `feed.detect_action_block()` after every write attempt → `raise_flag(account_id=..., kind="account_challenge")` + `HaltSession`. (§4.4)

**Telegram** — both flood errors caught **at the executor**, never swallowed:

```pseudocode
try:
    ok = client.join_channel(cand.username)
except FloodWaitError as e:          # global API rate limit; e.seconds backoff
    store.raise_flag(account_id=account_id, kind="rate_limit",
                     detail={"seconds": e.seconds, "op": "join"})
    raise HaltSession("rate_limit", kind="rate_limit")
except PeerFloodError:               # per-account spam-block
    store.raise_flag(account_id=account_id, kind="rate_limit",
                     detail={"op": "join", "peer_flood": True})
    raise HaltSession("rate_limit", kind="rate_limit")
except UserAlreadyParticipantError:  # idempotent no-op; no budget spend
    ok = True
```

| Error | Meaning | Action |
|-------|---------|--------|
| `FloodWaitError` | global API rate limit | `raise_flag(kind="rate_limit")` → HaltSession |
| `PeerFloodError` | per-account spam-block | `raise_flag(kind="rate_limit")` → HaltSession |
| `UserAlreadyParticipantError` | already joined | success no-op, don't spend budget |
| `ReactionInvalidError` | reaction disallowed | soft skip (`succeeded=False`), continue |

`kind="rate_limit"` is in `WARMING_CHALLENGE_KINDS`; `compute_penalty` craters via `PENALTY_RATE_LIMITED=0.50` + `throttled=True` (6h `RATE_LIMIT_WINDOW`). The flag is keyed on `account_id`, never the sentinel campaign — the penalty follows the account. Any flood signal ends the **whole** session, not just the current join. Combined with `joins≤1/day` at light/ramp, a fresh account should never trip flood normally; a trip means caps/pacing need widening downward.

---

## 10. Phased rollout, acceptance criteria, test plan & KPIs

### 10.1 Phased rollout (Instagram first)

Rollback at any phase is instant and non-destructive: clear `AIZU_WARMING_ENABLED` (L1, whole host) or set `warmingEnabled:false` / add the platform to `warmingDisabledPlatforms` (L2/L2b, per-org). No data migration, no code deploy.

| Phase | Scope | Entry gate | Exit gate |
|-------|-------|-----------|-----------|
| **P0 (shipped)** | Dwell-only; zero writes, zero flags, daytime enforced at session start. | live | — |
| **P1.0 IG dwell+like** | One allowlisted internal org; IG only; `like` + always-on dwell (follow/save/share probabilities pinned 0 via stage data). `AIZU_WARMING_ENABLED=1` on one host. Executor implemented + wired. | Isolation guard green; per-day cap test green; P0 stable in prod. | ≥7 days, 0 `account_challenge` flags, warmth `network` not regressing. |
| **P1.1 IG follow+save** | Same org; enable `follow` (tiny cap) + `save`. `share` still OFF. | P1.0 met; action-block halt verified live. | ≥14 days clean across ≥2 accounts; flag rate within KPI. |
| **P1.2 IG share + broaden** | Enable `share` behind explicit per-campaign opt-in; add real customer orgs via per-org `warmingEnabled` overlay (no host env change). | P1.1 met; panel read-only caps verified; DM-block regex landed. | Stable for the broadened set. |
| **P1.3 Telegram** | Add `"telegram"` to `WARMABLE_PLATFORMS` + weight row; build TG executor + 3 TelethonClient write methods; relevance gate live. | All IG phases stable; O-tg-weights + O-tg-join-gap signed off. | ≥14 days, 0 `peer_flood`, joins within cap. |

### 10.2 Acceptance criteria

| # | Criterion | Verification |
|---|-----------|--------------|
| A1 | Isolation guard flipped: warming MAY call `log_action`, only from the executor; harvest must NOT. | Replace `test_..._logs_no_engagement_actions_in_p0` with `test_log_action_only_in_action_executor` (grep: `log_action(` only in `warming/action_executor.py`); harvest read-only test unchanged + still green. |
| A2 | Per-day caps honored, never exceeded; robust across same-day sessions. | `budget.likes=2` ⇒ ≤2 successful `like` rows that account/day; a second same-day session sees the first's rows; bumping ramp day resets the bucket. |
| A3 | `observe` stage (days 0–3) makes ZERO writes. | `budget_for_day(2,"instagram").read_only is True`; `maybe_act` no-ops when `read_only`. |
| A4 | Caps NOT operator-crankable. | No write endpoint/brief key raises a `_PLATFORM_CAPS`/`_STAGES` value; brief with `max_likes_per_session=999` still clamps to the stage cap. |
| A5 | Per-window kill-switch + daytime re-check. | Inject a clock past `daytime_end` mid-session → halt `kind="daytime"`, no further writes; flip `warmingEnabled:false` mid-session → halt `kind="kill_switch"`. |
| A6 | `share_target` resolution. | `campaign.share_target` set → share targets that handle; unset → FIRST DM contact; malformed handle rejected by `_norm_share_target`. |
| A7 | IG action-block halts + flags. | Stub `detect_action_block()→True` → `HaltSession(kind="account_challenge")`, `raise_flag(account_id=...)`, warmth penalty ≤0.10. |
| A8 | Delays randomized + right-skewed. | Seeded RNG ⇒ delay variance > 0 and an occasional long pause observed; no literal `time.sleep(constant)` in the executor module. |
| A9 | All warming writes stamped `account_id` + sentinel. | Every `log_action` from the executor uses `sentinel_campaign` + `account_id`; harvest rows carry `account_id=None`. |
| A10 | Forced daytime cannot be weakened. | With `AIZU_IGNORE_DAYTIME=1`, `_warming_pacer()` still yields `enforce_daytime=True`. |
| A11 | Warmth `network` stays live. | Each successful IG `follow` increments `network` on the next read; `like`/`save`/`share` do not. |
| A12 (TG, P1.3) | TG flood handling. | Stub `join_channel` raising `FloodWaitError(seconds=N)` → halt `kind="rate_limit"`, flag with `account_id`, `compute_penalty` returns `throttled=True`; `PeerFloodError` likewise. |
| A13 (TG, P1.3) | TG relevance-gate scope. | Search hits invoke the gate; seeded channels never do; gate does NOT route through `router.score`/`gate_post`/`upsert_match`. |
| A14 (TG, P1.3) | TG join cap. | `budget.joins=1` ⇒ at most one `join` logged per TZ-day; widened network query counts `join`. |

### 10.3 Test plan (TDD; extends `tests/test_warming_isolation.py` + new files)

- **Isolation (updated):** `test_log_action_only_in_action_executor`; keep `upsert_match`/`log_spend`/`router.score`/`add_to_watchlist` call-form guards; add an allow-note for the gate path.
- **Caps:** `test_warming_respects_per_day_caps`, `test_observe_stage_logs_no_actions`, `test_caps_not_operator_crankable`.
- **Delays:** `test_delays_are_randomized_and_right_skewed`.
- **Stamping:** `test_warming_actions_stamped_account_and_sentinel`.
- **IG halt:** `test_ig_action_block_halts_and_flags`.
- **TG (P1.3):** `test_warming_telegram_relevance_gate_is_cheap_text_only`, `test_warming_telegram_join_logs_action_with_account_id`, `test_warming_telegram_respects_join_cap`, `test_warming_telegram_halts_on_flood`, `test_warming_telegram_seeded_channels_skip_gate`.
- **Warmth:** assert every `PLATFORM_WEIGHTS` row sums to 100; assert penalty routing for `action_block`/`peer_flood` when added.
- Target ≥80% coverage on the new executor module; AAA structure; fix implementation, not tests.

### 10.4 KPIs

**Safety (leading, must stay green):**
- `account_challenge` / `peer_flood` flags per warmed account per week — target **0**, alert at ≥1.
- `rate_limit` (FloodWait) events per TG account per day — target **0**, tolerate transient; trend, don't crash.
- Off-hours write count — must be **0** (proves the daytime per-window re-check).
- Per-day cap breaches — must be **0** (proves A2/A4).
- Consecutive-flag accounts auto-cooled (`COOLING`/`FLAGGED`) within one session of the flag.

**Success (lagging, the point of warming):**
- Warmth `network` trend per account (IG follows / TG joins) rising over the ramp; score crossing the harvest-readiness floor.
- Distinct-warming-days-in-14 (`ramp` consistency) ≥ target per stage.
- Clean-graduation rate: accounts reaching `READY` without a single challenge flag.
- Zero harvest regressions: harvest sessions byte-identical (warming early-routes before `select_engine()`, `dispatch.py:187–192`); isolation guard proves harvest never writes engagement.
