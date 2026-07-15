# Madison — Montage 1 Beat Sheet (0–12s)

**Target:** ONE HyperFrames composition · **1280×720** · **12s** · **30fps** (360 frames).
**Master timeline:** all `data-start` / `data-duration` values below are in **seconds on the 12s master**.
**Determinism:** every value is fixed. No random(), no jitter. Reproduce exactly.

## Global constants

| Token | Value |
|---|---|
| `--bg` | `#F4F4F5` (very light warm gray — full-frame background for all 3 scenes) |
| `--ink` | `#1A1A1E` (headline/body text) |
| `--brand` | `#4F3CE0` (indigo/violet — callout borders, accents) |
| `--card` | `#FFFFFF` (screen mockups, callout fills) |
| `--muted` | `#8A8A93` (caption text, placeholder) |
| Frame center | `(640, 360)` |
| Default ease | `cubic-bezier(.22,.61,.36,1)` (ease-out) unless noted |
| Pop ease (bounce) | `cubic-bezier(.34,1.56,.64,1)` (overshoot) — logos & callouts |
| Base font | rounded geometric sans (Poppins / Gilroy fallback: system-ui) |

**Track / z-order convention** (higher index = nearer viewer):
- Track 0: background fill
- Track 1: back cascade screens / orbit-behind mockups
- Track 2: front hero screen / profile card
- Track 3: search pill, orbit logos, headshot
- Track 4: callout boxes / typewriter text

Background `bg-canvas` (id) lives at Track 0 for the whole 12s: `data-start=0 data-duration=12`, fill `--bg`, no animation. All positions below are `position:absolute` from top-left of a 1280×720 stage.

---

# SCENE 1 — Google Search Hook
**Clip window:** `data-start=0 data-duration=3.2` (0.0s → 3.2s; slight overlap into Scene 2 for cross-fade)

Beat: search pill fades+scales in and types "Restaurants near me", then two phone/screen mockups cascade in behind it.

### s1-search-pill (Track 3)
- **Box:** `x=390 y=317 w=500 h=66`, centered horizontally (center x=640). Rounded pill `border-radius=33px`, fill `#ECECEE`, soft shadow `0 8px 24px rgba(0,0,0,.06)`.
- **Children:** `s1-g-logo` multicolor Google "G" at `x=398 y=323 w=36 h=36` (left-inset 18px). `s1-query-text` text baseline at `x=452 y=340`, 26px `--ink`, left-aligned.
- **Resting style:** opacity 1, scale 1.
- **Entrance:** `opacity 0→1` + `transform scale(.86)→1` (transform-origin center). start **0.0s**, dur **0.5s**, ease default.
- **Typewriter** `s1-query-text`: reveal string `Restaurants near me` character-by-character. start **0.55s**, dur **1.15s** (19 chars, linear ~60ms/char), ease `linear`. Blinking caret optional (600ms cycle), remove caret at 1.7s.
- **Exit:** `opacity 1→0` + `scale 1→1.06`. start **2.75s**, dur **0.45s**.

### s1-phone-back (Track 1) — Google local-results screen
- **Box:** `x=560 y=40 w=200 h=430`, white rounded card `border-radius=18px`, shadow `0 20px 50px rgba(0,0,0,.10)`. Content: faux Google "Restaurants near me" results list (3 rows w/ thumbnail + stars). Static texture image or drawn placeholder.
- **Resting style:** slight rotate `2deg`, sits behind front phone.
- **Entrance:** slides up from below + fade. `transform translateY(90px) rotate(2deg) → translateY(0) rotate(2deg)`, `opacity 0→1`. start **1.75s**, dur **0.6s**, ease default.
- **Exit:** carries into Scene 2 (no explicit exit here — see Scene 2 reuse note). Fade to 0 over **2.9s→3.2s** if not reused.

### s1-phone-front (Track 2) — "Your Business" profile w/ star reviews
- **Box:** `x=470 y=55 w=250 h=400`, white rounded card `border-radius=18px`, shadow `0 24px 60px rgba(0,0,0,.14)`. Content: business profile header "Your Business", star rows, review snippets, "More places" button.
- **Resting style:** rotate `-2deg`, offset left of and overlapping back phone (parallax stack).
- **Entrance:** slides up + fade, staggered after back phone. `translateY(120px) rotate(-2deg) → translateY(0) rotate(-2deg)`, `opacity 0→1`. start **2.05s**, dur **0.6s**, ease default.
- **Exit:** hold; morph-handed to Scene 2 profile card (see note). If not reused: `opacity 1→0` start **2.95s** dur **0.35s**.

