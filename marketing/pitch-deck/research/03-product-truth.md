# AIZU product-truth fact sheet

Sourced from the repo itself (code + shipped landing page), not from aspirational docs.
Primary sources: `CLAUDE.md`; `admin-panel/public/index.html` (shipped landing, verbatim);
`docs/architecture/overview.md`; `docs/architecture/engines.md`; `docs/README.md` +
`docs/prd/*`; `marketing/videos/madison/SCRIPT.md`; `engine/aizu/rbac.py`; `engine/aizu/billing.py`.

---

## 1. One-sentence product definition + elevator pitch (product's own words)

**Definition (from `docs/architecture/overview.md`):** "Aizu is a **read-only social-media
lead-discovery crawler**, delivered as a multi-tenant SaaS."

**Elevator pitch (landing hero, verbatim):**
- H1: "Meet customers while they decide"
- Lead: "Tell AIZU what you sell and who you serve. It returns a short list of ready-to-buy
  customers, every week."
- `<meta description>`: "Tell AIZU what you sell and who you serve. It returns a short list
  of qualified, ready-to-buy customers, every week."
- Footer tagline: "AIZU 合図 is a signal to act — not another platform to check."
- The `合図` (Japanese, "signal/cue") is a deliberate wordmark pairing — "AIZU" = "a signal
  to act," per the footer line.

## 2. The exact problem statement (verbatim, landing page)

Section id `core-hr`:
- H2: "Someone is searching for what you sell"
- Sub: "Right now, on some platform you don't have time to watch. AIZU collects them from
  every social and brings you the ones ready to buy."

Framing sub-head (bento section): "Built for people who sell direct" / "E-commerce and DTC
brands, agencies, service providers, coaches and creators, B2B sellers."

FAQ #3 sharpens the positioning against alternatives: "Is this cold outreach or a bought
list? No. AIZU doesn't sell contact databases, and it doesn't blast strangers. Every lead is
a person who has already signaled, in public and on their own, that they're looking for what
you sell. You're not interrupting anyone. You're answering."

## 3. How it actually works, end to end (5 non-engineer steps)

Grounded in `docs/architecture/overview.md` §"End-to-end data flow" and the landing's FAQ:

1. **Write one brief.** You describe what you sell and who you serve, once — either through
   the AI-assisted campaign interview or by hand. (Landing bento card 1: "One brief, every
   check. Write what you sell and who you serve once. AIZU checks every candidate against
   it, not the other way around.")
2. **AIZU picks its hunting grounds.** The brief maps to a platform (or several); the engine
   attaches to a warmed, already-logged-in browser session (or an official API) — it never
   launches its own browser and never DOM-scrapes or crafts unofficial API calls.
3. **It reads posts and comments, not people's inboxes.** For each post it runs a two-stage
   AI check: is this post on-topic (relevance gate), and — among the people commenting — who
   sounds like a buyer, not the seller (match scoring). Runs pace themselves to daytime hours
   with human-like dwell time.
4. **Matches become leads.** A commenter who clears your threshold is saved as a lead with
   their handle, what they said, when, and the specific fields you asked for (phone, budget,
   intent, etc.) — landing bento card 4: "Every lead, one drawer."
5. **You triage from one inbox.** All six platforms land in the same Leads dashboard, tagged
   by source, with New / Qualified / Contacted stages and a direct reply action — landing
   FAQ #5: "Your first leads typically arrive within days of describing your business, and
   then they keep coming, week after week."

## 4. Shipped platforms vs PRD-only

**Six SHIPPED platforms** — real engine under `engine/aizu/engines/`, listed in
`SUPPORTED_PLATFORMS` (`docs/README.md`, confirmed in `docs/architecture/engines.md`):

| Platform | Access | Vision/OCR | Engagement |
|---|---|---|---|
| Instagram | Playwright-over-CDP (warmed Chrome) | yes | opt-in like/follow/save/share |
| X (Twitter) | CDP (warmed Chrome) | yes (image/video posts) | none (read-only) |
| LinkedIn | CDP (warmed Chrome) | yes (when copy is thin) | none (read-only) |
| Reddit | Official Data API (OAuth2) | no | none (read-only) |
| YouTube | Official Data API v3 (API key) | no | none (read-only) |
| Telegram | MTProto (Telethon) / Bot API | no | none (read-only) |

The landing page's own "Six places people ask" section (`#integrations`) lists these
generically (social posts / professional network / public feed / video comments / community
threads / messaging groups) without naming the six platforms by brand — the marketing video
script is the only customer-facing asset that names all six explicitly: "Instagram ·
Telegram · YouTube · LinkedIn · Reddit · X."

**Must NOT be claimed — PRD-stage only, no engine exists** (`docs/prd/planned/`): Facebook,
Pinterest, Quora, Threads, TikTok. Do not present these as supported, "coming soon" is the
most that's defensible and only if the deck explicitly flags it as roadmap.

**Also caveat:** Instagram's local Uzbek speech-to-text relevance tier (KotibAI) is real code
but explicitly documented as **unverified against live Instagram** — the media-acquisition
step (fetching a reel's video URL) has only run against synthetic fixtures. Do not claim
"listens to video audio" as a proven capability; if mentioned at all, it must be framed as
gated, Uzbek-only, off by default, and unconfirmed on live traffic.

## 5. Real differentiators, each with the file that proves it

1. **Attach-never-launch (session safety).** The engine only ever attaches to a Chrome
   session the operator already opened and logged into over CDP; it never launches or owns
   a browser itself, and the external warmed Chrome must outlive the run.
   — `docs/architecture/overview.md` §6 ("Anti-wedge & safety"); landing testimonial card 1
   verbatim: "AIZU only ever attaches to a browser session you already opened. It never
   launches or owns one itself."
2. **Daytime pacing + human-like dwell.** Instagram/LinkedIn/X sessions halt outside roughly
   8am–9pm and pace with randomized dwell/between-post pauses to avoid platform action-blocks.
   — `docs/architecture/engines.md` (Instagram loop step 1, LinkedIn/X sections); landing
   testimonial card 2: "Every run paces itself to normal hours. No overnight bursts, no
   three-in-the-morning spikes."
3. **Per-campaign spend caps.** Every cloud LLM call in a run is budgeted against a cap set
   on the campaign; degrades to a soft "unknown" verdict + health flag on failure rather than
   overspending. — `docs/architecture/overview.md` §4 step 5; landing testimonial card 3:
   "Every model call in a run is budgeted against a cap you set on the campaign. Not an
   afterthought." Also `engine/aizu/core/router.py` (spend cap + cost tracking).
4. **Cooperative pause.** Runs are pausable at safe checkpoints via a sentinel file, not a
   hard kill. — `docs/architecture/overview.md` §6; `docs/architecture/engines.md` §"Crawl"
   step.
5. **Warming pool for cold accounts.** A distinct subsystem ramps a managed account through
   `observe → light → ramp → sustain` stages with per-action daily caps before it's trusted
   for harvest volume, expressed as a 0–100 "Warmth Score" that gates harvest. It is the
   *only* part of Aizu that performs deliberate writes (likes/follows/joins/reactions).
   — `docs/architecture/overview.md` §1, §4; `docs/architecture/engines.md` §"The warming
   subsystem."
6. **Server-enforced RBAC, not a rank.** Four roles (owner/admin/member/viewer) as an
   explicit action→allowed-roles matrix, deliberately non-linear (a `member` can edit leads
   but not view campaigns, which a `viewer` can) — enforced server-side in
   `engine/aizu/rbac.py` and mirrored, not re-implemented, in the frontend
   (`admin-panel/src/shared/auth/roles.ts`). Landing bento card 5 verbatim: "Owner, admin,
   member, viewer. Each sees exactly what their role allows, enforced on the server."
7. **Local-first, one shared SQLite DB.** Every process — bridge, on-demand run, worker
   fleet, desktop app — reads/writes one SQLite database (WAL mode); it is the durable
   source of truth, no separate app server or ORM. — `docs/architecture/overview.md` §3.
8. **Distributed worker fleet.** An off-cloud PULL-model sidecar (`aizu-worker`) long-polls
   for one job at a time, runs it against a *local* warmed Chrome in a killable child
   process, and ships results back — never accepts inbound connections. Selectable per-run
   via a superadmin execution-backend switch (in-process vs distributed).
   — `docs/architecture/overview.md` §2 ("The two off-cloud apps").
9. **Desktop app.** A Tauri 2 (Rust + system webview) shell that supervises the frozen
   `aizu-worker` binary with restart-on-crash and exposes a loopback-only control surface
   (pause/resume/stop/focus) — the control surface, not log scraping, is the single source
   of UI truth. — `docs/architecture/overview.md` §2; `desktop/`.
10. **AI-assisted campaign creation.** Landing FAQ implies (and the SPA's campaign flow
    provides) an AI-powered interview/generate step for authoring a brief — marketing script:
    "Just create a campaign with our AI-powered campaign creator." (Requires
    `OPENROUTER_API_KEY`; see CLAUDE.md.)
11. **Multi-tenant / agency-ready by construction.** Org plane with cookie sessions +
    4-role RBAC, a wholly separate superadmin plane (TOTP MFA, fail-closed IP allowlist,
    bootstrapped only out-of-band), and a distinct worker bearer-token plane — three
    isolated auth planes, not one shared login. — `docs/architecture/overview.md` §6.

## 6. Real pricing table, exactly as shipped

**On the landing page (`#plans`, verbatim), four visible tiers:**

| Tier | Monthly | Annual | Leads / month |
|---|---|---|---|
| Free | $0 (no card needed) | — | 10 qualified leads a month |
| Starter ("Most popular") | $24.99/mo | $249 billed yearly | 250 qualified leads a month |
| Pro | $149/mo | $1,490 billed yearly | 2,000 qualified leads a month |
| Scale | Custom | negotiated cap | "Your number" — agreed up front |

Footer line (verbatim): "Leads are qualified customers delivered to your account each month.
Upgrade, downgrade, or cancel anytime. No contracts, no lock-in." Headline: "Pay for
customers, not software." Sub: "Every plan delivers the same thing: qualified, ready-to-buy
customers, every month. The only question is how many you can handle."

**Discrepancy worth flagging to the deck author:** the billing engine
(`engine/aizu/billing.py::TIERS`) actually defines a **fifth, self-serve "Lite" tier — $9.99/
mo ($99/yr), 50 leads/month** — that is fully wired for checkout (`SELF_SERVE_TIERS`
includes `lite`) but is **not shown anywhere on the shipped landing page**. The marketing
video narration ("for as low as $9 a month") appears to reference this unlisted tier. Any
pitch-deck pricing slide should either (a) match the shipped landing exactly (Free/Starter/
Pro/Scale) or (b) explicitly note Lite as a real-but-unadvertised entry tier — do not
silently invent a number between $0 and $24.99.

## 7. ICP, in the landing page's own words

Bento section header: "Built for people who sell direct" — "E-commerce and DTC brands,
agencies, service providers, coaches and creators, B2B sellers."

Reinforced by the "For managers & leaders" card ("Know what a lead cost you — CPL trend,
channel comparison, spend by stage") and the "For teams & roles" card (multi-seat orgs with
distinct roles) — signals the ICP includes small-team operators/founders *and* agencies
running this for/with a team, not solo freelancers only.

## 8. Built vs aspirational — honest maturity read

**Genuinely built and running end-to-end:**
- All six platform engines (Instagram, X, LinkedIn, Reddit, YouTube, Telegram) with real
  discovery→relevance→comment-match→lead pipelines, per `docs/architecture/engines.md`.
- The bridge (stdlib HTTP server), RBAC, multi-tenant org model, scheduler + reclaim
  daemons, single-run lock, shared SQLite store.
- Billing integration with Polar.sh (checkout, portal, webhook verification), soft-enforced
  lead caps.
- Distributed worker sidecar + desktop app (Tauri) as real, wired components, not just specs.
- The shipped landing page itself (CoreShift design) is live production HTML/CSS/JS, not a
  mockup — though its own source comment flags: "See its 'Platform skins' note before
  treating this mockup as shippable" for the hand-drawn platform glyphs (no official brand
  assets are vendored — deliberate, to avoid brand-asset licensing/ToS issues, but a detail
  worth knowing).

**Aspirational / unverified / explicitly flagged as incomplete:**
- Instagram's Uzbek STT relevance tier — code exists, **never run against a live Instagram
  session**; multiple unconfirmed assumptions about how Instagram serves reel video
  (signed URL? progressive vs segmented delivery?). The doc explicitly says: "Do not claim
  this capability works end-to-end until it has been checked against a live warmed session."
- Five more platforms (Facebook, Pinterest, Quora, Threads, TikTok) are PRD-only, no code.
- Docs/Privacy/Terms pages: the landing page's own footer comment says these "TODO: no
  Docs/Privacy/Terms pages exist yet anywhere in the repo" — left as `href="#"` placeholders.
- Social profile links (Instagram/X/TikTok icons in the footer) are also placeholders — "no
  real AIZU social profiles exist yet on any of these platforms."
- The marketing video (`marketing/videos/madison/`) is itself unfinished: "No voiceover has
  been recorded or generated yet," scenes 06 and 08 use "provisional footage," and the
  narration copy/scene list are both explicitly marked "not locked."

## 9. What a diligent investor would ask about (risk slide, blunt)

1. **Platform ToS / automation exposure.** Three of six engines (Instagram, LinkedIn, X)
   attach to a real, logged-in browser session and read a platform's private internal JSON
   traffic by intercepting network responses (not the public API) — this is exactly the kind
   of automated access most social platforms' ToS prohibit, even though Aizu is careful to be
   read-only-by-default and to never auto-solve a checkpoint/CAPTCHA (the code explicitly
   halts and requires a human to resolve it manually, calling automated challenge-solving
   "how accounts get banned" — `docs/architecture/engines.md` §9). Ban/account-loss risk is
   real and structural, not hypothetical, and it scales with usage.
2. **Single-run lock.** By default only one engine run executes at a time per box/org path
   ("only one run at a time on one box" — `docs/architecture/overview.md` §2). The
   distributed worker fleet exists to relax this, but it's an opt-in superadmin switch, not
   the default — throughput and multi-campaign concurrency at scale is an open question
   worth probing on how many orgs/campaigns realistically run in parallel today.
3. **LLM dependency and cost pass-through.** Every live run needs `OPENROUTER_API_KEY`; the
   two-stage cascade (relevance + match, with escalate-band retries) means uncertain calls
   run twice through a paid model. Spend caps degrade calls to "unknown" on cap breach rather
   than fail outright, which protects the business financially but can silently degrade lead
   quality/coverage under cost pressure — worth asking how often that degrade path fires in
   production. (User's own working notes separately record a local-LLM override path via
   `AIZU_LLM_BASE_URL`/Ollama, which is not part of the shipped default and not documented in
   this repo's own docs tree — a second, less-vetted dependency surface if it's ever used in
   production.)
2b. **Platform API dependency for the other three.** Reddit/YouTube/Telegram lean on
   official APIs with their own quota/rate-limit ceilings (YouTube Data API v3 daily quota,
   Reddit OAuth2 rate limits) — halts gracefully but a persistent 429/5xx still stops lead
   flow on those channels; not something Aizu controls.
4. **Bus-factor / documentation honesty gaps that matter for diligence, not just polish.**
   The repo's own docs flag that both `engine/README.md` and `admin-panel/README.md` are
   stale/pre-multi-tenancy, and the landing page ships with a "Platform skins" caveat about
   its own shippability and empty Privacy/Terms/Docs pages — a technically-savvy diligence
   pass will notice missing legal pages (no Terms of Service, no Privacy Policy) on a product
   that automates access to third-party platforms and stores personal data (leads' handles,
   messages, contact fields) about people who never opted into being profiled — a real
   compliance question (GDPR/CCPA-style exposure) that isn't addressed anywhere in this repo.
5. **Pricing/marketing inconsistency (minor but a diligence tell).** The billing engine's
   real "Lite" tier ($9.99/mo, 50 leads) is live and checkout-capable but absent from the
   shipped landing page, and the marketing video's "$9/month" narration doesn't match any
   currently-advertised price — small, but the kind of inconsistency a careful investor flags
   when checking whether the pitch deck's numbers were verified against the actual product.
6. **Warming is inherently the riskiest subsystem by design.** It's the one part of the
   system that performs deliberate writes (likes/follows/joins/reactions) to build account
   credibility — i.e., it exists specifically to make automated accounts look human, which is
   the crux of platform ToS risk. It's gated behind `AIZU_WARMING_ENABLED` (default off) and
   has explicit daily caps and probabilistic firing, which shows safety-conscious design, but
   it doesn't remove the underlying exposure — it manages it.
