# Telegram Channel/Group Message-Comment Discovery Agent — PRD (v1 & v2)

**Status:** Draft v0.1 · **Date:** 2026-06-17 · *product name TBD*

> Generic engine. It knows no vertical and no goal — all domain meaning lives in the campaign brief. The flagship first campaign is lead-gen (e.g. a SaaS product), but that is one brief, not a built-in assumption.

> **Sibling spec.** This is one of a family of per-platform agents (Instagram, YouTube, Facebook, LinkedIn, X, Threads, Reddit, TikTok, Quora, Pinterest) that share an identical engine and contract. A future **campaign orchestrator** lets an operator author one `campaign.md`, fan it out to the platform engines they pick, and pool every match into a single leads dashboard over the shared SQLite contract. This PRD is self-contained; the orchestrator is a separate doc.

> **Why Telegram differs.** Unlike the rest of the family, Telegram is **not** an algorithmic media feed read by intercepting a web client's internal API. It is a set of **public channels and groups** with a **first-class API** (MTProto / Bot API). Discovery is *deterministic* — the operator seeds the channels/groups to watch (closest to the Reddit subreddit model), and the engine reads their recent messages and discussion replies. There is no "For You" feed to steer.

---

## 1. Summary
A local-first agent that reads **public Telegram channels and groups the operator seeds**, walks their recent messages, reads each message's text (and on-screen text of any image posts), then reads the discussion replies / group messages and scores/extracts them against a **campaign-defined brief**, surfacing matches in an admin panel for human follow-up. The engine is domain- and goal-agnostic and must run against a brief it has never seen with zero code change. Runs on the operator's own Mac in short daytime bursts — not on a server, not 24/7.

*A **match** = any message/reply scoring above threshold against the active brief. For the lead-gen campaign, a match is a lead.*

## 2. Goals / Non-Goals

**Goals**
- Surface brief-matching messages/replies from public channels and groups, where relevance, goal type, what-to-extract, and threshold all live in `campaign.md`.
- Survive on Telegram — behave like a real human account, never trip anti-spam/flood enforcement.
- Keep heavy data local; use cloud only as a targeted escalation when the local model is unsure.
- Operator-controllable and observable through an admin panel.

**Non-Goals (both versions)**
- The bot never sends messages, reacts, joins via invite-spam, comments, or posts. Read-only.
- Never solves login challenges/2FA — it halts and alerts a human.
- No multi-account farming, no cold mass-outreach (no unsolicited DMs).
- No reading private chats the operator's account is not legitimately a member of; **public channels/groups only**.
- No reselling collected data (separate product direction, out of scope).
- The engine hardcodes no vertical and no goal.

## 3. Core principles (locked)
- **Generic engine** — knows no vertical, no goal; runnable against an unseen brief with zero code change.
- **Local execution** on the operator's machine (M4 Max, 36GB), bursty daytime runs.
- **First-class API, not web scraping** — read messages through **MTProto (Telethon)** with the operator's warmed account session (or Bot API where a bot has legitimate access). Telegram exposes structured message objects, so there is no web client to attach to and no internal traffic to intercept — unlike the other platforms in the family.
- **Read-only collection** — passive reading only; the bot never sends, reacts, or joins anything it was not already a member of.
- **Discovery is deterministic + operator-seeded** — `campaign.md` lists the channels/groups (`seed_channels`); the engine reads their recent messages. No algorithmic feed to steer; re-nudging means editing the seed list.
- **Escalate only when unsure** — both relevance and match decisions run cheap-local first and ask the cloud model only on low-confidence cases.
- **SQLite is the only contract** between engine and panel (WAL mode; two processes meet at the DB).
- **soul.md + campaign.md** — config read only at session start.
- **Generic criteria & goal, fixed core record** — relevance/goal/extraction are free-text brief; every match lands in identical core columns plus a brief-defined `extracted` blob.

## 4. soul.md vs campaign.md (the generalization boundary)
- **soul.md** — engine identity + safety: read-only, pacing, halt-on-resistance, never act. Domain-free.
- **campaign.md** — seed direction (which **channels/groups** to read), relevance definition, goal type, what to extract & score, threshold, language mix. Telegram-specific knob: `seed_channels` (public @handles / invite links the account already belongs to).
- Test of "generic": swap `campaign.md` and the same binary runs a different hunt, no code change.

