//! Supervised `aizu-worker` child process (BUILD-PLAN Phase 6).
//!
//! BUILD-PLAN Phase 6, **C3 option A**: the desktop app supervises the Python sidecar
//! binary as a managed child — restart-on-crash watchdog + run-at-login. It spawns
//! **`aizu-worker`** (= `aizu.worker.sidecar:main`), feeding it `AIZU_*`
//! env vars only. It NEVER shells to `aizu.cli` and NEVER starts a `RunManager`.
//!
//! # Restart policy (mirrors sidecar.py `_backoff`)
//! On an unexpected child exit we relaunch with a jittered exponential backoff — base
//! `0.5s`, cap `30.0s` — the SAME shape as the Python lease-miss backoff. The attempt
//! counter RESETS after the child has stayed healthy for `MIN_HEALTHY_UPTIME_SEC = 60`,
//! so a crash-loop backs off while a rare crash after a long healthy run restarts fast.
//!
//! # Stopping (three very different reasons)
//! - **Stop ONE job**: NOT this module's job. That is a `stopCurrentJob` command to the
//!   control surface; the Python layer force-terminates the killable job child. The
//!   sidecar process keeps running.
//! - **Restart the sidecar** (`restart`): the setup wizard needs this after every write to
//!   `worker-secrets.env` / `dispatch-token.secret` / `config.toml`, because the child
//!   reads its environment ONCE at spawn. It kills the child WITHOUT setting
//!   `shutting_down`, so the watchdog's own crash path relaunches it with the new env —
//!   no app relaunch, no second supervisor.
//! - **Stop the sidecar process** (`stop_gracefully`): only for app exit.
//!   SIGTERM (POSIX) / CTRL_BREAK (Windows) → wait `STOP_GRACE_SEC` → SIGKILL.
//!
//! # Concurrency: `Arc<Self>` + interior mutability, NOT `Arc<Mutex<Self>>`
//! The watchdog runs for the whole life of the app. Wrapping the supervisor in an outer
//! mutex (as the first scaffold did) meant `supervisor.lock().await.start().await` held
//! that mutex forever, so EVERY command that wanted the supervisor — Restart worker, and
//! now every wizard step that must respawn the child — would have blocked on it for good.
//! State therefore lives in atomics plus one short-lived `Mutex<Option<Child>>`, and the
//! watchdog polls `try_wait()` on a 250ms cadence rather than parking inside
//! `child.wait().await` (which would hold that lock until the child died — the same
//! deadlock one level down). Polling a process handle every 250ms costs nothing measurable
//! and keeps the operator's Stop/Restart always answerable.
//!
//! An immutable `SidecarStatus` snapshot is emitted to the UI via a Tauri event on every
//! state change (starting / running / crashed / backing-off / stopped).

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager};
use tokio::process::{Child, Command};
use tokio::sync::Mutex;

use crate::config::SharedConfigCell;
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
/// How often the watchdog asks whether the child is still alive. See the module note on
/// why this is a poll and not a parked `wait()`.
const CHILD_POLL_MS: u64 = 250;

/// The console-script entry point name (matches `[project.scripts] aizu-worker`).
const SIDECAR_BINARY_NAME: &str = "aizu-worker";

/// Immutable snapshot of the supervised child's lifecycle, pushed to the UI.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SidecarStatus {
    /// "starting" | "running" | "crashed" | "backing_off" | "stopping" | "stopped"
    /// | "not_configured".
    pub state: String,
    pub pid: Option<u32>,
    /// Populated in "backing_off": seconds until the next restart attempt.
    pub restart_in_sec: Option<f64>,
    /// Populated in "crashed"/"stopped": the child's exit code, if any.
    pub last_exit_code: Option<i32>,
    pub restart_count: u32,
    /// Populated when a spawn failed outright (missing binary, permission denied). This is
    /// the message the UI shows — it used to exist only as an `eprintln!` on a GUI box
    /// with no terminal attached, which is how a worker that never started looked
    /// identical to one that was merely idle.
    pub detail: Option<String>,
}

