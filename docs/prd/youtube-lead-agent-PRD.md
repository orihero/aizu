# YouTube Video-Comment Discovery Agent — PRD (v1 & v2)

**Status:** Draft v0.1 · **Date:** 2026-06-12 · *product name TBD*

> Generic engine. It knows no vertical and no goal — all domain meaning lives in the campaign brief. The flagship first campaign is lead-gen (e.g. a SaaS product), but that is one brief, not a built-in assumption.

> **Sibling spec.** This is one of a family of per-platform agents (Instagram, Facebook, LinkedIn, X, Threads, Reddit, TikTok, Quora, Pinterest) that share an identical engine and contract. A future **campaign orchestrator** lets an operator author one `campaign.md`, fan it out to the platform engines they pick, and pool every match into a single leads dashboard over the shared SQLite contract. This PRD is self-contained; the orchestrator is a separate doc.

---

## 1. Summary
A local-first agent that discovers relevant videos (long-form + Shorts) through YouTube's home feed, Search, and channel/topic subscriptions, **reads the title, description, on-screen text, and spoken transcript of the video**, then reads the comments and scores/extracts them against a **campaign-defined brief**, surfacing matches in an admin panel for human follow-up. The engine is domain- and goal-agnostic and must run against a brief it has never seen with zero code change. Runs on the operator's own Mac in short daytime bursts — not on a server, not 24/7.

*A **match** = any comment scoring above threshold against the active brief. For the lead-gen campaign, a match is a lead.*

## 2. Goals / Non-Goals

**Goals**
- Surface brief-matching comments from public videos, where relevance, goal type, what-to-extract, and threshold all live in `campaign.md`.
- Survive on YouTube — behave like a real human account, never trip automation enforcement.
- Keep heavy data local; use cloud only as a targeted escalation when the local model is unsure.
- Operator-controllable and observable through an admin panel.

**Non-Goals (both versions)**
- The bot never likes, subscribes, follows, comments, or posts. Watch + scroll only.
- Never solves checkpoints/captchas — it halts and alerts a human.
- No multi-account farming, no cold mass-outreach.
- No reselling collected data (separate product direction, out of scope).
- The engine hardcodes no vertical and no goal.

## 3. Core principles (locked)
- **Generic engine** — knows no vertical, no goal; runnable against an unseen brief with zero code change.
- **Local execution** on the operator's machine (M4 Max, 36GB), bursty daytime runs.
- **Real Chrome over CDP** — attach to a warmed, logged-in profile; never launch a vanilla automation browser.
- **Read-only collection** — passive viewing only; the only "actions" are human-like watch and scroll.
- **Feed steering is manual + recurring** — the operator seeds discovery by hand (subscribing to the right channels, watching the right content, searching topics) during warming and re-nudges it periodically as it drifts (see §10).
- **Network interception, not DOM scraping** — read the page's own internal API traffic (InnerTube); never craft API calls.
- **Escalate only when unsure** — both relevance and match decisions run cheap-local first and ask the cloud model only on low-confidence cases.
- **SQLite is the only contract** between engine and panel (WAL mode; two processes meet at the DB).
- **soul.md + campaign.md** — config read only at session start.
- **Generic criteria & goal, fixed core record** — relevance/goal/extraction are free-text brief; every match lands in identical core columns plus a brief-defined `extracted` blob.

## 4. soul.md vs campaign.md (the generalization boundary)
- **soul.md** — engine identity + safety: read-only, pacing, halt-on-resistance, never act. Domain-free.
- **campaign.md** — warming/seed direction (which channels, topics, search queries), relevance definition, goal type, what to extract & score, threshold, language mix.
- Test of "generic": swap `campaign.md` and the same binary runs a different hunt, no code change.

