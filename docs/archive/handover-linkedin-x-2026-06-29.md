# Handover — LinkedIn & X engines

**Date:** 2026-06-29 · **Branch/worktree:** `engine/` (no git in this checkout)
**Plan of record:** `~/.claude/plans/let-us-plan-x-graceful-spark.md`
**Reference PRDs:** `docs/prd/linkedin-lead-agent-PRD.md`, `docs/prd/x-lead-agent-PRD.md`
**Architecture overview:** `docs/architecture/engines.md` (updated; read §0, §5, §6 first)

This is a handover for follow-on agents. The two engines are **code-complete, unit-tested,
and dry-run-verified**; what remains is **live validation, front-end exposure, and a few
refinements**. Read this top-to-bottom before touching anything.

---

## 1. What is DONE (do not redo)

All landed and green: **engine suite 580 passed**, **panel suite 294 passed**, both engines
dry-run clean.

- **Shared CDP harness** — `engine/aizu/core/cdp.py` (`CDPFeedBase` + `CDPBaseConfig`):
  attach over CDP, the interception template (`_on_response` → hint-filter → JSON-guard → set
  canary → delegate to `_classify`), seed-source `walk()`, scroll, screenshots, canary.
  Instagram was migrated onto it (`engines/instagram/cdp.py`) with **zero behavior change**
  (its tests are the guard — keep them green).
- **LinkedIn engine** — `engine/aizu/engines/linkedin/` (`parsers.py`, `cdp.py`,
  `cascade.py`, `prompts.py`, `session.py`). Managed-CDP, copy-first, vision when copy thin,
  single comment surface, read-only.
- **X engine** — `engine/aizu/engines/x/` (same file set). Managed-CDP, text-first, **two
  match surfaces** (replies + quote-posts) merged behind one `fetch_comments` via a composite
  cursor `"<replyCount>|<quoteCount>"`; each match tagged `extracted["surface"]` = `reply|quote`;
  **read-budget soft-cap** stops before X's hard daily lockout.
- **Wiring** — `"linkedin"`, `"x"` added to `core/config.py::SUPPORTED_PLATFORMS`; `build_feed`
  + `select_engine` branches in `dispatch.py`. **No secret, no schema change** — both are
  managed-CDP, so they ride the existing `credentials=None` path like Instagram.
- **Panel** — both fall through `IntegrationsPanel.tsx`'s `switch` to the existing
  `ManagedCard` (correct). A test asserts this: `admin-panel/src/features/interactions.test.tsx`
  → "X and LinkedIn are shown as managed".
- **Docs** — `docs/architecture/engines.md` (two new sections + 6-way table).