## 5. Architecture (target)
```
Engine (headless) ──► SQLite ◄── Admin panel (local web app)
        └─► model router ─► local text · local vision (MLX/Ollama) | cloud (OpenRouter)
```
- **Engine:** Python + **Telethon (MTProto)** with a warmed account session. Iterates seeded channels/groups, reads message text + on-screen text of image posts, reads discussion replies / group messages, runs the cascade against the active brief, writes matches + state. Maps cleanly onto the shared `FeedSource` interface: a **message/post ≈ `Reel`**, a **reply / group message ≈ `Comment`**.
- **Model router:** call sites — `classifyText` (relevance + match) and `classifyImage` (on-screen text of image posts). Telegram is text-first, so vision is secondary and audio is not used in v1.
- **Store:** SQLite (WAL). Matches, state, status, spend. Carries the shared `platform` dimension (`telegram`).
- **Panel:** local web app reading/writing SQLite. Never calls the engine.
- **v2 shell:** OpenJarvis wraps the frozen engine as a scheduled skill.

## 6. Model routing
| Stage | Task | Where | Notes |
|---|---|---|---|
| Relevance gate | is this message/post relevant to the brief? | Local text (message text) → **local vision only for image posts** → **escalate-if-unsure → cloud** | text-first platform; most posts judged on text alone |
| Match scoring | is this reply/message a match? | Local → escalate-if-unsure → cloud | |
| Vision / OCR | read price / project / text inside an image post | Local (Qwen2.5-VL 7B class) — **v1, secondary** | load-on-demand only for image posts, then unload |
| Audio / transcript | — | **not used in v1** | voice notes are rare in lead channels; defer to v2 if needed |

One model resident at a time (36GB cap). **Batch by stage** — run the text gate across messages, then load vision once for the image-post minority, unload, then escalate the unsure remainder to cloud. Cloud tier needs fallback (retry queue or degrade-to-local + flag) and per-campaign spend logging.

## 7. State model (the spine)
Persisted in SQLite between sessions:
- **seen_messages** — dedupe; forward-only watermark (per channel, by message id).
- **reply cursors** — per message/discussion thread; "new replies since last poll."
- **watchlist** — match-rich messages/threads, re-polled until aged out (~7–14 days).
- **session counters** — channels read, messages seen, **already-seen skips**, relevance passes, matches, escalations, spend.
- **feed-health flag** — set when a seeded channel returns mostly already-seen messages (channel quiet / tapped out).
- **account health flags** — last run, login/session state, flood-wait canary.
- **match status** — keyed on `comment_id` (the reply/message id); survives re-scrapes (re-poll never overwrites human status).

Writes idempotent on `comment_id`. Killed mid-run → resume from state. Telegram message ids are monotonic per chat — the seen-watermark is a simple `max(message_id)` per channel.

## 8. Match record (schema)
`campaign_id, platform, channel_id, message_id, comment_id, username, text, lang, score, reason, extracted, status, captured_at`
- Maps onto the shared core record: `channel_id`→ the seeded source, `message_id` → the parent post (the `reel_id` slot), `comment_id` → the reply/message scored.
- `score` / `reason` — interpreted per the active brief.
- `extracted` — brief-defined JSON (lead → channel/intent; partner → message author; signal → topic).
- `status` ∈ { new, reviewing, confirmed, discarded }

## 9. Failure handling (three tiers)
- **Auto-skip + continue:** transient (a message fails to fetch, single parse fails).
- **Soft flag (continue, surface on dashboard):** channel quiet/tapped out (mostly already-seen), spend cap hit, cloud degraded → degrade-to-local + flag, **flood-wait** returned by Telegram (back off + flag, do not hammer).
- **Halt + alert human (stop session):** session/login expired, 2FA/login challenge, account limited/banned, or repeated flood-waits indicating the account is being throttled. The bot never attempts to resolve a login challenge.

## 10. Pacing & seeding
- Weeks 1–2: warm a real account with genuine membership/history; join the seeded public channels by hand. Read-only reading via MTProto carries lower ban risk than web automation, but flood limits are real.
- **Seeding is manual + explicit, fully deterministic.** The operator lists channels/groups in `campaign.md`; there is no algorithm to drift. Re-steering = editing the seed list; a rising already-seen-skip ratio (§7) signals a channel has gone quiet.
- Agent runs: ramp from low — ~1–3 sessions/day, 20–40 min; read recent messages per seeded channel (bounded window, not full history); between-request delays 2–8s randomized; **respect every `FLOOD_WAIT` exactly**; daytime only. Caps discovered empirically.

