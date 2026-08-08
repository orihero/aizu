# CoreShift landing page — scene & motion spec

Reverse-engineered from the source video (`ref/source.mp4`, 1600×1200, 60fps, 19.47s, loops).
The video is an auto-scroll capture of a real landing page inside a rounded "device" frame on a
`#E3E3E3` backdrop. **We do not reproduce that outer frame** — we build the page itself, full-bleed.

All timestamps below are video time. Reference stills live in `ref/`.

---

## 0. Global system

### Fonts
Two families, strictly separated:

| Role | Family | Notes |
|---|---|---|
| Display — h1/h2, card titles, testimonial name, integration tile name | **Geist** (self-hosted, `assets/fonts`) | Neo-grotesque, double-storey `a`, straight-tail `y`, angled `t` cut. Stand-in for Helvetica Now Display. Weight 700; `letter-spacing: -0.035em`; `line-height: 1.02`. |
| Everything else — nav, body copy, buttons, logo wordmark, labels | **Outfit** (self-hosted) | Geometric, **single-storey `a`**, angled `t` cut. Stand-in for Gilroy. |

The `CoreShift` nav wordmark is Outfit 700, `letter-spacing:-0.02em`, ~19px.
The giant footer wordmark is Outfit 700 as well (single-storey `a` is not visible there, but the
`S`/`h`/`f`/`t` shapes match Outfit, not Geist).

### Palette
```
--white        #FFFFFF   hero bg, cards, integration + footer panels
--surface      #F0F4F5   page bg from "Core HR solutions" onward
--tile         #EEF1F3   inactive integration tiles
--ink          #0B0B0C   headings
--ink-2        #16181A   card titles
--muted        #8E9498   body copy, captions
--muted-2      #A9AFB3   fine print
--coral        #F2705D   primary CTA + giant footer wordmark
--coral-hi     #F58A79   CTA gradient top
--coral-lo     #ED6250   CTA gradient bottom
--violet       #9A67F9   secondary CTA, hero centre node, accents
--violet-hi    #A87CFB   gradient top
--violet-lo    #8B4FF7   gradient bottom
--yellow       #FAE55D   hero satellite tile
--cyan         #55D7F2   hero satellite tile
--red          #F4503F   hero satellite tile (shield)
--star         #FFC531   rating stars
```

### Shape language
- Nav pill: `border-radius: 999px`, white, `box-shadow: 0 8px 28px rgba(16,24,40,.08)`.
- Section cards: `border-radius: 28px`; feature cards `border-radius: 24px`.
- Icon tiles: squircle-ish `border-radius: 26%` of side; soft coloured drop shadow tinted to the tile hue.
- Portrait cards: `border-radius: 18px`, 3px white inner ring, `box-shadow: 0 18px 40px rgba(16,24,40,.14)`.

### Scroll model
Ordinary document scroll (no pinning, no stacked/overlapping sections) driven by **Lenis** smooth
scroll (`lerp ≈ 0.085`), with **GSAP + ScrollTrigger** for all reveals. Confirmed from `ref/B_blur_01.jpg`:
the outgoing section keeps moving normally while the incoming one enters from below.

### The signature effect — progressive blur text reveal
Used on **every** heading and paragraph on the page. This is the single most important animation.

Split into characters. Animate per-char with a left-to-right stagger:
```
from: { opacity: 0, filter: 'blur(12px)', color: '#B9BDC0', yPercent: 12 }
to:   { opacity: 1, filter: 'blur(0px)',  color: <final>,   yPercent: 0  }
ease: 'power2.out', duration: 0.55, stagger: 0.018
```
The result reads as a soft "focus wipe" travelling across the line — see `ref/B_blur_01.jpg`
("Built for ev|eryone" mid-wipe) and `ref/A_hero_01.jpg` ("All-in-on|e HR"). Subheads use the same
treatment at `stagger: 0.008`, `blur(8px)`. Buttons fade from a washed-out tint to full colour
(`opacity .35 → 1`) rather than sliding.

---

## 1. Fixed nav (visible in every frame)

Floating centred pill, `position: fixed; top: 22px`, never hides or shrinks on scroll.

`[CoreShift]  Product  Features  Pricing  Resources   ( Sign in )  [ Request a Demo ]`

