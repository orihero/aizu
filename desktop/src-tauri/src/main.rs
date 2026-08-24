//! AIZU Worker — desktop shell entry point (BUILD-PLAN Phase 6, C3 option A).
//!
//! # What this app is (and is NOT)
//! A thin Tauri shell that runs on a managed worker PC and supervises the Python
//! **`aizu-worker`** sidecar binary (= `aizu.worker.sidecar:main`) as a managed
//! child process — **BUILD-PLAN Phase 6, C3 option A**: restart-on-crash watchdog +
//! run-at-login. It feeds the child env vars only.
//!
//! It **NEVER**:
//!   - shells out to `aizu.cli`,
//!   - starts a `RunManager` (there is none on the box — the sidecar bypasses it, C3),
//!   - scrapes logs to infer state (superseded — the control surface is the source of truth),
//!   - kills the sidecar to stop a single job (that is a `stopCurrentJob` command; see below),
//!   - launches a second Chrome or kills a Chrome it did not launch,
//!   - decides for itself whether the box is healthy (the sidecar's preflight decides; this
//!     shell forwards the report and derives which setup step to show from it).
//!
//! # Responsibilities (startup order)
//! 1. **Resolve config** from an on-disk TOML at `app_config_dir` (`config.rs`). NO secrets
//!    are baked in or persisted there. A missing file is a normal FIRST RUN, not an error:
//!    the app opens into the setup wizard. A MALFORMED file is recorded as a
//!    `startup_error` the wizard displays — it used to go to stderr, where a GUI operator
//!    could never see it, which is exactly how a dead box looked like a quiet one.
//! 2. **Start Chrome** via `ChromeManager` (`chrome_manager.rs`), but only once the wizard
//!    is settled (finished, or explicitly deferred) — during setup the operator launches it
//!    explicitly from the wizard's Chrome step, so nothing opens a browser behind their back.
//!    Default CDP port is **9333** (the live-proven, now canonical port), NOT 9222.
//! 3. **Spawn the supervised sidecar** (`sidecar_supervisor.rs`) — **after** step 2 has
//!    settled, never concurrently with it (see [`start_worker_stack`]) — passing `AIZU_*` env:
//!    - `AIZU_DISPATCH_URL` (from config),
//!    - `AIZU_CONTROL_SURFACE=1`,
//!    - `AIZU_CONTROL_TOKEN=<generated-per-spawn token>` (also handed to `control_client`),
//!    - `AIZU_CONTROL_PORT=<config.control_port>`,
//!    - `AIZU_CDP_URL=http://127.0.0.1:<config.cdp_port>` (matches the Chrome we started),
//!    - `AIZU_WORKER_PLATFORMS` (the capability declaration — without it the box registers
//!      with `capabilities: []` and can never be leased to, ledger F9.2),
//!    - `AIZU_DB`, `AIZU_WORKER_STATE`,
//!    - plus every key from the 0600 `worker-secrets.env` (`AIZU_SECRET_KEY`,
//!      `OPENROUTER_API_KEY`, …) — F9.1 / F9.3.
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
//! app-exit / crash-restart / applying a setup write.

mod chrome_manager;
mod commands;
mod config;
mod control_client;
mod errors;
mod log_tail;
mod sidecar_supervisor;

use std::collections::BTreeSet;
use std::sync::{Arc, Mutex, RwLock};
use std::time::Duration;

use chrome_manager::ChromeManager;
use config::{ConfigCell, SharedConfigCell};
use control_client::ControlClient;
use errors::DesktopError;
use sidecar_supervisor::SidecarSupervisor;
use tauri::Manager; // brings manage() + app_handle() trait methods into scope

