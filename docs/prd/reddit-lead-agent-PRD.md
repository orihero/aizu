# Reddit Subreddit Post-Comment Discovery Agent — PRD (v1 & v2)

**Status:** Draft v0.2 · **Date:** 2026-06-19 · *product name TBD*

> Generic engine. It knows no vertical and no goal — all domain meaning lives in the campaign brief. The flagship first campaign is lead-gen (e.g. a SaaS product), but that is one brief, not a built-in assumption.

> **Sibling spec.** This is one of a family of per-platform agents (Instagram, YouTube, Facebook, LinkedIn, X, Threads, **Reddit**, TikTok, Quora, Pinterest) that share an identical engine and contract. A future **campaign orchestrator** lets an operator author one `campaign.md`, fan it out to the platform engines they pick, and pool every match into a single leads dashboard over the shared SQLite contract. This PRD is self-contained; the orchestrator is a separate doc. **X, Threads, Reddit, and Quora are being authored together and implemented as one combined phase** — they are the text-first siblings of the family and share the most cascade/scope behavior, so they ship as a single build wave rather than four independent efforts.

> **Why Reddit differs.** Reddit is the most *deterministic and most legitimately API-backed* platform in the family — it pairs Telegram's operator-seeded model (a curated **subreddit** list *is* the relevance filter, with no "For You" algorithm to steer) with YouTube's first-class read API (OAuth2, documented JSON, PRAW). So the engine reads it like YouTube/Telegram — deterministic seeds, a per-org encrypted credential, escalate-only-when-unsure — **not** like the web-intercepted media feeds. Two things set it apart from its siblings, both grounded: (1) its **comment trees are deeply nested** (`t1` replies carry a `depth` field and `more`/`morechildren` continuation placeholders), so the *whole tree* is in scope, not just top-level — the engine expands branches selectively and cost-bounded, exactly as it follows YouTube continuation tokens; and (2) its **access posture is legally restrictive** — the Responsible Builder Policy + Developer Terms gate commercial use (which lead-gen is) behind explicit written approval/contract, bar AI/ML training on the data, and require propagating deletions (all as of 2026-06; verify before build). Net: clean and robust to *read*, but the binding risk is a ToS/approval one to clear at onboarding, not an anti-bot one to engineer around — the opposite of Instagram.

---

## 1. Summary
A local-first agent that reads **public subreddits the operator seeds**, walks each subreddit's recent submissions, reads each submission's text, then walks the (deeply nested) comment tree and scores/extracts every reply against a **campaign-defined brief**, surfacing matches in an admin panel for human follow-up. The engine is domain- and goal-agnostic and must run against a brief it has never seen with zero code change. Runs on the operator's own Mac in short daytime bursts — not on a server, not 24/7.

