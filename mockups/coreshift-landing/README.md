# AIZU landing page (mockup, CoreShift structure)

> **This directory is design reference only.** The live, runtime copy of this landing page
> now ships from `admin-panel/public/` (served by the bridge at `https://aizu.uz/`) — see
> `admin-panel/public/index.html` and `admin-panel/public/landing/{css,js,vendor,fonts,photos}/`.
> Edit the shipped copy there for anything user-facing. What stays behind here is design
> source material only: [`SPEC.md`](./SPEC.md) (the motion spec), `sections/` (the nine
> section markups, kept standalone for reference/re-editing), and `ref/` (the original
> capture video and extracted stills this prototype was reverse-engineered from). None of
> those three are loaded at runtime by anything.

A pixel-oriented, fully-animated landing-page prototype — plain HTML/CSS/JS, no build step,
no framework, no backend. The layout and motion were reverse-engineered from a 19.47s
auto-scroll capture video (`ref/source.mp4`) plus extracted still frames (`ref/*.jpg`) of a
Dribbble/reference concept for a fictional HR SaaS product called "CoreShift". The full
scene-by-scene motion spec lives in [`SPEC.md`](./SPEC.md).

**It has since been rebranded to AIZU** — see [Rebrand](#rebrand-coreshift--aizu) below.
The CoreShift *structure, layout, radii and every animation* are unchanged; the palette,
typography, marks and copy are now AIZU's. Two sections with no CoreShift analog — Plans and
FAQ — were added afterward; see the section table below and the SPEC.md addendum.

This directory lives under `mockups/` in the Aizu repo and is **not** part of the Aizu
product build — see the note at the top of this file for where the live copy now lives.
`marketing/website/index.html` is a separate landing-page project under `marketing/`;
this move does not touch it.

## Rebrand: CoreShift → AIZU

| Layer | Before | After |
|---|---|---|
| Ground | white `#ffffff` / `#f0f4f5` | ink `#16161a`, with a raised panel/card ladder |
| Accents | coral, violet, yellow, cyan, red, star-gold | lime `#d9f24f` **only**, as signal; everything else on an ink/grey scale |
| Type | Geist (display) + Outfit (sans) | Inter Tight throughout, JetBrains Mono for labels and data beats |
| Logo | the CoreShift "C" ring glyph | the AIZU ping mark (dot + arc) — see [The mark](#the-mark-one-definition-only) |
| Copy | HR SaaS (attendance, onboarding, payroll) | AIZU lead discovery |

Notes on how the port was done, since they matter if you edit this:

- `css/tokens.css` holds the real semantic tokens (`--ground`, `--paper`, `--lime`, …). The
  old CoreShift names (`--white`, `--ink`, `--coral`, …) are kept beneath them as **aliases**
  so the nine section stylesheets needed no rewrite. This means `--white` is deliberately a
  dark value and `--ink` a light one — the alias preserves each token's *role*, not its
  brightness. Prefer the semantic names in new code.
- Panels/cards are **lifted above** the page ground rather than flattened onto it. On white,
  cards separated from the page by shadow; on ink a shadow is invisible, so separation comes
  from a lightness step: page < band < panel < chip.
- Drop shadows were re-cast from `rgba(16,24,40,…)` (invisible on ink) to real black at
  higher alpha. Colour-tinted glows were removed rather than recoloured, except where the
  element genuinely is the signal, which gets a lime glow.
- The integrations arc no longer shows third-party product logos (AIZU has no such
  integrations, and the brand does not name or depict source platforms). Each tile now
  carries its pipeline-checkpoint number in mono. The arc geometry in `js/integrations.js`
  only depends on tile *count*, so the motion is untouched.
- The testimonial coverflow no longer contains people. Fabricated customers, roles, companies
  and star ratings were removed entirely — AIZU has no real customers to quote. The cards now
  describe named safety mechanisms ("Attach, never launch", "Daytime pacing", "Spend caps"),
  and the avatar slot carries the ping mark, which also answers "whose photo is this?" with
  "nobody's". The 3D coverflow motion is unchanged.
- All 24 placeholder portrait photos are gone from the page — see
  [De-personalization](#de-personalization-what-replaced-the-portraits) below for what
  replaced them slot by slot.
- Two sections were added that have no CoreShift source scene: **Plans**
  ("Pay for customers, not software") and **FAQ**. They're built on the same tokens and
  shape system (`--radius-card`, `--radius-panel`, pill buttons, blur-reveal headings) so
  they read as native to the page rather than bolted on. Motion for both is documented in
  `SPEC.md`'s addendum.

### The mark: one definition only

The AIZU mark is defined **once**, as an SVG `<symbol id="aizu-mark">` in the sprite at the top
of `index.html`, and used everywhere else through `<use href="#aizu-mark">` — nav, both hero
signal chips, three bento lead rows, three testimonial avatars. **Do not draw it inline again.**

Its geometry is copied verbatim from the application's own component,
[`admin-panel/src/shared/ui/BrandMark.tsx`](../../admin-panel/src/shared/ui/BrandMark.tsx),
which is the single source of truth for the logo:

```
viewBox 0 0 100 100
circle cx=40 cy=60 r=16                      the lead
path   M47 31 A31 31 0 0 1 71 72             the cue arc breaking off it
       stroke-width 8.5, round cap
```

Colour arrives through two inherited custom properties, `--aizu-dot` and `--aizu-arc`, rather
than being baked into the symbol. That is not a style preference: CSS custom properties inherit
into a `<use>` shadow tree, while ordinary selectors cannot reach inside one, so per-element
rules like the old `.nav-logo__dot` would silently stop working. Defaults are BrandMark's
`rail` tone — lime dot, white arc — the pairing the panel itself uses on its dark nav rail.

Before this, the mark was hand-drawn inline **nine times in two different geometries** (a
32-unit box in the nav and hero, a 24-unit box in the bento and testimonials), and neither
matched the panel: both used a proportionally smaller dot and a lime rather than white arc. The
favicon now carries the panel's own `favicon.svg` geometry, which is a slightly tighter inset
because it sits inside a rounded square.

**Still divergent, outside this mockup:** `marketing/website/index.html` — the real landing page
— draws the old 32-unit mark inline four more times, plus a fifth larger variant. Aligning it is
a separate change to a shipping file and has not been made here.

### De-personalization: what replaced the portraits

The reference design used real portrait photography in four slots. AIZU has no customers to
photograph and does not depict people it has no rights to, so each slot was redesigned around
the brand mark instead of being re-shot:

| Slot (was) | Now | Where |
|---|---|---|
| Hero satellite portraits (2 tiles) | "Signal chip" tiles: the AIZU ping mark (dot + arc, lime on ink) plus a short mono label (`Ask`, `Match`) | `index.html` hero, `.hero-tile--signal` in `css/hero.css` |
| Core-HR flanking marquees (12 portrait cards) | Cards styled as **platform-native posts** — two each for Instagram, LinkedIn, X, Reddit, YouTube and Telegram, reproducing each surface's layout, palette and type conventions around a fabricated public intent signal. Six ask about a **product** (*"found this dinner set at a market — anyone know a maker who does these?"*), six about a **service** (*"can anyone recommend a customs broker for EU shipments?"*), and six carry **media**: five real licensed photos plus a sixth used as a video poster with a duration chip. There are **no drawn or placeholder graphics left in the card media** — every attachment is a real photograph from `assets/photos/`, see [Assets](#assets--sources--licences). Avatars remain monograms, never portraits. See [Platform skins](#platform-skins-in-someone-is-searching-for-what-you-sell) for the trademark position | `index.html` core-hr, `.cs-card` in `css/core-hr.css` |
| Bento "for teams" avatar ring (8 portraits) | 8 lime dot nodes on the same rotating orbit slots the photos used to fill | `index.html` bento card 5, `.bento-ring__dot` in `css/bento.css` |
| Testimonial coverflow avatars (3 portraits) | The AIZU ping mark, inline SVG, per card | `index.html` testimonials, `.testimonials-card-avatar--mark` in `css/testimonials.css` |

No `assets/people/` reference remains anywhere in the page, and no portrait photography is
used in any of these four slots. Three of them are pure CSS/SVG; the core-hr cards later
gained licensed **product/scene** photography (never people-as-decoration) in their attached
media — see [Assets](#assets--sources--licences) for per-file provenance.

### Platform skins in "Someone is searching for what you sell"

The 12 marquee cards are deliberately skinned as **real platforms** — Instagram, LinkedIn, X,
Reddit, YouTube and Telegram, two cards each — reproducing each surface's layout, palette and
type conventions. The section's whole argument is "this is the feed, AIZU gives you the one
signal instead", and a generic unbranded card doesn't make that argument: it has to look like
the places the conversation actually happens.

Read this as a deliberate, owner-directed exception to the rule stated further up this file
(*"the brand does not name or depict source platforms"*), not as a contradiction someone
should quietly fix. That rule still governs everywhere else on the page — the integrations arc
carries checkpoint numerals rather than logos, and the footer has no platform row.

Three constraints hold the exception in place, and should survive any edit here:

- **No vendored brand assets.** Every mark is hand-drawn inline SVG in the sprite at the top of
  `sections/core-hr.html`, approximating the glyph. Nothing was copied from an official brand
  kit, and nothing under `assets/` is referenced. This mirrors how the old footer social icons
  were handled.
- **Platform-native type, not AIZU type.** The cards use a system UI stack rather than Inter
  Tight. A card in the brand face stops reading as a real post, which defeats the section.
- **No lime, anywhere in the field.** Platform brand colours are allowed here precisely because
  they read as someone else's chrome; lime is AIZU's signal colour and must never mark noise.

**Unresolved before this ships as a real page:** reproducing third-party trade dress and marks
on a commercial landing page is a trademark question, not a design one, and it has not been
cleared. The two live options are to keep the skins and get sign-off, or to fall back to the
unbranded generic-post design (monogram avatar, hand-drawn heart/reply glyphs, no platform
identity), which is a markup-only revert of `sections/core-hr.html` — the CSS keeps working.

### Bento dashboard copy (was still CoreShift)

The section heading and surrounding copy for the bento cards were rebranded in the pass above,
but the fake dashboard UIs drawn *inside* two of the five cards were not — they still read as
literal CoreShift HR product screens. A follow-up pass found and fixed this:

| Card | Was | Now |
|---|---|---|
| Card 1, "One brief, every check" — dashboard title | "Attendance Report" | "Match Report" |
| Card 1 — dropdown chip | "Monthly" (inconsistent with the Mon–Fri chart under it) | "Weekly" |
| Card 4, "Every lead, one drawer" — first panel title | "Employees" | "Leads" |
| Card 4 — panel tabs | "Project Dept. 24" / "Sales Dept. 11" / "Marketing Dept." | "New 24" / "Qualified 11" / "Contacted" |
| Card 4 — row labels ×3 (+ envelope `aria-label`s) | "Owner / Full access", "Admin / PM/BA", "Member / PM/BA" | "New lead / Just matched", "Qualified / Ready to buy", "Contacted / Reply sent" |
| Card 4 — second panel title | "Training Participation" | "Response Rate" |

Matching class renames in `index.html`, `sections/bento.html`, and `css/bento.css` (none of
these are queried by `js/bento.js`, so no behaviour changed): `bento-visual--hr` →
`bento-visual--brief`, `bento-panel--employees` → `bento-panel--leads`, `bento-panel--training`
→ `bento-panel--response`, `bento-emplist` → `bento-leadlist`, `bento-emprow` →
`bento-leadrow`, `bento-emprow__name` → `bento-leadrow__name`.

Two things were checked and deliberately left as-is:

- The core-hr marquee's "employee background checks" / "team onboarding" lines are quote
  snippets from a fictional buyer asking about an HR-adjacent vendor — a plausible example of
  niche diversity in the public-conversation feed, not a description of AIZU's own product.
- The FAQ's "Monthly plans cancel..." line and the Response Rate panel's own
  Daily/Weekly/Monthly toggle are generic billing/reporting-cadence words, not HR vocabulary.

Still open: `SPEC.md` documents the original CoreShift design — old fonts (Geist/Outfit), the
"Attendance Report" and "Employees" panel copy, etc. That's expected; it's the historical
reverse-engineering record of the reference video, not a description of the live page, and
wasn't touched by this pass.

## How to run

No build step, no install. Any static file server works, since fonts are all local and the
page must not be opened via `file://` for module-less asset loading to behave consistently
across browsers (CSS `@import`/`@font-face` work either way, but a local server avoids
browser-specific `file://` quirks). From this folder:

```bash
python -m http.server 8080
# then open http://localhost:8080/
```

Any other static server (`npx serve`, `php -S`, VS Code "Live Server", etc.) works equally
well — there is no server-side logic.

## What's implemented

Nine sections, assembled in `index.html` in page order and each independently authored under
`sections/` (markup), `css/` (styles), and `js/` (behaviour, one `CS.initXxx()` per section):

| Section | Partial | Notes |
|---|---|---|
| Fixed nav | `sections/nav.html` / `css/nav.css` / `js/nav.js` | Floating pill, AIZU ping mark + wordmark, drop-in entrance. Links to `#plans` and `#faq`. |
| Hero | `sections/hero.html` / `css/hero.css` / `js/hero.js` | Node-graph illustration, icon roulette, connector line draw-in, idle float + mouse parallax. Two satellite slots are now ping-mark "signal chips" (see de-personalization table). |
| "Someone is searching for what you sell" (was Core HR solutions) | `sections/core-hr.html` / `css/core-hr.css` / `js/core-hr.js` | Flanking infinite marquees with scroll-scrubbed parallax, carrying platform-skinned post cards (Instagram, LinkedIn, X, Reddit, YouTube, Telegram) instead of portraits — the feed the heading contrasts against. See [Platform skins](#platform-skins-in-someone-is-searching-for-what-you-sell). |
| "Built for people who sell direct" (was Built for everyone) | `sections/bento.html` / `css/bento.css` / `js/bento.js` | 5-card bento grid: animated bar charts, a rolling pill stack, a rotating dot-node ring (was an avatar ring). |
| "Five checkpoints. One signal." (was Integrations) | `sections/integrations.html` / `css/integrations.css` / `js/integrations.js` | The arc carousel - five tiles on a large invisible circle, continuous recycle-loop rotation. Tiles carry checkpoint numerals, not logos. |
| "How AIZU behaves" (was Words of Appreciation) | `sections/testimonials.html` / `css/testimonials.css` / `js/testimonials.js` | Envelope-reveal entrance into a 3D coverflow carousel. Cards describe named safety mechanisms, avatar slot is the ping mark. |
| Plans — "Pay for customers, not software" | `sections/plans-faq.html` / `css/plans-faq.css` / `js/plans-faq.js` | New section, no CoreShift analog. 4-tier pricing grid (Free / Starter / Pro / Scale), one lime-accented featured card, scroll-triggered blur/fade-up entrance. See `SPEC.md` addendum. |
| FAQ — "Fair questions, straight answers" | `sections/plans-faq.html` / `css/plans-faq.css` / `js/plans-faq.js` | New section, no CoreShift analog. Two-column layout, accordion `<details>` list with GSAP height tweens, one-open-at-a-time. See `SPEC.md` addendum. |
| Footer | `sections/footer.html` / `css/footer.css` / `js/footer.js` | Giant AIZU wordmark with a played-once per-character drop-in (`js/footer.js`), plus a floating blur lens (`.footer-wordmark-blur`) that drifts across the word and blurs the letters behind it — pure CSS `backdrop-filter` + a soft horizontal mask, ~19s per pass, parked over the centre under `prefers-reduced-motion`. |

Plans and FAQ share one stylesheet/script pair (`css/plans-faq.css`, `js/plans-faq.js`) and
one markup partial (`sections/plans-faq.html`), each exposing `CS.initPlans` and `CS.initFaq`
respectively — still one `CS.initXxx()` call per section from `js/main.js`.

Shared foundation: `css/tokens.css` (AIZU palette/type/reset, imports `assets/fonts/fonts.css`),
`css/base.css` (layout primitives, button/heading styles), `js/split.js` (a ~100-line
char/word splitter standing in for GSAP's licensed `SplitText`), `js/reveal.js` (the shared
blur-reveal / scroll-reveal / fade-up helpers used by every section), and `js/main.js` (boots
Lenis + ScrollTrigger, then calls every section's init in page order).

Vendored (unmodified) in `vendor/`: GSAP 3.13, GSAP ScrollTrigger, Lenis — loaded as classic
`<script>` globals, no bundler.

## Assets — sources & licences

- **Fonts** (`assets/fonts/`) — **self-hosted, in active use.** `css/tokens.css` imports
  `assets/fonts/fonts.css`, which declares the `@font-face` rules the page actually loads:

  | Family | Weight | Subset | File |
  |---|---|---|---|
  | Inter Tight (variable) | 100–900 | latin-ext | `inter-tight-latin-ext.woff2` |
  | Inter Tight (variable) | 100–900 | latin | `inter-tight-latin.woff2` |
  | JetBrains Mono | 400 | latin-ext | `jetbrains-mono-400-latin-ext.woff2` |
  | JetBrains Mono | 400 | latin | `jetbrains-mono-400-latin.woff2` |
  | JetBrains Mono | 500 | latin-ext | `jetbrains-mono-500-latin-ext.woff2` |
  | JetBrains Mono | 500 | latin | `jetbrains-mono-500-latin.woff2` |

  Both families are licensed under the SIL Open Font License 1.1. Inter Tight is the display
  and body face; JetBrains Mono is used for labels, pill/plan numerals, and other data beats.
  These requests are **local** (served from this folder, not a CDN) — there is still zero
  *external* network request, but it is no longer accurate to say the page makes zero font
  requests at all: it makes six, all same-origin.
  The Geist and Outfit `.woff2` files from the CoreShift port were deleted from disk; nothing
  in the page ever loaded them.
- **Logos** (`assets/logos/`) — **empty.** The third-party product marks (Gmail, Google Meet,
  Teams, Outlook, Loom) and the footer's Instagram/X/TikTok glyphs have all been deleted from
  disk. Nothing in the page references this directory any more.
- **Photos** (`assets/people/`) — **empty.** All 24 portrait photos (`p01.jpg`…`p24.jpg` plus
  `-sq` crops) have been deleted from disk. See
  [De-personalization](#de-personalization-what-replaced-the-portraits) above for what
  replaced each usage; nothing in the page references this directory any more.

- **Card photography** (`assets/photos/`) — 5 files, ~100 KB total, all from **Pexels** under
  the [Pexels License](https://www.pexels.com/license/) (free for commercial use, no
  attribution required, modification permitted). Each was downloaded pre-cropped to its card's
  aspect ratio via Pexels' own resize parameters, so no local editing was applied beyond that
  crop. **Unlike `assets/people/`, the provenance here is recorded up front — keep this table
  updated if you swap a photo, since an unrecorded licence is exactly what got the previous
  24 photos deleted.**

  | File | Size | Subject | Source |
  |---|---|---|---|
  | `dinnerware.jpg` | 480×240 | handmade ceramic bowls and plates | [pexels.com/photo/3847470](https://www.pexels.com/photo/ceramic-dinnerware-3847470/) |
  | `camera.jpg` | 480×270 | DSLR camera body, front-on | [pexels.com/photo/1093065](https://www.pexels.com/photo/black-canon-dslr-camera-1093065/) |
  | `packaging.jpg` | 480×240 | plain kraft cardboard boxes | [pexels.com/photo/17260157](https://www.pexels.com/photo/close-up-of-cardboard-boxes-and-lids-17260157/) |
  | `chair.jpg` | 480×270 | mesh desk chair beside a desk | [pexels.com/photo/1957477](https://www.pexels.com/photo/office-chair-and-desk-1957477/) |
  | `photographer.jpg` | 480×270 | photographer shooting an outdoor event | [pexels.com/photo/31816828](https://www.pexels.com/photo/photographer-with-camera-capturing-outdoor-event-31816828/) |
  | `bathroom.jpg` | 480×270 | bathroom with grey subway tiles | [pexels.com/photo/8143708](https://www.pexels.com/photo/spacious-bathroom-with-tiled-walls-8143708/) |

  Two selection rules were applied while picking these, worth repeating on any swap: **no
  third-party brand names legible in the frame** (a first kraft-box candidate was rejected for
  carrying another company's printed branding), and **no identifiable faces** — the
  photographer's face is obscured by a cap and buff, which is why that frame was chosen over
  the alternatives.

## File map

```
index.html              assembled page — the file to open/serve
SPEC.md                 authoritative scene + motion spec, plus a Plans/FAQ addendum
README.md               this file
css/
  tokens.css            AIZU palette (+ CoreShift aliases), type tokens, reset, reduced-motion guard
                         (@imports assets/fonts/fonts.css)
  base.css               .container/.section/.surface, h1/h2/lead/body type, .btn variants
  nav.css / hero.css / core-hr.css / bento.css / integrations.css /
  testimonials.css / footer.css / plans-faq.css      one file per section, scoped class prefixes
js/
  split.js               CS.splitChars / CS.splitWords — SplitText replacement
  reveal.js               CS.blurReveal / CS.scrollReveal / CS.fadeUp — shared reveal helpers
  nav.js / hero.js / core-hr.js / bento.js / integrations.js /
  testimonials.js / footer.js / plans-faq.js         one CS.initXxx() per section
                         (plans-faq.js exports two: CS.initPlans, CS.initFaq)
  main.js                 boots Lenis + ScrollTrigger, calls every CS.initXxx() in order
sections/               the same nine section markups, kept standalone for reference/re-editing
                         (index.html inlines their content directly — edit here, then re-paste,
                         or edit index.html directly; the two are not auto-synced)
vendor/                 gsap.min.js, ScrollTrigger.min.js, lenis.min.js (unmodified, MIT/GSAP licence)
assets/
  fonts/                 self-hosted Inter Tight + JetBrains Mono woff2 subsets + fonts.css — see licences above
  photos/                 5 Pexels-licensed card photos (~100 KB) — per-file provenance table above
  logos/                  empty
  people/                 empty
ref/                    source video + extracted reference stills used to write SPEC.md
```

## Known limitations

A headless-Chrome pass has been run against the page (Chrome headless "new", served over
`http://127.0.0.1:8099`, widths 1920/1440/1280/390 plus a `prefers-reduced-motion: reduce` run
at 1440). What it verified:

- Zero console errors or warnings, and zero horizontal overflow, at every width checked.
- Both self-hosted font families load and are matched (`document.fonts.check` passes for Inter
  Tight 700 and JetBrains Mono 400).
- Zero `<img>` elements remain on the page and zero broken images.
- All 9 sections render; element counts hold at every width (12 marquee cards, 8 ring nodes, 3
  pill avatars, 6 arc tiles, 6 FAQ items).
- At 1440 with motion enabled: 18 ScrollTriggers and 97 GSAP tweens are registered. Under
  `prefers-reduced-motion: reduce`, this collapses to 1 ScrollTrigger, 1 tween, and Lenis
  disabled, with no element left collapsed or invisible.

What this did **not** cover, and is still open:

- No human has watched the motion frame by frame to judge quality/feel — the pass above checks
  that tweens *exist* and animate, not that they look right.
- No touch-device pass (mobile Safari/Chrome, real pointer/touch events).
- At 390px the flanking marquee in the "Someone is searching for what you sell" section is hidden by the
  existing `@media (max-width: 680px)` rule in `css/core-hr.css`. That's confirmed intentional
  behaviour, not a bug found by the pass — the page is desktop-first and was never designed for
  phone widths, which is a real limitation worth naming rather than a defect to fix.

- **Between 681px and 1100px the orbit still clips its outer cards.** Above 1100px every card
  is now fitted inside the gutter and stays fully visible (see the orbit note in
  `css/core-hr.css`), but at those middle widths the copy column widens to 78vw and the
  remaining gutter is narrower than a single 140px card, so `js/core-hr.js` deliberately falls
  back to the original edge-anchored ring where part of the orbit swings off screen. Fixing it
  properly means giving the copy column a narrower cap in that range, which is a layout
  decision, not a bug fix — flagged rather than guessed at.
