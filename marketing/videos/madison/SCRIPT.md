# AIZU — Narration Script

Full VO copy as supplied by the user (2026-08-06), split into the scene slots it maps onto.
Scene boundaries follow `reference/scenes.json`, which mirrors the reference video's structure —
they will be retimed per scene as we build, since AIZU's story is shorter than Madison's.

**Status:** **neither the copy nor the scene list is locked** — the user has confirmed both are open
to change, so lines get reworded and scenes get resequenced whenever the visuals are stronger for it.
The verbatim source is preserved below as the origin; the table is what we are actually building.

---

## Verbatim source

> In todays world best source to find clients and leads is the internet; So having strong online
> precense is crucial; But running ads and managing them is very expensive and inefficent. And hiring
> someone to do it for you is another headache; Untill now; Meet AIZU your new sales expert; Find
> leads; Manage them and uncover actionable insights; For as low as $9/months
>
> It is simple; Just create a campaign with our AI powered campaign creator and AIZU finds you
> relevant hot leads across 6 social media platforms by filtiring out the social accounts that
> reacted to the posts that is similar to your product or service; Over time AIZU learns your ICP and
> improves the lead quality; AIZU also uncovers actionable insights by centralizing and visualizing
> key analitics; Ready to acquire new customers without headaches and crazy cost go to AIZU.UZ

---

## Scene assignment

| Scene | File | Status | Line |
| --- | --- | --- | --- |
| **01** — Where leads live | `compositions/scene01.html` | **built** · 9.5s | In today's world, the best source to find clients and leads is the internet. So having a strong online presence is crucial. |
| **02** — Ads cost, and mostly miss | `compositions/scene02.html` | **built** · 5.5s | So you run ads. They're expensive — and most of what comes back isn't worth calling. |
| **03** — Doing it yourself | `compositions/scene03.html` | **built** · 5.5s | Managing them yourself is a full-time job. Hiring someone to do it is another headache. |
| **04** — Logo reveal | `compositions/scene04.html` | **built** · 5.0s | Until now. Meet AIZU — your new sales expert. |
| **05** — Product pillars + price | `compositions/scene05.html` | **built** · 6.5s | Find leads, manage them, and uncover actionable insights — for as low as $9 a month. |
| **06** — Campaign creator | `compositions/scene06.html` | **built** · 7.1s · *provisional footage* | It's simple. Just create a campaign with our AI-powered campaign creator, |
| **07** — Six platforms in, hot leads out | `compositions/scene07.html` | **built** · 5.8s | …and AIZU finds you relevant hot leads across six social media platforms — |
| **08** — The proof | `compositions/scene08.html` | **built** · 6.9s · *provisional footage* | — by filtering out the accounts that reacted to posts similar to your product or service. |
| **09** — It learns your ICP | `compositions/scene09.html` | **built** · 6.0s | Over time AIZU learns your ICP and improves the lead quality. |
| **10** — Centralized, visualized | `compositions/scene10.html` | **built** · 6.0s | AIZU also uncovers actionable insights by centralizing and visualizing key analytics. |
| **11** — Go to AIZU.UZ | `compositions/scene11.html` | **built** · 6.6s | Ready to acquire new customers without headaches and crazy cost? Go to AIZU.UZ |

**All 11 scenes built — 70.4 s.** Full cut: `out/aizu-full.mp4`. Still silent.

### Changes from the verbatim source, and why

- **Scenes 02 and 03 were resequenced.** The source had one line for cost and one for hiring. Built
  as two scenes they split better along *expensive* vs *exhausting*: scene 02 is a campaign burning
  budget, scene 03 is one person buried in the work. The hiring clause moved into 03, where the pile
  of work makes "hiring someone is another headache" land instead of needing its own beat.
- **Scene 02's line gained "most of what comes back isn't worth calling."** Cost alone is only half
  the problem, and lead *quality* is what AIZU actually fixes — planting it here makes the later
  ICP-learning scene a payoff rather than a new idea. The card's three-step drop
  ($4,820 spent → 38 replies → 3 worth calling) exists to carry that clause.
- **Scene 04 flips to the dark palette.** Ink × Lime promotes lime from accent to primary in dark
  mode, so the reveal is a register change, not just a new scene: scenes 01–03 are the light
  problem, 04 onward is the lime solution. Everything after this point should stay dark unless
  there's a reason to go back.
- **The longest line was split across scenes 07 and 08.** "Finds you hot leads across six
  platforms *by filtering out the accounts that reacted to posts like yours*" is ~10 s of VO and two
  distinct ideas — the claim, then the how. Scene 07 is the designed mechanic; scene 08 is the same
  thing proven in the real product.
- **The funnel metaphor was cut from scene 02 and parked** as `idea-funnel-payoff.html`. It is
  abstract where the pain needs to be concrete, and it never says "expensive." It belongs in
  scenes 07–10, where the same shape — budget into six platforms — resolves into *hot leads* instead
  of grey noise. Same visual, inverted outcome.

### Notes

- **Spelling normalised** for on-screen and VO use: *precense → presence*, *inefficent → inefficient*,
  *Untill → Until*, *filtiring → filtering*, *analitics → analytics*, *$9/months → $9 a month*.
  The verbatim block above is preserved unedited; the table is the version to record.
- **The reference video is 113 s; this script is shorter** — roughly 75–85 s at natural VO pace.
  Scenes will compress accordingly rather than being padded.
- **"6 social media platforms"** is stated in VO, so the six marks must stay consistent
  everywhere they appear: **Instagram · Telegram · YouTube · LinkedIn · Reddit · X**.
- No voiceover has been recorded or generated yet. Local TTS is unavailable (see BUILD-NOTES.md).

---

## Brand

**Ink × Lime** — the AIZU admin panel's design system (light palette used for the video).

| Role | Value |
| --- | --- |
| Background | `#eef0f3` |
| Surface / Surface-2 | `#ffffff` / `#f4f5f9` |
| Border | `#e6e8ee` |
| Text / muted / faint | `#16161a` / `#5b5e6b` / `#9b9eab` |
| Brand (primary fill) | `#16161a` ink, deep `#34343d` |
| Accent | `#d9f24f` lime (text on it: `#16161a`) |
| On-brand text | `#ffffff` |
| Success / Warn / Danger / Info | `#16a34a` / `#b45309` / `#dc2626` / `#0284c7` |
| Cloud | `#7c3aed` |
| Type | **Sora** headings · **Inter** body |
| Radii | 23px tiles · 17px cards |

Tokens live as CSS variables at the top of `index.html` so retheming is a single-block edit.
