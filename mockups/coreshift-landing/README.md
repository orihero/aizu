# AIZU landing page (mockup, CoreShift structure)

A pixel-oriented, fully-animated landing-page prototype — plain HTML/CSS/JS, no build step,
no framework, no backend. The layout and motion were reverse-engineered from a 19.47s
auto-scroll capture video (`ref/source.mp4`) plus extracted still frames (`ref/*.jpg`) of a
Dribbble/reference concept for a fictional HR SaaS product called "CoreShift". The full
scene-by-scene motion spec lives in [`SPEC.md`](./SPEC.md).

**It has since been rebranded to AIZU** — see [Rebrand](#rebrand-coreshift--aizu) below.
The CoreShift *structure, layout, radii and every animation* are unchanged; the palette,
typography, marks and copy are now AIZU's.

This lives under `mockups/` in the Aizu repo and is **not** part of the Aizu product build.
It is a design prototype only — the real landing page is `marketing/website/index.html`,
which this does **not** touch.

## Rebrand: CoreShift → AIZU

| Layer | Before | After |
|---|---|---|
| Ground | white `#ffffff` / `#f0f4f5` | ink `#16161a`, with a raised panel/card ladder |
| Accents | coral, violet, yellow, cyan, red, star-gold | lime `#d9f24f` **only**, as signal; everything else on an ink/grey scale |
| Type | Geist (display) + Outfit (sans) | Inter Tight throughout, JetBrains Mono for labels and data beats |
| Logo | the CoreShift "C" ring glyph | the AIZU ping mark (dot + arc), matching aizu.app |
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
  describe named safety mechanisms, and the avatar slot carries the ping mark, which also
  answers "whose photo is this?" with "nobody's". The 3D coverflow motion is unchanged.

### Still placeholder — resolve before this is treated as a real design

- **24 portrait photos** (`assets/people/`) remain in the hero satellites, the Core-HR
  marquee and the bento team ring. Their licence history was never recorded (see Assets
  below), and a wall of faces implies a customer roster AIZU does not have. They are
  placeholder-only and need replacing or removing.
- **`sections/*.html` were not updated** by the rebrand — only the assembled `index.html`
  was. The two were already documented as not auto-synced; they are now further apart.
- Section *content* decisions (what the marquee and bento cards should actually show) were
  deliberately deferred — the goal of this pass was to see the complete CoreShift design in
  AIZU's skin first.

## How to run

No build step, no install. Any static file server works, since fonts/logos/photos are all
local and the page must not be opened via `file://` for module-less asset loading to behave
consistently across browsers (CSS `@import`/`@font-face` and images work either way, but a
local server avoids browser-specific `file://` quirks). From this folder:

```bash
python -m http.server 8080
# then open http://localhost:8080/
```

Any other static server (`npx serve`, `php -S`, VS Code "Live Server", etc.) works equally
well — there is no server-side logic.

## What's implemented

Seven sections, assembled in `index.html` in page order and each independently authored under
`sections/` (markup), `css/` (styles), and `js/` (behaviour, one `CS.initXxx()` per section):

| Section | Partial | Notes |
|---|---|---|
| Fixed nav | `sections/nav.html` / `css/nav.css` / `js/nav.js` | Floating pill, AIZU ping mark + wordmark, drop-in entrance. |
| Hero | `sections/hero.html` / `css/hero.css` / `js/hero.js` | Node-graph illustration, icon roulette, connector line draw-in, idle float + mouse parallax. |
| "One signal, not a feed" (was Core HR solutions) | `sections/core-hr.html` / `css/core-hr.css` / `js/core-hr.js` | Flanking infinite portrait marquees with scroll-scrubbed parallax. |
| "Built for people who sell direct" (was Built for everyone) | `sections/bento.html` / `css/bento.css` / `js/bento.js` | 5-card bento grid: animated bar charts, a rolling pill stack, a rotating avatar ring. |
| "Five checkpoints. One signal." (was Integrations) | `sections/integrations.html` / `css/integrations.css` / `js/integrations.js` | The arc carousel - five tiles on a large invisible circle, continuous recycle-loop rotation. Tiles now carry checkpoint numerals, not logos. |
| "How AIZU behaves" (was Words of Appreciation) | `sections/testimonials.html` / `css/testimonials.css` / `js/testimonials.js` | Envelope-reveal entrance into a 3D coverflow carousel. Now safety mechanisms, not testimonials. |
| Footer | `sections/footer.html` / `css/footer.css` / `js/footer.js` | Giant scroll-scrubbed AIZU wordmark with a progressive blur-out. |

Shared foundation: `css/tokens.css` (AIZU palette/type/reset), `css/base.css` (layout primitives,
button/heading styles), `js/split.js` (a ~100-line char/word splitter standing in for GSAP's
licensed `SplitText`), `js/reveal.js` (the shared blur-reveal / scroll-reveal / fade-up
helpers used by every section), and `js/main.js` (boots Lenis + ScrollTrigger, then calls
every section's init in page order).

Vendored (unmodified) in `vendor/`: GSAP 3.13, GSAP ScrollTrigger, Lenis — loaded as classic
`<script>` globals, no bundler.

## Assets — sources & licences

- **Fonts** (`assets/fonts/`) — **no longer used.** The Geist and Outfit `.woff2` subsets are
  still on disk, but `css/tokens.css` no longer imports them: AIZU is Inter Tight with a mono
  face for labels, both taken from the system stack, so the page makes zero font requests.
  The files can be deleted once it is certain the CoreShift look will not be revisited.
- **Logos** (`assets/logos/`) — **no longer referenced by the page.** These third-party
  product marks (Google for Gmail/Google Meet, Microsoft for Teams/Outlook, plus a
  hand-authored `loom.svg`) were removed from the integrations arc during the rebrand, since
  AIZU does not integrate with them and the brand does not depict source platforms. The files
  remain on disk but nothing loads them. The footer's Instagram/X/TikTok glyphs are still
  present and are hand-inlined generic monochrome icon shapes (not official brand SVGs) so
  they can be recoloured with `currentColor` — whether AIZU should link any social channel at
  all is an open question.
- **Photos** (`assets/people/`) — 24 portrait photos (`p01.jpg`…`p24.jpg`, plus `-sq` square
  crops) used as placeholder headshots throughout (hero satellites, Core HR marquee, bento
  team ring, testimonial avatars). These were already present in the project's asset pool
  before this integration pass; their original source/licence was not recorded alongside
  them, so treat them as **placeholder-only** — swap in licensed or first-party photography
  before using this mockup as a real production page.

## File map

```
index.html              assembled page — the file to open/serve
SPEC.md                 authoritative scene + motion spec
README.md               this file
css/
  tokens.css            AIZU palette (+ CoreShift aliases), type tokens, reset, reduced-motion guard
  base.css               .container/.section/.surface, h1/h2/lead/body type, .btn variants
  nav.css / hero.css / core-hr.css / bento.css / integrations.css /
  testimonials.css / footer.css        one file per section, scoped class prefixes
js/
  split.js               CS.splitChars / CS.splitWords — SplitText replacement
  reveal.js               CS.blurReveal / CS.scrollReveal / CS.fadeUp — shared reveal helpers
  nav.js / hero.js / core-hr.js / bento.js / integrations.js /
  testimonials.js / footer.js          one CS.initXxx() per section
  main.js                 boots Lenis + ScrollTrigger, calls every CS.initXxx() in order
sections/               the same section markup, kept standalone for reference/re-editing
                         (index.html inlines their content directly — edit here, then re-paste,
                         or edit index.html directly; the two are not auto-synced)
vendor/                 gsap.min.js, ScrollTrigger.min.js, lenis.min.js (unmodified, MIT/GSAP licence)
assets/                 fonts/, logos/, people/ — see licences above
ref/                    source video + extracted reference stills used to write SPEC.md
```

## Known limitations

- No live browser was available during authoring or integration in this environment, so all
  verification was static (syntax checks, tag-balance checks, selector cross-referencing,
  asset-path resolution). A real browser pass (desktop + the 1280/1920 responsive breakpoints,
  `prefers-reduced-motion`) is worth doing before treating this as final.
- The portrait photos under `assets/people/` need a licence source confirmed/attached before
  any real-world (non-mockup) use.