/// Shared application state handed to every `#[tauri::command]`.
///
/// The config is a live CELL, not a frozen `Arc`: the wizard writes config as the operator
/// walks it and the supervisor must pick each write up on its next spawn without an app
/// relaunch. Everything else is a long-lived actor that owns its own invariants
/// (never-a-second-Chrome, reconnect-never-kill, token confidentiality).
pub struct AppState {
    pub config: SharedConfigCell,
    /// The per-spawn loopback control-surface bearer token. Held in memory only; NEVER
    /// logged, NEVER surfaced to the UI, NEVER written to the TOML.
    pub control_token: Arc<String>,
    pub control: Arc<ControlClient>,
    /// NOT behind an outer mutex: the watchdog runs for the life of the app, so a mutex
    /// around the supervisor would be held forever and every command that needs it would
    /// deadlock. It uses interior mutability instead — see `sidecar_supervisor`.
    pub supervisor: Arc<SidecarSupervisor>,
    /// A std mutex because every `ChromeManager` call is blocking (subprocess spawn, CDP
    /// probe) and is run on the blocking pool; nothing awaits while holding it.
    pub chrome: Arc<Mutex<ChromeManager>>,
    /// The one startup failure an operator would otherwise never learn about — a
    /// `config.toml` that would not load, or an `init_app` that did not finish. Surfaced
    /// through `get_setup_state` (the wizard footer) and `get_shell_note` (the dashboard's
    /// shell-problem strip).
    ///
    /// Chrome does NOT land here any more (see `chrome_boot_error`). It used to, and that
    /// is defect D: one slot held two unrelated facts, so nothing could retire either one
    /// without risking erasing the other — and a boot Chrome failure the operator had since
    /// fixed stayed pinned in the wizard footer for the life of the process.
    startup_error: RwLock<Option<String>>,
    /// BOOT's attempt at the warmed Chrome failed (no binary, a launch that never became
    /// attachable, or the grace running out).
    ///
    /// Its own slot for the same reason `chrome_degraded` is: each of these three facts must
    /// be able to clear without erasing the others. This one is retired — or overwritten —
    /// by the next COMPLETED launch attempt (`commands::boot_error_after_launch`), which is
    /// a newer first-hand observation of exactly the thing boot could not do, while a
    /// malformed config.toml beside it stays on screen untouched. An attempt that does NOT
    /// complete leaves it alone: it learned nothing to replace it with.
    chrome_boot_error: RwLock<Option<String>>,
    /// A Chrome that came up DEGRADED — resolution fell through to the system browser
    /// (`chrome_manager::ChromeReady::degradation`). Kept separate from `startup_error`
    /// because the two are independent and each must be able to clear without erasing the
    /// other: an operator who downloads the browser must stop seeing this note even though
    /// their config.toml is still malformed, and vice versa. They are shown together — see
    /// [`visible_problem`].
    chrome_degraded: RwLock<Option<ChromeNote>>,
    /// Browser platforms the operator pressed **Skip** on in the wizard's Sign-in step.
    ///
    /// In MEMORY only — never written to config.toml, never sent upstream, and gone on the
    /// next app start. It exists here rather than only in the webview because the step the
    /// wizard shows is derived in Rust (`commands::derive_step`), so a skip that never
    /// reached this side could never reach the Finish gate: the operator would be looking
    /// at a "skipped" pill while Finish stayed disabled forever. The skip suppresses
    /// nothing else — the login row stays in the preflight report the fleet console reads.
    login_skips: RwLock<BTreeSet<String>>,
}

/// A note about the Chrome this box actually started.
///
/// One kind now. It used to carry a `brand_conflict` flag so that
/// `install_chrome_browser` could tell an irreversible-data-loss warning from routine
/// follow-up advice and refrain from overwriting the first — a distinction that stopped
/// existing when the profile directory became a function of the browser brand: there is no
/// refusal left to warn about, and every note in here describes a browser we did start.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChromeNote {
    pub text: String,
}

impl ChromeNote {
    /// A degraded launch: a Chrome came up, but not the one the engine may be able to
    /// attach to. Transient, self-correcting, and safe to update in place.
    pub fn degraded(text: String) -> Self {
        Self { text }
    }
}

