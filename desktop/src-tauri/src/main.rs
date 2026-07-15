//! AIZU Worker — desktop shell entry point (BUILD-PLAN Phase 6, C3 option A).
//!
//! # SCAFFOLD SOURCE ONLY — UNCOMPILED
//! No cargo/rustc in this environment. This file is written to Tauri 2.x conventions but
//! has NOT been type-checked. An engineer with the toolchain must run `cargo check` and
//! `cargo tauri dev`; expect to adjust plugin builder calls and API surfaces to the exact
//! installed crate versions.
//!
//! # What this app is (and is NOT)
//! A thin Tauri shell that runs on a managed worker PC and supervises the Python
//! **`reelradar-worker`** sidecar binary (= `reelradar.worker.sidecar:main`) as a managed
//! child process — **BUILD-PLAN Phase 6, C3 option A**: restart-on-crash watchdog +
//! run-at-login. It feeds the child env vars only.
//!
//! It **NEVER**:
//!   - shells out to `reelradar.cli`,
//!   - starts a `RunManager` (there is none on the box — the sidecar bypasses it, C3),
//!   - scrapes logs to infer state (superseded — the control surface is the source of truth),
//!   - kills the sidecar to stop a single job (that is a `stopCurrentJob` command; see below),
//!   - launches a second Chrome or kills a Chrome it did not launch.
//!
//! # Responsibilities (startup order)
//! 1. **Resolve config** from an on-disk TOML at `app_config_dir` (`config.rs`). NO secrets
//!    are baked in or persisted there.
//! 2. **Start Chrome** via `ChromeManager` (`chrome_manager.rs`): reuse an attachable Chrome
//!    if one is already up (real CDP probe), else launch ONE with a dedicated user-data-dir.
//!    Default CDP port is **9333** (the live-proven port), NOT 9222.
//! 3. **Spawn the supervised sidecar** (`sidecar_supervisor.rs`), passing `REELRADAR_*` env:
//!    - `REELRADAR_DISPATCH_URL` (from config),
//!    - `REELRADAR_CONTROL_SURFACE=1`,
//!    - `REELRADAR_CONTROL_TOKEN=<generated-per-spawn token>` (also handed to `control_client`),
//!    - `REELRADAR_CONTROL_PORT=<config.control_port>`,
//!    - `REELRADAR_CDP_URL=http://127.0.0.1:<config.cdp_port>` (matches the Chrome we started),
//!    - `REELRADAR_DB`, `REELRADAR_WORKER_STATE`, `REELRADAR_CONFIG` (paths from config).
//!    The worker's own bootstrap token / provider keys come from env or the OS keychain at
//!    spawn time — never from the TOML.
//! 4. **Start the /status poller + log tail**: `control_client` polls `GET /status` on an
//!    interval and emits a `status-updated` Tauri event; `log_tail` follows
//!    `currentJob.logFilePath` returned by /status and emits a `log-line` event.
//! 5. **Register the Tauri commands** (`commands.rs`) the UI invokes.
//! 6. **On exit**: gracefully stop the sidecar (SIGTERM → wait → SIGKILL) THEN kill Chrome —
//!    but only a Chrome WE launched (`ChromeManager::kill_on_app_exit`). A Chrome we merely
//!    attached to must outlive us so the warmed login survives.
//!
//! # Mid-run stop
//! The Python layer runs each job in a killable child process; a halt SIGTERM→SIGKILLs it.
//! So the UI "Stop" is a `stopCurrentJob` command POSTed to the control surface (via
//! `control_client`), NOT a sidecar kill. Killing the sidecar process is reserved for
//! app-exit / crash-restart.

mod chrome_manager;
mod commands;
mod config;
mod control_client;
mod errors;
mod log_tail;
mod sidecar_supervisor;

use std::sync::Arc;

use chrome_manager::ChromeManager;
use config::DesktopConfig;
use control_client::ControlClient;
use errors::DesktopError;
use sidecar_supervisor::SidecarSupervisor;
use tauri::Manager;  // brings manage() + app_handle() trait methods into scope
use tokio::sync::Mutex;

/// Shared application state handed to every `#[tauri::command]`.
///
/// Immutable wiring (`config`) plus the long-lived actors behind async mutexes. Commands
/// never reach into raw process handles — they go through these actors, which own the
/// invariants (never-second-Chrome, reconnect-never-kill, token confidentiality).
pub struct AppState {
    pub config: Arc<DesktopConfig>,
    /// The per-spawn loopback control-surface bearer token. Held in memory only; NEVER
    /// logged, NEVER surfaced to the UI, NEVER written to the TOML.
    pub control_token: Arc<String>,
    pub control: Arc<ControlClient>,
    pub supervisor: Arc<Mutex<SidecarSupervisor>>,
    pub chrome: Arc<Mutex<ChromeManager>>,
}

