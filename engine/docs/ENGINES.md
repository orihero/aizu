# How the lead-finding engines work (Instagram · YouTube · Telegram · Reddit · LinkedIn · X)

Each social network has its **own engine** under `reelradar/engines/<platform>/`. They
share a thin kernel (`reelradar/core/`) and one control plane (CLI, server, panel, DB),
but each owns its own discovery loop, content model, access method, and scoring. A run
is dispatched to the right engine by `campaign.platform` (`reelradar/dispatch.py`).

---

## 0. The shared idea

Every engine does the same four things, in the same order, and writes to the same place:

```
DISCOVER content  →  GATE relevance  →  READ its comments  →  MATCH + EXTRACT each one
   (per-platform)     (AI, on the post)   (per-platform)        (AI, on the commenter)
                                                                         │
                                                          score ≥ threshold ⇒ LEAD → DB
```

- **A "lead"** = a *comment/reply* whose match score ≥ the campaign `threshold` (default
  **0.70**). It is persisted with the commenter's handle, text, language, the AI's score +
  reason, and the **extracted fields** (phone, intent, budget, …) the brief asked for.
- **The brief drives everything** (`core/config.py::Campaign`): `relevance_def` (what makes
  a post on-topic), `match_def` (what makes a commenter a buyer), `extract_def` (which
  fields to pull), `threshold`, and optional tuned system prompts (`relevance_prompt` /
  `match_prompt`). Swap the brief → different hunt, no code change.
- **The cascade** (`engines/<platform>/cascade.py`) is the AI decision logic. Two stages,
  each with an *escalate-if-unsure* retry:
  - **Relevance gate** — score the post; `_unsure` = the model's confidence falls in the
    campaign's `escalate_band` (default `[0.4, 0.75]`), the score sits within 0.05 of the
    threshold, or the cloud call degraded. If unsure, re-run (cloud).
  - **Comment match** — score the commenter using the post as context; same escalate rule;
    then **coerce** the AI's `extracted` object to exactly the brief's declared keys
    (drop strays, null missing). `is_match = score ≥ threshold`.
- **Shared kernel** (`core/`):
  - `router.py` — the LLM client (OpenRouter): `classify_text` / `classify_image`, retries,
    degrade-to-local, spend cap + cost tracking, tolerant JSON parsing → a `Decision(label,
    score, confidence, reason, extracted, tier)`.
  - `store.py` — SQLite. `is_seen`/`mark_seen` (dedup per `(campaign, platform, post_id)`),
    `get_cursor`/`set_cursor` (resumable comment pagination), `upsert_match` (the lead),
    counters, and `emit_run_event` (the panel's live activity feed). **No schema change** —
    every table is already platform-keyed.
  - `feed.py` — the `FeedSource` interface plus the neutral `Reel`/`Comment` data shapes
    every engine maps its content into.
  - `cdp.py` — `CDPFeedBase`, the **shared Playwright-over-CDP harness** the three
    browser-driven engines (Instagram, X, LinkedIn) inherit: `connect_over_cdp` attach to a
    warmed Chrome, the response-interception template (hint-filter → JSON-guard → set the
    empty-interception canary → delegate to the engine's `_classify`), the seed-source walk,
    human scrolling, and screenshots. Each engine supplies only its URL hints, shape-based
    classification, sources, and comment-fetch. The pure shape parsers live in the engine
    package (`engines/<platform>/parsers.py`) so they unit-test against fixtures without a browser.

The contract each engine implements is `engines/base.py::EngineProtocol.run_session(...)`,
returning a uniform summary dict (`matches`, `reels_seen`, `relevance_passes`, `spend_usd`,
`halt_reason`, …). Engines fold a halt into `halt_reason` themselves.

---

## 1. Instagram — `engines/instagram/`

The original, full-fidelity engine. Browser-driven, with vision and engagement.

**Discovery / access** — a real, warmed Chrome over the **Chrome DevTools Protocol**
(Playwright), reading Instagram's **InnerTube network responses** (interception, not DOM
scraping). Sources: the account's algorithmic **home reels feed** (optional; on by default
only when the campaign has no seeds), plus seeded **hashtags** and **accounts**.
`CDPFeed.walk()` yields a `Reel` per reel (caption, author, on-screen video frames).