**Scene-1 → Scene-2 bridge:** the two phone cards are visually continuous with Scene 2's back mockups. Simplest deterministic build = fade Scene 1 phones out at 2.9–3.2s while Scene 2 profile card + mockups fade in at 3.0s (0.3s cross-dissolve). Do NOT rely on shared node reuse across clips; use a timed cross-fade.

---

# SCENE 2 — The Platform Overload
**Clip window:** `data-start=3.0 data-duration=5.2` (3.0s → 8.2s)

Beat: a central business-profile card sits behind; 8 review/social logos POP IN one-by-one (radial, bounce) and rest in an orbit ring around the card.

### s2-profile-card (Track 2) — central Instagram-style / business profile
- **Box:** `x=538 y=190 w=204 h=390`, white rounded card `border-radius=16px`, shadow `0 24px 60px rgba(0,0,0,.14)`. Content (top→bottom): header "Your Business", star row `4.5 · 1,426 Reviews`, price/cuisine line, hero food photo, action row (Call·Map·Website·Review), "Ratings and reviews" `4.5`, ranking lines, RATINGS bars (Food/Service/Value/Atmosphere).
- **Resting style:** upright, centered on x=640.
- **Entrance:** `opacity 0→1` + `scale(.92)→1`. start **3.0s**, dur **0.5s** (cross-fades with Scene 1 phones).
- **Idle:** hold static (or optional 0.5px vertical bob, 4s loop — skip for determinism).
- **Exit:** `opacity 1→0` + `scale 1→.9`. start **7.7s**, dur **0.5s**.

### s2-mock-behind-left / s2-mock-behind-right (Track 1) — faded fan screens
- Two ghost mockups peeking behind profile: left `x=380 y=205 w=180 h=360 rotate(-3deg) opacity .55`; right `x=720 y=210 w=190 h=350 rotate(3deg) opacity .55`. White cards, blurred content.
- **Entrance:** with profile card, `opacity 0→.55` + small `scale(.94)→1`, start **3.1s**, dur **0.5s**.
- **Exit:** fade `→0` start **7.7s** dur **0.5s**.

### Orbit logos (Track 3) — 8 badges, pop-in one-by-one on a ring
Ring geometry: center `(640,360)`, radius **≈305px** (positions taken from reference frame). Each logo is a rounded badge/mark. **Resting style:** opacity 1, scale 1, no drift (deterministic — "orbit" is the arrangement, not continuous rotation). Pop cadence = **one every 0.28s** starting at **3.5s**. Each pop: `opacity 0→1` + `transform scale(0)→1`, dur **0.45s**, ease **Pop (bounce)**, transform-origin center.

| id | logo | x | y | w | h | pop start |
|---|---|---|---|---|---|---|
| `s2-logo-google` | Google G | 606 | 92 | 68 | 68 | 3.50s |
| `s2-logo-instagram` | Instagram | 944 | 150 | 68 | 68 | 3.78s |
| `s2-logo-yelp` | Yelp | 1004 | 528 | 112 | 74 | 4.06s |
| `s2-logo-tripadvisor` | Tripadvisor owl | 1038 | 342 | 70 | 70 | 4.34s |
| `s2-logo-twitter` | Twitter/X bird | 428 | 606 | 68 | 68 | 4.62s |
| `s2-logo-booking` | Booking "B." | 283 | 113 | 68 | 68 | 4.90s |
| `s2-logo-facebook` | Facebook | 206 | 497 | 68 | 68 | 5.18s |
| `s2-logo-trustpilot` | Trustpilot | 94 | 330 | 210 | 52 | 5.46s |

(Booking = the `B.` navy rounded square; Tripadvisor = green owl; positions mirror the reference so the cluster reads balanced around the card.)

- **Logos exit:** all together `opacity 1→0` + `scale 1→.7`, start **7.7s**, dur **0.45s**, ease default.

---

# SCENE 3 — The Overwhelmed Owner
**Clip window:** `data-start=8.0 data-duration=4.0` (8.0s → 12.0s)

