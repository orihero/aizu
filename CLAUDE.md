# Aizu

Aizu is a brief-driven, multi-platform lead-discovery agent plus a multi-tenant operator panel. You write a campaign brief; the engine drives warmed browser sessions (and platform APIs) across Instagram, LinkedIn, X, YouTube, Reddit, and Telegram to discover and qualify leads, while the React admin panel lets operators author campaigns, launch/pause runs, triage leads, manage teams, and handle billing. It is local-first: all state lives in one SQLite database shared by the engine bridge, on-demand runs, the distributed worker fleet, and the desktop app.

## Repo layout

- `engine/` — Python engine, package `aizu` (import as `aizu...`). Console scripts `aizu` (CLI) and `aizu-worker` (worker sidecar). Contains the bridge HTTP server, run manager, scheduler/reclaim daemons, platform engines, auth/RBAC, and billing. Source under `engine/aizu/`, tests under `engine/tests/`, sample briefs under `engine/config/`.
- `admin-panel/` — React 19 + Vite 6 + TypeScript (strict) SPA. Pure client that talks to the engine bridge JSON API; no backend code. Builds to `admin-panel/dist/`, which the bridge serves in production.
- `desktop/` — Tauri desktop shell (`src-tauri/` Rust, `ui/` frontend, `pyinstaller/` for packaging the `aizu-worker` binary).
- `docs/` — architecture reference, PRDs/build plans, integration and ops guides. See `docs/README.md` as the index. Technical docs only — no marketing or video content.
- `marketing/` — the single home for all brand/marketing work: the canonical creative treatment (`aizu-promo-video-treatment.md`), the aizu.app landing page (`website/`), every video project (`videos/`), and superseded concepts (`archive/`). See `marketing/videos/README.md` for the shared per-project layout; renders, snapshots, and vendored agent skills there are gitignored and regenerable.
- `mockups/` — standalone UI mockups/prototypes (not part of the build).
- `memory/` — running notes: `known-issues.md`, `feedback-ui-mistakes.md`.

## How to run

Requires Python ≥3.10 and Node. First-time setup:
- Engine: `python -m venv engine/.venv && engine/.venv/bin/pip install -e engine` (add `.[dev]` for pytest, `.[telegram]` for live Telegram/Telethon).
- Panel: `cd admin-panel && npm install`.

**Dev (both servers):** `./dev.sh` (macOS/Linux) or `.\dev.ps1` (Windows) — starts the bridge on `127.0.0.1:8765` (via `engine/scripts/dev_panel.py`, auto-restarts on `engine/aizu/*.py` edits) and the Vite panel dev server on `http://localhost:5173`, which proxies `/api` to the bridge. Ctrl+C tears both down; if either server dies the other is stopped too. Both scripts free their ports first, killing a stale bridge/panel left by an unclean exit. Overridable env: `BRIDGE_PORT` (8765), `BRIDGE_HOST` (127.0.0.1), `PANEL_PORT` (5173), `DB` (aizu.db) — these are shell vars read by the launcher, not by Python. `dev.ps1` also takes them as parameters (`-BridgePort`, `-BridgeHost`, `-PanelPort`, `-Database`; not `-Db`, which collides with the `-Debug` common parameter). Note that Vite binds `localhost` (IPv6 `::1`) on Windows, so use `http://localhost:5173`, not `127.0.0.1:5173`. Keep `dev.ps1` ASCII-only: Windows PowerShell 5.1 parses scripts as the ANSI codepage, and a UTF-8 em dash decodes into a curly quote that it treats as a string delimiter.

**Bridge only (manual):** `aizu panel [--host 127.0.0.1] [--port 8765] [--panel-dir ../admin-panel/dist] [--config config] [--db aizu.db]`. Errors if `index.html` is missing under `--panel-dir`.

**Panel dev only:** `cd admin-panel && npm run dev`.

**Engine run (usually launched from the panel Run button, not by hand):**
- `aizu run --campaign <id>` — run a panel-authored campaign brief resolved from the DB (multi-platform, sequential).
- `aizu run` — run the file brief `config/campaign.md`.
- `aizu run-all [--org <id>]` — run every `live`, non-archived campaign.
- `aizu status` — print open health flags. `aizu warm-register ...` — register a logged-in account into the warming pool.
- Useful run flags: `--dry-run` (fake feed, no network/LLM), `--target-leads N`, `--duration-minutes N`, `--engine-mode {harvest,warming}`, `--spend-cap`, `--cdp-url`. Global `--db` (default `aizu.db`), `-v/-q`.

**Worker sidecar:** `aizu-worker` (PULL model; requires `AIZU_DISPATCH_URL`).

**Production:** single server — `aizu panel` serves the built SPA (`admin-panel/dist`) plus the `/api/*` control plane from one process; run behind a reverse proxy. Build the panel first with `cd admin-panel && npm run build`.