/// How often the shell polls `GET /status`. The control surface is cheap and loopback-only,
/// so a short cadence keeps the UI responsive without hammering anything real.
const STATUS_POLL_INTERVAL_MS: u64 = 1_500;

fn main() {
    // A crash-tolerant boot: any startup failure is reported to the UI rather than
    // panicking the process (the operator must see WHY the worker didn't come up).
    if let Err(e) = run() {
        eprintln!("[aizu-worker] fatal startup error: {e}");
        std::process::exit(1);
    }
}

/// The fallible startup, extracted so `setup` can catch its errors and STILL open the
/// window. Loads config, wires the actors, manages state, and starts the pollers. Chrome +
/// the sidecar are only started when a dispatch URL is actually configured (a first-run box
/// with no `config.toml` opens in a "not configured" state instead of failing).
fn init_app(app: &mut tauri::App) -> Result<(), DesktopError> {
    let handle = app.handle().clone();

    // Config (first run with no config.toml → defaults with an EMPTY dispatch URL).
    let config = Arc::new(config::load(&handle)?);
    let configured = !config.dispatch_base_url.trim().is_empty();

    let control_token = Arc::new(control_client::generate_token());
    let control = Arc::new(ControlClient::new(config.control_port, control_token.clone()));
    let supervisor = Arc::new(Mutex::new(SidecarSupervisor::new(
        config.clone(),
        control_token.clone(),
        handle.clone(),
    )));
    let mut chrome = ChromeManager::new(config.clone());

    // Chrome is best-effort and only when configured — a launch/attach failure must NOT
    // stop the window from opening (the UI shows a disconnected Chrome badge instead).
    if configured {
        if let Err(e) = chrome.ensure_running() {
            eprintln!("[aizu-worker] Chrome not ready (continuing): {e}");
        }
    }

    app.manage(AppState {
        config: config.clone(),
        control_token: control_token.clone(),
        control: control.clone(),
        supervisor: supervisor.clone(),
        chrome: Arc::new(Mutex::new(chrome)),
    });

    // Start the supervised sidecar only when there is a dispatch to lease from.
    if configured {
        let sup = supervisor.clone();
        tauri::async_runtime::spawn(async move { sup.lock().await.start().await; });
    } else {
        eprintln!("[aizu-worker] no dispatch_base_url configured — edit config.toml \
                   (a config.example.toml was written next to it); sidecar not started");
    }

    // Status poller + log tail always run so the UI reflects live state.
    let poll_handle = handle.clone();
    let poll_control = control.clone();
    tauri::async_runtime::spawn(async move {
        control_client::run_status_poller(poll_control, poll_handle, STATUS_POLL_INTERVAL_MS).await;
    });
    let tail_handle = handle.clone();
    let tail_control = control.clone();
    tauri::async_runtime::spawn(async move {
        log_tail::run_log_tail(tail_control, tail_handle).await;
    });
    Ok(())
}

fn run() -> Result<(), DesktopError> {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_autostart::init(
            // Run-at-login (C3 option A). MacosLauncher chosen in tauri.conf.json.
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
        .setup(|app| {
            // The window is defined in tauri.conf.json and opens regardless of what
            // happens here. Startup work is therefore NON-FATAL: any failure is logged and
            // surfaced in the UI (via the status poller), never propagated out of setup —
            // otherwise Tauri aborts run() and the app exits with no visible window.
            if let Err(e) = init_app(app) {
                eprintln!("[aizu-worker] startup incomplete (window still opens): {e}");
            }
            // Open MAXIMIZED (fills the work area) — the full-screen operator dashboard.
            // Done here in Rust because Tauri 2's window config has no `maximized` key.
            // Non-fatal: a failure just leaves the window at its configured size.
            if let Some(win) = app.get_webview_window("main") {
                if let Err(e) = win.maximize() {
                    eprintln!("[aizu-worker] could not maximize window: {e}");
                }
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::get_status,
            commands::pause,
            commands::resume,
            commands::stop_current_job,
            commands::focus_chrome,
            commands::restart_sidecar,
            commands::set_capacity_override,
            commands::get_config,
            commands::save_config,
        ])
        .on_window_event(|window, event| {
            // On the last window close / app exit: stop the sidecar gracefully, THEN kill
            // only a Chrome we launched. Order matters — the sidecar must detach from CDP
            // before Chrome goes away, and an attached-only Chrome is left running.
            if let tauri::WindowEvent::Destroyed = event {
                let app = window.app_handle().clone();
                tauri::async_runtime::block_on(async move {
                    if let Some(state) = app.try_state::<AppState>() {
                        state.supervisor.lock().await.stop_gracefully().await;
                        state.chrome.lock().await.kill_on_app_exit();
                    }
                });
            }
        })
        .run(tauri::generate_context!())
        .map_err(|e| DesktopError::ConfigInvalid(format!("tauri run failed: {e}")))
}
