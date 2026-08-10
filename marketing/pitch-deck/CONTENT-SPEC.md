# AIZU — Pitch Deck Content Spec

## Front matter

**Audience.** Primary: pre-seed/seed investors evaluating a B2B SaaS lead-discovery
product, reading cold (DocSend-style, unnarrated) or in a first meeting. Secondary,
same deck: a partner/agency conversation (reseller or agency-of-record evaluating AIZU
as a service to run for their own clients) — nothing in this spec is investor-only
language; "raise" and "ask" slides are the only ones that don't double for that
audience, and a partner reader can simply stop before slide 15.

**Running order rationale.**
1. Purpose, Problem, Solution, Why Now, and Market Size are written as one
   interwoven opening arc (per DocSend's own finding), even though Market Size
   physically lands later — Why Now and Market Size logic is already implicit in
   how Problem and Solution are framed.
2. Product comes right after the hook, before Traction, because "what does it
   concretely do" is the highest-attention section in DocSend's data (59 sec) and
   must be answered before numbers are trusted.
3. Traction precedes Market Size and Business Model so real, modest evidence
   grounds the size claim that follows it, not the other way around.
4. Risks sits right after Competition, not buried in an appendix — naming the real
   ones (ToS exposure, single-run lock, LLM dependency, no legal pages yet) reads as
   founder self-awareness, which the research flags as more credible than a deck
   that avoids all tension.
5. Team, Financials, and the Ask close the deck in that order because Business
   Model (64 sec) and Product (59 sec) earn more investor attention than Team
   (4th–5th, ~38 sec) — Team is important context for the Ask, not the peak of the
   argument.

**MOCK REGISTER** — every invented figure, exactly once. All three decks and every
slide below copy these values verbatim; degree mark (°) travels with the number
wherever it's reused. Anything not in this table (six platforms, pricing tiers,
feature set, architecture) is real, sourced from the repo, and carries no mark.

| # | Figure | Value |
|---|---|---|
| 1 | Raise amount & instrument | $750,000° SAFE |
| 2 | Valuation cap | $6,000,000° post-money cap |
| 3 | Runway on this raise | 18 months° |
| 4 | Use of funds split | Product & Engineering 45°%, Sales & GTM 35°%, Compliance/Ops/Legal 20°% |
| 5 | Current MRR | $4,100°/mo |
| 6 | Current ARR run-rate | $49,200° (MRR × 12) |
| 7 | Paying customers (today) | 38° |
| 8 | Free-tier + waitlist signups | 1,150° |
| 9 | Leads delivered to date (cumulative) | 62,000° |
| 10 | Average lead-to-reply rate | 24°% |
| 11 | CAC (blended) | $85° |
| 12 | Gross margin | 78°% |
| 13 | Logo retention (trailing 3 months) | 91°% |
| 14 | Manual-search time lost, pre-AIZU baseline | 5+ hours/week° |
| 15 | Average time-to-first-lead | 4 days° |
| 16 | GTM channel mix | Self-serve 60°%, Agency/partner referral 25°%, Founder-led outbound 15°% |
| 17 | TAM (arithmetic) | 120,000,000° addressable social-selling SMBs/creators worldwide × $300°/yr Starter-equivalent ARPU = **$36B°** |
| 18 | SAM (arithmetic) | 8,000,000° businesses reachable today via the six shipped platforms, English/Russian-language markets × $300°/yr = **$2.4B°** |
| 19 | SOM (arithmetic, Year-3 target) | 40,000° customers × $300°/yr = **$12M° ARR** |
| 20 | 3-year plan | Y1 (now): $49,200° ARR / 38° customers → Y2: $1.8M° ARR / 1,500° customers → Y3: $12M° ARR / 40,000° customers |
| 21 | Founding | 2025°, Tashkent, Uzbekistan° |
| 22 | Team headcount | 9° full-time |
| 23 | Team — CEO/co-founder | Aziz Karimov° — ex-growth lead, a Tashkent e-commerce marketplace |
| 24 | Team — CTO/co-founder | Dilnoza Yusupova° — built the six-platform engine architecture and the RBAC model |
| 25 | Team — Head of Product & GTM | Sardor Nazarov° — ex-agency operator, ran paid social for CIS DTC brands |

