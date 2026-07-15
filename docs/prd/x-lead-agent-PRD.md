# X (Twitter) Post-Reply & Quote-Post Discovery Agent — PRD (v1 & v2)

**Status:** Draft v0.2 · **Date:** 2026-06-19 · *product name TBD*

> Generic engine. It knows no vertical and no goal — all domain meaning lives in the campaign brief. The flagship first campaign is lead-gen (e.g. a SaaS product), but that is one brief, not a built-in assumption.

> **Sibling spec.** This is one of a family of per-platform agents (Instagram, YouTube, Facebook, LinkedIn, **X (Twitter)**, Threads, Reddit, TikTok, Quora, Pinterest) that share an identical engine and contract. A future **campaign orchestrator** lets an operator author one `campaign.md`, fan it out to the platform engines they pick, and pool every match into a single leads dashboard over the shared SQLite contract. X, Threads, Reddit and Quora are being authored together and implemented as one combined phase, because all four are text-first conversation platforms that map onto the same `Reel`/`Comment` abstraction and differ mainly in access path and thread shape. This PRD is self-contained; the orchestrator is a separate doc.

> **Why X (Twitter) differs.** X is the same managed-CDP, *interception-not-API* model as Instagram — the engine attaches to a warmed, logged-in Chrome and reads x.com's own internal GraphQL traffic read-only — but it is the **most automation-hostile platform in the family** and its access surface is the most volatile. Its official **API v2 exists** and is honestly considered, but for a local lead-gen tool that needs to read large volumes of public posts + replies it is the wrong tool: as of 2026-06 there is no usable free read tier; new developers can no longer buy the legacy **Basic (~$200/mo)** or **Pro (~$5,000/mo)** fixed plans *(those are grandfathered-only, and legacy Basic is being auto-migrated to pay-per-use ~2026-06-01; as of 2026-06, verify before build)*; the **default consumption model bills ~$0.005 per post read against a 2M reads/month cap** *(as of 2026-06; verify before build)* — and even setting cost aside the API cannot surface the operator's personalized **For You** feed the engine is built to read. So X uses **CDP**: attach to a warmed Chrome and intercept x.com's internal GraphQL (`HomeTimeline` / `SearchTimeline` / `ListLatestTimeline` / `TweetDetail` / a quoted-tweets timeline) read-only. Two things make X distinctly harder than its siblings: **(1)** the content model has **two comment surfaces** — threaded **replies** *and* **quote-posts**, where a quote-post is a *separate standalone tweet* (its own `rest_id`, fetched from a different endpoint) rather than a node in the reply tree, so `XFeed.fetch_comments` merges two internal GraphQL sources behind the one interface (see §5); and **(2)** X rotates GraphQL `doc_id`s and required `features` params every **~2–4 weeks** specifically to break scrapers, enforces hard **daily read-view caps** (~10k verified / ~1k unverified / ~500 brand-new *as of 2026-06; verify before build*) and per-account/app/device/IP blocks, and gates login behind **Arkose Labs FunCaptcha** — so the empty-interception canary, weeks of warming on a (ideally verified) account, aggressive sub-ceiling pacing, and strict halt-on-challenge (never solve Arkose) are load-bearing, not optional.

---

## 1. Summary
A local-first agent that discovers relevant **posts of every public type — text-only tweets, image posts, link posts, and video posts** — through X's **For You feed, Search, and Lists**, reads **the post's text as the primary surface** (and, only when the post carries an image or video, the on-screen/OCR text via an optional vision pass), then reads **all of the post's replies (including nested/threaded replies) and its quote-posts** and scores/extracts them against a **campaign-defined brief**, surfacing matches in an admin panel for human follow-up. A text-only tweet with no media is fully first-class and is examined on its text alone; vision/OCR is a secondary, optional pass that runs only for image/video posts and is never a precondition for examining an item. The engine is domain- and goal-agnostic and must run against a brief it has never seen with zero code change. Runs on the operator's own Mac in short daytime bursts — not on a server, not 24/7.

