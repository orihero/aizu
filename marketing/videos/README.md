# Video projects

One project per subdirectory. Right now there is exactly one.

## `madison/` — the AIZU explainer

The current, actively developed explainer. Named `madison` after the reference video whose
structure it was built against — the content is entirely Aizu's.

| | |
| --- | --- |
| **Status** | All 11 scenes built · 70.4s · full cut at `out/aizu-full.mp4` |
| **Open** | Two scenes (06, 08) use provisional footage. See `BUILD-NOTES.md` for the live list. |
| **CTA** | `AIZU.UZ` |
| **Toolchain** | HyperFrames, pinned to `hyperframes@0.7.94` |

Read in this order: `SCRIPT.md` (narration and scene assignment) → `BUILD-NOTES.md`
(decisions, environment, what's blocked) → `ANALYSIS.md` (the reference-video breakdown the
structure came from).

```bash
cd marketing/videos/madison
npm run dev      # preview server
npm run check    # validate the composition
npm run render   # write out/
```

## Layout

```
madison/
  SCRIPT.md            narration copy + per-scene assignment and status
  BUILD-NOTES.md       running record of decisions and environment constraints
  ANALYSIS.md          breakdown of the reference video's structure
  CLAUDE.md AGENTS.md  agent onboarding
  index.html           composition entry point
  compositions/        the 11 scene files + shared components
  assets/              audio (generated VO), capture (real product recordings),
                       brand, fonts, images                          [TRACKED]
  vo/lines.json        narration source text fed to TTS
  tools/               use-scene.mjs, voice.py
  reference/           the reference video and our analysis of it
  meta.json hyperframes.json package.json

  out/                 rendered output                               [GITIGNORED]
  snapshots/           QA preview frames                             [GITIGNORED]
  .thumbnails/         preview-server cache                          [GITIGNORED]
  reference/clips,frames,sheets + the source mp4/mp3/webp/info.json  [GITIGNORED]
```

Everything marked GITIGNORED regenerates from what's tracked — rebuild with `npm run render`,
or re-download the reference video from the URL in `reference/madison.info.json`. Two things
deliberately stay tracked even though a tool produced them, because regenerating them costs
real money or manual work: `assets/audio/` (generated voiceover) and `assets/capture/` (screen
recordings of the actual product).

The root `.gitignore` applies these rules to `marketing/videos/*`, so a second project would be
covered automatically with no new rules.