Internal consistency check: $4,100 MRR ÷ 38 customers ≈ $108/mo blended ARPU today
(early, Pro-weighted mix); by Year 3 blended ARPU drifts down to the Starter-priced
$300°/yr ($25°/mo) as the customer base broadens down-market — TAM/SAM/SOM math uses
that same $300°/yr figure throughout, and Year-3 SOM (40,000° customers) equals the
Year-3 row of the 3-year plan exactly.

---

## 01 — Cover

- **Answers:** What is this company, in one line?
- **Headline:** AIZU — six noisy platforms, one lead list.
- **Deck (sub):** From a single plain-language brief, AIZU finds the people already asking for what you sell.
- **Body:**
  - AIZU 合図 — a signal to act
  - Instagram · LinkedIn · X · YouTube · Reddit · Telegram
  - Pre-seed · [confidential]
- **Visual:** Centered typographic title card — "AIZU" wordmark large, small secondary "合図" beside it, one-line description beneath, no chart. A quiet footer strip of 6 small platform chips (Instagram/LinkedIn/X/YouTube/Reddit/Telegram) is the only other element.
- **Notes:** We're not another platform to check — we're the signal that tells you when to act. Six platforms, one brief, one list of people already asking for what you sell. Everything after this slide just proves that sentence.
- **Density:** sparse

## 02 — Company Purpose

- **Answers:** What do you ultimately do, and why does it matter?
- **Headline:** Meet customers while they decide.
- **Deck (sub):** Tell AIZU what you sell and who you serve; it returns qualified, ready-to-buy customers every week.
- **Body:**
  - You write one brief, once
  - AIZU reads six platforms against it, continuously
  - You get a ranked list of people who already asked for what you sell
- **Visual:** Single full-width 3-node sentence diagram: brief icon → arrow → "AIZU" wordmark → arrow → leads-list icon. Node labels: "You write one brief" / "AIZU" / "You get ready-to-buy customers."
- **Notes:** Every business already has people asking for what they sell, in public, right now — on Instagram, on Reddit, in a YouTube comment. Nobody has time to watch six feeds for that. AIZU watches them and hands you the ones worth answering.
- **Density:** sparse

## 03 — Problem

- **Answers:** What urgent, expensive pain does the target customer have today?
- **Headline:** Buying intent is public. Nobody's watching.
- **Deck (sub):** Someone is asking for what you sell right now, on a platform you don't have time to monitor.
- **Body:**
  - Today: 6 separate apps, manually scrolled, no filter for buying intent
  - Today: 5+ hours/week° lost to search that turns up nothing systematic
  - Today: you only catch what you happened to be watching when it posted
  - With AIZU: one dashboard, an automated two-stage AI read of every post and comment, every qualifying match across all six platforms
- **Visual:** Two-column before/after comparison table, 4 rows. Row labels: "Where the signal lives" / "How you'd find it" / "Time it costs" / "What you catch." "Today" column: "6 separate apps" / "Manual scrolling, no intent filter" / "5+ hours/week°" / "Whatever you happened to see." "With AIZU" column: "One dashboard" / "Automated AI read of every post & comment" / "Minutes/week reviewing a ranked list" / "Every qualifying match, all 6 platforms."
- **Notes:** Before AIZU, our own team was doing this by hand — six tabs open, refreshing, hoping we didn't miss the comment that mattered. That's not a workflow, it's a part-time job nobody's paid to do. The pain isn't that the customers aren't out there; it's that finding them doesn't scale past one person's attention.
- **Density:** medium

## 04 — Solution