---

## 11. Scope: v1 vs v2

### v1 — Prove the loop (manual · headless)
- Manual account warming + manual channel/group seeding via `campaign.md`.
- Telethon (MTProto) attach to a warmed account session (read-only).
- Seeded-channel read loop: walk recent messages per channel; dedupe on message id.
- **Relevance gate: message text → on-screen text (vision/OCR) for image posts → escalate-if-unsure to cloud.**
- Read discussion replies / group messages; reply-expansion only on matching threads; follow continuation by message id.
- Comment/reply cascade: local pre-filter → local scoring → escalate-if-unsure to cloud.
- **Vision/OCR tier (secondary, image posts only). No audio in v1.**
- SQLite store: full schema + state model + resume; carries `platform = telegram`.
- soul.md + campaign.md, with one real brief (lead-gen) as proof.
- Three-tier failure handling (auto-skip / soft-flag / halt-alert), including `FLOOD_WAIT` handling.
- **Session counters + quiet-channel flag.**
- Pacing engine with flood-wait compliance.
- **Panel — read surfaces:** matches table (filter/sort/status-mark), health/canary panel (incl. quiet-channel flag), OpenRouter spend.
- Operation: operator triggers sessions manually. Follow-up: human, off-platform.

**v1 done =** account survives weeks unrestricted · sessions complete and resume cleanly · matches land with validated precision against a hand-labeled set, on at least one real brief · quiet-channel flag fires correctly · flood-waits are respected with no account throttling.

### v2 — Intelligence + automation
- **OpenJarvis integration:** frozen scraper as a scheduled skill; unattended **scheduled** (not continuous) mode; memory layer.
- **Voice notes (optional):** transcript tier only if lead channels prove to carry voice content.
- **Panel — write surfaces:** campaign editor (writes campaign.md incl. `seed_channels`; engine re-reads only at session start); **review queue** (each confirm/discard captured as labeled training data → tunes thresholds + escalation); richer status workflow.
- **Memory:** optional Obsidian vault as accumulating market model.
- **Scale:** multi-campaign (parallel briefs); discovery feedback (suggest adjacent channels).

**v2 done =** runs on a schedule with no babysitting · review-queue feedback measurably lifts precision · ≥2 distinct briefs run on the same engine with no code change.

---

## 12. Open questions
- **Access method (primary decision):** MTProto via Telethon (full read of public channels the account belongs to) vs Bot API (simpler, but bots only see messages in groups they're added to and can't read arbitrary channel history). **Default recommendation: Telethon with a warmed user-account session** for channel reach; revisit if account risk proves too high.
- Exact session/daily caps and safe request rates before `FLOOD_WAIT` — discover empirically.
- Discussion-group linkage: channels with comments attach a linked discussion group — confirm the Telethon path to read those replies and their pagination.
- Local model strength on Uzbek/Russian message text — cloud escalation + hand-labeled validation set.
- Already-seen-skip threshold that should trip the quiet-channel flag.
- Retention TTL for stored personal data; GDPR posture if targets extend beyond CIS.
- How far to schematize the `extracted` blob vs leave it free-form per brief.
- Whether private-but-operator-member groups are ever in scope (default: public only).

## 13. Risks
| Risk | Mitigation |
|---|---|
| Account ban / limitation / flood-wait | read-only · genuine-membership warming · human-paced ramp · respect every FLOOD_WAIT · halt-on-resistance |
| Bot API can't read channel history | use Telethon user-session for reach; Bot API only where it legitimately sees the messages |
| Seeded channel goes quiet / off-niche | already-seen-skip counter → dashboard flag → operator edits seed list |
| Relevance judged half-blind on image posts | vision/OCR for image posts + escalate-if-unsure to cloud |
| Local model weak on Uzbek | cloud escalation on both gate and scoring + hand-labeled validation set |
| Discussion-reply pagination drift | follow continuation by message id, not hardcoded offsets; canary on empty fetches |
| Shared-file concurrency | SQLite WAL; config read at session start only; status keyed on comment_id |
| Data / privacy | public sources only · store minimum · retention TTL · use only for the campaign's stated purpose · human-led follow-up |
