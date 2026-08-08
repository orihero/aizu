# AIZU — Promotional Explainer Video Treatment

**Format:** 16:9 landscape · ~75s · 30fps (2,250 frames) · voiceover + kinetic typography
**Audience:** online businesses that sell direct — **B2C and B2B alike**: e-commerce & DTC brands, agencies, service providers, coaches/creators, and B2B sellers. Anyone who needs more of the *right* customers. (Not SaaS-founder-specific.)
**Positioning:** outcome-first. AIZU is a black box: you tell it what you sell and who you serve, and qualified, ready-to-buy customers appear. We never show or imply *how* it finds them.
**Build target:** Remotion (programmatic React video).

> ### ⚠️ Hard content guardrails (non-negotiable)
> The discovery mechanism must never be shown or implied. **Do NOT** depict or reference: social platforms or their logos (Instagram/YouTube/Reddit/X/Telegram/LinkedIn), scraping, monitoring, reading comments/posts/captions, on-screen-text extraction, feed-walking, scoring streams, or verbatim public comments with a visible source.
> **Allowed source framing:** abstract "public signals" only — people *already signaling intent in public* / *already asking who can help*. Never say where, never show how.
> The gap between input ("describe your product") and output ("qualified prospects") is rendered as a single glowing **lime pulse** — the honest black box.

**Concept (logline):** In a world of noise, one clear lime signal surfaces the customer worth reaching — and AIZU turns that into your time back, a short list of genuinely ready-to-buy customers, and a pipeline that keeps filling itself. Never *how*; only *what you get*.

**Emotional spine:** the brand-native "signal to act" metaphor carries three value pillars in sequence — **Time & Focus → Precision & Quality → Growth & Pipeline**.

---

## Part 1 — The Script (VO + on-screen text)

| # | Time | Scene | Voiceover | On-screen text |
|---|------|-------|-----------|----------------|
| 1 | 0–10s | Cold open — the signal | "Right now, someone out there is looking for exactly what you sell — and asking who can help." | **They're already asking.** |
| 2 | 10–18s | The miss | "By the time you'd ever find them, they've moved on — and you've spent hours you don't have." | **Gone before you find them.** |
| 3 | 18–28s | Name payoff — the turn to lime | "AIZU means a signal to act. In all the noise online, it surfaces the one that matters — the customer worth reaching, right now." | **AIZU · 合図 — a signal to act.** |
| 4 | 28–40s | Pillar 1 · Time & Focus | "Just tell AIZU what you sell and who you serve. It does the finding — so you stop chasing leads and start winning customers." | **Stop chasing. Start winning.** |
| 5 | 40–52s | Pillar 2 · Precision & Quality | "Not more leads — the right ones. A short list of people ready to buy, not a hundred dead ends." | **5 qualified > 100 junk.** |
| 6 | 52–63s | Pillar 3 · Growth & Pipeline | "And it keeps going — a steady stream of customers ready to buy, filling your pipeline week after week." | **A pipeline that fills itself.** |
| 7 | 63–70s | Trust | "It works quietly and respectfully in the background, and your data always stays yours." | **Quiet. Respectful. Yours.** |
| 8 | 70–75s | CTA + bookend | "AIZU finds the signal. You win the customer. Start today at aizu.uz." | **AIZU finds the signal. You win the customer. — aizu.uz** |

**CTA:** AIZU finds the signal. You win the customer. Start today at **aizu.uz**.

**Audience fit:** deliberately vertical-agnostic — "what you sell and who you serve", "people ready to buy", and "customers" all read cleanly for a DTC store, an agency, a service business, or a B2B seller. No "product/founder/closing/sales-ops" language that would narrow it to SaaS.

**Word-count sanity:** ~150 words of VO across 75s ≈ 2.0 wps — comfortably sayable with calm, paused delivery, leaving room for the on-screen text to land after each clause.

---

## Part 2 — Visual System (global)

