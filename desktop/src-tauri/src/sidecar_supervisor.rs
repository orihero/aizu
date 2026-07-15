//! Supervised `reelradar-worker` child process (Phase 6 SCAFFOLD, UNCOMPILED).
//!
//! BUILD-PLAN Phase 6, **C3 option A**: the desktop app supervises the Python sidecar
//! binary as a managed child — restart-on-crash watchdog + run-at-login. It spawns
//! **`reelradar-worker`** (= `reelradar.worker.sidecar:main`), feeding it `REELRADAR_*`
//! env vars only. It NEVER shells to `reelradar.cli` and NEVER starts a `RunManager`.
//!
//! # Restart policy (mirrors sidecar.py `_backoff`)
//! On an unexpected child exit we relaunch with a jittered exponential backoff — base
//! `0.5s`, cap `30.0s` — the SAME shape as the Python lease-miss backoff. The attempt
//! counter RESETS after the child has stayed healthy for `MIN_HEALTHY_UPTIME_SEC = 60`,
//! so a crash-loop backs off while a rare crash after a long healthy run restarts fast.
//!
//! # Stopping (two very different reasons)
//! - **Stop ONE job**: NOT this module's job. That is a `stopCurrentJob` command to the
//!   control surface; the Python layer force-terminates the killable job child. The
//!   sidecar process keeps running.
//! - **Stop the sidecar process** (`stop_gracefully`): only for app-exit / crash-restart.
//!   SIGTERM (POSIX) / CTRL_BREAK (Windows) → wait `STOP_GRACE_SEC` → SIGKILL.
//!
//! An immutable `SidecarStatus` snapshot is emitted to the UI via a Tauri event on every
//! state change (starting / running / crashed / backing-off / stopped).

use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager};
use tokio::process::{Child, Command};

use crate::config::SharedConfig;
use crate::errors::DesktopError;

/// Tauri event carrying each `SidecarStatus` snapshot to the UI.
pub const SIDECAR_STATUS_EVENT: &str = "sidecar-status";

/// Backoff shape — identical to sidecar.py's `_BACKOFF_BASE_SEC` / `_BACKOFF_CAP_SEC`.
const BACKOFF_BASE_SEC: f64 = 0.5;
const BACKOFF_CAP_SEC: f64 = 30.0;
/// Reset the crash-backoff counter after this much continuous healthy uptime.
const MIN_HEALTHY_UPTIME_SEC: u64 = 60;
/// Grace between SIGTERM/CTRL_BREAK and SIGKILL on `stop_gracefully`.
const STOP_GRACE_SEC: u64 = 8;

/// The console-script entry point name (matches `[project.scripts] reelradar-worker`).
const SIDECAR_BINARY_NAME: &str = "reelradar-worker";

/// Immutable snapshot of the supervised child's lifecycle, pushed to the UI.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SidecarStatus {
    /// "starting" | "running" | "crashed" | "backing_off" | "stopping" | "stopped".
    pub state: String,
    pub pid: Option<u32>,
    /// Populated in "backing_off": seconds until the next restart attempt.
    pub restart_in_sec: Option<f64>,
    /// Populated in "crashed"/"stopped": the child's exit code, if any.
    pub last_exit_code: Option<i32>,
    pub restart_count: u32,
}

impl SidecarStatus {
    fn new(state: &str) -> Self {
        Self {
            state: state.to_string(),
            pid: None,
            restart_in_sec: None,
            last_exit_code: None,
            restart_count: 0,
        }
    }
}

pub struct SidecarSupervisor {
    config: SharedConfig,
    /// The per-spawn control-surface token, injected as `REELRADAR_CONTROL_TOKEN`.
    control_token: Arc<String>,
    app: AppHandle,
    child: Option<Child>,
    /// Set true by `stop_gracefully` so the watchdog does NOT restart an intentional stop.
    shutting_down: bool,
    restart_count: u32,
}

impl SidecarSupervisor {
    pub fn new(config: SharedConfig, control_token: Arc<String>, app: AppHandle) -> Self {
        Self {
            config,
            control_token,
            app,
            child: None,
            shutting_down: false,
            restart_count: 0,
        }
    }