Beat: a business-owner headshot fades into center; 8 rounded task-callout boxes stagger-in radially around it (mounting workload).

### s3-headshot (Track 3)
- **Box:** circle `x=511 y=222 w=258 h=258` (center `(640,351)`), `border-radius=50%`, overflow hidden. Content: business-owner headshot on muted sage backdrop (`#A9C4C2`), head slightly turned/looking down (overwhelmed).
- **Entrance:** `opacity 0→1` + `scale(.9)→1`. start **8.0s**, dur **0.6s**, ease default.
- **Idle:** static (optional single face-swap cross-fade at 10.0s if a 2nd headshot asset exists — otherwise hold).
- **Exit:** `opacity 1→0` + `scale 1→.94`. start **11.6s**, dur **0.4s** (into converge/next montage).

### Callout boxes (Track 4) — 8 rounded outlined boxes, radial stagger-pop
Style (all): fill `#FFFFFF`, border `1.5px solid --brand`, `border-radius=10px`, padding `18px 20px`, text 19px/1.35 `--ink`, soft shadow `0 6px 18px rgba(79,60,224,.08)`, left-aligned. **Resting:** opacity 1, scale 1.
Each entrance: `opacity 0→1` + `transform scale(.6)→1` from box center, dur **0.4s**, ease **Pop (bounce)**. Stagger = **one every 0.22s** starting at **8.4s** (clockwise from top).
Exit (all together): `opacity 1→0` + `scale 1→.9`, start **11.5s**, dur **0.4s**.

Positions (x,y = top-left; sizes from reference frame f_012):

| id | x | y | w | h | pop start | string (verbatim) |
|---|---|---|---|---|---|---|
| `s3-callout-1` | 483 | 78 | 300 | 68 | 8.40s | Optimize my social media campaigns for each platform |
| `s3-callout-2` | 830 | 205 | 253 | 68 | 8.62s | SEO optimize my Google Business Profile |
| `s3-callout-3` | 903 | 332 | 285 | 68 | 8.84s | Consistently create engaging SEO optimized content |
| `s3-callout-4` | 828 | 460 | 348 | 68 | 9.06s | Ensure my business information is consistent across dozens of platform |
| `s3-callout-5` | 489 | 588 | 310 | 66 | 9.28s | Respond to all my online reviews across multiple platforms |
| `s3-callout-6` | 190 | 460 | 262 | 68 | 9.50s | Continuously monitor and analyze competitors |
| `s3-callout-7` | 79 | 332 | 300 | 70 | 9.72s | Derive actionable insights from data spread across platforms |
| `s3-callout-8` | 192 | 205 | 258 | 68 | 9.94s | Post content daily to all my social media accounts |

All 8 boxes are settled by **10.34s**; hold 10.34s→11.5s, then exit. This leaves the composition on-screen at the 12s cut (headshot + full ring) for the montage boundary.

**Exact callout strings (copy verbatim — the 8 required):**
1. Optimize my social media campaigns for each platform
2. Post content daily to all my social media accounts
3. SEO optimize my Google Business Profile
4. Derive actionable insights from data spread across platforms
5. Consistently create engaging SEO optimized content
6. Continuously monitor and analyze competitors
7. Ensure my business information is consistent across dozens of platforms
8. Respond to all my online reviews across multiple platforms

> Note: reference frame renders callout #7 truncated as "…dozens of platform"; the scenario master string ends "…dozens of platforms" (plural). Use the plural scenario string (row `s3-callout-4` above uses the verbatim scenario copy).

---

## Timing summary (master 12s)
| t (s) | event |
|---|---|
| 0.0 | search pill scale/fade in |
| 0.55–1.7 | typewriter "Restaurants near me" |
| 1.75 / 2.05 | back phone / front phone cascade in |
| 2.75 | search pill exit |
| 2.9–3.2 | Scene1↔Scene2 cross-dissolve |
| 3.0 | profile card + behind mockups in |
| 3.5–5.9 | 8 logos pop in (0.28s apart) |
| 7.7 | Scene 2 elements exit |
| 8.0 | headshot fade in |
| 8.4–10.34 | 8 callouts stagger-pop in (0.22s apart) |
| 11.5–12.0 | Scene 3 exit / montage boundary |
