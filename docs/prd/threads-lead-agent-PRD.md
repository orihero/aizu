# Threads Post-Reply Discovery Agent — PRD (v1 & v2)

**Status:** Draft v0.2 · **Date:** 2026-06-19 · *product name TBD*

> Generic engine. It knows no vertical and no goal — all domain meaning lives in the campaign brief. The flagship first campaign is lead-gen (e.g. a SaaS product), but that is one brief, not a built-in assumption.

> **Sibling spec.** This is one of a family of per-platform agents (Instagram, YouTube, Facebook, LinkedIn, X, **Threads**, Reddit, TikTok, Quora, Pinterest) that share an identical engine and contract. A future **campaign orchestrator** lets an operator author one `campaign.md`, fan it out to the platform engines they pick, and pool every match into a single leads dashboard over the shared SQLite contract. **X, Threads, Reddit, and Quora are being authored together and implemented as one combined phase** — they are the text-first surfaces, share the most cascade behavior, and reuse one set of seed knobs — but each ships its own FeedSource. This PRD is self-contained; the orchestrator is a separate doc.

> **Why Threads differs.** Threads is the only platform in the family with a usable **but deliberately narrow** official API. Meta's Threads Graph API (`graph.threads.net`, as of 2026-06; verify before build) is built for publishing-your-own-posts, own-account insights, mentions, and a public `keyword_search` — but the API has **no** For-You/Following feed read and **no** general "read any user's thread + its full reply tree" firehose. Two facts shape how much weight `keyword_search` can carry: (1) public `keyword_search` requires **Meta App Review approval of the `threads_keyword_search` permission** (plus `threads_basic`); without that approval `keyword_search` does **not** error — it silently restricts results to the **authenticated account's own posts** (as of 2026-06; verify before build). (2) Where approved, `keyword_search` is **generous, not scarce**: the documented limit is **2,200 requests per rolling 24-hour period, per user** (official docs, developers.facebook.com/docs/threads/keyword-search, as of 2026-06; verify before build), and it supports `search_type=TOP|RECENT` plus optional `since`/`until` Unix-timestamp date ranges back to the earliest allowable date **1688540400 (2023-07-05)** — so it can target arbitrary historical windows, not just fresh posts. So unlike YouTube and Telegram, the API still cannot carry **open** discovery on its own (no feed read, no third-party reply firehose). Threads therefore runs a **hybrid split**: keyword/tag-seeded, deterministic pulls go through the official API *where the `threads_keyword_search` permission is approved*, but **primary broad discovery** (For You, Following, deep reply trees) is **CDP scraping of the warmed Chrome's own internal GraphQL traffic** — exactly like Instagram, and on the **same managed, Instagram-linked identity** (the warmed Threads account *is* an Instagram account, so enforcement blast-radius spans both). And Threads' web client is the **youngest and most endpoint-drift-prone** of the four text platforms (X, Threads, Reddit, Quora), so the empty-interception canary is **load-bearing** here, not a nicety.

---

## 1. Summary
A local-first agent that discovers relevant **public Threads posts** through the **For You** and **Following** feeds plus **Search**, examines **every public post type — text-only posts, image posts, and video posts (all first-class)** — reads the **post's text body first** and only then, for image/video posts, the **on-screen text of any attached media (a secondary, optional vision pass)**, then reads the **replies beneath each post, including nested reply trees**, and scores/extracts every reply against a **campaign-defined brief**, surfacing matches in an admin panel for human follow-up. Threads is **text-first**: a text-only post is fully first-class and is examined on its text alone — the vision/OCR pass is skipped entirely and `on_screen_frames` is simply empty for it. The engine is domain- and goal-agnostic and must run against a brief it has never seen with zero code change. Runs on the operator's own Mac in short daytime bursts — not on a server, not 24/7.

*"Reel" appears in this document only as the name of the engine's internal dataclass (inherited from the Instagram origin); on Threads the parent unit is **a post of any type**, not a video.*

*A **match** = any reply scoring above threshold against the active brief. For the lead-gen campaign, a match is a lead.*

