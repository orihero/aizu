# Build Notes

Running record of decisions and environment constraints for the AIZU rebuild.
See `ANALYSIS.md` for the reference-video breakdown and `SCRIPT.md` for narration + brand tokens.

## Environment

| Capability | Status |
| --- | --- |
| HyperFrames CLI | ✅ pinned `hyperframes@0.7.94` (latest at scaffold time) |
| Chrome (render) | ✅ bundled headless shell |
| FFmpeg / FFprobe | ✅ 8.1.2 |
| Brand logos (`--type logo`) | ✅ works — svgl / simple-icons, no auth needed |
| Photos & illustrations (`--type image`) | ❌ needs the HeyGen CLI, or local FLUX (`mflux-generate`) |
| Voiceover / TTS (`--type voice`) | ❌ needs the HeyGen CLI, or local Kokoro |
| Music bed (`--type bgm`) | ❌ needs the HeyGen CLI |
| Transcription (whisper-cpp) | ❌ not built |

**To unlock audio and photography:** install the HeyGen CLI
(<https://developers.heygen.com/cli>) then `heygen auth login --oauth`. Until then, scenes are
built silent and use no photographic media.

## Decisions so far

- **Route:** `/general-video`. This is a custom scene-by-scene rebuild, not a site-capture or
  captioned-footage job, and the user drives scene assignment.
- **Timing anchor:** S01 keeps the reference video's 9.5 s so the rebuild can be A/B'd against
  `reference/clips/s01_search-discovery.mp4`. Later scenes will compress — the AIZU script is
  ~75–85 s of VO against Madison's 113 s.
- **Six platforms** (stated in VO, so they must stay consistent everywhere):
  Instagram · Telegram · YouTube · LinkedIn · Reddit · X.
  Resolved marks live in `.media/images/`; the mapping is in `.media/index.md`.
  Facebook, TikTok, Google and the legacy Twitter bird were also resolved and are unused —
  available if the set changes.
- **`Sora` is embedded locally** (`assets/fonts/sora-latin*.woff2` + `@font-face` in `index.html`)
  rather than left to the compiler's implicit Google Fonts fetch. That avoids the
  `font_family_without_font_face` lint warning and the fail-closed fetch in cloud renders.
  `Inter` is pre-bundled by the renderer, so it is just named.
- **`--faint` is darkened to `#747680`** from Ink × Lime's `#9b9eab`. At video scale the shipped
  value is 2.67:1 on white and fails the contrast gate. Noted inline in `index.html`.
- **No stock portraits.** Account avatars use ink-gradient initials (`NS`, `AR`), which read as
  real accounts and sidestep the licensing/consent question the reference video's stock faces
  raise. Revisit if photo resolution becomes available.

## Project layout

Scene sources live in `compositions/`. **`index.html` is the only root-level composition** — a second
one trips `multiple_root_compositions`. So `index.html` is a swap slot, not a source file:

```bash
node tools/use-scene.mjs scene02   # load a scene into index.html
npm run check                      # gate it (check only ever sees index.html)
npm run dev                        # review it in Studio at localhost:3002
npx hyperframes render --composition compositions/scene02.html -o out/scene02.mp4
```

`render --composition` reads `compositions/` directly and needs no swap; `check` and Studio do.

**Asset paths are always project-root-relative** — `assets/…`, `.media/…` — in every file regardless
of which directory it sits in. The renderer resolves them against the project root, and a `../`
prefix fails `invalid_parent_traversal_in_asset_path` (Studio resolves from the root and 404s even
though renders would rewrite it).

## Real product footage — `assets/capture/`

| File | What |
| --- | --- |
| `dashboard.png` / `dashboard.mp4` | The AIZU panel Dashboard, 1920×1080, dark theme, fully rendered |
| `reports.png` / `reports.mp4` | The Reports page — channel-over-time, CPL trend, spend donut, system health |

**Provenance:** the app lives at `D:\projects\aizu`. The panel runs standalone with **no backend**
via `cd admin-panel && npm run dev:demo` — `--mode demo` swaps in a seeded deterministic repository
(`src/demo/createDemoRepository.ts`), so captures are reproducible.

**Headless Chrome cannot capture the charts.** `chrome-headless-shell --screenshot` renders the
whole dashboard except "Leads by channel" and "Capture funnel", which come out empty at any
`--virtual-time-budget`. The stills above were therefore pulled from an existing Playwright capture
rather than a fresh headless shot. Use Playwright (see below) or a headed browser for new captures.

**The user's stability rule:** the **Dashboard is stable** — safe to capture and reuse. **Every other
page will change** (functionality, not design), so any capture of Campaigns / Leads / Settings /
the new-campaign wizard should be treated as provisional and re-shot before final render.

### There was an earlier promo-video project

`D:\video\_retired-aizu-video-projects\aizu-demo\` (retired 2026-08-06 and moved out of the Aizu
repo, but kept on disk) is a separate, earlier **HyperFrames** project — its own
`script.md`, `DESIGN.md`, `motion-library.md`, `timing-map.json`, compositions, four renders
(2026-08-05), and a **Playwright capture harness** (`capture/`, with `node_modules` installed) that
produced ten 1920×1080/30fps captures in `assets/captures/`:

| | | | |
| --- | --- | --- | --- |
| 01 Campaigns list | 02 New-campaign brief | 03 "Drafting your campaign…" | 04 "Analyzing your product…" |
| 05 Campaigns + live Run drawer | 06 Leads board + drawer | 07 Lead detail w/ comments | 08a Leads board |
| 08b Settings — team | 09 **Dashboard + Reports** | | |

`capture-plan.md` there documents the seeded state and interaction script behind each one. That
harness is the right tool for any further product footage — it is already wired to the demo
repository. **Flagged to the user as possible duplicated effort; no decision taken.**

## Scene 01 — built

`index.html` · 9.5 s · output `out/scene01.mp4`

| Beat | Time | What happens |
| --- | --- | --- |
| 1 | 0.15–0.75 s | Search pill arrives (`power3.out`, no overshoot) |
| 2 | 0.75–3.35 s | `How to find clients/leads?` types in at ~10 cps |
| 3 | 3.35–9.5 s | Caret drops into a finite square-wave blink |
| 4 | 3.60–4.50 s | Pill lifts and settles at the top of frame |
| 5 | 4.50–5.41 s | Card deck cascades: results → social post → lead profile |
| 6 | 5.70–6.95 s | Six platform chips land, 0.14 s stagger, alternating sides |
| 7 | 6.95–9.5 s | Slow camera settle (`scale 1 → 1.035`) so the hold breathes |

Structure: 2 clips (`#bg` track 0, `#scene` track 1), one paused GSAP timeline on
`window.__timelines["main"]`. Static 3D poses sit on `.slot` wrappers so GSAP never fights a CSS
transform on the same element.

`npx hyperframes check` → **0 errors**. 5 contrast warnings remain, all transient: they are sampled
mid-fade while cards are still at partial opacity, and clear once each card is fully on.

### Open on this scene

- No VO or music yet (see Environment).
- The three result-row thumbnails are neutral tiles. They read as favicon slots at final scale;
  real imagery would need the media path unlocked.
- The lead card's lime **Reacted** badge plants AIZU's mechanic early. Cut it if scene 1 should stay
  purely problem-framing.

## Scene 02 — built · "Ads cost, and mostly miss"

`compositions/scene02.html` · 5.5 s · `out/scene02.mp4`

A live campaign card. Budget races to **$4,820** on a red bar; replies reach **38**; only **3** are
worth calling. Cost per qualified lead climbs $180 → **$1,606** against "industry average · $180",
punching on the landing as the panel deepens. The three bars stepping down are the argument.

Chosen over two alternatives the user reviewed side by side — it is the only one of the three that
says *expensive* with a number, and it reuses scene 01's card-and-chip vocabulary so the two cut
together. Small red text uses `--danger-deep: #9d1414`; `#dc2626` at 21px over the soft tint is only
4.2:1 and fails AA (the large `$1,606` keeps `--danger`, since display sizes clear 3:1).

## Scene 03 — built · "Doing it yourself"

`compositions/scene03.html` · 5.5 s · `out/scene03.mp4`

Seven jobs land on one person one at a time, creep inward, then collapse through the centre and
leave the lone figure — a deliberate hand-off into "Until now." The inward creep is computed from
each chip's authored position at setup, never measured at tween time.

The figure is an ink circle with a glyph rather than a stock portrait, matching scene 01's
initials-not-photos decision.

## Scene 04 — built · "Until now. Meet AIZU."

`compositions/scene04.html` · 5.0 s · `out/scene04.mp4`

Scene 03's lone figure keeps contracting, the ink it is made of blooms out and takes the whole
frame, and one lime spark survives at centre — which turns out to be **the dot in AIZU's own mark**.
The arc draws itself around it (`strokeDashoffset` on a `pathLength="1"` path), then the wordmark
rises out of its mask beside it and the tagline types in.

Chosen over a lime corner-sweep and a diagonal blade wipe. It was the only candidate where the
transition *means* something: the problem doesn't get wiped away and replaced by a brand, it turns
into the brand. The other two are in `compositions/alt-scene04-sweep.html` and
`alt-scene04-blade.html`, both passing check — the blade is the louder option if the video ends up
being watched mostly in a feed.

**This is where the piece flips to the dark palette** (`--bg: #0e0f14`, lime as primary fill),
matching Ink × Lime's dark theme. Everything after scene 04 should stay dark.

### The seam with scene 03

Scene 04's first frame is pixel-identical to scene 03's last: same `#hub` size, position, gradient
and `--shadow-card`, the same red pressure glow at full strength, and the pinch starts at
`scale: 0.9` — exactly where scene 03 leaves it — beginning at `t=0` so there is no jump. The
`power2.in` ease supplies the held beat instead of a delay, because a delayed tween would render
frame 0 at the CSS default and break the match. Verified frame-by-frame in `out/sheets/seam-check.jpg`.

**If either scene is retimed, re-check that seam.** It is the only cross-scene dependency in the
project so far.

### Open on this scene

- The wordmark is **set in Sora**, not a real logotype — only the icon exists as artwork
  (`assets/brand/aizu-mark.svg`, plus a transparent-background `aizu-glyph.svg`). A real AIZU
  logotype would be a single-element swap in `#mark`.
- The mark is inlined as SVG rather than `<img>` so the arc can be drawn; its ink rounded-square
  container is dropped because the frame is already ink.

## Scene 05 — built · "Three pillars + price"

`compositions/scene05.html` · 6.5 s · `out/scene05.mp4`

The **real dashboard** (`assets/capture/dashboard.png`) rises in, then one pillar chip lands per
spoken phrase — Find leads · Manage them · Actionable insights — and **$9/month** gets its own beat
on a lime pill with a scrim behind it.

- **Chip stagger is 0.58 s, deliberately wider than the "one arriving beat" rule.** Each chip is
  cued to a phrase in the VO, so they should read as three separate statements, not one group.
  Retime these if the VO comes in at a different pace.
- **The price is on screen.** The reference only ever said its price out loud; $9 is aggressive
  enough to be shown. The scrim exists purely so it stays legible over the busy lower half of the
  dashboard.
- Uses the **still**, not `dashboard.mp4` — the clean capture is only 2.9 s against a 6.5 s scene,
  so a live dashboard would need a hold-on-last-frame. Worth revisiting if the scene gets shorter.
- No palette change from scene 04; the panel is already dark Ink × Lime, so the cut is seamless.

## Scene 06 — built · "The AI campaign creator" ⚠ provisional footage

`compositions/scene06.html` · 7.1 s · `out/scene06.mp4`

Three real beats cut back to back in one frame: typing the brief → AIZU asking which platforms
(all six chips, `SUGGESTED`) → drafting → the finished campaign. A persistent "AI campaign creator"
chip sits above.

**⚠ This is the new-campaign wizard, which the user said will change.** Re-shoot before final
render. The composition itself will survive — only the three files in `assets/capture/` need
replacing.

### Two contract rules this scene ran into

1. **Media may not be nested in a timed wrapper.** The first build wrapped each `<video>` in a
   `.clip` card and failed lint with `media_missing_data_start`. Timing has to live *on* the media
   element; wrappers stay untimed.
2. **A `.clip` element's visibility belongs to HyperFrames** — so the videos can't be faded in.
   The beat swaps are therefore hard cuts, which suits a product demo anyway. All motion (the slow
   push) is on the untimed `#stage` wrapper instead.

### Captured UI must be shown near 1:1

First cut placed the full 1920×1080 browser frame into a 1348×758 box; at ~70 % the panel's own
type was unreadable. The segments are now **cropped to the content column with ffmpeg**
(`crop=1440:810:376:<y>`) and displayed at their native 1440×810, so nothing is downscaled.

**Apply the same rule to every future product scene:** crop to the region that matters and show it
1:1 or larger — never shrink a whole browser frame to fit.

## Scene 07 — built · "Six platforms in, hot leads out"

`compositions/scene07.html` · 5.8 s · `out/scene07.mp4`

**The funnel payoff.** The six platform marks sit across the top with faint lime beams running down
from each; raw grey signal pours out of them and almost all of it dies on a lime filter line
(*relevance + buying intent*); three survive as scored lead cards — `@dana_t 0.91 reddit`,
`@marcus_ops 0.84 linkedin`, `@sarah_kb 0.78 x`.

This is the shape parked back at scene 02, inverted: same six platforms, same pour, but the output
is signal instead of noise. Scores and platform labels follow the real product's `ScorePill` /
`PlatformChip` conventions rather than invented ones.

Two revisions worth keeping in mind for similar scenes:

- **The beams were added to fill the middle.** Without them the frame was a top row, a bottom row,
  and 300 px of nothing between; the beams make the empty space read as a mechanism.
- **Beats must overlap.** First cut had signal dying at ~2.8 s and leads arriving at 3.55 s, leaving
  a visibly dead frame. The filter now lands at 2.2 s and leads at 3.15 s, while signal is still
  falling.

**Dark-mode `--faint` fails contrast too.** Ink × Lime's `#6b7080` is 3.5:1 on `--surface` — the
dark twin of the light palette's `--faint` problem. Uses `#8f96a8` here. Expect this in every dark
scene with small secondary text.

## Scene 08 — built · "The proof" ⚠ provisional footage

`compositions/scene08.html` · 6.9 s · `out/scene08.mp4`

Two real beats. First the **run drawer's live feed** — `Match: @devops_kate (score 0.93)`,
`Run started — campaign cmp-001 (x)`, `Match: sarah.chen (score 0.88)` — the filtering actually
happening. Then the **leads board** filling up, Won climbing 15 → 17 and win rate 73 % → 77 %.

- **The feed is upscaled 2× at cut time** (`crop=440:330:1470:195,scale=880:660:lanczos`). Its
  source text is tiny; everywhere else we crop 1:1, but here 1:1 would be unreadable. Upscaling a
  tight crop beats downscaling a wide one.
- **The leads board window dodges the lead drawer**, which is open in capture 06 from roughly
  6.5 s to ~11.9 s and slides into the right of the crop. The clean window is 12.15 s onward.
  Check for it if this is ever re-cut.

## Scene 09 — built · "It learns your ICP"

`compositions/scene09.html` · 6.0 s · `out/scene09.mp4`

Three period tiles — Week 1 / Week 4 / Week 12 — with the average match score counting up
0.60 → 0.78 → 0.91 and a lime trend drawing through them. Each tile carries two real lead handles
with score pills.

**Score colours follow the panel's own rule**, not invention: `--warn #fbbf24` for weak scores,
`--success #34d399` for strong, lime for the middle. Pills always keep ink text on a coloured fill —
an early version tinted both text and fill with the same token and hit 1:1 contrast.

**Dashed placeholder slots sit behind the tiles.** Without them the frame is two-thirds empty while
the first tile sits alone on the left; the tiles are opaque and land exactly on top.

## Scene 10 — built · "Centralized, visualized"

`compositions/scene10.html` · 6.0 s · `out/scene10.mp4`

Two beats matching the line's two verbs: the **dashboard** ("every channel in one place"), then the
**Reports** page ("and the answers already drawn") — leads-by-channel over time with the
six-platform legend, CPL trend at $1.13, spend-by-stage donut.

Both are stills, so duration is free; `dashboard.mp4` / `reports.mp4` are only ~2.8 s each.

**Crop bounds were measured, not eyeballed.** Two earlier attempts were wrong because I read them
off a coordinate-grid overlay by eye. The reliable way is to threshold against the page background
and take the bounding box, ignoring the sidebar:

```python
mask = (abs(img - [14,15,20]).sum(axis=2) > 18)
sub  = mask[:, 280:]            # skip the nav rail
# content x: 400 -> 1783,  y: 16 -> 1060   (identical on both pages)
```

Every panel page shares that content box, so `crop=1392:783:396:12` is the reusable full-width 16:9
crop and `crop=1400:788:392:8` its dashboard twin.

## Scene 11 — built · "Go to AIZU.UZ"

`compositions/scene11.html` · 6.6 s · `out/scene11.mp4`

Twelve scored customer cards arrive around the edges of the frame, clear out, and the AIZU lockup
lands in the space they leave — mark, arc drawing itself on, wordmark rising from its mask — then
"Get started at" and the **AIZU.UZ** lime pill.

The handles are the same ones used in scenes 07–09, so the people who arrive at the end are the
leads the product found earlier. Positions ring the frame deliberately: the centre stays clear for
the lockup that lands there.

## Parked — `compositions/idea-funnel-payoff.html`

The third scene-02 candidate: budget rains onto the six platforms, gets chewed through, and falls out
as grey noise with two survivors. Cut from scene 02 for being abstract where the pain must be
concrete — its idea now lives in scene 07. Kept on disk as the light-palette variant of that shape;
passes check, not in the cut.
