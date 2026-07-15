# LinkedIn Post-Comment Discovery Agent — PRD (v1 & v2)

**Status:** Draft v0.1 · **Date:** 2026-06-12 · *product name TBD*

> Generic engine. It knows no vertical and no goal — all domain meaning lives in the campaign brief. The flagship first campaign is lead-gen (e.g. a SaaS product), but that is one brief, not a built-in assumption.

> **Sibling spec.** This is one of a family of per-platform agents (Instagram, Facebook, X, Threads, YouTube, Reddit, TikTok, Quora, Pinterest) that share an identical engine and contract. A future **campaign orchestrator** lets an operator author one `campaign.md`, fan it out to the platform engines they pick, and pool every match into a single leads dashboard over the shared SQLite contract. This PRD is self-contained; the orchestrator is a separate doc.

---

## 1. Summary
A local-first agent that discovers relevant LinkedIn posts through the feed plus followed people, hashtags, and companies, **reads both the post copy and the on-screen text of any carousel/document/image**, then reads the comments and scores/extracts them against a **campaign-defined brief**, surfacing matches in an admin panel for human follow-up. The engine is domain- and goal-agnostic and must run against a brief it has never seen with zero code change. Runs on the operator's own Mac in short daytime bursts — not on a server, not 24/7.

*A **match** = any comment scoring above threshold against the active brief. For the lead-gen campaign, a match is a lead.*

## 2. Goals / Non-Goals

**Goals**
- Surface brief-matching comments from public posts, where relevance, goal type, what-to-extract, and threshold all live in `campaign.md`.
- Survive on LinkedIn — behave like a real human account, never trip automation enforcement (the strictest of any platform here).
- Keep heavy data local; use cloud only as a targeted escalation when the local model is unsure.
- Operator-controllable and observable through an admin panel.

**Non-Goals (both versions)**
- The bot never likes, reacts, follows, connects, comments, or messages. Dwell + scroll only.
- Never solves checkpoints/captchas — it halts and alerts a human.
- No multi-account farming, no cold mass-message outreach.
- No reselling collected data (separate product direction, out of scope).
- The engine hardcodes no vertical and no goal.

## 3. Core principles (locked)
- **Generic engine** — knows no vertical, no goal; runnable against an unseen brief with zero code change.
- **Local execution** on the operator's machine (M4 Max, 36GB), bursty daytime runs.
- **Real Chrome over CDP** — attach to a warmed, logged-in profile; never launch a vanilla automation browser.
- **Read-only collection** — passive viewing only; the only "actions" are human-like dwell and scroll.
- **Feed steering is manual + recurring** — the operator seeds the feed by hand (following the right people, hashtags, and companies, watching the right content) during warming and re-nudges it periodically as it drifts (see §10).
- **Network interception, not DOM scraping** — read the page's own internal API traffic (Voyager); never craft API calls.
- **Escalate only when unsure** — both relevance and match decisions run cheap-local first and ask the cloud model only on low-confidence cases.
- **SQLite is the only contract** between engine and panel (WAL mode; two processes meet at the DB).
- **soul.md + campaign.md** — config read only at session start.
- **Generic criteria & goal, fixed core record** — relevance/goal/extraction are free-text brief; every match lands in identical core columns plus a brief-defined `extracted` blob.

## 4. soul.md vs campaign.md (the generalization boundary)
- **soul.md** — engine identity + safety: read-only, pacing, halt-on-resistance, never act. Domain-free.
- **campaign.md** — warming/seed direction (which people, hashtags, companies, topics), relevance definition, goal type, what to extract & score, threshold, language mix.
- Test of "generic": swap `campaign.md` and the same binary runs a different hunt, no code change.

## 5. Architecture (target)
```
Engine (headless) ──► SQLite ◄── Admin panel (local web app)
        └─► model router ─► local text · local vision (MLX/Ollama) | cloud (OpenRouter)
```
- **Engine:** Python + Playwright over CDP. Walks the feed, reads post copy + carousel/image text, intercepts JSON (Voyager), runs the cascade against the active brief, writes matches + state.
- **Model router:** call sites — `classifyText`, `classifyImage` (carousel/image text / relevance), `transcribe` (v2) — each routes per-tier to local or OpenRouter.
- **Store:** SQLite (WAL). Matches, state, status, spend.
- **Panel:** local web app reading/writing SQLite. Never calls the engine.
- **v2 shell:** OpenJarvis wraps the frozen engine as a scheduled skill.

## 6. Model routing
| Stage | Task | Where | Notes |
|---|---|---|---|
| Relevance gate | is this post relevant to the brief? | Local text → **local vision (carousel/image text, secondary)** → **escalate-if-unsure → cloud** | text-first platform; vision only when copy is thin |
| Match scoring | is this comment a match? | Local → escalate-if-unsure → cloud | |
| Vision / OCR | read text in carousels, documents, image posts | Local (Qwen2.5-VL 7B class) — **v1, secondary** | load-on-demand, then unload; skipped on text-only posts |
| Audio (v2) | native-video voiceover transcript | Local (Whisper) | rare, runs last |

One model resident at a time (36GB cap). **Batch by stage** — run the text gate across posts, then load vision once for the batch that needs it, unload, then escalate the unsure minority to cloud. Cloud tier needs fallback (retry queue or degrade-to-local + flag) and per-campaign spend logging.