**Conventions to follow** (so you match the codebase): each engine is self-contained and shares
only `core/`; per-engine scoring (don't generalize the cascades); pure parsers in
`engines/<p>/parsers.py` are the only CI-testable part of the CDP layer; detection is **by
response shape, not URL** (URL hints are a pre-filter); read-only (engagement methods are no-ops);
halts raise `HaltSession`, folded into `halt_reason` by `run_session`.

**Test commands:**
```
cd engine && .venv/bin/python -m pytest -q                 # full engine suite
cd engine && .venv/bin/python -m pytest tests/engines/linkedin tests/engines/x tests/core -q
cd admin-panel && npx vitest run                            # full panel suite
```

---

## 2. REMAINING WORK

### A. Engine — live endpoint capture & DOM confirmation  *(blocker for production; needs warmed accounts)*

This is the deferred operational task. The parsers are shape-based and the loop is proven on
fixtures; what is **not** verified is that the live selectors/URLs actually fire interception.

1. **Warm the accounts** (manual, weeks — see PRDs §10). LinkedIn: a complete, credible profile.
   X: ideally a **verified** account (≈10k/day read ceiling vs ≈1k unverified). Log both into the
   single warmed Chrome the engine attaches to (`AIZU_CDP_URL`, default `:9222` — note the
   live-run port gotcha recorded in project memory).
2. **Capture current endpoints in DevTools**, drop as fixtures, and confirm the parsers:
   - **X** — `HomeTimeline` / `SearchTimeline` / `ListLatestTimeline` / `TweetDetail` / the
     **Quotes** timeline. Capture real response bodies → add as fixtures under
     `tests/engines/x/` and assert `parse_posts` / `parse_replies` / `parse_quotes` extract them.
     X rotates `doc_id`s every ~2–4 weeks — the **empty-interception canary** must trip on drift
     (it's wired; confirm it fires).
   - **LinkedIn** — the Voyager feed + comments calls. Same: capture → fixture → assert
     `parse_posts` / `parse_comments`.
   - If the live shape differs from the representative shape the parsers assume, **adjust the
     parser helpers** (`_full_text`/`_screen_name`/`_tweet_nodes` for X; `_text`/`_actor_name`/
     `_commenter_name`/`_urn` for LinkedIn) — keep them tolerant, keep the fixtures.
3. **Confirm the DOM selectors** (the only live-only code, all flagged in comments as
   "DevTools-confirmation point"):
   - LinkedIn `engines/linkedin/cdp.py`: `_open_comment_thread`, `_load_more_comments`,
     `_page_unavailable`, the `_sources()` URL templates (`FEED_URL`/`HASHTAG_URL`/`ACTIVITY_URL`),
     and `POST_PERMALINK` (`/feed/update/{urn}/`).
   - X `engines/x/cdp.py`: `_page_unavailable`, `_load_replies`/`_load_quotes` scrolling, the
     `_sources()` URLs (For You / Search / profile), `STATUS_PERMALINK`, `QUOTES_URL`.
4. **Run one live session per platform** and validate against a hand-labeled set (PRD "v1 done"
   criteria): account survives, sessions resume cleanly, matches land with good precision, the
   tired-feed flag fires, the canary halts on drift, X's read-budget soft-flag stops before any
   hard lockout, and X captures **both** replies and quote-posts.

**Acceptance:** a live session on each platform produces correct matches with the canary and
(for X) read-budget behaving; new fixtures committed.

### B. Front-end — expose the two platforms in the campaign UI  *(no live account needed)*

The create/edit campaign form does **not** offer the new platforms. Files:
`admin-panel/src/features/campaigns/useCampaignForm.ts`.

1. **Add to the platform picker** — `PLATFORMS` (line ~10) is `['instagram','youtube','telegram']`.
   Add `'linkedin'` and `'x'`. *(Note: `'reddit'` is also missing here — a pre-existing gap from
   the Reddit engine; add it too while you're in this file.)*
2. **Add seed configs** — `PLATFORM_SEEDS` (line ~31). Add entries so the right seed fields show
   with platform-appropriate labels. Both are managed-CDP with an algorithmic home feed, so seeds
   are **optional** (no `requireAnyOf`, like Instagram):
   ```ts
   linkedin: { fields: [
     { key: 'seedHashtags', label: 'Seed hashtags', placeholder: 'saas, productivity' },
     { key: 'seedAccounts', label: 'People / companies', placeholder: 'in/jane-doe, company/acme' },
   ] },
   x: { fields: [
     { key: 'seedHashtags', label: 'Saved searches / hashtags', placeholder: 'project management tools' },
     { key: 'seedAccounts', label: 'Accounts / List members', placeholder: '@acme, @devuz' },
   ] },
   ```
   Seed→engine mapping (already implemented engine-side, match the labels to it): `seed_accounts`
   = LinkedIn people/company slugs · X @handles/List members; `seed_hashtags` = LinkedIn hashtags ·
   X saved searches; `include_home_feed` = "walk the home / For You feed" (seed-aware default).
3. **Verify the platform select control** renders the new options (whatever component reads
   `PLATFORMS` — check `CampaignForm`/new-campaign wizard). Add/extend a test mirroring the
   existing `interactions.test.tsx` "submitting the new-campaign form" cases for `platform: 'x'`
   and `platform: 'linkedin'` (assert the brief travels with the right platform + seeds).
4. **PlatformChip** (`admin-panel/src/features/leads/PlatformChip.tsx`) already colors `x`/
   `linkedin` (palette in `shared/ui/charts/chartColors.ts`). It renders the raw platform string
   `capitalize`d → `x`→"X" (fine), `linkedin`→"Linkedin" (slightly off). **Optional polish:** add a
   small display-label map (`x`→"X", `linkedin`→"LinkedIn") so casing is correct everywhere chips
   render (LeadsTable, LeadDrawer, LeadCard, TopCampaignsTile).

**Acceptance:** a user can create an `x` and a `linkedin` campaign from the panel with the right
seed fields; leads show a correctly-labeled chip; panel suite green.

### C. Integration / operations

1. **(Optional) Surface managed rows in Settings.** Today the server only emits `integration`
   rows for the secret-backed platforms, so X/LinkedIn won't appear in Settings → Integrations
   until they have a managed campaign/lead. If product wants them always visible as "Managed"
   (like Instagram), have the server's integration derivation emit managed rows for `x`/`linkedin`
   (`engine/aizu/server.py`, wherever the Instagram managed row is derived). The panel already
   renders them via `ManagedCard` — no front-end change needed. **Do NOT** add connect cards /
   `connectX`/`connectLinkedIn` repository methods / `integration_secrets` — these are managed-CDP.
2. **Warming runbook.** Document the warm-Chrome setup for x.com and linkedin.com (extend
   whatever `warm_chrome.sh` / ops doc Instagram uses): one warmed, logged-in Chrome; daytime-only;
   conservative ramp; never solve Arkose/checkpoints (the engine halts and alerts).
3. **Run flow is already correct** — `cli._SINGLE_PASS_PLATFORMS` excludes both, so they loop
   back-to-back toward `--target-leads` like Instagram (their feeds are algorithmic). Nothing to do.

### D. Refinements (post-v1, nice-to-have — all flagged in code/PRD)

- **X per-source tired-feed** — PRD §7 wants For You / Search / Lists tracked separately; the
  session currently keeps a single `feed_health_flag`. Needs the feed to signal the active source
  to the session.
- **X read-budget across sessions** — currently a per-session view cap (`SessionConfig.
  read_view_soft_cap`). The true daily ceiling spans sessions → store-backed daily aggregate.
- **X quote enumeration** — `_load_quotes` runs on every relevant post; PRD §5/§12 suggests
  walking quotes only on watchlisted match-rich posts to cut read volume.
- **LinkedIn reply expansion** — v1 reads top-level comments; expand replies only on matching
  comments (PRD §11).

---

## 3. Key file map

| Area | Path |
|---|---|
| Shared CDP base | `engine/aizu/core/cdp.py` |
| LinkedIn engine | `engine/aizu/engines/linkedin/{parsers,cdp,cascade,prompts,session}.py` |
| X engine | `engine/aizu/engines/x/{parsers,cdp,cascade,prompts,session}.py` |
| Dispatch wiring | `engine/aizu/dispatch.py` (`build_feed`, `select_engine`) |
| Platform list (engine) | `engine/aizu/core/config.py::SUPPORTED_PLATFORMS` |
| Engine tests | `engine/tests/engines/{linkedin,x}/`, `engine/tests/core/test_cdp.py` |
| Campaign form (front-end) | `admin-panel/src/features/campaigns/useCampaignForm.ts` |
| Platform chip | `admin-panel/src/features/leads/PlatformChip.tsx` |
| Managed-card test | `admin-panel/src/features/interactions.test.tsx` |
| Architecture doc | `docs/architecture/engines.md` |

**Local quirks:** Python via `engine/.venv/bin/python` (system `python3` has no pytest). CLI `--db`
is a top-level arg (before the `run` subcommand). Dry-run a platform with a temp config dir
(`soul.md` + `campaign.md` with `platform: <p>`): `python -m aizu.cli --db /tmp/x.db run
--config <dir> --dry-run`.
```
```