**Typography** — One geometric-grotesque family (Inter Tight / General Sans, self-hosted via `@remotion/fonts`, never a CDN). Three roles:
- **Kinetic headline** 72–120px / 600 / tracking −1.5% — the on-screen TEXT lines.
- **UI/label** 15–20px / 500 — the minimal product surface (input field, prospect cards, trust badge).
- **Mono accent** (JetBrains Mono) — sparingly, for the 合図 romaji and any numeric beat (the "5 / 100" count). No verbatim user text anywhere.

Never more than two type sizes on screen at once. Text is lime-on-ink or muted grey `#6b6b73` on ink; pure white only inside faked light-mode UI thumbnails.

**Color — "Ink × Lime", rationed as meaning:**
- Ink `#16161a` is the ground in ~90% of frames and the color of all structure.
- Electric lime `#d9f24f` is **signal only** — it never decorates; it always means "this is worth acting on."
- Grey scale for noise: `#2a2a30 · #43434c · #6b6b73 · #9a9aa2`.
- **Enforced arc:** scenes 1–2 near-monochrome (lime appears *only* on the single signal dot/arc in scene 1, then withheld through the miss). **Scene 3 is the inflection** — grey drains, lime arrives as relief. Scenes 4–8 use lime strictly for the qualified prospects, the black-box pulse, the ping, and the CTA. Lime is always the brightest thing in frame; never a second saturated hue. Faint lime at 8–14% opacity allowed for glows/pulses/hairlines.
- **Semantic mapping, held end to end:** grey = noise / unqualified / lost; lime = the signal / qualified / worth acting on.

**Motion principles** — Calm and precise, never flashy. Everything animates on `useCurrentFrame()` — no CSS transitions or Tailwind animation classes. Two vocabularies:
- **Structure** moves with `interpolate` + `Easing.out(cubic)` / `Easing.inOut(quad)` — slides, blurs, drains, the noise flood.
- **Signal** moments (ping forming, prospects surfacing, the black-box pulse, the CTA) use `spring({ damping: 200 })` for smooth reveals, `damping: 26` for the one or two UI "clicks."
- Bounce is essentially banned (max one subtle overshoot on the "describe" input submit). The ping arc **always** draws via SVG `stroke-dashoffset` (spring-driven pathLength 0→1), never fades in. Noise moves fast + blurs (chaos); signal moves slow + settles (intention). Depth via blur + scale, never colored drop shadows.

**Pacing** — 75s @ 30fps = 2,250 frames. ~9.4s/scene; longer holds on the emotional beats (scene 3 turn, scene 5 precision reveal), tight cut through the miss (scene 2). On-screen TEXT lands ~0.5s after the VO clause that earns it, holds still, then clears before the transition. ~1 primary motion event per 1.5–2s. Cold open and final scene share tempo + composition to bookend.

**Music & sound — sound design *is* the brand:**
- One continuous minimal ambient bed ~90 BPM, low, no drop/build-to-hype. Thins under VO; opens up at the scene-3 turn and the scene-8 resolve.
- The signature **"ping"**: one short clean sine bell (~880Hz, fast decay) fires *only* on true signal events — scene-1 arc completing, scene-3 ping draw, the black-box pulse landing (scene 4), each qualified prospect surfacing (scene 5), and the final CTA (scene 8).
- Scene 2 gets a low grey rumble/wash that ducks away on the scene-3 turn. Final ping rings out into silence over the CTA.

---

## Part 3 — Shot-by-shot storyboard

