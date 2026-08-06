# Desktop packaging & ops runbook — AIZU Worker (Phase 6)

> ## ✅ macOS BUILD VERIFIED (ad-hoc, 2026-07-02)
>
> The toolchain was installed (Rust via rustup, `@tauri-apps/cli` 2.11.4, PyInstaller 6.21)
> and the **macOS build now compiles, bundles, and ad-hoc-signs cleanly**:
>
> - `aizu-worker` PyInstaller binary (59 MB) — **built + smoke-verified booting**
>   (configures logging, writes machine-id, runs the register path, exits cleanly).
> - `AIZU Worker.app` + `AIZU Worker_0.1.0_aarch64.dmg` — **built, ad-hoc signed**
>   (`Signature=adhoc`, id `com.aizu.workerdesktop`), sidecar bundled at
>   `Contents/Resources/sidecar/aizu-worker`. Gatekeeper reports `rejected` (expected:
>   ad-hoc ≠ notarized — strip quarantine or distribute out-of-band on the managed fleet).
>
> **Fixes applied to the original scaffold to make it build** (already in the tree):
> 1. `tauri.conf.json` — removed all `"//"` comment keys (Tauri's schema is strict),
>    dropped the updater block (needs a signing key), set macOS `signingIdentity: "-"`
>    (ad-hoc) and Windows `certificateThumbprint: null` (unsigned).
> 2. `Cargo.toml` — made it a pure binary crate (removed the mobile-style `[lib]` with no
>    `lib.rs`), removed `tauri-plugin-updater` + the v1 `custom-protocol` feature.
> 3. `src/main.rs` — added `use tauri::Manager;` (for `manage()`/`app_handle()`), removed
>    the updater plugin `.plugin(...)` line.
> 4. `src/control_client.rs` — derived `Default` on the response DTOs (serde `#[serde(default)]`
>    on `Envelope<T>` requires `T: Default`).
> 5. `run_sidecar.py` — added a package-import shim as the PyInstaller entry (running
>    `sidecar.py` directly broke its `from ..` relative imports); spec collects
>    `aizu`/`playwright` submodules for the lazy engine imports.
> 6. Icons generated via `tauri icon` from a brand PNG (Ink × Lime "ping").
>
> **Still uncompiled here (needs a Windows host):** the `.exe`/`.msi`/NSIS bundles — build
> them on Windows with `cargo tauri build --bundles nsis` (unsigned; see §Blockers).
>
> ### Reproduce the macOS build
> ```sh
> desktop/scripts/build_macos.sh   # onedir sidecar → tauri app → embed sidecar → re-sign
> # artifact: desktop/src-tauri/target/release/bundle/macos/AIZU Worker.app
> ```
> Why a script (not plain `tauri build`): the sidecar is a PyInstaller **onedir** folder
> (near-instant cold start — onefile re-extracted its 59MB every launch ≈ 20s). Tauri's
> `bundle.resources` copier FAILS on the onedir tree (`_internal/Python` is a Mach-O →
> "Not a directory"), so the script builds the app WITHOUT a sidecar resource, copies the
> onedir folder into `Contents/Resources/sidecar/aizu-worker/`, then re-`codesign
> --force --deep --sign -` the whole bundle (Apple Silicon needs every embedded Mach-O
> ad-hoc-signed). `sidecar_supervisor::resolve_binary` looks there.
>
> **Cold-start note:** the FIRST launch of a freshly-built binary pays a one-time macOS
> security scan (~20s, near-zero CPU); every launch after is ~0.1s and the control surface
> binds in ~2s. Ad-hoc signing + de-quarantine minimizes it.
>
> **Env switcher:** a hidden dev menu (tap the "AIZU Worker" title 7×) switches dispatch
> local⇄cloud at runtime — writes config.toml + a 0600 token file, then relaunches. Default
> control port is 8788 (NOT 8799 — avoids the dev-panel clash).
>
> **Token backend note:** `keyring` is now an installed dep, but `AIZU_TOKEN_BACKEND`
> defaults to `auto` = **Fernet file** (keyring is OPT-IN via `=keyring` only), so an
> unattended box never risks a blocking Keychain unlock prompt at startup.

This is the authoritative runbook. The `desktop/README.md` is the quick orientation; this
doc is the ordered first-run, the blocker list, the Tailscale-for-ops policy, and the
residual gaps.

---

## 0. Architecture recap (why the app is thin)

The desktop app supervises the Python **`aizu-worker`** sidecar binary as a managed
child process (BUILD-PLAN Phase 6, **C3 option A**): restart-on-crash watchdog +
run-at-login. It feeds the child env vars only.

- **No RunManager, no CLI subprocess.** The sidecar (`aizu.worker.sidecar:main`)
  bypasses `RunManager` and runs each leased job in a killable child process
  (`aizu.worker.job_child`).
- **Mid-run hard-stop already exists at the Python layer.** A `halt` SIGTERM→SIGKILLs the
  job child. So the app's "Stop current job" is a `stopCurrentJob` command to the control
  surface — it does NOT kill the sidecar. Killing the sidecar is only for app-exit /
  crash-restart.
- **The UI's truth is the control surface, not logs.** `aizu.worker.control_surface`
  is a loopback-only (`127.0.0.1`) stdlib HTTP server, enabled via
  `AIZU_CONTROL_SURFACE=1` + `AIZU_CONTROL_TOKEN=<token>` +
  `AIZU_CONTROL_PORT` (default `8799`). The app polls `GET /status` and POSTs
  `POST /command`. The app generates the control token per spawn and injects it into the
  child's env.
- **Managed Chrome** mirrors `engine/scripts/warm_chrome.sh` / `aizu.worker.chrome_manager`:
  resolve Playwright's Chrome-for-Testing via the venv python, launch with
  `--remote-debugging-port` + a dedicated `--user-data-dir` + `--no-first-run`
  `--no-default-browser-check`, detect an existing attachable Chrome via a **real
  `connect_over_cdp` probe** (an HTTP 200 on `/json/version` is NOT sufficient — a degraded
  Chrome answers HTTP but rejects CDP), and **never launch a second Chrome / never kill a
  Chrome on sidecar restart** (the warmed login must survive). **Default CDP port is 9333**
  (the live-proven port), NOT 9222.
- **The job channel is outbound HTTPS only** (sidecar → cloud dispatch). It must NEVER
  depend on Tailscale. Tailscale is for ops SSH/RDP only (see §4).

---

## 1. First-run, in order (engineer WITH the toolchain)

Prereqs: Rust stable + `cargo`, `cargo tauri` CLI (`cargo install tauri-cli --version '^2'`),
Python 3.10+ with the engine venv, and a real cloud dispatch reachable (for a local loop,
run the engine server on `127.0.0.1:8765`).

1. **`cargo check` the Rust app** (catch the scaffold's compile errors first):
   ```bash
   cd desktop/src-tauri
   cargo update          # resolve the best-guess version pins
   cargo check
   ```
   Fix API drift against the exact installed Tauri 2.x + plugin versions (plugin builder
   signatures, `Manager`/`Emitter` imports, the POSIX signal shim vs. the `nix` crate, the
   Windows CTRL_BREAK stub).

2. **Generate the icon set** (build blocker — none are checked in):
   ```bash
   cargo tauri icon path/to/aizu-worker-source.png   # >= 1024x1024
   ```

3. **Write `config.toml`** into the app config dir (see §3 for the schema). NO secrets go in
   it. Point `dispatch_base_url` at the real dispatch on `http://127.0.0.1:8765` for a local
   loop, set `cdp_port = 9333`, and a dedicated `chrome_profile_dir`.

4. **`cargo tauri dev` against the real dispatch** — do NOT freeze the sidecar yet. For dev,
   set `sidecar_binary_path` to the `aizu-worker` console script from the editable
   install (`cd engine && pip install -e .` puts it on the venv PATH), and export the
   secrets the child needs (`AIZU_WORKER_BOOTSTRAP_TOKEN`, `OPENROUTER_API_KEY`, …) and
   `AIZU_VENV_PYTHON=<engine>/.venv/bin/python` so the Chrome CDP probe works:
   ```bash
   cd desktop/src-tauri && cargo tauri dev
   ```
   Verify: Chrome attaches (or launches once) on 9333; the sidecar registers and leases; the
   UI shows worker id / accounts / current job from `GET /status`; the log pane follows
   `currentJob.logFilePath`; Pause/Resume/Stop/Focus all round-trip.

5. **Freeze the sidecar with PyInstaller** (see `desktop/pyinstaller/`):
   ```bash
   cd engine
   pip install -e ".[telegram]"          # runtime deps (drop the extra if no Telegram box)
   pip install -r ../desktop/pyinstaller/requirements-build.txt
   pyinstaller --clean ../desktop/pyinstaller/sidecar.spec
   ```
   Iterate on `hiddenimports` / `datas` until the frozen `dist/aizu-worker` actually
   runs a job on each target OS (Playwright driver, cryptography OpenSSL backend, and the
   correct per-OS `keyring` backend are the usual misses). Then point
   `sidecar_binary_path` (or the `tauri.conf.json` `bundle.resources`) at `dist/aizu-worker`.

6. **`cargo tauri build`** to produce the installers (`app`/`dmg`/`msi`/`nsis`). Unsigned
   builds work locally; distribution needs the signing setup in §2.

---

## 2. Blockers (must be resolved before distributable builds)

| # | Blocker | What's needed |
|---|---------|---------------|
| 1 | **macOS signing + notarization** | Apple **Developer ID Application** certificate + an app-specific password / notary API key. Set `bundle.macOS.signingIdentity` in `tauri.conf.json` and run notarization (`xcrun notarytool`). Without it, macOS Gatekeeper blocks the app. |
| 2 | **Windows code signing** | An **Authenticode** cert, ideally **EV** (SmartScreen reputation). Set `bundle.windows.certificateThumbprint`. Without it, SmartScreen warns/blocks. |
| 3 | **Tauri updater signing keypair** | `cargo tauri signer generate` → put the PUBLIC key in `tauri.conf.json` `plugins.updater.pubkey`; the **PRIVATE key NEVER goes in the repo** — it lives in CI secrets (`TAURI_SIGNING_PRIVATE_KEY`). Every released bundle is signed with it or the updater rejects the update. |
| 4 | **Icon set** | `cargo tauri icon <source.png>` — none checked in (`desktop/src-tauri/icons/README.md`). Build fails without them. |
| 5 | **Updater endpoint host** | Stand up the update server / static host that serves the `{{target}}/{{arch}}/{{current_version}}` manifest, and replace the `REPLACE-ME.updates.aizu.example` placeholder endpoint in `tauri.conf.json`. |
| 6 | **Secret delivery to the child** | The scaffold inherits secrets from the app's env; wire a per-OS keychain read (macOS Keychain / Windows Credential Manager / libsecret) before the child spawn so the operator isn't exporting env vars by hand. |

---

## 3. `config.toml` schema (no secrets)

Lives at the OS app-config dir (`app_config_dir()`); parsed by `src-tauri/src/config.rs`.

```toml
# AIZU Worker desktop config — NON-SECRET wiring only.
dispatch_base_url  = "https://cloud.aizu.example"   # outbound HTTPS job channel (NOT loopback)
cdp_port           = 9333                            # live-proven port (NOT 9222)
chrome_profile_dir = "/Users/you/.aizu-cft-profile"  # DEDICATED, non-default profile
sidecar_binary_path = ""                             # empty → resolve from app resources
state_dir          = "/Users/you/.aizu-worker-state" # AIZU_WORKER_STATE
db_path            = "/Users/you/.aizu-worker.db"    # AIZU_DB
control_port       = 8799                            # loopback control surface (default)
```

**Secrets are NOT here.** The control-surface token is generated per spawn and injected as
`AIZU_CONTROL_TOKEN`; `AIZU_WORKER_BOOTSTRAP_TOKEN` and provider keys come from
env/keychain at spawn time.

---

## 4. Tailscale — ops SSH/RDP ONLY, never the job channel

Tailscale is provisioned **out of band** (this doc records the policy; the app has no
Tailscale dependency and must not gain one):

- **Purpose: remote OPS only** — SSH (macOS/Linux) and RDP (Windows) into a managed worker
  PC for maintenance/debugging.
- **The job channel does NOT use Tailscale.** The sidecar reaches the cloud dispatch over
  ordinary outbound HTTPS. If Tailscale is down, jobs must keep flowing. Do not route the
  dispatch URL over a tailnet.
- **Tagged enrollment + ACLs:** enroll each worker with an ACL **tag** (e.g. `tag:aizu-worker`)
  via an auth key, not a user identity. Write ACLs so only the ops group can reach
  `tag:aizu-worker` on the SSH/RDP ports, and workers cannot reach each other or anything
  else. Use Tailscale SSH (or RDP over the tailnet) so access is auditable and key-rotatable.
- Keep the control surface **loopback-only** regardless — it is never exposed on the tailnet.

---

## 5. Residual gaps (still open after this scaffold)

- **Windows / Linux `focus_window`.** macOS focus is real (`osascript ... activate`, no
  process ownership needed — implemented in the sidecar). Windows and Linux focus are
  **logged no-ops** in both the Python (`chrome_manager._unsupported_focus`) and the Rust
  scaffold; the "2FA / focus Chrome" button does nothing there until real per-OS strategies
  land (Win32 `SetForegroundWindow` / `wmctrl`/`xdotool`).
- **Windows CTRL_BREAK child stop.** `sidecar_supervisor.rs` has a stubbed
  `send_ctrl_break` — needs `GenerateConsoleCtrlEvent` via the `windows` crate. Until then
  the graceful phase falls through to `TerminateProcess`.
- **Live warmed-Chrome exit-gate verification.** The reconnect-never-kill invariant
  (attach → don't kill on restart; only kill a Chrome we launched, only on app exit) is
  coded but **unverified on real hardware** end-to-end (sidecar crash → app restart →
  login still warm).
- **Warming + daytime-guard under a frozen binary.** Verify the account-warming path and
  the daytime/quiet-hours guard survive PyInstaller freezing on BOTH macOS and Windows
  (lazy imports, resource paths, subprocess spawning of the job child from inside a frozen
  parent are the risk areas).
- **Capacity override is soft.** `set_capacity_override` clamps + validates but the sidecar
  does not yet expose a `setCapacity` control-surface action; it is presentational until
  that lands. One-live-job-per-(org,platform,account) is still enforced by single-flight.
- **Keychain secret delivery** (see blocker #6) — dev relies on env vars.
- **ffmpeg is not bundled for the packaged sidecar.** The engine's optional gated Uzbek-STT
  tier (Instagram only — see `docs/architecture/engines.md` §1) shells out to `ffmpeg` on
  `PATH` (`core/transcribe.py::extract_audio_wav`) to extract a WAV from a reel's video
  before transcription. That's confirmed present on the dev machine's `PATH` only; it is
  **not** bundled into the PyInstaller onedir tree by `sidecar.spec`, and the frozen
  `aizu-worker` binary does not inherit the dev machine's `PATH`. Until ffmpeg is bundled
  (or a static binary path is wired into the frozen app's env), the STT tier fails soft on
  any packaged worker — `extract_audio_wav` just returns `False` (no transcript, no
  crash) — this is a silently-degraded feature there, not a build blocker.
