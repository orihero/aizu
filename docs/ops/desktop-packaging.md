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
  (the live-proven port), NOT 9222. All three also derive the `--user-data-dir` the same way
  (§3.1): the configured path is a **base**, and the directory actually opened is
  `<base>/<brand>` — so two browser brands can never open one profile.
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
   loop, set `cdp_port = 9333`, and a `chrome_profile_dir` — the profile **base**; the app
   opens `<base>/<brand>` under it, so the base does not need to be new (§3.1). If it already
   holds a `Default/` of its own that is a pre-derivation profile: leave it, the app will
   describe it and warm a fresh brand dir beside it.

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
chrome_profile_dir = "/Users/you/.aizu-cft-profile"  # profile BASE; opens <base>/<brand> (§3.1)
sidecar_binary_path = ""                             # empty → resolve from app resources
state_dir          = "/Users/you/.aizu-worker-state" # AIZU_WORKER_STATE
db_path            = "/Users/you/.aizu-worker.db"    # AIZU_DB
control_port       = 8799                            # loopback control surface (default)
```

**Secrets are NOT here.** The control-surface token is generated per spawn and injected as
`AIZU_CONTROL_TOKEN`; `AIZU_WORKER_BOOTSTRAP_TOKEN` and provider keys come from
env/keychain at spawn time.

> ⚠ **A Chrome profile belongs to exactly one browser BRAND — crossing them wipes the
> operator's logins.** The app launches Playwright's **Chrome for Testing**, which on macOS
> reads the Keychain item `Chromium Safe Storage`; system Google Chrome writes
> `Chrome Safe Storage`. Point one brand at a profile the other warmed and cookie decryption
> fails — and Chrome **deletes** the undecryptable rows instead of quarantining them. Measured
> on a clone of a warmed profile: 18 cookies → 0, live Instagram `sessionid` included,
> unrecoverable afterwards by any browser (ledger A9; the version gap is NOT the cause — the
> same build with `--use-mock-keychain` loses everything too, and the downgrade move-aside
> machinery is Windows-only).
>
> `chrome_profile_dir` is therefore a **base**, not the profile: every launch site opens
> `<base>/<brand>` (§3.1). The trap it removes is the default — `~/.aizu-cft-profile` is a
> *name*, not a guarantee, and on a box where the operator warmed that dir by hand with system
> Chrome the wizard's "Download browser" → "Launch warmed Chrome" sequence used to point
> Chrome for Testing straight at it.

### 3.1 The profile directory is derived from the brand — one contract, three launch sites

A profile is owned by one brand, so the **directory is a function of the brand**:

```
profile_dir_for(base, brand) = <base>/<brand>

  ~/.aizu-cft-profile/chrome-for-testing    ← Playwright's Chrome for Testing opens this
  ~/.aizu-cft-profile/chrome                ← system Google Chrome opens this
