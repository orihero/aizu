# Pinterest Pin-Comment Discovery Agent — PRD (v1 & v2)

**Status:** Draft v0.1 · **Date:** 2026-06-12 · *product name TBD*

> Generic engine. It knows no vertical and no goal — all domain meaning lives in the campaign brief. The flagship first campaign is lead-gen (e.g. a SaaS product), but that is one brief, not a built-in assumption.

> **Sibling spec.** This is one of a family of per-platform agents (Instagram, Facebook, LinkedIn, X, Threads, YouTube, Reddit, TikTok, Quora) that share an identical engine and contract. A future **campaign orchestrator** lets an operator author one `campaign.md`, fan it out to the platform engines they pick, and pool every match into a single leads dashboard over the shared SQLite contract. This PRD is self-contained; the orchestrator is a separate doc.

---

## 1. Summary
A local-first agent that discovers relevant Pins through Pinterest's home feed, Search, and Boards, **reads the pin title, description, and — above all — the text inside the image**, then reads the comments and scores/extracts them against a **campaign-defined brief**, surfacing matches in an admin panel for human follow-up. The engine is domain- and goal-agnostic and must run against a brief it has never seen with zero code change. Runs on the operator's own Mac in short daytime bursts — not on a server, not 24/7.

*A **match** = any comment scoring above threshold against the active brief. For the lead-gen campaign, a match is a lead.*

## 2. Goals / Non-Goals

**Goals**
- Surface brief-matching comments from public Pins, where relevance, goal type, what-to-extract, and threshold all live in `campaign.md`.
- Survive on Pinterest — behave like a real human account, never trip automation enforcement.
- Keep heavy data local; use cloud only as a targeted escalation when the local model is unsure.
- Operator-controllable and observable through an admin panel.

**Non-Goals (both versions)**
- The bot never likes, saves, follows, comments, or messages. View + scroll only.
- Never solves checkpoints/captchas — it halts and alerts a human.
- No multi-account farming, no cold mass-message outreach.
- No reselling collected data (separate product direction, out of scope).
- The engine hardcodes no vertical and no goal.

## 3. Core principles (locked)
- **Generic engine** — knows no vertical, no goal; runnable against an unseen brief with zero code change.
- **Local execution** on the operator's machine (M4 Max, 36GB), bursty daytime runs.
- **Real Chrome over CDP** — attach to a warmed, logged-in profile; never launch a vanilla automation browser.
- **Read-only collection** — passive viewing only; the only "actions" are human-like view and scroll.
- **Feed steering is manual + recurring** — the operator seeds discovery by hand (following the right topics/boards, viewing the right Pins) during warming and re-nudges it periodically as it drifts (see §10).
- **Network interception, not DOM scraping** — read the page's own internal API traffic; never craft API calls.
- **Escalate only when unsure** — both relevance and match decisions run cheap-local first and ask the cloud model only on low-confidence cases.
- **SQLite is the only contract** between engine and panel (WAL mode; two processes meet at the DB).
- **soul.md + campaign.md** — config read only at session start.
- **Generic criteria & goal, fixed core record** — relevance/goal/extraction are free-text brief; every match lands in identical core columns plus a brief-defined `extracted` blob.

## 4. soul.md vs campaign.md (the generalization boundary)
- **soul.md** — engine identity + safety: read-only, pacing, halt-on-resistance, never act. Domain-free.
- **campaign.md** — warming/seed direction (which topics, boards, search queries), relevance definition, goal type, what to extract & score, threshold, language mix.
- Test of "generic": swap `campaign.md` and the same binary runs a different hunt, no code change.

## 5. Architecture (target)
```
Engine (headless) ──► SQLite ◄── Admin panel (local web app)
        └─► model router ─► local text · local vision (MLX/Ollama) | cloud (OpenRouter)
```
- **Engine:** Python + Playwright over CDP. Walks the feed/Search/Boards, reads title + description + **image text**, intercepts JSON, runs the cascade against the active brief, writes matches + state.
- **Model router:** call sites — `classifyText`, `classifyImage` (image text / relevance), `transcribe` (v2) — each routes per-tier to local or OpenRouter.
- **Store:** SQLite (WAL). Matches, state, status, spend.
- **Panel:** local web app reading/writing SQLite. Never calls the engine.
- **v2 shell:** OpenJarvis wraps the frozen engine as a scheduled skill.

## 6. Model routing
| Stage | Task | Where | Notes |
|---|---|---|---|
| Relevance gate | is this Pin relevant to the brief? | Local text (title + description) → **local vision (image text — primary signal)** → **escalate-if-unsure → cloud** | image-first platform; vision is central, not a backup |
| Match scoring | is this comment a match? | Local → escalate-if-unsure → cloud | |
| Vision / OCR | read price / project / text inside the Pin image | Local (Qwen2.5-VL 7B class) — **v1, primary** | runs on nearly every Pin; load-resident for the session |
| Audio (v2) | Idea-Pin / video-pin voiceover transcript | Local (Whisper) | only on video Pins; rare, runs last |

One model resident at a time (36GB cap). Because vision runs on nearly every Pin, **keep the vision model resident** for the session rather than load-on-demand; **batch** the text pass first to skip clearly-irrelevant Pins before paying for vision, then escalate the unsure minority to cloud. Cloud tier needs fallback (retry queue or degrade-to-local + flag) and per-campaign spend logging.

