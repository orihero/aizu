# Marketing & video

Everything brand-, promo-, and video-related lives here — one place to look, instead of the
four separate locations this consolidated.

## What's here

| Path | What it is |
| --- | --- |
| [`videos/madison/`](videos/madison/) | **The AIZU explainer** — the current, actively developed video. 11 scenes, 70.4s, still silent. Start at [`videos/README.md`](videos/README.md). |
| [`website/`](website/) | A self-contained landing page draft. **Not the shipped aizu.uz home page** — see the caveat below. No build step — open `index.html`. |
| [`aizu-promo-video-treatment.md`](aizu-promo-video-treatment.md) | An earlier written treatment for a ~75s promo. **Historical — read the caveat below before treating it as binding.** |
| [`archive/`](archive/) | Superseded creative work, kept for reference. Not current direction. |

## `website/` is not the live aizu.uz page

The site actually shipped at aizu.uz's `/` is `admin-panel/public/index.html` (plus
`admin-panel/public/landing/`), built by `admin-panel`'s Vite build into
`admin-panel/dist/` and served by the engine bridge (`aizu panel`). That page is sourced
from the CoreShift design in `mockups/coreshift-landing/`, not from anything in this
directory. `website/` here is a separate, self-contained draft that was never wired into
any deploy path — nothing in CI/CD publishes it. Treat it as design/content reference
only until (or unless) it is deliberately adopted as the live page.

## Caveat on the promo treatment

`aizu-promo-video-treatment.md` describes a deliberate black box: it forbids naming or showing
the discovery mechanism, forbids depicting social platforms, and uses `aizu.uz` as the CTA.
The video actually being built departs from all three — its script says leads are found
"across six social media platforms" by "filtering out the accounts that reacted to posts
similar to your product or service," and its CTA is `AIZU.UZ`.

So the treatment documents an earlier positioning, not the shipping one. Its craft sections
(Ink × Lime palette discipline, motion physics, the Ping mark) are still good reference; its
content guardrails and CTA are not current. Worth either updating or archiving it.

## Conventions

- Rendered output, caches, and third-party reference footage are never tracked in git — see
  the root `.gitignore`. Everything there regenerates from tracked source.
- Generated voiceover and real product screen captures **are** tracked, because regenerating
  them costs money or manual recording time.
- Anything under `archive/` is explicitly not current creative direction. Read its own README
  before reusing assets from it.