impl SidecarStatus {
    fn new(state: &str) -> Self {
        Self {
            state: state.to_string(),
            pid: None,
            restart_in_sec: None,
            last_exit_code: None,
            restart_count: 0,
            detail: None,
        }
    }
}

pub struct SidecarSupervisor {
    config: SharedConfigCell,
    /// The per-spawn control-surface token, injected as `AIZU_CONTROL_TOKEN`.
    control_token: Arc<String>,
    app: AppHandle,
    /// Locked only for the microseconds it takes to spawn / poll / signal. NEVER held
    /// across a sleep or a `wait()`.
    child: Mutex<Option<Child>>,
    /// Set true by `stop_gracefully` so the watchdog does NOT restart an intentional stop.
    /// `restart` deliberately leaves it false — that is the whole difference between them.
    shutting_down: AtomicBool,
    /// True while a watchdog task is alive. Guards against two watchdogs racing to spawn
    /// two sidecars (which would double-register the box).
    watchdog_live: AtomicBool,
    /// Set by `restart` to cut a pending crash-backoff short. Without it, an operator who
    /// saves a fix while the box is deep in a crash-loop waits out the full 30s ceiling
    /// before their change is even attempted — and reads that as "the app ignored me".
    wake: AtomicBool,
    restart_count: AtomicU32,
}

impl SidecarSupervisor {
    pub fn new(config: SharedConfigCell, control_token: Arc<String>, app: AppHandle) -> Self {
        Self {
            config,
            control_token,
            app,
            child: Mutex::new(None),
            shutting_down: AtomicBool::new(false),
            watchdog_live: AtomicBool::new(false),
            wake: AtomicBool::new(false),
            restart_count: AtomicU32::new(0),
        }
    }

    /// Start the watchdog if it is not already running. Idempotent and cheap — the wizard
    /// calls it after the operator supplies a dispatch URL on a box that booted with none,
    /// so the sidecar comes up WITHOUT an app relaunch.
    ///
    /// Does nothing while the box has no dispatch URL: a sidecar with no
    /// `AIZU_DISPATCH_URL` exits immediately and would crash-loop behind the wizard.
    ///
    /// An associated function rather than a method: it needs the `Arc` to hand to the
    /// spawned task, and `self: &Arc<Self>` is not a receiver type stable Rust accepts.
    pub fn ensure_started(this: &Arc<Self>) {
        if !this.config.get().is_configured() {
            let mut s = SidecarStatus::new("not_configured");
            s.detail = Some("No dispatch URL yet — finish Setup → Connect.".into());
            this.emit(s);
            return;
        }
        // compare_exchange, not a load-then-store: two wizard clicks 10ms apart must not
        // both win and spawn two sidecars.
        if this
            .watchdog_live
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_err()
        {
            return;
        }
        this.shutting_down.store(false, Ordering::SeqCst);
        let me = this.clone();
        tauri::async_runtime::spawn(async move {
            let flag = me.clone();
            Self::watchdog(me).await;
            flag.watchdog_live.store(false, Ordering::SeqCst);
        });
    }

    /// Spawn the child, then run the restart-on-crash watchdog until `stop_gracefully`.
    /// Each spawn/exit transition emits a `SidecarStatus`. Takes the `Arc` by value so the
    /// spawned task owns it for its whole life.
    async fn watchdog(self: Arc<Self>) {
        loop {
            if self.shutting_down.load(Ordering::SeqCst) {
                return;
            }
            self.emit(SidecarStatus::new("starting"));
            match self.spawn_child().await {
                Ok(()) => {}
                Err(e) => {
                    // A spawn failure is the most operator-visible failure this module has
                    // (missing binary on a fresh install), so it rides the status event
                    // with its message instead of dying into stderr.
                    eprintln!("[supervisor] spawn failed: {e}");
                    let mut s = SidecarStatus::new("crashed");
                    s.detail = Some(e.to_string());
                    s.restart_count = self.restart_count.load(Ordering::SeqCst);
                    self.emit(s);
                    self.backoff(None).await;
                    continue;
                }
            }
            let started = Instant::now();
            {
                let mut running = SidecarStatus::new("running");
                running.pid = self.child.lock().await.as_ref().and_then(|c| c.id());
                running.restart_count = self.restart_count.load(Ordering::SeqCst);
                self.emit(running);
            }

            // Wait for the child to exit (a poll loop — see the module note).
            let exit_code = self.wait_for_exit().await;

            if self.shutting_down.load(Ordering::SeqCst) {
                self.emit(SidecarStatus::new("stopped"));
                return;
            }

            // Unexpected exit → crash path. `restart()` lands here too, on purpose: the
            // relaunch it wants IS the crash path, minus the alarm.
            let mut crashed = SidecarStatus::new("crashed");
            crashed.last_exit_code = exit_code;
            crashed.restart_count = self.restart_count.load(Ordering::SeqCst);
            self.emit(crashed);

            // Reset the backoff counter if the child had been healthy long enough.
            if started.elapsed() >= Duration::from_secs(MIN_HEALTHY_UPTIME_SEC) {
                self.restart_count.store(0, Ordering::SeqCst);
            }
            self.backoff(exit_code).await;
        }
    }

