# Campaign Lab — research, benchmark & evaluate campaign inputs before launch

**Status:** Sheets #1 and #2 are BUILT (see the "as built" sections under each);
Sheet #3 is researched and confirmed but not yet built; #4-#5 are still open
research. This doc is the durable record of the research so each build can start
cold.
**Researched:** 2026-08-20 (12-agent ultracode study + 3-agent deep-dive per input).
Endpoint/ToS claims below carry that date — re-verify anything marked ⚠️ before coding
against it.
**Decision-ready summary artifact:** https://claude.ai/code/artifact/9154f31d-b055-48e3-954c-abb00f78fb8c

## Why this exists

Campaign creation today accepts guessed inputs. The AI-generate flow asks the LLM to
invent seed hashtags and accounts from parametric memory (`core/prompts.py:80-81`),
string-joins them (`campaign_gen.py:500-502`), `str()`-coerces them
(`core/config.py:421-423`), and nothing anywhere checks they exist before a warmed
browser navigates to them. Entity-recall hallucination base rates are 30–45%. In the
2026-08-19 live run **all six Instagram hashtag sources 302-redirected** (detected at
`engines/instagram/cdp.py:342-350`, recorded nowhere but a log line).

The umbrella study (three competing designs, two adversarial judges) converged on an
MVP-first architecture in four stages — attribution & negative capture → seed
verification waterfall → prompt benchmark → closed loop — at ~9–13 engineering days,
≤$1 LLM + 3–5 min browser per campaign creation. This doc then goes deeper: **one
remedy sheet per generated input**, each researched to exact mechanisms. Sheet #1
(hashtags/search terms) is complete; the rest are pending.

- [x] **#1 Hashtags & search terms** — researched below, **shipped** (part 1 of 3)
- [x] #2 Seed accounts — researched, **shipped** (part 2 of 3)
- [~] #3 Match prompt (+ threshold) — researched; part 3 of 3 PARTIALLY built (step 1 plumbing + step 2's statistics half; the gold set and everything measured against it are open)
- [x] #4 Vision prompt — researched 2026-08-21, NOT built →
      [`campaign-lab-sheet-4-vision.md`](campaign-lab-sheet-4-vision.md)
- [x] #5 Everything else (languages, extract fields, relevance def) — researched
      2026-08-21, NOT built →
      [`campaign-lab-sheet-5-fields.md`](campaign-lab-sheet-5-fields.md)

## Machinery we already pay for and throw away (found by the codebase audit)

These are the enablers every remedy plugs into:

1. **Per-source yield accounting** — `walk()` computes `from_source` per tag/account
   every run (`core/cdp.py:563-591`) and drops it at a debug line (`:616-617`). The
   302 keyword-search redirect verdict lands at `core/cdp.py:546-551`. Promoting that
   epilogue into a `source_stats` row is a ~5-line change with all inputs in scope.
2. **`Reel.source` provenance** — stamped at `core/cdp.py:515-519`, never leaves the
   file. No `source` column on `seen_reels` (`core/store.py:158-174`) or `matches`,
   so "which seed produced this lead" is unanswerable. Add via the additive-column
   idiom (v18/v19 precedent, see `SCHEMA_VERSION` note at `store.py:37`), threading
   through `engines/instagram/session.py:565-574` (`store.mark_seen`) and the X/
   LinkedIn twins.
3. **A labeled caption corpus already in SQLite** — `seen_reels` holds
   `caption + relevant` per campaign; read surface exists:
   `store.reels(campaign_id, only_relevant=True)` (`core/store.py:2605-2626`). The
   hashtag-extraction regex is already written twice — used only to *delete* tags:
   `engines/instagram/cascade.py:37` and `engines/warming/executor.py:56`.
4. **A proven search→gate→cap pipeline to copy** — Telegram warming:
   `derive_keywords` (`engines/warming/telegram.py:92-106`, cap 6), read-only search
   with over-fetch slack (`:171-184`), fail-closed LLM gate on untrusted hits
   (`:186-192`, `tg_relevance.py:57-99`, degrades to skip with no key). Reuse this
   shape for all seed vetting.
5. **Warming sessions as free scouts** — `WarmingSession` walks the same `_sources()`
   (same seed tags) and already tokenizes captions (`executor.py:56-67, 109-117`).
   Constraint: warming is forbidden from router/LLM/spend calls
   (`engines/warming/session.py:11-13`) — any scout hook must stay heuristic-only.
6. **Discarded response fields** worth parsing when touched anyway: IG
   like/comment/play counts (`core/parsers.py:89-92` tests key presence, drops
   values); YouTube `pageInfo.totalResults` + `publishedAt`
   (`engines/youtube/feed.py:199-216`); Reddit `score`/`num_comments`/
   `subreddit_subscribers` (`engines/reddit/feed.py:373-379`); X
   `entities.hashtags[]` pre-parsed and dropped (`engines/x/parsers.py:112-124`).
7. **IG search-box typing already exists once** — `share_reel` finds the search input
   and types (`engines/instagram/cdp.py:602-616`). `HumanSim` has no typing helper;
   a typeahead probe would lift this pattern and add jittered typing to
   `core/human.py`.

---

# Remedy Sheet #1 — Hashtags & search terms  ✅ researched, confirmed direction

`seedHashtags` doubles as search queries (YouTube `search.list`, Reddit
per-subreddit search, X search URL). There is no separate "search query" field.

## Structural facts that shape the design (2026-08 research)