- **Answers:** How do you solve that pain in a fundamentally better way?
- **Headline:** One brief. Six platforms. One list.
- **Deck (sub):** AIZU attaches to a session you already opened, reads what's already public, and ranks who's ready to buy.
- **Body:**
  1. Write one brief describing what you sell and who you serve
  2. AIZU attaches to a warmed, already-logged-in session or an official API — never launches its own browser
  3. Two-stage AI check per post: is this on-topic, then who's commenting like a buyer, not a seller
  4. Matches become leads: handle, verbatim ask, timestamp, your requested fields
  5. All six platforms land in one Leads dashboard — New / Qualified / Contacted
- **Visual:** 5-step horizontal process diagram, numbered 1–5 left to right, one icon and one short label per step, matching the Body list exactly.
- **Notes:** The unlock isn't "scrape more" — it's "attach, don't own." AIZU only ever reads a session you already logged into; it never launches a browser or crafts an unofficial API call itself. That's what makes it safe to run continuously instead of in short, risky bursts.
- **Density:** medium

## 05 — Why Now

- **Answers:** Why is this the moment this becomes possible or inevitable?
- **Headline:** The signal moved. The tools caught up.
- **Deck (sub):** Buying intent now surfaces in public comments, and AI can finally read all of them, at human pace.
- **Body:**
  - Buying intent moved into public comments, not just search
  - Per-comment AI reading became cheap and fast enough to run continuously
  - Warmed-session automation makes reading that traffic safe, at daytime, human pace
- **Visual:** Three dated milestones set on a single horizontal rule spanning the full slide width — no icons, no chart, pure typography, matching the Swiss-register signature slide.
- **Notes:** Five years ago this would have meant either hiring someone to scroll all day, or building something that looks like a bot and gets banned. Neither was viable. What changed is that AI got cheap enough to read every comment, and warmed-session automation got disciplined enough to do it at the pace a real person would.
- **Density:** sparse

## 06 — Product

- **Answers:** What does the product actually do, concretely?
- **Headline:** Six feeds in. One ranked list out.
- **Deck (sub):** Every candidate is checked twice — is the post on-topic, then does this commenter sound like a buyer.
- **Body:**
  - Instagram, LinkedIn, X — warmed browser session, vision-assisted reading
  - Reddit, YouTube, Telegram — official platform APIs
  - Stage 1: relevance gate — is this post on-topic for your brief
  - Stage 2: match score — does this commenter sound like a buyer, not the seller
  - Output: handle, verbatim ask, timestamp, your requested fields
  - Runs pace themselves to daytime hours with human-like dwell time
- **Visual:** 6 platform chips (Instagram, LinkedIn, X, YouTube, Reddit, Telegram) with converging lines funneling into a single two-stage AI gate box labeled "Relevance gate → Match score," which funnels into one ranked leads table with 4 columns: Handle, Verbatim ask, Score, Stage (New/Qualified/Contacted).
- **Notes:** This is the whole engine in one picture. Six platforms, two AI checks per candidate, one list at the end — nothing about it is a black box you have to trust; every lead on that list carries the exact comment that qualified them.
- **Density:** dense

## 07 — Traction

- **Answers:** What evidence shows this is already working?
- **Headline:** 62,000 leads delivered, and growing monthly.
- **Deck (sub):** One metric, tracked consistently, six months running — not a cherry-picked best month.
- **Body:**
  - 62,000° leads delivered to date, across 6 platforms
  - 38° paying customers
  - 24°% average lead-to-reply rate
  - 91°% logo retention, trailing 3 months
- **Visual:** Line chart, x-axis = Mar–Aug 2026° (6 months), y-axis = leads delivered per month, single accent line with flat fill beneath, current cumulative total (62,000°) annotated directly at the last point — no legend, no gridlines. Monthly values: Mar 3,200° / Apr 5,100° / May 7,400° / Jun 10,800° / Jul 14,600° / Aug 20,900°. Three supporting stat chips beside the chart: 38° customers, 24°% reply rate, 91°% retention.
- **Notes:** This is one metric, the same metric, every month — leads actually delivered, not signups or page views. It's growing because the pipeline compounds: more warmed accounts, more platforms tuned, more briefs running in parallel. We're not claiming more than this yet, on purpose.
- **Density:** dense

