# ReelRadar — engine

Local-first, brief-driven Instagram Reel-Comment discovery agent (see
`../instagram-lead-agent-PRD.md`). The engine knows no vertical and no goal —
all domain meaning lives in `config/campaign.md`. SQLite is the only contract
between the engine and the admin panel.

## Layout
```
config/soul.md          engine identity + safety (domain-free)
config/campaign.md      the active brief (shipped default: generic SaaS lead-gen)
reelradar/config.py     loaders for soul.md + campaign.md
reelradar/store.py      SQLite (WAL): matches, state, counters, flags, spend
reelradar/router.py     model router — OpenRouter cloud tier + spend/degrade
reelradar/mock_router.py deterministic offline router (dry runs + tests)
reelradar/cascade.py    relevance gate + comment match (escalate-if-unsure)
reelradar/feed.py       FeedSource interface · FakeFeed
reelradar/parsers.py    pure shape-driven reel/comment extractors (tested)
reelradar/cdp.py        CDPFeed — live Playwright-over-CDP attach + interception
reelradar/pacing.py     human-paced, daytime-only timing
reelradar/session.py    orchestrator + three-tier failure handling
reelradar/panel.py      DB → /api/state adapter for the admin panel
reelradar/server.py     local bridge server: /api/state + write/control endpoints
reelradar/runner.py     RunManager — spawns + tracks panel-triggered engine runs
reelradar/cli.py        `reelradar run [--campaign] [--dry-run]` · `run-all` · `status` · `panel`
tests/                  store, config, router, cascade, session, panel, runner, parsers
```

## Run
> To start the bridge **and** the React panel together with one command, use
> [`../dev.sh`](../README.md#run-everything-one-command) from the repo root. The
> commands below drive the engine on its own.

```bash
pip install -e .            # or: pip install playwright httpx python-dotenv
python -m reelradar.cli --db reelradar.db run --dry-run    # full loop, no network
python -m reelradar.cli --db reelradar.db run-all --dry-run # every live campaign, sequentially
python -m reelradar.cli --db reelradar.db status           # open health flags
python -m reelradar.cli --db reelradar.db panel            # serve admin panel at :8765

# live (needs key; CDP attach not yet wired — see below)
export OPENROUTER_API_KEY=...
python -m reelradar.cli run                       # runs config/campaign.md
python -m reelradar.cli run --campaign <id>       # runs a panel-authored brief (DB)
python -m reelradar.cli run-all                   # runs every status=live campaign
```

Campaigns can be authored/edited in the admin panel (Campaigns → New / Edit): the
full brief (platform, threshold, languages, relevance/match/extract, seeds) is
stored in the `campaign_briefs` table via `POST /api/campaign`. `run --campaign
<id>` builds a `Campaign` from that brief (`config.campaign_from_brief`) and runs
it through the same loop as a file campaign — no `config/campaign.md` needed.

**Running from the panel (PRD §5 revision, v5).** The bridge server is also a
*control plane*: `POST /api/run` (`{campaignId|all, mode:'dry'|'live'}`) spawns the
engine for one campaign or all live ones — dry-run by default, live as an explicit
opt-in, with a process-global single-run lock (one browser ⇒ one live run). The
Campaigns page exposes a per-card **Run** and a header **Run all live**. In-flight +
recent run status rides on `/api/state` as the additive `RUN` block; SQLite
(`sessions`) remains the durable record. See `reelradar/runner.py`.

## Platforms
One engine, one brief; `platform:` in `campaign.md` selects the feed
(`feeds/build_feed`). The session/cascade/store/panel layers are platform-agnostic
— matches are keyed `(campaign_id, platform, comment_id)` and pooled in the panel.

| platform | feed | discovery | auth (live) |
|---|---|---|---|
| `instagram` | `cdp.py` CDPFeed | seed hashtags/accounts | warmed Chrome over CDP |
| `youtube` | `feeds/youtube.py` (Data API v3) | `seed_channels` (channel ids) + `seed_hashtags` (search queries) | `YOUTUBE_API_KEY` |
| `telegram` | `feeds/telegram.py` (Bot API) | `seed_channels` the bot is admin of | `TELEGRAM_BOT_TOKEN` |

```bash
# youtube: campaign.md has `platform: youtube` + seed_channels / seed_hashtags
export OPENROUTER_API_KEY=...  YOUTUBE_API_KEY=...
python -m reelradar.cli run

# telegram: `platform: telegram` + seed_channels; create the bot via @BotFather
# and add it as ADMIN to each channel/group (the Bot API sees new messages it
# receives, not channel history — unlike an MTProto user session).
export OPENROUTER_API_KEY=...  TELEGRAM_BOT_TOKEN=...
python -m reelradar.cli run
```
The HTTP/API binding of each new feed is isolated behind a small port, so the
item→Reel / comment→Comment mapping is fully unit-tested with fakes (no key, no
network); the live API call is the only part to validate on a real account.

## Test
```bash
PYTHONPATH=. pytest -q
```

## What's built (v1 foundation)
- Full SQLite schema + state model: idempotent matches (re-poll never overwrites
  human status), seen-reels watermark, comment cursors, watchlist TTL, session
  counters, health flags, per-campaign spend. Resumes from state after a kill.
- OpenRouter cloud router with retry → degrade-to-local-stub + soft flag, spend
  cap guard, per-stage spend logging.
- Cascade: caption → vision/OCR → escalate-to-cloud relevance gate; local
  pre-score → escalate-if-unsure comment scoring.
- Session loop: pacing, dedupe, tired-feed flag, empty-interception canary,
  three-tier failure handling (auto-skip / soft-flag / halt).

## Admin panel (read-only, PRD v1 surface)
`reelradar panel` serves the mockup in `../admin-mockup` and injects a generated
`window.__RAW__` built live from this DB, so every view (matches, watchlist,
health, spend, sessions, overview) shows real engine state. `data.js` falls back
to its demo records when no live data is present — open `index.html` directly and
it's still the standalone mockup. Status writes (Confirm/Discard persisting to
SQLite) are a PRD v2 surface and are intentionally not wired here.