## 5. Architecture (target)
```
Engine (headless) ──► SQLite ◄── Admin panel (local web app)
        └─► model router ─► local text · local vision (MLX/Ollama) · local audio (Whisper) | cloud (OpenRouter)
```
- **Engine:** Python + Playwright over CDP. Walks the feed/Search/channels, reads title + description + on-screen text + transcript, intercepts JSON (InnerTube), runs the cascade against the active brief, writes matches + state.
- **Model router:** call sites — `classifyText`, `classifyImage` (on-screen text / relevance), `transcribe` (voiceover) — each routes per-tier to local or OpenRouter.
- **Store:** SQLite (WAL). Matches, state, status, spend.
- **Panel:** local web app reading/writing SQLite. Never calls the engine.
- **v2 shell:** OpenJarvis wraps the frozen engine as a scheduled skill.

## 6. Model routing
| Stage | Task | Where | Notes |
|---|---|---|---|
| Relevance gate | is this video relevant to the brief? | Local text (title + description) → **local vision (on-screen text)** + **local transcript** → **escalate-if-unsure → cloud** | video platform; vision **and** audio are central |
| Match scoring | is this comment a match? | Local → escalate-if-unsure → cloud | |
| Vision / OCR | read price / project / text burned into frames | Local (Qwen2.5-VL 7B class) — **v1** | load-on-demand, then unload |
| Audio / transcript | spoken voiceover transcript | Local (Whisper) — **v1, promoted** | prefer native captions when present; else transcribe |

One model resident at a time (36GB cap). **Batch by stage** — run the text gate across videos, then load vision/audio once for the batch that needs it, unload, then escalate the unsure minority to cloud. Native caption tracks (when available via interception) are cheaper than Whisper — use them first. Cloud tier needs fallback (retry queue or degrade-to-local + flag) and per-campaign spend logging.

## 7. State model (the spine)
Persisted in SQLite between sessions:
- **seen_videos** — dedupe; forward-only watermark.
- **comment cursors** — per video; "new comments since last poll."
- **watchlist** — match-rich videos, re-polled until aged out (~7–14 days).
- **session counters** — videos seen, **already-seen skips**, relevance passes, matches, escalations, spend.
- **feed-health flag** — set when the already-seen-skip ratio crosses threshold (feed tapped out / drifting).
- **account health flags** — last run, checkpoint state, empty-interception canary.
- **match status** — keyed on `comment_id`; survives re-scrapes (re-poll never overwrites human status).

Writes idempotent on `comment_id`. Killed mid-run → resume from state.

## 8. Match record (schema)
`campaign_id, video_id, comment_id, username, text, lang, score, reason, extracted, status, captured_at`
- `score` / `reason` — interpreted per the active brief.
- `extracted` — brief-defined JSON (lead → channel/intent; partner → video author; signal → topic).
- `status` ∈ { new, reviewing, confirmed, discarded }