## 08 — Market Size

- **Answers:** How big can this business become (TAM/SAM/SOM)?
- **Headline:** A $2.4B market we can reach today.
- **Deck (sub):** Bottom-up, not a market-report citation: customer count times a Starter-equivalent price, at three scopes.
- **Body:**
  - TAM: 120,000,000° addressable social-selling SMBs & creators worldwide × $300°/yr = $36B°
  - SAM: 8,000,000° businesses reachable today via our six shipped platforms, English/Russian-language markets × $300°/yr = $2.4B°
  - SOM: 40,000° customers by Year 3 × $300°/yr = $12M° ARR
- **Visual:** 3 nested circles (TAM outer, SAM middle, SOM inner), each labeled with its arithmetic written beside it — not just the dollar figure. TAM: "120,000,000° × $300°/yr = $36B°." SAM: "8,000,000° × $300°/yr = $2.4B°." SOM: "40,000° × $300°/yr = $12M° ARR."
- **Notes:** We priced every circle off our own Starter plan, not an analyst report — $25 a month is what a real customer already pays us today. SAM is deliberately narrow: only markets where our six shipped platforms and our language coverage actually work right now, not a global aspiration.
- **Density:** dense

## 09 — Business Model

- **Answers:** How, specifically, do you make money?
- **Headline:** You pay for customers, not software.
- **Deck (sub):** Four tiers, same promise at every level — qualified, ready-to-buy customers delivered to your account.
- **Body:**
  - Free — $0, 10 leads/mo, no card needed
  - Starter ("Most popular") — $24.99/mo or $249/yr, 250 leads/mo
  - Pro — $149/mo or $1,490/yr, 2,000 leads/mo
  - Scale — custom, negotiated cap
  - Upgrade, downgrade, or cancel anytime — no contracts, no lock-in
- **Visual:** 4-column price table (real, unmarked): Free / Starter / Pro / Scale, each column showing monthly price, annual price where applicable, and leads/mo. "Starter" column tagged "Most popular." Footer row spans all four columns: "Upgrade, downgrade, or cancel anytime. No contracts."
- **Notes:** Pricing scales with the thing customers actually value — leads delivered — not seats or API calls. Starter is our center of gravity today; Pro and Scale are where agencies and higher-volume sellers land once the free-to-paid motion proves out.
- **Density:** dense

## 10 — Go-to-Market

- **Answers:** How will you acquire and scale customers repeatably?
- **Headline:** Self-serve first, agencies compound it.
- **Deck (sub):** Free tier drives trial, agencies bring multi-client volume, founder-led outbound fills the gap.
- **Body:**
  - Self-serve trial-to-paid — 60°% of new customers
  - Agency & partner referral — 25°% of new customers
  - Founder-led outbound — 15°% of new customers
  - Blended CAC: $85°
  - 1,150° free-tier + waitlist signups in the funnel today
- **Visual:** Horizontal funnel/bar chart with 3 segments sized to their share of new customers — Self-serve 60°%, Agency & partner referral 25°%, Founder-led outbound 15°%. CAC ($85°) annotated once, beside the whole bar, as the blended figure across all three channels.
- **Notes:** The free tier does the qualifying for us — anyone who upgrades has already seen real leads land in their dashboard. Agencies are the channel we're leaning into next: one agency relationship brings a dozen client campaigns, not one.
- **Density:** medium

## 11 — Competition

- **Answers:** Why do you win against alternatives and the status quo?
- **Headline:** We answer people. Others interrupt strangers.
- **Deck (sub):** One honest axis: does the approach interrupt a stranger, or answer someone who already asked.
- **Body:**
  - Bought contact lists / cold email tools — interrupts strangers who never signaled interest
  - Generic AI SDR / outbound agents — automates the interruption, doesn't remove it
  - Social-listening & brand-monitoring dashboards — tracks mentions, doesn't score buying intent
  - Manual social selling — a person scrolling by hand, doesn't scale past one attention span
  - AIZU — reads public comments where intent was already stated, answers instead of interrupts
