# Aizu documentation

Central index for all Aizu docs. Start with the project [`README.md`](../README.md)
for how to run everything, then dive into the areas below.

Looking for marketing copy, the brand site, or a video project (demo, explainer, ad
concepts)? Those all live in [`../marketing/`](../marketing/), not here — this tree is
technical reference only.

## Architecture (as-built reference)

| Doc | What's inside |
| --- | --- |
| [`architecture/overview.md`](architecture/overview.md) | System overview: the engine bridge, admin panel, on-demand run, distributed worker, and desktop app — and how they share one SQLite DB |
| [`architecture/data-model.md`](architecture/data-model.md) | Complete SQLite schema: tables, columns, keys, relationships, ER diagram |
| [`architecture/api-reference.md`](architecture/api-reference.md) | Every `/api/*` endpoint on the bridge server (`:8765`) |
| [`architecture/engines.md`](architecture/engines.md) | Platform-engine model and per-platform behavior |
| [`architecture/agent-flow-diagram.svg`](architecture/agent-flow-diagram.svg) | Agent crawl/run flow diagram |

## Product specs & plans

Platform lead agents are split by whether the engine actually exists yet.

**Shipped** — real engine under `engine/aizu/engines/`, listed in `SUPPORTED_PLATFORMS`
(`engine/aizu/core/config.py`):

- [Instagram](prd/instagram-lead-agent-PRD.md) · [YouTube](prd/youtube-lead-agent-PRD.md) ·
  [LinkedIn](prd/linkedin-lead-agent-PRD.md) · [X](prd/x-lead-agent-PRD.md) ·
  [Reddit](prd/reddit-lead-agent-PRD.md) · [Telegram](prd/telegram-lead-agent-PRD.md)

**Planned** — spec only, no engine yet: [`prd/planned/`](prd/planned/) (Facebook, Pinterest,
Quora, Threads, TikTok).

Cross-cutting specs:

- **Multi-platform campaign** — [PRD](prd/multi-platform-campaign-PRD.md) · [build plan](prd/multi-platform-campaign-BUILD-PLAN.md)
- **Distributed workers** — [PRD](prd/distributed-workers-PRD.md) · [build plan](prd/distributed-workers-BUILD-PLAN.md)
- **Campaign lifecycle controls** — [PRD](prd/campaign-lifecycle-controls-PRD.md)
- **Campaign Lab** (research phase) — [PRD](prd/campaign-lab-PRD.md): verify seeds/hashtags and benchmark prompts before launch
- **Warming system** — [system PRD](prd/warming-system-PRD.md) · [writes PRD](prd/warming-writes-PRD.md)

## Integrations

- [`integrations/billing-polar.md`](integrations/billing-polar.md) — Polar billing integration
- [`integrations/polar-sandbox-setup.md`](integrations/polar-sandbox-setup.md) — Polar sandbox setup

## Operations

- [`ops/server-deployment.md`](ops/server-deployment.md) — the aizu.uz production VDS: topology,
  services, CI/CD deploy pipeline, runbook, and current drift
- [`ops/desktop-packaging.md`](ops/desktop-packaging.md) — packaging the Tauri desktop app + Python sidecar

## Archive

- [`archive/`](archive/) — superseded or actioned docs, kept for history. Nothing in here is
  current guidance.

## Component-level docs

- [`../engine/README.md`](../engine/README.md) — engine internals, CLI, campaigns, platforms
- [`../admin-panel/README.md`](../admin-panel/README.md) — panel architecture, scripts, tests
- [`../desktop/README.md`](../desktop/README.md) — desktop app
- [`../CLAUDE.md`](../CLAUDE.md) — guide for AI coding agents working in this repo

## Elsewhere in the repo

- [`../marketing/`](../marketing/) — brand site, promo video treatment, ad concepts, and every video project
- [`../mockups/`](../mockups/) — frozen pre-build UI prototypes (not part of the build)
- [`../memory/`](../memory/) — durable bug/gotcha ledger
