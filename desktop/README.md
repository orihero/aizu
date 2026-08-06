# AIZU Worker — desktop shell (Phase 6 SCAFFOLD)

> **SCAFFOLD SOURCE ONLY — NOTHING HERE HAS BEEN COMPILED OR TESTED.**
>
> This environment has no Rust / cargo / tauri-cli / PyInstaller toolchain, so none of
> these files were built or run. They are written to be *syntactically plausible* and
> *consistent with Tauri 2.x + PyInstaller conventions*, and they reference the REAL
> worker mechanisms already shipped in `engine/aizu/worker/`. An engineer WITH the
> toolchain must run `cargo check` / `cargo tauri dev` / `pyinstaller --clean` and iterate
> — expect version pins, hidden imports, and icon assets to need adjustment.
>
> The authoritative first-run + blocker runbook is **[`docs/ops/desktop-packaging.md`](../docs/ops/desktop-packaging.md)**.

## What this is

A thin desktop control shell (Tauri 2.x, Rust + system webview — **not** Electron, no
bundled Chromium) that runs on a managed worker PC. It:

1. Supervises the **`aizu-worker`** sidecar binary as a managed child process
   (restart-on-crash watchdog + run-at-login) — BUILD-PLAN Phase 6, **C3 option A**.
   It feeds the child env vars only. It NEVER shells to `aizu.cli` and NEVER touches
   `RunManager`.
2. Manages a separate **warmed, logged-in Chrome** over CDP (mirrors
   `engine/scripts/warm_chrome.sh` and the Python reference `aizu.worker.chrome_manager`).
3. Is a THIN client over the sidecar's **loopback-only control surface**
   (`aizu.worker.control_surface`): it polls `GET /status` for truth and POSTs
   operator commands to `POST /command`. **It does NOT scrape logs for state.**

The job channel itself is outbound HTTPS from the sidecar to the cloud dispatch — the
desktop app is not on that path and must never depend on Tailscale for it (Tailscale is
ops SSH/RDP only; see the packaging doc).

## Subdirectories

| Path | Purpose |
|------|---------|
| `src-tauri/` | The Tauri 2.x Rust app: manifest, config, and `src/` modules (sidecar supervisor, Chrome manager, control-surface client, log tailer, commands, config, errors). |
| `src-tauri/icons/` | App icon set. **None checked in** — generate with `cargo tauri icon` (build blocker). |
| `ui/` | The thin front-end (vanilla HTML/JS/CSS, no React): status badges, per-account health, a prominent "focus Chrome for 2FA/captcha" button, start/stop/pause controls, live log tail, capacity override. |
| `pyinstaller/` | PyInstaller spec + build-time requirements to freeze `aizu-worker` into the sidecar binary the Tauri app supervises. |

## Key invariants (do not regress)

- **No RunManager, no CLI subprocess.** The app spawns `aizu-worker` (=
  `aizu.worker.sidecar:main`) only. Mid-run hard-stop is a `stopCurrentJob` command
  to the control surface — the Python layer force-terminates the killable job child; the
  app does NOT kill the whole sidecar to stop one job.
- **Killing the sidecar is only for app-exit / crash-restart.**
- **Never launch a second Chrome, never kill a Chrome on sidecar restart** — the warmed
  login must survive. Only a Chrome the app itself launched may be killed, and only on
  app exit.
- **CDP port defaults to 9333** (the live-proven port), NOT 9222.
- **Secrets are never persisted in the on-disk TOML.** The control-surface token is
  generated at spawn and passed to the child via env; other secrets come from env/keychain.
- **Soul is baked into the job spec (C5).** `config/soul.md` is NOT bundled by default;
  the PyInstaller `--add-data` escape hatch for the legacy file fallback is documented.