- **Visual:** Single-axis positioning line spanning the full slide width, labeled "Interrupts strangers" (left) to "Answers people who already asked" (right). 5 points plotted left to right: bought lists/cold email, generic AI SDR agents, social-listening dashboards, manual social selling, AIZU (rightmost).
- **Notes:** We didn't build a 2x2 because most of them are spin — you can always pick two axes that flatter your own dot. This is one honest claim: every lead we surface already said something in public. Nobody has to guess whether we're interrupting them.
- **Density:** medium

## 12 — Risks

- **Answers:** What could go wrong, and what are you doing about it?
- **Headline:** Here's what we're watching closely.
- **Deck (sub):** Four real risks, named directly, with what we do about each one today.
- **Body:**
  - Platform ToS / automation exposure — read-only by default; never auto-solves a checkpoint, halts for a human instead
  - Single-run lock by default — one run per box today; a distributed worker fleet exists as an opt-in path to relax this
  - LLM & platform-API dependency — per-campaign spend caps degrade softly to "unknown" instead of overspending or failing outright
  - No Terms/Privacy pages yet — a real compliance gap on a product that stores handles and messages; named here, not hidden
- **Visual:** Plain 4-row, 2-column text table — no chart, no icons — columns "Risk" and "What we do about it today," matching the deck's honesty register exactly.
- **Notes:** We'd rather you hear these from us than find them yourselves in diligence. None of them are fatal — they're the normal cost of automating access to platforms that don't publish an API for this — but we're not going to pretend they're solved when they're managed.
- **Density:** medium

## 13 — Team

- **Answers:** Why is this team the one to solve it?
- **Headline:** Built by people who ran this by hand.
- **Deck (sub):** Three founders — engine, growth, and agency operations — who lived the problem before automating it.
- **Body:**
  - Aziz Karimov° — CEO/co-founder — ex-growth lead, a Tashkent e-commerce marketplace
  - Dilnoza Yusupova° — CTO/co-founder — built the six-platform engine architecture and the RBAC model
  - Sardor Nazarov° — Head of Product & GTM — ex-agency operator, ran paid social for CIS DTC brands
  - 9° full-time, founded 2025°, Tashkent, Uzbekistan°
- **Visual:** 3-card row, one card per founder — name, role, one-line credential, initials-avatar placeholder (no stock photography). Footer stat line beneath the row: headcount, founding year, location.
- **Notes:** All three of us either ran growth for a business that needed this or built the systems it required — nobody on this founding team is guessing at the problem. That's also why the product is CIS-first: we built it to solve our own team's problem before selling it to anyone else's.
- **Density:** medium

## 14 — Financials

- **Answers:** Where do revenue, burn, and growth go over 2–3 years?
- **Headline:** From $49K to $12M ARR in 3 years.
- **Deck (sub):** ARPU drifts down as the base broadens — from today's early mix toward the Starter price point.
- **Body:**
  - Year 1 (now): $49,200° ARR, 38° customers
  - Year 2: $1.8M° ARR, 1,500° customers
  - Year 3: $12M° ARR, 40,000° customers
  - CAC $85° · gross margin 78°% · 18 months° runway on this raise
- **Visual:** 3-bar chart, Year 1 / Year 2 / Year 3, y-axis ARR on a log scale (noted directly on the chart, since a linear axis would flatten Year 1 to invisible), each bar annotated with its exact value: $49,200° / $1.8M° / $12M°. A row of 3 stat chips beneath the chart: CAC $85°, gross margin 78°%, runway 18 months°.
- **Notes:** We used a log axis on purpose and said so on the slide — a linear chart would make Year 1 disappear and Year 3 look fake. The growth curve matches Year-3 SOM exactly: 40,000 customers at the Starter price is the same number on the market slide and this one.
- **Density:** dense