**Key env vars** (`AIZU_*` and a few others; loaded from `./.env` then `engine/.env`, existing env wins):
- `OPENROUTER_API_KEY` — required for any live (non-warming) run and for AI campaign generate/interview. `OPENROUTER_TEXT_MODEL` (default `openrouter/owl-alpha`), `OPENROUTER_VISION_MODEL` (default `nex-agi/nex-n2-pro:free`).
- `AIZU_CDP_URL` — CDP attach URL for warmed Chrome (default `http://127.0.0.1:9222`).
- `AIZU_SECRET_KEY` — Fernet key (32 bytes, urlsafe-base64) for per-org integration secrets and admin TOTP; required for integrations and admin bootstrap/login.
- `AIZU_ADMIN_IP_ALLOWLIST` — CSV IPs/CIDRs for the superadmin plane; empty/unset = fail-closed (no admin access). `AIZU_TRUSTED_PROXIES` gates `X-Forwarded-For`.
- `AIZU_ALLOWED_ORIGINS` — CSV of exact `scheme://host[:port]` origins the panel is served from on a hosted deployment (e.g. `https://aizu.uz`). Loopback is always allowed, so local-first runs need this unset; without it a network-served panel gets `403 cross-origin request rejected` on every POST.
- `AIZU_WARMING_ENABLED` — layer-1 warming hard-stop (default off). `AIZU_IGNORE_DAYTIME` — disable daytime write guard (testing).
- Worker plane: `AIZU_DISPATCH_URL` (required), `AIZU_WORKER_BOOTSTRAP_TOKEN`, `AIZU_DB` (default `aizu.db`), `AIZU_WORKER_STATE` (default `.worker-state`), `AIZU_SPEND_CAP`, `AIZU_CONTROL_SURFACE`/`AIZU_CONTROL_TOKEN`.
- Billing (Polar, optional; missing ⇒ billing disabled): `POLAR_ACCESS_TOKEN`, `POLAR_WEBHOOK_SECRET`, `POLAR_SERVER` (default `sandbox`), `POLAR_PRODUCTS`.
- Per-platform live creds when no per-org stored secret: `YOUTUBE_API_KEY`; `TELEGRAM_BOT_TOKEN` or `TELEGRAM_API_ID`/`TELEGRAM_API_HASH`/`TELEGRAM_SESSION`; `REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET`/`REDDIT_USER_AGENT`.
- Logging: `AIZU_LOG_LEVEL`, `AIZU_LOG_FILE_LEVEL`, `AIZU_LOG_FILE`, `AIZU_LOG_COLOR`.
- Defaults: DB is `aizu.db`, log file is `aizu.log`.

Superadmins are created out-of-band only: `python -m aizu.admin_bootstrap --db <db> --email <email>` (requires `AIZU_SECRET_KEY`).

## How to test

- Engine: `cd engine && engine/.venv/bin/python -m pytest` (tests under `engine/tests/`; install `.[dev]` first). Marker `slow` covers end-to-end tests that spawn a real subprocess. Use `python -m pytest`, not the bare `pytest` script: `test_telegram_warming_*.py` import `tests.fakes.telegram_warming`, and with no `tests/__init__.py` that namespace package only resolves when `engine/` is on `sys.path` — which `python -m` provides and the console script does not.
- Panel: `cd admin-panel && npm test` (Vitest, one-shot); `npm run test:watch`, `npm run test:coverage`. Also `npm run typecheck` (strict `tsc -b`) and `npm run lint` (`eslint src`). Tests colocate with source under `src/**` and inject a fake repository through the DI seam.

## Key conventions

- Python package is `aizu` — always `import aizu...`. Two console entry points: `aizu` (CLI: `run`/`run-all`/`status`/`panel`/`warm-register`) and `aizu-worker` (distributed worker sidecar; same target packaged via PyInstaller for desktop).
- The bridge is stdlib only (`ThreadingHTTPServer`, no web framework); the panel is a pure client with a never-throw `Result`-based repository and Zod-validated boundaries. Only one engine run executes at a time (single browser/account), enforced by a lock in the run manager.
- RBAC (`engine/aizu/rbac.py`) is an explicit action→roles matrix (owner/admin/member/viewer), NOT a linear rank; the frontend mirror `admin-panel/src/shared/auth/roles.ts` must stay in lockstep. The server is the real gate; UI gating is UX only.
- Some READMEs (`engine/README.md`, `admin-panel/README.md`) are stale (single-platform / pre-multi-tenancy, CDP port 9333, "dark default"). Trust the code over the READMEs.

## Pointers

Architecture reference (indexed by `docs/README.md`):
- `docs/architecture/overview.md` — system overview: bridge, panel, on-demand run, worker fleet, desktop app, shared SQLite DB.
- `docs/architecture/data-model.md` — complete SQLite schema and ER diagram.
- `docs/architecture/api-reference.md` — every `/api/*` bridge endpoint.
- Also `docs/architecture/engines.md` (per-platform engine model) and `docs/prd/` (product specs and build plans).
