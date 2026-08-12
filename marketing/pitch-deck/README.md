# AIZU pitch deck

Three separate bodies of work live here, with **different audiences, lengths and
content**. Do not mix them.

## 1. The 9-slide pitch (current)

`dist/deck-pitch.html` — the short deck. Nine slides: name, problem, solution,
market, product, business model, competition, team, thanks. Dark register, because
the real dashboard capture on slide 05 is dark and sits on the ground rather than in
a card. Build it with `tools/embed_assets.py`, **not** `embed_fonts.py` — it inlines
the screenshot as well as the type.

It is exactly nine slides. Alternative framings for slides 02, 03 and 05 were carried
alongside them for a while, badged and excluded from the counter, so a choice could be
made by looking; the choice has been made and they are gone. Do not re-add variants
without deleting them again — a deck that ships with its own alternatives in it is a
deck nobody finished.

Every statistic on it survived an adversarial fact-check — each was re-fetched from
its source and dropped unless the page actually stated it. What that pass threw out
is recorded in `research/04-verified-facts.md`, which is the reason that file exists:
several plausible, widely-quoted figures are fabrications, and the record stops them
being re-added later.

## 2. The scholarship deck

`dist/deck-scholarship.html` — 18 slides + 2 appendices, written for the **IT Park
Uzbekistan scholarship committee**, in English. It does not ask for money; it asks
for a place in the programme.

It shares nothing with the investor decks below except the brand. Its content is
authored directly in `src/deck-scholarship.html` and is **not** governed by
`CONTENT-SPEC.md`.

Its governing rule is that every capability figure is **counted from this
repository**, not modelled — 6 platform engines, 1,661 tests, ~57,000 lines, 55 API
endpoints, 40 tables, 26 screens. Slide 14 states the pre-revenue position outright,
and slide 13 is the only slide carrying projections, each marked `°`.

## 3. The investor decks (earliest work, different audience)

Three complete investor decks, same story, three visual registers. They exist so a
style can be picked by looking rather than by describing — the content is identical
across all three, so the only variable is how it reads.

| Style | File | Register | Wins with |
| --- | --- | --- | --- |
| **Signal** | `dist/deck-signal.html` | Dark bento, product/data dense, matches the shipped aizu.uz landing | An operator or technical investor who needs to believe it is *built* |
| **Paper** | `dist/deck-paper.html` | Light Swiss editorial — big display type, hairline rules, whitespace | A deck read silently and forwarded to a partner; survives with no presenter |
| **Narrative** | `dist/deck-narrative.html` | Sparse cinematic — one idea per full-bleed slide, under 40 words at its densest | A live pitch, on stage, spoken over |

Open any file in a browser. `←` / `→` move, `s` shows speaker notes, `g` opens the
slide overview, `?` lists the keys, `Ctrl+P` exports a clean PDF.

## Layout

```
CONTENT-SPEC.md      binding content for the three INVESTOR decks only — 15 slides
                     + 2 appendix, final copy, every figure, every speaker note,
                     and the MOCK REGISTER. The scholarship deck does not use it.
research/            the three research briefs the spec was written from
src/*.html           deck sources, authored as document fragments (no <html>/<body>)
tools/embed_fonts.py the build: inlines the brand webfonts, emits both output forms
dist/*.html          standalone decks — open these
dist/artifact/*.html the same decks as fragments, for publishing as an Artifact
```

## Build

```bash
cd marketing/pitch-deck
python tools/embed_assets.py src/deck-pitch.html          # 9-slide pitch (+ screenshot)
python tools/build_pptx.py   dist/deck-pitch.html         # optional: dist/deck-pitch.pptx
python tools/embed_fonts.py  src/deck-scholarship.html
python tools/embed_fonts.py src/deck-signal.html src/deck-paper.html src/deck-narrative.html
```

Each source carries the literal marker `/*__AIZU_FONTS__*/` inside its first
`<style>`. The build replaces it with `@font-face` rules whose `src` is a base64
`data:` URI for Inter Tight and JetBrains Mono, taken from
`admin-panel/public/landing/fonts/`. Nothing else is transformed. The decks make
zero external requests by design — a published Artifact runs under a CSP that
blocks every external host, and a deck that loses its type on a plane is worse
than one that never had it.

`build_pptx.py` is a separate, optional step: it renders each slide in a headless
browser at 2x and lays the images into a 13.333×7.5in PowerPoint, carrying the speaker
notes across so presenter view still works. The slides arrive as images — the layouts
lean on CSS the format has no notion of, so text is not editable there. Edit `src/` and
rebuild. It needs `playwright` and `python-pptx`, which are deliberately not project
dependencies.

Two outputs come out of one source because the destinations want opposite things:
`dist/` is doctype-wrapped so a locally-opened file is not in quirks mode, and
`dist/artifact/` stays a fragment because the Artifact publisher supplies the
skeleton itself.

## The mock register (investor decks only)

AIZU is pre-traction. There is no real revenue, customer, funnel, team or fundraise
data in this repo, so the spec invents a coherent set — and marks every one of them.

- A fabricated figure always carries a degree marker: `62,000°`, `$750,000°`.
- Any slide containing one also carries a quiet `° illustrative placeholder` chip.
- Appendix B restates which numbers were modeled.

Anything sourced from the product is **real and unmarked**: the four pricing tiers
and their lead caps, the six shipped platforms, the feature set, the architecture.
The register in `CONTENT-SPEC.md` is the single source for every invented value, so
the three decks never disagree with each other. Before these decks are shown to
anyone, replace the register's values with real ones and drop the markers — that is
the only edit needed, and it is confined to one table plus its uses.

## Content rules that are load-bearing

- Six shipped platforms only: Instagram, LinkedIn, X, YouTube, Reddit, Telegram.
  Facebook, Pinterest, Quora, Threads and TikTok are PRD-stage and may appear only
  where they are explicitly labelled as not built.
- Pricing is quoted exactly as shipped on the landing page: Free $0 / 10 leads,
  Starter $24.99/mo ($249/yr) / 250 leads, Pro $149/mo ($1,490/yr) / 2,000 leads,
  Scale custom.
- Lime `#d9f24f` is signal, never decoration — at most two lime elements per slide
  unless the slide is itself a lime field.

## Editing

Edit `src/`, not `dist/` — `dist/` is generated and overwritten. To change what a
deck *says*, change `CONTENT-SPEC.md` first and then propagate to all three, so they
stay comparable. To change how one deck *looks*, edit only that source.

## Scholarship deck: what to change before submitting

Three placeholders, all on ink slides:

- Slide 01 speaker notes — `[NAME]`.
- Slide 18 (closing) — `[NAME] · [EMAIL]` and `[PHONE]`.
- Slide 13 — the two `°` figures ($120k export revenue, 9 engineering roles) are
  modelled from shipped pricing. Replace with the applicant's own numbers, or
  defend them as modelled; the footnote already says which.

Everything else is either counted from the repository or sourced from `docs/`, and
each such slide names its source in the footer.

## Presenting

`←` / `→` or space move, `s` toggles speaker notes (every slide has them), `g` opens
the overview grid, `f` fullscreen, `?` lists the keys, `Ctrl+P` exports a clean
landscape PDF at 13.333in × 7.5in. Clicking the left fifth of the screen goes back,
anywhere else advances.