## 15 — The Ask

- **Answers:** How much are you raising, and what will it buy?
- **Headline:** $750,000 to reach $1.8M ARR.
- **Deck (sub):** 18 months of runway, split across product, go-to-market, and closing the compliance gap.
- **Body:**
  - $750,000° SAFE, $6,000,000° valuation cap
  - 18 months° runway
  - Product & Engineering 45°% — deepen the two-stage AI, ship the platforms already on the roadmap
  - Sales & GTM 35°% — agency channel, founder-led outbound
  - Compliance/Ops/Legal 20°% — Terms/Privacy pages, ToS-risk review
  - ° illustrative placeholder — figures marked ° are modeled, not audited
- **Visual:** One enormous centered number, $750,000°, with "SAFE, $6,000,000° cap" set directly beneath in a smaller size. A 3-segment horizontal use-of-funds bar beneath that, sized to 45°/35°/20°%. Nothing else on the slide.
- **Notes:** This is the one slide where we want precision, not a story — $750K gets us to $1.8M ARR with 18 months of room, and a fifth of it is earmarked specifically to close the legal gap we just told you about on the risks slide, not to hide it.
- **Density:** sparse

## 16 — Appendix A: Architecture & Platform Detail

- **Answers:** What backup technical detail supports diligence, off the live read?
- **Headline:** How each of the six platforms actually works.
- **Deck (sub):** Access method and mode per platform, plus what's roadmap-only and not yet claimed as shipped.
- **Body:**
  - Instagram — warmed-session CDP + vision — opt-in engagement (like/follow/save/share)
  - X — warmed-session CDP + vision — read-only
  - LinkedIn — warmed-session CDP + vision — read-only
  - Reddit — official Data API (OAuth2) — read-only
  - YouTube — official Data API v3 — read-only
  - Telegram — MTProto / Bot API — read-only
  - Roadmap only, no engine built yet: Facebook, Pinterest, Quora, Threads, TikTok
  - Server-enforced RBAC (owner/admin/member/viewer), local-first single shared SQLite DB, per-campaign spend caps on every LLM call
- **Visual:** 6-row table, columns "Platform" / "Access method" / "Mode," one row per shipped engine exactly as listed in Body. A footer note beneath the table: "Facebook, Pinterest, Quora, Threads, TikTok are roadmap only — no engine exists yet."
- **Notes:** This is the slide for the technical partner in the room, not the pitch itself — every row here maps to a real engine in the codebase, and we've drawn the roadmap line deliberately so nobody mistakes a planned platform for a shipped one.
- **Density:** dense

## 17 — Appendix B: Contact & Mock Register Recap

- **Answers:** How do we follow up, and which numbers in this deck were modeled versus real?
- **Headline:** Get in touch. Here's what's modeled.
- **Deck (sub):** Every figure marked ° in this deck is illustrative, not audited — listed here in one place for reference.
- **Body:**
  - Contact: [founder name] · [email placeholder] · AIZU 合図
  - ° illustrative placeholder — every marked figure in this deck, in one place: raise $750,000°, cap $6,000,000°, runway 18 months°, ARR $49,200°→$1.8M°→$12M°, customers 38°→1,500°→40,000°, CAC $85°, gross margin 78°%, logo retention 91°%, leads delivered 62,000°, TAM $36B°/SAM $2.4B°/SOM $12M°, team of 9°
  - Real, unmarked, sourced from the shipped product: six platforms, four pricing tiers, RBAC model, spend caps, warmed-session architecture
- **Visual:** Simple contact block — name, email placeholder, company wordmark — beside a compact recap strip listing every marked figure used across the deck in one line, so a reader can audit them without hunting back through slides.
- **Notes:** We'd rather over-disclose than have someone find a modeled number and wonder what else was soft. Everything with a ° is our best model of where this goes, not a claim about where it already is — happy to walk through the assumptions behind any single one of them.
- **Density:** sparse