```

The path **is** the ownership record. There is nothing to mark, nothing to police, no refusal
to survive and no question anybody can answer wrong — the cross-brand open is not prevented,
it is unreachable. That replaces the `.aizu-browser-brand` marker, its decision table, its
refusal and the wizard's brand declaration, all of which are **deleted** (ledger A12: three
rounds of guard, each fixing the last round's hole and opening a new one).

Brand detection is by binary **path** — symlinks resolved first, then lowercased with `\`
normalised to `/` — and it takes three rules where an obvious implementation takes one:

1. path contains `chrome for testing` (case-insensitive) → `chrome-for-testing`;
2. any path **segment** matching `^chromium(_headless_shell)?-[0-9]+$` → `chrome-for-testing`
   (Playwright's browsers-cache dir);
3. otherwise → `chrome`.

Rule 2 is not redundant: rule 1 only ever fires on macOS, where Playwright's build is a
`Google Chrome for Testing.app`. The same build installs as `chrome-linux64/chrome`,
`chrome-linux/chrome` and `chrome-win64\chrome.exe` (the driver's own `EXECUTABLE_PATHS`
table), so on Linux/Windows a substring check alone would label CfT `chrome` and send it into
the system-Chrome directory. Symlinks are resolved *before* rule 1 because a wrapper or a
`/usr/local/bin/google-chrome` symlink into the Playwright cache is exactly how a CfT binary
reaches a launch site wearing another name.

The three launch sites implement the identical derivation and must not drift, or the shell
warms one directory while the worker opens another and the operator is asked to sign in again
on a box that was already warmed: `desktop/src-tauri/src/chrome_manager.rs`,
`engine/aizu/worker/chrome_manager.py`, `engine/scripts/warm_chrome.sh`. The bash half is
exercised without a browser — `AIZU_WARM_CHROME_LIB=1 source engine/scripts/warm_chrome.sh`
defines its functions and returns before `main` — and `engine/tests/test_warm_chrome_sh.py`
asserts bash and Python agree path-for-path.

**One variable name, one default, repo-wide.** Two spellings of the same setting is how a
check ends up watching a directory nothing warms (A12):

| setting | canonical | default | the other spelling |
|---|---|---|---|
| profile **base** | `AIZU_CHROME_PROFILE` | `~/.aizu-cft-profile` | `AIZU_CHROME_PROFILE_DIR` is **gone**. It was read only by the worker, and only it defaulted to `~/.aizu-chrome-profile` — a directory nothing in this repo ever warms, which is how the preflight's profile row came to watch an empty dir on every box. |
| Chrome binary override | `AIZU_CHROME_BINARY` | unset → Playwright CfT → system Chrome | `CHROME_BIN` is still read, after the canonical name. It is what every runbook and the desktop shell have said for months, and it is one `elif`; dropping it would silently move an operator's pinned browser — and with it their profile directory. |

The desktop app's `chrome_profile_dir` in `config.toml` is the same base by another route.

**The legacy profile.** A base that itself holds a `Default/` is a profile from *before* this
change, warmed by an unknown brand. It is never opened, never moved, never renamed, never
copied, never backed up and never deleted. Every launch site surfaces it **once,
informationally, never blocking**: it says the directory is there, that it was left untouched,
that whichever browser warmed it still owns it, and that an operator who *knows* which browser
that was can move it into the matching subdirectory themselves — printing **both** candidate
destinations so the move is a copy-paste. No component guesses which brand warmed it. Guessing
is the whole reason the earlier design failed, and a wrong guess costs every saved login in
that directory, unrecoverably.

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
- **Chrome-for-Testing is downloaded at setup, not bundled** (ledger A6). The `.app` ships no
  browser (that would add ~356 MB to every build and every update). Instead `run_sidecar.py` pins
  `PLAYWRIGHT_BROWSERS_PATH` to the standard per-user cache — macOS `~/Library/Caches/ms-playwright`,
  Linux `~/.cache/ms-playwright`, Windows `%USERPROFILE%\AppData\Local\ms-playwright` — because a
  FROZEN Playwright otherwise forces `PLAYWRIGHT_BROWSERS_PATH=0` and looks inside the bundle, where
  no browser exists. The wizard's Chrome step downloads it with the bundled Node driver
  (`-m aizu.worker.chrome_path --install`), so the box needs no Python and no pip.
  - The pin is a `setdefault`: ops can still point a box at a pre-seeded cache, which is the
    airgapped path (copy a populated `ms-playwright` dir and set the variable).
  - Because the cache is per-user and OUTSIDE the bundle, the browser survives app updates.
  - **Verify a packaged build with:** `cd / && <app>/Contents/Resources/sidecar/aizu-worker/aizu-worker
    -m aizu.worker.chrome_path` — it must print a path and exit 0. Run it from OUTSIDE the repo;
    inside, the dev venv is reachable and masks the very failure you are testing for.
- **ffmpeg is not bundled for the packaged sidecar.** The engine's optional gated Uzbek-STT
  tier (Instagram only — see `docs/architecture/engines.md` §1) shells out to `ffmpeg` on
  `PATH` (`core/transcribe.py::extract_audio_wav`) to extract a WAV from a reel's video
  before transcription. That's confirmed present on the dev machine's `PATH` only; it is
  **not** bundled into the PyInstaller onedir tree by `sidecar.spec`, and the frozen
  `aizu-worker` binary does not inherit the dev machine's `PATH`. Until ffmpeg is bundled
  (or a static binary path is wired into the frozen app's env), the STT tier fails soft on
  any packaged worker — `extract_audio_wav` just returns `False` (no transcript, no
  crash) — this is a silently-degraded feature there, not a build blocker.