### Scene 1 · 0–10s (frames 0–300) · "They're already asking."
- **Layout:** Full-bleed ink. Single dot at ~46% height (optical center); an **abstract, anonymized signal** (a soft pulse/waveform or a chrome-less thought bubble — no platform, no avatar, no source) drifts near it; headline low bottom-left. Mostly empty ink.
- **Elements:** (1) 14px grey signal dot that breathes; (2) an abstract "someone is asking" mark — a minimal waveform or plain rounded bubble with a short generic intent phrase like *"who can help with this?"* set in kinetic type, **deliberately not a social comment card**; (3) the lime cue **arc** breaking off the dot toward the signal — the ping forming live; (4) headline.
- **Animation:** dot fades up + breathes (sine, synced to sub-pulse) → the abstract signal fades/rises in → dot flushes grey→lime, then the arc **draws on** via `stroke-dashoffset` spring (signature ping tone fires) → headline reveals word-by-word via clip mask. The lime dot+arc is the only bright thing.
- **Remotion:** `<Ping/>` (SVG `<circle>` + `<path pathLength>`, `strokeDashoffset = interpolate(spring({damping:200}),[0,1],[len,0])`); dot pulse via `Math.sin(frame/fps*π*1.4)`; `<SignalBubble/>` (abstract, chrome-less); `<KineticLine/>`; dot color via `interpolateColors`.
- **Out:** lime dot detaches and drifts toward center; carried as the seed of scene 2's flood (8-frame `TransitionSeries` fade, position preserved for continuity).

### Scene 2 · 10–18s (300–540) · "Gone before you find them."
- **Layout:** Starts on scene-1 comp, then grey noise floods in from all edges and buries the lime signal; a subtle clock/hours element drains in a corner. Headline punches in centered as the pile peaks.
- **Elements:** ~150 muted grey abstract cards/marks (generic, **no platform chrome, no readable content**), procedurally placed + slightly blurred; the scene-1 signal gets buried + desaturated (lime→grey); a faint grey "hours" counter or clock arc that visibly loses time.
- **Animation:** cards fly in staggered (2–3 frame offsets) with resolving blur; the lime signal saturates to 0 and sinks (z-order + y drift); the hours element ticks down / a clock sweep accelerates — time slipping. Headline fades in centered ~13s. Grey rumble builds.
- **Remotion:** array of ~150 descriptors from a **seeded PRNG** (stable renders) → `<NoiseCard/>` (abstract variant only), each in `<Sequence from={base+i*stagger} premountFor>`; buried signal = scene-1 mark with `filter:saturate()` 1→0; `<HoursDrain/>` small clock/counter via `interpolate`.
- **Out:** field blurs + darkens (`brightness→0.4`); as scene-3 grey drain begins the whole mass recedes to reveal empty ink; slow 22-frame fade.

### Scene 3 · 18–28s (540–840) · "AIZU · 合図 — a signal to act." — THE TURN
- **Layout:** Empty ink → dead-center hero brand lockup (ping + AIZU + 合図 + tagline). Calmest frame in the film, lots of breathing room.
- **Elements:** hero ping (~120px, dot + one cue arc — explicitly **NOT** a radar sweep); "AIZU" wordmark in lime; 合図 kanji in lime with generous tracking; tagline "a signal to act".
- **Animation:** **palette event** — a lingering grey overlay's opacity `1→0` `Easing.inOut(quad)` over 25 frames (color returning to the world; music opens). Then ping **draws** (dot spring scale 0→1 damping:200, arc pathLength spring; signature ping tone). Wordmark reveals via left-to-right clip mask ~21s; 合図 fades+rises 8px; tagline last (~24s). Hold ~2.5s — let it land. This is the emotional inflection; nothing rushes.
- **Remotion:** reuse `<Ping size={120}/>`; grey drain = `<AbsoluteFill background:#43434c, mixBlendMode:saturation, opacity:drain>`; `<KineticWordmark/>` clip-path inset; `interpolateColors` for tagline grey→lime.
- **Out:** hero ping shrinks + travels up-left to become the persistent workspace logo of scene 4 (ping as connective tissue); 14-frame fade.