## 9. Failure handling (three tiers)
- **Auto-skip + continue:** transient (video won't load, single parse fails).
- **Soft flag (continue, surface on dashboard):** feed exhaustion/drift (too many already-seen), spend cap hit, cloud degraded → degrade-to-local + flag.
- **Halt + alert human (stop session):** login expired, checkpoint/captcha/challenge, account restriction, or empty interception for N videos (endpoint drift). The bot never attempts to resolve a checkpoint.

## 10. Pacing & feed steering
- Weeks 1–2: manual warming only, no automation. Read-only watching carries lower ban risk than Instagram; warm a real account with genuine watch history.
- **Steering is manual + recurring, but semi-deterministic.** Subscriptions and search make discovery far less algorithm-dependent than a pure feed — the operator subscribes to the right channels and searches topics, and those reliably resurface relevant videos. Re-nudge periodically; a rising already-seen-skip ratio (§7) is the signal.
- Agent runs: ramp from low — ~1–3 sessions/day, 20–40 min, 20–50 videos; dwell scales to video length (sample, don't fully watch); between-video 2–8s, randomized; daytime only. Ramp until resistance, then hold below it. Caps discovered empirically.

---

## 11. Scope: v1 vs v2

### v1 — Prove the loop (manual · headless)
- Manual account warming (genuine watch history) + subscription/search seeding; ongoing re-steering.
- CDP attach to warmed Chrome.
- Home + Search + subscriptions discovery loop: dwell-on-relevant / scroll-past-irrelevant.
- **Relevance gate: title + description → on-screen-text (vision/OCR) + transcript (captions/Whisper) → escalate-if-unsure to cloud.**
- Interception of videos + comments; top-level comments; reply-expansion only on matching comments.
- Comment cascade: local pre-filter → local scoring → escalate-if-unsure to cloud.
- **Vision/OCR tier + audio/transcript tier (both in v1).**
- SQLite store: full schema + state model + resume.
- soul.md + campaign.md, with one real brief (lead-gen) as proof.
- Three-tier failure handling (auto-skip / soft-flag / halt-alert).
- **Session counters + tired-feed flag.**
- Pacing engine.
- **Panel — read surfaces:** matches table (filter/sort/status-mark), health/canary panel (incl. feed-health flag), OpenRouter spend.
- Operation: operator triggers sessions manually. Follow-up: human, off-platform.

**v1 done =** account survives weeks unrestricted · sessions complete and resume cleanly · matches land with validated precision against a hand-labeled set, on at least one real brief · tired-feed flag fires correctly.

### v2 — Intelligence + automation
- **OpenJarvis integration:** frozen scraper as a scheduled skill; unattended **scheduled** (not continuous) mode; memory layer.
- **Deeper audio:** speaker-aware / long-form transcript summarization (the basic transcript tier already ships in v1).
- **Panel — write surfaces:** campaign editor (writes campaign.md; engine re-reads only at session start); **review queue** (each confirm/discard captured as labeled training data → tunes thresholds + escalation); richer status workflow.
- **Memory:** optional Obsidian vault as accumulating market model.
- **Scale:** multi-campaign (parallel briefs); discovery feedback tuning.

**v2 done =** runs on a schedule with no babysitting · review-queue feedback measurably lifts precision · ≥2 distinct briefs run on the same engine with no code change.

---

## 12. Open questions
- Exact session/daily caps — discover empirically (read-only watching is comparatively safe).
- Local model sizes (text + vision + audio all resident-capable?) — measure real free memory on the M4 first; audio adds pressure.
- The InnerTube video + comment endpoints + caption track URLs — identify once in DevTools; expect drift; comments paginate (continuation tokens).
- Native-captions-vs-Whisper decision rule (cost vs coverage).
- Already-seen-skip threshold that should trip the tired-feed flag.
- Whether to also persist *scored non-matches* (needed only if the market-intelligence direction is pursued) before the schema is frozen.
- Retention TTL for stored personal data; GDPR posture if targets extend beyond CIS.
- How far to schematize the `extracted` blob vs leave it free-form per brief.

## 13. Risks
| Risk | Mitigation |
|---|---|
| Account ban / restriction | read-only · genuine-history warming · CDP-attach · human-paced ramp · halt-on-resistance |
| Comment endpoint paging drift breaks scraping | intercept InnerTube by URL + response shape, follow continuation tokens, not hardcoded IDs; empty-interception canary halts |
| Relevance judged half-blind | vision/OCR **and** transcript read in **v1** + escalate-if-unsure to cloud |
| Local model weak on Uzbek | cloud escalation on both gate and scoring + hand-labeled validation set |
| Feed runs dry / drifts off-niche | already-seen-skip counter → dashboard flag → operator subscribes more / refreshes searches |
| Audio tier oversubscribes memory | prefer native captions; load Whisper on-demand, unload after batch |
| Shared-file concurrency | SQLite WAL; config read at session start only; status keyed on comment_id |
| Data / privacy | store minimum · retention TTL · use only for the campaign's stated purpose · human-led follow-up |