**Two converging access paths (the hybrid split):**
- **CDP path (primary, ships in v1):** Playwright attaches over CDP to the operator's warmed, logged-in Chrome and reads the Threads web client's **own internal GraphQL traffic by network interception** (never crafted API calls, never DOM scraping) — identical mechanism to the live Instagram feed. This is the only path that can walk For You + Following + Search and follow arbitrary reply trees. Works day one, no App Review.
- **API path (secondary, optional accelerant):** the official Threads Graph API (`graph.threads.net`) serves the slice it genuinely covers — **keyword/tag-seeded discovery via `keyword_search`** — deterministically and ToS-clean, *where the operator has obtained App-Review approval of the `threads_keyword_search` permission*. **Reply reading on the API is OWN-media-oriented** (Meta's reply endpoints are documented around the authenticated user's own media); reading the reply/conversation tree of arbitrary **third-party** public posts via the official API is **unconfirmed and likely CDP-only** (as of 2026-06; verify before build) — treat any "API replies on reachable posts" as optimistic until empirically confirmed. The two paths converge on the same `FeedSource` so the cascade and store never see the difference.

## 2. Goals / Non-Goals

**Goals**
- Surface brief-matching replies (incl. nested replies) from public posts of any type, where relevance, goal type, what-to-extract, and threshold all live in `campaign.md`.
- Survive on Threads — behave like a real human account, never trip automation enforcement — **and protect the linked Instagram account** (shared blast-radius).
- Use the official API where it cleanly covers a need (keyword/tag discovery; own-media reply reading where applicable); use CDP for everything the API cannot reach (For You/Following feeds, deep third-party reply trees, discovery with no API access).
- Keep heavy data local; use cloud only as a targeted escalation when the local model is unsure.
- Operator-controllable and observable through an admin panel.

**Non-Goals (both versions)**
- The bot never likes, reposts, follows, replies, quote-posts, or DMs. Dwell + scroll (CDP) / read-only fetch (API) only.
- Never solves Meta's enforcement challenges — checkpoint / captcha / login challenge / 2FA / account-restriction screen — it halts and alerts a human.
- No multi-account farming, no cold mass-DM outreach.
- No reading posts from **private** accounts (the API excludes private-account mentions; the CDP path reads only what the warmed account can already see).
- No reselling collected data (separate product direction, out of scope).
- The engine hardcodes no vertical and no goal.

## 3. Core principles (locked)
- **Generic engine** — knows no vertical, no goal; runnable against an unseen brief with zero code change.
- **Local execution** on the operator's machine (M4 Max, 36GB), bursty daytime runs.
- **Hybrid access, CDP-primary** — broad open discovery is **real Chrome over CDP** (attach to a warmed, logged-in profile; never launch a vanilla automation browser); the official API is a deterministic secondary slice used only where it genuinely covers the need and the `threads_keyword_search` permission is approved.
- **Network interception, not DOM scraping** (CDP path) — read the page's own internal GraphQL traffic; never craft API calls. **Sanctioned token-authed calls** (API path) — never scrape what the API serves.
- **Read-only collection** — passive viewing only; the only CDP "actions" are human-like dwell and scroll; the API path issues only read endpoints.
- **Feed steering is manual + recurring** — the operator seeds the feed by hand (following the right accounts, leaning on the **linked Instagram graph**, searching topics/tags) during warming and re-nudges it periodically as it drifts (see §10). NO Lists (unlike X), NO channels/subreddits.
- **Escalate only when unsure** — both relevance and match decisions run cheap-local first and ask the cloud model only on low-confidence cases.
- **SQLite is the only contract** between engine and panel (WAL mode; two processes meet at the DB).
- **soul.md + campaign.md** — config read only at session start.
- **Generic criteria & goal, fixed core record** — relevance/goal/extraction are free-text brief; every match lands in identical core columns plus a brief-defined `extracted` blob.