- Wordmark glyph: the `C` is a custom mark — a bold ring with a wedge notch cut out of the right
  side and a solid dot at its centre. Author as inline SVG, 1em tall, then the rest as text.
- `Sign in`: white pill, 1px `#E6E9EB` border.
- `Request a Demo`: black pill `#0B0B0C`, white text, `box-shadow: 0 10px 24px rgba(0,0,0,.22)`;
  it visually overhangs the bottom edge of the nav pill by ~6px.
- Page-load: nav drops in `y:-24 → 0`, `opacity 0→1`, `0.6s power3.out`.

---

## 2. Hero — "All-in-one HR platform"  (t≈18.0 → 19.4, and 0.0 → 1.0)

White background, full viewport. Node-graph illustration on top, headline below.

### Layout (percentages of a 1440-wide container)
```
                     ┌──yellow bulb──┐            ┌──red shield──┐
   [portrait]────────┤               ├──[VIOLET]──┤              ├────────[👀 tile]
                     └──cyan balloons┘   CENTRE   └──[portrait]──┘
```
- Centre node: 176px violet squircle, gradient `--violet-hi → --violet-lo`,
  `box-shadow: 0 24px 48px rgba(139,79,247,.42)`, white 2px circled-check icon inside (~62px).
- Satellites (clockwise from top-left): yellow **lightbulb** (86px), cyan **balloons** (110px),
  left **portrait** (150px, photo), red **shield-with-bolt** (118px), lower-right **portrait**
  (96px), far-right white tile with the **👀 eyes** glyph drawn as SVG (140px).
- Connectors: 1px `#DDE1E4` polylines fanning out from the centre node, with 6px violet dots at the
  fork vertices.

> **AIZU implementation note:** the two **portrait** satellites above are reference-video content
> only. AIZU has no customer photography, so both slots are built as ping-mark "signal chip"
> tiles (mark + short mono label) instead — same position, size and idle/parallax motion, no
> photo. See `README.md` § De-personalization. Do not reintroduce photos here.

### Entrance choreography (total ≈1.45s) — `ref/A_hero_01.jpg`
| t (rel) | Beat |
|---|---|
| 0.00 | Centre tile scales `0.4 → 1` with `back.out(1.7)`, opacity 0→1. |
| 0.00–0.75 | **Icon roulette** inside the tile: the glyph cycles through ~8 shapes (scan-frame, sparkle-X, diagonal slash, plus, back-slash, 4-point star, orbit/planet) at ~90ms each, then lands on the circled check and settles. Each swap is an instant cut with a tiny `scale 1.15 → 1` pop. |
| 0.15 | Horizontal trunk lines draw outward from the tile, left and right, via `strokeDashoffset` (`0.5s power2.inOut`). |
| 0.30 | Diagonal branch lines draw from the fork points. |
| 0.35–0.70 | Satellite tiles fade + scale in `0.6 → 1`, `back.out(1.4)`, staggered 0.06s, ordered inner→outer. |
| 0.70 | Violet fork dots pop in, `scale 0 → 1`, `back.out(3)`, stagger 0.04. |
| 0.55 | H1 "All-in-one HR platform" — progressive blur reveal, per char, stagger 0.02. |
| 0.95 | Sub-copy blur reveal. |
| 1.15 | Coral CTA fades from washed tint to full. |

### Idle
Each satellite tile floats on its own loop: `y ±6px`, 3.6–5.2s, `sine.inOut`, `yoyo`, random offset.
The whole graph gets a light mouse-parallax (max 10px, tiles further from centre move more).

---

## 3. "Core HR solutions"  (t≈1.25 → 3.6) — `ref/t2.60.jpg`, `ref/C_para_01.jpg`

Background switches to `--surface`. Centred column, flanked by **two portrait-card marquees**.

Centre: white squircle badge (94px) with a violet "person" glyph → H2 `Core HR / solutions`
(two lines) → sub-copy → violet **Learn more** pill.

Marquees — 3 columns per side (6 total, mirrored):
- Column contents: portrait cards, 150–190px wide, aspect ≈ 3:4, staggered vertical offsets so the
  columns interlock into a loose diagonal.