### Scene 4 · 28–40s (840–1200) · Pillar 1 · "Stop chasing. Start winning."
- **Layout:** Left third: a clean "What do you sell? Who do you serve?" input on ink (the only product surface shown). Center: the **black-box lime pulse**. Right third: a calm short-list of customers begins to appear. Headline bottom-left.
- **Elements:** (1) a minimal input field with the small ping logo (carried from scene 3) and a short typed line naming what the business sells + who it serves (deliberately generic so it reads for any vertical — e.g. *"handmade candles · gift shoppers"* or *"bookkeeping · small businesses"*); (2) a single lime **pulse/ping** that travels from the input into empty space and blooms — this is the entire "how," honestly opaque; (3) 2–3 abstract ready-to-buy customer cards fading up on the right (avatar silhouette + a lime "qualified" indicator — **no quote, no source**); (4) a subtle "hours returned" grace note (the scene-2 clock, now still/reversed).
- **Animation:** the what-you-sell line typewrites in the input; on submit the input gives one small spring dip (the allowed click); a lime pulse launches, arcs across the gap, and **blooms** (radial lime glow, `spring damping:200`, ping tone) — then customer cards surface on the far side (staggered spring). Emphasis: input → *(pulse)* → output, with nothing legible in between. Headline ~29s.
- **Remotion:** `<InputField text ping/>` with `<Typewriter/>`; black box = `<SignalPulse/>` (a lime `<circle>` whose position `interpolate`s across frame + radial-gradient bloom via scale/opacity spring); `<ProspectCard variant='qualified'/>` staggered `<Sequence>`; reuse `<HoursDrain reversed/>`.
- **Out:** the surfaced prospect cards slide toward center and multiply → sets up the pile/collapse of scene 5; 12-frame fade.

### Scene 5 · 40–52s (1200–1560) · Pillar 2 · "5 qualified > 100 junk."
- **Layout:** Frame fills with a churning pile of grey junk cards; then it collapses away, leaving a few lime qualified-prospect cards standing clean in center. Headline top-center / lower-left. The signature precision beat.
- **Elements:** ~100 grey junk cards (abstract, unreadable) vs. a handful (≈5) of lime qualified-prospect cards; a large kinetic count that resolves to **5 > 100**; each qualified card = avatar silhouette + a lime "qualified / ready" fit indicator + a short generic intent tag (e.g. *"intent: evaluating options"*) — **no verbatim comment, no source**.
- **Animation:** junk pile swells (fast, blurred, chaotic) then **collapses/falls away** at once (`Easing.in(cubic)`, opacity + y-drop, ~16f); the ~5 lime cards remain and settle (spring damping:200, ping per card, staggered). The count animates: a "100" of grey junk drains while a lime "5" holds — the comparison lands as a single beat. Hold the clean five ~2s.
- **Remotion:** reuse `<NoiseCard/>` for the junk (grey); `<ProspectCard variant='qualified'/>` for the survivors; collapse via a shared `interpolate` on the junk group's opacity/translateY; the count = `<KineticCount from=100 to=5/>` with `Math.round(interpolate())`; `<Chip variant='fit'/>` on each card.
- **Out:** the five qualified cards line up and begin flowing in one direction → becomes the pipeline stream of scene 6; 12-frame fade.

### Scene 6 · 52–63s (1560–1890) · Pillar 3 · "A pipeline that fills itself."
- **Layout:** A calm horizontal (or gently ascending) pipeline lane across the frame; lime qualified-prospect cards flow steadily into it and stack; a simple, quiet up-and-to-the-right sense. Headline lower-left.
- **Elements:** a minimal pipeline/lane rail; a steady procession of lime qualified cards entering and docking; a small unobtrusive tally that ticks up; faint "week after week" rhythm markers (soft grey gridlines the cards pass). Aspirational, always-on — never frantic.
- **Animation:** cards enter at a steady cadence (one every ~10–14 frames), each with a small ping and a settle into the lane; the tally counts up smoothly; a gentle overall upward drift/scale conveys growth without a spiky "hockey stick." The rhythm is metronomic and calm — pipeline as a dependable heartbeat, echoing the scene-1 dot pulse.
- **Remotion:** `<Pipeline/>` lane container; `<ProspectCard/>` instances mapped to timed `<Sequence>`s entering from one edge, docking via `interpolate` translate; `<KineticCount/>` tally; subtle global `translateY`/`scale` drift on the lane.
- **Out:** the pipeline settles and softens; everything eases toward center to make room for the trust beat + close; 14-frame fade.