## 4. soul.md vs campaign.md (the generalization boundary)
- **soul.md** — engine identity + safety: read-only, pacing, halt-on-resistance, never act. Domain-free.
- **campaign.md** — warming/seed direction (which accounts to follow, which topics/tags to search), relevance definition, goal type, what to extract & score, threshold, language mix. Threads-specific knobs: **`seed_hashtags`** (search terms / tags — reused as Threads search queries on both paths, exactly as YouTube reuses `seed_hashtags` as Data-API queries) and **`seed_accounts`** (the follow graph steering For You/Following on the CDP path). No `seed_channels` (Threads has no channel primitive). `include_home_feed` maps to the For-You feed toggle on the CDP path.
- The campaign **Extract** input drives the AI's extracted-field schema (injected into the cascade contract; JSON mode).
- Test of "generic": swap `campaign.md` and the same binary runs a different hunt, no code change.

## 5. Architecture (target)
```
Engine (headless) ──► SQLite ◄── Admin panel (local web app)
        └─► model router ─► local text · local vision (MLX/Ollama) | cloud (OpenRouter)
```
- **Engine:** Python + Playwright over CDP (primary) and/or a thin `graph.threads.net` HTTP client (secondary). Walks the feeds/search, reads **post text first**, then (image/video posts only) **on-screen text of attached media**, reads the **reply tree (incl. nested replies)**, runs the cascade against the active brief, writes matches + state.

  **Content examined (explicit):** every public post type — **text-only posts, image posts, and video posts** — and **all replies beneath them, including nested/second-level replies**. **Text is the primary surface**; the parent post's body lives in `Reel.caption` and replies live in `Reel.comments`. The **vision/OCR tier is a secondary, optional pass run ONLY on image/video posts**; it is never a precondition for examining a post, and `on_screen_frames` is empty for text-only posts. Audio (attached-video voiceover) is **v2 only**.

  **Maps onto `FeedSource`:**
  - **Threads post (any type) = `Reel`.** `Reel.reel_id` = Threads post id (API `id`, or the media pk intercepted from web GraphQL). `Reel.caption` = the post's **text body** (the primary surface — a text-only post is fully examinable on caption alone). `Reel.author` = post author username. `Reel.on_screen_frames` / `Reel.ocr_text` = populated **only** for image/video posts via `capture_frame`/`capture_frames` (base64 JPEGs of the attached media for OCR).
  - **Reply = `Comment`.** `Comment.comment_id` = reply id; `Comment.username` = replier; `Comment.text` = reply body; `Comment.lang` per-reply language; `Comment.is_reply = True` for nested/second-level replies in the conversation tree.
  - **Seed unit:** on the **API path** the seed unit is a **keyword/tag** (`q` + `search_type`) → maps to `seed_hashtags`. On the **CDP path** the seed unit is the warmed account's **followed-accounts graph + searched topics** (`seed_accounts` + `seed_hashtags`), steering For You/Following.
  - Engagement methods (`like_reel` / `follow_author` / `detect_action_block`) stay **no-ops** — read-only feed.

  **How `build_feed` wires it** (`engine/reelradar/feeds/__init__.py`):
  1. **Platform allow-list (two enforcement points).** `config.py` line 23 today is exactly `SUPPORTED_PLATFORMS = ("instagram","youtube","telegram")` — `threads` is **not** yet listed, so a threads brief currently raises `ValueError` at `load_campaign` (`config.py:223-226`) **before** `build_feed` is ever reached. The allow-list is enforced in **`config.py`** (`load_campaign` + `campaign_from_brief` both validate `CampaignBrief.platform` against `SUPPORTED_PLATFORMS`), so adding `"threads"` there — making it `("instagram","youtube","telegram","threads")` — is a **prerequisite** to the `build_feed` branch below; otherwise the brief is rejected before the feed is constructed. Threads reuses the existing `seed_hashtags` (search terms/tags) + `seed_accounts` (follow graph) knobs — **no `seed_channels` usage**. `include_home_feed` → For-You toggle on CDP.
  2. New `engine/reelradar/feeds/threads.py` with **two** `FeedSource` impls behind one factory choice:
     - **`ThreadsCdpFeed` (primary)** — parallels the Instagram `CDPFeed`: `attach()` does `connect_over_cdp` to the warmed Chrome; `walk()` iterates For You / Following / Search; `open_reel(reel) -> bool` opens the post's permalink so its reply tree / engagement become fetchable (load-bearing for a CDP feed that must page a permalink, **not** a no-op); `fetch_comments()` pages a post's reply tree (incl. nested); `capture_frame(s)` grab attached image/video frames; `healthy()` = empty-interception canary; engagement methods (`like_reel` / `follow_author` / `detect_action_block`) are no-ops.
     - **`ThreadsApiFeed` (secondary)** — wraps a `ThreadsApiClient` (`graph.threads.net`): `walk()` drives `keyword_search(q, search_type=TOP|RECENT, since=…, until=…)` over `seed_hashtags` → Reels; `open_reel()` is effectively a no-op for the API path (the post id is the handle); `fetch_comments()` is **OWN-media-oriented** — it can page replies/conversation only where Meta's reply endpoints reach (the authenticated account's own media); **third-party reply trees are unconfirmed/likely unavailable** via the API and fall to CDP; `capture_frames` pulls media URLs → base64; `healthy()` checks token + 24h request budget + permission status; engagement no-op. `ThreadsApiClient.from_credentials(credentials)` / `from_env()`.
  3. `build_feed()`: add a `platform == "threads"` branch **before** the `SUPPORTED_PLATFORMS` NotImplementedError. Logic — *if* `credentials` present **and** the campaign is keyword/tag-seeded **and** the `threads_keyword_search` permission is approved → `ThreadsApiFeed.from_credentials(credentials)`; *else* `ThreadsCdpFeed(CDPConfig(cdp_url, seed_hashtags, seed_accounts, include_home_feed))`. Then `feed.attach()`; return `feed`. Mirrors the YouTube `credentials`/`from_env` fallback.
  4. **Per-org connection** (API path only): a **schema v9** `integration_secrets` entry (Fernet-encrypted under `REELRADAR_SECRET_KEY`) + a **"Connect Threads"** OAuth endpoint mirroring YT/TG; `needs-reconnect` surfaced at run time. The **CDP path needs no secret** — it is **managed**, like Instagram (attach to the operator's warmed Chrome; identity is the Instagram-linked Threads login). `credentials=None` → CDP managed path; else the stored per-org token enables the API path.
  5. Cascade / store / state **unchanged** — both feeds yield `Reel`/`Comment`; matches carry `platform = "threads"`; writes idempotent on `comment_id` (reply id); resume from state.
- **Model router:** call sites — `classifyText` (relevance + match, the primary path), `classifyImage` (on-screen text of attached media, secondary), `transcribe` (attached-video voiceover, **v2**) — each routes per-tier to local or OpenRouter.
- **Store:** SQLite (WAL). Matches, state, status, spend. Carries the shared `platform` dimension (`threads`).
- **Panel:** local web app reading/writing SQLite. Never calls the engine.
- **v2 shell:** OpenJarvis wraps the frozen engine as a scheduled skill.

## 6. Model routing
| Stage | Task | Where | Notes |
|---|---|---|---|
| Relevance gate | is this post relevant to the brief? | Local text (**post body** — primary) → **local vision (on-screen text), image/video posts only** → **escalate-if-unsure → cloud** | **text-first**; most posts judged on text alone; text-only posts skip vision entirely |
| Match scoring | is this reply (incl. nested) a match? | Local → escalate-if-unsure → cloud | runs on every reply in the tree |
| Vision / OCR | read text burned into an attached image / video frame | Local (Qwen2.5-VL 7B class) — **v1, secondary** | load-on-demand for the image/video-post minority, then unload; **skipped on text-only posts** |
| Audio / transcript | attached-video voiceover transcript | **not used in v1** (Whisper, **v2**) | rare; defer until lead posts prove to carry video voiceover |

One model resident at a time (36GB cap). **Batch by stage** — run the text gate across posts, then load vision once for the image/video-post minority, unload, then escalate the unsure remainder to cloud. Cloud tier needs fallback (retry queue or degrade-to-local + flag) and per-campaign spend logging.

## 7. State model (the spine)
Persisted in SQLite between sessions:
- **seen_posts** — dedupe; forward-only watermark (per post id).
- **reply cursors** — per post / conversation thread; "new replies since last poll," following the reply tree by id (CDP) or pagination cursor (API).
- **watchlist** — match-rich posts/threads, re-polled until aged out (~7–14 days).
- **session counters** — posts seen, **already-seen skips**, relevance passes, matches, escalations, spend; **API path** also counts `keyword_search` requests spent against the **2,200-request rolling-24h** budget.
- **feed-health flag** — set when the already-seen-skip ratio crosses threshold (For-You tapped out / drifting off-niche).
- **account health flags** — last run, checkpoint state, **empty-interception canary**, linked-Instagram-restriction signal; **API path:** token validity + long-lived-token expiry + `threads_keyword_search`-permission status.
- **match status** — keyed on `comment_id` (the reply id); survives re-scrapes (re-poll never overwrites human status).

Writes idempotent on `comment_id`. Killed mid-run → resume from state.

## 8. Match record (schema)
`campaign_id, platform, post_id, comment_id, username, text, lang, score, reason, extracted, status, captured_at`
- Maps onto the shared core record: `post_id` → the parent post (the `reel_id` slot); `comment_id` → the reply (incl. nested) scored.
- `score` / `reason` — interpreted per the active brief.
- `extracted` — brief-defined JSON, schema driven by the campaign Extract input (lead → handle/intent; partner → post author; signal → topic).
- `status` ∈ { new, reviewing, confirmed, discarded }

## 9. Failure handling (three tiers)
- **Auto-skip + continue (transient):** a single post fails to load/intercept; one reply page fails to fetch; one malformed GraphQL JSON parse (**tolerant, never-throw boundary** — never crash the run); a transient API 5xx on one `keyword_search` call.
- **Soft flag (continue, surface on dashboard):** feed exhaustion/drift (already-seen-skip ratio over threshold → For-You tapped out / off-niche → re-steer signal); OpenRouter spend cap hit → degrade-to-local + flag; cloud tier degraded → degrade-to-local + flag; **API path** — `keyword_search` 2,200-request / rolling-24h budget approached or hit (an **edge case** given the generous ceiling) → flag + **fall back to CDP search** for the rest of the window; API rate-limit hit, or long-lived token nearing ~60-day expiry → flag **"reconnect Threads."**
- **Halt + alert human (stop session):** login/session expired in the warmed Chrome; **checkpoint / captcha / login challenge / 2FA / account-restriction screen** (Meta's own enforcement family — same as Instagram/Facebook checkpoints); **empty-interception canary** trips for N consecutive posts (endpoint drift — the marquee Threads failure given the young, drift-prone web client); **linked-Instagram restriction/ban signal** (blast-radius guard); **API path** — `threads_keyword_search` permission revoked / OAuth token invalidated / App-Review status lost. The agent **never** attempts to resolve a challenge.

## 10. Pacing & steering/seeding
- **Weeks 1–2: manual warming only, no automation.** Warm via the **linked Instagram identity** (follow the right accounts on IG — Threads inherits the graph and it seeds For You strongly) plus follow/search on Threads itself. **Blast-radius caution:** a restriction on Threads can hit the linked Instagram account, so ramp conservatively.
- **Steering/seeding is manual + recurring, deterministic where possible.** The operator lists keyword/tag seeds (`seed_hashtags`) and accounts to follow (`seed_accounts`) in `campaign.md`; re-nudges follows + the IG graph + search terms when the already-seen-skip ratio rises. **NO Lists, NO channels.** Re-steering = editing seeds and following more accounts, not a code change. Search-seeded discovery (API or CDP) is deterministic and re-runnable from seeds; **For You is non-deterministic** and re-steered indirectly.
- **Agent runs (CDP path):** ramp from low — ~1–2 sessions/day, 15–30 min, ~20–40 posts/session; dwell 3–30s/post, between-post 2–8s randomized; **daytime only**; ramp until resistance, then hold below it. Caps discovered empirically.
- **API path pacing:** the `keyword_search` ceiling is **2,200 requests per rolling 24-hour period, per user** (official docs, as of 2026-06; verify before build) — a generous budget, not a bottleneck. Spend it on a handful of high-value seed terms per session (use `search_type=TOP` for lead-bearing posts, `RECENT` for freshness, and `since`/`until` date ranges — earliest allowable `1688540400` / 2023-07-05 — to reach older windows), and avoid tight loops out of courtesy, but exhaustion within a 24h window is an **edge case** for normal seed-driven use rather than a core design pressure. Respect the broader publisher-oriented Graph rate limits.

---

## 11. Scope: v1 vs v2

### v1 — Prove the loop (manual · headless · CDP-primary)
- Manual account warming (via the **linked Instagram identity**) + follow/topic/tag seeding; ongoing re-steering.
- **CDP attach to warmed Chrome (managed identity, no per-org secret).**
- For You + Following + Search discovery loop: dwell-on-relevant / scroll-past-irrelevant.
- **Examines every post type — text-only, image, and video posts.** Relevance gate: **post text (primary) → on-screen-text (vision/OCR, image/video posts only) → escalate-if-unsure to cloud.** Text-only posts judged on text alone.
- Interception of posts + replies; **full reply tree incl. nested replies**; deeper-thread expansion on matching threads; follow continuation by id.
- Reply cascade: local pre-filter → local scoring → escalate-if-unsure to cloud.
- **Vision/OCR tier (secondary, image/video posts only). No audio in v1.**
- SQLite store: full schema + state model + resume; carries `platform = threads`.
- soul.md + campaign.md, with one real brief (lead-gen) as proof; Extract input drives the `extracted` schema.
- Three-tier failure handling (auto-skip / soft-flag / halt-alert), incl. **empty-interception canary** tuned hard for Threads.
- **Session counters + tired-feed flag.**
- Pacing engine (daytime, randomized dwell, blast-radius-aware ramp).
- **Panel — read surfaces:** matches table (filter/sort/status-mark), health/canary panel (incl. feed-health + linked-IG signal), OpenRouter spend.
- Operation: operator triggers sessions manually. Follow-up: human, off-platform.
- **API path is OPTIONAL in v1** — if the operator already holds App-Review approval of the `threads_keyword_search` permission, the keyword/tag seed slice can layer in (schema v9 connection + `ThreadsApiFeed`); otherwise v1 ships **CDP-only** with zero App Review.

**v1 done =** account survives weeks unrestricted (and the linked Instagram is unaffected) · sessions complete and resume cleanly · every post type (text/image/video) is examined and matches land with validated precision against a hand-labeled set, on at least one real brief · tired-feed flag fires correctly · the empty-interception canary halts on induced endpoint drift rather than hammering.

### v2 — Intelligence + automation
- **OpenJarvis integration:** frozen scraper as a scheduled skill; unattended **scheduled** (not continuous) mode; memory layer.
- **API accelerant promoted:** App-Reviewed `keyword_search` discovery (with `since`/`until` historical windows) + any reachable own-media reply reading layered in as a deterministic seed lane alongside CDP, with the schema v9 connection + reconnect flow.
- **Audio tier:** Whisper transcription of attached-video voiceover.
- **Panel — write surfaces:** campaign editor (writes campaign.md incl. `seed_hashtags`/`seed_accounts`; engine re-reads only at session start); **review queue** (each confirm/discard captured as labeled training data → tunes thresholds + escalation); richer status workflow.
- **Memory:** optional Obsidian vault as accumulating market model.
- **Scale:** multi-campaign (parallel briefs); discovery feedback (suggest adjacent accounts/tags).

**v2 done =** runs on a schedule with no babysitting · review-queue feedback measurably lifts precision · ≥2 distinct briefs run on the same engine with no code change.

---

## 12. Open questions
- `keyword_search` historical reach is **largely documented**: it supports `since`/`until` date-range filtering back to the earliest allowable date `1688540400` (2023-07-05), so older lead-bearing posts **are** reachable — remaining work is a **verification note** (confirm the date-range params behave as documented and that `TOP` vs `RECENT` returns the expected mix), not an open "can it reach old posts?" question. (as of 2026-06; verify before build)
- Is reading the **reply/conversation tree of arbitrary third-party public posts** (beyond `keyword_search` hits) available via the official API under any scope, or is it **own-media-only**? Meta's documented reply-management endpoints (`threads_read_replies`/`threads_manage_replies`, `GET {media-id}/replies` and `/conversation`) are oriented to the user's **own** media; third-party reply retrieval is **not documented and likely unavailable** (docs lean own-media-only as of 2026-06; verify) — so reply collection should be assumed to fall to CDP.
- Per-session budget against the **2,200-request rolling-24h** `keyword_search` ceiling: how many seed terms × runs in practice, and confirm the (edge-case) fallback policy to CDP search if the window is ever approached.
- Will the operator actually pursue App Review approval of the `threads_keyword_search` permission (plus any Business/access verification), or ship **CDP-only** for v1 and treat the API as a v1.5/v2 accelerant?
- Long-lived token lifetime and refresh cadence (assumed ~60 days; verify) — confirm and wire the `needs-reconnect` flag.
- Endpoint-drift cadence of the Threads web GraphQL — how aggressively must the canary + interception-**by-shape** (not hardcoded ids) be tuned vs the Instagram feed?
- Blast-radius policy: which Threads signals should **pre-emptively halt** to protect the linked Instagram account?
- Local model strength on Uzbek/Russian Threads text; size of the hand-labeled validation set.
- Already-seen-skip threshold that should trip the tired-feed flag.
- Retention TTL for stored personal data; GDPR posture if targets extend beyond CIS.
- How far to schematize the `extracted` blob vs leave it free-form per brief.

## 13. Risks
| Risk | Mitigation |
|---|---|
| Account ban/restriction on Threads with blast-radius onto the **linked Instagram** account | read-only · warm via genuine IG identity · conservative bursty daytime ramp · CDP-attach to warmed Chrome (no crafted calls) · halt-on-challenge · IG-restriction signal **pre-emptively halts** |
| Endpoint drift breaks CDP interception (Threads web GraphQL is the **youngest/most volatile** of the four) | intercept by URL + response **shape**, not hardcoded ids · empty-interception canary (`healthy()`) **halts** after N empty posts · tolerant never-throw JSON boundary on intercepted payloads |
| `threads_keyword_search` permission denied or revoked (Meta App Review gate; without it `keyword_search` silently returns only own posts) | ship **CDP-first** so v1 works with zero App Review · treat the API slice as an optional accelerant · degrade to CDP search if approval is lost (flag) |
| `keyword_search` 2,200-request / rolling-24h budget approached (edge case for seed-driven use, not a core bottleneck) | spend a handful of high-value seed terms per session; use `since`/`until` + `TOP`/`RECENT` deliberately · soft-flag if the window is approached · fall back to CDP search for the remainder of the 24h window |
| API cannot read third-party reply trees (own-media-oriented; third-party reply reading unconfirmed/likely unavailable) — reply collection leans on CDP | CDP `fetch_comments()` pages the full conversation tree (incl. nested) · API replies used only on own/reachable media where confirmed · both yield `Comment` so the cascade is path-agnostic |
| Meta anti-bot (own fingerprint / behavioral / device checks · checkpoint · login challenge) blocks scripted access | **never a scripted HTTP client** — attach to real warmed Chrome and read its own authenticated traffic · halt (never auto-solve) on checkpoint / captcha / login challenge / 2FA / account-restriction screen · API path carries near-zero anti-bot risk (sanctioned, token-authed) |
| Relevance judged half-blind on image/video posts | vision/OCR tier (secondary, `on_screen_frames`) reads media text on image/video posts; text-only posts judged on caption alone · escalate-if-unsure to cloud |
| Local model weak on Uzbek/Russian | cloud escalation on both gate and scoring + hand-labeled validation set |
| For You feed runs dry / drifts off-niche | already-seen-skip ratio → dashboard flag → operator re-steers via follows / IG graph / search terms (no Lists, no channels) |
| OAuth token expiry/invalidation on the API path | long-lived-token refresh + `needs-reconnect` flag surfaced in panel · halt API path on invalidation, **continue on CDP** |
| Shared-file concurrency between engine and panel | SQLite WAL · config read at session start only · status keyed on `comment_id` (reply id) |
| Data / privacy (collecting personal data from public posts) | public sources only · API excludes private-account mentions · store minimum · retention TTL · use only for the campaign's stated purpose · human-led off-platform follow-up |