    /// Build the child `Command` with the full `AIZU_*` env and spawn it.
    ///
    /// Reads a FRESH config snapshot every time: that is what makes `restart()` pick up a
    /// wizard write (new dispatch URL, new platform list, new CDP port) without an app
    /// relaunch.
    async fn spawn_child(&self) -> Result<(), DesktopError> {
        let cfg = self.config.get();
        let binary = self.resolve_binary(&cfg.sidecar_binary_path)?;
        let mut cmd = Command::new(&binary);

        // --- Env vars only. NO CLI args, NO RunManager. -----------------------------
        // Non-secret wiring from config:
        cmd.env("AIZU_DISPATCH_URL", &cfg.dispatch_base_url);
        cmd.env("AIZU_CDP_URL", cfg.cdp_url()); // matches the Chrome we launched
        // …and the profile BASE the shell launches against, for the same reason: the child's
        // preflight inspects a Chrome profile directory, and without this it inspected its
        // own default — a directory neither this app nor the shipped `warm_chrome.sh` had
        // ever warmed, so the row could only ever describe somebody else's box.
        cmd.env(crate::config::CHROME_PROFILE_ENV, &cfg.chrome_profile_base);
        cmd.env("AIZU_WORKER_STATE", &cfg.state_dir);
        cmd.env("AIZU_DB", &cfg.db_path);
        // Capability declaration: which platforms this box advertises it can run. Without
        // it the worker registers with NO capabilities, can never be leased to, and its
        // own preflight parks it (ledger F9.2). "all" → every supported platform.
        cmd.env("AIZU_WORKER_PLATFORMS", &cfg.worker_platforms);
        // Control surface: enable it, pass the generated token + the port.
        cmd.env("AIZU_CONTROL_SURFACE", "1");
        cmd.env("AIZU_CONTROL_TOKEN", self.control_token.as_str());
        cmd.env("AIZU_CONTROL_PORT", cfg.control_port.to_string());

        // The worker bootstrap/enrolment token (a SECRET, never in config.toml) comes from
        // the 0600 token file Setup → Enrol writes; the child registers with it.
        if let Some(token) = crate::config::read_bootstrap_token(&self.app) {
            cmd.env("AIZU_WORKER_BOOTSTRAP_TOKEN", token);
        }

        // Engine/provider secrets (OPENROUTER_API_KEY, AIZU_SECRET_KEY, model/platform
        // overrides, …) from the 0600 worker-secrets.env file. A Finder-launched GUI worker
        // inherits only a minimal launchd environment, so these can NOT be assumed present —
        // without AIZU_SECRET_KEY the box cannot persist its identity (F9.1) and without an
        // LLM backend every live run dies at `_build_run_io` (F9.3). Both are preflight
        // fatals now, which is why Setup → Credentials exists at all.
        // Injected LAST so an operator-supplied value wins over anything inherited; values
        // are never logged. NOTE this loop is a GENERIC passthrough of every key in that
        // file, not a whitelist — so non-secret worker flags (AIZU_WORKER_WARMING_ONLY,
        // AIZU_PREFLIGHT_ENFORCE, AIZU_CDP_URL) can be set there too, and a GUI-managed box
        // is NOT limited to the variables named explicitly above. Do not add a per-key
        // cmd.env() for those and assume it was previously unreachable.
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
        *self.child.lock().await = Some(child);
        Ok(())
    }