### Scene 7 · 63–70s (1890–2100) · Trust · "Quiet. Respectful. Yours."
- **Layout:** Calm, near-empty ink. A small centered trust lockup — three short assurances with tiny lime checks — beneath a single softly pulsing ping. Headline centered or lower-left.
- **Elements:** three quiet assurances: **"works quietly in the background" · "respectful, never pushy" · "your data stays yours"**, each with a small lime check; one gently breathing ping above; deliberately no product UI, no mechanism.
- **Animation:** the three assurances fade up in sequence (~10f apart), each lime check drawing on via `strokeDashoffset` spring; the ping breathes once. Stillness dominates — this beat is about reassurance, so it holds calm.
- **Remotion:** `<TrustBadge items/>` mapped to staggered `<Sequence>`; checks = SVG `<path pathLength>` spring; reuse `<Ping size={40}/>` breathing.
- **Out:** assurances fade; the ping travels to center and scales up into the final brand lockup; 12-frame fade.

### Scene 8 · 70–75s (2100–2250) · CTA + bookend · "AIZU finds the signal. You win the customer."
- **Layout:** Centered brand lockup (ping + AIZU + 合図), the CTA line in lime beneath — same tempo and composition as scene 3, bookending scene 1.
- **Elements:** hero ping (pulses once); "AIZU" + 合図; CTA line **"AIZU finds the signal. You win the customer. — aizu.uz"** in lime.
- **Animation:** brand lockup resolves at center (ping springs, wordmark clip-reveal — reused from scene 3 for exact bookend); the final ping pulses **once** and rings into silence; the CTA line fades in last and holds to frame 2250. Music resolves and drops to the single ping.
- **Remotion:** reuse `<Ping/>` + `<KineticWordmark/>` from scene 3; CTA = `<KineticLine/>` lime; single final pulse = one sine cycle of scale on the ping, then static.
- **Out:** hold static lockup + CTA to frame 2250, then a clean 10-frame fade to full ink `#16161a`. The calm hold *is* the ending — no outro animation.

---

## Part 4 — Reusable Remotion components

- **`<Ping size tone>`** — signature brand mark: SVG `<circle>` + single cue `<path>` arc with `pathLength`; draw-on via `strokeDashoffset` `spring(damping:200)`; hero(120)/nav(24)/small(40) scales, grey vs lime tone. Connective tissue across scenes 1,3,4,7,8.
- **`<KineticLine text>`** — the on-screen headline: per-word reveal via clip-mask/spring; every TEXT line, all 8 scenes.
- **`<KineticWordmark>`** — "AIZU" + 合図 lockup, left-to-right clip reveal; shared by scenes 3 & 8 so the bookend is pixel-identical.
- **`<Typewriter text startFrame>`** — string-slice typewriter + blinking caret; the scene-1 abstract phrase and the scene-4 product-description input.
- **`<SignalBubble>`** — abstract, chrome-less "someone is asking" mark (waveform or plain bubble); **carries no platform/source/avatar**; scene 1.
- **`<NoiseCard>`** — faked *abstract* grey cards (no platform chrome, unreadable); one instance carries the lime→grey burial arc (scenes 1→2); ~150 procedurally instanced for the flood (scene 2) and ~100 for the junk pile (scene 5).
- **`<SignalPulse>`** — the black-box lime pulse: a `<circle>` that travels across the frame via `interpolate` and blooms (radial-gradient scale/opacity spring); the honest "how"; scene 4.
- **`<InputField text ping>`** — minimal "What do you sell? Who do you serve?" surface with the small ping logo; the only product chrome shown; scene 4.
- **`<ProspectCard variant='qualified'|'fit'>`** — abstract ready-to-buy customer card: avatar silhouette + lime "qualified/ready" indicator + generic intent tag; **never a quote or source**; scenes 4,5,6.
- **`<Chip variant='fit'>`** — small lime fit/intent pill on prospect cards; scenes 4,5.
- **`<KineticCount from to>`** — animated number for the "100 → 5" precision beat and the scene-6 pipeline tally.
- **`<Pipeline>`** — calm lane container that qualified cards flow into and dock; steady, ascending; scene 6.
- **`<HoursDrain reversed?>`** — small clock/hours element that loses time (scene 2) and is stilled/reversed as the "hours returned" grace note (scene 4).
- **`<TrustBadge items>`** — three short assurances with lime draw-on checks; scene 7.
- **`<SceneShell bg>`** — `AbsoluteFill` ink ground + optional grey-drain overlay + safe-zone padding; wraps every scene for consistent margins and the scene-3 palette drain.
- **`PRNG(seed)`** — deterministic seeded pseudo-random for stable placement of the abstract noise cards (renders must be reproducible).