**Loop** (`session.py::Session.run`):
1. **Daytime guard** — outside ~8am–9pm it halts (avoids Instagram action-blocks).
2. Walk reels up to a randomized **reel budget** (~20–40), with human **dwell** + between-reel
   pauses. For each reel:
   - `is_seen` → skip if already processed.
   - **Relevance gate** — `classify_text` on the caption. If the caption is thin/empty or
     the verdict is unsure, capture video **frames** and run **`classify_image`** (OCR of
     on-screen price/project/phone text). Escalate to cloud if still unsure.
   - If relevant — `open_reel` (navigate to the permalink full-screen so comments +
     controls exist); optionally **Like** (opt-in, rate-limited).
   - **Read comments** (intercepted, cursor-paginated) → `score_comment` with reel context
     (*"REEL BEING COMMENTED ON (posted by the seller/agency)"*) → match + extract.
     ≥ threshold ⇒ `upsert_match` (a lead). On a real lead, optionally **Follow** the author.
3. **Failure tiers** — auto-skip a single parse error; soft-flag a *tired feed*
   (already-seen ratio), spend cap, or degraded cloud; **halt** on login expiry, checkpoint,
   action-block, or the empty-interception canary.

**Distinctive:** vision/OCR ✓ · Like/Follow engagement ✓ · daytime + pacing ✓ · single-tenant
warmed Chrome (env/CDP).

---

## 2. YouTube — `engines/youtube/`

Read-only, API-driven, text-only. No browser, no engagement, no vision.

**Discovery / access** — the official **YouTube Data API v3** (`httpx`, API key). Deterministic:
operator-seeded **channel IDs** (`seed_channels`) and **search queries** (`seed_hashtags`
reused as query terms). `YouTubeFeed.walk()` calls `search.list` and yields a `Reel` per
video (`reel_id = videoId`, `caption = title + description`, `author = channel`), de-duping a
video that surfaces via both a channel and a query.

**Loop** (`session.py::YouTubeSession.run`) — no daytime guard, no reel budget, no dwell
(the API is stateless and instant):
1. For each video:
   - `is_seen` → skip.
   - **Relevance gate** (`YouTubeCascade.gate_video`) — `classify_text` on **title +
     description only**. The Data API exposes no frames, so there is **no vision tier**:
     `classify_image` is never called. Escalate-if-unsure re-runs the text call.
   - If relevant — **read comment threads** (`commentThreads.list`, paginated; the
     `nextPageToken` is stored as the resumable cursor). Score each comment
     (`score_comment`) with the video as context (*"VIDEO BEING COMMENTED ON"*) → match +
     extract. ≥ threshold ⇒ `upsert_match`.
2. **Failure** — a single parse error auto-skips. **429 (rate limit / daily quota) and 5xx
   are retried with bounded backoff** (`_get`, honoring `Retry-After`); a persistent failure
   raises `YouTubeApiError`, which the session folds into a **graceful halt** — leads found so
   far are kept, a `youtube_api` health flag is raised, and `halt_reason` stops the
   back-to-back loop instead of crashing the run. No engagement.

**Distinctive:** vision ✗ · engagement ✗ (read-only) · per-org **API key** (encrypted) or env.

---

## 3. Telegram — `engines/telegram/`

Read-only, deterministic, text-only. There is **no algorithmic feed** to walk.

**Discovery / access** — an **MTProto user session** via Telethon (Bot-API adapter also
available), read-only. Fully deterministic: the operator seeds public **channels/groups**
(`seed_channels`). `TelegramFeed.walk()` reads each channel's recent messages and yields a
`Reel` per message (`reel_id = "channel/msgId"`, `caption = message text`, `author = channel`).
A **discussion reply** plays the role of a comment.