    /// Spawn the child, then run the restart-on-crash watchdog until `stop_gracefully`.
    /// Each spawn/exit transition emits a `SidecarStatus`.
    pub async fn start(&mut self) {
        loop {
            if self.shutting_down {
                return;
            }
            self.emit(SidecarStatus::new("starting"));
            match self.spawn_child() {
                Ok(()) => {}
                Err(e) => {
                    eprintln!("[supervisor] spawn failed: {e}");
                    // Treat a spawn failure like a crash: back off and retry.
                    self.backoff_and_maybe_reset(None).await;
                    continue;
                }
            }
            let started = Instant::now();
            {
                let mut running = SidecarStatus::new("running");
                running.pid = self.child.as_ref().and_then(|c| c.id());
                running.restart_count = self.restart_count;
                self.emit(running);
            }

            // Wait for the child to exit.
            let exit_code = self.wait_for_exit().await;

            if self.shutting_down {
                self.emit(SidecarStatus::new("stopped"));
                return;
            }

            // Unexpected exit → crash path.
            let mut crashed = SidecarStatus::new("crashed");
            crashed.last_exit_code = exit_code;
            crashed.restart_count = self.restart_count;
            self.emit(crashed);

            // Reset the backoff counter if the child had been healthy long enough.
            if started.elapsed() >= Duration::from_secs(MIN_HEALTHY_UPTIME_SEC) {
                self.restart_count = 0;
            }
            self.backoff_and_maybe_reset(exit_code).await;
        }
    }

    /// Build the child `Command` with the full `REELRADAR_*` env and spawn it.
    fn spawn_child(&mut self) -> Result<(), DesktopError> {
        let binary = self.resolve_binary()?;
        let mut cmd = Command::new(&binary);

        // --- Env vars only. NO CLI args, NO RunManager. -----------------------------
        // Non-secret wiring from config:
        cmd.env("REELRADAR_DISPATCH_URL", &self.config.dispatch_base_url);
        cmd.env("REELRADAR_CDP_URL", self.config.cdp_url()); // matches the Chrome we launched
        cmd.env("REELRADAR_WORKER_STATE", &self.config.state_dir);
        cmd.env("REELRADAR_DB", &self.config.db_path);
        // Capability declaration: which platforms this box advertises it can run. Without
        // it the worker registers with NO capabilities and the fleet dispatch rejects every
        // run as "no capable worker". "all" → every supported platform, pool-wide.
        cmd.env("REELRADAR_WORKER_PLATFORMS", &self.config.worker_platforms);
        // Control surface: enable it, pass the generated token + the port.
        cmd.env("REELRADAR_CONTROL_SURFACE", "1");
        cmd.env("REELRADAR_CONTROL_TOKEN", self.control_token.as_str());
        cmd.env("REELRADAR_CONTROL_PORT", self.config.control_port.to_string());

        // The worker bootstrap token (a SECRET, never in config.toml) comes from the 0600
        // token file the dev menu writes; pass it to the child as the register bearer.
        if let Some(token) = crate::config::read_bootstrap_token(&self.app) {
            cmd.env("REELRADAR_WORKER_BOOTSTRAP_TOKEN", token);
        }

        // Engine/provider secrets (OPENROUTER_API_KEY, REELRADAR_SECRET_KEY, model/platform
        // overrides, …) from the 0600 worker-secrets.env file. A Finder-launched GUI worker
        // inherits only a minimal launchd environment, so these can NOT be assumed present —
        // without OPENROUTER_API_KEY every live run dies at the engine's `_build_run_io`
        // guard and the job completes with zero leads (the "fleet can't run campaigns" bug).
        // Injected LAST so an operator-supplied value wins over anything inherited; values
        // are never logged.
        for (key, val) in crate::config::read_worker_secrets(&self.app) {
            cmd.env(key, val);
        }

        // Windows: a new process group so we can send CTRL_BREAK to the child alone.
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
            cmd.creation_flags(CREATE_NEW_PROCESS_GROUP);
        }