- ⚠️ **Reddit's unauthenticated `.json` API is dead** (~May 2026; verified: 403 on
  all `.json` paths while HTML returns 200). Free options: grandfathered
  pre-Nov-2025 OAuth apps (100 QPM) or logged-in browser HTML (ToS Rule 8 risk —
  surface to operators). New OAuth apps need manual approval ("Responsible Builder
  Policy", Nov 2025).
- **LinkedIn hashtag pages are dead** (removed Oct 2024; no feeds, no follower
  counts). Hashtags there are just search keywords → invest in query *phrasing*.
  Content search: `/search/results/content/?keywords=` under the commercial-use
  limit (~250–350 searches/mo, hidden counter). Never replay Voyager XHR.
- **Instagram `media_count` survives in the API** even though the UI dropped it:
  typeahead `GET /api/v1/web/search/topsearch/?query=%23tag` returns
  `{name, id, media_count, search_result_subtitle}`;
  `GET /api/v1/tags/web_info/?tag_name=` returns `media_count` (needs login cookies
  + header `x-ig-app-id: 936619743392459`; treat 403 as *session-health* signal).
  Related-tags endpoint is dead; hashtag *following* removed Dec 2024; hard
  5-hashtags-per-post limit since Dec 2025. Banned tag = "page isn't available";
  restricted = "recent posts hidden" banner. Safe read pace: ≤1 req/10s
  (Instaloader encodes 199 api/v1 req/1800s); reads don't trip write action-blocks.
- ⚠️ **YouTube quota model changed June 2026**: `search.list` bills to its own
  bucket — **100 searches/day** — separate from the 10k-unit pool. `videos.list`
  with 50 IDs = 1 flat unit.
- **pytrends is dead** (archived 2025-04). Maintained successor: `trendspy` (pip,
  no key). Official Google Trends API is waitlist-only alpha.

## Remedy A — LLM proposes *nouns*, oracles expand them (off-account, free)

1. **Autocomplete mining** — `https://suggestqueries.google.com/complete/search`
   — free, keyless, live-verified 2026-08-20. `ds=yt` → YouTube; `client=firefox`
   → clean JSON; `client=chrome` → adds `google:suggestrelevance` scores (free
   ranking signal); `hl=uz|ru&gl=UZ` → what real local users type. Recipe: seed +
   each letter (Latin + Cyrillic alphabets) + question prefixes ("qanday", "как",
   "how") ≈ 30 GETs → 200+ real queries per seed. Replicates ~80% of
   AnswerThePublic for $0. Unofficial: backoff on 429/503.
2. **Demand-side request-pattern matrix** — buyers write "any videographer recs?"
   / "videograf kerak" / "кто снимает свадьбы?", not marketing tags. A fixed
   multilingual pattern list (looking-for / recommend-me / how-much / kerak /
   посоветуйте / worth-it …) × LLM nouns → the search strings for X, Telegram,
   Reddit, LinkedIn — and the vocabulary the match prompt should expect in
   comments. (The GummySearch/Linkeddit product category validated
   "actively-asking ranks above keyword-mention".)
3. **Uzbek script fan-out** — `UzTransliterator` (pip; arXiv:2205.09578) emits
   old-Cyrillic / official-Latin / 2021-reformed-Latin; also collapse apostrophe
   variants (`oʻzbek`→`ozbek`) since hashtags do. Validate which script real users
   type via the `hl/gl` autocomplete above.
4. **Co-occurrence ledger (compounding, zero requests)** — mine co-occurring
   hashtags from `seen_reels` captions, weighted by the stored `relevant` label;
   rank by lift (co-count ÷ global frequency). Hook: `co_occurring_tags(caption)`
   in `core/parsers.py`, called offline over `store.reels(cid, only_relevant=True)`
   or at enqueue in `_enqueue_reel` (`core/cdp.py:501-519`). Warming runs feed the
   same ledger heuristic-only.

## Remedy B — Rank by real demand (off-account)

`trendspy` interest-over-time + related-queries with `geo='UZ'`; **Yandex Wordstat
API** (`api.wordstat.yandex.net` — `/v1/topRequests`, `/v1/dynamics`; free with
OAuth) for *exact* Russian query volumes — strongest CIS signal; autocomplete
relevance as tiebreaker; SerpApi free tier (100/mo) as fallback. YouTube publishes
no search volume — every tool's number is a model; use suggest-relevance +
result recency instead.

## Remedy C — Validate the shortlist per platform

| Platform | Validator | Cost | Risk |
|---|---|---|---|
| YouTube | `search.list` (order=date, publishedAfter=now-30d, part=id) + one `videos.list` (statistics,snippet) → recency, views, commentCount (missing key = comments off). Never `search.list` for volume alone. | 1 search-bucket call + 1 unit per term | none (official) |
| Telegram | `t.me/s/<channel>` unauthenticated preview → subscribers, recency, per-post views (verified live). t.me has **no search** — discovery via Telemetr.io free tier (1,000 req/mo, Stripe) or Lyzem/Telegago. TGStat is RUB-only, no foreign cards. | free | zero for `/s/`; MTProto only on aged real-SIM accounts, cache entity ids, never rotate |
| Instagram | One `topsearch` typeahead call per tag from the warmed session (existence + media_count + near-matches in one call); fold banned-banner/empty-grid detection into normal runs; static banned-list (e.g. MetaHashtags) as free prefilter. Graph API `ig_hashtag_search` is useless for scoring (no media_count, 30 tags/7 days). | 1 call/tag, ≥10s apart | low at that pace |
| Reddit | Term-in-subreddit search behind grandfathered OAuth (`restrict_sr=1`, `sort=new`; retry `sort=relevance&t=month` — empty `new` under load is a false negative). No free anonymous path. | free if grandfathered | ToS decision for operator |
| X | No pre-validation worth the exposure (login-walled, attributable, no result counts, doc_ids rotate). Validate passively via per-source yield during runs. | — | keep zero |
| LinkedIn | Keyword phrasing (Remedy A), content-search sparingly under CUL, render-don't-replay. | quota-bound | highest (logged-in = UA breach; hiQ settlement) |

## Remedy D — Persist what runs already know (the enabler)

`source_stats` table fed from `walk()`'s epilogue; `source` column on
`seen_reels`/`matches`; park dry tags (3 sessions AND ≥30 reels AND 0 relevance
passes; never below 2 active sources); record `banned_at` on banner detection;
append productive/dead lists to the synthesis prompt in `_build_user_message`
so the generator stops re-proposing known-dead tags.

## Build order (hashtags alone)

1. Persist yield + redirect verdicts; caption co-occurrence miner (~2 days, no
   external I/O).
2. Autocomplete expansion + transliteration + request-pattern matrix in
   `campaign_gen` (~2 days, free endpoints).
3. Validators: YouTube + Telegram (safe tier), IG typeahead probe (warmed tier)
   (~2 days).
4. trendspy/Wordstat ranking + banned-list prefilter (~1 day).

## Sheet #1 — as built

Schema **v24** and the `engine/aizu/discovery/` package. What each build step
became, and what deliberately did not get built:

**Step 1 — attribution (Remedy D).** `source_stats` table keyed on the SEED TERM
(not the URL); `seen_reels.source` + `matches.source`; `Store.record_source_walk`
/ `source_stats` / `park_dry_sources` / `live_seeds` / `parked_sources` /
`unpark_source` / `seed_history`. `walk()` reports a `SourceOutcome` per source
through `FeedSource.on_source_done` — inside a `try/finally`, because the caller
abandons the generator the moment it hits its lead target, i.e. on the most
productive source. `_source_unavailable()` lifts each engine's already-written
`_page_unavailable` DOM probe out of `open_reel`, so a dead SEED (not just a dead
reel) is detectable and skips the ~45s of empty scrolls it used to burn.
Attribution reaches `matches` by derivation from `seen_reels`, not by threading a
kwarg through six engines. `Reel.source` carries the SEED TERM, not the URL it was
originally stamped with: it is written straight through to `seen_reels.source`,
which `source_stats` joins on, so a URL stamp makes that join silently return zero
for every seed (caught by an end-to-end smoke run, not by any unit test). Operator surface: `aizu sources`. Generator feedback:
`Store.seed_history` → `campaign_gen._seed_feedback_block`.

**Step 1b — the miner (Remedy A.4).** `core/parsers.extract_hashtags` /
`co_occurring_tags` (the canonical Unicode-aware extractor — the two existing
regexes both exist to *delete* tags) and `core/tagmine.py`, which ranks
co-occurring tags by the Wilson lower bound of their relevance rate against the
campaign's own base rate. Lift, not frequency: the most frequent co-occurring tag
in any niche is the generic one.

**Step 2 — expansion (Remedy A.1-A.3).** `discovery/translit.py`,
`discovery/patterns.py`, `discovery/autocomplete.py`, `discovery/expand.py`. The
generator prompt now asks for `seedNouns` (bare nouns in the audience's language)
and the oracles expand them. Two guardrails the research implied but did not
state: the transliteration fan-out only fires for briefs that declare a
Cyrillic-script language (otherwise "marathon" becomes "маратҳон"), and a brief
with NO declared language gets no locale guessing at all.

**Step 3 — validators (Remedy C).** `discovery/validate.py`:
`YouTubeTermValidator` (search-bucket-budgeted, recency-window) and
`InstagramTagProbe` (one `topsearch` typeahead per tag from the warmed session,
paced ≥10s, treating 401/403/429 as a verdict about *our session* and stopping).
`UNKNOWN` is a first-class verdict and keeps the term — "we could not check" must
never be presentable as "this is fine". Not built, on purpose: Reddit (needs a
grandfathered OAuth app — an operator credential decision), Telegram (t.me has no
search; the unit to validate there is a channel → Sheet #2), X and LinkedIn (the
research says the correct amount of probing is zero).

**Step 4 — prefilter (Remedy C).** `discovery/banned.py`, zero requests, runs
before any validator. Ships a small verifiable seed list plus a generic-word list
and reads an operator list from `AIZU_BANNED_TAGS_FILE`; a large scraped
blocklist is deliberately NOT vendored, because an inaccurate one silently
deletes working seeds — a worse failure than the one it prevents. **Deferred:**
trendspy / Yandex Wordstat demand ranking (Remedy B) — both need credentials or a
new dependency, and autocomplete relevance already supplies a usable tiebreaker.

**Deliberately excluded:** RiteKit/Flick/IQ-Hashtags (UI products, weak APIs);
IG related-tags endpoint (dead); X API (~$200/mo, nothing needed); scraping
Display Purposes; PhantomBuster-style cookie-injection (their own docs show the
risk model; TexAu retired LinkedIn automation entirely late 2025).

---

# Remedy Sheet #2 — Seed accounts  ✅ researched, confirmed direction

A good seed for this engine is an account whose **comment sections contain
buyers** — not a big-follower account. No commercial tool scores that (verified
gap 2026-08: HypeAuditor/Modash score audience authenticity, nobody scores
buyer-question density in comments) — it can be our moat.

## Bugs found during the audit — fix regardless of the lab

1. **LinkedIn: both documented seed formats produce dead URLs.** The panel
   placeholder says `'in/jane-doe, company/acme'`
   (`admin-panel/src/features/campaigns/useCampaignForm.ts:70`) but the URL
   builder does `str(acct).lstrip("@").strip("/")` into `.../in/{slug}/...`
   (`engines/linkedin/cdp.py:38, 86-87`) → `/in/in/jane-doe/` and
   `/in/company/acme/`. Company pages need `/company/{slug}/posts/`. No test
   covers `LinkedInFeed._sources()`.
2. **YouTube: a handle (`@name`) instead of a UC-id crashes the run** — 400 from
   `raise_for_status()` (`engines/youtube/feed.py:195`) is not a
   `YouTubeApiError`, escaping the catch in `youtube/session.py:177-183`.
   Nonexistent UC-id silently returns 0 videos (indistinguishable from quiet).
3. **Reddit: one private/quarantined seed subreddit disconnects the whole org
   integration** — the 403 matches `cli._is_auth_error` (`cli.py:253-266`) →
   `_flag_needs_reconnect` (`cli.py:269-281`).
4. **Telegram: one dead `@username` kills the whole session** — resolution error
   raised inside `feed.walk()` generator (`engines/telegram/feed.py:77-84,
   284-287`), no per-channel try/except → crash-guard halt.
5. **All-dead seeds = silent zero.** Any non-empty seed list turns the home feed
   OFF (`core/config.py:197-210`), so a campaign whose seeds are all dead walks
   N dead URLs, harvests nothing, and reports `completed`. IG/X/LinkedIn burn
   nav + 4 empty-scroll rounds ≤45s per dead account per session
   (`core/cdp.py:88, 130-138`), forever — no flag, no DB row.

## Remedy A — Mine our own data first (queryable TODAY, zero schema change)

Authors of relevant posts are already persisted: `seen_reels.author` + the
`relevant` label written by every engine (`core/store.py:165, 1546-1575`).
`SELECT author, COUNT(*) FROM seen_reels WHERE campaign_id=? AND relevant=1
GROUP BY author` works right now on IG and X; a join to `matches` on
`(campaign_id, platform, reel_id)` gives "authors whose posts produced actual
leads" — the best possible seed candidates, from proof. Commenter→author
bipartite overlap is also computable (`matches.username` × `seen_reels.author`;
add an index on `matches.username` first). Nothing aggregates any of this today.

Per-platform author fixes so mined authors are *seed-shaped* (each field is in
the payload already, currently unread):
- YouTube: keep `snippet.channelId`, not `channelTitle`
  (`engines/youtube/feed.py:209-215`).
- LinkedIn: keep `actor.navigationContext.actionTarget` (canonical profile URL),
  not display name (`engines/linkedin/parsers.py:57-62`).
- Telegram: keep the real `TgMessage.sender` instead of `author = channel`
  (`engines/telegram/feed.py:82-83, 298-307`).

## Remedy B — Buyer-density score (the moat metric)

For a candidate seed: sample first-page comments of last 5–10 posts →
**price-intent question rate per 100 comments** — regex+LLM over multilingual
patterns ("narxi?", "qancha?", "сколько стоит", "как заказать", "price?",
"how much") — plus comment/like ratio and owner reply-rate (commerce-active
signal). Use HypeAuditor's published two-factor rule to discount pods: a comment
is inauthentic only if the *account* is low-quality AND the *content* is
low-value. Context: IG comment volume fell ~16% in 2025, so a price-question-rich
comment section is an even stronger outlier.

## Remedy C — Anonymous prescore, one request per candidate

| Platform | One-call validator | What it returns |
|---|---|---|
| Instagram | `GET i.instagram.com/api/v1/users/web_profile_info/?username=<u>` with `x-ig-app-id: 936619743392459` (from the warmed session it's the same XHR the web app fires — lowest-risk read; single-digit reads/min, jittered; stop on 401/429 — those flag *our* session, not the seed) | follower/following/media counts, `is_private`, `is_business`, `category_name`, bio, external_url, numeric `id`, **last ~12 posts with like/comment counts + timestamps** (public accounts), and `edge_related_profiles` when logged in. 404 = dead; 200+`is_private` = size-scoreable only |
| YouTube | `channels.list` — **50 channel IDs per 1 unit** (statistics, contentDetails, brandingSettings) + `playlistItems.list` on the `UU…` uploads playlist (1 unit; NEVER `search.list` = 100 units) + batched `videos.list?part=statistics` (1 unit/50 ids) | subs/videos/views/country/age; latest uploads with dates; per-video commentCount. **~13 units fully scores 10 channels** (0.13% of daily quota). Empty `items[]` = dead |
| Telegram | `t.me/s/<channel>` (free, unauth) + TGStat/Telemetr free stat pages | subscribers, per-post views/reactions/forwards, recency, forwarded-from; a **"Comments" bubble under posts = linked discussion group exists** — i.e. the channel HAS reachable commenters (categorically better seed). TGStat/Telemetr add reach/ER/growth free |
| Reddit | grandfathered OAuth `/r/<sub>/about` (100 QPM) | subscribers, active_user_count, created_utc, type; `/new` for cadence + num_comments. No free anonymous path since May 2026 |
| X | passive read of the profile page's own `UserByScreenName`/`UserTweets` GraphQL responses in the warmed session (never hardcode queryIds — they rotate 2–4 wks) | followers, post cadence, per-post reply counts, `rest_id`. Prefer discrete profile reads over search sweeps |
| LinkedIn | **Company Pages + search snippets only** — never open personal profile URLs to validate (CUL + detection; ~50 direct-profile/day vs ~2,000 search-result rows/day, ~150 detailed actions/24h ceiling) | company follower count, headcount, industry, recent posts with reaction/comment counts |

**Liveness gate** (aizu policy, synthesized): last post ≤14d AND ≥3 posts/30d AND
follower/following > 2 AND ER within published tier band (IG nano 4–6%, micro
2–4%, mid 1.5–3%; account-level median ~0.45% — treat >1% as above-average) AND
comment/like ratio ≥ ~1–2%. Cost: one request per candidate, zero warmed-account
exposure on YT/TG/Reddit.

**Key rule: persist the stable numeric ID as the seed's primary key, never the
handle** — IG `id`, YouTube `UC…`, X `rest_id`, LinkedIn URN. Renames then read
as "same seed, new handle"; a 404 on the *ID* is the true death signal. Telegram
is the exception (public surface exposes no stable id — treat renames as new
seeds); Reddit sub names are immutable.

## Remedy D — Expansion (lookalikes from proof, not guesses)

1. **Passive chaining capture** — `edge_related_profiles` arrives in profile
   XHRs the warmed session already fires while browsing; capture it instead of
   issuing calls. Alternative with zero account risk: anonymous
   `web_profile_info` (~200 req/hr/IP off-session) or Apify related-profiles
   actors at ~$0.01/profile, ~20 suggestions/seed, recursive.
2. **Telegram forward-graph** — channels a good seed forwards from are
   same-niche by construction; forwarded-from headers are public on `t.me/s/`.
3. **Co-commenter overlap** — compute from our own `matches`×`seen_reels` data;
   normalize as probability multipliers (SubredditStats formula: P(commenter of
   A also in B) ÷ baseline), NOT raw overlap — raw overlap over-ranks giant
   generic accounts. Commercially unclaimed on IG/YT.
4. **Uzbekistan discovery** — TGStat (`uz.tgstat.com`, API from ~$25/mo) and
   Telemetr.io (free tier 1,000 req/mo) categorized channel catalogs — Telegram
   reaches 76% of the UZ internet audience; Google dorks
   (`site:instagram.com "Toshkent" narxi <niche>` — IG posts index in Google
   since 2025); YouTube `search.list regionCode=UZ&relevanceLanguage=uz`.
5. **Cross-platform identity mapping** — parse link-in-bio pages
   (Linktree/bio.link) to enumerate the same seed's other-platform handles
   (self-asserted, reliable); Sherlock/Maigret only prove a handle is taken.

## Remedy E — Runtime hardening + seed lifecycle (reuse account-pool machinery)

- **`_source_dead` hook** in `walk()`'s per-source loop (`core/cdp.py:551-659`),
  slotting between the existing `_source_redirected` and `_login_wall_reason`
  hooks; the `_page_unavailable` DOM probes are already written per platform
  (`instagram/cdp.py:413-425`, `x/cdp.py:123-131`, `linkedin/cdp.py:112-120`)
  but only reachable from `open_reel` today.
- **`_classify_account` third branch** in `_classify`: profile XHRs (follower
  counts, `is_private`, verified) already reach `_classify` and are dropped
  (`instagram/cdp.py:123-152`); harvesting them costs zero extra requests.
- **Seed lifecycle copied from the accounts machinery**: state machine
  (`core/accounts.py:22-74` pattern: `unverified → live → dry → dead`),
  derived-not-stored quality score (`core/warmth.py` pattern, incl.
  `neutral_default` so unmeasured seeds aren't punished), exponential backoff
  for dry seeds (`session_cooldowns` pattern, `store.py:2047-2106`), and
  `health_flags` soft rows (`seed_dead`/`seed_dry`) for operator visibility.
- **Pre-flight resolve/normalize in `dispatch.build_feed`**
  (`dispatch.py:25-136`) — the one place all seeds are known before browsing:
  fix LinkedIn slugs, resolve YouTube `@handle`→`UC…`, wrap Telegram per-channel
  errors, exempt seed-404s from Reddit's `_is_auth_error`.
- **Vetting gate**: reuse Telegram warming's metadata-only LLM gate shape
  (`engines/warming/tg_relevance.py:57-99` — judges username+title+size vs
  campaign goal, fail-closed on untrusted, degrades without key) for all
  platforms' candidate seeds.

## Build order (seed accounts alone)

1. **Bug fixes** (LinkedIn slugs, YouTube handle crash, Reddit 403 misclass,
   Telegram per-channel guard, all-dead-seeds health flag) — ~1–2 days,
   pays back instantly.
2. **Mine matched-post authors + fix author fields to be seed-shaped; Sources
   aggregation query + index on `matches.username`** — ~1–2 days, zero new I/O.
3. **Prescore validators (IG web_profile_info, YT channels/playlistItems,
   t.me/s/) + liveness gate + stable-ID keying in `seed_probes`** — ~2–3 days.
4. **Buyer-density scorer + seed lifecycle states + dry-seed backoff** — ~2 days.
5. **Expansion: passive chaining capture, forward-graph, co-commenter
   multipliers, TGStat/Telemetr for UZ** — ~2–3 days, staged.

## Sheet #2 — as built

Schema **v25**, four engine bug fixes, and three more modules under
`engine/aizu/discovery/`.

**Step 1 — the five audit bugs.** All five are fixed, each at the engine boundary
rather than in `cli`, which turned out to matter: classifying a bad seed where the
platform knowledge lives means `cli._is_auth_error` needed no change at all.
- LinkedIn (`engines/linkedin/cdp.seed_activity_url`): both documented seed
  formats built dead URLs. Company/school/showcase pages now use
  `/{kind}/{slug}/posts/`; people use `/in/{slug}/recent-activity/all/`. Accepts
  bare slugs, `@`-prefixed, path forms and pasted full URLs. `_sources()` had zero
  coverage and now has a test pinning its ORDER, since `_source_seeds` labels
  sources by position.
- YouTube: new `YouTubeSeedError` (400/404), deliberately NOT a `YouTubeApiError`
  so a bad seed skips instead of halting the run; `resolve_channel()` turns a
  `@handle` into a `UC…` id in `attach()` (1 quota unit, cached, never
  `search.list`). An unresolvable seed is dropped and recorded `unavailable`
  instead of silently returning zero videos forever.
- Reddit: new `RedditSeedError` (403/404). Every `_get` path is `r/<sub>/…`, so
  those name the SUBREDDIT — one private seed was disconnecting the org's whole
  integration through `_is_auth_error`. 401 still raises raw with `.response`.
- Telegram: per-channel `try/except` in `walk()`. Session-level failures
  (AuthKey/SessionRevoked/FloodWait/Unauthorized, matched structurally so telethon
  stays unimported) still propagate; everything else skips that channel. Partial
  yields before a mid-iteration failure are kept.
- All-dead seeds: already covered by Sheet #1's `seeds_all_dead` flag in
  `Store.live_seeds`, plus every dead seed now lands in `source_stats`.

**Step 2 — mine our own data (Remedy A).** `Reel.author_id` + `seen_reels.author_id`
(v25) carry the STABLE id alongside the display name: IG `user.pk`, X's AUTHOR
`rest_id` (not the tweet's own top-level one — a trap worth a test),
LinkedIn `actor.navigationContext.actionTarget`, YouTube `snippet.channelId`,
Telegram the `@channel`. Every one was already in the payload and discarded.
`Store.seed_candidates()` ranks accounts whose posts this campaign already judged
relevant, leads first — proof beats signal — grouped on the stable id so a rename
stays one candidate. `Store.co_commenter_overlap()` computes the
`matches.username` x `seen_reels.author` bipartite join. Operator surface:
`aizu seeds`.

  Note on the overlap metric, learned the hard way: defining "our commenters" as
  every lead in the campaign makes the set include the author's own commenters, so
  every author trivially overlaps 100% and the metric says nothing. Shared means
  shared with a DIFFERENT author, and it is reported as a share of that author's
  own audience, never as a raw count — raw overlap ranks giant generic accounts
  first purely because they are large.

**Step 3 — prescore (Remedy C).** `discovery/prescore.py`: `InstagramProfileProbe`
(`web_profile_info` from the warmed session, paced, treating 401/403/429 as a
verdict about OUR session), `YouTubeChannelProbe` (50 channels per quota unit via
`channels.list` + the `UU…` uploads playlist for cadence — never `search.list`),
`TelegramPreviewProbe` (`t.me/s/`, free and unauthenticated, and the only place
the "has a linked discussion group" signal exists). Plus `liveness_gate()`. The
rule that shapes all of it: a signal the platform did not report is UNKNOWN, never
zero — a channel hiding its subscriber count must not read as having none, and an
unreachable probe must never read as a pass. Not built, on purpose: X and
LinkedIn (the research says the right amount of probing is zero), Reddit (no free
anonymous path since ~May 2026).

**Step 4 — buyer density (Remedy B), the moat metric.**
`discovery/buyer_density.py`: price-intent questions per 100 comments, with the
supply side subtracted. Regex-first across uz-Latin / uz-Cyrillic / ru / en, not
LLM-first — these are 3-to-6-word fragments where a fixed table is cheaper and
more reproducible than a model call, and the score is computed in code from
counted criteria for the same reason Sheet #3 refuses a model-verbalized scalar.
The discriminator between a buyer section and a vendor pit is DIRECTION, not
keyword: `narxi 500000 dan boshlab` is a seller, `narxi qancha? +998…` is a buyer
leaving a callback number. The weights are an explicit starting point to be tuned
against real per-seed lead history once `source_stats` has enough of it.

**Deferred from Sheet #2** (not started): the seed lifecycle state machine and
dry-seed exponential backoff (Remedy E) — `source_stats`'s park/ban verdicts
already cover the load-bearing part; and all of Remedy D's expansion work
(passive `edge_related_profiles` capture, the Telegram forward-graph, TGStat/
Telemetr catalogs, link-in-bio cross-platform mapping).

**Deliberately excluded:** logged-in `discover/chaining` API calls from warmed
accounts (passive capture gives the same data); follower-list pulls (real
detection minutes, marginal value); HypeAuditor/Modash subscriptions (score the
wrong thing for us); Sherlock-style username sweeps as identity proof (collision
-prone — link-in-bio parsing instead); LinkedIn personal-profile validation
visits (CUL + highest detection risk).

# Remedy Sheet #3 — Match prompt & threshold  ✅ researched, confirmed direction

## The disease (verified in code)

- **The design itself is the anti-pattern.** Free-text `matchDef` + model-emitted
  0..1 score + numeric threshold is exactly the "holistic scalar" pattern the
  2025-26 rubric literature shows to be least reproducible. LLM-verbalized scores
  collapse to a handful of values (0.7/0.8/0.9), carry ~45% calibration error,
  and sometimes correlate *inversely* with accuracy (arXiv:2606.22179; Nyckel).
- **Instagram's rubric prompts are dead code for DB campaigns.**
  `engines/instagram/prompts.py` is never imported by the cascade — a
  panel-authored campaign with blank prompts runs on the ~50-word
  `SYSTEM_GENERIC` (`core/prompts.py:26-32`), not the shipped rubric. (Other
  platforms do fall back to their platform prompts; IG is the odd one out.)
- **Escalation is a byte-identical re-ask** — same model, same prompt, temp 0,
  `[ESCALATED]` prefix only (`instagram/cascade.py:314-318` + 5 twins) — and it
  fires *by construction* on short comments because the shipped prompts instruct
  lower confidence on short text. The relevance escalation even discards the
  vision/STT evidence earlier tiers paid for (`cascade.py:281-285`).
- **Prompts hard-code `0.70` in prose** (`engines/instagram/prompts.py:75,83`)
  while the gate uses `campaign.threshold` — editing the knob silently
  desynchronizes the rubric from the gate.
- **No flip-list substrate**: only accepted matches get a row
  (`session.py:470`); `confidence` and `Decision.raw` are never persisted;
  relevance persists a bare boolean (`store.mark_seen`). No 0..1 range check on
  threshold (`server.py:734-738` checks finiteness only); `escalate_band` not
  panel-editable; `_classify_text_with_comparison` always gets `threshold=None`
  so `model_comparison_log.agreed` is always NULL (`router.py:684, 814-828`).
- **`language_mix` never enters any prompt**; no transliteration/normalization;
  the only gold sets (`scripts/eval/gold.json` 25 items, `gold_relevance.json`
  20) are 100% English — while `config/campaigns/tashkent-renovation.md`
  carries dozens of real uz/ru intent phrases as unlabeled prose.
- **Good news**: `scripts/eval/run_eval.py` + prompt-variant A/B skeleton exist;
  and a store-less real-router replay works TODAY with zero code change:
  `OpenRouterRouter(store=None)` + real `Cascade.score_comment()` —
  `_spend_guard` passes, `_record`/`raise_flag` no-op. Watch two per-router
  latches (`_json_mode`, `_degraded`) — build one router per variant and abort
  on degraded runs.

## Remedy A — Replace the model-invented score with decomposed criteria + code-side aggregation

New output contract (single call, evidence-first, nullable extraction —
dottxt-validated pattern):

```json
{"evidence": "≤200 chars: quoted phrase + what it replies to",
 "role": "buyer | seller | neutral",
 "stage": "none | problem_aware | solution_seeking | price_ask | ready_to_buy",
 "product_match": true,
 "addressed_to": "post | vendor_comment | other | unclear",
 "contact": {"phone": null, "tg": null, "city": null},
 "spamish": false}
```

The 0..1 score is **computed in engine code** from the criteria (stage ladder
base × product_match gate, modifiers for deadline/quantity/city, hard 0 for
role=seller) — so `campaign.threshold` keeps working but now thresholds a
deterministic, auditable aggregate. Evidence: decomposed boolean/enum judgments
consistently beat holistic scalars (arXiv:2509.16093, 2606.08625, 2603.00077);
CoT adds ~nothing on short-text classification (arXiv:2409.12183) — the
evidence-quote field is the right-sized rationale. Stage ladder + modifiers
mirror the only published shipped rubrics (Linkeddit 5-family 1–10 scale;
Facebook patent US10311493B2 weighting transactional over wish language).

## Remedy B — The role gate (demand vs supply), our worst confusion, solved structurally

Decide BUYER/SELLER **before** intent stage: seller = imperative CTA to reader
("заказывайте", "yozing"), volunteered phone/handle/link, price lists ("от 99
000 сум", "dan boshlab"), stock claims ("bizda bor"), promo-emoji clusters;
buyer = first-person want/ask directed at the post. Direction of contact
exchange is the discriminator: "DM me" *requested* = buyer signal; "DM me"
*offered with a price* = seller. Sellers still get extraction (competitor
intel) — routed, never dropped.

## Remedy C — Prompt plumbing fixes

- Wire `engines/instagram/prompts.py` as the IG fallback (match other engines).
- Template the threshold into the rubric prose (or strip numbers from prose).
- Inject **parent comment** context for replies — "what is this addressed to?"
  becomes a rubric question ("Сколько?" under the post vs under a vendor's
  price-list comment are different leads).
- **6–10 in-language few-shot exemplars** (uz-Latin + uz-Cyrillic + ru, never
  translated to English — arXiv:2406.18880, 2502.11364), including the hard
  negatives: enthusiastic non-buyer ("ajoyib 🔥"), competing vendor asking
  price, emoji-only. Source them from the Tashkent brief's phrase lists + real
  shakedown comments.
- Cheap pre-filters before any LLM call: cross-post duplicate text, same-author
  repeats, seller-pattern routing, emoji-only → chatter unless context promotes.

## Remedy D — Benchmark harness & gold set (the lab part)

1. **Harness**: store-less real-router cascade replay (above) + the existing
   `scripts/eval/` variant skeleton; one router per variant; report cost/item
   incl. escalations (`cascade.escalations`).
2. **Gold set v1 (150–200 items)**: bootstrap from real shakedown comments
   (open-code ~100 to failure-taxonomy saturation — Husain/Shankar procedure);
   strata: ~25% easy pos, ~25% easy neg, ~30% hard neg (price complaints,
   past-purchase reviews, competitor mentions, supply-side), ~10% boundary;
   **≥30 items per language/script slice** {uz-Latin, uz-Cyrillic, ru, en,
   code-switched}; +20 contrastive minimal pairs ("qancha turadi?" vs "qancha
   turgan edi?") + 15 transliteration pairs (same comment both scripts — any
   label flip = script-robustness bug). Synthetic only to fill strata, generated
   by a different model family, 100% human-verified for positives/boundary.
3. **Statistics discipline**: always Wilson CIs, never point estimates (n=50
   detects only large regressions; n=200 ⇒ ~±5pp). Threshold: sweep the full
   set, pick **max precision s.t. recall ≥ 0.9**, place mid-gap between score
   clusters, bootstrap-check stability, re-sweep on every prompt/model change.
4. **Regression suite**: temp 0; paired runs on the frozen set; **McNemar on
   discordant pairs + per-slice flip lists** (a net-zero delta with 12 flips =
   moved boundary); baseline keyed to
   `sha256(prompt_template + model + params + threshold)`; track self-flip rate
   as the noise floor; weekly scheduled run to catch hosted-model drift (pin
   OpenRouter model variants).

## Remedy E — Persistence + validation fixes

Store `confidence` and (sampled) `Decision.raw` on matches/`eval_candidates`;
persist relevance score/reason, not just the boolean; sample rejected comments
(Sheet #1 Stage 0 negative capture); add `0 < threshold < 1` +
`0 ≤ lo ≤ hi ≤ 1` validation; make `escalate_band` panel-editable; pass
`threshold` into `classify_text` (fixes the always-NULL `agreed` column).

## Remedy F — Fix escalation

Replace the byte-identical re-ask: the "unclear" ordinal bucket (from Remedy A)
replaces the confidence band as the escalation trigger, and escalation becomes a
**k=5 sampled vote at temp>0** (vote fraction = a score with actual frequentist
meaning) or a different-model second opinion — never the same call twice.
Escalation must carry forward vision/STT evidence instead of discarding it.

## Constraints discovered AFTER the research (2026-08-20, from the live-shakedown work)

The engine changed under this sheet while Sheets #1-#2 were being built. Three of
those changes constrain the design above, and two of them make a remedy as written
unshippable. Read these before starting.

1. **`core/router.py` now enforces ONE wall-clock budget per logical
   classification** (`_CLASSIFY_BUDGET_SEC = 80.0`, `_CallBudget`, injectable
   clock). `_post_verdict`'s parse-retries and `_post`'s transport retries draw on
   the SAME allowance; before that they multiplied (2 x 3 = 6 requests, ~360s) and
   blew the 180s session watchdog.
   **This breaks Remedy F as written.** A "k=5 sampled vote at temp>0" is five
   model calls inside one logical classification — against an 80s total budget
   that a single call can already consume. Remedy F must either thread its own
   budget explicitly, run the k calls CONCURRENTLY under the shared allowance, or
   drop to k=3 with a measured per-call ceiling. Any new retry layer or call site
   that does not thread the budget reintroduces the multiplication bug.

2. **Per-request timeouts are per-PHASE now** (`connect`/`read`/`write`/`pool`),
   not one scalar. httpx applies a scalar to EACH phase separately, which is why a
   nominal "60s timeout" measured 141s in a live run. Any benchmark harness that
   reports cost/latency per item must use the phase-aware config or its numbers
   are wrong by ~2x.

3. **The match stage is wrapped by `_HeartbeatRouter`** in each `session.py`. It
   bumps `sessions.last_activity_at` in a `finally` around every
   `classify_text`/`classify_image`. **Routing a model call around that facade
   loses the heartbeat**, and the watchdog then halts long comment loops. Remedy
   F's escalation and Remedy A's redesigned single call must both go THROUGH it.

4. Unrelated but worth not re-chasing: a `new=0 total=0` comment fetch is a stable
   property of specific reels (comments disabled or empty), not a regression —
   confirmed across five runs on the same reel id, predating the focus fix.

## Build order (match prompt alone)

1. Validation + plumbing fixes (threshold range check, IG fallback wiring,
   threshold templating, comparison-threshold bug) — ~1 day.
2. Real-router replay harness + gold set v1 from real comments + labeled
   Tashkent phrases — ~2–3 days.
3. Output-contract redesign (role/stage/evidence + code-side scoring) +
   in-language few-shots + pre-filters — ~2–3 days.
4. Escalation replacement + persistence additions — ~1–2 days.
5. CI regression suite (paired flips, McNemar, prompt hashing, weekly drift
   run) — ~1 day.

**Deliberately excluded:** DSPy/MIPROv2 below 200 labels (noise; GEPA becomes
viable at ~100+ trusted labels with textual-feedback metric — revisit after
triage promotion grows the corpus); logprob scoring (unavailable on key
models; saturates >0.999 under JSON mode); the verbalized 0..1 score as a
decision variable (rank-only at best); LLM-guessed thresholds.

## Sheet #3 — as built SO FAR (step 1 only)

Only build-order step 1 (validation + plumbing) is done, and one of its four items
is blocked. Steps 2-5 are not started.

**Done:**
- **IG prompt fallback wired** — but NOT the way this sheet said. "Wire
  `engines/instagram/prompts.py` as the IG fallback (match other engines)" is
  wrong: those constants are the shipped **Acme SaaS** baseline, locked verbatim
  to `config/campaign.md` by `tests/test_config.py` and imported by
  `scripts/eval/variants/`. Wiring them would have imposed a SaaS rubric on every
  Instagram campaign, including the Tashkent renovation brief — worse than the
  generic prompt it replaced. Separate vertical-neutral `IG_RELEVANCE` /
  `IG_VISION` / `ig_match()` were written instead, modelled on
  `engines/x/prompts.py`, and the baseline is untouched. The cascade reaches them
  through properties, not locals, because IG has nine call sites across four
  methods.
- **Threshold templated into the rubric**, replacing the hard-coded "the 0.70
  threshold separates genuine inquiry from banter". Band edges are FRACTIONS of
  the space either side of the live threshold, not fixed offsets: fixed offsets
  collapse to three zero-width bands at t=0.05 and invert the top band at t=0.95
  (`0.95-0.90`). Below a threshold that leaves no room for five bands, the prompt
  drops to qualitative guidance rather than printing a contradictory ladder.
- **Range validation** — `0 < threshold < 1` at the bridge (0 matched every
  comment, 1 matched none; only finiteness was checked) and `0 <= lo <= hi <= 1`
  on `escalate_band` (an inverted band silently disables escalation entirely).
  `campaign_gen` now clamps into `[0.01, 0.99]` so a generated draft cannot be
  rejected by the endpoint that saves it.
- `RELEVANCE_GATE = 0.5` named in all six cascades instead of a bare literal.

**Blocked — the always-NULL `agreed` column.** `router._classify_text_with_comparison`
computes `agreed` correctly but only `if threshold is not None`, and no caller
passes one, so `model_comparison_log.agreed` is NULL for every row ever written.
Threading `threshold=` through the twelve cascade call sites breaks 124 tests:
every fake router in the suite implements the older, narrower `classify_text`
signature. The right fix is a per-router `default_threshold` the cascade sets once,
which needs a `core/router.py` change — owned by a concurrent session at the time
of writing, and proposed to them rather than forced through.

**Step 2, first half — statistics discipline (done).** `core/evalstats.py`, pure
and network-free: Wilson intervals on every proportion, exact (not chi-squared)
McNemar over paired discordant counts, per-item flip lists, a threshold sweep that
lands MID-GAP between score clusters with a bootstrap stability range, per-slice
reports that flag any slice under 30 items as underpowered, and a `baseline_key`
that hashes prompt + model + params + threshold **+ the gold set's own item ids**
(adding items changes every measurement; a baseline silently spanning two
different sets is worse than no baseline). `scripts/eval/run_eval.py` now reports
all of it, and comparing variants prints the paired flip list with a p-value
instead of a bare F1 delta. `core/tagmine.py`'s Wilson implementation was
de-duplicated onto this module.

Why this came before the gold set: on the existing 25-item set a precision of
0.80 carries a 95% interval of roughly 0.59-0.93, so the harness could not have
told a real improvement from noise no matter how good the data was.

**Step 3, the pre-filter half (done).** Remedy C's last bullet: "cheap
pre-filters before any LLM call". Three cascade docstrings had advertised
"local pre-filter -> local scoring -> escalate-if-unsure -> cloud" since they were
written and no pre-filter existed — every comment, including every bare emoji
reaction, bought a model call. `core/matching.comment_prefilter_reason` now runs
as the FIRST statement of `score_comment` in all six engines, with a
`Cascade.prefiltered` counter per reason.

Deliberately only three rules, each a certainty, because a pre-filtered comment
is never scored AND never stored — a wrong skip is an invisible lost lead, the
same failure mode as the relevance gate's false negatives:
  * empty;
  * no letters anywhere AND fewer than 7 digits (a bare phone number survives —
    it is a real signal, and a *run*-based digit test filtered exactly those,
    since real numbers are written "+998 90 123 45 67", not as one run);
  * the same AUTHOR repeating text they already had scored this session.

Two things NOT filtered, on purpose. **Sellers** — Remedy B is explicit that
supply-side commenters are routed for competitor intel, never dropped; detecting
them is `discovery/buyer_density`, acting on them belongs to the output-contract
redesign. And **cross-post duplicate text from different authors**: the sheet
lists it, but it is wrong for this engine and a test caught it. The
highest-value comments are short, common buyer questions — "narxi qancha?",
"how much?" — so two people asking the same thing under two posts are two leads,
and text-only dedupe drops the second precisely BECAUSE it was a textbook buyer
phrase. Keying on `(author, text)` catches the real spam pattern instead.

**Step 2, negative capture — the prerequisite nobody had noticed (done).** The
gold set could not be bootstrapped at all, and measuring the live DB is what
showed it: `matches` held **2 rows** (both accepted, both Russian price
questions) and **no table anywhere held a rejected comment**. `session.py`'s
`if res.is_match:` is the only path to `matches`, so every rejected comment was
scored, paid for and discarded. The negatives are a gold set's expensive half —
easy positives do not discriminate between two prompts; hard negatives (price
complaints, past-purchase reviews, vendors quoting their own prices) do.

Schema **v26**: `eval_candidates`, plus `matches.confidence` / `matches.raw`
(the escalate band reads `confidence` and it was never persisted, so a verdict
could not be re-examined). Capture is banded and sampled — `accepted` and `near`
(within `NEAR_BAND` of the threshold) always kept, `clear` deterministically
sampled 1-in-8 by a hash of the comment id, with a per-session cap. Deterministic
rather than `random` so a re-poll lands the same way and a test can assert it.

The seam is **`self.router.store`**, not a constructor argument: three engines
wrap the router in a `_HeartbeatRouter` facade that forwards attribute access, so
one seam covers all six cascades WITHOUT touching a session file (three of which
belong to a concurrent session). A store-less router captures nothing, which is
what dry runs, tests and the replay harness need. A human `label` is never
overwritten by a re-score — the same rule as `matches.status`.

Operator surface: `aizu gold --campaign <id>` lists the queue most-informative
first, `--label <id> --verdict yes|no` records ground truth, `--export` writes the
`scripts/eval/gold.json` shape the harness already reads.

**Not started:** the gold set's LABELS (v1, 150-200 items, >=30 per language
slice — the candidates now accumulate on every run; the labelling decision is
still the operator's), the store-less real-router replay,
the rest of the output-contract redesign (role/stage/evidence + code-side
scoring), the escalation replacement, and the CI regression suite.

# Remedy Sheets #4–#5 — researched, in companion docs

Both were researched on 2026-08-21 by a 15-agent run and live in their own files
because they are ~5x the length of a sheet here:

- [`campaign-lab-sheet-4-vision.md`](campaign-lab-sheet-4-vision.md) — the vision
  prompt. Headline: the tier is currently **bricked** on any box that does not
  override the model ids in env (see below), so nothing about it has been
  measurable; plus an image-ablation grounding test that needs no labels at all.
- [`campaign-lab-sheet-5-fields.md`](campaign-lab-sheet-5-fields.md) —
  `languageMix`, `extractDef`, `relevanceDef`.

⚠️ Read the provenance header in each. Only the claims flagged time-sensitive
went through adversarial verification, and **6 of the 7 that did were refuted**;
everything else is a researched proposal, not a checked fact.

## LIVE DEFECT found by that run, outside Campaign Lab's scope

Both of the engine's default model ids are **absent from OpenRouter** (verified
independently against `GET https://openrouter.ai/api/v1/models`, 2026-08-21, 419
models): `openrouter/owl-alpha` and `nex-agi/nex-n2-pro:free`
(`nex-agi/nex-n2-pro` exists; the `:free` variant does not). This deployment is
masked by `engine/.env` setting `OPENROUTER_TEXT_MODEL` / `OPENROUTER_VISION_MODEL`;
any box without them sends a dead id on every LLM call.

Five sites carry the dead ids — `core/router.py:336-337`, `cli.py:1013,1015`,
`worker/config.py:299-300,361-363,416-417`, `engines/warming/tg_relevance.py:32`,
`.env.example:20-21`. The CLI and worker pass them as EXPLICIT args and
`router.py:366-370` gives an explicit arg top precedence, so fixing the router
default alone changes nothing on either path.

Vision prompt (real image fixtures or honest "un-benchmarked" amber), and the
remaining generated fields (languages, extract fields, relevance def — note
relevance shares most of Sheet #3's machinery: same replay harness, same gold-set
discipline, plus the hard-coded 0.5 gate and label-wins-over-score rule at
`instagram/cascade.py:287-288`).