## Live capture (CDPFeed)
`cdp.py` attaches to a warmed, logged-in Chrome over CDP and reads the page's
own response traffic — it never crafts API calls or launches a vanilla browser.
Intercepted JSON is parsed by `parsers.py`, whose detection is **by shape, not
hardcoded IDs**, so endpoint drift (PRD §13) degrades gracefully. The parsers
are fully unit-tested against fixtures; the live attach/scroll choreography
can't run in CI and is the part to validate on a real machine.

Setup on the operator's Mac:
```bash
pip install playwright            # attaches over CDP — does NOT download a browser
# start the warmed Chrome with remote debugging (port 9333, matches REELRADAR_CDP_URL):
bash scripts/warm_chrome.sh        # dedicated profile; log into Instagram once in the window
# (manual equivalent — note Chrome 136+ refuses --remote-debugging-port on the DEFAULT profile:
#   /Applications/Google\ Chrome.app/.../Google\ Chrome \
#     --remote-debugging-port=9333 --user-data-dir="$HOME/.reelradar-chrome-profile")
export OPENROUTER_API_KEY=...
python -m reelradar.cli run        # attaches, walks the reels feed, scores comments
```
DevTools-confirmation points (expected to need tuning): the reel/comment
endpoint URL hints (`CDPConfig.reel_url_hints` / `comment_url_hints`) and the
comment-open selector in `_open_comments_and_paginate`.

## Not yet wired (next)
- Real local model tiers (this build is cloud-only by choice).
- Panel write surfaces (status workflow, campaign editor) — PRD v2.
- Live validation of the CDP scroll/comment choreography on a real account.