## 7. State model (the spine)
Persisted in SQLite between sessions:
- **seen_pins** — dedupe; forward-only watermark.
- **comment cursors** — per Pin; "new comments since last poll."
- **watchlist** — match-rich Pins, re-polled until aged out (~7–14 days).
- **session counters** — Pins seen, **already-seen skips**, relevance passes, matches, escalations, spend.
- **feed-health flag** — set when the already-seen-skip ratio crosses threshold (feed/boards tapped out / drifting).
- **account health flags** — last run, checkpoint state, empty-interception canary.
- **match status** — keyed on `comment_id`; survives re-scrapes (re-poll never overwrites human status).

Writes idempotent on `comment_id`. Killed mid-run → resume from state.

## 8. Match record (schema)
`campaign_id, pin_id, comment_id, username, text, lang, score, reason, extracted, status, captured_at`
- `score` / `reason` — interpreted per the active brief.
- `extracted` — brief-defined JSON (lead → username/intent; partner → pin author; signal → topic).
- `status` ∈ { new, reviewing, confirmed, discarded }

## 9. Failure handling (three tiers)
- **Auto-skip + continue:** transient (Pin won't load, single parse fails).
- **Soft flag (continue, surface on dashboard):** feed exhaustion/drift (too many already-seen), spend cap hit, cloud degraded → degrade-to-local + flag.
- **Halt + alert human (stop session):** login expired, checkpoint/captcha/challenge, account restriction, or empty interception for N Pins (endpoint drift). The bot never attempts to resolve a checkpoint.

## 10. Pacing & feed steering
- Weeks 1–2: light warming only. Read-only ban risk on Pinterest is low; warm a normal account.
- **Steering is manual + recurring.** The operator follows the right topics/boards and views matching Pins by hand; the home feed and related-Pins both reward this. Note many Pins are commercial and carry buyer intent, which suits lead-gen. A rising already-seen-skip ratio (§7) is the signal to go re-steer (follow more topics/boards).
- Agent runs: ramp from low — ~1–2 sessions/day, 15–30 min, 30–60 Pins; dwell 2–15s, between-Pin 2–6s, randomized; daytime only. Ramp until resistance, then hold below it. Caps discovered empirically.

---

## 11. Scope: v1 vs v2

### v1 — Prove the loop (manual · headless)
- Manual account warming + topic/board seeding; ongoing re-steering.
- CDP attach to warmed Chrome.
- Home + Search + Boards discovery loop: view-relevant / scroll-past-irrelevant.
- **Relevance gate: title + description → image text (vision/OCR — primary) → escalate-if-unsure to cloud.**
- Interception of Pins + comments; top-level comments; reply-expansion only on matching comments.
- Comment cascade: local pre-filter → local scoring → escalate-if-unsure to cloud.
- **Vision/OCR tier (primary — reads image text on nearly every Pin).**
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
- **Audio tier:** Whisper transcription of Idea-Pin / video-pin voiceover.
- **Panel — write surfaces:** campaign editor (writes campaign.md; engine re-reads only at session start); **review queue** (each confirm/discard captured as labeled training data → tunes thresholds + escalation); richer status workflow.
- **Memory:** optional Obsidian vault as accumulating market model.
- **Scale:** multi-campaign (parallel briefs); discovery feedback tuning.

**v2 done =** runs on a schedule with no babysitting · review-queue feedback measurably lifts precision · ≥2 distinct briefs run on the same engine with no code change.

---

## 12. Open questions
- Exact session/daily caps — discover empirically (read-only is comparatively safe).
- Local model sizes — vision is resident most of the session; confirm text + vision coexistence on the M4 first.
- The Pin + comment endpoints — identify once in DevTools; expect drift (note: comments are sparser on Pinterest than on most platforms).
- Whether thin comment volume warrants treating the Pin author / saver as the lead instead of commenters, for some briefs.
- Already-seen-skip threshold that should trip the tired-feed flag.
- Whether to also persist *scored non-matches* (needed only if the market-intelligence direction is pursued) before the schema is frozen.
- Retention TTL for stored personal data; GDPR posture if targets extend beyond CIS.
- How far to schematize the `extracted` blob vs leave it free-form per brief.

## 13. Risks
| Risk | Mitigation |
|---|---|
| Account ban / restriction | read-only · normal-account warming · CDP-attach · human-paced ramp · halt-on-resistance |
| Endpoint drift breaks scraping | intercept by URL + response shape, not hardcoded IDs; empty-interception canary halts |
| Relevance judged half-blind (text only) | vision/OCR reads image text as the **primary** signal **in v1** + escalate-if-unsure to cloud |
| Sparse comments → thin match yield | for some briefs treat the Pin author/saver as the lead; lean on vision relevance to prioritize high-yield Pins |
| Local model weak on Uzbek | cloud escalation on both gate and scoring + hand-labeled validation set |
| Feed runs dry / drifts off-niche | already-seen-skip counter → dashboard flag → operator follows more topics/boards |
| Shared-file concurrency | SQLite WAL; config read at session start only; status keyed on comment_id |
| Data / privacy | store minimum · retention TTL · use only for the campaign's stated purpose · human-led follow-up |
