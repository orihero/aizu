# Quora Question/Answer Discovery Agent — PRD (v1 & v2)

**Status:** Draft v0.2 · **Date:** 2026-06-19 · *product name TBD*

> Generic engine. It knows no vertical and no goal — all domain meaning lives in the campaign brief. The flagship first campaign is lead-gen (e.g. a SaaS product), but that is one brief, not a built-in assumption.

> **Sibling spec.** This is one of a family of per-platform agents (Instagram, YouTube, Facebook, LinkedIn, X, Threads, Reddit, TikTok, **Quora**, Pinterest) that share an identical engine and contract. A future **campaign orchestrator** lets an operator author one `campaign.md`, fan it out to the platform engines they pick, and pool every match into a single leads dashboard over the shared SQLite contract. **X, Threads, Reddit and Quora are being authored together and implemented as one combined phase** — they share the CDP-scraping access shape and the text-first content model, so they ship as a single batch rather than four separate efforts. This PRD is self-contained; the orchestrator is a separate doc.

> **Why Quora differs.** Quora is the only platform in the family whose parent unit is a **question**, not a post or a video — and the asker of that question is itself the **highest-purchase-intent lead signal anywhere in the family** (someone publicly asking "which project-management tool should I buy for my team?" is a lead by intent, before a single answer exists). The whole surface — questions, the answers beneath them, and the comments nested beneath answers — is **text**; there are no videos here (the engine's `Reel` dataclass is just an inherited name), and the vision/OCR tier is a rare secondary pass for the occasional image embedded inside an answer. Access is the **hardest of the four combined-phase platforms**: Quora has **no public content API** — it never shipped one (only a defunct logged-in-user "Extension API"; the Poe API is an unrelated AI-model gateway, *not* a content read API). Its Terms expressly prohibit automated access, and it sits behind the family's **heaviest bot wall** — Cloudflare fingerprinting + Turnstile/JS challenges stacked on Quora's own login/checkpoint enforcement (quora.com returned **HTTP 403** to an automated fetch during this research, as of 2026-06; verify before build). So the **only** viable path is **CDP browser scraping** — a warmed, logged-in real Chrome attached over CDP, intercepting the page's own internal GraphQL JSON, with a **DOM-read fallback** where that JSON isn't cleanly interceptable. Identity is **managed** (warmed account, no per-org secret — like Instagram); discovery is **semi-deterministic** (followed Spaces/topics + saved searches — steerable but still ranked); and because the defenses are the heaviest in the family, **slow human-paced reads and immediate halt-on-challenge are not just safety rails but the core of the design**.

---

## 1. Summary
A local-first agent that examines **public Quora content the operator steers it toward** — discovered through Quora's home feed, **followed Spaces/topics**, and **saved/recurring searches**. The scope is **text-first and covers every public content type Quora carries**: **questions** (the parent unit and the strongest intent signal), the **answers** beneath them, and the **comments nested beneath those answers** (including threaded replies); where a Space surfaces a plain **text post**, **image post**, or **link post**, that too is examined. The parent item's title/body lives in `Reel.caption`, and the answers/comments are delivered as `Comment` objects via `fetch_comments()` (the parent's text in `Reel.caption`), so a **pure-text question with pure-text answers is fully first-class — examined on its text alone**. The **vision/OCR tier is a secondary, optional pass** used only for the occasional **image embedded inside an answer**; it is never a precondition for examining an item, and `on_screen_frames` is simply empty for the (common) text-only case. There is **no audio surface** on Quora.

Crucially, the **question asker is itself a first-class match surface** alongside answer and comment authors: a high-intent question with zero answers is still a lead by intent. The engine reads the relevant text, scores/extracts it against a **campaign-defined brief**, and surfaces matches in an admin panel for human follow-up. The engine is domain- and goal-agnostic and must run against a brief it has never seen with zero code change. It runs on the operator's own Mac in short daytime bursts — not on a server, not 24/7.

*A **match** = any answer, comment, **or question (via its asker)** scoring above threshold against the active brief. For the lead-gen campaign, a match is a lead — and a high-intent **question** itself is the strongest kind.*

> "Reel" appears below only as the name of the engine's internal `Reel` dataclass, inherited from the Instagram origin. On Quora the parent item it carries is a **question** (or a text/image/link post) — never a video.

## 2. Goals / Non-Goals

**Goals**
- Surface brief-matching answers, comments, **and high-intent questions (via their askers)** from public Quora threads, where relevance, goal type, what-to-extract, and threshold all live in `campaign.md`.
- Survive on Quora — behave like a real, slow, human account; never trip Cloudflare or Quora's own checkpoint/challenge enforcement.
- Keep heavy data local; use cloud only as a targeted escalation when the local model is unsure.
- Operator-controllable and observable through an admin panel.

**Non-Goals (both versions)**
- The bot never upvotes, follows, answers, comments, messages, or posts. Read + scroll only.
- **Never solves a Cloudflare challenge / Turnstile / captcha / login checkpoint / 2FA** — it halts and alerts a human. No CAPTCHA-solver, no FlareSolverr, no fingerprint-spoofing arms race.
- No multi-account farming, no cold mass-message outreach.
- No reselling collected data (separate product direction, out of scope).
- No reading private/members-only content the operator's account is not legitimately able to see; **public content only**.
- The engine hardcodes no vertical and no goal.

## 3. Core principles (locked)
- **Generic engine** — knows no vertical, no goal; runnable against an unseen brief with zero code change.
- **Local execution** on the operator's machine (M4 Max, 36GB), bursty daytime runs.
- **Real Chrome over CDP** — attach to a warmed, logged-in profile; never launch a vanilla automation browser. On Quora this is **mandatory, not a preference**: a crafted/headless client is detected by Cloudflare on TLS/HTTP2 fingerprint alone. The engine rides a browser Cloudflare already trusts — it does not try to defeat the wall.
- **Read-only collection** — passive reading only; the only "actions" are human-like read and scroll. Read-only removes the entire write-side abuse signal that trips most automation enforcement.
- **Network interception, not crafted API calls** — read the page's own internal GraphQL JSON traffic as it flows; never craft requests against a documented endpoint (there is none). **DOM-read fallback** where the internal JSON isn't cleanly interceptable (the one structural difference from Instagram's pure-interception model).
- **Discovery steering is semi-deterministic + recurring** — relevance is steered mostly by **followed Spaces/topics and saved searches** in the brief; the operator curates these during warming and re-nudges periodically (see §10). Between Instagram's opaque "For You" and Telegram's fully-deterministic seed list.
- **Slow, human-paced reads + immediate halt-on-challenge are first-class** — Quora's defenses are the heaviest in the family, so pacing is more conservative and the halt threshold more sensitive than the other three platforms.
- **Escalate only when unsure** — both relevance and match decisions run cheap-local first and ask the cloud model only on low-confidence cases.
- **SQLite is the only contract** between engine and panel (WAL mode; two processes meet at the DB).
- **soul.md + campaign.md** — config read only at session start.
- **Generic criteria & goal, fixed core record** — relevance/goal/extraction are free-text brief; every match lands in identical core columns plus a brief-defined `extracted` blob.

## 4. soul.md vs campaign.md (the generalization boundary)
- **soul.md** — engine identity + safety: read-only, pacing, halt-on-resistance, never act. Domain-free. (On Quora the halt-on-resistance clause is load-bearing: any challenge/checkpoint stops the session.)
- **campaign.md** — warming/seed direction (which **Spaces/topics** to follow, which **saved searches** to run, optional specific profiles to watch), relevance definition, goal type, what to extract & score, threshold, language mix. Quora-specific knobs map onto existing Campaign fields (no schema change): `seed_channels` = followed Spaces/topics; `seed_hashtags` = saved/recurring search queries; `seed_accounts` = optional specific Quora profiles to watch; `include_home_feed` = whether to walk the algorithmic home feed.
- Test of "generic": swap `campaign.md` and the same binary runs a different hunt, no code change.

## 5. Architecture (target)
```
Engine (headless) ──► SQLite ◄── Admin panel (local web app)
        └─► model router ─► local text · local vision (MLX/Ollama) | cloud (OpenRouter)
```
- **Engine:** Python + Playwright over CDP (`connect_over_cdp` to a warmed, logged-in Chrome — port 9222/9333 per the engine-live-run gotcha). Walks the home feed (if enabled) + followed Spaces/topics + saved searches; reads question title/body + answer bodies + nested comment bodies (+ any answer-image text); **intercepts Quora's internal GraphQL JSON** (POSTed `operationName`/`variables`, commonly persisted-query hashed) and **falls back to reading the rendered DOM** (question-title node, answer-body nodes, comment nodes) where that JSON isn't cleanly interceptable; runs the cascade against the active brief; writes matches + state.

  **Content types in scope (text-first, vision-optional):** **questions** · **answers** · **comments nested beneath answers (incl. threaded replies)** · and any **text / image / link posts** a Space surfaces. Every item is examined **on its text**; the vision/OCR tier runs **only** when an answer carries an embedded image, and is **never a precondition** for examining an item. There is no video and no audio on Quora.

  **Maps onto `FeedSource` (platform-unique within the family):**
  - **QUESTION = `Reel`** (the parent item). Question title/body → `Reel.caption`; asker → `Reel.author`. This is the strongest single intent signal of any platform in the family. `Reel.on_screen_frames` / `Reel.ocr_text` are **empty** for the common pure-text case.
  - **ANSWER = `Comment`** (the primary scored unit), delivered via `fetch_comments()`. Answer body → `Comment.text`; answer author → `Comment.username`; `is_reply=False`.
  - **ANSWER COMMENT = `Comment`** (nested beneath an answer, in scope), also delivered via `fetch_comments()`. Comment body → `Comment.text`; `is_reply=True`.
  - **Seed unit:** a followed **Space**, a followed **topic**, or a **saved/recurring search query** (plus optional specific profiles to watch).
  - **Platform-unique rule — asker as a first-class match surface:** when the matched unit is the **question itself**, the `comment_id` slot in the match record **references the asker** and the scored `text` is the **question body** (a question-as-lead). Attribution is always derived from the matched unit, never from a global "active question" (mirrors the prior lead-misattribution fix).

- **How `build_feed` wires it (`feeds/__init__.py`):** add an `if platform == "quora":` branch **before** the `SUPPORTED_PLATFORMS` `NotImplementedError` fallthrough, **mirroring the `instagram` branch** (managed, `credentials=None`). It constructs a `QuoraFeed` from a **`QuoraConfig` — a NEW Quora-specific config object, analogous to `CDPConfig` but a distinct dataclass** — carrying `cdp_url` + Quora's own seed knobs. The `build_feed` branch maps the brief's existing seed fields onto those NEW knobs (`build_feed` already accepts `seed_channels` — verified in `feeds/__init__.py`): followed Spaces/topics ← `seed_channels`; saved/recurring searches ← `seed_hashtags`; optional watched profiles ← `seed_accounts`; `include_home_feed` controls walking the algorithmic home feed via the seed-aware `_resolve_home_feed` default — OFF when Spaces/searches are seeded, ON when seedless. The branch then calls `feed.attach()` (`connect_over_cdp`, like `CDPFeed`). **No `from_credentials` path is added** — there is no secret to load. `config.py` adds `"quora"` to `SUPPORTED_PLATFORMS = ("instagram","youtube","telegram","quora")`, so `CampaignBrief.platform` validates `"quora"`. (Note: those Quora seed-knob field names live on the NEW `QuoraConfig`, **not** on the existing `CDPConfig` — whose only seed fields are `seed_hashtags` / `seed_accounts` / `include_home_feed`.)

- **Per-org connection / secret: NONE.** Quora identity is **managed-CDP, identical to Instagram** — the engine attaches to the operator's own warmed, logged-in Chrome; the Quora session cookie lives in that browser, never in the DB. There is therefore **no schema v9 `integration_secrets` row and no connect endpoint** for Quora. This is the key wiring contrast with YouTube/Telegram, which **do** carry per-org encrypted secrets (schema v8 / Fernet, `AIZU_SECRET_KEY`). A platform that needed OAuth/an API key (e.g. Reddit's official path) would add the v9 entry + a connect endpoint; Quora deliberately does not, because CDP-attach to a warmed browser is the access method and there is no API key to store.

- **Model router:** call sites — `classifyText` (relevance + match) and `classifyImage` (text inside an answer image). Quora is text-first, so vision is secondary and rare; audio is not applicable. See §6.
- **Store:** SQLite (WAL). Matches, state, status, spend. Carries the shared `platform` dimension (`quora`).
- **Panel:** local web app reading/writing SQLite. Never calls the engine.
- **The session/cascade/store layers are untouched** — they already speak `Reel`/`Comment` and the shared SQLite contract; **only the feed differs** (`engine/aizu/feeds/quora.py` → `class QuoraFeed(FeedSource)`).
- **v2 shell:** OpenJarvis wraps the frozen engine as a scheduled skill.

## 6. Model routing
| Stage | Task | Where | Notes |
|---|---|---|---|
| Relevance gate | is this question/thread relevant to the brief? | Local text (question title/body + answer text) → **escalate-if-unsure → cloud** | text-first platform; most threads judged on text alone |
| Match scoring | is this answer / comment a match — **or is the question (its asker) itself a lead?** | Local → escalate-if-unsure → cloud | the question-as-lead is the strongest intent signal |
| Vision / OCR | read text inside an answer-embedded image | Local (Qwen2.5-VL 7B class) — **v1, secondary & rare** | load-on-demand only for the occasional image-bearing answer, then unload; text is never gated on it |
| Audio / transcript | — | **not applicable** | Quora has no native audio surface |

One model resident at a time (36GB cap). **Batch by stage** — run the text gate across threads, then load vision once for the rare image-bearing-answer minority, unload, then escalate the unsure remainder to cloud. Cloud tier needs fallback (retry queue or degrade-to-local + flag) and per-campaign spend logging.

## 7. State model (the spine)
Persisted in SQLite between sessions:
- **seen_questions** — dedupe; forward-only watermark (per question id).
- **answer/comment cursors** — per question; "new answers / new nested comments since last poll."
- **watchlist** — match-rich questions (active threads), re-polled until aged out (~7–14 days).
- **session counters** — questions seen, **already-seen skips**, relevance passes, matches (incl. question-as-lead), escalations, spend.
- **feed-health flag** — set when the already-seen-skip ratio crosses threshold (Spaces/feed/saved-search tapped out or stale). Spaces vs saved searches tap out at different rates, so the flag is tracked per source.
- **account health flags** — last run, login/session state, **challenge state** (Cloudflare/checkpoint seen), **empty-interception canary** (true when neither interception **nor** DOM-fallback produced content for N consecutive threads).
- **match status** — keyed on the matched-unit id (the answer's / nested comment's `comment_id`, or the **asker** for a question-as-lead); survives re-scrapes (re-poll never overwrites human status).

Writes idempotent on the matched-unit id. Killed mid-run → resume from state.

## 8. Match record (schema)
`campaign_id, platform, reel_id (=question_id), comment_id (=answer_id / nested-comment-id / asker), username, text, lang, score, reason, extracted, status, captured_at`
- These are the **real shared core columns** (`engine/aizu/store.py`): `reel_id` and `comment_id`, with PRIMARY KEY `(campaign_id, platform, comment_id)`. `question_id` / `answer_id` are Quora's **aliases** for those slots, not literal columns — mirroring the Telegram PRD's `message_id → reel_id slot, comment_id → reply scored`.
- Maps onto the shared core record: `question_id` → the parent question (the `reel_id` slot); `answer_id` → the answer or nested comment scored (the `comment_id` slot).
- **Question-as-lead:** when the matched unit is the question itself, the `comment_id` slot references the **asker** and `text` is the **question body** — attribution derived from the matched unit, never a global "active question".
- `score` / `reason` — interpreted per the active brief.
- `extracted` — brief-defined JSON (lead → asker/answerer username + intent; partner → answer author; signal → topic). The campaign Extract input drives the AI's extracted-field schema (injected into the cascade contract; JSON mode).
- `status` ∈ { new, reviewing, confirmed, discarded }

## 9. Failure handling (three tiers)
- **Auto-skip + continue (transient, no human):** a single question/answer thread fails to load; one intercepted GraphQL payload fails to parse (routed through a **tolerant, never-throw JSON boundary + repair + schema validation** per the LLM/untrusted-JSON rule — one malformed payload must never crash the run); an individual answer-image OCR fails. Skip the item, keep walking.
- **Soft flag (continue, surface on dashboard):** Space/topic/feed exhaustion or staleness (already-seen-skip ratio crosses threshold → stale-topic flag, operator adds/prunes Spaces); a saved search returns mostly already-seen results (query tapped out); spend cap hit; cloud (OpenRouter) degraded → degrade-to-local + flag; an **intermittent single Cloudflare soft-challenge that the warmed session clears on its own** without a wall (back off + flag — do not hammer).
- **Halt + alert human (stop session — Quora's tier is the fattest in the family):** **Cloudflare Turnstile / JS-challenge interstitial or full block page**; login/session expired; **checkpoint / captcha / "verify you're human" wall**; account restriction or ban; **empty-interception canary** for N consecutive threads (endpoint drift **or** a silent challenge — the engine can't tell, so it halts). The engine **never** attempts to solve a challenge/captcha/2FA and **never spoofs fingerprints** to push through — it stops and alerts. Because Quora's defenses are heaviest, the halt tier is wider and the halt threshold more sensitive than on the other three platforms.

## 10. Pacing & steering/seeding
- **Warming (weeks 1–2 — more than Instagram, less ceremony than a write account):** warm a **real account** with genuine browsing history; **follow the brief-relevant Spaces and topics by hand** (this is simultaneously warming **and** the discovery-steering act); run a few genuine human sessions so the account + browser look lived-in to Cloudflare and to Quora. Read-only lowers ban risk vs a write account, but Quora's challenge walls fire on read volume too — warming is not optional.
- **Agent runs:** ramp from low — ~**1–2 sessions/day, 15–30 min, ~20–40 questions/session**; long dwell **3–25s per thread**, between-thread **2–6s**, all randomized; **daytime-only bursts** (not a server, not 24/7). Ramp until the first sign of resistance (soft-challenge/slowdown), then hold **below** it. All caps discovered empirically — **no documented limit exists** (as of 2026-06; verify before build).
- **Steering/seeding cadence — semi-deterministic + recurring.** Steer via followed Spaces/topics + saved searches curated during warming; re-nudge periodically (follow/unfollow, add/prune queries). A rising already-seen-skip ratio (§7) flags a topic as tapped out → add new Spaces/searches. Re-steering is operator config, not an algorithm to fight.
- **Respect every challenge:** any Cloudflare/login wall = immediate back-off (soft) or halt (hard); **never retry-hammer through a challenge.**

---

## 11. Scope: v1 vs v2

### v1 — Prove the loop (manual · headless)
- Manual account warming + Space/topic following + saved-search curation; ongoing add/prune.
- CDP attach to warmed, logged-in Chrome (`connect_over_cdp`); **managed identity, no per-org secret**.
- Discovery loop: home feed (if `include_home_feed`) + followed Spaces/topics + saved searches; read-relevant / scroll-past-irrelevant.
- **Relevance gate: question title/body + answer text → (rarely) answer-image text via vision/OCR → escalate-if-unsure to cloud.** Text-first: a pure-text question with pure-text answers is fully first-class.
- Interception of questions + answers + nested comments via internal GraphQL JSON, **with the DOM-read fallback** where JSON isn't cleanly interceptable; top-level answers; comment-expansion only on matching answers; cursors for new-since-last-poll.
- Scoring cascade: local pre-filter → local scoring → escalate-if-unsure to cloud — **including the question-as-lead path (asker as a first-class match surface).**
- **Vision/OCR tier (secondary, image-bearing answers only). No audio (n/a on Quora).**
- SQLite store: full schema + state model + resume; carries `platform = quora`.
- soul.md + campaign.md, with one real brief (lead-gen) as proof.
- Three-tier failure handling (auto-skip / soft-flag / halt-alert), **with halt-on-challenge central** (Cloudflare/Turnstile/checkpoint/captcha → halt + alert, never solve, never spoof).
- **Session counters + stale-topic flag + empty-interception canary** (true only when neither interception nor DOM-fallback produced content for N threads).
- Pacing engine — most conservative in the family; respects every challenge.
- **Panel — read surfaces:** matches table (filter/sort/status-mark, incl. question-as-lead rows), health/canary panel (incl. stale-topic flag + challenge state), OpenRouter spend.
- Operation: operator triggers sessions manually. Follow-up: human, off-platform.

**v1 done =** account survives weeks unrestricted · sessions complete and resume cleanly · matches (answers, nested comments, **and question-as-lead via asker**) land with validated precision against a hand-labeled set, on at least one real brief · stale-topic flag fires correctly · interception works with the DOM-fallback covering the gaps · every Cloudflare/checkpoint challenge halts+alerts (none are auto-solved or hammered).

### v2 — Intelligence + automation
- **OpenJarvis integration:** frozen scraper as a scheduled skill; unattended **scheduled** (not continuous) mode; memory layer.
- **Question-intent ranking:** score and rank open questions by purchase-intent (Quora's strongest signal — the asker-as-lead surface) so the highest-intent askers float to the top of the queue.
- **Panel — write surfaces:** campaign editor (writes campaign.md incl. Space/topic list + saved searches; engine re-reads only at session start); **review queue** (each confirm/discard captured as labeled training data → tunes thresholds + escalation); richer status workflow.
- **Memory:** optional Obsidian vault as accumulating market model.
- **Scale:** multi-campaign (parallel briefs); topic-discovery suggestions (suggest adjacent Spaces).

**v2 done =** runs on a schedule with no babysitting · review-queue feedback measurably lifts precision · ≥2 distinct briefs run on the same engine with no code change.

---

## 12. Open questions
- **Internal GraphQL endpoint + persisted-query hashes** for questions/answers/comments — identify once in DevTools; expect drift and persisted-hash churn. Confirm the **DOM-fallback selectors** (question-title node, answer-body nodes, comment nodes) as a backstop.
- How cleanly is Quora's internal JSON interceptable vs how often the **DOM-read fallback** is actually needed? (drives engineering effort and canary tuning.)
- **Empirical safe read pacing** before Cloudflare/Quora fires a challenge — no documented limit exists; discover conservatively (as of 2026-06; verify before build).
- **Already-seen-skip threshold** that trips the stale-topic flag — tuned **separately for Spaces vs saved searches** (they tap out at different rates).
- Whether to treat a high-intent **question (and its asker)** as a first-class match type vs only scoring answers/comments — decision leans **YES** (asker = strongest intent), but confirm before freezing the match schema's asker-pointer.
- **ToS / commercial-use and legal posture** for read-only collection of public Quora content — Quora's Terms expressly prohibit automated access; this is the operator's call and a **real contractual risk**, flagged as an open question, not a resolved fact.
- Retention TTL for stored personal data; GDPR posture if targets extend beyond CIS.
- How far to schematize the `extracted` blob vs leave it free-form per brief.
- Whether the **DOM-fallback** can itself be detected/challenged differently than interception (does heavy DOM walking trip a different Quora signal?).

## 13. Risks
| Risk | Mitigation |
|---|---|
| Cloudflare Turnstile / JS-challenge wall blocks the session (heaviest defense in the family) | Attach to a warmed, logged-in **real** Chrome over CDP (inherit a trusted browser fingerprint + session); read-only; slow human-paced reads; **halt + alert on any challenge — never solve, never spoof fingerprints** |
| Account checkpoint / login wall / restriction / ban on heavy reading | Weeks of genuine warming + Space-following; tiny per-session item counts; daytime bursts; ramp-until-resistance-then-hold-below; halt-on-resistance |
| No API + ToS prohibits automated access (contractual/legal exposure) | Public content only · read-only · store minimum · retention TTL · human-led off-platform follow-up; surface ToS/commercial-use as an explicit operator decision and open question — **do not assume it is cleared** |
| Internal GraphQL endpoint drift / persisted-query hash churn breaks interception | Intercept by URL pattern + response shape (**not** hardcoded hashes); **DOM-read fallback** when JSON isn't cleanly interceptable; empty-interception canary halts on N consecutive misses |
| Malformed / partial intercepted JSON crashes the run | Tolerant **never-throw** parse boundary + repair + schema validation (per the LLM/untrusted-JSON rule); auto-skip the single item and continue |
| Spaces / topics / saved-searches go stale or off-niche (semi-deterministic discovery drifts) | Already-seen-skip counter → stale-topic dashboard flag → operator follows/prunes Spaces and edits saved searches |
| Relevance misjudged when an answer carries an embedded image with the real signal | Secondary vision/OCR pass on answer images only (load-on-demand, then unload); escalate-if-unsure to cloud. Text remains primary; image is never a precondition |
| Local model weak on Uzbek/Russian question + answer text | Escalate-if-unsure to cloud (OpenRouter) on both the relevance gate and match scoring; hand-labeled validation set |
| Question-as-lead (asker) misattributed in the match record | When the matched unit is the question, the id slot references the **asker** and the scored text is the question body; derive attribution from the matched unit, never a global "active question" (mirrors the prior lead-misattribution fix) |
| Shared-file concurrency between engine and panel | SQLite WAL; config read at session start only; match status keyed on the matched-unit `comment_id` (idempotent writes; re-poll never overwrites human status) |
