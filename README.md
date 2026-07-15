# ReelRadar

Brief-driven, multi-platform Reel/post-comment lead-discovery agent plus its
operator panel. Two apps share one SQLite database:

| App | Path | What it is |
| --- | --- | --- |
| **engine** | [`engine/`](engine/README.md) | Python crawl agent + the local **bridge** server that exposes the JSON API (`/api/*`) on `:8765` |
| **panel** | [`admin-panel/`](admin-panel/README.md) | React 19 + Vite operator panel; in dev it runs on `:5173` and proxies `/api` → the bridge |

The live Instagram/YouTube/etc. **run** is a third, on-demand process — you start
it from the panel's **Run** button or `reelradar.cli ... run`. It is *not* a
long-lived server, so the launcher below does not start it.

## Run everything (one command)

```bash
./dev.sh
```

This starts both long-lived dev servers with interleaved, color-prefixed logs and
tears **both** down on `Ctrl+C`:

- `[bridge]` — engine API on `http://127.0.0.1:8765` (auto-restarts on `engine/reelradar/*.py` edits)
- `[panel]`  — Vite dev server; open the `http://localhost:5173` URL it prints

### First-time setup

`dev.sh` checks for these and tells you exactly what to run if they're missing:

```bash
# engine: virtualenv + editable install
python -m venv engine/.venv
engine/.venv/bin/pip install -e engine

# panel: node deps
(cd admin-panel && npm install)
```

### Overrides (environment variables)

| Variable | Default | Effect |
| --- | --- | --- |
| `BRIDGE_PORT` | `8765` | Port the engine bridge listens on |
| `BRIDGE_HOST` | `127.0.0.1` | Host the bridge binds to |
| `DB` | `reelradar.db` | SQLite file the engine/bridge use |

```bash
BRIDGE_PORT=8770 DB=staging.db ./dev.sh
```

> If you change `BRIDGE_PORT`, also update the proxy target
> `BRIDGE_SERVER_URL` in `admin-panel/vite.config.ts` so the panel proxies to the
> right place.

## Run the apps manually (two terminals)

Equivalent to what `dev.sh` does, if you'd rather see each on its own:

```bash
# Terminal 1 — engine bridge (auto-restarting dev wrapper)
cd engine
.venv/bin/python scripts/dev_panel.py            # API on 127.0.0.1:8765

# Terminal 2 — panel dev server
cd admin-panel
npm run dev                                       # http://localhost:5173
```

## Production (single server)

In production the bridge also serves the panel's built assets, so there is only
one process:

```bash
(cd admin-panel && npm run build)                 # bundle -> admin-panel/dist
cd engine
.venv/bin/python -m reelradar.cli --db reelradar.db panel \
    --panel-dir ../admin-panel/dist --config config --port 8765
# open http://127.0.0.1:8765/
```

## Logs & debugging

Three layers, from quietest to loudest:

1. **Bridge console** (`[bridge]` lines from `dev.sh`) — access log per request
   (`METHOD path → status · ms`). Failures now show *why*: a 4xx/5xx response logs
   its error reason, and a **failed engine run prints the full stack trace inline**
   plus the path to its complete log.
2. **Per-run logs** — every panel-triggered run streams its full stdout+stderr
   (including tracebacks) to `engine/run-logs/run-<id>-<scope>.log`. This is the
   complete record of a single run; open it to see everything that run did.
3. **Rotating archive** — all engine/bridge logging is also written, ANSI-free, to
   `engine/logs/reelradar.log` (rotating, DEBUG level — the fullest archive).

### Full firehose (every incoming/outgoing body)

Run the bridge at `DEBUG` to log every request and response **body** (secrets are
scrubbed automatically):

```bash
REELRADAR_LOG_LEVEL=DEBUG ./dev.sh
```

| Variable | Values | Effect |
| --- | --- | --- |
| `REELRADAR_LOG_LEVEL` | `DEBUG`/`INFO`/`WARNING`/… | Console verbosity (`DEBUG` = body firehose) |
| `REELRADAR_LOG_FILE` | path / `off` | Override or disable the rotating archive |
| `REELRADAR_LOG_COLOR` | `auto`/`always`/`never` | Console color |

> A run that exits non-zero is summarized on `/api/state` (and the panel's
> last-run line) with the real exception, not a bare `exit 1`. For the complete
> trace, read its `engine/run-logs/run-<id>-*.log`.

## More detail

- Engine internals, CLI, campaigns, platforms → [`engine/README.md`](engine/README.md)
- Panel architecture, scripts, tests → [`admin-panel/README.md`](admin-panel/README.md)
- Product docs → [`docs/`](docs/)