- Each column is an **infinite vertical marquee** (content duplicated once, `y` tweened to `-50%`,
  `repeat: -1`, linear). Speeds alternate per column: 26s / 34s / 30s, and **alternate direction**
  (up / down / up) so the cluster churns.
- On top of the marquee, ScrollTrigger adds `scrub` parallax: outer columns get `yPercent: -18`,
  inner columns `yPercent: -8` across the section's scroll range.
- Cards nearest the viewport edges are partially clipped — that is intentional (see `ref/C_para_01.jpg`).

> **AIZU implementation note:** the "portrait cards" above are reference-video content only.
> AIZU's marquee columns carry the same size/count/stagger/speed/direction/parallax choreography
> but each card shows a fabricated public-intent-signal quote (short first-person ask + relative
> timestamp) instead of a photo. See `README.md` § De-personalization. Do not swap photos back in.

Entrance: badge pops (`back.out(1.7)`), H2 + sub blur-reveal, button fades, marquee columns fade in
from `opacity 0, y 40` with 0.08 stagger.

---

## 4. "Built for everyone"  (t≈3.75 → 6.9) — `ref/t5.05.jpg`, `ref/t6.20.jpg`

`--surface` background. Centred H2 + 2-line sub, then a 2-row bento grid.

**Row 1 — three equal white cards** (`1fr 1fr 1fr`, gap 24px, radius 24px, padding 28px,
`box-shadow: 0 2px 4px rgba(16,24,40,.03)`), each: visual area (~240px) then title then 2-line body.

1. **For HR professionals** — "Attendance Report" mini dashboard: white card with a `Monthly ⌄`
   dropdown, Mon–Fri row labels, and a grouped violet/coral bar chart with a dashed average line and
   a black `+17%` pill above the tallest bar. Two blurred ghost cards peek out left and right behind it.
   *Animation:* bars grow from `scaleY:0` (transform-origin bottom) with 0.06 stagger on reveal.
2. **For managers & leaders** — concentric radar rings (3 rings, 1px `#EAEDEF`) behind a **vertical
   pill roller**. Three white pills stacked; the middle one is opaque and elevated, the ones above
   and below are faded (`opacity .35`) and slightly scaled down. Every ~1.6s the stack rolls up one
   slot (`0.7s power3.inOut`), cycling:
   - 🔵 `Access Real-Time Insights`  (cyan icon chip)
   - 🔴 `Make Data-Driven Decisions`  (coral icon chip)
   - 🟡 `Track Performance in Real Time`  (yellow icon chip)
3. **For legal teams** — two overlapping pale document cards with a violet shield-check badge
   centred on top; faint vertical rule lines in the background. Badge has a slow idle float.

**Row 2 — two cards** (`~1.6fr 1fr`):
4. **All employee data at once** — wide card. A coral document icon tile top-left, then two
   overlapping panels: an **Employees** list (`Project Dept. 24 / Sales Dept. 11 / Marketing Dept.`
   tabs; rows: *Willem Gray — Visual Director*, *Dimitri Ryabell — PM/BA*, *Olivia Klyver — PM/BA*,
   each with avatar + envelope button) and a **Training Participation** bar chart
   (`Daily / Weekly / Monthly` segmented control, one violet gradient bar with a black `46%` pill).
5. **For teams & employees** — 8 portrait squircles arranged evenly on a circle around a white
   centre disc holding a black "people" glyph. The ring **rotates slowly and continuously**
   (`rotation: 360`, 40s, linear, repeat -1) while each avatar counter-rotates so faces stay upright.

> **AIZU implementation note:** the 8 **portrait squircles** above are reference-video content
> only. AIZU's ring keeps the same slot count, radius, rotation speed and counter-rotation, but
> each slot holds a lime dot node instead of a photo. See `README.md` § De-personalization.

Card entrance: `y: 48 → 0`, `opacity 0 → 1`, `scale .97 → 1`, `0.8s power3.out`, stagger 0.09,
triggered at `top 85%`.

---

## 5. "Integrate with your existing tools in seconds"  (t≈7.0 → 10.6) — `ref/t9.10.jpg`, `ref/D_arc_01.jpg`

One large white panel (radius 28px) inset in the `--surface` page, ~1300px wide.