        let child = cmd
            .spawn()
            .map_err(|e| DesktopError::SidecarSpawnFailed(format!("{}: {e}", binary.display())))?;
        self.child = Some(child);
        Ok(())
    }

    /// Resolve the sidecar binary: explicit config path wins; else fall back to the
    /// bundled resource name next to the app. (The scaffold uses the config path or the
    /// bare name on PATH; the resource-dir resolution is wired at packaging time.)
    fn resolve_binary(&self) -> Result<PathBuf, DesktopError> {
        // 1. Explicit config override wins.
        if !self.config.sidecar_binary_path.as_os_str().is_empty() {
            if self.config.sidecar_binary_path.exists() {
                return Ok(self.config.sidecar_binary_path.clone());
            }
            return Err(DesktopError::SidecarSpawnFailed(format!(
                "configured sidecar_binary_path does not exist: {}",
                self.config.sidecar_binary_path.display()
            )));
        }
        // 2. Bundled resource (packaged app). The onedir PyInstaller build ships as
        //    <resource_dir>/sidecar/reelradar-worker/reelradar-worker (folder + executable).
        //    Fall back to a flat layout in case the resource copy flattened it.
        if let Ok(rd) = self.app.path().resource_dir() {
            let nested = rd.join("sidecar").join(SIDECAR_BINARY_NAME).join(SIDECAR_BINARY_NAME);
            if nested.exists() {
                return Ok(nested);
            }
            let flat = rd.join("sidecar").join(SIDECAR_BINARY_NAME);
            if flat.exists() {
                return Ok(flat);
            }
        }
        // 3. Dev fallback: rely on the binary being on PATH.
        Ok(PathBuf::from(SIDECAR_BINARY_NAME))
    }

    /// Await the child's exit, returning its exit code if available.
    async fn wait_for_exit(&mut self) -> Option<i32> {
        if let Some(child) = self.child.as_mut() {
            match child.wait().await {
                Ok(status) => status.code(),
                Err(_) => None,
            }
        } else {
            None
        }
    }

    /// Jittered exponential backoff before the next restart attempt, capped, mirroring
    /// sidecar.py `_backoff`. Emits a "backing_off" status with the wait it chose.
    async fn backoff_and_maybe_reset(&mut self, exit_code: Option<i32>) {
        self.restart_count = self.restart_count.saturating_add(1);
        let ceiling = (BACKOFF_BASE_SEC * 2f64.powi(self.restart_count as i32)).min(BACKOFF_CAP_SEC);
        let jitter = rand::random::<f64>();
        let wait = BACKOFF_BASE_SEC + jitter * (ceiling - BACKOFF_BASE_SEC).max(0.0);

        let mut s = SidecarStatus::new("backing_off");
        s.restart_in_sec = Some(wait);
        s.last_exit_code = exit_code;
        s.restart_count = self.restart_count;
        self.emit(s);

        tokio::time::sleep(Duration::from_secs_f64(wait)).await;
    }

    /// Stop the sidecar PROCESS gracefully. ONLY for app-exit / crash-restart — never to
    /// stop a single job (that is a control-surface `stopCurrentJob`). Sets `shutting_down`
    /// so the watchdog does not relaunch, sends SIGTERM/CTRL_BREAK, waits the grace, then
    /// SIGKILLs anything still alive.
    pub async fn stop_gracefully(&mut self) {
        self.shutting_down = true;
        self.emit(SidecarStatus::new("stopping"));
        let Some(child) = self.child.as_mut() else {
            return;
        };
        let Some(pid) = child.id() else {
            return;
        };

        // Ask nicely first.
        #[cfg(unix)]
        {
            // SAFETY: kill(2) with SIGTERM on our own child's pid.
            unsafe {
                libc_kill(pid as i32, SIGTERM);
            }
        }
        #[cfg(windows)]
        {
            // CTRL_BREAK to the child's process group (we created it with a new group).
            send_ctrl_break(pid);
        }

        // Wait up to the grace period for a clean exit.
        let deadline = Instant::now() + Duration::from_secs(STOP_GRACE_SEC);
        loop {
            if Instant::now() >= deadline {
                let _ = child.start_kill(); // SIGKILL / TerminateProcess
                break;
            }
            match child.try_wait() {
                Ok(Some(_)) => break, // exited cleanly
                Ok(None) => tokio::time::sleep(Duration::from_millis(200)).await,
                Err(_) => {
                    let _ = child.start_kill();
                    break;
                }
            }
        }
        let _ = child.wait().await;
        self.child = None;
        self.emit(SidecarStatus::new("stopped"));
    }

    fn emit(&self, status: SidecarStatus) {
        let _ = self.app.emit(SIDECAR_STATUS_EVENT, &status);
    }
}

// --- POSIX signal shims -------------------------------------------------------------
// tokio's Child::start_kill sends SIGKILL; there is no built-in SIGTERM, so we FFI to
// kill(2) for the graceful phase. (An engineer may prefer the `nix` crate — kept minimal
// here to avoid another dependency in the scaffold.)
#[cfg(unix)]
const SIGTERM: i32 = 15;

#[cfg(unix)]
extern "C" {
    #[link_name = "kill"]
    fn libc_kill(pid: i32, sig: i32) -> i32;
}

#[cfg(windows)]
fn send_ctrl_break(_pid: u32) {
    // Windows CTRL_BREAK delivery to a child process group requires
    // GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid). Left as a documented stub for the
    // engineer with the toolchain (needs the `windows`/`winapi` crate + a console handle).
    // Until wired, the grace loop falls through to start_kill() (TerminateProcess).
}
