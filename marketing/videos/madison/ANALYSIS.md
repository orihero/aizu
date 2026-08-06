# Reference Analysis — "Explainer Video for Madison: AI Powered Digital Marketing Tool"

Source: https://www.youtube.com/watch?v=mtPqxJBMXCQ
Studio: *What a Story — The Video Partner for SaaS & AI* · Published 2024-02-14

| Property | Value |
| --- | --- |
| Duration | 113.08 s |
| Resolution | 1920 × 1080 |
| Frame rate | 30 fps (CFR) |
| Audio | AAC 44.1 kHz stereo — single VO track + light music bed |
| Hard cuts detected | 9 (`ffmpeg` scene ≥ 0.2) — the piece is **continuously animated**, not cut-driven |

Local copies live in `reference/` (see [File map](#file-map)).

---

## 1. What kind of video this is

A **faceless SaaS product explainer**: problem → agitation → product reveal → four feature demos → CTA.
Almost every shot is a **vector/UI mockup animated in 2.5D** — no live footage, no talking head, no
3D. Motion vocabulary is narrow and repeatable:

- **Card float-in** — UI panels drift in from off-axis with slight scale + parallax, never a hard cut.
- **Orbiting satellites** — platform logos pop in around a stack of screens with staggered spring scale.
- **Cursor puppetry** — a fake cursor moves, clicks, and the UI reacts. This carries most feature demos.
- **Typewriter reveals** — headlines, subtitles, chat inputs, and AI-generated copy all type in.
- **Counter roll-ups** — follower counts, invoice totals, and analytics numbers tick upward.
- **Purple blob wipes** — the brand purple enters as large circles sliding in from the corners; used
  as the only "hard" transition device (at ~17 s and ~23 s).

Because there are so few real cuts, **rebuilding this scene-by-scene in HyperFrames is a good fit** —
each scene is an independently timed clip, and the transitions between them are cross-dissolves or
blob wipes rather than seams that must match frame-exactly.

---

## 2. Design system (extracted from frames)

### Color

| Role | Hex | Notes |
| --- | --- | --- |
| Brand purple | `#4F45E2` | Wordmark, buttons, blobs, full-bleed backgrounds. Sampled at 19 s / 26 s / 78 s. |
| Purple (shaded variant) | `#4F42E0` | Same fill under a slightly different grade at 78 s — treat as one token. |
| Page background | `#F4F4F4` | Light-mode scenes 1–3, 8, 15–17. |
| Page background (alt) | `#FBFBFB` | End card — very slightly warmer. |
| Card surface | `#FFFFFF` | All UI panels; soft large-radius shadow. |
| Positive / growth | green chips | Dashboard delta pills, NPS "Promoters" bar. |
| Warning / passive | amber | NPS "Passives", stacked bars. |
| Negative | red | NPS "Detractors", invoice **Balance Due**, Delete button. |
| Star rating | gold/amber | Review widgets. |

Full-bleed **purple** backgrounds are used deliberately, and only twice: the product-pillars beat
(23.8–29 s) and the campaign-editor beat (75–80 s). Everything else is off-white. That contrast is
the piece's main rhythmic device.

### Type

- **Wordmark / headlines** — geometric humanist sans, generous letter spacing, single-weight.
  Closest free matches: **Poppins**, **Plus Jakarta Sans**, or **Baloo 2** (HyperFrames already
  caches Baloo 2 locally). Needs a call before scene 4 is built.
- **UI text inside mockups** — a neutral UI sans; **Inter** is a faithful stand-in and is already
  the scaffold default.
- Body copy is small and low-contrast — it reads as texture, not as something the viewer parses.
  This matters: we can substitute our own filler copy freely in the dense screens.

### Layout

- Safe area is generous — key subject always inside the middle ~70 %.
- UI mockups are rendered at less than 100 % scale and float on the background with a large soft
  drop shadow; they are never edge-to-edge.
- Corner radius on cards is large (~12–16 px at 1920 scale).

---

## 3. Narration script (as delivered)

Timings are VO start times from the YouTube caption track. Word counts are what our custom text
needs to roughly match if we keep the original pacing and music bed.

| # | In | Line |
| --- | --- | --- |
| 1 | 0:00.4 | In today's world, the internet is where consumers discover local businesses like yours. So having a strong online presence is crucial. |
| 2 | 0:09.4 | But building one is time-consuming and complex — and hiring someone to do it for you is expensive. |
| 3 | 0:16.4 | Until now. Meet Madison — |
| 4 | 0:20.3 | your new AI-powered digital marketing specialist. Improving SEO, managing social media, and uncovering actionable insights — for as low as $99 a month. |
| 5 | 0:32.1 | It's simple. Just connect your business's accounts, |
| 6 | 0:37.0 | and Madison helps you show up higher on Google by SEO-optimizing your Google Business Profile, |
| 7 | 0:45.0 | crafting tailored review responses across all platforms. Simply review, modify if needed, and approve. |
| 8 | 0:53.1 | Over time, Madison learns your brand's voice. |
| 9 | 0:58.7 | And by increasing your ratings and reviews through simplified review requests — only directing the positive ones to Google to be posted publicly. |
| 10 | 1:06.9 | Madison also expands your social media reach by automatically creating, targeting, and scheduling engaging campaigns. Again — simply review, modify if needed, and approve. |
| 11 | 1:18.2 | And if you want to announce a promotion or new offering, or just say hi — simply tell Madison. |
| 12 | 1:27.0 | Madison also uncovers actionable insights by centralizing and visualizing key analytics. |
| 13 | 1:36.9 | It makes understanding what your customers think as simple as asking. |
| 14 | 1:42.5 | Ready to acquire new customers without the crazy cost? Go to MeetMadison.ai. |
| — | 1:50.2 | *(music out)* |

Raw caption files: `reference/madison.en-orig.vtt` (original) and `reference/madison.en.vtt`.

---

## 4. Scene-by-scene breakdown

17 scenes. Boundaries are ±0.5 s and will be tightened per scene as we build.
Every scene has a trimmed reference clip in `reference/clips/`.

### S01 · Search & Discovery — 0.0 → 9.5 s (9.5 s)
`clips/s01_search-discovery.mp4` · VO 1
Empty off-white frame. A Google search pill types **"Restaurants near me"**. Result and profile
mockups (Google SERP card, an Instagram business profile, a Google Business Profile) cascade in and
stack in loose 2.5D. Then platform logos pop in around the stack on a stagger: **Google, Instagram,
TripAdvisor, Yelp, Facebook, Twitter/X, Trustpilot, Booking.com**, plus a small telegram-style
plane mark. Ends with the full constellation held.
*Rebuild note:* eight logo satellites × staggered spring scale over a 3-layer parallax card stack.
Logo assets must be sourced or replaced — see [Open questions](#6-open-questions).

### S02 · Owner Overwhelm — 9.5 → 13.6 s (4.1 s)
`clips/s02_owner-overwhelm.mp4` · VO 2 (first half)
A circular portrait of a business owner centers; **seven task bubbles** pop in around them, roughly
one every 6–8 frames, each a small white rounded card with a purple 1 px border:
1. Post content daily to all my social media accounts
2. Optimize my social media campaigns for each platform
3. SEO optimize my Google Business Profile
4. Derive actionable insights from data spread across platforms
5. Consistently create engaging SEO optimized content
6. Continuously monitor and analyze competitors
7. Ensure my business information is consistent across dozens of platforms
8. Respond to all my online reviews across multiple platforms
Then everything shrinks toward the center and whips away.
*Rebuild note:* the highest-density text scene — our custom copy lands here first and hardest.

### S03 · Agency Invoice — 13.6 → 17.3 s (3.7 s)
`clips/s03_agency-invoice.mp4` · VO 2 (second half)
An invoice card from **"$$$ Expensive Ineffective Marketing LLC."** — *May Invoice*, line items
(5 Social Media Posts, 5 Google Review Responses, 2 Social Media Stories). The **Balance Due** in red
ratchets up across three beats: **$1,441.18 → $2,985.29 → $3,500.00**, while the smug agency-rep
avatar beside it swaps portraits on each ratchet. Ends collapsing to a dot.
*Rebuild note:* number roll-up + avatar cross-fade, synced to the word "expensive".

### S04 · Logo Reveal — 17.3 → 23.8 s (6.5 s)
`clips/s04_logo-reveal.mp4` · VO 3
Large purple circles slide in from opposite corners and squeeze the frame. **"Madison"** scales up
letter-by-letter in purple, then the subtitle **"Your New AI-Powered Digital Marketing Specialist"**
types in beneath it. The blobs then expand to fill the frame purple, wiping into S05.
*Rebuild note:* the signature beat. Two-blob wipe → wordmark → typewriter → blob-fill transition.

### S05 · Product Pillars + Price — 23.8 → 29.0 s (5.2 s)
`clips/s05_product-pillars.mp4` · VO 4
Full-bleed **purple** background. The Madison dashboard floats in as a white card. Three labelled
chips appear above it in sequence: **SEO** → **Social Media** → **Actionable Insights**, each with a
small icon. ($99/mo is spoken, not shown.)
*Rebuild note:* the cleanest scene to rebuild; also the natural home for a price chip if we want the
number on screen.

### S06 · "How do they do it?" — 29.0 → 32.8 s (3.8 s)
`clips/s06_how-do-they-do-it.mp4` · VO 4 tail
Background returns to off-white, dashboard fills more of the frame. A circular reaction portrait
(wide-eyed, then hands-on-head) enters bottom-right with a speech bubble: **"How do they do it?"**
The cursor appears and moves to the sidebar **Integrations** item.
*Rebuild note:* the pivot from pitch to demo — comedic beat, keep it short.

### S07 · Connect Your Accounts — 32.8 → 37.8 s (5.0 s)
`clips/s07_connect-accounts.mp4` · VO 5
**Integrations** screen: *"Authorize Madison to manage your online presence."* Two columns of
provider rows — Google, Instagram, TripAdvisor / Facebook, Twitter. The cursor clicks each
**Connect** button in turn; the button flips to **Remove** and the row fills in an account handle
(`support@yourbusiness.com`, `@your_business`, …).
*Rebuild note:* five sequential click→state-change beats on a stagger. Pure cursor puppetry.

### S08 · Rank Higher on Google — 37.8 → 45.6 s (7.8 s)
`clips/s08_google-rank-gbp.mp4` · VO 6
A Google local-pack card: **Cucina Gustosa / Gyro Heaven / Your Business**. *Your Business* animates
from 3rd position to **1st**. The cursor clicks it; the card expands into a full **Google Business
Profile** — photo grid, Reserve/Website/Directions/Save/Call actions, Overview / Menu / Reviews tabs,
service options, address, description, and social profile icons that pop in last.
*Rebuild note:* list-reorder (FLIP) + card-expand morph. The most complex layout transition.

### S09 · AI Review Responses — 45.6 → 57.5 s (11.9 s) — *longest scene*
`clips/s09_review-responses.mp4` · VO 7 + VO 8
Madison dashboard → **Pending Reviews** panel with a campaign calendar beside it. The cursor clicks
a review; the background dims and a **Review** modal opens (reviewer *Donna Dino*, star rating,
Google mark, full review text). Madison's drafted **Your Response** types in, the cursor edits one
phrase inline, then clicks **Submit Response**. Back on the list, the handled review clears and the
next reviews (Donald Glover, Martin Skolnik, Andrew Reznor) are shown with platform badges.
*Rebuild note:* modal-over-dimmed-parent, long typewriter, inline text edit. Two VO lines share this
scene — the "learns your brand's voice" line lands on the inline edit.

### S10 · Review Requests Funnel — 57.5 → 67.0 s (9.5 s)
`clips/s10_review-requests.mp4` · VO 9
**Review Requests** screen: a contact table (Oliver Paltrow, Amelia Downey, Carla Milne, Stephen
James, Jack Hemsworth) with per-row SMS/call/email icons. The cursor hits **Select all** — checkboxes
tick down the list — then **Send Request**. Cut to the customer's view: a **"How was your experience
with Your Business"** card where stars fill on hover and 5 is chosen; that routes into a Google
review composer (Food / Service / Atmosphere sub-ratings + a typewritten review) and **Post**.
*Rebuild note:* the "only positive ones go to Google" idea is shown, never stated on screen —
worth deciding whether our version makes it explicit.

### S11 · Social Growth Counters — 67.0 → 70.8 s (3.8 s)
`clips/s11_social-growth.mp4` · VO 10 (open)
An Instagram-style business profile card. Three counters roll upward across the shot:
**Posts 67 → 165 → 262 → 360**, **Followers 770 → 4k → 8k → 14k**, Following stays 102. The photo
grid populates beneath.
*Rebuild note:* short, punchy, trivially re-skinnable. Pure counter animation.

### S12 · Campaign Email Approval — 70.8 → 80.5 s (9.7 s)
`clips/s12_campaign-approval.mp4` · VO 10 (body)
A Gmail-style inbox slides in with purple blobs flanking it. One row highlights: **Madison — "Here's
your New Year's Eve campaign for review"**. Cursor clicks; the email opens with a designed purple
header banner (*New Year's Eve Campaign · for review*), body copy from Madison, and a **Review Now**
button. Clicking it wipes to purple and lands in the Madison **Social** composer: *New Year's Eve
2024*, publish-to avatars, platform tabs (Facebook / Instagram / Twitter / Google), a 4-image grid,
generated caption with hashtags, and **Schedule** / **Delete** buttons. Ends on a big
**Schedule** press.
*Rebuild note:* longest multi-app sequence — inbox → email → app. Three distinct layouts in 9.7 s.

### S13 · Tell Madison / Campaign Generator — 80.5 → 88.5 s (8.0 s)
`clips/s13_campaign-generator.mp4` · VO 11
Opens on a **campaign calendar** month view with scheduled posts on dates. Cursor hits **New
Campaign**; a modal opens with *What tone would you like to use?* — the dropdown opens showing
**Authoritative, Celebratory, Energetic, Friendly, Happy, Joking, …** and **Celebratory** is picked.
*What would you like to say?* types in: **"50% off Sangria, Beers, and Mixed Drinks for happy hour
this Friday between 4–6pm."** Schedule field reads *Wednesday, Jan 3, 2024 at 2:00 pm*. **Generate
Campaign** is clicked → the finished post appears with images, caption, and hashtags.
*Rebuild note:* dropdown-open + select, then the second long typewriter. This is the "AI does the
work" money shot.

### S14 · Reports & Analytics — 88.5 → 96.8 s (8.3 s)
`clips/s14_reports-analytics.mp4` · VO 12
**Reports** hub: three grouped sections (Review Performance / Google Performance / Social
Performance) each with descriptive sub-cards, revealed on a stagger with a tooltip popping on hover.
Then it dives into the report itself — **Net Promoter Score 62 % / 20 % / 19 %** with a stacked
green-amber-red bar chart that grows bar-by-bar plus a rising trend line, then the frame zooms out to
a full dashboard: **Google Performance Overview 586,741 · 15.6 %**, **Google Actions 60,728 / 8,093 /
22,430**, **Social Actions 39,077 / 424 / 132 / 16,347 / 12,536**, and multi-series line charts.
*Rebuild note:* the only data-viz scene. Bars grow, lines draw, numbers roll. Needs real chart
rendering, not screenshots, if we want it crisp.

### S15 · Ask Madison (Chat) — 96.8 → 103.2 s (6.4 s)
`clips/s15_ask-madison-chat.mp4` · VO 13
An isolated input + **Send** button on off-white. Two questions type in and send:
**"How is our valet doing?"** then **"What are our customers loving?"**. The frame then expands into
the Madison **Chat** view: Madison's greeting, the user question as a purple bubble, a paragraph-long
AI answer about valet service feedback, and a **Reviews** side panel with the source reviews that
back the answer. Pulls back to show the whole app.
*Rebuild note:* strongest single idea in the video — start with the bare input, reveal the app around
it. Worth preserving structurally even if the copy changes.

### S16 · New Customers Swarm — 103.2 → 107.6 s (4.4 s)
`clips/s16_customer-swarm.mp4` · VO 14 (open)
A field of circular customer portraits and reaction emoji (❤️ 👍 😍 😊 😂) drifts across the frame with
parallax and depth-of-field. The **Madison** wordmark assembles in the middle of the swarm.
*Rebuild note:* ~30 floating elements on randomized drift. Cheap to fake convincingly, expensive to
overdo. Portraits will need replacing.

### S17 · End Card / CTA — 107.6 → 113.1 s (5.5 s)
`clips/s17_end-card.mp4` · VO 14 (close)
Off-white. **Madison** wordmark, **"Get Started Today at"**, and a purple pill button typing out
**MeetMadison.ai**. Cursor rests on it. Holds ~3 s to the end.
*Rebuild note:* trivial; the hold length is the only real decision.

---

## 5. Structural summary

```
0     10    20    30    40    50    60    70    80    90    100   113
|-----|-----|-----|-----|-----|-----|-----|-----|-----|-----|-----|
[  PROBLEM  ][ RVL ][PITCH][ SETUP ][   SEO/REVIEWS   ][ SOCIAL ][ DATA ][CTA]
 S01 S02 S03  S04    S05 S06  S07     S08  S09   S10   S11 S12 S13 S14 S15 S16 S17
```

| Act | Scenes | Span | Share |
| --- | --- | --- | --- |
| Problem / agitation | S01–S03 | 0 – 17.3 s | 15 % |
| Reveal & pitch | S04–S06 | 17.3 – 32.8 s | 14 % |
| Feature demo 1 — Google & reviews | S07–S10 | 32.8 – 67.0 s | 30 % |
| Feature demo 2 — social campaigns | S11–S13 | 67.0 – 88.5 s | 19 % |
| Feature demo 3 — insights & chat | S14–S15 | 88.5 – 103.2 s | 13 % |
| Close | S16–S17 | 103.2 – 113.1 s | 9 % |

Roughly **62 % of runtime is UI demo driven by a fake cursor.** That is the dominant build cost and
the dominant reuse opportunity: one reusable cursor component, one reusable app-shell component
(sidebar + topbar + content slot), and one modal component cover most of it.

---

## 6. Open questions

These need answers before scene-by-scene building starts — none block the folder prep, but the first
two block S01, S04, and S16.

1. **Whose product is this?** Custom text implies a different brand — name, wordmark, palette, and
   URL. Do we keep the Madison purple `#4F45E2` or swap to your brand color?
2. **Third-party logos and stock portraits** — the original leans on Google/Instagram/Yelp/Facebook
   marks and real people's faces. Do we reproduce them, use generic substitutes, or drop the
   satellites entirely?
3. **Narration** — reuse the original VO timing with new recorded lines, use TTS, or go
   music-and-text-only? This sets every scene's duration.
4. **Runtime** — hold at ~113 s or tighten? The SEO/reviews act (S07–S10, 34 s) is the obvious place
   to cut.
5. **Font** — pick the wordmark face (Poppins / Plus Jakarta Sans / Baloo 2) before S04.
6. **Modifications** — you mentioned "some modifications." Which scenes change, and how?

---

## 7. File map

```
D:\video\madison\
├── ANALYSIS.md              ← this document
├── CLAUDE.md / AGENTS.md    ← HyperFrames authoring contract (from scaffold)
├── hyperframes.json         ← project config, authoringSkill: general-video
├── index.html               ← empty composition, 1920×1080, GSAP loaded
├── package.json             ← pins hyperframes@0.7.94; npm run dev/check/render
├── meta.json
└── reference/
    ├── madison.mp4          ← 1080p30 source, 25 MB
    ├── madison_audio.mp3    ← VO + music bed, extracted
    ├── madison.en-orig.vtt  ← caption track (original language)
    ├── madison.en.vtt
    ├── madison.info.json    ← full YouTube metadata
    ├── madison.webp         ← thumbnail
    ├── scenes.json          ← machine-readable scene table (id, name, start, end, title)
    ├── scenes_raw.txt       ← ffmpeg hard-cut detection output
    ├── clips/               ← 17 trimmed per-scene MP4s, named s01_… → s17_…
    ├── frames/              ← 113 timestamped 1 fps stills (f_001.jpg = 0 s)
    ├── sheets/              ← 6 contact sheets, 20 frames each (sheet_0 = 0–19 s)
    └── key_*.png            ← full-res stills used for color sampling
```

### Handy commands

```bash
# rewatch one scene
ffplay reference/clips/s09_review-responses.mp4

# pull a full-res still at an exact second
ffmpeg -ss 46.5 -i reference/madison.mp4 -frames:v 1 -q:v 1 still.png

# re-cut all scene clips after editing reference/scenes.json
#   (the loop lives in the session history; scenes.json is the source of truth)
```