Top: white squircle badge with a **coral gears** icon → 2-line H2 → the arc carousel.

### The arc carousel — the second signature animation
Five tiles positioned on a large invisible circle (centre far **below** the panel, radius ≈ 620px)
so they form a shallow downward-opening arc; the centre slot sits at the apex.

- Slot angles: `-32°, -16°, 0°, +16°, +32°`. Each tile is rotated **tangentially** — i.e. its own
  `rotate` equals its slot angle — so the row visibly "rolls".
- Centre tile (`0°`): **white**, 150px, `box-shadow: 0 20px 46px rgba(16,24,40,.10)`, logo at 78px.
- Off-centre tiles: `--tile` grey, 128px, no shadow, logo at 62px, `opacity` falls off toward the
  edges (`1 → .92 → .55`).
- Below the apex: the active tile's **name** (Geist 700, 22px) and **description** (Outfit, 15px,
  `--muted`), which crossfade + slide 8px on each step.

**Motion:** every 1.2s the whole wheel rotates **one slot counter-clockwise** (tiles travel left),
`1.0s power3.inOut`, then holds. The tile leaving on the left fades to 0 and is recycled to the
right edge, fading in — an infinite loop. Deck order and captions:

| Logo | Name | Description |
|---|---|---|
| Microsoft Teams | Microsoft Teams | Team chat & collaboration |
| Gmail | Gmail | Email in one inbox |
| Loom | Loom | Video feedback & communication |
| Google Meet | Google Meet | Seamless video meetings |
| Microsoft Outlook | Microsoft Outlook | Email & schedule management |

Real logos: `assets/logos/{gmail,google-meet,outlook,teams}.svg` are the official Wikimedia SVGs.
**Loom** must be hand-authored: a `#625DF5` sunburst — 12 tapered rays radiating from a small centre
circle, rays are rounded-tip teardrops, overall 24×24 viewBox.

> **AIZU implementation note:** the deck and logos above are reference-video content only. AIZU
> does not integrate with these products, does not depict third-party marks, and has no source
> platforms to name, so the arc carries five checkpoint-numbered tiles instead — same slot count,
> angles, sizing and roll/recycle motion, no logos, no deck table. `assets/logos/` is empty; do
> not repopulate it to "restore" this deck.

Entrance: panel fades/rises, badge pops, H2 blur-reveals, then the five tiles fan out from the
centre slot into their arc positions (`0.9s power3.out`, stagger 0.05 outward from the middle).

---

## 6. "Words of Appreciation"  (t≈10.75 → 14.9) — `ref/t13.95.jpg`, `ref/E_env_01.jpg`

`--surface`. H2 + 2-line sub, then a 3D testimonial carousel that **arrives out of an envelope**.

### 6a. Envelope reveal — the third signature animation
Sequence over ≈1.6s, all scroll-triggered:
1. A white envelope body (rounded rect, ~420×260) sits centred, slightly below final card position.
2. Two **violet triangles** (left flap and right flap) point inward from the sides, and a third
   violet triangle points **down** from the top — together they read as an open envelope mouth.
   Draw them as CSS `clip-path: polygon(...)` on `--violet` blocks.
3. The testimonial card, initially clipped inside the envelope (`clip-path` + `translateY(72%)`),
   **slides up and out** (`1.0s power3.out`), overshooting slightly.
4. As it clears, the flaps rotate open and fade (`scale 1 → 1.35`, `opacity 1 → 0`, violet
   desaturating to `#DCC9FF`), and the envelope body fades out.
5. Simultaneously the two neighbouring cards fade in at their 3D positions, and the prev/next
   controls fade up.

### 6b. The carousel
Perspective container (`perspective: 1400px`). Three cards visible:
- **Centre**: white, 420×470, radius 26px, `box-shadow: 0 24px 60px rgba(16,24,40,.10)`.
  Contents: 66px rounded avatar → name (Geist 700, 22px) → role (Outfit, 15px, `--muted`) →
  5 gold stars + `5.0` → quote (Outfit, 15px/1.6, `--muted`, centred, in curly quotes).