---

## Part 5 — Asset list

- **Fonts (self-hosted via `@remotion/fonts staticFile`, no CDN):** Inter Tight / General Sans 500/600; JetBrains Mono 400/500 for the 合図 romaji and numeric beats. Ensure 合図 kanji coverage (bundle a Noto Sans JP subset if the primary face lacks CJK).
- **Ping icon** — hand-authored master SVG (dot + one cue arc), single `<path>` arc so `pathLength` draw works; matches `public/favicon.svg`.
- ~~Platform glyphs~~ — **removed** (no platform depiction).
- ~~Video-frame / burned-in-text mock~~ — **removed** (no mechanism depiction).
- **Audio:** one ~78s ambient/minimal bed ~90 BPM (calm, no drop; quiet VO-bed section + slight opening at the scene-3 turn); the signature ping SFX (short sine bell ~880Hz fast decay) + a couple of pitched variants for the scene-5/6 prospect surfacing; a low grey noise-wash/rumble for scene 2 that ducks on the turn.
- **Voiceover:** calm, warm, mid-paced recording of the locked script (~75s, natural pauses); one WAV + per-scene cue timings so TEXT lands ~0.5s after each VO clause.
- **Faked UI content strings (on-brand, vertical-agnostic):** the scene-1 abstract intent phrase, the scene-4 what-you-sell / who-you-serve line (rotate 2–3 generic examples spanning B2C and B2B so no single vertical dominates), the scene-5 generic intent tag — all abstract, none quoting a real public comment or naming a source.
- **Color tokens module** mirroring the app: ink `#16161a`, lime `#d9f24f`, grey scale (`#2a2a30 / #43434c / #6b6b73 / #9a9aa2`), lime-glow 10–14% — a shared TS constants file every component references.

---

## Suggested build order (Remotion)

1. **Foundations:** `<SceneShell>`, color tokens module, self-hosted fonts, `PRNG`, and the master `<Ping>` — everything else depends on these.
2. **The bookend (scenes 3 + 8 lockup):** `<KineticWordmark>` + `<Ping>` hero — locks brand look early and is reused verbatim at the end.
3. **The value spine (scenes 4, 5, 6):** `<InputField>`, `<SignalPulse>`, `<ProspectCard>`, `<KineticCount>`, `<Pipeline>` — the outcome story; this is where the pitch lives.
4. **The problem beats (1, 2):** `<SignalBubble>`, `<NoiseCard>` flood, `<HoursDrain>` — the seeded-noise system.
5. **Trust (7):** `<TrustBadge>`.
6. **Timing polish + audio sync:** drop the VO WAV, align each TEXT `<Sequence>` to land ~0.5s after its clause, place ping SFX on the signal events, master.
```

## Content-safety pass (before render)
- [ ] No platform names or logos anywhere on screen or in VO.
- [ ] No depiction of reading/monitoring/scraping/comments/feeds.
- [ ] No verbatim public comment with a visible source; all "signals" are abstract.
- [ ] The input→output gap is only ever a lime pulse (no mechanism between).
- [ ] Source framing stays at "already asking / signaling in public" — never where, never how.