*(Note: "reel" is only the engine's internal dataclass name, inherited from the Instagram origin; on X the parent item is a **post/tweet of any type**, not a video.)*

**Content types in scope (text-first, vision-optional):**
- **Parent items examined:** text-only tweets, image posts, link posts, video posts. Each parent's text/body is read first; the vision/OCR pass runs only for the image/video subset.
- **Reply-level items examined (two distinct match surfaces):** **(a)** **replies** in the post's conversation tree, including nested/threaded replies; **(b)** **quote-posts** — standalone tweets that embed the parent — which are a *second* match surface fetched from a different endpoint than replies.

*A **match** = any reply or quote-post scoring above threshold against the active brief. For the lead-gen campaign, a match is a lead.*

## 2. Goals / Non-Goals

**Goals**
- Surface brief-matching replies **and quote-posts** from public posts of every type, where relevance, goal type, what-to-extract, and threshold all live in `campaign.md`.
- Survive on X — behave like a real human account, never trip automation enforcement; the canary catches GraphQL drift before it becomes blind scrolling.
- Keep heavy data local; use cloud only as a targeted escalation when the local model is unsure.
- Operator-controllable and observable through an admin panel.

**Non-Goals (both versions)**
- The bot never likes, reposts, follows, replies, quotes, bookmarks, or DMs. The only "actions" are human-like dwell + scroll. Read-only.
- Never solves Arkose FunCaptcha / checkpoints / "confirm you're human" — it halts and alerts a human.
- No multi-account farming, no cold mass-DM outreach.
- No reading protected/private accounts the warmed account cannot legitimately see; **public posts only**.
- No reselling collected data (separate product direction, out of scope).
- The engine hardcodes no vertical and no goal.

## 3. Core principles (locked)
- **Generic engine** — knows no vertical, no goal; runnable against an unseen brief with zero code change.
- **Local execution** on the operator's machine (M4 Max, 36GB), bursty daytime runs.
- **Real Chrome over CDP** — attach to a warmed, logged-in profile; never launch a vanilla automation browser. **Managed identity, like Instagram** — no per-org secret (see §5).
- **Read-only collection** — passive viewing only; the only "actions" are human-like dwell and scroll. Read-only removes the like/follow/repost patterns that are the single biggest automated-suspension trigger.
- **Feed steering is manual + recurring** — the operator seeds discovery by hand (following the right accounts, building topic Lists, saving searches) during warming and re-nudges it periodically as it drifts (see §10).
- **Network interception, not DOM scraping** — read the page's own internal GraphQL traffic; parse **by response shape, not by URL/`doc_id`** (hints are a cheap pre-filter only); never craft API calls; never write actions.
- **Escalate only when unsure** — both relevance and match decisions run cheap-local first and ask the cloud model only on low-confidence cases.
- **SQLite is the only contract** between engine and panel (WAL mode; two processes meet at the DB).
- **soul.md + campaign.md** — config read only at session start.
- **Generic criteria & goal, fixed core record** — relevance/goal/extraction are free-text brief; every match lands in identical core columns plus a brief-defined `extracted` blob.
- **Halt on resistance, never auto-solve** — any FunCaptcha / checkpoint / login-expiry / "rate limit exceeded" lockout stops the session and alerts a human.

## 4. soul.md vs campaign.md (the generalization boundary)
- **soul.md** — engine identity + safety: read-only, pacing, halt-on-resistance, never act. Domain-free.
- **campaign.md** — warming/seed direction (which accounts to follow, which Lists, which saved searches/hashtags, whether to walk For You), relevance definition, goal type, what to extract & score, threshold, language mix.
- X-specific seed knobs (reuse existing Campaign fields, no new schema required):
  - `seed_accounts` — X `@handles` whose posts to walk; also carries **List members** for the semi-deterministic List source.
  - `seed_hashtags` — saved **Search** queries / hashtags.
  - `include_home_feed` (the Instagram-only flag) is **repurposed** to mean "walk the **For You** feed."
  - An optional **List-id** knob may ride in the campaign `knobs` blob if List membership proves better modeled by id than by member handles (open question, §12).
- Test of "generic": swap `campaign.md` and the same binary runs a different hunt, no code change.

## 5. Architecture (target)
```
Engine (headless) ──► SQLite ◄── Admin panel (local web app)
        └─► model router ─► local text · local vision (MLX/Ollama) | cloud (OpenRouter)
```
- **Engine:** Python + Playwright over CDP (`connect_over_cdp` to the warmed Chrome on x.com). Walks **For You / Search / Lists**, reads **post text first** + (for image/video posts only) on-screen/image text, intercepts JSON (GraphQL) **by response shape**, reads the **reply tree and the quote-posts**, runs the cascade against the active brief, writes matches + state.

- **Maps onto `FeedSource`** (`engine/reelradar/feed.py`):
  - **the parent post (any type) = `Reel`** — `Reel.reel_id` = tweet `rest_id` (numeric status id); `Reel.caption` = the post's `full_text`/`note_tweet` text (**the primary surface — a text-only tweet is examined on caption alone**); `Reel.author` = posting account `@handle` (`screen_name`); `Reel.on_screen_frames` = base64 JPEG frames captured **only** when the post carries media (`extended_entities` photo/video) — **empty for text/link-only posts**; `Reel.ocr_text` = vision-tier output for image/video posts.
  - **a reply OR a quote-post = `Comment`** — **two distinct match surfaces**:
    1. **Replies** — tweets in the parent's conversation tree (`conversation_id == parent`), including nested/threaded replies. `Comment.comment_id` = reply `rest_id`; `.username` = replier `@handle`; `.text` = reply `full_text`; `.is_reply = True`.
    2. **Quote-posts** — a *second, standalone* tweet (its own `rest_id`) that embeds the parent via `quoted_status`. It is **not** a node in the reply tree; it is surfaced by a **different GraphQL endpoint** (the Quotes / quoted-tweet timeline). It maps onto `Comment` too: `comment_id` = the quoting tweet's `rest_id`, `is_reply = False` (plus an `extracted` flag marking it a quote), so a lead who quote-posts an operator's seeded thread is captured exactly like a replier.
  - **`walk()`** yields one `Reel` per intercepted post across the three discovery sources; **`fetch_comments(reel_id, since_cursor)`** is a **single call against the one-cursor-in / one-cursor-out interface** — `XFeed.fetch_comments` **merges TWO internal GraphQL sources behind that single interface** (the reply/conversation tree via `TweetDetail`/conversation, incl. nested replies, **and** the Quotes timeline), returning a **merged `Comment` list** and a **composite cursor** (e.g. `"replyCur|quoteCur"`) packed into the **one `new_cursor` slot** — the **same pattern by which YouTube/Telegram hide their own pagination** behind one cursor. **No second cursor column and no contract change:** the session loop calls `fetch_comments` once, reads one cursor via `store.get_cursor`, and unpacks one `(comments, new_cursor)` tuple (`engine/reelradar/session.py:152-153`). **`capture_frame`/`capture_frames`** screenshot media posts only (empty for text); **`healthy()`** is the empty-interception canary; **`open_reel()`** opens the status permalink; **`like_reel`/`follow_author`/`detect_action_block`** are **no-ops** (read-only).

- **`build_feed` wiring** (`engine/reelradar/feeds/__init__.py`): add an `if platform == "x":` branch **before** the `SUPPORTED_PLATFORMS` `NotImplementedError`. It constructs a new `XFeed(FeedSource)` (new module `engine/reelradar/feeds/x.py`, modeled on `cdp.py`/`CDPFeed`) from `cdp_url` + `seed_accounts` + `seed_hashtags` + `include_home_feed` (For You toggle); **`credentials` stays `None`** (managed, no secret). `feed.attach()` then return — the **same shape as the `instagram` branch**. Also add `"x"` to `SUPPORTED_PLATFORMS` in `config.py` so `CampaignBrief.platform` validation accepts it (the doc path `docs/prd/x-lead-agent-PRD.md` already matches the `NotImplementedError` pointer).

- **Connection / identity:** **MANAGED-CDP, like Instagram — NO per-org connection and NO secret.** A secret-backed platform would add a new `integration_secrets` row (a future schema bump) **and** a connect endpoint; X needs **neither**. That machinery (schema v8 encrypted `integration_secrets` / Fernet) exists only for the deterministic-API platforms (YouTube Data API key, Telegram Telethon session). Identity is the operator's single warmed, logged-in Chrome, so X is **one managed account per operator install**. RBAC + campaign-ownership (schema v7) still applies to the campaigns and matches; only the *platform connection* is managed rather than secret-backed.

- **Model router:** call sites — `classifyText` (relevance + match scoring), `classifyImage` (on-screen/image text for image-bearing posts), `transcribe` (v2 only). Text-first, so vision is secondary and audio is deferred.
- **Store:** SQLite (WAL). Matches, state, status, spend. Carries the shared `platform` dimension (`x`).
- **Panel:** local web app reading/writing SQLite. Never calls the engine.
- **v2 shell:** OpenJarvis wraps the frozen engine as a scheduled skill.

## 6. Model routing
| Stage | Task | Where | Notes |
|---|---|---|---|
| Relevance gate | is this post relevant to the brief? | Local text (post text) → **local vision (image/on-screen text) only for image/video posts** → **escalate-if-unsure → cloud** | **text-first**; every post judged on its text; vision runs only on the media subset |
| Match scoring | is this reply **or quote-post** a match? | Local → escalate-if-unsure → cloud | both comment surfaces scored identically |
| Vision / OCR | read text inside an attached image / video frame (e.g. a price card or flyer) | Local (Qwen2.5-VL 7B class) — **v1, secondary** | load-on-demand for the image/video minority, then unload; **skipped entirely on text/link-only posts** |
| Audio / transcript | attached-video voiceover transcript | **not used in v1; v2** | rare on lead posts; runs last when added |

One model resident at a time (36GB cap). **Batch by stage** — run the text gate across posts, then load vision once for the image/video minority, unload, then escalate the unsure remainder to cloud. Cloud tier needs fallback (retry queue or degrade-to-local + flag) and per-campaign spend logging.

## 7. State model (the spine)
Persisted in SQLite between sessions:
- **seen_posts** — dedupe; forward-only watermark (per source, by tweet `rest_id`).
- **comment cursor** — per post, **one composite cursor** in the single cursor slot, encoding both sub-cursors (reply-tree sub-cursor + Quotes-timeline sub-cursor, e.g. `"replyCur|quoteCur"`) so both "new replies since last poll" and "new quote-posts since last poll" advance behind one stored value — **no second cursor column**.
- **watchlist** — match-rich posts (high-reply / quote-magnet threads), re-polled until aged out (~7–14 days); quote-post + deep-reply walking is exhaustive only on watchlisted posts, to keep read volume down.
- **read-budget counter** — cumulative post **views** this session/day vs the account's known daily read-view ceiling; soft-flag and stop **before** the hard "rate limit exceeded" lockout.
- **session counters** — posts seen, **already-seen skips**, relevance passes, matches, escalations, spend.
- **feed-health flags** — set per source (**For You** / **Lists** / **Search** tracked separately) when the already-seen-skip ratio crosses threshold (source tapped out / drifting).
- **account health flags** — last run, login/session state, **empty-interception canary** state.
- **match status** — keyed on `comment_id` (the reply *or* quote-post `rest_id`); survives re-scrapes (re-poll never overwrites human status).

Writes idempotent on `comment_id`. Killed mid-run → resume from state.

## 8. Match record (schema)
`campaign_id, platform, post_id, comment_id, username, text, lang, score, reason, extracted, status, captured_at`
- Maps onto the shared core record: `post_id` = parent tweet `rest_id` (the `reel_id` slot); `comment_id` = the reply **or quote-post** `rest_id` (the idempotent write key); `platform` = `"x"`.
- **Quote-vs-reply** is distinguished via `is_reply` (False for quotes) and/or a small flag inside `extracted` (e.g. `"surface": "quote"|"reply"`), so a quote-post lead is captured exactly like a replier without a new column.
- `score` / `reason` — interpreted per the active brief.
- `extracted` — brief-defined JSON (lead → handle/intent; partner → post author; signal → topic), schema driven by the campaign Extract input (injected into the cascade contract; JSON mode).
- `status` ∈ { new, reviewing, confirmed, discarded }

## 9. Failure handling (three tiers)
- **Auto-skip + continue (transient):** a single post permalink won't load; one GraphQL response fails to parse; a reply/quotes page 404s mid-pagination; a media frame screenshot fails (skip vision for that item, keep its text).
- **Soft flag (continue, surface on dashboard):** feed exhaustion/drift — already-seen-skip ratio over threshold on **For You / Lists / Search** (tired-feed flag → operator widens Lists / refreshes saved searches); OpenRouter spend cap hit; cloud tier degraded → degrade-to-local + flag; a seeded List/account goes quiet; **approaching the daily read-view cap** (slow down + flag *before* the hard block).
- **Halt + alert human (stop session, never auto-resolve):** login/session expired; **Arkose FunCaptcha / checkpoint / "confirm you're human"** challenge; **"rate limit exceeded"** daily-read lockout; account locked/limited/suspended; **empty-interception canary trips** (no GraphQL captured for N posts ⇒ `doc_id`/`features` drift). X-specific emphasis: the canary is **more load-bearing here than on any sibling** because `doc_id`s rotate every ~2–4 weeks; and the challenge tier is Arkose-specific (the bot must never touch it).

## 10. Pacing & steering/seeding
**Most conservative of the family — X rate-limits aggressively and bans hard, network-wide.**
- **Warming:** weeks 1–2 (and ideally longer than Instagram), manual human use only, no automation. **Strongly prefer a verified account** so the daily read ceiling is ~10,000 views/day rather than ~1,000 (unverified) or ~500 (brand-new) *(as of 2026-06; verify before build)*. Build genuine follows, topic **Lists**, and **saved searches** by hand.
- **Ramp:** start very low — ~1–3 sessions/day, ~15–30 min, ~30–60 posts/session; dwell 3–20s per relevant post, 2–6s between posts, randomized; **daytime only**; never approach the daily read-view cap. Ramp until the first sign of rate-limit resistance, then **hold well below it**. All caps discovered empirically per account.
- **Read-budget awareness:** track cumulative post views against the daily ceiling in the state model; soft-flag and stop before the hard lockout (which typically requires ~24h to clear).
- **Steering is manual + recurring** (not a one-time seed list): the operator follows the right accounts, builds topic **Lists**, and saves **searches** during warming, then re-nudges periodically. **Lists** produce a stable, low-algorithm timeline (the most deterministic source); **Search** is semi-deterministic (deterministic in what it asks, X-controlled in ranking); **For You** is least deterministic and drifts. A rising already-seen-skip ratio (§7) is the signal to widen Lists / refresh queries. Per-post discovery is steerable; per-reply/quote discovery is **exhaustive only on watchlisted match-rich posts**, to keep read volume down.

---

## 11. Scope: v1 vs v2

### v1 — Prove the loop (manual · headless)
- Manual account warming (ideally a verified account) + Lists/saved-search/follow seeding; ongoing re-steering.
- CDP attach to warmed Chrome (`connect_over_cdp`); managed identity, no secret.
- Discovery loop across **For You + Search + Lists**: dwell-on-relevant / scroll-past-irrelevant.
- **Relevance gate: post text → image/on-screen text (vision/OCR, on image/video posts only) → escalate-if-unsure to cloud.** Text-only posts judged on text alone.
- Interception by **response shape** (new `looks_like_*` helpers) of posts + replies + quotes; `XFeed.fetch_comments` merges **two internal GraphQL sources behind the single `fetch_comments` interface** — the reply tree (`TweetDetail`/conversation, incl. nested replies) **and** the Quotes timeline — paged by a **composite cursor in the one `new_cursor` slot**; deeper-thread/quote expansion only on watchlisted match-rich posts.
- Comment cascade: local pre-filter → local scoring → escalate-if-unsure to cloud; replies and quote-posts scored identically.
- **Vision/OCR tier (secondary; image/video posts only). No audio in v1.**
- SQLite store: full schema + state model + resume; carries `platform = x`.
- soul.md + campaign.md, with one real brief (lead-gen) as proof.
- Three-tier failure handling (auto-skip / soft-flag / halt-alert), including **read-budget soft-flag** and **empty-interception canary** halt.
- **Session counters + per-source tired-feed flags + read-budget counter.**
- Pacing engine with sub-ceiling read velocity and daytime-only bursts.
- **Panel — read surfaces:** matches table (filter/sort/status-mark, with quote-vs-reply visible), health/canary panel (incl. feed-health + read-budget + canary state), OpenRouter spend.
- Operation: operator triggers sessions manually. Follow-up: human, off-platform.

**v1 done =** account survives weeks unbanned · sessions complete and resume cleanly · matches (replies **and** quote-posts) land with validated precision against a hand-labeled set, on at least one real brief · tired-feed flag fires correctly · canary halts on `doc_id`/`features` drift instead of scrolling blind · read-budget soft-flag stops the session before any hard "rate limit exceeded" lockout.

### v2 — Intelligence + automation
- **OpenJarvis integration:** frozen scraper as a scheduled skill; unattended **scheduled** (not continuous) mode; memory layer.
- **Audio tier:** Whisper transcription of attached-video voiceover (rare; runs last).
- **Panel — write surfaces:** campaign editor (writes campaign.md incl. follows/Lists/saved searches; engine re-reads only at session start); **review queue** (each confirm/discard captured as labeled training data → tunes thresholds + escalation); richer status workflow.
- **Memory:** optional Obsidian vault as accumulating market model.
- **Scale:** multi-campaign (parallel briefs); discovery feedback (suggest adjacent accounts/Lists/queries from match-rich sources).

**v2 done =** runs on a schedule with no babysitting · review-queue feedback measurably lifts precision · ≥2 distinct briefs run on the same engine with no code change.

---

## 12. Open questions
- Exact current GraphQL `doc_id`s + required `features` params for `HomeTimeline` / `SearchTimeline` / `ListLatestTimeline` / `TweetDetail` / the Quotes timeline — identify in DevTools once and expect ~2–4 week rotation; the canary must catch drift *(as of 2026-06; confirm in DevTools before build)*.
- Confirmed daily read-view ceiling for the specific (ideally verified) warmed account in 2026 — discover empirically; the ~10k/~1k/~500 figures are volatile.
- Whether to **require a verified (paid) X account** for the ~10k/day read ceiling vs accept the ~1k unverified ceiling — affects throughput and warming cost.
- Best path to **enumerate quote-posts at scale** (Quotes-timeline cursoring + rate cost) vs only walking quote-posts on watchlisted match-rich posts.
- **Lists:** model List membership via the existing `seed_accounts` (members) or add a dedicated **List-id** knob in the campaign `knobs` blob?
- Safe sub-ceiling read velocity and session caps before X throttles a warmed account — discover empirically per account.
- Whether to also persist **scored non-matches** (needed only if the market-intelligence direction is pursued) before the schema is frozen.
- Retention TTL for stored personal data; GDPR posture if targets extend beyond CIS; X's commercial-use/redistribution terms even for CDP-collected data.
- Already-seen-skip threshold that should trip the tired-feed flag **separately for For You vs Lists vs Search**.

## 13. Risks
| Risk | Mitigation |
|---|---|
| **Account suspension / ban** — X is the most automation-hostile platform and bans hard, network-wide | Strict read-only (no like/follow/repost/reply/quote/DM — removes the top trigger) · weeks of manual warming on a real, ideally verified account · human-paced ramp held well below the resistance point · CDP-attach to a genuine warmed profile · halt-on-resistance |
| **GraphQL `doc_id`s / `features` params rotate every ~2–4 weeks**, breaking interception | Parse by **response shape**, not URL/`doc_id` (hints are a pre-filter only) · empty-interception canary halts + alerts on drift · re-confirm endpoints in DevTools as a maintenance task |
| **Daily read-view cap** ("rate limit exceeded") hard-locks the account for ~24h | Track cumulative views in state vs the account's known ceiling · soft-flag and stop **before** the hard cap · prefer a verified account for the higher ceiling · daytime bursty sessions only |
| **Arkose FunCaptcha / checkpoint** challenge mid-session | **Never auto-solve** · halt + alert a human immediately (locked family principle) · resume only after a human clears it manually |
| **ToS / legal** — X's Terms (2025-05-08 revision) **expressly prohibit crawling/scraping in any form without prior written consent**, removed the old robots.txt carve-out, and are enforced via lawsuits since 2023 *(verified-via-web)* | State the risk plainly · restrict to **public** content the warmed account legitimately sees · read-only · store the minimum · retention TTL · human-led off-platform follow-up · treat as an operator-accepted legal risk, not a hidden one |
| **Quote-posts missed** because they are not in the reply tree (a whole second match surface lost) | Merge the **Quotes timeline as a second internal source inside `XFeed.fetch_comments`** (behind the single interface, composite cursor) · map quote-posts onto `Comment` (`is_reply=False` / `extracted` flag) · walk quotes on watchlisted match-rich posts |
| **For You feed drifts off-niche / runs dry**; relevance judged half-blind on image posts | Lean on semi-deterministic **Lists + saved searches** · already-seen-skip ratio → tired-feed flag → operator re-steers · secondary vision/OCR pass + escalate-if-unsure to cloud for image/video posts (text-only posts judged on text alone) |
| **Shared-file concurrency** between engine and panel | SQLite WAL · config read at session start only · match status keyed idempotently on `comment_id` (reply or quote-post `rest_id`) |