    /// Resolve the sidecar binary. Thin wrapper over [`resolve_sidecar_binary`], which is
    /// a free function because `ChromeManager` needs the SAME precedence to ask that binary
    /// for Playwright's Chrome-for-Testing path, and a second copy of this ladder would
    /// drift the day the packaging layout changes.
    fn resolve_binary(&self, configured: &std::path::Path) -> Result<PathBuf, DesktopError> {
        resolve_sidecar_binary(&self.app, configured)
    }

    /// Poll for the child's exit, returning its exit code if available. The lock is taken
    /// and released around each `try_wait`, never held across the sleep.
    async fn wait_for_exit(&self) -> Option<i32> {
        loop {
            {
                let mut guard = self.child.lock().await;
                match guard.as_mut() {
                    // Someone else (stop/restart) already reaped it.
                    None => return None,
                    Some(child) => match child.try_wait() {
                        Ok(Some(status)) => {
                            *guard = None;
                            return status.code();
                        }
                        Ok(None) => {}
                        Err(_) => {
                            *guard = None;
                            return None;
                        }
                    },
                }
            }
            tokio::time::sleep(Duration::from_millis(CHILD_POLL_MS)).await;
        }
    }

    /// Jittered exponential backoff before the next restart attempt, capped, mirroring
    /// sidecar.py `_backoff`. Emits a "backing_off" status with the wait it chose.
    async fn backoff(&self, exit_code: Option<i32>) {
        let n = self.restart_count.fetch_add(1, Ordering::SeqCst) + 1;
        let ceiling = (BACKOFF_BASE_SEC * 2f64.powi(n as i32)).min(BACKOFF_CAP_SEC);
        let jitter = rand::random::<f64>();
        let wait = BACKOFF_BASE_SEC + jitter * (ceiling - BACKOFF_BASE_SEC).max(0.0);

        let mut s = SidecarStatus::new("backing_off");
        s.restart_in_sec = Some(wait);
        s.last_exit_code = exit_code;
        s.restart_count = n;
        self.emit(s);

        // Sliced rather than one long sleep, so `restart` (the wizard applying a fix) and
        // `stop_gracefully` (app exit) both cut it short instead of waiting out the cap.
        let deadline = Instant::now() + Duration::from_secs_f64(wait);
        while Instant::now() < deadline {
            if self.wake.swap(false, Ordering::SeqCst)
                || self.shutting_down.load(Ordering::SeqCst)
            {
                break;
            }
            tokio::time::sleep(Duration::from_millis(200)).await;
        }
    }

    /// Restart the sidecar so it re-reads its environment — the wizard calls this after
    /// every secrets / token / config write, because the child reads `AIZU_*` ONCE at
    /// spawn and a value written afterwards would otherwise sit on disk doing nothing
    /// until the next app launch.
    ///
    /// Deliberately does NOT set `shutting_down`: killing the child drops it into the
    /// watchdog's crash path, which relaunches it with the fresh config after a sub-second
    /// backoff. The counter is reset first so an intentional restart never inherits a
    /// crash-loop's 30s wait.
    ///
    /// The caller is responsible for refusing this while a job is live (see
    /// `commands::restart_sidecar`) — killing a tenant's running job to apply a config
    /// write is never the right trade — and for calling [`Self::ensure_started`] first, so
    /// that a box which booted with no dispatch URL gets a watchdog at all.
    pub async fn restart(&self) {
        self.restart_count.store(0, Ordering::SeqCst);
        self.wake.store(true, Ordering::SeqCst);
        self.terminate_child().await;
    }