impl AppState {
    pub fn startup_error(&self) -> Option<String> {
        match self.startup_error.read() {
            Ok(g) => g.clone(),
            Err(poisoned) => poisoned.into_inner().clone(),
        }
    }
    /// Record (or clear) the config/init startup failure. `None` retires it — which is
    /// only ever correct once the file it describes has provably been rewritten.
    pub fn set_startup_error(&self, msg: Option<String>) {
        match self.startup_error.write() {
            Ok(mut g) => *g = msg,
            Err(poisoned) => *poisoned.into_inner() = msg,
        }
    }
    /// Boot's own Chrome failure, if any.
    pub fn chrome_boot_error(&self) -> Option<String> {
        match self.chrome_boot_error.read() {
            Ok(g) => g.clone(),
            Err(poisoned) => poisoned.into_inner().clone(),
        }
    }
    /// Record (or clear) boot's Chrome failure. Passing `None` is the clearing path defect
    /// D was missing: a launch attempt that SUCCEEDED retires it, because that attempt is a
    /// fresher first-hand answer to the same question. It touches nothing else — a malformed
    /// config.toml and a degraded-Chrome note both survive it. Who calls it with what is
    /// `commands::boot_error_after_launch`, which is pure so the rule can be tested.
    pub fn set_chrome_boot_error(&self, msg: Option<String>) {
        match self.chrome_boot_error.write() {
            Ok(mut g) => *g = msg,
            Err(poisoned) => *poisoned.into_inner() = msg,
        }
    }
    /// The current Chrome note's text, if any.
    pub fn chrome_degraded(&self) -> Option<String> {
        self.chrome_note().map(|n| n.text)
    }
    /// The whole note, kind included. The kind is load-bearing: a brand-conflict note warns
    /// about irreversible data loss and nothing may quietly overwrite it (defect E).
    pub fn chrome_note(&self) -> Option<ChromeNote> {
        match self.chrome_degraded.read() {
            Ok(g) => g.clone(),
            Err(poisoned) => poisoned.into_inner().clone(),
        }
    }
    /// Record (or clear) the degraded-Chrome note. Called after EVERY `ensure_running` —
    /// boot's and the wizard's — so it always describes the last resolution, and after a
    /// successful browser download, which advances it to the step still outstanding (the
    /// download changes what is on DISK, never which browser currently holds the port).
    pub fn set_chrome_degraded(&self, msg: Option<String>) {
        self.set_chrome_note(msg.map(ChromeNote::degraded));
    }
    pub fn set_chrome_note(&self, note: Option<ChromeNote>) {
        match self.chrome_degraded.write() {
            Ok(mut g) => *g = note,
            Err(poisoned) => *poisoned.into_inner() = note,
        }
    }
    /// What the wizard footer and the dashboard's shell-problem strip show — the latter via
    /// [`commands::get_shell_note`]. See [`visible_problem`].
    ///
    /// Folded in two passes rather than one three-argument function so the de-duplication
    /// rule below applies to every pair: one launch failure can reach BOTH the boot slot and
    /// the Chrome note, and printing it twice reads as two problems.
    pub fn visible_problem(&self) -> Option<String> {
        visible_problems(self.startup_error(), self.chrome_boot_error(), self.chrome_degraded())
    }
    /// The operator's current Sign-in skips, sorted.
    pub fn login_skips(&self) -> Vec<String> {
        match self.login_skips.read() {
            Ok(g) => g.iter().cloned().collect(),
            Err(poisoned) => poisoned.into_inner().iter().cloned().collect(),
        }
    }
    /// Replace the skip set wholesale — the UI owns it and sends the complete list, so
    /// there is no merge to get wrong and no order in which two clicks can disagree.
    pub fn set_login_skips(&self, platforms: BTreeSet<String>) {
        match self.login_skips.write() {
            Ok(mut g) => *g = platforms,
            Err(poisoned) => *poisoned.into_inner() = platforms,
        }
    }
}

/// Fold the two independent "something is wrong with this box" facts into the ONE string
/// the UI renders, keeping both.
///
/// A pure function so the composition is testable without an `AppHandle`. It matters
/// because each source alone has already been a silent failure: the startup error was an
/// `eprintln!` on a GUI process until the wizard learned to show it, and the degraded-Chrome
/// note was one until now. Dropping either one behind the other would just move the silence.
fn visible_problem(startup: Option<String>, chrome_degraded: Option<String>) -> Option<String> {
    match (startup, chrome_degraded) {
        // …but not the SAME fact twice. One launch failure can reach both sinks — boot
        // records it, a wizard "Launch warmed Chrome" records the same wording again — and
        // it is one long sentence. Printing it twice, joined by a dash, reads as two
        // different problems and buries the way out.
        (Some(a), Some(b)) if a == b => Some(a),
        (Some(a), Some(b)) => Some(format!("{a} — {b}")),
        (Some(a), None) => Some(a),
        (None, Some(b)) => Some(b),
        (None, None) => None,
    }
}