- **Side cards**: much larger footprint, rotated in Y (`±26deg`) and slightly in Z, washed out to a
  near-white gradient (`#FDFDFE → #F3F5F6`) with their content hidden — pure geometry, exactly as in
  `ref/t13.95.jpg`. `translateZ(-220px)`, `translateX(∓58%)`.
- Auto-advance every 3.2s; cards animate between slots with `1.0s power3.inOut`.
- Controls: two 46px white circular buttons (`‹` `›`) centred below the card, `box-shadow: 0 6px 16px rgba(16,24,40,.08)`.

Testimonials (from the video):
1. **Sarah Mitchell** — HR Director at Nexa Solutions — 5.0 — *"CoreShift has streamlined our HR processes, making tasks like onboarding and performance tracking more efficient. It helps us stay organised and saves our team time, allowing us to focus more on supporting our employees."*
2. **James Carter** — HR Manager at BrightPath Solutions — 5.0 — *"The platform is easy to use, keeps everything in one place, and helps our team stay on top of things without extra hassle."*
3. Add a third in the same voice to make the loop feel full.

> **AIZU implementation note:** the named people, roles, companies and star ratings above are
> reference-video content only. AIZU has no real customers to quote, so the centre-card slot
> keeps its exact geometry (avatar → name → role → quote) but the avatar is the ping mark (not a
> photo), the name/role pair names a safety mechanism (e.g. "Attach, never launch" / "Session
> safety"), the quote explains it, and the 5-star rating row is dropped. The envelope reveal and
> 3D coverflow motion, including the washed-out side-card geometry, are unchanged. See
> `README.md` § Rebrand for the current card copy.

---

## 7. Footer  (t≈15.0 → 17.6) — `ref/t16.00.jpg`, `ref/F_foot_01.jpg`

A white panel (radius 28px) inset in `--surface`, containing:
- Left: `CoreShift is the HRM platform / that build a thriving workplace / culture—all in one place.`
  (Outfit 500, 15px — keep the copy verbatim, typo included).
- Four link columns: **Product** (CoreHR, Recruit, Perform, Pulse) · **Features** (Desk, Time,
  Analytics) · **Pricing** · **Resources**.
- Right: `Follow us` + three 34px light-grey squircle buttons — Instagram, X, TikTok (monochrome
  `#16181A` glyphs).
- Below, filling the full panel width and bleeding off both edges: the giant **CoreShift** wordmark
  in `--coral`, Outfit 700, `font-size: clamp(120px, 15.5vw, 260px)`, `letter-spacing: -0.03em`,
  bottom-cropped by the panel.

**Wordmark motion (scroll-scrubbed):**
- On entry it rises and settles (`yPercent: 26 → 0`, scrubbed) and is sharp when the panel is
  centred (`ref/F_foot_01.jpg` row 1 col 3).
- As scrolling continues past that point it **blurs out progressively** —
  `filter: blur(0px) → blur(26px)`, `opacity 1 → .78`, scrubbed against the remaining scroll.
  The blur reads slightly stronger on the right, so apply a matching left→right gradient mask.

---

## 8. Cross-cutting requirements

- **Loop parity.** The video loops seamlessly from footer back to hero. Add a "back to top" behaviour
  only if it costs nothing; not required.
- **prefers-reduced-motion**: disable Lenis, disable marquees/roulette/auto-advance, replace every
  blur reveal with a plain opacity fade.
- **Responsive.** Design canvas is 1440×900. Below 1100px the bento collapses to 2-up then 1-up, the
  Core-HR marquees drop to one column per side, and the testimonial side cards hide.
- **No external network at runtime** — every font, logo and photo is already local under `assets/`.
- Vendored JS in `vendor/`: `gsap.min.js`, `ScrollTrigger.min.js`, `lenis.min.js`.
  **Do not use `SplitText.min.js`** (licence-restricted) — ship a ~20-line char/word splitter in
  `js/split.js` instead, which must preserve spaces and be safe to run twice.

---

## 9. ADDENDUM — Plans and FAQ (AIZU-only, no CoreShift source)

> Everything below this line documents scenes that do not exist in the reference video. They
> were added directly against the AIZU brief, not reverse-engineered from `ref/`, so there is no
> timestamp or still frame to cite. Markup: `sections/plans-faq.html`. Styles:
> `css/plans-faq.css`. Behaviour: `js/plans-faq.js` (`CS.initPlans`, `CS.initFaq`).

### 9a. Plans — "Pay for customers, not software"

`--surface` background (`.section--plans`), same page rhythm as the rest of the site
(140px top padding / 120px bottom). Centred head: H2 + one-line sub, max-width 560px.

**Grid:** four pricing cards, `grid-template-columns: repeat(4, 1fr)`, 22px gap, `align-items:
stretch` so every card matches the tallest. Card = `--white` fill, 1px `--line` border,
`--radius-card` corners, flex column with the CTA button pinned to the bottom via `margin-top:
auto`. Each card: plan name → price (mono, big amount + small `/mo` period) → billed-yearly
note → a divider-topped lead count (`<strong>` in mono) → CTA button.

One card only — **Starter**, the "Most popular" tier — carries the lime accent
(`border-color: var(--lime)`, a lime ring + heavier shadow, a lime pill badge, and the coral CTA
button) per the one-accent brand rule; the other three stay on the ink/grey scale with a ghost
CTA. Tiers, top to bottom: **Free** ($0, 10 leads/mo, no card) → **Starter** ($24.99/mo or
$249/yr, 250 leads/mo, featured) → **Pro** ($149/mo or $1,490/yr, 2,000 leads/mo) → **Scale**
(custom price, negotiated lead volume, "Talk to sales"). A one-line footnote below the grid
states leads are delivered monthly and plans can be changed or cancelled anytime.

**Entrance** (`CS.initPlans`, fired by `CS.scrollReveal` when the section enters view — same
IntersectionObserver-driven helper every other section uses, not a bespoke ScrollTrigger):
a single GSAP timeline —
```
0.00  title  — CS.blurReveal, word-granularity, stagger 0.03, blur 10
0.12  sub    — CS.fadeUp, y:24
0.18  cards  — CS.fadeUp, y:36, stagger 0.07 (all 4 cards as one call)
0.42  foot   — CS.fadeUp, y:20
```
No scroll-scrub, no pinning, no auto-advance — the section plays once on entry and is otherwise
static. This matches the ordinary-document-scroll model in § 0; there is no new scroll
mechanism to learn.

**Responsive:** 4-up collapses to 2×2 at ≤1020px, then 1-up at ≤520px (with reduced section
padding). No breakpoint-specific animation changes.

### 9b. FAQ — "Fair questions, straight answers"

`--white` background (`.section--faq`), 140px vertical padding both sides — this is the one
section on the page that inverts to the light/paper surface rather than staying on `--surface`
or ink, matching the footer panel's role as a closing, calmer beat.

**Layout:** two-column grid (`0.85fr 1.15fr`, 72px gap, `align-items: start`), collapsing to a
single stacked column at ≤980px. Left column ("aside"): H2 + a short "can't find your answer"
note linking `mailto:hello@aizu.uz` (lime underline-on-hover). Right column: a list of six
`<details>`/`<summary>` accordion items, top/bottom-bordered in `--line`, each summary pairing a
question (display font, 19px) with a custom plus/minus icon (a bordered circle with two
`::before`/`::after` bars; the vertical bar fades out and the circle rotates 180° + turns lime
when open).

**Entrance:** same `CS.scrollReveal` pattern as Plans —
```
0.00  title — CS.blurReveal, word-granularity, stagger 0.03, blur 10
0.14  note  — CS.fadeUp, y:24
0.16  items — CS.fadeUp, y:26, stagger 0.05 (all 6 items as one call)
```

**Interaction (not scroll-driven):** native `<details>` handles the actual open/close state and
keyboard accessibility. `CS.initFaq` layers a GSAP height/opacity tween on top purely for the
visual expand/collapse, and enforces one-open-at-a-time by closing any other open item
(`height → 0, opacity → 0`, 0.32s `power2.inOut`) before opening the clicked one
(`height 0 → auto, opacity 0 → 1`, 0.38s `power3.out`). Under `prefers-reduced-motion` the
JS listener returns immediately and lets the browser's native instant toggle run instead — no
custom height tween at all.

**Responsive:** stacks to one column at ≤980px; question font drops slightly at ≤520px along
with reduced section padding. No content or interaction changes at any breakpoint.
