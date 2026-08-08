# AIZU landing page port — final implementation plan

Replaces `marketing/website/index.html`. Base stance: **Ruthless Edit**, restructured around the
product loop (Proposal 3's ordering), grafted with Proposal 1's bento micro-details, and hardened
against every blocker/major raised in adversarial critique.

---

## 1. Decision summary

- **Keep the existing dark-native, zero-dependency `marketing/website/index.html` as the literal
  build base.** Do not adopt CoreShift's stack (GSAP + ScrollTrigger + Lenis, 133,360 B minified) or
  its multi-file `css/`/`js/`/`vendor/`/`assets/fonts/` tree. The current page already does every
  interaction job CoreShift's stack does (reveal-on-scroll, sticky CTA, hero choreography) in two
  inline `<script>` blocks and ~60 KB total.
- **Port CoreShift's layout ideas, not its code**: a floating pill nav with a drop-in entrance, a
  3-card real-mechanism bento (grafted detail: report-name roller + RBAC avatar ring), and a
  restaged live counter-rotating dot-ring for the existing static "why" diagram. Everything else
  CoreShift offers (testimonials coverflow, integrations logo arc, stock-photo hero graph, portrait
  marquee) is cut outright — no honest Aizu content exists for any of them, and reskinning them
  keeps a shape that still reads as fabricated social proof or forbidden platform-logo integration.
- **Reorder the page around the actual product loop** (brief → run → triage → pricing → FAQ) instead
  of leaving CoreShift's original section order or the current site's order untouched — this is the
  one structural idea from the losing proposals worth adopting wholesale.
- **Keep ~95% of existing copy verbatim.** It is already finished, on-voice, reviewed copy sitting
  behind real guardrails (no fake customers, no platform names, no fabricated metrics, no
  "unlimited"). New copy is written only for: the 3 new bento tiles, the restaged why-diagram
  captions, and small nav/CTA precision edits (e.g. "Start free — no card").
- **Ship as one self-contained HTML file** (current pattern), not a folder tree — this also resolves
  the deploy blocker: the file goes to the same out-of-band manual/CI-less path the current site
  already uses, with zero new asset requests, zero new fonts to self-host, zero vendor licenses to
  track.

---

## 2. Design direction

**Visual thesis.** The page keeps the ink-and-lime discipline the current site already proves works:
ink (`#16161a`) is ~90% of every frame, lime (`#d9f24f`) fires only at the exact moment something is
"the signal" — a qualified lead, a fired CTA, a settled cascade state — and every other differentiator
(section rhythm, card hierarchy, tile grouping) is carried by *lightness steps and hairline borders*,
never by a second hue. Where CoreShift used 4–6 accent colors to keep dashboard tiles/hero
tiles/testimonial ratings visually distinct on white, this page does the identical differentiation
job with one lightness scale (`ink → ink-2 → ink-3`), one border color, and icon-glyph variety —
because on a near-black ground, a second hue reads as noise, not information. Corners tighten from
CoreShift's soft 28/24px "consumer SaaS" radii to the site's existing 16/12px "engineered" radii.
Shadows disappear entirely (a shadow one shade darker than `#16161a` is invisible) and are replaced
by elevation steps plus, on exactly one node per section, a lime glow — the same rule the site's
`.blackbox.fire` already demonstrates.

### Token table

| CoreShift token (css/tokens.css) | Aizu value | Note |
|---|---|---|
| `--white #ffffff` (ground) | `--ink #16161a` | Polarity flip. Ground, not fill-on-ground. |
| `--surface #f0f4f5` (2nd ground, section rhythm) | `--ink-2 #1b1b20` | Aizu has one ground + elevation steps, not two grounds. New-section rhythm = ink/ink-2 lightness step + `border-top:1px solid var(--line)`, exactly how `.nav`/`.hero` already separate (index.html `border-bottom:1px solid var(--line)`). |
| `--tile #eef1f3` (chip/icon-tile fill) | `--ink-3 #202026` | Recessed-chip tone, one step brighter than ground. |
| `--ink` (body text) `#0b0b0c` | `--paper #f2f2f4` | Text-on-ground polarity flip. Never reuse `#0b0b0c` anywhere — it is darker than the site's ground and would be an unauthorized third near-black. |
| `--ink-2` (secondary text) | split: headings/card-titles → `--paper`; body/muted copy → `--grey-1` | Matches the site's existing thin 2-tier hierarchy. |
| `--muted #8e9498` | `--grey-1 #9a9aa2` | Body/secondary copy. 6.45:1 on ink — passes AA comfortably. |
| `--muted-2 #a9afb3` (lighter-than-muted on white) | `--grey-2 #6b6b73` (darker-than-grey-1 on ink) | Direction of the raw value flips; the *intent* ("recede further than body copy") is preserved. **3.42:1 on ink — fails AA for text.** Border/divider/decorative-tick use only, never text (see §5). |
| `--line #dde1e4` | `--line #2a2a30` | Same token name already exists on the live site; value-swap only, zero refactor for any `border:1px solid var(--line)`. |
| `--border-soft #e6e9eb` | `--grey-3 #43434c` | Border-only, never text (contrast is worse than grey-2 on ink). |
| coral / violet / yellow / cyan / red / star (6 accent hues, CTA + icon + rating jobs) | `--lime #d9f24f` for the ONE signal job; everything else desaturates to the ink/grey scale | See collapse rule below. |

**Accent collapse rule** (the single highest-risk item in the whole port): CoreShift spreads 6 hues
across 6 unrelated jobs — primary CTA (coral), secondary CTA (violet), category iconography
(violet/yellow/cyan), decoration, and star ratings. None of these map 1:1 to lime, because lime means
exactly one thing on this brand: *this is the qualified signal, worth acting on.* Collapse rule:

1. **CTA.** Both `.btn--coral` and `.btn--violet` collapse to the site's one existing `.btn-lime`.
   There is no second-tier colored CTA — a second action becomes `.btn-ghost` (line-bordered, no
   fill), exactly as the current hero's two-button pair already does.
2. **Category/decorative icon fills** (violet shield badge, yellow/cyan hero tiles) collapse to a
   flat `--ink-3` chip with a `--grey-1` stroke icon — full desaturation, no exceptions. These were
   never "signal" in CoreShift, they were illustration palette.
3. **Tile-to-tile differentiation**, now monochrome, is carried by: icon glyph (already distinct per
   tile), a heavier `--grey-3` border on exactly one "featured" tile per row instead of a color
   change, and consistent `--ink-3` chip elevation.
4. **The one place a non-lime accent is earned**: the hero's black-box pulse and the restaged
   why-diagram ring may show lime on exactly the node that IS the signal (one qualified dot, one
   settled cascade frame) — echoing the existing grey-dots-vs-lime-dots bento diagram. All sibling
   nodes stay grey.
5. **Star ratings have no destination.** They are deleted with the testimonials section — a numeric
   review score is itself a fabricated-trust-signal even attached to zero names (see §9).

### Typography decision

CoreShift runs a two-family split: Geist 700 (display: h1 `clamp(48px,5.2vw,76px)`, tracking -3.5%,
h2 `clamp(40px,4.6vw,66px)`, card-title 26px) over Outfit 400/600 (lead `clamp(17,1.6vw,20px)`, body
15px) — confirmed at `mockups/coreshift-landing/css/base.css:19-62`. **Verdict: collapse to Aizu's
single family outright, do not blend.** Reasons: Geist's display weight (700) and top-end size (76px)
fight the brand's calmer voice ("never more than two type sizes on screen at once"); a two-family
split has no Aizu analog to port into. Final stack:

```
--sans: "Inter Tight","Inter","SF Pro Display","Segoe UI Variable Display","Segoe UI",system-ui,-apple-system,sans-serif;
--mono: ui-monospace,"JetBrains Mono","SFMono-Regular","Cascadia Mono",Consolas,monospace;
```

(Identical to the current site's already-shipped stack — index.html:31-32 — no change.) h1/h2/h3
stay weight 600 max, tracking -0.025em to -0.033em, line-height 1.12; body stays one size (17px/1.6),
no separate lead/body split. Mono is reserved for the 合図 lockup, stage labels, numeric/data beats
(prices, lead caps, report names) — never body copy. Both faces are already system/web-safe on the
current site (no self-hosted woff2) — **do not adopt CoreShift's self-hosted Geist/Outfit pipeline**;
it would add 564 KB / 22 font files for zero benefit, working directly against the page-weight goal.

### Shape / elevation decision

Two fixed radius steps, matching the current site exactly — no CoreShift squircle percentages:

- `--r-surface: 16px` — outer panels, cards, the theater, pricing cards, bento cards.
- `--r-inner: 12px` — nested elements (input line, sample-card quote block, chip icons).
- Buttons stay pill (current site behavior, unchanged).

CoreShift's `border-radius:26%` squircle icon tiles (bento-pill icon, integrations-tile, hero-tile,
ring-avatar — all sized differently, giving *different* absolute corner softness by design) collapse
to one fixed `12px` value. A flatter, fixed-radius system reads as "precise/engineered" — consistent
with, not a departure from, the brand's calm-declarative-exact-numbers voice.

Elevation replaces CoreShift's shadow system outright: every `box-shadow: 0 Npx Mpx rgba(16,24,40,x)`
(bento-card, nav-pill, footer-panel, testimonials-card) becomes `background: var(--ink-2); border:
1px solid var(--line); /* no box-shadow */` — the exact pattern `.theater`/`.input-card` already use.
The two hue-tinted CoreShift shadows (violet shield glow `rgba(154,103,249,.4)`, coral doctile glow
`rgba(242,112,93,.16)`) are deleted, not recolored — no node in the ported page keeps a tinted glow
unless it is the literal signal node, and if so it becomes exactly `0 0 0 1px var(--lime-08), 0 0
34px var(--lime-14)` (the site's existing `.blackbox.fire` value — reuse verbatim, do not invent a
new glow recipe).

### Motion budget

Reuse the current site's two canonical mechanisms unchanged, and add exactly one new one:

1. **Scroll reveal** — existing `.reveal`/`.reveal.in` IntersectionObserver pattern (index.html
   ~955-966), `cubic-bezier(.22,.61,.36,1)`, fade + `translateY(16px)`, staggered `d1`/`d2`/`d3`
   delay classes. Applies to every section, including the 3 new bento cards.
2. **Tactile press** — existing `.btn:active{transform:translateY(1px) scale(.99)}` (index.html:76).
   Do not import CoreShift's slightly different `translateY(-1px)`/`(0)` hover-press numbers; the
   site's own values are brand-canonical.
3. **New: interaction easing token.** Adopt CoreShift's `--ease-out: cubic-bezier(.16,1,.3,1)`
   (tokens.css:106) as a second, purpose-scoped easing reserved for hover/press micro-transitions
   (nav-link underline, chip hover) — the current site only has one easing, scoped to scroll-reveal;
   this fills a real gap without colliding with it.
4. **New: live why-diagram ring.** The existing static grey-dots-vs-lime-dots SVG (index.html
   625-660-equivalent, `#why` section) gets restaged as a slow, continuously-rotating ring of dots
   (mostly `#43434c`, one firing `#d9f24f` as it crosses 12 o'clock) — CSS `@keyframes` rotation only,
   no JS, no GSAP. **Explicit throttle**, per the brand's own motion rule ("bounce essentially
   banned... signal moves slow and settles, noise moves fast and blurs"): one full rotation ≥ 14s,
   `prefers-reduced-motion` swaps it back to the current static SVG with zero animation — not a
   paused-in-place version, the literal existing markup.
5. **New: hero cascade micro-animation.** The existing black-box pulse (`#blackbox.fire`) gets a
   3-frame internal state instead of one flat "fire": scan (grey dot pulses along wire) → gate (ring
   tightens, still grey) → score (ring settles, turns lime) — reuses the existing `.pulse`/`.wire`/
   `.blackbox` DOM and CSS custom properties, only the keyframe count changes. Cost: ~20 lines of CSS,
   no new JS.
6. **Explicitly NOT ported**: CoreShift's continuous ambient orbit-ring rotation (portrait marquee),
   `setInterval`-driven auto-rotating arc carousel, and 3D coverflow perspective transforms — all are
   either attached to cut sections or are exactly the "continuous ambient/decorative motion" the
   brand's own motion doc flags for restraint.

---

## 3. Page structure

Ordered around the loop: **brief → run → triage → pricing → FAQ → close.** Every "source" cell names
where the content/mechanic comes from; "carried" = existing copy moved with no rewrite.

| # | Section | Source | What it says | Effort |
|---|---|---|---|---|
| 1 | Nav | remap: CoreShift pill mechanic (drop-in entrance, underline-on-hover) + carried current-site copy/links/mark | `AIZU 合図` lockup · links Why AIZU / How it works / Pricing / FAQ · ghost "Log in" (new) + lime "Start free — no card" | S |
| 2 | Hero ("signal theater") | carried, + hero cascade micro-animation (§2 item 5) | H1 "Meet your next customers while they're still deciding." Sub + input→blackbox→3 lead-cards choreography, now literally re-enacting scan→gate→score→lime-check | S (copy) / S (animation add) |
| 3 | Audience strip | carried verbatim | "Built for people who sell direct: ..." | S |
| 4 | Why / "the miss" (restaged) | remap: existing static diagram → live rotating dot-ring (§2 item 4) | "They're already asking." + WITHOUT/WITH-AIZU diagram, now animated | M |
| 5 | Name payoff | carried verbatim | "AIZU 合図. A signal to act." + etymology paragraph | S |
| 6 | Bento — "What you get." | remap: 3-card cap (Ruthless Edit) + grafted detail (report-name roller, RBAC avatar ring, from Proposal 1) | Card 1 (anchor): "5 qualified > 100 junk." + dot diagram (carried). Card 2: "One brief, every check." — brief-driven pipeline + report-name roller. Card 3: "Roles that actually mean something." — owner/admin/member/viewer avatar ring. All inline SVG/CSS, no screenshots. | L |
| 7 | How you use it (3-step) | carried verbatim | Describe / Signal / Customers, closer "You never see the noise." | S |
| 8 | Sample lead | carried verbatim, disclaimer unweakened | "What lands in your account." + anonymized sample card | S |
| 9 | Trust | carried verbatim | "Quiet. Respectful. Yours." | S |
| 10 | Pricing | carried verbatim (4 visible tiers: Free/Starter/Pro/Custom, Lite tier held in reserve per §8 open question) + explicit lime-only-on-recommended-tier rule | "Pay for customers, not software." 5-tier billing.py data | S |
| 11 | FAQ | carried verbatim | 7-question accordion incl. "not a contact database" | S |
| 12 | Final CTA | carried verbatim, CTA line tightened per loop-close graft | "Your next customer may be asking right now." / "AIZU finds the signal. You win the customer." — closing line now points at "your next brief" concretely (copy tweak, see §4) | S |
| 13 | Footer | carried, + explicit resolved decision: zero platform/social icons | Logo lockup, tagline, link columns, no icon row | S |
| — | Sticky CTA bar | carried verbatim (existing 3-IntersectionObserver mechanism) | unchanged | — |
| — | Integrations (CoreShift) | **cut**, no replacement section | n/a — no honest Aizu "integrations" exist; showing the 5 logos or their 6 real source-platform equivalents both violate the platform-silence guardrail | — |
| — | Testimonials (CoreShift) | **cut**, no replacement section | n/a — no real customers exist in the repo; trust load is carried instead by Trust + Sample Lead + FAQ, already on the page | — |
| — | Core HR marquee (CoreShift) | **cut**, superseded by remap of §4 | n/a — portrait roster implies a customer base Aizu doesn't have | — |

---

## 4. Copy

Everything below is either **carried verbatim** from the current `marketing/website/index.html` (cited)
or **new** (flagged). Paste-ready.

### Meta (carried, index.html:6-11 — unchanged)

```html
<title>AIZU: a signal to act</title>
<meta name="description" content="Tell AIZU what you sell and who you serve. It returns a short list of qualified, ready-to-buy customers, every week. A pipeline that fills itself.">
<meta property="og:title" content="AIZU: a signal to act">
<meta property="og:description" content="Tell AIZU what you sell and who you serve. It returns a short list of qualified, ready-to-buy customers, every week.">
<meta property="og:type" content="website">
<meta property="og:url" content="https://aizu.uz/">
<link rel="canonical" href="https://aizu.uz/">
```

*(New: add the `<link rel="canonical">` line — the current site is missing it; see §5 SEO hazard.)*

### Nav (carried links/mark, new CTA precision)

- Logo: `AIZU` + `合図` (ping-mark SVG, pixel-identical to index.html:12/502-506 — do not redraw)
- Links: `Why AIZU` → `#why` · `How it works` → `#how` · `Pricing` → `#pricing` · `FAQ` → `#faq`
- Actions: ghost `Log in` → `https://aizu.uz/app/login` (new — precision addition, current site has
  no login link in nav) + `.btn-lime` **"Start free — no card"** → `https://aizu.uz/app` (copy
  change from carried "Start free" — precision graft from Proposal 3, reflects Free tier's real
  no-checkout default per `engine/aizu/billing.py:76-79`)

### Hero (carried verbatim, index.html:523-528)

- H1: **"Meet your next customers while they're still deciding."**
- Sub: **"Tell AIZU what you sell and who you serve. It returns a short list of ready-to-buy
  customers, every week."**
- CTAs: `Start free` (lime) / `See how it works` (ghost, scrolls to `#how`)
- Theater caption (carried, index.html ~562): "Describe your business once. AIZU delivers the people
  already asking for it."

### Audience strip (carried verbatim)

"Built for people who sell direct: e-commerce and DTC brands, agencies, service providers, coaches
and creators, B2B sellers."

### Why / restaged diagram (carried copy, new only: diagram is now animated, no text change)

- H2: **"They're already asking."**
- Body: "Right now, someone out there is looking for exactly what you sell, and asking who can
  help." / "Gone before you find them. By the time you'd ever find them, they've moved on. And
  you've spent hours you don't have: chasing, sorting, guessing."
- Kicker: "The customer was real. The moment was real. *You just weren't there.*"

### Name payoff (carried verbatim)

- H2: **"AIZU 合図. A signal to act."**
- "AIZU is Japanese for a signal to act. In all the noise online, it surfaces the one that matters:
  the customer worth reaching, right now."
- "Not more noise. Not another tab to check. One clear signal, delivered while it still counts."

### Bento — "What you get." (Card 1 carried; Cards 2–3 are new)

**Section head (carried):** "What you get."

**Card 1 — anchor cell (carried verbatim, index.html ~625-647):**
- H3: "5 qualified > 100 junk."
- Body: "Not more leads. The right ones. A short list of people ready to buy, each one already
  looking for what you offer."
- Diagram: existing grey-dots/lime-dots SVG, unchanged.

**Card 2 — new, "One brief, every check." (replaces CoreShift's radar+roller card):**
- H3: "One brief, every check."
- Body: "Write what you sell and who you serve once. AIZU checks every candidate against it — not
  the other way around."
- Visual: a small mono-labeled roller cycling through 4 real report names, one at a time, on a
  1.6s interval (`prefers-reduced-motion`: static list, no cycling): `CPL trend` / `Channel
  comparison` / `Spend by stage` / `System health`. Grafted from Proposal 1's "report-name roller"
  detail; report names are real, drawn from `admin-panel/src/features/reports`.

**Card 3 — new, "Roles that actually mean something." (replaces CoreShift's employee-panel card):**
- H3: "Roles that actually mean something."
- Body: "Owner, admin, member, viewer. Each one sees exactly what their role allows — enforced on
  the server, not just hidden in the UI."
- Visual: a 4-dot avatar ring (owner/admin/member/viewer initials in ink-3 chips, no photography),
  grafted from Proposal 1's "RBAC avatar ring" detail. Grounded in `engine/aizu/rbac.py:6-24`.

### How you use it (carried verbatim, index.html 685-725)

- H2: "How you use it." Kicker: "Three steps. **The middle one is ours.**"
- Step 1 "Describe": "Tell AIZU what you sell and who you serve. One short description in plain
  words. That's the whole setup."
- Step 2 "Signal": "Out in the open, people are already asking who can help. AIZU surfaces the ones
  that match you while the moment is live."
- Step 3 "Customers": "A short list lands in your account: who they are, what they asked for in
  their own words, and a direct way to respond. Next week, it refills."
- Closer: "You never see the noise. *You only see the signal.*" + `Start free` CTA.

### Sample lead (carried verbatim, index.html 728-745)

- H2: "What lands in your account."
- Intro: "Every lead answers three things: who they are, what they asked for in their own words, and
  when they asked. With a direct way to respond. A representative example:"
- Card: "K., furnishing a first apartment" · tag "ready to buy" · quote: "Looking for a handmade
  ceramic dinnerware set for our new place. Happy to pay more for something that lasts. Who should I
  talk to?" · foot: "asked in public · surfaced 2h later" / "Direct way to respond included"
- Note (**do not shorten or drop** — see §5/§9): "Details anonymized and illustrative. Every
  delivered lead carries these same fields, matched to what you sell."

### Trust (carried verbatim, index.html 748-757)

- H2: "Quiet. Respectful. Yours."
- Quiet: "AIZU works in the background. No dashboards demanding attention, nothing to babysit. It
  shows up when there's something worth acting on."
- Respectful: "Never pushy, never spam. AIZU finds people who are already looking; it doesn't
  manufacture interest that isn't there."
- Yours: "What you tell AIZU about your business, and every lead it delivers, belongs to you. Never
  sold, never shared. Leave anytime and take it with you."

### Pricing (carried verbatim, index.html 760-844 — 4 visible tiers; see §8 for the Lite-tier
question)

- H2: "Pay for customers, not software." Sub: "Every plan delivers the same thing: qualified,
  ready-to-buy customers, every month. The only question is how many you can handle."
- Free — $0/mo, 10 leads/mo, "Start free"
- **Starter** (badge "Most popular") — $24.99/mo · $249/yr, 250 leads/mo, "Choose Starter"
- Pro — $149/mo · $1,490/yr, 2,000 leads/mo, "Choose Pro"
- Custom (Scale) — "Talk to sales" → `mailto:hello@aizu.uz?subject=AIZU%20Custom%20plan`
- Foot: "Leads are qualified customers delivered to your account each month. Upgrade, downgrade, or
  cancel anytime. No contracts, no lock-in."
- **New explicit rule (graft from Proposal 1, now binding, not implied):** lime accent appears ONLY
  on the Starter card's badge/border/CTA fill. Free/Pro/Custom stay `--grey-3` border, `.btn-ghost`.

### FAQ (carried verbatim, index.html 847-894 — all 7 Q&As, including the load-bearing ones)

Headings and full answer text carried unchanged — most important two for guardrail compliance:

- "Is this cold outreach or a bought list?" → "No. AIZU doesn't sell contact databases, and it
  doesn't blast strangers. Every lead is a person who has already signaled, in public and on their
  own, that they're looking for what you sell. You're not interrupting anyone. You're answering."
- "Where do the customers come from?" → "From public intent signals: people already asking, in
  public, for exactly what you offer. ... The finding is our work, not yours."

Footer line (carried): "Can't find your answer? Write to hello@aizu.uz. A person replies."

### Final CTA (carried structure, closing line tightened per loop-close graft)

- H2 (carried): "Your next customer may be asking right now."
- Sigline (carried, lime): "AIZU finds the signal. You win the customer."
- **New micro-edit** (graft from Proposal 3's "point at 'your next brief' concretely"): add one line
  directly above the CTAs, small/grey: **"Write your brief. See what AIZU finds."** — makes the
  closing beat point at the demonstrated loop (brief → run → triage → pricing → FAQ) instead of
  staying purely atmospheric, without touching the locked sigline.
- CTAs (carried): `Start free` / `Talk to sales` → `mailto:hello@aizu.uz?subject=AIZU%20sales`

### Footer (carried, one resolved decision)

- Logo lockup + "A signal to act." tagline (carried)
- Links (carried): Pricing / FAQ / Terms / Privacy / Contact: hello@aizu.uz
- Bottom (carried): "Quiet. Respectful. Yours. Your data is never sold or shared." / "© 2026 AIZU ·
  aizu.uz"
- **Resolved (was an open risk in 3 of 4 proposals): zero platform/social icons.** No X/Instagram/
  TikTok row. Default chosen per the critic's own recommended fix — most consistent with the
  "never name/show a platform" guardrail — rather than punting to a later brand-owner call.

---

## 5. Dark-port hazards

Every light-canvas dependency found in the mockup, and its concrete fix:

1. **Nav pill fill.** CoreShift's pill sits on `--surface #f0f4f5` with a soft drop shadow
   (`nav.css`). Fix: reuse the current site's already-correct pattern — `rgba(22,22,26,.85)` +
   `backdrop-filter: blur(14px)` + `1px solid var(--line)`, no shadow (index.html ~91-95). Do not
   introduce a light or bright pill fill at any point.
2. **Hero connector/graph elements.** Not ported (hero graph is cut per §3) — but if any grey line
   art is reused from CoreShift assets, it must come off `--line #dde1e4` (light-on-white) and land
   on the brand grey scale (`--grey-3 #43434c` / `--grey-2 #6b6b73`), never left near-white.
3. **All box-shadows.** CoreShift shadows are `rgba(16,24,40,x)` — a near-black cast as a shadow,
   invisible on `#16161a`. Fix per §2 elevation decision: replace every shadow with an `ink-2`
   background + `1px solid var(--line)` border. The two hue-tinted shadows (violet shield glow,
   coral doctile glow) are deleted outright, not recolored, per the "no colored drop shadows except
   lime-on-the-signal-node" rule.
4. **`--grey-2`/`--grey-3` as text.** CoreShift's `--muted-2 #a9afb3` was safe as helper/axis-label
   text on white (contrast ok). Its dark-scale analog `--grey-2 #6b6b73` measures **3.42:1 on ink —
   fails WCAG AA for normal text** (needs 4.5:1; only clears AA-large at ≥18.66px bold/24px regular).
   `--grey-3 #43434c` is worse still. **Hard rule: grey-2/grey-3 are border/divider/decorative-tick
   only, never body or label text** — exactly how the current site already uses grey-3 (`.btn-ghost`
   border only). Enforce with a CSS-lint pass, not a code review glance (see §7 Phase 4 checkpoint).
5. **Section-rhythm alternation.** CoreShift alternates `--white`/`--surface` per section for visual
   pause. Aizu has one ground, not two. Fix (already stated in §2): alternate `ink`/`ink-2` by one
   lightness step, marked by a hairline `border-top: 1px solid var(--line)`, matching the existing
   `.nav`/`.hero` separator technique.
6. **Squircle percentage radii.** `border-radius:26%` on differently-sized elements gives CoreShift
   intentionally-varied corner softness. Fix: collapse to one fixed `12px` (`--r-inner`) everywhere,
   per §2 shape decision — no percentage radii anywhere in the shipped page.
7. **`backdrop-filter` photo-blur mask** (core-hr progressive blur). The *mechanism* ports fine to
   dark (nav already proves it), but it exists in CoreShift only to blur photographs that are cut.
   Fix: do not port the technique at all unless a future abstract visual explicitly calls for it —
   nothing in this plan's section 6/9 needs it.
8. **Self-hosted Geist/Outfit woff2 pipeline.** Reuse-worthy as *infrastructure* only, and this plan
   explicitly declines to reuse it — Inter Tight/JetBrains Mono stay on the current system-font stack
   (already proven, zero extra requests). Do not carry the font files or the `@font-face` blocks
   forward under any circumstance.
9. **Continuous ambient motion** (orbit rings, `setInterval` arc auto-rotation) reads as "noise" under
   the brand's own motion doc when left as perpetual/fast loops. The one new animated element this
   plan ships (§2 item 4, why-diagram ring) is throttled to ≥14s/rotation and has a full static
   fallback under `prefers-reduced-motion`, specifically to avoid this failure mode.

---

## 6. File plan

**Single file, in place of the current one.** Justification: the current `marketing/website/index.html`
is 1,150 lines / 60,254 bytes with zero external requests (two inline `<script>` blocks, an inline
SVG favicon). Adopting CoreShift's folder tree (9 CSS files/2,323 lines, 10 JS files/1,698 lines,
3 vendor files/133,360 bytes, 564 KB of fonts, 1.4 MB of unlicensed photos) would turn one
manually-deployed static file into a 45+-request, ~800 KB asset bundle for a page whose actual new
content (3 bento cards + one animated ring) does not need it. This also directly resolves the
critic's **deploy blocker**: `.github/workflows/ci-cd.yml`'s `deploy` job (confirmed at lines 72-147)
rsyncs only `engine/` and `admin-panel/dist/` to `/opt/aizu/` and restarts the `aizu` systemd service
— there is no `marketing/` step anywhere in CI/CD. Publishing to aizu.uz is already an out-of-band
manual process; keeping the page a single file keeps that manual step a one-file copy instead of a
one-directory sync-and-verify. (See §8 for whether that manual/CI gap itself should be fixed.)

**Created / modified / deleted:**

| Action | Path | Notes |
|---|---|---|
| Modify (full rewrite in place) | `marketing/website/index.html` | The deliverable. Single file, inline `<style>` + two inline `<script>` blocks, inline SVG favicon — same shape as today, new sections per §3. |
| No change | `marketing/website/README.md` (if present) or equivalent | Confirm/update any doc pointing at file structure, if one exists. |
| Not touched | `mockups/coreshift-landing/**` | Read-only reference during build. Nothing from it is copied byte-for-byte except: the two CSS values explicitly named in §2 (`--ease-out` interaction easing) and the two grafted bento micro-ideas (report roller, avatar ring), both re-implemented natively in the target file's existing CSS/JS style, not pasted in from the mockup's files. |
| Not created | `marketing/website/css/`, `js/`, `vendor/`, `assets/fonts/`, `assets/people/` | Explicitly declined — see justification above. |
| Not created | Any new CI/CD workflow step | Out of scope for this port; flagged as an open question in §8, not silently solved. |

**Mechanical gate (resolves critic blocker on `assets/people`):** before merge, run
`grep -rniE "assets/people|\.jpg[\"']" marketing/website/index.html` and require zero matches. This
is enforced as a build-phase check (§7 Phase 5), not left to proposal text.

---

## 7. Build phases

Each phase is independently verifiable in a browser against the live file; no phase depends on an
asset that doesn't already exist in the repo.

**Phase 0 — Baseline snapshot.**
Copy current `marketing/website/index.html` to a local diff reference (e.g. `git show HEAD:...` is
sufficient; no new file needed). Checkpoint: `git diff --stat` against this baseline is available at
every later phase to confirm no carried section was silently altered.

**Phase 1 — Token + shell pass.**
Apply the §2 token table to the `:root` block (already present, values unchanged from what's live —
confirm no accidental drift), add the new `--ease-out` interaction-easing token, confirm the
`--r-surface`/`--r-inner` radius pair is unchanged. Checkpoint: page renders identically to current
production (this phase should produce a *no-visual-diff* build — it's a values audit, not a redesign).

**Phase 2 — Restructure section order.**
Move existing sections into the loop order from §3 (positions 1–13) with no content changes yet —
pure reorder + anchor-link updates (`#why`, `#how`, `#pricing`, `#faq` targets must still resolve).
Checkpoint: click every nav link and the sticky-CTA bar's `#pricing` link; each must scroll to the
correct, now-relocated section.

**Phase 3 — Nav + hero micro-edits.**
Add ghost "Log in" link, change CTA copy to "Start free — no card", implement the hero cascade
micro-animation (scan → gate → score → lime-check keyframes on the existing `.blackbox`/`.pulse`
elements). Checkpoint: `prefers-reduced-motion: reduce` in devtools shows the static end-state with
zero animation; motion-on shows the 3-frame cascade completing in under ~2.5s per cycle (matching the
existing loop's pacing).

**Phase 4 — Why-diagram restage + accent-collapse audit.**
Implement the live rotating dot-ring (§2 item 4) with the ≥14s rotation + reduced-motion static
fallback. Then run the explicit accent audit named in §5 item 4: grep the diff for any hex color
outside `{--ink,--ink-2,--ink-3,--line,--grey-1,--grey-2,--grey-3,--paper,--lime,--lime-08,
--lime-14}` and justify or remove every hit. Checkpoint: zero non-token hex colors in the stylesheet;
`--grey-2`/`--grey-3` appear only in `border`/`box-shadow`/decorative-SVG-stroke declarations, never
`color`.

**Phase 5 — New bento cards + mechanical asset gate.**
Build Card 2 (report-name roller) and Card 3 (RBAC avatar ring) as inline SVG/CSS, no screenshots,
capped at 3 cards total (Card 1 unchanged). Run the §6 mechanical gate:
`grep -rniE "assets/people|\.jpg[\"']" marketing/website/index.html` must return zero matches. Also
confirm zero `<script src=` / zero external `<link rel="stylesheet" href=` in the file (single-file
invariant). Checkpoint: both greps pass; bento section visually reads as 3 cards, asymmetric
3fr/2fr/full-width grid preserved from the current site.

**Phase 6 — Pricing/FAQ/footer precision pass.**
Apply the lime-only-on-Starter rule explicitly (verify no lime bleeds onto Free/Pro/Custom cards),
confirm footer has zero platform icons, add the "Write your brief. See what AIZU finds." line above
the final CTA. Checkpoint: visual scan of pricing section shows exactly one lime border/badge/CTA
fill across all 4 cards.

**Phase 7 — Meta/SEO + a11y pass.**
Add `<link rel="canonical" href="https://aizu.uz/">`. Confirm the existing `aria-label`s on
`.theater`, `.miss-diagram`, `.sample-card` tag, and pricing toggle are all still present after the
reorder (nothing in this plan removes any `aria-*` attribute the current site has — the plan adds no
new carousel/coverflow, so the critic's carousel-a11y and split-text-a11y findings don't apply to any
surviving section; confirm this is still true by grepping the final file for `role="region"` /
`aria-roledescription` candidates — none should be needed). Checkpoint: run the page through a
contrast checker on `--grey-2 #6b6b73` / `--grey-3 #43434c` against `--ink #16161a` and confirm
neither is used as a text color anywhere (automatable via a CSS rule scan, not just visual review).

**Phase 8 — Cross-viewport pass.**
Explicitly test at 390px, 720px, 1280px, 1920px (the critic's flagged gap — CoreShift's own README
only verified 1280/1920). Every new element from this plan (bento roller, avatar ring, rotating
dot-ring) must have a real content-present state at 390px, not a hidden/`display:none` fallback with
no replacement. Checkpoint: no horizontal scroll at any width; no section collapses to empty space.

**Phase 9 — Final diff review + publish handoff.**
Full side-by-side diff against the Phase 0 baseline; every changed section must map to an entry in
§3's table (no unplanned edits to already-correct carried sections). Hand off the single updated
`marketing/website/index.html` for whatever manual/out-of-band process currently publishes aizu.uz
(see §8 — this plan does not invent a new deploy path).

---

## 8. Open questions for the user

1. **How does aizu.uz actually get published today?** Confirmed: `.github/workflows/ci-cd.yml`'s
   `deploy` job only touches `engine/` and `admin-panel/dist/` → `/opt/aizu/`; there is no
   `marketing/` step. If the real hosting path is fragile/manual, that should be documented (or
   automated with a health-check step mirroring the panel deploy) independent of this content port —
   flagging, not deciding, since it's an infra decision outside this plan's scope.
2. **Lite tier ($9.99/mo·$99/yr, 50 leads) is in `engine/aizu/billing.py:81-83` but not shown as a
   card on the current live pricing section** (which shows Free/Starter/Pro/Custom only — Lite is a
   real self-serve tier per `SELF_SERVE_TIERS` but absent from the page). This plan carries the
   current 4-card display forward unchanged as a deliberate "carry existing reviewed copy verbatim"
   choice — but flag to the user whether Lite's omission from the page was intentional (e.g. an
   upsell-ladder decision) or a stale gap that should be fixed while this file is being touched
   anyway.
3. **`/app/login` URL** — this plan adds a nav "Log in" link pointing at `https://aizu.uz/app/login`
   by inference from `https://aizu.uz/app` (the existing CTA target); confirm this is the actual
   login route before shipping, or supply the correct one.
4. **Footer social icons** — resolved to zero in this plan (§4/§9) as the safest default under the
   platform-silence guardrail. If the brand owner wants a single non-platform "follow us" channel
   (e.g. a company blog or a newsletter link) instead of zero, that's a one-line footer addition, not
   a plan change — just confirm the destination URL.
5. **GSAP/ScrollTrigger/Lenis and the mockup's `assets/fonts`/`assets/people` folders** — this plan's
   recommendation is to leave `mockups/coreshift-landing/` entirely untouched as a reference and never
   copy any file out of it. Confirm whether the mockup directory itself should be deleted from the
   repo after this port ships (it's flagged `mockups/` = "not part of the build" per `CLAUDE.md`), or
   kept as a permanent design reference.

---

## 9. Accepted risks

- **The page will look plainer than CoreShift's own showreel** — no coverflow carousel, no rotating
  logo arc, no parallax node-graph, no continuous orbit marquee. This is accepted deliberately: every
  one of those mechanics either has no honest Aizu content (testimonials, integrations logos) or
  reads as fabricated proof even after de-fabrication (a coverflow of "safety mechanism cards" still
  *looks* like a testimonial carousel to a skeptical viewer, per the judge's own scoring of the
  rejected Max-Fidelity proposal). Restraint is treated as a feature of a truth-constrained B2B
  landing page, not a shortfall.
- **No real admin-panel screenshots ship in the bento section.** Cards 2 and 3 are inline SVG/CSS
  diagrams referencing real product concepts (report names, RBAC roles), not literal screenshots.
  Sourcing real screenshots (with mandatory per-component cropping to avoid leaking platform-name UI
  chips, per the critic's finding) is explicitly out of scope for this pass — capped at 3 cards,
  illustrated, not photographed, to avoid the "quietly becomes new illustrated fakes" failure mode
  the critique flagged for heavier bento remaps.
- **GSAP/Lenis/ScrollTrigger licensing question goes away by non-adoption**, not by resolution — this
  plan does not vendor those files at all, so the unresolved "no LICENSE file alongside the vendored
  minified JS" critique finding is moot for this deliverable. Accepted as the correct outcome, not a
  gap.
- **The deploy-path blocker (§8 item 1) is named, not fixed.** This plan changes page content only;
  it does not add a CI/CD job to publish `marketing/website/index.html` to aizu.uz. Whatever
  out-of-band process currently ships the file continues to be the mechanism after this port. Fixing
  that pipeline is accepted as separate infra work.
- **Mobile design below 720px is scoped as a build-phase checkpoint (§7 Phase 8), not exhaustively
  pre-specified pixel-by-pixel in this document.** Accepted because the three new/changed visual
  elements (bento roller, avatar ring, rotating why-ring) are simple enough (CSS + small inline SVG,
  no complex parallax math) that phase-time verification at 390px is sufficient; this is a materially
  smaller mobile-risk surface than any proposal that ported CoreShift's orbit/coverflow/arc mechanics
  wholesale.
- **The sample-lead "proof" card remains the single highest scrutiny point on the page** even carried
  verbatim: a spotlighted quote card with attribution styling is structurally adjacent to a
  testimonial. Accepted risk, mitigated by keeping the "Details anonymized and illustrative" disclaimer
  unweakened and unshortened (explicitly called out in §4) — this is a materially different claim
  ("this is what a delivered lead looks like") from a testimonial ("this is what a customer said about
  us"), and the existing copy already draws that line correctly; this plan does not touch it further.
