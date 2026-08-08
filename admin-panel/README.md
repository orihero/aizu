# Aizu — Admin Panel (React)

Production admin panel for the Instagram Reel-Comment Discovery Agent
(`../instagram-lead-agent-PRD.md`). React 19 · TypeScript (strict) ·
Tailwind CSS v4 · TanStack Query · Recharts · Zod · Vite.

Reads the engine's SQLite state through the bridge server's JSON API and
writes operator status decisions back (`POST /api/status`, keyed on
`comment_id` — PRD §11 v1 status-mark). The static mockup remains at
`../admin-mockup` for design reference.

## Run

> To start the panel **and** the engine bridge together with one command, use
> [`../dev.sh`](../README.md#run-everything-one-command) from the repo root. The
> steps below run this app on its own.

```bash
# 1. Engine bridge (serves the API and, in production, this app's build)
cd ../engine
./.venv/bin/python -m aizu.cli --db aizu.db panel \
    --panel-dir ../admin-panel/dist --config config --port 8765

# 2a. Production: open http://127.0.0.1:8765/app/  (after `npm run build`; "/" serves the
#     marketing landing, not this app)
# 2b. Development: hot-reloading dev server proxying /api to the bridge
npm install
npm run dev          # http://localhost:5173/app/  (the bare "/" serves the static landing)
```

## Scripts

| Script | Purpose |
| --- | --- |
| `npm run dev` | Vite dev server (proxies `/api` → `127.0.0.1:8765`) |
| `npm run build` | Typecheck + production bundle to `dist/` |
| `npm run test` | Vitest suite (54 tests) |
| `npm run test:coverage` | Coverage report (94% lines / 82% branches) |
| `npm run lint` | ESLint (typescript-eslint strict, type-checked) |
| `npm run typecheck` | `tsc -b` with strict + exactOptionalPropertyTypes |

## Architecture

```
src/
  app/                 composition root: providers, hash router, layout chrome
  features/<page>/     one folder per page (overview, matches, watchlist,
                       health, spend, sessions, campaigns, review)
  shared/
    api/               PanelRepository interface (DIP) + HTTP implementation
    schemas/           Zod boundary schemas — nothing unvalidated enters the app
    selectors/         pure functions deriving every displayed number
    hooks/             usePanelState (query), useSetMatchStatus (optimistic
                       mutation), useTheme (context), useHotkeys
    ui/                presentational primitives (Card, Badge, Drawer, …)
    lib/               Result type, formatters
  test/                fixtures, FakePanelRepository, render helpers
```

Design rules this codebase follows:

- **Dependency inversion** — features depend on the `PanelRepository`
  interface; the HTTP transport is injected at the composition root and
  swapped for `FakePanelRepository` in tests.
- **Never-throw boundary** — all network/parse/shape failures come back
  as typed `Result` values; payloads are Zod-validated before use.
- **Single source of truth** — the DB drives everything. Status writes
  are optimistic with rollback, then re-synced from the server. The
  review queue snapshots its order at session start so background
  refetches never reshuffle what the operator is looking at.
- **Pure selectors** — components never compute aggregates inline;
  every derived number has a unit-tested selector.
- **Theming** — semantic CSS custom properties (dark default, light
  override) mapped into Tailwind tokens; charts resolve concrete colors
  per theme via `useChartPalette`.