**Content in scope — every public content type, text-first.** The agent examines **every submission type**: text/self-posts, link posts, image/gallery posts, **and** video posts — *not* video only — **and every reply in the nested comment tree beneath each, including deeply-nested branches**, not just top-level comments. **Text is the primary surface**: a submission's title + self-text live in the parent item's text field and the replies live in its comment list, so a text-only self-post is fully first-class and is examined on its text alone. The **vision/OCR tier is a secondary, optional pass** used only for submissions that carry an image or video — it is never a precondition for examining an item, and the on-screen-frames field is simply empty for text and link posts. Audio is not used. ("Reel" appears in this doc only as the name of the engine's internal dataclass, inherited from the Instagram origin; on Reddit the parent item is a *submission*, never a video or a "reel".)

*A **match** = any comment (at any depth) scoring above threshold against the active brief. For the lead-gen campaign, a match is a lead.*

## 2. Goals / Non-Goals

**Goals**
- Surface brief-matching comments from public subreddits, where relevance, goal type, what-to-extract, and threshold all live in `campaign.md`.
- Survive on Reddit within the official API's terms — operate inside an approved OAuth client, never trip rate-limit or ToS enforcement.
- Keep heavy data local; use cloud only as a targeted escalation when the local model is unsure.
- Operator-controllable and observable through an admin panel.

**Non-Goals (both versions)**
- The bot never upvotes, joins, follows, comments, posts, or DMs. Read-only.
- Never solves login challenges/captchas/2FA — it halts and alerts a human, and never silently re-mints credentials.
- No multi-account farming, no cold mass-outreach (no automated DMs or comment replies).
- No reading private/quarantined content the OAuth client is not authorized to read; **public subreddits only**.
- No feeding collected Reddit data into model/ML training (ToS-prohibited, as of 2026-06; verify before build); no reselling collected data.
- The engine hardcodes no vertical and no goal.

## 3. Core principles (locked)
- **Generic engine** — knows no vertical, no goal; runnable against an unseen brief with zero code change.
- **Local execution** on the operator's machine (M4 Max, 36GB), bursty daytime runs.
- **First-class API, not web scraping (Path A, primary)** — read submissions and comment trees through the **official Reddit Data API over OAuth2** (PRAW or a thin `httpx` adapter against `oauth.reddit.com`), with the operator's approved, per-org client. Documented, versioned JSON; PRAW handles pagination, `more`-expansion, and rate-limit headers. A CDP scraper — a warmed, logged-in Chrome intercepting Reddit's own internal JSON (Path B) — is treated and explicitly kept only as a break-glass fallback the same `FeedSource` can back (see §5, §12).
- **Read-only collection** — passive reading only; the engine never upvotes, joins, follows, comments, or DMs.
- **Discovery is deterministic + operator-seeded** — `campaign.md` lists the **subreddits** (`seed_channels`); the engine reads their recent submissions. No algorithmic feed to steer; the subreddit list *is* the relevance filter — the strongest, most predictable steering of the four text-first siblings. Re-nudging means editing the seed list.
- **Escalate only when unsure** — both relevance and match decisions run cheap-local first and ask the cloud model only on low-confidence cases.
- **SQLite is the only contract** between engine and panel (WAL mode; two processes meet at the DB).
- **soul.md + campaign.md** — config read only at session start.
- **Generic criteria & goal, fixed core record** — relevance/goal/extraction are free-text brief; every match lands in identical core columns plus a brief-defined `extracted` blob.

## 4. soul.md vs campaign.md (the generalization boundary)
- **soul.md** — engine identity + safety: read-only, pacing, halt-on-resistance, never act. Domain-free.
- **campaign.md** — seed direction (which **subreddits** to read), relevance definition, goal type, what to extract & score, threshold, language mix. Reddit-specific knobs: `seed_channels` (the subreddit names, e.g. `r/SaaS`, `r/projectmanagement`) and, optionally, `seed_hashtags` reused as per-subreddit **search queries** (`subreddit.search(q)`, YouTube-style, still scoped to the seeded subreddit so it can't drift off-niche).
- Test of "generic": swap `campaign.md` and the same binary runs a different hunt, no code change.

## 5. Architecture (target)
```
Engine (headless) ──► SQLite ◄── Admin panel (local web app)
        └─► model router ─► local text · local vision (MLX/Ollama) | cloud (OpenRouter)
```
- **Engine:** Python + the official Reddit Data API over OAuth2 (PRAW, or a thin `httpx` adapter against `oauth.reddit.com`). Iterates seeded subreddits, reads each submission's **title + self-text** (and, only for image/gallery/video submissions, decodes the post image(s)/thumbnail to base64 frames for the optional vision pass), walks the **nested comment tree**, runs the cascade against the active brief, writes matches + state.
- **FeedSource mapping** (every platform maps its primitives onto `Reel`/`Comment`):
  - **seed unit = a SUBREDDIT** — lives in `seed_channels` (Telegram-style deterministic seed), *not* `seed_hashtags`/`seed_accounts`.
  - **submission = `Reel`** (Reddit kind `t3`), and it is **every submission type** — text/self, link, image/gallery, **and** video — *not* video only. `Reel.reel_id` = the `t3` fullname/id36 (e.g. `t3_abc123`); `Reel.caption` = **title + selftext** (the primary text surface — title is always present, selftext on text posts); `Reel.author` = submission author; `Reel.on_screen_frames` = the post's image(s)/thumbnail as base64 JPEG **only for image/gallery/video posts** (empty for link and text posts); `Reel.ocr_text` filled by the optional vision pass. A text-only self-post is fully first-class and examined on `caption` (title + selftext) alone — vision never gates it.
  - **comment = `Comment`** (Reddit kind `t1`), and the **whole deeply-nested tree** is in scope, not just top-level. `Comment.comment_id` = `t1` id36 (the idempotency key); `Comment.username` = comment author; `Comment.text` = body; `Comment.is_reply` = `depth > 0` (Reddit exposes a `depth` field; top-level is depth 0). Each `t1` carries a `replies` Listing of more `t1`s plus `more`/`morechildren` placeholders for collapsed/deep branches.
  - **Content types examined:** text/self posts, link posts, image/gallery posts, video posts — **and every reply in the nested comment tree beneath each**. **Text-first, vision-optional**: text/link posts have empty `on_screen_frames` and are judged on text alone; the vision/OCR pass runs only on image/video posts. Audio not used.
- **`build_feed` wiring:** add an `if platform == "reddit":` branch to `feeds/__init__.py`, mirroring the YouTube/Telegram branches exactly — require `seed_channels` (raise `ValueError("reddit needs seed_channels (subreddits) in campaign.md — deterministic discovery, there is no algorithmic feed")`), build the client `RedditDataApiClient.from_credentials(credentials) if credentials else RedditDataApiClient.from_env()`, construct `RedditFeed(client=..., subreddits=tuple(seed_channels), queries=tuple(seed_hashtags))`, call `feed.attach()` (no-op symmetry hook; the OAuth token is minted lazily on first request, mirroring `YouTubeFeed.attach()` — `attach()` could optionally pre-warm one cheap call to seed the `healthy()`/401 canary, consistent with the empty-interception canary contract), and return. Remove `reddit` from the `NotImplementedError` fallthrough and add it to `SUPPORTED_PLATFORMS` in `config.py`. `include_home_feed` is ignored (Reddit has no algorithmic feed in scope).
- **Connection — per-org secret (Path A):** Reddit needs a **per-org connection**, a new **schema v9 `integration_secrets`** entry, mirroring YouTube + Telegram (v8). Store the operator's Reddit OAuth credentials Fernet-encrypted (`REELRADAR_SECRET_KEY`): minimally `client_id` + `client_secret` + a long-lived **refresh token** (read-only `read` scope), plus the Reddit-required descriptive `user_agent`. App-only `client_credentials` is preferred for public reads because it avoids storing an end-user account session, and is expected to fall under the same ~100 QPM/`client_id` ceiling (exact per-flow QPM as of 2026-06; verify before build); an authorization-code refresh token is the alternative when a real account context is needed. Add a `/api/connect/reddit` endpoint mirroring the YT/TG connect flow (operator pastes app credentials or runs the one-time OAuth dance; server stores the encrypted secret + marks the org connected); run-time surfaces **needs-reconnect** on 401/`invalid_grant`. *(Path B fallback would instead be "managed"/no-secret like Instagram — CDP attach to the operator's one warmed, logged-in Chrome, intercepting Reddit's own internal JSON; the same `RedditFeed` can back either backend, and the campaign/connection picks it.)*
- **Model router:** call sites — `classifyText` (relevance + match) and `classifyImage` (on-screen text of image/video posts). Reddit is text-first, so vision is secondary and audio is not used.
- **Store:** SQLite (WAL). Matches, state, status, spend. Carries the shared `platform` dimension (`reddit`).
- **Panel:** local web app reading/writing SQLite. Never calls the engine.
- **v2 shell:** OpenJarvis wraps the frozen engine as a scheduled skill.

**Access decision (Path A vs Path B).** Decided on the four criteria the engine actually cares about (all access claims as of 2026-06; verify before build):

| Criterion | Path A — official API (OAuth2 + PRAW) **[RECOMMENDED]** | Path B — CDP interception (warmed, logged-in Chrome) |
|---|---|---|
| Commercial-use ToS posture | Only path with a legitimate, licensable posture: apply, declare the lead-gen use case, operate inside an approved client. Lead-gen is unambiguously commercial. | Same commercial restriction, **no** legitimizing channel + an anti-scraping/ToS violation. |
| Robustness | Documented, versioned JSON; PRAW handles pagination / `more`-expansion / rate headers. No endpoint drift. | Undocumented internal JSON from a logged-in session behind Cloudflare managed challenges; the bare unauthenticated `.json` suffix is largely closed (403) and not a viable mechanism. Endpoint drift risk. |
| Multi-tenant key management | Clean sibling of YT/TG: per-org OAuth secret in a v9 Fernet-encrypted row + connect endpoint; `build_feed` gets `credentials`. | No per-org secret (managed-CDP like Instagram), but every org shares one warmed-Chrome identity + ban surface. |
| Volume | ~100 QPM/`client_id` is ample for bursty daytime subreddit reads (cap is per `client_id`, shared across that client's users). | N/A (no API cap), but throttled fresh accounts + challenge friction. |

**Decision:** commit to **Path A**. It is the closest sibling to Telegram/YouTube — deterministic seeding, first-class read API, per-org secret. Path B is the documented break-glass fallback only, never the default: it trades a survivable legal posture for fragility + higher enforcement risk with no upside on a text API. The single biggest open risk for *both* is that commercial lead-gen may require a paid contract/approval Reddit can decline — flagged in §12/§13, not handled in code.

## 6. Model routing
| Stage | Task | Where | Notes |
|---|---|---|---|
| Relevance gate | is this submission relevant to the brief? | Local text (title + selftext) → **local vision only for image/video posts** → **escalate-if-unsure → cloud** | text-first platform; most submissions judged on text alone; subreddit pre-filter already narrows the set |
| Match scoring | is this comment (at any depth) a match? | Local → escalate-if-unsure → cloud | runs across the whole nested tree, not just top-level |
| Vision / OCR | read price / project / text inside a post image or video frame | Local (Qwen2.5-VL 7B class) — **v1, secondary** | load-on-demand only for image/gallery/video posts, then unload; `on_screen_frames` empty for text/link posts |
| Audio / transcript | — | **not used** | Reddit is text-first; no transcript tier in v1 or v2 |

One model resident at a time (36GB cap). **Batch by stage** — run the text gate across submissions, then load vision once for the image/video-post minority, unload, then escalate the unsure remainder to cloud. Cloud tier needs fallback (retry queue or degrade-to-local + flag) and per-campaign spend logging.

## 7. State model (the spine)
Persisted in SQLite between sessions:
- **seen_submissions** — dedupe; forward-only watermark per subreddit (by `t3` fullname / listing `after` cursor).
- **comment cursors** — per submission/thread; "new comments since last poll" + how far each `more` branch was expanded.
- **watchlist** — match-rich submissions/threads, re-polled until aged out (~7–14 days).
- **session counters** — subreddits read, submissions seen, **already-seen skips**, relevance passes, matches, escalations, spend, API calls used.
- **feed-health flag** — set when a seeded subreddit returns mostly already-seen submissions (subreddit quiet / tapped out / stale).
- **client health flags** — last run, OAuth credential state, rate-limit (429) canary, API-returning-items canary.
- **match status** — keyed on `comment_id` (the `t1` id36); survives re-polls (re-poll never overwrites human status).

Writes idempotent on `comment_id`. Killed mid-run → resume from state. Listings paginate forward via the `after` cursor (fullname of the last child under `data.children`), so the seen-watermark is a simple per-subreddit forward marker.

## 8. Match record (schema)
`campaign_id, platform, subreddit, submission_id, comment_id, username, text, lang, score, reason, extracted, status, captured_at`
- Maps onto the shared core record: `subreddit` → the seeded source (`channel_id` slot), `submission_id` → the `t3` parent submission (the `reel_id` slot), `comment_id` → the `t1` comment scored.
- `score` / `reason` — interpreted per the active brief.
- `extracted` — brief-defined JSON, driven by the campaign's Extract input injected into the cascade contract (JSON mode); e.g. lead → subreddit/intent; partner → submission author; signal → topic.
- `status` ∈ { new, reviewing, confirmed, discarded }
- **Deletion propagation (ToS):** on re-poll, a comment that has vanished/been removed since last poll is flagged (and dropped from active display per the Developer Terms, as of 2026-06; verify before build) **without overwriting human-set status**. Retention-TTL / deletion-flagging is enforced as engine behavior plus a status side-channel (e.g. a `removed_at` soft-flag set on re-poll), **not** a new free-text core column — it does not extend the fixed-core record list above.

## 9. Failure handling (three tiers)
*Path A primary; Path B notes in brackets.*
- **Auto-skip + continue (transient, log only):** a single submission 404/gone (deleted/removed post), one comment-tree fetch fails, one `more`/`morechildren` expansion errors, a single JSON field missing. Skip the item, keep walking.
- **Soft flag (continue, surface on dashboard):** HTTP 429 / thinning rate-limit headers → back off per `X-Ratelimit-Reset` (PRAW auto-sleeps), flag if sustained; subreddit exhaustion/staleness (already-seen-skip ratio crosses threshold) → stale-subreddit flag; OpenRouter spend cap hit; cloud tier degraded → degrade-to-local + flag; a subreddit gone private/quarantined/banned → flag + drop from this run. *[Path B adds: Cloudflare soft-challenge / partial empty interception → back off + flag.]*
- **Halt + alert human (stop session, never auto-resolve):** 401 / `invalid_grant` (OAuth refresh token expired/revoked) → needs-reconnect; 403 (app not approved for commercial use / scope revoked / account or app suspended / commercial-use enforcement); repeated/global 429 indicating the `client_id` is throttled platform-wide; a Responsible-Builder/ToS enforcement notice. *[Path B adds: login expired, Cloudflare captcha/checkpoint/challenge, account or IP action-block, empty interception for N consecutive posts = endpoint drift.]* The agent **never** solves a captcha/checkpoint or re-mints credentials silently — it stops and alerts.

## 10. Pacing & steering/seeding
- **Warming (lighter than Instagram, Path A):** the "warming" work is *approving + aging the OAuth app/account*, not behavioral camouflage. Read-only API reads carry low ban risk; the real gate is the Responsible-Builder approval + staying under the rate cap. *[Path B: warm a real, aged, some-karma account by hand for weeks; fresh low-karma accounts are throttled.]*
- **Rate ceiling:** hold comfortably below **~100 QPM per `client_id`** (10-minute rolling average; ~60 QPM is reported for script/password and per-user authorization-code flows — exact per-flow QPM as of 2026-06, and Reddit's own rate-limit guidance ties the limit to the `client_id` rather than the grant type, so confirm before build). PRAW self-throttles off the `X-Ratelimit-*` headers — respect every back-off exactly (the Telegram `FLOOD_WAIT` analogue). Budget calls: ~1 listing call per subreddit page (~25–100 posts) + 1 `/comments` call per opened submission + extra `morechildren` calls **only on match-rich branches**. A bounded daytime burst lands far under the cap.
- **Cadence:** bursty **daytime** runs, ~1–3 sessions/day, 15–30 min, 20–50 posts/session; not a server, not 24/7. Bounded recent window per subreddit (`new(limit=N)` for freshness, `.hot`/`.top(time_filter=…)` for density), not full history. Randomized small inter-call delays remain good hygiene even on the API.
- **Steering/seeding (deterministic + recurring, strongest of the family):** relevance = the subreddit list in `seed_channels`, period — there is no opaque feed to fight and no "For You" algorithm in the loop. Re-steer by editing the seed list (re-read only at session start); optional per-subreddit search queries in `seed_hashtags` (`subreddit.search(q)`, still scoped to the seed). A rising already-seen-skip ratio (§7) signals a tapped-out subreddit → operator adds/prunes.

---

## 11. Scope: v1 vs v2

### v1 — Prove the loop (manual · headless)
- Manual OAuth app onboarding (operator brings their own approved Reddit app) + manual subreddit seeding via `campaign.md`.
- Official Reddit Data API over OAuth2 (PRAW or thin `httpx` adapter), `from_credentials` (per-org v9 secret) / `from_env` (single-tenant local), read-only.
- Seeded-subreddit read loop: walk recent submissions per subreddit (`new`/`hot`, optional `subreddit.search(query)`); dedupe on `t3` id; forward `after` cursor.
- **Relevance gate: title + selftext → on-screen text (vision/OCR) for image/gallery/video posts → escalate-if-unsure to cloud.** Text/link posts judged on text alone.
- Walk the **nested comment tree**: `replace_more` / `morechildren` expansion only on match-rich branches; `is_reply = depth > 0`; idempotent on `t1` id36.
- Comment cascade: local pre-filter → local scoring → escalate-if-unsure to cloud, across the whole tree.
- **Vision/OCR tier (secondary, image/video posts only). No audio.**
- SQLite store: full schema + state model + resume; carries `platform = reddit`.
- soul.md + campaign.md, with one real brief (lead-gen) as proof.
- Three-tier failure handling (auto-skip / soft-flag / halt-alert), including 429 back-off and 401/403 needs-reconnect/halt.
- **Session counters + stale-subreddit flag.**
- Pacing engine with rate-header compliance.
- Connection: schema v9 `integration_secrets` row for reddit + `/api/connect/reddit` endpoint; run-time needs-reconnect.
- **Panel — read surfaces:** matches table (filter/sort/status-mark), health/canary panel (incl. stale-subreddit flag + needs-reconnect), OpenRouter spend.
- Operation: operator triggers sessions manually. Follow-up: human, off-platform.

**v1 done =** an approved OAuth client reads seeded subreddits for weeks without rate/ToS enforcement · sessions complete and resume cleanly · matches land with validated precision against a hand-labeled set, on at least one real brief · the nested comment tree is walked (not just top-level) with selective `more`-expansion · stale-subreddit flag fires correctly · 429s are respected with no client throttling · 401/403 halts + alerts cleanly.

### v2 — Intelligence + automation
- **OpenJarvis integration:** frozen scraper as a scheduled skill; unattended **scheduled** (not continuous) mode; memory layer.
- **Panel — write surfaces:** campaign editor (writes campaign.md incl. `seed_channels` subreddits + optional `seed_hashtags` queries; engine re-reads only at session start); **review queue** (each confirm/discard captured as labeled training data → tunes thresholds + escalation; **not** fed to model training, per ToS); richer status workflow.
- **Memory:** optional Obsidian vault as accumulating market model.
- **Scale:** multi-campaign (parallel briefs); discovery feedback (suggest adjacent subreddits).
- **Path B break-glass:** the same `RedditFeed` backed by a managed-CDP adapter (warmed, logged-in Chrome over CDP intercepting Reddit's internal JSON), gated behind a connection flag, only if Path A access is lost — never the default.

**v2 done =** runs on a schedule with no babysitting · review-queue feedback measurably lifts precision · ≥2 distinct briefs run on the same engine with no code change.

---

## 12. Open questions
- **Self-serve credentials in 2026:** can a new org still self-serve OAuth credentials at `reddit.com/prefs/apps`, or is pre-approval under the Responsible Builder Policy now mandatory for any new app? Sources conflict (Nov-2025 crackdown vs a 2026 step-by-step guide still showing "Create App"). Verify before build — drives the connect-endpoint UX and onboarding gate. (uncertain, as of 2026-06)
- **Commercial approvability (biggest go/no-go):** is a commercial lead-gen use case approvable on the **free** tier, or does it force a paid/enterprise contract? What is the real current commercial price (is the ~$0.24 per 1,000 calls figure still accurate)? (pricing from training, hedge; verify before build)
- **OAuth flow / secret shape:** is read-only app-only OAuth (`client_credentials`) sufficient for all public reads, or is a per-user refresh token (authorization-code) needed for any target subreddit? Picks the secret shape stored in v9 `integration_secrets`. (resolve alongside the per-flow QPM question below)
- **Rate-window mechanics:** exact per-flow QPM (the 60-vs-100 split is reported inconsistently across third-party sources, and Reddit's own guidance ties the cap to the `client_id` rather than the grant type) and the 10-minute-window mechanics in 2026 — confirm against live `X-Ratelimit-*` headers; tune the bounded-window/session caps empirically.
- **Comment-tree depth:** how deep to expand nested trees by default (`replace_more` limit) — cost (extra `morechildren` calls against the QPM cap) vs coverage; expand only match-rich branches.
- **Deletion propagation:** confirm the re-poll + retention-TTL design (engine behavior + a `removed_at` soft-flag side-channel, not a core column) satisfies the Developer Terms — flag/stop using comments deleted between polls without overwriting human status.
- **Path B viability:** is the CDP fallback materially still viable in 2026 — a warmed, logged-in Chrome intercepting Reddit's internal JSON (the only form likely still viable), given the unauthenticated `.json` path is 403'd and behind Cloudflare managed challenges (one source dating the unauth 403 to 2026-05-30)? Or has it degraded to where the fallback is hollow? (uncertain; verify before relying on it)
- **Dependency surface:** PRAW as a hard dependency vs a thin `httpx` adapter against `oauth.reddit.com` (mirroring the youtube.py httpx pattern + the lazy-import Telethon pattern) — pick to keep the install surface small.
- **Per-`client_id` sharing:** if many orgs run under one operator `client_id` they share the ~100 QPM — do orgs each bring their own approved Reddit app, or pool under one? Affects key management + rate budgeting.
- Retention TTL for stored personal data; GDPR/CIS posture; how far to schematize the `extracted` blob vs leave it free-form per brief.

## 13. Risks
| Risk | Mitigation |
|---|---|
| Commercial lead-gen not approvable on the free tier / requires a paid contract Reddit can decline or price out (Responsible Builder Policy + Developer Terms restrict commercial use + auto-lead-generation; as of 2026-06, verify) | Treat Reddit access as an onboarding **gate**: operator applies with the declared lead-gen use case and brings their own approved app; engine stays read-only + human-led follow-up (no automated outreach); document the contract requirement; do not ship until approval status is known |
| ToS / AI-training prohibition + deletion-propagation breach | Never feed collected Reddit data to model training; re-poll flags vanished/removed comments without overwriting human status; retention TTL; use only for the campaign's stated purpose |
| New-app self-service credential issuance may be closed (Nov-2025 crackdown) so a new org can't get a key | Connect endpoint accepts operator-supplied app credentials (BYO approved app) rather than minting; support both `client_credentials` and refresh-token; surface needs-reconnect clearly; verify the 2026 self-serve reality before build |
| OAuth token expiry/revocation mid-run (401 / `invalid_grant`) | Halt + alert human + needs-reconnect flag; never silently re-mint; store refresh token encrypted (v9 Fernet); `from_credentials`/`from_env` symmetry with YT/TG |
| ~100 QPM/`client_id` exhaustion (shared across orgs or deep comment-tree expansion) | PRAW self-throttle on `X-Ratelimit-*` headers; expand `more`/`morechildren` only on match-rich branches; bounded recent window per subreddit; soft-flag sustained 429; consider per-org BYO app to isolate budgets |
| Path-B fallback hollow: unauthenticated `.json` blocked/403 + Cloudflare managed challenges defeat scraping | Keep Path A primary; Path B only break-glass via warmed, logged-in Chrome over CDP reading internal JSON (interception, not the bare `.json` suffix and not crafted calls); halt + alert on any captcha/checkpoint, never auto-solve; empty-interception canary |
| Subreddits go private/quarantined/banned or stale/off-niche | Already-seen-skip ratio → stale-subreddit flag → operator adds/prunes `seed_channels`; drop unavailable subreddits from the run + flag; subreddit list is the deterministic relevance filter |
| Relevance judged half-blind on media posts | Subreddit pre-filter + title/selftext primary text + optional vision/OCR on image/video posts only (`on_screen_frames` empty for text/link) + escalate-if-unsure to cloud |
| Local model weak on Uzbek/Russian comment text | Cloud escalation on both relevance gate and match scoring + hand-labeled validation set per brief |
| Shared-file concurrency between engine and panel | SQLite WAL; config read at session start only; match status keyed/idempotent on `t1` comment_id; killed mid-run resumes from state |