/// The three independent "something is wrong with this box" facts, folded into the ONE
/// string the UI renders — pairwise, so the de-duplication rule below applies to every pair.
///
/// Three slots rather than one is the shape defect D asked for: a fact that shares a slot
/// with an unrelated fact can never be retired, because retiring it risks erasing the other.
/// Each of these clears on its own evidence — a config rewrite, a completed launch attempt,
/// a successful attach — and none of them clears the others.
fn visible_problems(
    startup: Option<String>,
    chrome_boot: Option<String>,
    chrome_note: Option<String>,
) -> Option<String> {
    visible_problem(visible_problem(startup, chrome_boot), chrome_note)
}

/// How often the shell polls `GET /status`. The control surface is cheap and loopback-only,
/// so a short cadence keeps the UI responsive without hammering anything real. The wizard's
/// green badges are driven by this same beat — the operator signs in inside the real Chrome
/// and the badge flips on its own within ~1.5s of the next preflight.
const STATUS_POLL_INTERVAL_MS: u64 = 1_500;

/// How long boot waits for the managed Chrome before starting the sidecar ANYWAY.
///
/// It has to exceed everything `ChromeManager::ensure_running` can spend on the slow path,
/// or the cap would re-create the very race it exists to close (F-2: the sidecar's preflight
/// beating Chrome up). That worst case is **29.6s** — a TCP pre-check, BOTH
/// Chrome-for-Testing helper tiers with their stdout graces, then the 15s launch-attach
/// deadline; `chrome_manager::CFT_SIDECAR_TIMEOUT_SEC` carries the itemised table, and
/// `chrome_manager`'s `the_worst_case_launch_budget_fits_under_the_boot_grace` fails the
/// build if anyone raises a row past this ceiling.
///
/// It was 30s against a claimed 28.8s, except the code could not honour that claim: the
/// CDP probe shelled a Python subprocess with NO timeout, inside a loop whose deadline was
/// only tested at loop ENTRY. Both are bounded now; this is the margin that keeps a slow
/// (not wedged) box from tripping the cap by a second.
///
/// It is a CEILING, not a wait: the normal path returns the moment Chrome is attachable.
/// Nothing beyond it is fatal — a Chrome that never came up must still get a sidecar,
/// because the control surface is how the operator finds out why (rule 3), and because a
/// sidecar that never starts is a box that never self-heals (rule 1).
const CHROME_BOOT_GRACE_SEC: u64 = 35;

/// Which boot order this box needs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum BootPlan {
    /// Nothing here launches a browser: the box has no dispatch yet, or the operator is
    /// still in the wizard, where Chrome is launched explicitly from the Chrome step.
    SidecarOnly,
    /// A box being RUN: get Chrome up first, THEN start the sidecar.
    ChromeThenSidecar,
}

/// What the Chrome half of boot ended up doing. Recorded, never fatal.
#[derive(Debug, Clone, PartialEq, Eq)]
enum ChromeBoot {
    /// This box does not launch Chrome at boot.
    Skipped,
    Ready(String),
    Failed(String),
    /// The launch outlived [`CHROME_BOOT_GRACE_SEC`]; the sidecar started without it.
    TimedOut,
}

/// Decide the boot order. `wizard_settled` is `setup_complete || setup_deferred`, not
/// `setup_complete` alone — see `DesktopConfig::wizard_settled`.
fn boot_plan(configured: bool, wizard_settled: bool) -> BootPlan {
    if configured && wizard_settled {
        BootPlan::ChromeThenSidecar
    } else {
        BootPlan::SidecarOnly
    }
}

/// Bring the worker stack up **in order**: Chrome, then the sidecar.
///
/// # Why the order is the whole point (ledger F-2)
/// This used to start Chrome on a detached task and call `ensure_started` on the next line,
/// i.e. concurrently. The sidecar runs its launch preflight seconds after spawn, and on a
/// cold box it beat Chrome up essentially every time: `cdp_reachable` failed, the preflight
/// went FATAL, and the box registered with `capabilities: []` and parked for 30s — on a
/// completely healthy machine. Worse, it cost TWO `register_worker` calls per launch (the
/// empty one, then the real one when the park cleared), and each rotates
/// `worker_token_hash` — the exact rotation pattern F9.1 was filed about. A false fatal on
/// a healthy box is the worst outcome this feature has; systematically manufacturing one on
/// every normal launch is worse still.
///
/// # …but a Chrome that fails must NOT hold the sidecar back
/// `start_sidecar` runs on every path — success, failure, and timeout. Withholding it would
/// mean no control surface, so the operator would have no way to see why (rule 3), and no
/// preflight re-probe, so the box could never self-heal (rule 1). The wait is a courtesy to
/// a browser that is coming, never a gate on one that is not.
///
/// Generic over the two effects purely so the ORDER is testable without a display, a
/// Chrome, or a Tauri runtime.
async fn start_worker_stack<F, Fut>(
    plan: BootPlan,
    grace: Duration,
    launch_chrome: F,
    start_sidecar: impl FnOnce(),
) -> ChromeBoot
where
    F: FnOnce() -> Fut,
    Fut: std::future::Future<Output = Result<String, String>>,
{
    let outcome = match plan {
        BootPlan::SidecarOnly => ChromeBoot::Skipped,
        BootPlan::ChromeThenSidecar => match tokio::time::timeout(grace, launch_chrome()).await {
            Ok(Ok(url)) => ChromeBoot::Ready(url),
            Ok(Err(detail)) => ChromeBoot::Failed(detail),
            Err(_) => ChromeBoot::TimedOut,
        },
    };
    start_sidecar();
    outcome
}

