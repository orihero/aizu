# Verified fact bank — 9-slide pitch

Every figure in `src/deck-pitch.html` comes from here. Each was researched, then
independently re-fetched by an adversarial checker whose instruction was to refute
it. Anything the source page did not actually state was dropped.

Compiled 10 August 2026.

## Used in the deck

| Fact | Source | Year | Slide |
|---|---|---|---|
| 57% of small firms call reaching customers their top challenge (up from 53%) | Federal Reserve Banks, Small Business Credit Survey | 2024 | 02 |
| 41% of Gen Z search social first; Google 32% | Sprout Social / Glimpse, n=2,280 | 2025 | 02, 02·B |
| 90% of Gen Z say social content shaped a purchase in the last 6 months | Sprout Social / Glimpse, n=2,280 | 2025 | 02·B |
| 0.45% cold-email reply rate across 7.5M sent | Belkins B2B cold email study | 2025 | 02·A |
| 59% of CMOs say budget can't cover strategy; spend flat at 7.7% of revenue | Gartner CMO Spend Survey | 2025 | 02·A |
| Sales intelligence software $4.85B | Fortune Business Insights | 2025 | 04 |
| Social/media monitoring $6.30B | Fortune Business Insights | 2025 | 04 |
| Lead generation software $8.76B | 360iResearch | 2025 | 04 |
| Apollo.io $49–119/user/mo | apollo.io pricing page | Aug 2026 | 07 |
| Brand24 $199–399/mo | brand24.com pricing page | Aug 2026 | 07 |
| Sprout Social $79–299/seat/mo | sproutsocial.com pricing page | Aug 2026 | 07 |
| Syften $29.95–119.95/mo | syften.com pricing page | Aug 2026 | 07 |
| Clay $167–446/mo | clay.com pricing page | Aug 2026 | 07 |
| AIZU tiers: $0/10, $24.99/250, $149/2,000, custom | `engine/aizu/billing.py` | 2026 | 06 |

## Modelled, not sourced — say so if asked

The **SAM** on slide 04 is not a published figure. It is
`$6.30B + 30%x$8.76B + 20%x$4.85B = $9.9B`, where the 30% (social-attributable share
of lead-gen spend) and 20% (SMB-usable share of sales-intel spend) are our own
assumptions. The **SOM** band ($10-50M, 0.1-0.5% of SAM) is a generic early-stage
capture heuristic, not a bottom-up count. The slide footnote states both.

Also note that research houses disagree by 30-35% on identically-named markets
(Mordor puts sales intelligence at $4.42B against Fortune's $4.85B), so no single
TAM figure is a consensus figure.

## Rejected — do not re-add

These are the tempting ones. Each failed verification; several are widely quoted.

- **"97% of brand comments go unanswered" / "68% read comments before buying"** —
  both trace to one vendor's self-published PR announcing its own competing AI
  product. No methodology disclosed.
- **"$87B US social commerce in 2025"** (eMarketer) — primary source unreachable;
  only secondary outlets relay it.
- **"ZoomInfo median contract ~$33,500/yr, per Vendr"** — the cited page never
  mentions Vendr or the figure. Fabricated.
- **"SBA recommends 7-8% of revenue on marketing"** — no traceable SBA.gov citation.
  Attributing an unsourced number to a government agency in front of a government
  committee is the single worst risk in this list.
- **"LinkedIn CPM rising 3-8% YoY"** — invented range; the source supports one flat
  8% figure for general ad costs.
- **"Central Asia e-commerce $14.7B (2024) -> $182B (2033)"** (IMARC) — the live page
  says $19.2B (2025) -> $191.6B (2034).
- **"IT Park's own site says exports surpassed $1B"** — that page does not say it.
  The $1B figure is a Ministry of Digital Technologies forecast from a different
  source. The number may be usable; the attribution was not.
- **"~28M online stores worldwide" / "90M+ Facebook small businesses"** — used for a
  bottom-up SOM cross-check. Both failed; the cross-check was dropped rather than
  kept with a caveat.
- **"300,000 new IT jobs by 2030"**, **"average Uzbek developer salary ~$26,600"** —
  neither present in the cited sources.

## Uzbekistan context — verified, currently unused

Held back because the nine slides had no room, not because they failed.

- Telegram reaches 76% of Uzbekistan's internet users (~25M); TikTok remains blocked.
  (DataReportal-linked data via UzDaily, 2025)
- IT Park Uzbekistan: 2,990+ resident companies, 750+ international, 44,000+ jobs.
  (IT Park Uzbekistan, July 2025)
- Uzbekistan IT service exports set to exceed $1B for the first time in 2025,
  targeting $5B by 2030. (Ministry of Digital Technologies, via Gazeta.uz, 2025)
- 1.21 million active small businesses contribute 51.5% of GDP.
  (National Statistics Committee, via UzDaily, Oct 2025)

## One competitive fact worth knowing

GummySearch, a Reddit-native lead-discovery tool with 140,000+ users, shut down
entirely on 30 November 2025 after a single platform's API licensing change. It is
the clearest available argument for why multi-platform, non-API-locked architecture
matters — and the clearest warning about what platform dependence costs.