## 7. State model (the spine)
Persisted in SQLite between sessions:
- **seen_posts** — dedupe; forward-only watermark.
- **comment cursors** — per post; "new comments since last poll."
- **watchlist** — match-rich posts, re-polled until aged out (~7–14 days).
- **session counters** — posts seen, **already-seen skips**, relevance passes, matches, escalations, spend.
- **feed-health flag** — set when the already-seen-skip ratio crosses threshold (feed tapped out / drifting).
- **account health flags** — last run, checkpoint state, empty-interception canary.
- **match status** — keyed on `comment_id`; survives re-scrapes (re-poll never overwrites human status).

Writes idempotent on `comment_id`. Killed mid-run → resume from state.

## 8. Match record (schema)
`campaign_id, post_id, comment_id, username, text, lang, score, reason, extracted, status, captured_at`
- `score` / `reason` — interpreted per the active brief.
- `extracted` — brief-defined JSON (lead → name/title/company/intent; partner → post author; signal → topic).
- `status` ∈ { new, reviewing, confirmed, discarded }

## 9. Failure handling (three tiers)
- **Auto-skip + continue:** transient (post won't load, single parse fails).
- **Soft flag (continue, surface on dashboard):** feed exhaustion/drift (too many already-seen), spend cap hit, cloud degraded → degrade-to-local + flag.
- **Halt + alert human (stop session):** login expired, checkpoint/captcha/challenge, account restriction, or empty interception for N posts (endpoint drift). The bot never attempts to resolve a checkpoint.

## 10. Pacing & feed steering
- Weeks 1–2: manual warming only, no automation. **LinkedIn runs the most aggressive automation enforcement of any platform here** — warm a credible, complete profile and ramp the slowest.
- **Steering is manual + recurring.** The operator follows the right people, hashtags, and companies, and reads matching content by hand; the feed rewards this. Expect a recurring few-minutes maintenance task, not one-time setup. A rising already-seen-skip ratio (§7) is the signal to go re-steer (follow more, widen hashtags).
- Agent runs: ramp from very low — ~1 session/day, 10–20 min, 10–25 posts; dwell 3–30s, between-post 3–10s, randomized; daytime only. Ramp until resistance, then hold well below it. Caps discovered empirically and kept conservative.

---

## 11. Scope: v1 vs v2

### v1 — Prove the loop (manual · headless)
- Manual account warming (complete, credible profile) + follow/hashtag seeding; ongoing re-steering.
- CDP attach to warmed Chrome.
- Feed discovery loop: dwell-on-relevant / scroll-past-irrelevant.
- **Relevance gate: post copy → carousel/image text (vision/OCR, when copy is thin) → escalate-if-unsure to cloud.**
- Interception of posts + comments; top-level comments; reply-expansion only on matching comments.
- Comment cascade: local pre-filter → local scoring → escalate-if-unsure to cloud.
- **Vision/OCR tier (reads carousel/document/image text).**
- SQLite store: full schema + state model + resume.
- soul.md + campaign.md, with one real brief (lead-gen) as proof.
- Three-tier failure handling (auto-skip / soft-flag / halt-alert).
- **Session counters + tired-feed flag.**
- Pacing engine (conservative caps).
- **Panel — read surfaces:** matches table (filter/sort/status-mark), health/canary panel (incl. feed-health flag), OpenRouter spend.
- Operation: operator triggers sessions manually. Follow-up: human, off-platform.

**v1 done =** account survives weeks unrestricted · sessions complete and resume cleanly · matches land with validated precision against a hand-labeled set, on at least one real brief · tired-feed flag fires correctly.

### v2 — Intelligence + automation
- **OpenJarvis integration:** frozen scraper as a scheduled skill; unattended **scheduled** (not continuous) mode; memory layer.
- **Audio tier:** Whisper transcription of native-video voiceover.
- **Panel — write surfaces:** campaign editor (writes campaign.md; engine re-reads only at session start); **review queue** (each confirm/discard captured as labeled training data → tunes thresholds + escalation); richer status workflow.
- **Memory:** optional Obsidian vault as accumulating market model.
- **Scale:** multi-campaign (parallel briefs); discovery feedback tuning.

**v2 done =** runs on a schedule with no babysitting · review-queue feedback measurably lifts precision · ≥2 distinct briefs run on the same engine with no code change.

---

## 12. Open questions
- Exact session/daily caps — discover empirically, kept the most conservative of any platform here.
- Local model sizes (text + vision both resident-capable?) — measure real free memory on the M4 first.
- The Voyager post + comment endpoints — identify once in DevTools; expect drift.
- Already-seen-skip threshold that should trip the tired-feed flag.
- Whether to also persist *scored non-matches* (needed only if the market-intelligence direction is pursued) before the schema is frozen.
- Retention TTL for stored personal data; GDPR posture if targets extend beyond CIS.
- How far to schematize the `extracted` blob vs leave it free-form per brief.

## 13. Risks
| Risk | Mitigation |
|---|---|
| Account ban / restriction (highest enforcement) | read-only · credible-profile warming · CDP-attach · slowest human-paced ramp · halt-on-resistance |
| Endpoint drift breaks scraping | intercept Voyager by URL + response shape, not hardcoded IDs; empty-interception canary halts |
| Relevance judged half-blind (copy only) | vision/OCR reads carousel/image text **in v1** + escalate-if-unsure to cloud |
| Local model weak on Uzbek | cloud escalation on both gate and scoring + hand-labeled validation set |
| Feed runs dry / drifts off-niche | already-seen-skip counter → dashboard flag → operator follows more / re-steers |
| Shared-file concurrency | SQLite WAL; config read at session start only; status keyed on comment_id |
| Data / privacy | store minimum · retention TTL · use only for the campaign's stated purpose · human-led follow-up |