    /// Stop the sidecar PROCESS gracefully. ONLY for app exit — never to stop a single job
    /// (that is a control-surface `stopCurrentJob`), and never to apply a config change
    /// (that is `restart`). Sets `shutting_down` so the watchdog does not relaunch.
    pub async fn stop_gracefully(&self) {
        self.shutting_down.store(true, Ordering::SeqCst);
        self.emit(SidecarStatus::new("stopping"));
        self.terminate_child().await;
        self.emit(SidecarStatus::new("stopped"));
    }

    /// SIGTERM/CTRL_BREAK → wait `STOP_GRACE_SEC` → SIGKILL. Safe to call concurrently
    /// with the watchdog: whoever observes the exit first clears the slot, and the other
    /// sees `None` and returns.
    async fn terminate_child(&self) {
        let pid = { self.child.lock().await.as_ref().and_then(|c| c.id()) };
        let Some(pid) = pid else {
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

        let deadline = Instant::now() + Duration::from_secs(STOP_GRACE_SEC);
        let mut killed = false;
        loop {
            {
                let mut guard = self.child.lock().await;
                let Some(child) = guard.as_mut() else {
                    return; // the watchdog's poll reaped it first
                };
                match child.try_wait() {
                    Ok(Some(_)) => {
                        *guard = None;
                        return;
                    }
                    Err(_) => {
                        let _ = child.start_kill();
                        *guard = None;
                        return;
                    }
                    Ok(None) => {
                        if killed {
                            // SIGKILL was already sent and it still has not been reaped;
                            // drop the handle rather than block app exit. tokio's reaper
                            // collects the zombie.
                            *guard = None;
                            return;
                        }
                        if Instant::now() >= deadline {
                            let _ = child.start_kill(); // SIGKILL / TerminateProcess
                            killed = true;
                        }
                    }
                }
            }
            tokio::time::sleep(Duration::from_millis(200)).await;
        }
    }

    /// Announce that BOOT is deliberately holding the child back until the managed Chrome
    /// is up (see `main::start_worker_stack`). Emitted as `starting` with a reason, because
    /// the alternative — a silent gap before the Worker tile goes green — reads to an
    /// operator as a worker that failed to start, and the UI renders `detail` verbatim.
    pub fn emit_holding_for_chrome(&self, detail: &str) {
        let mut s = SidecarStatus::new("starting");
        s.detail = Some(detail.to_string());
        self.emit(s);
    }

    fn emit(&self, status: SidecarStatus) {
        let _ = self.app.emit(SIDECAR_STATUS_EVENT, &status);
    }
}

/// Resolve the `aizu-worker` binary: explicit config path wins; else the bundled resource;
/// else the bare name on PATH (dev).
///
/// A free function with an explicit `&AppHandle` because there are TWO callers with nothing
/// else in common: this supervisor (which spawns it as the worker) and `ChromeManager`
/// (which asks that same binary for Playwright's Chrome-for-Testing path, because on a
/// packaged box the frozen sidecar is the only Playwright on the machine). Duplicating the
/// resource_dir/nested/flat/PATH ladder in the Chrome side is how the two would silently
/// disagree after the next packaging change.
///
/// NOTE for the Chrome caller: step 1 turns a configured-but-missing path into an `Err`
/// rather than a fallthrough, and step 3 can return a BARE RELATIVE name that does not
/// exist on disk (it is resolved by PATH at spawn time). So do not `.exists()`-gate the
/// result, and do not propagate the `Err` as a Chrome error — swallow it and fall through.
pub(crate) fn resolve_sidecar_binary(
    app: &AppHandle,
    configured: &std::path::Path,
) -> Result<PathBuf, DesktopError> {
    // 1. Explicit config override wins.
    if !configured.as_os_str().is_empty() {
        if configured.exists() {
            return Ok(configured.to_path_buf());
        }
        return Err(DesktopError::SidecarSpawnFailed(format!(
            "configured sidecar_binary_path does not exist: {}",
            configured.display()
        )));
    }
    // 2. Bundled resource (packaged app). The onedir PyInstaller build ships as
    //    <resource_dir>/sidecar/aizu-worker/aizu-worker (folder + executable).
    //    Fall back to a flat layout in case the resource copy flattened it.
    if let Ok(rd) = app.path().resource_dir() {
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