fn main() {
    // A crash-tolerant boot: any startup failure is reported to the UI rather than
    // panicking the process (the operator must see WHY the worker didn't come up).
    if let Err(e) = run() {
        eprintln!("[aizu-worker] fatal startup error: {e}");
        std::process::exit(1);
    }
}

/// The fallible startup, extracted so `setup` can catch its errors and STILL open the
/// window.
///
/// Nothing here can prevent the app from opening, and nothing here is silent. A first-run
/// box with no `config.toml` gets defaults and an empty dispatch URL; a box with a broken
/// `config.toml` gets the same defaults PLUS a `startup_error` the wizard shows verbatim.
/// Chrome and the sidecar start only when there is something to start them for.
fn init_app(app: &mut tauri::App) -> Result<(), DesktopError> {
    let handle = app.handle().clone();

    // Config (first run with no config.toml → defaults with an EMPTY dispatch URL).
    // A parse failure is NOT fatal: falling back to defaults keeps the wizard reachable,
    // which is the only place the operator can fix the file from.
    let (loaded, load_error) = match config::load(&handle) {
        Ok(cfg) => (cfg, None),
        Err(e) => (config::defaults(&handle), Some(e.to_string())),
    };
    let configured = loaded.is_configured();
    let wizard_settled = loaded.wizard_settled();

    let config: SharedConfigCell = Arc::new(ConfigCell::new(loaded));
    let control_token = Arc::new(control_client::generate_token());
    let control = Arc::new(ControlClient::new(config.get().control_port, control_token.clone()));
    let supervisor = Arc::new(SidecarSupervisor::new(
        config.clone(),
        control_token.clone(),
        handle.clone(),
    ));
    let chrome = Arc::new(Mutex::new(ChromeManager::new(config.clone(), handle.clone())));

    app.manage(AppState {
        config: config.clone(),
        control_token: control_token.clone(),
        control: control.clone(),
        supervisor: supervisor.clone(),
        chrome: chrome.clone(),
        startup_error: RwLock::new(load_error),
        chrome_boot_error: RwLock::new(None),
        chrome_degraded: RwLock::new(None),
        login_skips: RwLock::new(BTreeSet::new()),
    });

    // Chrome is best-effort, and only on a box whose wizard is settled. Mid-setup the
    // operator launches it from the wizard's Chrome step, where the failure has somewhere
    // to be shown; auto-launching a browser behind a first-run operator is both surprising
    // and (when it fails) invisible.
    //
    // The sidecar then starts AFTER that launch settles — never alongside it. See
    // `start_worker_stack` for why concurrency here manufactured a false fatal, and two
    // token rotations, on every normal launch. `ensure_started` on an unconfigured box is
    // still a normal first run: it emits `not_configured`, and the wizard starts the child
    // itself the moment the operator saves a dispatch URL — no app relaunch.
    let plan = boot_plan(configured, wizard_settled);
    let chrome_boot = chrome.clone();
    let boot_handle = handle.clone();
    // A handle for the launcher closure itself: a DEGRADED success (we started the system
    // Chrome because there is no Chrome-for-Testing on the box) is still an `Ok`, so it
    // cannot ride the `ChromeBoot::Failed` path below — and it is exactly the case that used
    // to reach the operator nowhere at all.
    let degraded_handle = handle.clone();
    let boot_supervisor = supervisor.clone();
    // A second handle so the start closure OWNS one: it is held across the Chrome wait, and
    // an owned `Arc` keeps that future trivially `Send` for `spawn`.
    let start_supervisor = supervisor.clone();
    tauri::async_runtime::spawn(async move {
        if plan == BootPlan::ChromeThenSidecar {
            // Name the wait. An unexplained gap before the worker tile goes green reads as
            // a crash, and "it just sat there" is the failure mode this app exists to end.
            boot_supervisor.emit_holding_for_chrome(
                "waiting for the warmed Chrome before starting the worker",
            );
        }
        let outcome = start_worker_stack(
            plan,
            Duration::from_secs(CHROME_BOOT_GRACE_SEC),
            move || async move {
                let joined = tauri::async_runtime::spawn_blocking(move || {
                    let mut guard = match chrome_boot.lock() {
                        Ok(g) => g,
                        Err(poisoned) => poisoned.into_inner(),
                    };
                    guard.ensure_running()
                })
                .await;
                match joined {
                    Ok(Ok(ready)) => {
                        if let Some(state) = degraded_handle.try_state::<AppState>() {
                            state.set_chrome_degraded(ready.degradation);
                        }
                        Ok(ready.cdp_url)
                    }
                    Ok(Err(e)) => Err(e.to_string()),
                    Err(_) => Err("the Chrome launch task did not complete".to_string()),
                }
            },
            move || SidecarSupervisor::ensure_started(&start_supervisor),
        )
        .await;

        // Recorded, not swallowed: the dashboard's shell-problem strip and the wizard footer
        // both read this. The sidecar preflight independently reports the same condition as
        // `cdp_reachable` / `cdp_attachable` — this is the local half, and it is the only
        // place the launcher's own error (no binary, unwritable profile dir) exists.
        let detail = match outcome {
            ChromeBoot::Skipped | ChromeBoot::Ready(_) => None,
            ChromeBoot::Failed(detail) => Some(detail),
            ChromeBoot::TimedOut => Some(format!(
                "Chrome did not become available within {CHROME_BOOT_GRACE_SEC}s — the \
                 worker started anyway; its preflight will report the browser checks."
            )),
        };
        if let Some(detail) = detail {
            eprintln!("[aizu-worker] Chrome not ready (continuing): {detail}");
            if let Some(state) = boot_handle.try_state::<AppState>() {
                // Its OWN slot, not `startup_error`: this is the fact the operator goes and
                // fixes (quit a stale browser, download the right one, answer the profile
                // question), so it has to be retirable without taking a config error with
                // it. `commands::launch_chrome` is what retires it.
                state.set_chrome_boot_error(Some(detail));
            }
        }
    });

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
                if let Some(state) = app.try_state::<AppState>() {
                    state.set_startup_error(Some(e.to_string()));
                }
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
            // steady-state controls
            commands::get_status,
            commands::pause,
            commands::resume,
            commands::stop_current_job,
            commands::focus_chrome,
            commands::restart_sidecar,
            commands::set_capacity_override,
            commands::get_config,
            commands::save_config,
            commands::get_shell_note,
            // first-run setup wizard
            commands::get_setup_state,
            commands::save_setup_step,
            commands::save_worker_secrets,
            commands::save_enrolment_token,
            commands::generate_secret_key,
            commands::launch_chrome,
            commands::install_chrome_browser,
            commands::run_preflight,
            commands::open_login_tab,
            commands::probe_dispatch,
            commands::set_login_skips,
            commands::finish_setup,
            commands::defer_setup,
        ])
        .on_window_event(|window, event| {
            // On the last window close / app exit: stop the sidecar gracefully, THEN kill
            // only a Chrome we launched. Order matters — the sidecar must detach from CDP
            // before Chrome goes away, and an attached-only Chrome is left running.
            if let tauri::WindowEvent::Destroyed = event {
                let app = window.app_handle().clone();
                tauri::async_runtime::block_on(async move {
                    if let Some(state) = app.try_state::<AppState>() {
                        state.supervisor.stop_gracefully().await;
                        match state.chrome.lock() {
                            Ok(mut g) => g.kill_on_app_exit(),
                            Err(poisoned) => poisoned.into_inner().kill_on_app_exit(),
                        }
                    }
                });
            }
        })
        .run(tauri::generate_context!())
        .map_err(|e| DesktopError::ConfigInvalid(format!("tauri run failed: {e}")))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc as StdArc, Mutex as StdMutex};

    /// Record of what boot did, in the order it did it.
    type Trace = StdArc<StdMutex<Vec<&'static str>>>;

    fn trace() -> Trace {
        StdArc::new(StdMutex::new(Vec::new()))
    }
    fn seen(t: &Trace) -> Vec<&'static str> {
        t.lock().unwrap().clone()
    }

    #[test]
    fn only_a_settled_configured_box_launches_chrome_at_boot() {
        assert_eq!(boot_plan(true, true), BootPlan::ChromeThenSidecar);
        // Mid-wizard: the operator launches Chrome from the Chrome step themselves.
        assert_eq!(boot_plan(true, false), BootPlan::SidecarOnly);
        // No dispatch yet — nothing to run, nothing to open a browser for.
        assert_eq!(boot_plan(false, true), BootPlan::SidecarOnly);
        assert_eq!(boot_plan(false, false), BootPlan::SidecarOnly);
    }

    /// F-2: the sidecar must NOT be started next to the Chrome launch. Its preflight runs
    /// seconds after spawn, and beating Chrome up is a false fatal plus two
    /// `register_worker` calls (two `worker_token_hash` rotations) on a healthy box.
    #[tokio::test]
    async fn the_sidecar_starts_only_after_chrome_has_settled() {
        let t = trace();
        let (tc, ts) = (t.clone(), t.clone());
        let outcome = start_worker_stack(
            BootPlan::ChromeThenSidecar,
            Duration::from_secs(5),
            move || async move {
                // A launch that takes real time, as a cold Chrome does.
                tokio::time::sleep(Duration::from_millis(50)).await;
                tc.lock().unwrap().push("chrome");
                Ok("http://127.0.0.1:9333".to_string())
            },
            move || ts.lock().unwrap().push("sidecar"),
        )
        .await;
        assert_eq!(seen(&t), vec!["chrome", "sidecar"]);
        assert_eq!(outcome, ChromeBoot::Ready("http://127.0.0.1:9333".into()));
    }

    /// …and a Chrome that FAILS must not withhold the sidecar: no sidecar means no control
    /// surface (rule 3, the operator cannot see why) and no re-probe (rule 1, the box can
    /// never self-heal).
    #[tokio::test]
    async fn a_failed_chrome_still_starts_the_sidecar() {
        let t = trace();
        let (tc, ts) = (t.clone(), t.clone());
        let outcome = start_worker_stack(
            BootPlan::ChromeThenSidecar,
            Duration::from_secs(5),
            move || async move {
                tc.lock().unwrap().push("chrome");
                Err("no Chrome binary found".to_string())
            },
            move || ts.lock().unwrap().push("sidecar"),
        )
        .await;
        assert_eq!(seen(&t), vec!["chrome", "sidecar"]);
        assert_eq!(outcome, ChromeBoot::Failed("no Chrome binary found".into()));
    }

    /// A wedged launcher must not park the worker forever either — the grace is a ceiling.
    #[tokio::test]
    async fn a_wedged_chrome_launch_times_out_and_the_sidecar_still_starts() {
        let t = trace();
        let ts = t.clone();
        let outcome = start_worker_stack(
            BootPlan::ChromeThenSidecar,
            Duration::from_millis(20),
            || async {
                // Never completes.
                futures_never().await
            },
            move || ts.lock().unwrap().push("sidecar"),
        )
        .await;
        assert_eq!(seen(&t), vec!["sidecar"]);
        assert_eq!(outcome, ChromeBoot::TimedOut);
    }

    /// A first-run / mid-wizard box waits for nothing: the wizard needs the control surface
    /// promptly, and this shell has not launched a Chrome to wait for.
    #[tokio::test]
    async fn sidecar_only_boot_does_not_wait_for_anything() {
        let t = trace();
        let (tc, ts) = (t.clone(), t.clone());
        let outcome = start_worker_stack(
            BootPlan::SidecarOnly,
            Duration::from_secs(30),
            move || async move {
                tc.lock().unwrap().push("chrome");
                Ok(String::new())
            },
            move || ts.lock().unwrap().push("sidecar"),
        )
        .await;
        assert_eq!(seen(&t), vec!["sidecar"]);
        assert_eq!(outcome, ChromeBoot::Skipped);
    }

    /// A degraded Chrome (we started the SYSTEM browser because the box has no
    /// Chrome-for-Testing) is an `Ok`, so it can never ride `ChromeBoot::Failed`. It must
    /// still reach the operator, and it must not evict a config-load error to do it:
    /// before this, the fall-through reached the UI NOWHERE — boot computed
    /// `ChromeBoot::Ready`, `startup_error` stayed null, the wizard painted the Chrome step
    /// green, and the operator was left retrying a remedy that cannot work.
    #[test]
    fn both_startup_problems_survive_being_shown_together() {
        assert_eq!(visible_problem(None, None), None);
        assert_eq!(visible_problem(Some("bad toml".into()), None), Some("bad toml".into()));
        assert_eq!(
            visible_problem(None, Some("system Chrome".into())),
            Some("system Chrome".into())
        );
        let both = visible_problem(Some("bad toml".into()), Some("system Chrome".into()))
            .expect("both problems are live");
        assert!(both.contains("bad toml"), "the config error was dropped: {both}");
        assert!(both.contains("system Chrome"), "the Chrome note was dropped: {both}");
    }

    /// …and the one fact that legitimately lands in BOTH sinks is shown once.
    ///
    /// A box with no browser on it fails boot's launch, and fails the wizard's "Launch
    /// warmed Chrome" the same way ten seconds later — the same paragraph, verbatim, in two
    /// slots. Folded blindly it renders twice with a dash between, which reads as two
    /// separate problems and pushes the remedy off the end of the footer.
    #[test]
    fn one_problem_recorded_in_both_sinks_is_shown_once() {
        let failure = "No Chrome on this box at all — neither Playwright's \
                       Chrome-for-Testing nor a system Chrome."
            .to_string();
        assert_eq!(
            visible_problem(Some(failure.clone()), Some(failure.clone())),
            Some(failure)
        );
    }

    // --- defect D: a fact nobody can retire is a fact that outlives its fix ------------

    /// Three slots, three independent clearing paths. This is the whole reason the boot
    /// Chrome failure moved out of `startup_error`: retiring it must not take a malformed
    /// config.toml with it, and repairing the config must not hide a Chrome that is still
    /// broken.
    #[test]
    fn each_startup_fact_clears_without_erasing_the_others() {
        let cfg = || Some("TOML parse error: line 3".to_string());
        let chrome = || Some("Chrome did not become available within 30s".to_string());
        let note = || Some("Chrome for Testing is not installed on this box".to_string());

        // All three live: all three are shown. Losing one behind another just moves the
        // silence this whole surface exists to end.
        let all = visible_problems(cfg(), chrome(), note()).expect("all three");
        assert!(all.contains("TOML parse error"), "{all}");
        assert!(all.contains("did not become available"), "{all}");
        assert!(all.contains("not installed"), "{all}");

        // The operator launches Chrome successfully: the boot verdict goes, the config
        // error stays. This is the case that was impossible before — a Chrome failure at
        // start-up was pinned in the wizard footer for the life of the process.
        let after_launch = visible_problems(cfg(), None, None).expect("config error survives");
        assert!(after_launch.contains("TOML parse error"));
        assert!(!after_launch.contains("did not become available"));

        // …and the mirror image: the wizard rewrites config.toml, the Chrome facts stay.
        let after_save = visible_problems(None, chrome(), note()).expect("chrome facts survive");
        assert!(!after_save.contains("TOML parse error"));
        assert!(after_save.contains("did not become available"), "{after_save}");

        assert_eq!(visible_problems(None, None, None), None, "a healthy box says nothing");
    }

    /// The de-duplication still applies to every pair once there are three. One launch
    /// failure genuinely lands in two slots — boot records it, a wizard Launch re-records
    /// it — and printing one long sentence twice, joined by a dash, reads as two problems.
    #[test]
    fn the_same_failure_in_two_of_three_slots_is_still_shown_once() {
        let failure = "No Chrome on this box at all — neither Playwright's \
                       Chrome-for-Testing nor a system Chrome."
            .to_string();
        assert_eq!(
            visible_problems(None, Some(failure.clone()), Some(failure.clone())),
            Some(failure.clone())
        );
        assert_eq!(
            visible_problems(Some(failure.clone()), Some(failure.clone()), None),
            Some(failure)
        );
    }

    /// A future that never resolves — `std::future::pending` typed to the launcher's shape.
    async fn futures_never() -> Result<String, String> {
        std::future::pending::<Result<String, String>>().await
    }
}