**Loop** (`session.py::TelegramSession.run`) — no daytime guard, no reel budget, no dwell:
1. For each channel message:
   - `is_seen` → skip.
   - **Relevance gate** (`TelegramCascade.gate_message`) — `classify_text` on the **message
     text** (text-first; **no vision**, `classify_image` never called). Escalate-if-unsure.
   - If relevant — **read the replies** from the channel's linked discussion group
     (`iter_replies`, a forward-only `min_id` watermark stored as the resumable cursor).
     Score each reply (`score_comment`) with the message as context (*"CHANNEL MESSAGE BEING
     REPLIED TO"*) → match + extract. ≥ threshold ⇒ `upsert_match`.
2. **Failure** — auto-skip a single parse error. No engagement, no session halts.

**Distinctive:** vision ✗ (v1 text-only; media is v2) · engagement ✗ (read-only) · per-org
**MTProto session** (`api_id`/`api_hash`/StringSession, encrypted) warmed out-of-band via the
login wizard, or env.

---

## 4. Reddit — `engines/reddit/`

Read-only, API-driven, text-first. Deterministic operator-seeded subreddits (no
algorithmic feed), with the genuinely new piece: a **deeply-nested comment tree**.

**Discovery / access** — the official **Reddit Data API over OAuth2** (`httpx`, app-only
`client_credentials` — no per-user account). Deterministic: the operator seeds public
**subreddits** (`seed_channels`) and, optionally, per-subreddit **search queries**
(`seed_hashtags` → `r/{sub}/search?restrict_sr=1`, still scoped to the seed so it can't
drift off-niche). `RedditFeed.walk()` reads each subreddit's `new` listing and yields a
`Reel` per submission (`reel_id = t3 id36`, `caption = title + selftext`, `author = submission
author`), de-duping a submission that surfaces via both a listing and a search.

**Loop** (`session.py::RedditSession.run`) — no daytime guard, no reel budget, no dwell:
1. For each submission:
   - `is_seen` → skip.
   - **Relevance gate** (`RedditCascade.gate_submission`) — `classify_text` on **title +
     selftext only**. Text-first: there is **no vision tier** in v1 (`classify_image` is never
     called; image/video OCR is a follow-up). Escalate-if-unsure re-runs the text call.
   - If relevant — **read the whole nested comment tree** (`r/{sub}/comments/{id}`, the adapter
     recursively flattens `t1` replies carrying a `depth` field and expands a bounded number of
     `more`/`morechildren` continuation branches). Score every comment **at any depth**
     (`score_comment`, `is_reply = depth > 0`) with the submission as context (*"SUBMISSION
     BEING COMMENTED ON"*) → match + extract. ≥ threshold ⇒ `upsert_match`. The newest scored
     comment's `created_utc` is stored as the resumable watermark cursor so later sessions only
     re-score fresh replies.
2. **Failure** — a single parse error auto-skips; the JSON mappers are tolerant (a malformed
   Listing yields nothing, never a crash). **429 (rate limit / client throttle) and 5xx are
   retried with bounded backoff** (`_get`, honoring `Retry-After` **and** `X-Ratelimit-Reset`);
   a persistent failure raises `RedditApiError`, folded into a **graceful halt** — leads kept,
   a `reddit_api` flag raised, `halt_reason` stops the loop. **401/403** keep the raw httpx error
   so the CLI flags **needs-reconnect**. No engagement.

**Distinctive:** vision ✗ (v1 text-only) · engagement ✗ (read-only) · **whole nested comment
tree** in scope (not just top-level) · per-org **app credentials** (`client_id` /
`client_secret` / `user_agent`, encrypted) minted to a token at run time, or env.

---

## 5. LinkedIn — `engines/linkedin/`

Managed-CDP, **like Instagram** — no per-org secret. Read-only, copy-first, with a
vision pass. A single match surface (post comments).

**Discovery / access** — a real, warmed Chrome over the **Chrome DevTools Protocol**
(Playwright), reading LinkedIn's **Voyager network responses** by *shape*
(`engines/linkedin/parsers.py`, drift-tolerant). Sources: the account's **home feed**
(optional) plus seeded **hashtags** and **people/companies** (`seed_accounts`).
`LinkedInFeed.walk()` yields a `Reel` per post (`reel_id = activity urn`, `caption = post
copy`, `author = actor name`).

**Loop** (`session.py::LinkedInSession.run`) — modeled on Instagram's but **read-only**
and the most conservatively paced of the family (PRD §10):
1. **Daytime guard** + human dwell / between-post pauses, up to the **reel budget**.
2. For each post: `is_seen` → skip; **relevance gate** (`LinkedInCascade.gate_post`) —
   `classify_text` on the copy, falling back to **`classify_image`** (carousel/document/image
   OCR) only when the copy is thin; escalate-if-unsure. If relevant — `open_reel` (open the
   post full-screen so its comment thread loads, attributing streamed comments to it), then
   **read comments** → `score_comment` with post context → match + extract ⇒ `upsert_match`.
3. **Failure tiers** — auto-skip a single parse error; soft-flag a tired feed / spend /
   degraded cloud; **halt** on login expiry, checkpoint, or the empty-interception canary.

**Distinctive:** managed-CDP (no secret) · vision ✓ (carousel/image text) · engagement ✗
(read-only) · loops toward target (algorithmic feed).

---

## 6. X (Twitter) — `engines/x/`

Managed-CDP, **like Instagram** — no per-org secret. Read-only, text-first. The genuinely
new piece: **two match surfaces** — threaded **replies** *and* standalone **quote-posts**.

**Discovery / access** — a real, warmed Chrome over **CDP**, reading x.com's internal
**GraphQL** responses by *shape* (`engines/x/parsers.py`; X rotates `doc_id`s every ~2–4
weeks, so URL hints are a pre-filter only). Sources: the **For You** feed (optional),
**Search** (`seed_hashtags`), and **Lists / accounts** (`seed_accounts`). `XFeed.walk()`
yields a `Reel` per post (`reel_id = rest_id`, `caption = full_text`, `author = screen_name`);
a text-only tweet is first-class.

**Loop** (`session.py::XSession.run`) — read-only, conservatively paced:
1. **Daytime guard** + dwell, up to the reel budget, **and a read-budget soft-cap** that
   stops the session *before* X's hard daily read-view lockout (`read_view_soft_cap`, PRD §7/§10).
2. For each post: `is_seen` → skip; **relevance gate** (`XCascade.gate_post`) — text first,
   **`classify_image`** only for image/video posts when the text is thin; escalate-if-unsure.
   If relevant — `open_reel` (the status page loads the reply tree), then `fetch_comments`
   **merges both surfaces behind the one interface**: the reply tree (`TweetDetail`) *and* the
   **Quotes timeline**, paged by a **composite cursor** in the single cursor slot
   (`"<replyCount>|<quoteCount>"`). Replies and quotes are scored identically; each match
   records its surface in `extracted["surface"]` (`reply`|`quote`), so a quote-post lead is
   captured like a replier with **no new column**.
3. **Failure tiers** — auto-skip a single parse error; soft-flag a tired feed / read-budget /
   spend / degraded cloud; **halt** on login expiry, **Arkose/checkpoint**, or the
   empty-interception canary (load-bearing here, since `doc_id`s rotate).

**Distinctive:** managed-CDP (no secret) · vision ✓ (image/video posts only) · engagement ✗
(read-only) · **two match surfaces** (replies + quote-posts) merged on one composite cursor ·
read-budget soft-cap · loops toward target (algorithmic feed).

---

## 7. Side by side

| Aspect            | Instagram                          | YouTube                       | Telegram                          | Reddit                              | LinkedIn                            | X (Twitter)                         |
|-------------------|------------------------------------|-------------------------------|-----------------------------------|-------------------------------------|-------------------------------------|-------------------------------------|
| Access            | Chrome + CDP interception          | Data API v3 (httpx, key)      | MTProto user session (Telethon)   | Data API OAuth2 (httpx, client_credentials) | Chrome + CDP (Voyager)          | Chrome + CDP (GraphQL)              |
| Discovery         | home feed + hashtags + accounts    | seeded channels + queries     | seeded public channels (only)     | seeded subreddits + optional searches | home feed + hashtags + people/companies | For You + Search + Lists       |
| Feed type         | algorithmic                        | deterministic search          | deterministic                     | deterministic                       | algorithmic                         | algorithmic                         |
| "Post" → "comment"| reel → comment                     | video → comment thread        | channel message → discussion reply | submission → **nested comment tree** | post → comment               | post → **reply *and* quote-post**   |
| Relevance signal  | caption **+ on-screen frames (OCR)** | title + description (text)    | message text                      | title + selftext (text)             | copy **+ carousel/image (OCR)**     | tweet text **+ media frames (OCR)** |
| Vision (`classify_image`) | **yes**                    | no                            | no                                | no (v1)                             | **yes** (when copy thin)            | **yes** (image/video posts)         |
| Engagement        | Like / Follow (opt-in)             | none (read-only)              | none (read-only)                  | none (read-only)                    | none (read-only)                    | none (read-only)                    |
| Pacing / halts    | daytime + dwell + reel budget; action-block/login halts | retry+backoff, then graceful halt on quota | none | retry+backoff, then graceful halt on throttle | daytime + dwell + budget; checkpoint/canary halts | daytime + dwell + **read-budget**; Arkose/canary halts |
| Auth              | warmed Chrome (env/CDP)            | per-org API key (encrypted)   | per-org MTProto session (encrypted) | per-org app credentials (encrypted) | **managed warmed Chrome (no secret)** | **managed warmed Chrome (no secret)** |

---

## 8. From "Run" to a lead in the dashboard

1. Panel **Run** → `POST /api/run` → `RunManager` spawns `python -m reelradar.cli run
   --campaign <id>` as one background subprocess (one run at a time).
2. The CLI resolves the brief from the DB, reads `campaign.platform`, and
   `dispatch.run_engine_session(...)` selects that platform's `run_session`.
3. **Instagram, LinkedIn and X** run **back-to-back sessions** until the **lead target**
   (`--target-leads`) is met or a max-time cap elapses (their algorithmic feeds surface fresh
   items each scroll). **YouTube, Telegram and Reddit do a single discovery pass** — their
   sources are deterministic, so re-running would re-fetch the same already-seen items at full
   API quota cost for zero new leads (`cli._SINGLE_PASS_PLATFORMS`).
4. Every match → `store.upsert_match(..., platform=…)` in the **one shared SQLite**; progress
   streams via `store.emit_run_event` to the panel's live activity feed.
5. The Pulse panel pools all platforms into a single **Leads** dashboard, each row tagged with
   a `PlatformChip` and carrying its extracted fields.

> All six engines write the same row shape, so leads from Instagram, YouTube, Telegram,
> Reddit, LinkedIn, and X land in one place — different hunting grounds, one inbox.

---

## 9. Warming runbook — the managed-CDP platforms (Instagram · LinkedIn · X)

The three managed-CDP engines attach to **one** warmed, logged-in Chrome over the DevTools
Protocol (`REELRADAR_CDP_URL`, default `127.0.0.1:9333`). The engine never launches its own
browser; `engine/scripts/warm_chrome.sh` brings up the Chrome-for-Testing build it can attach
to (see that script's header for the two Chrome 149 / default-profile gotchas). **All three
accounts live in this same profile** — log into each site once and the session persists.

**Setup (once):**

1. `engine/scripts/warm_chrome.sh` — launches Chrome-for-Testing on the CDP port with a
   dedicated `--user-data-dir`.
2. In that window, log into **all** the managed platforms you'll run:
   `instagram.com`, `linkedin.com`, and `x.com`. The cookies persist in the profile dir.
3. Re-running the engine reuses the same window; you only re-log-in if a session expires or a
   site forces a checkpoint.

**Warming the accounts (manual, weeks — see the PRDs §10):** a cold account that suddenly
reads at machine pace gets challenged. Before running the engine against a platform, warm its
account like a real person would:

- **LinkedIn** — a complete, credible profile (photo, headline, a few connections, some normal
  browsing history). Ramp reads conservatively.
- **X** — ideally a **verified** account: the read ceiling is ≈10k posts/day verified vs ≈1k
  unverified, and the X engine's read-budget soft-cap is what keeps a session under the hard
  daily lockout. An unverified account hits the wall far sooner.
- **Instagram** — already covered by the existing single-tenant warm flow.

**Operating rules (all three):**

- **Daytime only**, with human-like dwell between actions (the sessions already enforce this).
- **Read-only** — the engines never like/follow/comment on LinkedIn or X (engagement methods
  are no-ops); they only observe.
- **Never solve an Arkose/checkpoint challenge for the engine.** When a site challenges the
  account, the engine raises `HaltSession` and stops — that's the alert. Resolve it manually in
  the warmed window, then resume. Auto-solving challenges is how accounts get banned.
- **X `doc_id` drift** — X rotates its GraphQL `doc_id`s every ~2–4 weeks. The empty-interception
  canary halts the session when interception stops firing; that's the signal to re-capture the
  endpoints (see the handover §A) rather than a silent zero-lead run.
