//! Managed Chrome lifecycle for the desktop shell (BUILD-PLAN Phase 6).
//!
//! Rust mirror of `aizu.worker.chrome_manager.ChromeManager` and
//! `engine/scripts/warm_chrome.sh`. Owns exactly ONE warmed, logged-in Chrome per box:
//!
//!   - **`ensure_running`**: reuse an existing attachable Chrome if one is up, else launch
//!     ONE. **Never a second Chrome.**
//!   - **`kill_on_app_exit`**: terminate a Chrome ONLY if WE launched it (`owned == false`
//!     → no-op). A Chrome we merely attached to must outlive us so the warmed login
//!     survives a sidecar restart.
//!
//! # Detection is a REAL connect_over_cdp probe, not a bare HTTP 200
//! A stale or degraded Chrome answers `/json/version` with HTTP 200 while REJECTING
//! `connect_over_cdp` ("Browser context management is not supported"). So an HTTP 200 is
//! only a cheap pre-check; the source of truth is a real Playwright
//! `connect_over_cdp(no_defaults=True)` attach. We run that probe by shelling to the venv's
//! Python (the SAME resolution `warm_chrome.sh` uses), not by hitting HTTP from Rust.
//!
//! ## …and "system Chrome 149+ cannot be attached to" is NOT that reason
//! Comments here and in `warm_chrome.sh` used to give a version as the cause: Chrome 149
//! supposedly removed the CDP browser-context surface. Measured on 151 and false — system
//! Google Chrome 151.0.7922.138 attaches, Playwright's Chrome-for-Testing 151.0.7922.34
//! attaches, and a read-only `Target.getBrowserContexts` against the live system browser
//! returns normally. Chrome-for-Testing is still the right default (its protocol matches the
//! installed Playwright, which is what `no_defaults=True` leans on), it is simply not
//! load-bearing for attachability any more. The narrative below is kept as the HISTORY of
//! the packaged-box fix; read every "149+" in it as "a browser this box could not attach
//! to", because the probe — not a version number — is what decides.
//!
//! # …but a probe we cannot RUN is `Unknown`, never `Rejected`
//! The probe needs a Python with Playwright. On a packaged box there may not be one this
//! shell can find (the frozen sidecar carries its own, inside the binary). The old code
//! collapsed "probe unavailable" into "attach failed", which made `ensure_running` report
//! the scary degraded-Chrome error on a box whose Chrome was perfectly fine — and the
//! wizard's Chrome step would have been un-passable there. So [`cdp_probe`] is TRI-state
//! and `Unknown` means "something is listening; let the authority decide". The authority
//! is the sidecar's own preflight (`cdp_attachable`), which runs inside an interpreter
//! that definitely has Playwright and whose verdict is what both UIs actually render.
//! This manager only has to get a Chrome *started* and never start a second one.
//!
//! # …and the profile directory is DERIVED from the browser brand
//! Chrome-for-Testing and system Google Chrome unlock their cookie stores with DIFFERENT
//! macOS Keychain items (`Chromium Safe Storage` vs `Chrome Safe Storage`). Point the second
//! one at the first one's profile and the decryption fails and Chrome DELETES the cookies —
//! measured on a clone of a warmed profile: 18 cookies to 0, the live Instagram sessionid
//! included, unrecoverable afterwards. So the `--user-data-dir` is not configured, it is
//! COMPUTED: `profile_dir_for(base, brand_of(binary))` puts each brand in its own
//! subdirectory of the configured base. Two brands cannot open one directory because
//! neither can name the other's — there is nothing to mark, police, refuse or declare. See
//! [`profile_dir_for`]. A profile left directly in the base by the old design is surfaced
//! by [`legacy_profile_notice`] and otherwise never touched.
//!
//! # Port
//! The CDP port comes from `DesktopConfig` (default **9333** — the live-proven port, NOT
//! 9222). There is no hardcoded port here.
//!
//! # Binary resolution (ported from warm_chrome.sh precedence)
//! explicit `AIZU_CHROME_BINARY` / `CHROME_BIN` override → Playwright's bundled
//! Chrome-for-Testing (protocol-matched, the safe default) → system Chrome (last resort: a
//! different BRAND, so it simply lands in its own `<base>/chrome` directory — there is no
//! guard to refuse it, which is the point of deriving the path).
//! Launch flags mirror the shell: `--remote-debugging-port`, a DEDICATED `--user-data-dir`,
//! `--no-first-run`, `--no-default-browser-check`.
//!
//! ## …and the Chrome-for-Testing tier has to work on a PACKAGED box
//! That middle tier used to be resolved by shelling to a dev-tree venv Python
//! (`engine/.venv/bin/python`, found RELATIVE TO CWD). A packaged install has no such tree
//! and a Finder/LaunchAgent launch inherits no shell profile, so on the only machines this
//! app ships to, the tier silently evaluated to nothing and resolution fell through to
//! **system Chrome** — which on 149+ launches happily and then refuses `connect_over_cdp`.
//! The shell reported success (its own probe is `Unknown` with no interpreter), the sidecar
//! preflight reported `cdp_attachable: fail`, and the wizard's Chrome step could never go
//! green.
//!
//! The fix is the same trick as the job-child re-exec (ledger A2): the FROZEN SIDECAR
//! carries Playwright inside its own binary, so we ask *it* — `<sidecar> -m
//! aizu.worker.chrome_path` — instead of hunting for an interpreter. Order is
//! sidecar-binary first (the packaged reality, and the tier that must stay exercised in
//! development too), then the venv fallback, then system Chrome. See [`helper_path`] for
//! the three rules that make shelling out to Playwright safe here.
//!
//! ## …and when the browser is simply not on the box, we can DOWNLOAD it
//! The frozen sidecar carries Playwright's Node driver but no browsers (356 MB is not
//! going into a ~30 MB app), so a fresh packaged box resolves nothing and lands on system
//! Chrome. [`install_chrome_for_testing`] drives that same sidecar
//! (`-m aizu.worker.chrome_path --install`) to fetch Chrome-for-Testing into the per-user
//! `ms-playwright` cache the freeze pins, streaming progress to the wizard. That is the
//! ONLY remedy that works on a packaged box — `playwright install chromium` needs an
//! interpreter the operator does not have.

use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{AppHandle, Emitter};

use crate::config::SharedConfigCell;
use crate::errors::DesktopError;

/// Wait for a freshly launched Chrome to become CDP-attachable.
const LAUNCH_TIMEOUT_SEC: u64 = 15;
/// Poll interval while waiting for the launch to attach.
const LAUNCH_POLL_INTERVAL_MS: u64 = 500;
/// Timeout for a single CDP-attach probe — both the `connect_over_cdp` timeout handed to
/// Playwright AND the hard wall-clock deadline on the subprocess that runs it. The two used
/// to be one number and no number: the probe passed `timeout=5000` to Playwright and then
/// waited on `Command::status()` with NO deadline at all, so an interpreter that wedged
/// before it ever reached the attach ran forever inside the boot budget. See
/// [`exit_code_within`].
const CDP_PROBE_TIMEOUT_SEC: u64 = 5;
/// Grace between terminate and kill when we stop a Chrome we own.
const STOP_GRACE_SEC: u64 = 5;
/// The Chrome-for-Testing download, measured — not the ~150 MB an earlier estimate guessed.
/// It appears in the operator copy because nobody should start a multi-minute download on a
/// worker PC's link without being told what it costs first.
const CHROME_DOWNLOAD_SIZE: &str = "356 MB";

/// Fixed launch flags — never magic strings scattered across call sites.
const NO_FIRST_RUN: &str = "--no-first-run";
const NO_DEFAULT_BROWSER_CHECK: &str = "--no-default-browser-check";

/// The sidecar sub-module that prints Playwright's Chrome-for-Testing path on stdout.
/// `desktop/pyinstaller/run_sidecar.py` dispatches this argv to it; an argv that dispatch
/// table does NOT recognise falls through and boots a whole second sidecar, so this string
/// and that table are ONE unit (ledger A2). Never send an argv the table does not know.
const CHROME_PATH_MODULE: &str = "aizu.worker.chrome_path";

/// The flag that turns that same module into an INSTALLER (contract E). Same module, same
/// dispatch-table entry, same stdout contract — so a caller can use `--install` and plain
/// resolution identically. Progress arrives on stderr.
const CHROME_INSTALL_FLAG: &str = "--install";

/// Budget for asking the frozen sidecar for the Chrome-for-Testing path. It has to start a
/// PyInstaller onedir bundle and Playwright's Node driver, so it is not instant.
///
/// # The boot budget, counted honestly
/// `resolve_chrome_binary` runs inside `ChromeManager::launch`, which runs while boot holds
/// the `Arc<Mutex<ChromeManager>>` that app-exit also blocks on, under
/// `main::CHROME_BOOT_GRACE_SEC`. Blowing that ceiling re-creates ledger F-2 (the sidecar
/// preflight beats Chrome up → `capabilities: []` → a false fatal → a 30s park → two
/// `register_worker` calls, i.e. two `worker_token_hash` rotations, on a healthy box).
///
/// The worst path through [`ChromeManager::ensure_running`] is:
///
/// | step                                   | cost |
/// |----------------------------------------|------|
/// | TCP pre-check on a filtered port       | 0.8s |
/// | sidecar helper (this)                  | 8.0s |
/// | its stdout grace                       | 0.5s |
/// | venv helper ([`CFT_VENV_TIMEOUT_SEC`])  | 4.0s |
/// | its stdout grace                       | 0.5s |
/// | launch-attach loop                     | 15.0s |
/// | its trailing TCP pre-check             | 0.8s |
///
/// = **29.6s**. That table is only true because two things now hold it up: [`exit_code_within`]
/// bounds the CDP probe (it used to be an unbounded `Command::status()`), and the
/// launch-attach loop clamps each probe to its own REMAINING time (the deadline used to be
/// checked at loop entry only, so a final iteration entered at 14.9s ran seconds past it).
/// The old comment claimed 28.8s while the code could not honour it. Raising any row here
/// without raising `main::CHROME_BOOT_GRACE_SEC` walks boot back into F-2 —
/// `the_worst_case_launch_budget_fits_under_the_boot_grace` fails the build if it does.
const CFT_SIDECAR_TIMEOUT_SEC: u64 = 8;
/// Same, for the dev-tree venv interpreter — a plain `python -c`, no bundle to unpack.
const CFT_VENV_TIMEOUT_SEC: u64 = 4;
/// How long we keep waiting for the helper's first stdout line after it has already exited.
/// The line is normally sitting in the pipe by then; this only covers reader-thread wakeup.
const HELPER_STDOUT_GRACE_MS: u64 = 500;
/// Poll cadence while waiting for a helper to exit.
const HELPER_POLL_MS: u64 = 50;

/// Ceiling on the browser download. Deliberately enormous next to every other timeout in
/// this file: it is 356 MB over whatever link a worker PC happens to have, which is MINUTES
/// even when everything is healthy. It is not on the boot path and it holds no lock, so the
/// only thing this bounds is a download that has genuinely died — nothing else waits on it.
const CHROME_INSTALL_TIMEOUT_SEC: u64 = 20 * 60;
/// Tauri event carrying ONE line of download progress to the wizard, as it happens. A
/// multi-minute action with no output is indistinguishable from a hung one.
pub const CHROME_INSTALL_PROGRESS_EVENT: &str = "chrome-install-progress";

/// Payload for [`CHROME_INSTALL_PROGRESS_EVENT`] — one stderr line of the installer's
/// output, verbatim. The installer writes progress to stderr precisely so stdout can stay
/// the one-path answer channel.
#[derive(Debug, Clone, Serialize)]
pub struct ChromeInstallProgress {
    pub line: String,
}

/// What [`ChromeManager::ensure_running`] settled on.
///
/// The url used to be the whole answer, and the other half — "…but the only browser on this
/// box is the SYSTEM Chrome" — existed only as an `eprintln!` on a GUI process with no
/// terminal attached. That silence is the precise failure this preflight/wizard work exists
/// to end: `ensure_running` returned `Ok`, boot recorded `ChromeBoot::Ready`,
/// `startup_error` stayed null, the wizard painted the Chrome step GREEN — and the next
/// preflight painted `cdp_attachable` RED with a remedy ("quit that Chrome completely and
/// relaunch") that can never work, because relaunching produces the identical system Chrome.
/// The operator loops forever with no way to learn the real cause.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChromeReady {
    /// The CDP url this box settled on.
    pub cdp_url: String,
    /// Operator copy for a DEGRADED success: we started a browser, but not the one the
    /// engine needs. `None` on every healthy path. Surfaced through `AppState` into the
    /// wizard footer and the dashboard's shell-problem strip (`commands::get_shell_note`).
    pub degradation: Option<String>,
}

// No `ChromeReady::at(url)` convenience constructor on purpose. It existed, and every call
// site that used it was a place that hard-coded `degradation: None` — which is precisely how
// the sticky note got dropped on the attach path. Every construction now has to say what it
// does with the note, and `attach_branch_degradation` is the single place that decides.

/// What a CDP probe could actually establish. See the module note on why the third
/// variant exists: collapsing it into `Rejected` fabricates a failure.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CdpProbe {
    /// `connect_over_cdp` really attached.
    Attached,
    /// A CDP endpoint answered but refused the attach — the B6/D3 "degraded Chrome".
    Rejected,
    /// No interpreter available to run the probe. Says nothing about Chrome.
    Unknown,
}

pub struct ChromeManager {
    config: SharedConfigCell,
    /// Needed ONLY to resolve the sidecar binary's path (`resource_dir()`), which is where
    /// a packaged box's only Playwright lives. See [`ChromeManager::resolve_chrome_for_testing`].
    app: AppHandle,
    /// The handle to a Chrome WE launched, or None if we attached to an existing one.
    handle: Option<Child>,
    /// True only when this manager launched the Chrome (governs `kill_on_app_exit`).
    owned: bool,
    /// Set by [`ChromeManager::resolve_chrome_binary`] when it fell through to system
    /// Chrome, cleared again if the launch then proved attachable from here. Recomputed on
    /// every `ensure_running` and handed out in [`ChromeReady::degradation`] — it is state
    /// about the LAST resolution, never a sticky flag.
    system_chrome_fallback: Option<String>,
    /// The degraded-launch fact, which OUTLIVES the call that discovered it.
    ///
    /// `system_chrome_fallback` above is per-resolution and only survives a call that
    /// actually reaches `launch()`. That is not enough, and the gap restored the exact
    /// silent dead end this whole feature exists to close: on a PACKAGED box `cdp_probe`
    /// can only ever return `Unknown` (there is no interpreter to probe with), so the
    /// second `ensure_running` — one more click of "Launch warmed Chrome", or the next app
    /// start — takes the attach branch, finds the wrong Chrome we started still holding
    /// the port, and used to return `degradation: None`. That wiped the footer note, painted
    /// the step green, and left `cdp_attachable` red under a remedy ("quit that Chrome and
    /// relaunch") that reproduces the identical browser forever.
    ///
    /// So the fact is sticky until something actually changes it: a launch that resolves a
    /// real Chrome-for-Testing clears it, and so does a port that proves genuinely
    /// `Attached`. Nothing else may.
    degraded_launch: Option<String>,
}

impl ChromeManager {
    pub fn new(config: SharedConfigCell, app: AppHandle) -> Self {
        Self {
            config,
            app,
            handle: None,
            owned: false,
            system_chrome_fallback: None,
            degraded_launch: None,
        }
    }

    /// Drop our claim on a Chrome that is NOT our live child.
    ///
    /// The attach branches used to clear `handle`/`owned` unconditionally, which meant a
    /// Chrome THIS app started stopped being ours the moment a later `ensure_running` saw
    /// the port answering — so `kill_on_app_exit` left it running, and the next launch
    /// found the port poisoned by a browser nobody would clean up. Only a Chrome that is
    /// gone, or that was never ours, may be forgotten.
    fn forget_unowned(&mut self) {
        let still_our_child = match self.handle.as_mut() {
            // `Ok(None)` from try_wait means "running"; anything else means it is gone.
            Some(child) => matches!(child.try_wait(), Ok(None)),
            None => false,
        };
        if !still_our_child {
            self.handle = None;
            self.owned = false;
        }
    }

    /// Reconnect to an attachable Chrome if present, else launch ONE. Idempotent — safe to
    /// call on every app start AND from the wizard's "Launch warmed Chrome" button.
    ///
    /// Returns [`ChromeReady`] — the CDP url it settled on, so the caller can show the
    /// operator *where* the Chrome is, PLUS the degraded-success copy when the browser we
    /// started is not the one the engine needs. Errors:
    ///   - `ChromeAttachFailed` if a Chrome is listening and DEMONSTRABLY rejects CDP (we
    ///     will NOT kill a process we did not launch — the operator must quit the stale
    ///     Chrome), or if no binary was found, or if a launch never becomes attachable.
    ///
    /// The error string is the wizard's Chrome-step failure copy. It used to go to an
    /// `eprintln!` that a GUI operator never sees — that silence is half of the bug this
    /// work exists to kill, and [`ChromeReady::degradation`] is the other half.
    pub fn ensure_running(&mut self) -> Result<ChromeReady, DesktopError> {
        let cdp_url = self.config.get().cdp_url();
        // Every call recomputes it: a box the operator just fixed (a Download, a CHROME_BIN
        // export) must not keep reporting the last run's fall-through.
        self.system_chrome_fallback = None;

        // Cheap pre-check: is anything listening at all? If yes, gate on the REAL attach.
        if http_precheck(&cdp_url) {
            match cdp_probe(&cdp_url, CDP_PROBE_TIMEOUT_SEC) {
                CdpProbe::Attached => {
                    self.forget_unowned();
                    eprintln!("[chrome] attached to existing Chrome at {cdp_url}");
                    // Nothing was resolved and nothing was launched: an already-attachable
                    // Chrome is by definition the right one, whatever binary it came from.
                    // This is also the ONE observation that can honestly retire a sticky
                    // degraded-launch note — the engine can attach, so the browser on this
                    // port is fit for purpose whatever we thought when we started it.
                    let degradation =
                        attach_branch_degradation(CdpProbe::Attached, &self.degraded_launch);
                    self.degraded_launch = None;
                    return Ok(ChromeReady { cdp_url, degradation });
                }
                CdpProbe::Unknown => {
                    // Something owns the port. Launching a second Chrome on the same
                    // --remote-debugging-port would fail anyway, and killing a browser we
                    // did not start is forbidden. Report the port as taken and let the
                    // sidecar preflight deliver the real attach verdict.
                    self.forget_unowned();
                    eprintln!(
                        "[chrome] a CDP endpoint answers at {cdp_url}; could not verify the \
                         attach from here (no interpreter, no Playwright in it, or the probe \
                         ran out of time) — the worker preflight decides"
                    );
                    // Carry the sticky note forward. On a packaged box this branch is the
                    // ONLY one reachable, so returning `None` here — as this did — is what
                    // erased the operator's one explanation on their second click and put
                    // the green-UI/red-preflight loop back. "Unverifiable" is not "fine".
                    return Ok(ChromeReady {
                        cdp_url,
                        degradation: attach_branch_degradation(
                            CdpProbe::Unknown,
                            &self.degraded_launch,
                        ),
                    });
                }
                CdpProbe::Rejected => {
                    return Err(DesktopError::ChromeAttachFailed(format!(
                        "Chrome at {cdp_url} answers HTTP but rejects connect_over_cdp — \
                         a stale or degraded browser session. Quit that Chrome COMPLETELY \
                         — do not just reload a tab — then launch it again from here. This \
                         app will not kill a browser it did not launch."
                    )));
                }
            }
        }

        self.launch(&cdp_url)?;
        // Promote this resolution's verdict to the sticky one. A healthy launch (a real
        // Chrome-for-Testing) stores `None` and thereby CLEARS a stale note — which is how
        // an operator who has just run Download stops being told about the browser they
        // already replaced.
        let degradation = self.system_chrome_fallback.take();
        self.degraded_launch = degradation.clone();
        Ok(ChromeReady { cdp_url, degradation })
    }

    fn launch(&mut self, cdp_url: &str) -> Result<(), DesktopError> {
        let binary = self.resolve_chrome_binary()?;
        let cfg = self.config.get();
        let argv = launch_argv(&cfg.chrome_profile_base, &binary, cfg.cdp_port);
        eprintln!("[chrome] launching managed Chrome-for-Testing: {}", binary.display());
        let child = Command::new(&binary)
            .args(&argv)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| DesktopError::ChromeAttachFailed(format!("launch {}: {e}", binary.display())))?;
        self.handle = Some(child);
        self.owned = true;

        // Gate on a REAL attach (not HTTP 200) before returning — same as warm_chrome.sh.
        // With no interpreter to probe with, fall back to "the port came up", which is the
        // most this shell can honestly assert; the sidecar preflight asserts the rest.
        //
        // Each probe is clamped to the time this loop has LEFT, not to a flat 5s. The
        // deadline used to be tested at loop entry only, so an iteration entered at 14.9s
        // was free to run a full probe past it — which is how a "15s" launch wait became a
        // ~20s+ one and walked boot past `main::CHROME_BOOT_GRACE_SEC` (ledger F-2).
        let deadline = Instant::now() + Duration::from_secs(LAUNCH_TIMEOUT_SEC);
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            let Some(attach_sec) = probe_budget_within(remaining) else {
                break; // not enough time left for an honest probe — stop, do not overrun
            };
            match cdp_probe(cdp_url, attach_sec) {
                CdpProbe::Attached => {
                    eprintln!("[chrome] CDP up and attachable at {cdp_url}");
                    // The fall-through warning is about a browser the engine cannot attach
                    // to. If `connect_over_cdp` demonstrably WORKS against the one we just
                    // started, saying otherwise would be a false red — and a false red on a
                    // healthy box is the worst outcome this feature has.
                    self.system_chrome_fallback = None;
                    return Ok(());
                }
                CdpProbe::Unknown if http_precheck(cdp_url) => {
                    eprintln!("[chrome] CDP port is up at {cdp_url} (attach unverified here)");
                    return Ok(());
                }
                _ => {}
            }
            std::thread::sleep(Duration::from_millis(LAUNCH_POLL_INTERVAL_MS));
        }
        Err(DesktopError::ChromeAttachFailed(format!(
            "Chrome launched but did not become CDP-attachable within {LAUNCH_TIMEOUT_SEC}s at \
             {cdp_url}. Check that no other Chrome already owns that port, and that the profile \
             directory is writable."
        )))
    }

    /// Kill a Chrome THIS manager launched. A no-op when we only attached — a Chrome we did
    /// not start must outlive us (LOCKED reconnect-never-kill semantics; the warmed login
    /// must survive a sidecar/app restart). Called ONLY on app exit.
    pub fn kill_on_app_exit(&mut self) {
        if !self.owned {
            eprintln!("[chrome] attached-only — leaving Chrome running (login must survive)");
            return;
        }
        let Some(child) = self.handle.as_mut() else {
            return;
        };
        // Terminate, wait a grace, then kill.
        let pid = child.id();
        #[cfg(unix)]
        unsafe {
            libc_kill(pid as i32, SIGTERM);
        }
        #[cfg(windows)]
        {
            let _ = pid; // TerminateProcess path handled by kill() below on timeout.
        }
        let deadline = Instant::now() + Duration::from_secs(STOP_GRACE_SEC);
        loop {
            if Instant::now() >= deadline {
                let _ = child.kill();
                break;
            }
            match child.try_wait() {
                Ok(Some(_)) => break,
                Ok(None) => std::thread::sleep(Duration::from_millis(200)),
                Err(_) => {
                    let _ = child.kill();
                    break;
                }
            }
        }
        let _ = child.wait();
        self.handle = None;
        self.owned = false;
    }

    /// Resolve the Chrome binary (precedence ported from warm_chrome.sh / chrome_manager.py):
    /// binary override → Playwright Chrome-for-Testing (protocol-matched) → system Chrome.
    ///
    /// The override is first and is read from THIS process's environment — which a
    /// Finder-launched app inherits from launchd, not from the operator's shell profile. It
    /// is an escape hatch for a terminal-launched dev box, not the packaged path.
    ///
    /// Two spellings, `AIZU_CHROME_BINARY` then `CHROME_BIN` — see
    /// [`chrome_binary_override`] for why that ORDER is load-bearing and identical in all
    /// three implementations. Honouring only one of them is not tidier: an operator
    /// following the worker's own remedy text would be silently ignored HERE while being
    /// obeyed one process away, and the two halves of the same app would start different
    /// browsers against different profile directories.
    ///
    /// Takes `&mut self` for ONE reason: the system-Chrome tier records why it was reached
    /// in [`ChromeManager::system_chrome_fallback`], so `ensure_running` can hand that fact
    /// to the UI instead of leaving it in an `eprintln!` nobody reads.
    fn resolve_chrome_binary(&mut self) -> Result<PathBuf, DesktopError> {
        if let Some((var, explicit)) = chrome_binary_override() {
            let p = PathBuf::from(&explicit);
            if p.exists() {
                return Ok(p);
            }
            return Err(DesktopError::ChromeAttachFailed(format!(
                "{var} does not exist: {explicit}"
            )));
        }
        // Already existence-checked inside `accept_helper_output` — the ONE place that stat
        // lives. Do not re-add a guard here and do not remove it there: Playwright hands
        // back a well-formed path for a browser build that may never have been downloaded.
        if let Some(cft) = self.resolve_chrome_for_testing() {
            return Ok(cft);
        }
        for candidate in system_chrome_candidates() {
            if candidate.exists() {
                // This branch is how the box ends up on system Chrome — a browser whose
                // CDP surface is not the one the installed Playwright is built against, and
                // whose only symptom when it does not attach is `cdp_attachable: fail` in a
                // preflight two layers away, under a remedy that cannot work. The note is
                // provisional: if the launch below then proves genuinely `Attached`, the
                // loop CLEARS it rather than scaring an operator whose Chrome is fine. The
                // eprintln! stays for the log; the string that reaches the OPERATOR is the
                // one recorded here.
                let note = system_chrome_degradation(&candidate);
                eprintln!("[chrome] WARNING: {note}");
                self.system_chrome_fallback = Some(note);
                return Ok(candidate);
            }
        }
        Err(DesktopError::ChromeAttachFailed(format!(
            "No Chrome on this box at all — neither Playwright's Chrome-for-Testing nor a \
             system Chrome. Use “Download browser” on this step (≈{CHROME_DOWNLOAD_SIZE}, \
             one time) to install the matched browser, or set CHROME_BIN to one you already \
             have."
        )))
    }

    /// Ask for Playwright's bundled Chrome-for-Testing path — the protocol-matched binary
    /// `warm_chrome.sh` prefers over system Chrome.
    ///
    /// 1. **The frozen sidecar binary** (`<sidecar> -m aizu.worker.chrome_path`). On a
    ///    packaged box this is the only Playwright on the machine, and it is FIRST rather
    ///    than a last-ditch fallback so that development exercises the same path production
    ///    depends on. A dev tree with a stale `engine/.venv` would otherwise keep taking
    ///    the old branch and the packaged fix would ship untested.
    /// 2. **The dev-tree venv Python**, unchanged, so a checkout with no built sidecar
    ///    still resolves.
    ///
    /// Every failure is a `None` that falls through to the next tier — never an error. The
    /// worst outcome available here is landing on system Chrome, and `resolve_chrome_binary`
    /// says so loudly when it happens.
    fn resolve_chrome_for_testing(&self) -> Option<PathBuf> {
        match crate::sidecar_supervisor::resolve_sidecar_binary(
            &self.app,
            &self.config.get().sidecar_binary_path,
        ) {
            Ok(binary) => {
                let mut cmd = Command::new(&binary);
                cmd.arg("-m").arg(CHROME_PATH_MODULE);
                // NOT belt-and-braces: `resolve_sidecar_binary`'s dev tier returns the bare
                // name `aizu-worker`, i.e. the pip console script, which is
                // `sidecar:main` with NO argv dispatch (the dispatch table lives in the
                // FROZEN entry shim). That binary would ignore this argv and boot a real
                // sidecar — a second registration racing the supervised one. An empty
                // AIZU_DISPATCH_URL makes `WorkerConfig.from_env()` raise before anything
                // opens a DB, binds a control surface, or registers the box. The frozen
                // binary never reaches that code, so this is inert on the path that works.
                cmd.env("AIZU_DISPATCH_URL", "");
                if let Some(path) = helper_path(cmd, Duration::from_secs(CFT_SIDECAR_TIMEOUT_SEC)) {
                    eprintln!(
                        "[chrome] Chrome-for-Testing resolved via the sidecar binary: {}",
                        path.display()
                    );
                    return Some(path);
                }
            }
            // A configured-but-missing `sidecar_binary_path` is a SIDECAR error; surfacing
            // it out of Chrome resolution would put the wrong copy in front of the operator.
            Err(e) => eprintln!("[chrome] no sidecar binary to ask for Chrome-for-Testing: {e}"),
        }

        let py = venv_python()?;
        let mut cmd = Command::new(py);
        cmd.arg("-c").arg(CFT_VENV_SNIPPET);
        let path = helper_path(cmd, Duration::from_secs(CFT_VENV_TIMEOUT_SEC))?;
        eprintln!(
            "[chrome] Chrome-for-Testing resolved via the dev venv: {}",
            path.display()
        );
        Some(path)
    }
}

/// The operator's explicit browser override, and WHICH variable supplied it.
///
/// Two names, one knob: `CHROME_BIN` is what the shipped `warm_chrome.sh` reads and is
/// canonical; `AIZU_CHROME_BINARY` is what `aizu.worker.chrome_manager` reads. The name is
/// returned with the value so a bad path is reported against the variable the operator
/// actually set — "CHROME_BIN does not exist" in front of someone who set the other one is
/// a message that sends them looking in the wrong place. Blank is not a setting: an empty
/// export must fall through to resolution, not fail the launch with an empty path.
fn chrome_binary_override() -> Option<(&'static str, String)> {
    // ORDER MATTERS and must match warm_chrome.sh and chrome_manager.py exactly. It did not:
    // bash read AIZU_CHROME_BINARY first while this and the Python read CHROME_BIN first, so
    // a box with BOTH set had the launcher warm <base>/chrome-for-testing while the worker
    // opened <base>/chrome. Since the binary now chooses the PROFILE DIRECTORY, a precedence
    // disagreement is a split-brain about which logins exist.
    ["AIZU_CHROME_BINARY", "CHROME_BIN"].into_iter().find_map(|var| {
        let value = std::env::var(var).ok()?;
        let value = value.trim().to_string();
        (!value.is_empty()).then_some((var, value))
    })
}

/// The complete argv for a launch: the derived per-brand profile plus the two anti-nag
/// flags, mirroring warm_chrome.sh.
///
/// This is the WHOLE of `launch`'s decision, in one pure function, on purpose. The previous
/// round's blocker was a correct helper that the call site never consulted — a declaration
/// button whose answer `resolve_chrome_binary` did not read — so the derivation and the
/// argv are not two things a caller has to remember to combine. Hand it a base and a binary
/// and there is no way to produce a `--user-data-dir` that is not `<base>/<brand>`.
///
/// The dedicated `--user-data-dir` is not a preference either: Chrome refuses
/// `--remote-debugging-port` on the default profile, so pointing this at the operator's
/// everyday profile produces a Chrome that starts and never listens.
fn launch_argv(base: &Path, binary: &Path, cdp_port: u16) -> Vec<String> {
    // Pointing one Chrome BRAND at a profile another brand warmed DELETES every saved login
    // in it — proven on a clone of a warmed profile, 18 cookies to 0, the live Instagram
    // sessionid included, unrecoverable afterwards by any browser. Deriving the directory
    // from the brand makes that unreachable rather than guarded.
    let profile_dir = profile_dir_for(base, brand_of(binary));
    // Best-effort: Chrome creates the profile dir itself, but creating it here turns a
    // permissions problem into an error at LAUNCH time instead of a silent no-listen.
    let _ = std::fs::create_dir_all(&profile_dir);
    vec![
        format!("--remote-debugging-port={cdp_port}"),
        format!("--user-data-dir={}", profile_dir.display()),
        NO_FIRST_RUN.to_string(),
        NO_DEFAULT_BROWSER_CHECK.to_string(),
    ]
}

/// The operator copy for the system-Chrome fall-through (see [`ChromeReady`]).
///
/// A free function so the WORDING is testable without an `AppHandle`, a display or a
/// Chrome — the same reason the acceptance rule lives outside the process plumbing.
///
/// It names the in-app **Download browser** action and nothing else, because on a packaged
/// box every other remedy is a dead end: `playwright install chromium` needs an interpreter
/// and a CLI the operator does not have, and "quit that Chrome and relaunch" — the remedy
/// the preflight's own `cdp_attachable` row offers — produces the identical system Chrome.
fn system_chrome_degradation(binary: &Path) -> String {
    format!(
        "No Chrome-for-Testing on this box — started the system Chrome at {} instead, and \
         could not confirm from here that the engine can attach to it. Its CDP surface is \
         not the one the installed Playwright is built against, so the worker's \
         cdp_attachable check may go red, and relaunching this same browser will never \
         clear it. Use “Download browser” on the Chrome step (≈{CHROME_DOWNLOAD_SIZE}, one \
         time) to install the browser the engine expects.",
        binary.display()
    )
}

// --- the profile directory is DERIVED from the browser brand -------------------------
//
// Pointing a browser of one brand at a profile directory another brand warmed destroys the
// logins in it, silently and irreversibly. Chrome-for-Testing unlocks its cookie store with
// the macOS Keychain item `Chromium Safe Storage`; system Google Chrome writes
// `Chrome Safe Storage`. Wrong key -> decryption fails -> Chrome DELETES the rows rather
// than quarantining them, and the move-aside/snapshot machinery that would have saved a copy
// is `#if BUILDFLAG(IS_WIN)` — on macOS nothing is moved aside. Measured on a CLONE of a
// warmed profile: 18 cookies to 0, the live Instagram sessionid among them, unreadable
// afterwards by the browser that wrote it. The version difference is NOT the cause — the
// SAME system Chrome build run with `--use-mock-keychain` (identical version, wrong key)
// lost the identical everything.
//
// Three rounds tried to let two brands SHARE one directory safely — a marker file, a
// decision table, a refusal, an operator declaration, a wizard question — and every round
// fixed the last one's hole and opened a new one, ending with a declaration button whose
// answer no launch site ever read. The shape was wrong: a profile is owned by exactly one
// brand, so the DIRECTORY is a function of the brand. `<base>/chrome-for-testing` and
// `<base>/chrome` cannot collide, there is nothing to mark, nothing to police, nothing to
// refuse, and no question anyone can answer wrong.

const BRAND_CHROME_FOR_TESTING: &str = "chrome-for-testing";
const BRAND_CHROME: &str = "chrome";
/// Debian/Ubuntu Chromium — a THIRD brand with its own keyring entry, not a spelling of
/// Chrome. Only reachable on Linux, where the system-Chrome fallback list carries
/// /usr/bin/chromium next to /usr/bin/google-chrome.
const BRAND_CHROMIUM: &str = "chromium";
const DISTRO_CHROMIUM_NAMES: [&str; 2] = ["chromium", "chromium-browser"];
/// What a Chrome-for-Testing binary path contains, matched case-insensitively — the app
/// bundle Playwright installs is literally `Google Chrome for Testing.app`.
const CFT_BINARY_MARKER: &str = "chrome for testing";
/// A directory that contains THIS is one a browser has actually run against, i.e. one with
/// something to lose. Statting it is the whole test: reading, copying or opening the cookie
/// database to learn more would risk the very thing this exists to protect.
const PROFILE_USED_DIR: &str = "Default";

/// The brand token for a browser binary, by path — the FIXED CONTRACT, implemented
/// identically here, in `aizu.worker.chrome_manager` and in `engine/scripts/warm_chrome.sh`.
///
/// Symlinks are resolved FIRST, then:
///   1. the path contains `chrome for testing`   -> chrome-for-testing
///   2. any path SEGMENT matches `^chromium(_headless_shell)?-[0-9]+$`
///                                               -> chrome-for-testing
///   3. otherwise                                -> chrome
///
/// Resolving first is not tidiness: a wrapper script or a `~/bin/chrome` symlink is exactly
/// how a Playwright build reaches a launch under a name that says nothing, and a wrong
/// answer here now picks the wrong DIRECTORY — the browser would open a profile the other
/// brand warmed and delete every cookie in it. A path that does not exist (a `CHROME_BIN`
/// typo, a synthetic path in a test) cannot be canonicalised and is judged as written.
///
/// Rule 1 alone — which is all this used to be — is a macOS-only test. Playwright's own
/// `EXECUTABLE_PATHS` table (driver `coreBundle.js`) resolves chromium to
/// `chrome-linux64/chrome` on linux-x64, `chrome-linux/chrome` on linux-arm64 and
/// `chrome-win64/chrome.exe` on win-x64 — not one of them carries the string. Rule 2 is what
/// holds on every platform: the browsers cache lays every build out as
/// `<name with - as _>-<revision>` (`readDescriptors`, same file), so Chrome-for-Testing
/// always sits under a `chromium-1234` / `chromium_headless_shell-1234` directory whatever
/// the leaf is called.
pub fn brand_of(binary: &Path) -> &'static str {
    let resolved = std::fs::canonicalize(binary).unwrap_or_else(|_| binary.to_path_buf());
    // Backslashes normalised so a Windows path segments the same way as a POSIX one; the
    // token is a cross-platform contract and `\` is not a separator on unix.
    let p = resolved.to_string_lossy().to_lowercase().replace('\\', "/");
    if p.contains(CFT_BINARY_MARKER) {
        return BRAND_CHROME_FOR_TESTING;
    }
    if p.split('/').any(is_playwright_browser_dir) {
        return BRAND_CHROME_FOR_TESTING;
    }
    // Rule 3 (Linux): distro Chromium is a THIRD brand, not a spelling of Chrome. It seals
    // cookies under its own keyring entry, so filing it under `chrome` — as the two-token
    // rule did — hands /usr/bin/chromium and /usr/bin/google-chrome one directory and wipes
    // whichever warmed it first. Matched on the file NAME (`.exe` stripped so Windows and
    // POSIX agree); rule 2 has already claimed anything inside Playwright's cache.
    let leaf = p.rsplit('/').next().unwrap_or("");
    let leaf = leaf.strip_suffix(".exe").unwrap_or(leaf);
    if DISTRO_CHROMIUM_NAMES.contains(&leaf) {
        return BRAND_CHROMIUM;
    }
    BRAND_CHROME
}

/// One path segment of Playwright's browsers cache: `chromium-1234`,
/// `chromium_headless_shell-1234`. Hand-rolled rather than a regex because the crate has no
/// regex dependency and the shape is fixed by Playwright's own directory naming.
fn is_playwright_browser_dir(segment: &str) -> bool {
    let Some(rest) = segment.strip_prefix("chromium") else {
        return false;
    };
    let rest = rest.strip_prefix("_headless_shell").unwrap_or(rest);
    let Some(revision) = rest.strip_prefix('-') else {
        return false;
    };
    !revision.is_empty() && revision.bytes().all(|b| b.is_ascii_digit())
}

/// The `--user-data-dir` for `brand` under `base` — the ONE derivation, used by every
/// launch site in Rust, and mirrored byte-for-byte in Python and bash.
///
/// The path IS the ownership record. Two brands can never open one directory because
/// neither can name the other's, so there is nothing left to detect, mark or refuse.
pub fn profile_dir_for(base: &Path, brand: &str) -> PathBuf {
    base.join(brand)
}

/// The brand token as an operator would name the browser.
fn brand_label(token: &str) -> &str {
    match token {
        BRAND_CHROME_FOR_TESTING => "Chrome for Testing",
        BRAND_CHROME => "Google Chrome",
        other => other,
    }
}

/// A profile left directly in `base` by the design this replaced — warmed by a browser
/// whose brand nothing on disk records.
///
/// Returned as copy, never as a blocker and never as an action. The app must not open it
/// (the brand is unknowable, and guessing costs every login in it), must not move, rename,
/// copy, back up or delete it, and must not guess on the operator's behalf — guessing is
/// precisely what the last three rounds got wrong. So it says what is there, that it was
/// left alone, and prints BOTH destinations so an operator who KNOWS which browser warmed
/// it can move it themselves with a copy-paste rather than a puzzle.
///
/// `None` — the common case — when `base` holds only brand subdirectories, which is every
/// box set up after this change.
/// The profile a box warmed at the desktop shell's FORMER default location.
///
/// Until the base was unified, the shell defaulted to `<app data>/chrome-profile` while
/// `warm_chrome.sh` used `~/.aizu-cft-profile` and the worker preflight used a third path
/// (ledger A12). Unifying them is what makes the shell, the sidecar and the launcher open
/// the same directory — the shell now exports the base to its child — but a box that took
/// the OLD default has real warmed logins at a path nothing looks at any more.
///
/// Nothing is moved. Left unsaid, that box simply looks signed-out with no cause attached,
/// which is the failure this whole arc exists to stop; said out loud, the operator can carry
/// it over in one move. The brand is as unknowable here as for any legacy profile, so this
/// names both destinations and guesses nothing.
pub fn former_default_profile_notice(former_base: &Path, current_base: &Path) -> Option<String> {
    // Nothing to say when the box never used the old location, or is still pointed at it.
    if former_base == current_base || !former_base.join(PROFILE_USED_DIR).is_dir() {
        return None;
    }
    Some(format!(
        "This box has a warmed Chrome profile at {former} — where Aizu used to keep it. It \
         now uses {current}, so those sign-ins are not being used, and nothing has been \
         moved or deleted. To carry them over, move that {used}/ folder into whichever of \
         {cft} ({cft_label}) or {chrome} ({chrome_label}) matches the browser that warmed \
         it. Two Chrome builds unlock saved cookies with DIFFERENT system keys, so the wrong \
         one would DELETE every login in it — and nothing on disk records which browser it \
         was, which is why this app will not pick for you. If you are not sure, leave it and \
         sign in again.",
        former = former_base.display(),
        current = current_base.display(),
        used = PROFILE_USED_DIR,
        cft = profile_dir_for(current_base, BRAND_CHROME_FOR_TESTING).display(),
        cft_label = brand_label(BRAND_CHROME_FOR_TESTING),
        chrome = profile_dir_for(current_base, BRAND_CHROME).display(),
        chrome_label = brand_label(BRAND_CHROME),
    ))
}

pub fn legacy_profile_notice(base: &Path) -> Option<String> {
    if !base.join(PROFILE_USED_DIR).is_dir() {
        return None;
    }
    Some(format!(
        "There is an older Chrome profile sitting directly in {base} (it has a \
         {used}/ folder). Nothing has been done to it — Aizu now keeps one profile per \
         browser, in {cft} ({cft_label}) and {chrome} ({chrome_label}), so this one is not \
         opened by anything any more. \
         Two Chrome builds unlock their saved cookies with DIFFERENT system keys, so the \
         wrong one opening this folder would DELETE every login in it, and nothing on disk \
         records which browser warmed it — which is why this app will not guess. If YOU \
         know which one it was, move the folder's contents into the matching directory \
         above yourself and sign-ins carry over; otherwise leave it and sign in again in \
         the new profile.",
        base = base.display(),
        used = PROFILE_USED_DIR,
        cft = profile_dir_for(base, BRAND_CHROME_FOR_TESTING).display(),
        cft_label = brand_label(BRAND_CHROME_FOR_TESTING),
        chrome = profile_dir_for(base, BRAND_CHROME).display(),
        chrome_label = brand_label(BRAND_CHROME),
    ))
}

/// Extra wall-clock the CDP probe gets on TOP of the `connect_over_cdp` timeout it hands to
/// Playwright: interpreter start + `import playwright` + driver launch + teardown all
/// happen outside that timeout. Too small and every probe is killed mid-attach and reads
/// `Unknown` (fabricating "we could not tell" on a box that could tell perfectly well).
const CDP_PROBE_KILL_MARGIN_SEC: u64 = 3;

/// The attach timeout a probe may use given `remaining` time, or `None` when there is not
/// enough left to run one honestly.
///
/// Pure, and separate from the loop, because it is the whole of defect 2: a probe is only
/// bounded if the caller subtracts the process overhead the probe itself cannot see.
fn probe_budget_within(remaining: Duration) -> Option<u64> {
    let floor = 1 + CDP_PROBE_KILL_MARGIN_SEC;
    if remaining < Duration::from_secs(floor) {
        return None;
    }
    Some((remaining.as_secs() - CDP_PROBE_KILL_MARGIN_SEC).min(CDP_PROBE_TIMEOUT_SEC))
}

/// Cheap "is anything listening" pre-check. A raw TCP connect to the CDP port; an HTTP GET
/// would work too but is heavier. Never the source of truth — `cdp_attaches` decides.
fn http_precheck(cdp_url: &str) -> bool {
    // Parse host:port out of the cdp_url; loopback only.
    let addr = cdp_url
        .trim_start_matches("http://")
        .trim_start_matches("https://")
        .split('/')
        .next()
        .unwrap_or("");
    std::net::TcpStream::connect_timeout(
        &match addr.parse() {
            Ok(a) => a,
            Err(_) => return false,
        },
        Duration::from_millis(800),
    )
    .is_ok()
}

/// The REAL detection signal: does Playwright's `connect_over_cdp(no_defaults=True)`
/// actually attach? We shell to the venv Python — the SAME probe `warm_chrome.sh` runs —
/// because only that exercises the exact protocol surface the engine uses. An HTTP 200 is
/// NOT sufficient (a degraded Chrome answers HTTP but rejects CDP).
///
/// TRI-state on purpose: no interpreter ⇒ `Unknown`, not `Rejected`. A missing probe is a
/// fact about THIS shell, not about Chrome, and reporting it as a rejected attach would
/// fabricate the one failure the operator can do nothing about. A probe that ran out of
/// wall clock is the same kind of fact, so it is `Unknown` too.
///
/// `timeout_sec` is the `connect_over_cdp` timeout; the SUBPROCESS gets that plus
/// [`CDP_PROBE_KILL_MARGIN_SEC`] and is killed at it. It used to get no deadline at all —
/// this runs on the boot path, in a loop, under `main::CHROME_BOOT_GRACE_SEC`.
fn cdp_probe(cdp_url: &str, timeout_sec: u64) -> CdpProbe {
    let Some(py) = venv_python() else {
        eprintln!("[chrome] no Playwright interpreter found — CDP attach unverified here");
        return CdpProbe::Unknown;
    };
    let mut cmd = Command::new(py);
    // The url and the timeout ride in as ARGV, never interpolated into the source: `python
    // -c CODE a b` puts them in `sys.argv[1..]`, so no quoting rule can turn a cdp_url into
    // Python code, and the script itself stays a fixed literal a test can compile.
    cmd.arg("-c")
        .arg(CDP_PROBE_SCRIPT)
        .arg(cdp_url)
        .arg((timeout_sec * 1000).to_string());
    match exit_code_within(cmd, Duration::from_secs(timeout_sec + CDP_PROBE_KILL_MARGIN_SEC)) {
        Some(0) => CdpProbe::Attached,
        // The interpreter could not even import Playwright. That is a fact about THIS
        // shell's interpreter, not about Chrome — exactly like having no interpreter at all
        // — so it must not be read as a rejected attach. `venv_python()` picks a `.venv` by
        // relative path and never asks what is installed in it; the sidecar's own
        // `playwright` check reports the missing dependency, precisely and once.
        Some(PROBE_RC_NO_PLAYWRIGHT) => {
            eprintln!(
                "[chrome] the probe interpreter has no Playwright — CDP attach unverified here"
            );
            CdpProbe::Unknown
        }
        // The interpreter ran, Playwright was there, and the attach did not succeed — that
        // IS the degraded-Chrome signal (B6/D3).
        Some(_) => CdpProbe::Rejected,
        None => CdpProbe::Unknown,
    }
}

/// Exit code the probe uses for "this interpreter cannot import Playwright", so the caller
/// can tell that apart from a browser that refused the attach. Any other non-zero code is a
/// real rejection. Kept in lockstep with the script by
/// `the_probe_reports_a_missing_playwright_with_its_own_exit_code`.
const PROBE_RC_NO_PLAYWRIGHT: i32 = 3;

/// The CDP attach probe, as Python source: **one Rust literal per Python line**, each
/// carrying its own `\n`.
///
/// Written this way because the obvious alternative is silently broken, and shipped broken.
/// A `format!` whose lines end in `\n\` reads perfectly in Rust and emits Python with every
/// indent gone — a trailing backslash eats the newline AND all leading whitespace on the
/// continuation line. The probe was built exactly like that, so it could never survive its
/// own `try:`:
///
/// ```text
/// IndentationError: expected an indented block after 'try' statement on line 4
/// ```
///
/// The interpreter ran and exited non-zero, which this function reads as "Chrome refused the
/// attach" — so `cdp_probe` could only ever answer [`CdpProbe::Rejected`], in every build,
/// and `ensure_running` told operators to "quit that Chrome COMPLETELY" about a perfectly
/// healthy browser. A false red on a healthy box is the worst outcome this feature has.
///
/// `concat!` of one line per literal makes that class of bug structurally impossible: there
/// is no continuation to eat anything. `the_probe_script_is_valid_python` and
/// `every_block_body_in_the_probe_script_is_indented` fail the build if anyone flattens it
/// back.
const CDP_PROBE_SCRIPT: &str = concat!(
    "import sys\n",
    // Import failure is NOT an attach failure — see `PROBE_RC_NO_PLAYWRIGHT`.
    "try:\n",
    "    from playwright.sync_api import sync_playwright\n",
    "except Exception:\n",
    "    raise SystemExit(3)\n",
    "url, timeout_ms = sys.argv[1], float(sys.argv[2])\n",
    "pw = sync_playwright().start()\n",
    "try:\n",
    "    b = pw.chromium.connect_over_cdp(url, no_defaults=True, timeout=timeout_ms)\n",
    // `close()` on a CDP-CONNECTED browser disconnects Playwright; it does not close the
    // operator's browser. Probing must never cost a warmed session.
    "    b.close()\n",
    "finally:\n",
    "    pw.stop()\n",
);

/// Run a command that answers with nothing but its EXIT CODE, under a HARD deadline.
/// `Some(code)` when it exited in time, `None` when it could not be spawned or had to be
/// killed at the deadline.
///
/// The CODE, not a bool: [`cdp_probe`] has to tell "Playwright is not installed in this
/// interpreter" (a fact about the probe) from "the browser refused the attach" (a fact about
/// Chrome), and collapsing the two fabricates a degraded-Chrome verdict. A child killed by a
/// signal has no code at all — that is a `None`, same as a timeout, for the same reason: it
/// says nothing about Chrome.
///
/// This exists because `Command::status()` waits forever. That was tolerable nowhere and
/// actively dangerous here: [`cdp_probe`] shells a Python interpreter from inside the
/// launch-attach loop, on the boot path, holding the mutex app-exit blocks on, under
/// `main::CHROME_BOOT_GRACE_SEC` — so one wedged Node driver spent the whole boot budget
/// and handed the sidecar a Chrome that was not up yet (ledger F-2).
///
/// Both pipes are `null`, not piped: nothing here reads the child's output, and a null
/// stdio cannot fill and wedge the way the piped helper in [`helper_path`] can.
fn exit_code_within(mut cmd: Command, timeout: Duration) -> Option<i32> {
    let mut child = cmd
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return status.code(),
            Err(_) => return None,
            Ok(None) => {}
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return None;
        }
        std::thread::sleep(Duration::from_millis(HELPER_POLL_MS));
    }
}

/// Locate an interpreter that can run the Playwright probe, mirroring warm_chrome.sh's
/// `../.venv/bin/python`. `AIZU_VENV_PYTHON` wins; otherwise we try the dev-tree venv
/// relative to the current directory. A packaged install usually has NEITHER — the frozen
/// sidecar carries Playwright inside its own binary — which is exactly why the caller
/// treats "no interpreter" as [`CdpProbe::Unknown`] rather than a failure.
fn venv_python() -> Option<PathBuf> {
    if let Ok(explicit) = std::env::var("AIZU_VENV_PYTHON") {
        let p = PathBuf::from(explicit);
        if p.exists() {
            return Some(p);
        }
    }
    for candidate in [
        "engine/.venv/bin/python",
        "../engine/.venv/bin/python",
        "../../engine/.venv/bin/python",
        r"engine\.venv\Scripts\python.exe",
        r"..\engine\.venv\Scripts\python.exe",
    ] {
        let p = PathBuf::from(candidate);
        if p.exists() {
            return Some(p);
        }
    }
    None
}

/// The dev-tree fallback probe: ask Playwright directly through the venv interpreter.
const CFT_VENV_SNIPPET: &str = "from playwright.sync_api import sync_playwright; \
p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()";

/// Run a helper that prints ONE filesystem path on stdout, under a HARD deadline, and
/// return that path only if it is usable.
///
/// Three properties are deliberate, and each one is a bug that has already happened:
///
///   - **stdout ONLY; stderr is noise by design.** Both helpers start Playwright, and
///     Playwright reliably dumps an unrelated `TargetClosedError` traceback (plus
///     `Task was destroyed but it is pending!`) to stderr at interpreter shutdown — with a
///     rc of 0 and a perfectly clean stdout. Treating stderr content, or stderr *volume*,
///     as failure would reject the answer on every single call.
///   - **bounded.** `Command::output()` — what this used to be — waits for EOF with no
///     timeout whatsoever. This runs on the boot path, inside the lock app-exit blocks on,
///     under a 30s ceiling; a wedged Node driver must cost us `timeout`, not the app.
///   - **the pipes are drained by threads that are never joined.** The child spawns
///     Playwright's Node driver as a GRANDCHILD which inherits the pipe write ends, so
///     after we kill the child the pipe need not reach EOF — a `join()` (or any read-to-EOF
///     after exit) could park forever. Lines are therefore streamed over a channel and the
///     threads are left to die on their own when the pipe finally closes.
///   - **the answer is read as it ARRIVES, not after the exit.** These helpers print the
///     path and then tear Playwright down, and that teardown is the one that reliably emits
///     `Task was destroyed but it is pending!`. Waiting for the exit before looking at
///     stdout meant a correct path already sitting in the pipe was thrown away whenever the
///     teardown outlived the deadline — a resolvable box falling through to system Chrome
///     because of a shutdown race that has nothing to do with the answer.
fn helper_path(mut cmd: Command, timeout: Duration) -> Option<PathBuf> {
    let mut child = cmd
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .ok()?;

    let (out_tx, out_rx) = mpsc::channel::<String>();
    if let Some(stdout) = child.stdout.take() {
        let tx = out_tx.clone();
        std::thread::spawn(move || drain_first_line(stdout, tx));
    }
    drop(out_tx); // so a helper that prints nothing disconnects instead of burning the grace

    // stderr is drained purely so a chatty helper cannot fill its 64KB pipe and wedge —
    // and its FIRST line is kept so a failure can quote the helper's own remedy back.
    let (err_tx, err_rx) = mpsc::channel::<String>();
    if let Some(stderr) = child.stderr.take() {
        std::thread::spawn(move || drain_first_line(stderr, err_tx));
    }

    let deadline = Instant::now() + timeout;
    let mut answer: Option<String> = None;
    let exit_ok = loop {
        if answer.is_none() {
            answer = out_rx.try_recv().ok();
        }
        match child.try_wait() {
            Ok(Some(status)) => break status.success(),
            Err(_) => break false,
            Ok(None) => {}
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            // One last look: the line may have landed between the poll above and now.
            if answer.is_none() {
                answer = out_rx.try_recv().ok();
            }
            // A helper that already printed its answer and then hung is the KNOWN
            // Playwright teardown, not a failed resolution — keep the answer. It still has
            // to survive `accept_helper_output`'s existence stat, so this cannot promote a
            // path that is not on disk. `true` because the child never got to set an exit
            // code at all: the line it printed is the only fact we have, and it is the one
            // the contract is about.
            if let Some(line) = answer {
                if let Some(path) = accept_helper_output(true, &line) {
                    eprintln!(
                        "[chrome] the Chrome-for-Testing helper answered and then hung in \
                         teardown ({}s) — killed it and kept the answer",
                        timeout.as_secs()
                    );
                    return Some(path);
                }
            }
            eprintln!(
                "[chrome] the Chrome-for-Testing helper did not answer within {}s — killed it",
                timeout.as_secs()
            );
            return None;
        }
        std::thread::sleep(Duration::from_millis(HELPER_POLL_MS));
    };

    // Exited without us having seen the line yet: it is normally already in the pipe, so
    // this grace only covers reader-thread wakeup.
    let first_line = match answer {
        Some(line) => line,
        None => out_rx
            .recv_timeout(Duration::from_millis(HELPER_STDOUT_GRACE_MS))
            .unwrap_or_default(),
    };
    let accepted = accept_helper_output(exit_ok, &first_line);
    if accepted.is_none() {
        // The FIRST line, not the last: the helper's contract is one actionable remedy line
        // written before it returns, and Playwright then buries it under a `Task was
        // destroyed` / `TargetClosedError` dump at interpreter shutdown. Logging the last
        // line would faithfully report the noise and throw away the answer.
        if let Some(first) = err_rx.try_iter().next() {
            eprintln!("[chrome] Chrome-for-Testing helper: {first}");
        }
    }
    accepted
}

/// Read a child pipe to EOF, forwarding only its FIRST line and discarding the rest.
///
/// Both halves matter. Forwarding one line keeps memory O(1) against a helper that streams
/// megabytes; reading to EOF anyway is what stops that helper from filling the ~64KB pipe
/// buffer and blocking on a write while we wait for an exit that can then never come.
fn drain_first_line<R: std::io::Read>(pipe: R, tx: mpsc::Sender<String>) {
    let mut tx = Some(tx);
    for line in BufReader::new(pipe).lines().map_while(Result::ok) {
        if let Some(sender) = tx.take() {
            if sender.send(line).is_err() {
                return; // receiver gone: nobody is left to care what this prints
            }
        }
    }
}

/// The acceptance rule, isolated from the process plumbing so it is testable on every
/// platform without spawning anything: exit code 0, a non-empty trimmed first stdout line,
/// and a path that EXISTS.
///
/// The existence stat lives here and ONLY here. It is not defensive tidiness: Playwright
/// computes `chromium.executable_path` from its registry without ever stat-ing it, so a box
/// that never ran `playwright install chromium` gets a well-formed path to a browser that
/// is not on disk. Launching that yields a Chrome that never starts; skipping the stat and
/// falling through yields the CDP-broken system Chrome. Both look like "the wizard is
/// stuck".
fn accept_helper_output(exit_ok: bool, stdout_first_line: &str) -> Option<PathBuf> {
    if !exit_ok {
        return None;
    }
    let trimmed = stdout_first_line.trim();
    if trimmed.is_empty() {
        return None;
    }
    let path = PathBuf::from(trimmed);
    if path.exists() {
        Some(path)
    } else {
        None
    }
}

/// Read a child pipe to EOF, forwarding EVERY line.
///
/// The opposite trade to [`drain_first_line`], and only safe where the receiver is drained
/// continuously — which the installer's loop does, line by line, as it emits them to the
/// UI. Using this for [`helper_path`]'s stderr would buffer a Playwright traceback storm in
/// a channel nobody reads until the end.
fn stream_lines<R: std::io::Read>(pipe: R, tx: mpsc::Sender<String>) {
    for line in BufReader::new(pipe).lines().map_while(Result::ok) {
        if tx.send(line).is_err() {
            return; // receiver gone: nobody is left to care what this prints
        }
    }
}

/// Download Playwright's Chrome-for-Testing through the FROZEN SIDECAR, streaming progress
/// to the wizard, and return the installed browser's path.
///
/// This is the only remedy that works on the box that needs it. The freeze ships
/// Playwright's Node driver but no browsers, so a packaged install resolves nothing and
/// falls through to system Chrome (see [`system_chrome_degradation`]) — and every classic
/// fix for that (`playwright install chromium`, a pip install, a venv) assumes an
/// interpreter and a CLI a worker PC does not have. The sidecar's `--install` drives the
/// BUNDLED driver, which needs no Python on the box at all.
///
/// Contract with `aizu.worker.chrome_path --install`: progress on **stderr**, the resolved
/// browser path on **stdout**, rc 0 — identical to plain resolution, so the acceptance rule
/// ([`accept_helper_output`], existence stat included) is the same one.
///
/// # It must not be on the boot path and must not hold the Chrome lock
/// A 356 MB download is MINUTES. It takes `&AppHandle` + the configured path rather than
/// `&ChromeManager` precisely so that no caller can hold the `Arc<Mutex<ChromeManager>>`
/// — which app-exit and every wizard Chrome action block on — across it.
pub fn install_chrome_for_testing(
    app: &AppHandle,
    configured_sidecar: &Path,
) -> Result<PathBuf, DesktopError> {
    let binary = crate::sidecar_supervisor::resolve_sidecar_binary(app, configured_sidecar)?;
    let mut cmd = Command::new(&binary);
    cmd.arg("-m").arg(CHROME_PATH_MODULE).arg(CHROME_INSTALL_FLAG);
    // Same reason as the resolution call (ledger A2): the dev tier can hand back the pip
    // console script, which has NO argv dispatch and would boot a second, unsupervised
    // sidecar — a duplicate registration. An empty dispatch URL makes that binary raise in
    // `WorkerConfig.from_env()` before it opens a DB or registers anything. Inert in the
    // freeze, which dispatches this argv long before it reads any config.
    cmd.env("AIZU_DISPATCH_URL", "");
    // Put the installer in its OWN process group so the timeout path can take down the
    // whole tree. Playwright's downloader is a GRANDCHILD (python shim -> bundled node
    // driver), and killing only the direct child orphans that driver: it keeps pulling
    // ~356 MB over the operator's link and keeps its `__dirlock` in the browsers cache, so
    // the retry we invite them to make blocks silently on the lock of a download we told
    // them we had stopped.
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    let mut child = cmd
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| {
            DesktopError::ChromeAttachFailed(format!(
                "could not start the browser installer ({}): {e}",
                binary.display()
            ))
        })?;

    // Same never-joined-threads discipline as `helper_path`: the driver runs as a
    // GRANDCHILD holding the pipe write ends, so nothing here may read to EOF or join.
    let (out_tx, out_rx) = mpsc::channel::<String>();
    if let Some(stdout) = child.stdout.take() {
        let tx = out_tx.clone();
        std::thread::spawn(move || drain_first_line(stdout, tx));
    }
    drop(out_tx);
    let (err_tx, err_rx) = mpsc::channel::<String>();
    if let Some(stderr) = child.stderr.take() {
        std::thread::spawn(move || stream_lines(stderr, err_tx));
    }

    let deadline = Instant::now() + Duration::from_secs(CHROME_INSTALL_TIMEOUT_SEC);
    // The LAST non-blank stderr line, not the first — the exact opposite of `helper_path`.
    // There, stderr is Playwright's shutdown noise burying a remedy written first; here
    // stderr IS the progress stream, so anything actionable is the last thing said.
    let mut last_stderr = String::new();
    let pump = |rx: &mpsc::Receiver<String>, last: &mut String| {
        for line in rx.try_iter() {
            if !line.trim().is_empty() {
                *last = line.clone();
            }
            let _ = app.emit(CHROME_INSTALL_PROGRESS_EVENT, ChromeInstallProgress { line });
        }
    };

    let exit_ok = loop {
        pump(&err_rx, &mut last_stderr);
        match child.try_wait() {
            Ok(Some(status)) => break status.success(),
            Err(e) => {
                return Err(DesktopError::ChromeAttachFailed(format!(
                    "lost track of the browser installer: {e}"
                )))
            }
            Ok(None) => {}
        }
        if Instant::now() >= deadline {
            kill_process_group(&mut child);
            let _ = child.wait();
            return Err(DesktopError::ChromeAttachFailed(format!(
                "The browser download did not finish within {} minutes and was stopped. \
                 Check this box's network (and any proxy), then press “Download browser” \
                 again — it restarts the download from the beginning.",
                CHROME_INSTALL_TIMEOUT_SEC / 60
            )));
        }
        std::thread::sleep(Duration::from_millis(HELPER_POLL_MS));
    };
    pump(&err_rx, &mut last_stderr); // whatever it said on its way out

    let first_line = out_rx
        .recv_timeout(Duration::from_millis(HELPER_STDOUT_GRACE_MS))
        .unwrap_or_default();
    match accept_helper_output(exit_ok, &first_line) {
        Some(path) => {
            eprintln!("[chrome] Chrome-for-Testing installed at {}", path.display());
            Ok(path)
        }
        None => Err(DesktopError::ChromeAttachFailed(install_failure_copy(&last_stderr))),
    }
}

/// Turn the installer's own last word into operator copy. Isolated from the plumbing so it
/// is testable, same as [`accept_helper_output`].
fn install_failure_copy(last_stderr: &str) -> String {
    let tail = last_stderr.trim();
    if tail.is_empty() {
        "The browser download failed and the installer said nothing. Check this box's \
         network (and any proxy), then try again."
            .to_string()
    } else {
        format!("The browser download failed: {tail}")
    }
}

/// Per-OS system-Chrome fallbacks (mirrors chrome_manager.py `_SYSTEM_CHROME_PATHS`).
fn system_chrome_candidates() -> Vec<PathBuf> {
    #[cfg(target_os = "macos")]
    {
        vec![PathBuf::from(
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )]
    }
    #[cfg(target_os = "windows")]
    {
        vec![
            PathBuf::from(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            PathBuf::from(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        ]
    }
    #[cfg(target_os = "linux")]
    {
        vec![
            PathBuf::from("/usr/bin/google-chrome"),
            PathBuf::from("/usr/bin/google-chrome-stable"),
            PathBuf::from("/usr/bin/chromium"),
            PathBuf::from("/usr/bin/chromium-browser"),
        ]
    }
    #[cfg(not(any(target_os = "macos", target_os = "windows", target_os = "linux")))]
    {
        vec![]
    }
}

/// The degraded-Chrome note an ATTACH-branch return should carry.
///
/// Split out of `ensure_running` because that method needs an `AppHandle` and cannot be
/// unit-tested (see the note at the top of `mod tests`) — and this one-line rule is exactly
/// what regressed once already, so it belongs somewhere a test can pin it.
fn attach_branch_degradation(probe: CdpProbe, sticky: &Option<String>) -> Option<String> {
    match probe {
        // A real `connect_over_cdp` succeeded. Whatever we believed when we started this
        // browser, the engine can attach to it — that retires the note honestly.
        CdpProbe::Attached => None,
        // "Could not verify" is NOT "fine". On a packaged box there is no interpreter to
        // probe with, so this is the ONLY branch reachable, and returning None here is what
        // erased the operator's one explanation on their second click.
        CdpProbe::Unknown => sticky.clone(),
        // Not reached: `Rejected` returns an Err before any note is consulted. Kept total
        // so that adding a variant is a compile error rather than a silent None.
        CdpProbe::Rejected => sticky.clone(),
    }
}

/// Kill an installer and everything it spawned.
///
/// The child is its own process-group leader (see the `process_group(0)` at spawn), so a
/// signal to `-pid` reaches the bundled node driver too. Falls back to killing just the
/// child if the group signal fails, and on Windows — where std has no process-group
/// equivalent — that fallback is the whole implementation, so an orphaned driver stays
/// possible there until a job-object stop lands.
fn kill_process_group(child: &mut Child) {
    #[cfg(unix)]
    {
        let pid = child.id() as i32;
        // SIGKILL, not SIGTERM: this path exists because the download is already wedged.
        const SIGKILL: i32 = 9;
        // Safety: a plain libc call with a pid we own; the worst case is ESRCH.
        let sent = unsafe { libc_kill(-pid, SIGKILL) };
        if sent == 0 {
            return;
        }
    }
    let _ = child.kill();
}

#[cfg(unix)]
const SIGTERM: i32 = 15;

#[cfg(unix)]
extern "C" {
    #[link_name = "kill"]
    fn libc_kill(pid: i32, sig: i32) -> i32;
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Everything below this line tests the two halves the packaged-box fix actually turns
    /// on — the acceptance RULE and the bounded subprocess PLUMBING — without a Tauri
    /// runtime, a display, a Chrome, or a built sidecar. The `AppHandle` hop
    /// (`resolve_chrome_for_testing`) is deliberately left untested, exactly as
    /// `config::load` is: there is no way to construct an `AppHandle<Wry>` in a unit test,
    /// so the code is arranged to keep that hop trivial and put the logic here.

    // --- the acceptance rule (portable) --------------------------------------------

    #[test]
    fn a_non_zero_exit_is_rejected_even_when_stdout_names_a_real_path() {
        let real = std::env::temp_dir();
        let real = real.to_string_lossy().to_string();
        assert!(std::path::Path::new(&real).exists(), "fixture must be a real path");
        assert_eq!(accept_helper_output(false, &real), None);
    }

    #[test]
    fn empty_stdout_is_rejected() {
        assert_eq!(accept_helper_output(true, ""), None);
        // A helper that prints only a newline (or whitespace) has printed no path either.
        assert_eq!(accept_helper_output(true, "   "), None);
    }

    #[test]
    fn a_path_that_does_not_exist_is_rejected() {
        // Playwright answers with exactly this shape on a box that never downloaded the
        // browser: rc 0, clean stdout, well-formed path, nothing on disk.
        assert_eq!(
            accept_helper_output(
                true,
                "/no/such/dir/ms-playwright/chromium-1234/Google Chrome for Testing"
            ),
            None
        );
    }

    #[test]
    fn a_clean_exit_naming_an_existing_path_is_accepted() {
        let real = std::env::temp_dir();
        let line = format!("  {}\n", real.to_string_lossy());
        assert_eq!(accept_helper_output(true, &line), Some(real));
    }

    // --- the subprocess plumbing ----------------------------------------------------
    // POSIX only: these need a throwaway executable, and the crate has no dev-dependencies
    // (no tempfile), so they hand-roll one in the temp dir and remove it on drop.

    #[cfg(unix)]
    struct TempScript {
        path: PathBuf,
    }

    #[cfg(unix)]
    impl TempScript {
        fn new(name: &str, body: &str) -> Self {
            use std::os::unix::fs::PermissionsExt;
            let path = std::env::temp_dir().join(format!(
                "aizu-chrome-path-{}-{}-{}",
                std::process::id(),
                name,
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_nanos())
                    .unwrap_or(0)
            ));
            std::fs::write(&path, format!("#!/bin/sh\n{body}\n")).expect("write script");
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755))
                .expect("chmod script");
            Self { path }
        }
    }

    #[cfg(unix)]
    impl Drop for TempScript {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.path);
        }
    }

    #[cfg(unix)]
    #[test]
    fn a_helper_that_exits_non_zero_resolves_to_nothing() {
        // It even prints a real, existing path first — the exit code still governs.
        let script = TempScript::new("rc", "echo \"$0\"; exit 3");
        let got = helper_path(Command::new(&script.path), Duration::from_secs(10));
        assert_eq!(got, None);
    }

    #[cfg(unix)]
    #[test]
    fn a_helper_that_prints_nothing_resolves_to_nothing() {
        let script = TempScript::new("silent", "exit 0");
        let got = helper_path(Command::new(&script.path), Duration::from_secs(10));
        assert_eq!(got, None);
    }

    #[cfg(unix)]
    #[test]
    fn a_helper_that_prints_a_missing_path_resolves_to_nothing() {
        let script = TempScript::new("ghost", "echo /no/such/chrome-for-testing; exit 0");
        let got = helper_path(Command::new(&script.path), Duration::from_secs(10));
        assert_eq!(got, None);
    }

    #[cfg(unix)]
    #[test]
    fn stderr_noise_never_fails_a_good_answer() {
        // This is the VERIFIED real behaviour, not a hypothetical: the Playwright-backed
        // helper exits 0 with a clean stdout and dumps a TargetClosedError traceback to
        // stderr at interpreter shutdown. The volume here (well past a 64KB pipe buffer)
        // also proves the stderr drain keeps a chatty helper from wedging on a full pipe.
        let script = TempScript::new(
            "noisy",
            "i=0\n\
             while [ $i -lt 2000 ]; do\n\
             echo 'Traceback (most recent call last): playwright._impl._errors.TargetClosedError: Target page, context or browser has been closed' >&2\n\
             i=$((i+1))\n\
             done\n\
             echo \"$0\"\n\
             echo 'Task was destroyed but it is pending!' >&2\n\
             exit 0",
        );
        let got = helper_path(Command::new(&script.path), Duration::from_secs(20));
        assert_eq!(got, Some(script.path.clone()));
    }

    // --- the CDP probe script: it has to be Python a real interpreter accepts ---------
    //
    // The probe was built by a `format!` whose lines ended in `\n\`, which in Rust strips the
    // newline AND the continuation line's leading whitespace — so the emitted Python had no
    // indentation at all and died on `IndentationError: expected an indented block after
    // 'try' statement`. Non-zero exit, in every build, on every box, which `cdp_probe` reads
    // as "Chrome refused the attach": the scary quit-that-Chrome-COMPLETELY error about a
    // perfectly healthy browser. Nothing asserted on the string, so nothing caught it.

    /// The exact defect, in the cheapest possible form: after a line that opens a block,
    /// the next non-blank line must be indented. This fails on the flattened script.
    #[test]
    fn every_block_body_in_the_probe_script_is_indented() {
        let lines: Vec<&str> = CDP_PROBE_SCRIPT.lines().collect();
        assert!(lines.len() > 4, "the probe script is suspiciously short: {CDP_PROBE_SCRIPT:?}");
        for (i, line) in lines.iter().enumerate() {
            if !line.trim_end().ends_with(':') {
                continue;
            }
            let body = lines[i + 1..]
                .iter()
                .find(|l| !l.trim().is_empty())
                .unwrap_or_else(|| panic!("`{line}` opens a block with nothing in it"));
            assert!(
                body.starts_with(' ') || body.starts_with('\t'),
                "`{line}` is followed by an UNINDENTED `{body}` — the Rust line-continuation \
                 bug is back and every CDP probe now answers Rejected"
            );
        }
    }

    /// The url is passed as ARGV, never spliced into the source — so a cdp_url can never
    /// become Python code, and the script stays the fixed literal the tests here compile.
    #[test]
    fn the_probe_script_takes_its_url_from_argv() {
        assert!(CDP_PROBE_SCRIPT.contains("sys.argv[1]"), "{CDP_PROBE_SCRIPT}");
        assert!(!CDP_PROBE_SCRIPT.contains("http"), "a url was baked in: {CDP_PROBE_SCRIPT}");
    }

    /// The one number the script and the Rust side must agree on: an interpreter with no
    /// Playwright exits with this, and `cdp_probe` maps it to `Unknown` rather than to a
    /// fabricated "your Chrome refused the attach".
    #[test]
    fn the_probe_reports_a_missing_playwright_with_its_own_exit_code() {
        assert!(
            CDP_PROBE_SCRIPT.contains(&format!("SystemExit({PROBE_RC_NO_PLAYWRIGHT})")),
            "the script and PROBE_RC_NO_PLAYWRIGHT have drifted: {CDP_PROBE_SCRIPT}"
        );
    }

    /// An interpreter to syntax-check with: the dev-tree venv if this checkout has one,
    /// else a bare `python3`/`python` on PATH. `None` on a box with neither — which is a
    /// fact about the box, so the test skips rather than failing.
    fn any_python() -> Option<PathBuf> {
        if let Some(py) = venv_python() {
            return Some(py);
        }
        ["python3", "python"].into_iter().find_map(|name| {
            let ran = Command::new(name)
                .arg("-c")
                .arg("pass")
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .map(|s| s.success())
                .unwrap_or(false);
            ran.then(|| PathBuf::from(name))
        })
    }

    /// The test that would actually have caught this: hand the emitted script to a REAL
    /// interpreter and ask it to compile. `compile()`, not a run — the probe imports
    /// Playwright and dials a browser, and neither belongs in a unit test.
    #[test]
    fn the_probe_script_is_valid_python() {
        let Some(py) = any_python() else {
            eprintln!("skipping: no Python on this box to syntax-check the probe with");
            return;
        };
        let out = Command::new(&py)
            .arg("-c")
            .arg("import sys; compile(sys.argv[1], '<cdp-probe>', 'exec')")
            .arg(CDP_PROBE_SCRIPT)
            .stdin(Stdio::null())
            .output()
            .expect("run the interpreter");
        assert!(
            out.status.success(),
            "the emitted probe is not valid Python — every cdp_probe would answer Rejected:\n{}",
            String::from_utf8_lossy(&out.stderr)
        );
    }

    // --- the profile directory is DERIVED from the brand ------------------------------
    //
    // These replace the guard's decision table, its marker, its refusal copy and the
    // operator declaration that tried to rescue it. Nothing is policed any more: getting
    // `brand_of` wrong now picks a different DIRECTORY, which is a fresh profile and a
    // re-login, not eighteen deleted cookies.

    #[test]
    fn the_brand_of_a_binary_is_read_off_its_path() {
        assert_eq!(
            brand_of(Path::new(
                "/Users/x/Library/Caches/ms-playwright/chromium-1234/chrome-mac-arm64/Google \
                 Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
            )),
            BRAND_CHROME_FOR_TESTING
        );
        // Case-insensitive: the same build ships as `chrome-for-testing` on Linux paths.
        assert_eq!(
            brand_of(Path::new("/opt/chrome for testing/chrome")),
            BRAND_CHROME_FOR_TESTING
        );
        assert_eq!(
            brand_of(Path::new("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")),
            BRAND_CHROME
        );
        // An override to something unrecognised is a regular Chrome as far as the Keychain
        // is concerned — the safe assumption, since it shares the system key.
        assert_eq!(brand_of(Path::new("/opt/vendor/chrome")), BRAND_CHROME);
        // …but distro Chromium is NOT that: it seals cookies under its own keyring entry,
        // so filing it as `chrome` would hand it and /usr/bin/google-chrome one directory
        // and wipe whichever warmed it first. Both sit on the Linux fallback list together.
        assert_eq!(brand_of(Path::new("/usr/bin/chromium")), BRAND_CHROMIUM);
        assert_eq!(brand_of(Path::new("/usr/bin/chromium-browser")), BRAND_CHROMIUM);
        assert_eq!(brand_of(Path::new(r"C:\tools\chromium.exe")), BRAND_CHROMIUM);
        assert_eq!(brand_of(Path::new("/usr/bin/google-chrome")), BRAND_CHROME);
        // Rule 2 still runs first: a CfT build whose leaf is `chrome` stays CfT.
        assert_eq!(
            brand_of(Path::new("/x/ms-playwright/chromium-1234/chrome-linux64/chrome")),
            BRAND_CHROME_FOR_TESTING
        );
    }

    /// Rule 2 of the contract, on the platforms rule 1 cannot see.
    ///
    /// The string `chrome for testing` appears ONLY in Playwright's macOS bundle. Its own
    /// `EXECUTABLE_PATHS` table resolves chromium to `chrome-linux64/chrome` (linux-x64),
    /// `chrome-linux/chrome` (linux-arm64) and `chrome-win64/chrome.exe` (win-x64) — read
    /// out of the installed driver's `coreBundle.js`, not guessed. A substring-only
    /// `brand_of` labels every one of those `chrome`, which would put a Playwright browser
    /// and the system browser in ONE directory on every non-mac box — the collision the
    /// whole derivation exists to make impossible.
    #[test]
    fn playwrights_non_mac_builds_are_chrome_for_testing_too() {
        assert_eq!(
            brand_of(Path::new(
                "/home/op/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome"
            )),
            BRAND_CHROME_FOR_TESTING,
            "linux-x64"
        );
        assert_eq!(
            brand_of(Path::new("/home/op/.cache/ms-playwright/chromium-1234/chrome-linux/chrome")),
            BRAND_CHROME_FOR_TESTING,
            "linux-arm64"
        );
        assert_eq!(
            brand_of(Path::new(
                r"C:\Users\op\AppData\Local\ms-playwright\chromium-1234\chrome-win64\chrome.exe"
            )),
            BRAND_CHROME_FOR_TESTING,
            "win-x64 — and the backslashes must segment like a path, not read as one blob"
        );
        // The headless-shell build shares the cache layout: Playwright names every browser
        // directory `<name with - as _>-<revision>` (`readDescriptors`).
        assert_eq!(
            brand_of(Path::new(
                "/home/op/.cache/ms-playwright/chromium_headless_shell-1234/chrome-linux64/\
                 headless_shell"
            )),
            BRAND_CHROME_FOR_TESTING
        );
    }

    /// …and rule 2 must not swallow the system browser. It is a SEGMENT match on a fixed
    /// shape, so anything that merely mentions chromium stays `chrome` — the safe label,
    /// because an unrecognised build shares the system Keychain key.
    #[test]
    fn the_browsers_cache_rule_is_a_whole_segment_and_a_revision() {
        for path in [
            "/opt/chromium-dev/chrome",           // no revision
            "/opt/chromium-12ab/chrome",          // not digits
            "/opt/my-chromium-1234x/chrome",      // segment does not START with chromium
            "/opt/chromium-/chrome",              // empty revision
            "/opt/build-chromium-1234/chrome",    // the revision shape as a SUBSTRING only
            "/srv/chromium-1234-backup/chrome",   // …and as a prefix of a longer segment
        ] {
            assert_eq!(brand_of(Path::new(path)), BRAND_CHROME, "{path}");
        }
        assert!(is_playwright_browser_dir("chromium-1"));
        assert!(is_playwright_browser_dir("chromium_headless_shell-1234"));
        assert!(!is_playwright_browser_dir("chromium"));
        assert!(!is_playwright_browser_dir("chromium_headless_shell"));
    }

    /// The shapes a real argv arrives in. Trailing slashes and `.` segments come from an
    /// operator's `CHROME_BIN` export; a RELATIVE path comes from one typed in a terminal
    /// sitting in the browsers cache. Each of them decides a directory now, so each of them
    /// has to land on the same one the absolute form does.
    #[test]
    fn the_brand_survives_the_shapes_an_operator_actually_types() {
        assert_eq!(
            brand_of(Path::new("chromium-1234/chrome-linux64/chrome")),
            BRAND_CHROME_FOR_TESTING,
            "relative — a path typed from inside the browsers cache"
        );
        assert_eq!(
            brand_of(Path::new("./chromium-1234/chrome-linux64/chrome")),
            BRAND_CHROME_FOR_TESTING
        );
        assert_eq!(
            brand_of(Path::new("/home/op/.cache/ms-playwright/chromium-1234/chrome-linux64/")),
            BRAND_CHROME_FOR_TESTING,
            "a trailing slash must not hide the segment before it"
        );
        assert_eq!(
            brand_of(Path::new("/OPT/Chromium-1234/Chrome-Linux64/CHROME")),
            BRAND_CHROME_FOR_TESTING,
            "mixed case — the cache is lower-case but a case-insensitive filesystem is not"
        );
        assert_eq!(
            brand_of(Path::new("/opt/CHROME FOR TESTING/chrome")),
            BRAND_CHROME_FOR_TESTING,
            "rule 1 is case-insensitive too"
        );
    }

    /// A SYMLINK is the one input that can lie, and it is the common one: `~/bin/chrome`
    /// pointing into the browsers cache, or a wrapper an operator dropped on PATH. Judged
    /// as written it reads `chrome`, so a Playwright build would be launched against the
    /// system browser's directory — a profile the other brand warmed, and every cookie in
    /// it deleted. Resolving first is what makes the link's name irrelevant.
    #[cfg(unix)]
    #[test]
    fn a_symlink_is_resolved_before_the_brand_is_read() {
        let dir = TempDir::new("symlink");
        let cache = dir.path.join("chromium-1234").join("chrome-linux64");
        std::fs::create_dir_all(&cache).expect("create the cache layout");
        let real = cache.join("chrome");
        std::fs::write(&real, "#!/bin/sh\n").expect("write the browser");
        let link = dir.path.join("chrome"); // a name that says nothing
        std::os::unix::fs::symlink(&real, &link).expect("symlink");

        assert_eq!(brand_of(&real), BRAND_CHROME_FOR_TESTING);
        assert_eq!(
            brand_of(&link),
            BRAND_CHROME_FOR_TESTING,
            "the link's own name must not decide the profile directory"
        );
    }

    /// A path that does not exist cannot be canonicalised, and MUST still be judged rather
    /// than defaulted: a `CHROME_BIN` typo, or a binary on a volume that is not mounted
    /// yet, would otherwise silently become `chrome` and pick the other brand's directory.
    #[test]
    fn a_path_that_does_not_exist_is_judged_as_written() {
        assert_eq!(
            brand_of(Path::new("/no/such/chromium-1234/chrome-linux64/chrome")),
            BRAND_CHROME_FOR_TESTING
        );
        assert_eq!(brand_of(Path::new("/no/such/chrome")), BRAND_CHROME);
    }

    /// The derivation itself: one subdirectory per brand, and the two can never be the same
    /// directory. That single property is what retired the marker, the decision table, the
    /// refusal and the declaration.
    #[test]
    fn each_brand_gets_its_own_subdirectory_of_the_base() {
        let base = Path::new("/Users/op/.aizu-cft-profile");
        assert_eq!(
            profile_dir_for(base, BRAND_CHROME_FOR_TESTING),
            PathBuf::from("/Users/op/.aizu-cft-profile/chrome-for-testing")
        );
        assert_eq!(
            profile_dir_for(base, BRAND_CHROME),
            PathBuf::from("/Users/op/.aizu-cft-profile/chrome")
        );
        assert_ne!(
            profile_dir_for(base, BRAND_CHROME_FOR_TESTING),
            profile_dir_for(base, BRAND_CHROME),
            "if these can ever be equal the cookie loss is back"
        );
        // …and the base itself is never a profile. Launching against it is the act the old
        // guard existed to refuse; here no derivation can produce it.
        assert_ne!(profile_dir_for(base, BRAND_CHROME), base.to_path_buf());
        assert_ne!(profile_dir_for(base, BRAND_CHROME_FOR_TESTING), base.to_path_buf());
    }

    /// End to end, from binary to `--user-data-dir`: the two browsers on a real box land in
    /// two directories, computed by the same code the launch uses.
    #[test]
    fn two_browsers_on_one_box_land_in_two_directories() {
        let base = Path::new("/Users/op/.aizu-cft-profile");
        let cft = Path::new(
            "/Users/op/Library/Caches/ms-playwright/chromium-1234/chrome-mac-arm64/Google \
             Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        );
        let system = Path::new("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome");
        let cft_dir = profile_dir_for(base, brand_of(cft));
        let system_dir = profile_dir_for(base, brand_of(system));
        assert_ne!(cft_dir, system_dir);
        assert!(cft_dir.ends_with("chrome-for-testing"), "{}", cft_dir.display());
        assert!(system_dir.ends_with("chrome"), "{}", system_dir.display());
    }

    /// …and the argv `launch` really passes — the same function, not a re-implementation.
    ///
    /// This is the assertion the last round did not have. Its helper was correct and its one
    /// call site never consulted it, so the operator's declaration button was a dead end and
    /// the launch it was supposed to unblock was refused exactly as before. A derivation
    /// that the launch path does not go through is not a fix.
    #[test]
    fn the_launch_argv_points_at_the_derived_directory_not_the_base() {
        let dir = TempDir::new("argv");
        let cft = Path::new(
            "/x/chromium-1234/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/\
             Google Chrome for Testing",
        );
        let system = Path::new("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome");

        let argv = launch_argv(&dir.path, cft, 9333);
        let expected = profile_dir_for(&dir.path, BRAND_CHROME_FOR_TESTING);
        assert!(argv.contains(&format!("--user-data-dir={}", expected.display())), "{argv:?}");
        assert!(
            !argv.contains(&format!("--user-data-dir={}", dir.path.display())),
            "the BASE is not a profile — launching against it is the cookie loss: {argv:?}"
        );
        assert!(argv.contains(&"--remote-debugging-port=9333".to_string()), "{argv:?}");
        // It creates the directory it names, so an unwritable location fails at LAUNCH
        // instead of as a Chrome that starts and never listens.
        assert!(expected.is_dir());

        // The other brand, same base, same call: a different directory, every time.
        let other = launch_argv(&dir.path, system, 9333);
        assert!(
            other.contains(&format!(
                "--user-data-dir={}",
                profile_dir_for(&dir.path, BRAND_CHROME).display()
            )),
            "{other:?}"
        );
        assert_ne!(argv, other);
    }

    /// The same rule against the browsers actually installed on THIS box, so the contract is
    /// pinned to real paths (symlinks, `/private` prefixes, a `.app` bundle) and not only to
    /// strings a test author typed. Skips where a browser is absent — that is a fact about
    /// the box, the same idiom as `any_python` above.
    #[test]
    fn the_real_browsers_on_this_box_land_in_two_directories() {
        let base = Path::new("/tmp/aizu-base-that-is-never-created");
        let mut seen: Vec<(&str, PathBuf)> = Vec::new();
        for candidate in system_chrome_candidates() {
            if candidate.exists() {
                seen.push(("system", profile_dir_for(base, brand_of(&candidate))));
                break;
            }
        }
        if let Some(cft) = playwright_cache_chrome() {
            seen.push(("chrome-for-testing", profile_dir_for(base, brand_of(&cft))));
        }
        if seen.len() < 2 {
            eprintln!("skipping: this box has fewer than two browsers to tell apart");
            return;
        }
        assert_ne!(seen[0].1, seen[1].1, "{seen:?}");
        assert!(seen.iter().any(|(_, d)| d.ends_with(BRAND_CHROME)), "{seen:?}");
        assert!(
            seen.iter().any(|(_, d)| d.ends_with(BRAND_CHROME_FOR_TESTING)),
            "{seen:?}"
        );
    }

    /// A Chrome-for-Testing already in the local Playwright cache, if this box has one. A
    /// plain directory walk, not the sidecar helper: this test is about path SHAPES, and
    /// shelling out to Playwright to get one would make it a subprocess test.
    fn playwright_cache_chrome() -> Option<PathBuf> {
        let home = std::env::var_os("HOME").map(PathBuf::from)?;
        let cache = home.join("Library/Caches/ms-playwright");
        let revision = std::fs::read_dir(&cache)
            .ok()?
            .flatten()
            .map(|e| e.path())
            .find(|p| {
                p.file_name()
                    .map(|n| is_playwright_browser_dir(&n.to_string_lossy()))
                    .unwrap_or(false)
            })?;
        let leaf = revision
            .join("chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS")
            .join("Google Chrome for Testing");
        leaf.exists().then_some(leaf)
    }

    /// The legacy profile: seen, named, and NEVER touched.
    ///
    /// A base warmed before this change has its `Default/` sitting directly in it, left by
    /// a browser whose brand nothing on disk records. Three rounds tried to work that out;
    /// this one says so and stops. The notice has to name both destinations, or "move it
    /// yourself" is a puzzle rather than a copy-paste.
    #[test]
    fn a_legacy_profile_is_reported_and_left_exactly_as_it_is() {
        let dir = TempDir::new("legacy").used();
        std::fs::write(dir.path.join(PROFILE_USED_DIR).join("Cookies"), b"not really")
            .expect("a cookie jar to not touch");

        let notice = legacy_profile_notice(&dir.path).expect("a used base must be reported");
        assert!(notice.contains(&dir.path.display().to_string()), "which directory? {notice}");
        assert!(
            notice.contains(&profile_dir_for(&dir.path, BRAND_CHROME_FOR_TESTING).display().to_string()),
            "destination 1 missing: {notice}"
        );
        assert!(
            notice.contains(&profile_dir_for(&dir.path, BRAND_CHROME).display().to_string()),
            "destination 2 missing: {notice}"
        );
        assert!(notice.contains("Nothing has been done to it"), "{notice}");
        assert!(notice.contains("will not guess"), "the whole lesson of three rounds: {notice}");
        assert!(notice.contains("DELETE"), "the consequence is invisible unless named: {notice}");

        // Reading it changed nothing: the profile, its cookie jar and both would-be
        // destinations are exactly as they were. This app does not move, rename, copy, back
        // up or delete an operator's profile — not even to be helpful.
        assert!(dir.path.join(PROFILE_USED_DIR).join("Cookies").is_file());
        assert!(!profile_dir_for(&dir.path, BRAND_CHROME_FOR_TESTING).exists());
        assert!(!profile_dir_for(&dir.path, BRAND_CHROME).exists());
    }

    /// …and a base that only holds brand subdirectories — every box set up after this
    /// change — says nothing at all. A notice on a healthy box is the clutter that trains
    /// operators to click past the one that matters.
    #[test]
    fn a_base_with_only_brand_subdirectories_is_silent() {
        let dir = TempDir::new("modern");
        assert_eq!(legacy_profile_notice(&dir.path), None, "an empty base");
        let profile = profile_dir_for(&dir.path, BRAND_CHROME_FOR_TESTING);
        std::fs::create_dir_all(profile.join(PROFILE_USED_DIR)).expect("a warmed brand profile");
        assert_eq!(
            legacy_profile_notice(&dir.path),
            None,
            "a Default/ INSIDE a brand directory is a normal profile, not a legacy one"
        );
        // The base of a box that never existed says nothing either.
        assert_eq!(legacy_profile_notice(Path::new("/no/such/base")), None);
    }

    /// The moved default. Unifying the base is what makes the shell, its sidecar and
    /// warm_chrome.sh open ONE directory (the shell exports the base to the child), but a
    /// box that took the shell's old default has real warmed logins at a path nothing looks
    /// at any more. Saying nothing is what makes that box look merely signed-out.
    #[test]
    fn a_profile_at_the_former_default_location_is_reported_not_moved() {
        let former = TempDir::new("former-default");
        let current = TempDir::new("current-base");
        std::fs::create_dir_all(former.path.join(PROFILE_USED_DIR)).unwrap();

        let notice = former_default_profile_notice(&former.path, &current.path)
            .expect("a used former default must be reported");
        assert!(notice.contains(&former.path.display().to_string()), "names where it is");
        assert!(notice.contains(&current.path.display().to_string()), "names where we look now");
        // Both destinations, never one: the brand is as unknowable here as anywhere else, and
        // pre-picking it is the mistake that sank three earlier rounds.
        assert!(notice.contains(
            &profile_dir_for(&current.path, BRAND_CHROME_FOR_TESTING).display().to_string()));
        assert!(notice.contains(
            &profile_dir_for(&current.path, BRAND_CHROME).display().to_string()));
        assert!(notice.contains("DELETE"), "states the cost of guessing wrong");

        // …and it is a REPORT: the directory is exactly as it was.
        assert!(former.path.join(PROFILE_USED_DIR).is_dir(), "never moved, never deleted");
    }

    #[test]
    fn nothing_is_said_about_a_former_default_that_is_empty_absent_or_still_in_use() {
        let former = TempDir::new("former-empty");
        let current = TempDir::new("current-2");
        // Never used by a browser.
        assert_eq!(former_default_profile_notice(&former.path, &current.path), None);
        // Does not exist at all.
        assert_eq!(
            former_default_profile_notice(Path::new("/no/such/former"), &current.path),
            None
        );
        // Still the configured base — telling someone their profile moved when it did not is
        // its own false alarm.
        std::fs::create_dir_all(former.path.join(PROFILE_USED_DIR)).unwrap();
        assert_eq!(former_default_profile_notice(&former.path, &former.path), None);
    }
    /// One knob, two spellings — because the two halves of the same app read different
    /// ones. `CHROME_BIN` is what the shipped `warm_chrome.sh` reads and wins;
    /// `AIZU_CHROME_BINARY` is what `aizu.worker.chrome_manager` reads, and an operator who
    /// followed the worker's own remedy text used to be obeyed there and silently ignored
    /// here — the two processes starting different browsers on one box.
    ///
    /// It mutates process env, so it is ONE test rather than four: `cargo test` runs threads
    /// in parallel and a second env-touching test would flake against this one.
    #[test]
    fn either_spelling_of_the_binary_override_is_honoured_and_named() {
        for var in ["AIZU_CHROME_BINARY", "CHROME_BIN"] {
            std::env::remove_var(var);
        }
        assert_eq!(chrome_binary_override(), None);

        std::env::set_var("AIZU_CHROME_BINARY", "/opt/worker-chrome");
        assert_eq!(
            chrome_binary_override(),
            Some(("AIZU_CHROME_BINARY", "/opt/worker-chrome".to_string())),
            "the worker's spelling must not be ignored here"
        );

        // …and AIZU_CHROME_BINARY wins when both are set, matching warm_chrome.sh and
        // chrome_manager.py. The ORDER is the contract, not just the pair: bash read the
        // namespaced name first while this and the Python read CHROME_BIN first, so a box
        // with both set warmed <base>/chrome-for-testing and harvested <base>/chrome.
        std::env::set_var("CHROME_BIN", " /opt/shell-chrome ");
        assert_eq!(
            chrome_binary_override(),
            Some(("AIZU_CHROME_BINARY", "/opt/worker-chrome".to_string())),
            "the namespaced spelling outranks the generic one"
        );

        // The loser is still honoured on its own, and still trimmed — a trailing space in
        // an export is not part of a filename.
        std::env::remove_var("AIZU_CHROME_BINARY");
        assert_eq!(
            chrome_binary_override(),
            Some(("CHROME_BIN", "/opt/shell-chrome".to_string())),
        );

        // A blank export is not a setting. Treated as one it fails the launch with an empty
        // path instead of falling through to Playwright's browser, which is a box with a
        // perfectly good Chrome reporting that it has none.
        std::env::set_var("CHROME_BIN", "");
        std::env::set_var("AIZU_CHROME_BINARY", "  ");
        assert_eq!(chrome_binary_override(), None);

        for var in ["AIZU_CHROME_BINARY", "CHROME_BIN"] {
            std::env::remove_var(var);
        }
    }

    // --- the boot budget (defect 2) -------------------------------------------------

    /// The arithmetic in [`CFT_SIDECAR_TIMEOUT_SEC`]'s table, asserted rather than claimed.
    /// The doc comment used to assert a total the code could not honour; a comment cannot
    /// fail a build, so this does. Blowing `main::CHROME_BOOT_GRACE_SEC` re-creates F-2 —
    /// the sidecar preflight beating Chrome up, `capabilities: []`, a false fatal, a 30s
    /// park and two `worker_token_hash` rotations on a perfectly healthy box.
    #[test]
    fn the_worst_case_launch_budget_fits_under_the_boot_grace() {
        let worst_ms = 800                              // TCP pre-check on a filtered port
            + CFT_SIDECAR_TIMEOUT_SEC * 1_000           // sidecar helper
            + HELPER_STDOUT_GRACE_MS                    // …its stdout grace
            + CFT_VENV_TIMEOUT_SEC * 1_000              // venv helper
            + HELPER_STDOUT_GRACE_MS                    // …its stdout grace
            + LAUNCH_TIMEOUT_SEC * 1_000    // launch-attach loop (probe-clamped, see below)
            + 800; // …and the pre-check the loop's own `Unknown` exit runs before returning
        assert_eq!(worst_ms, 29_600, "the table in CFT_SIDECAR_TIMEOUT_SEC is out of date");

        // Not merely "under": a ceiling a slow box can trip by a second is not a ceiling.
        const MARGIN_MS: u64 = 3_000;
        let grace_ms = crate::CHROME_BOOT_GRACE_SEC * 1_000;
        assert!(
            worst_ms + MARGIN_MS <= grace_ms,
            "ensure_running can spend {worst_ms}ms but boot only waits {grace_ms}ms — raise \
             CHROME_BOOT_GRACE_SEC or lower a timeout, or boot walks back into ledger F-2"
        );
    }

    /// …and the launch loop's own bound, which is what makes the `LAUNCH_TIMEOUT_SEC` row
    /// above true. Every probe must fit INSIDE the time the loop has left, process overhead
    /// included; the loop used to test its deadline at entry only and then start a fresh
    /// unbounded probe.
    #[test]
    fn a_probe_is_never_started_without_room_to_finish_it() {
        // Plenty of time: the full probe timeout, never more.
        assert_eq!(
            probe_budget_within(Duration::from_secs(60)),
            Some(CDP_PROBE_TIMEOUT_SEC)
        );
        // Squeezed: the attach timeout shrinks by the process-overhead margin, so the
        // subprocess deadline (attach + margin) still lands inside what is left.
        let squeezed = probe_budget_within(Duration::from_secs(6)).expect("6s is enough");
        assert!(
            squeezed + CDP_PROBE_KILL_MARGIN_SEC <= 6,
            "a probe budgeted {squeezed}s + {CDP_PROBE_KILL_MARGIN_SEC}s overhead overruns \
             the 6s it was given"
        );
        // Not enough left to learn anything: do not start one at all.
        assert_eq!(probe_budget_within(Duration::from_secs(1)), None);
        assert_eq!(probe_budget_within(Duration::ZERO), None);
    }

    #[cfg(unix)]
    #[test]
    fn a_status_only_child_is_bounded_too() {
        // The shape `cdp_probe` needs: exit code in, verdict out.
        let ok = TempScript::new("status-ok", "exit 0");
        assert_eq!(exit_code_within(Command::new(&ok.path), Duration::from_secs(10)), Some(0));
        // A distinct non-zero code, not just "failed": `cdp_probe` reads
        // `PROBE_RC_NO_PLAYWRIGHT` off this and must get the number back intact.
        let bad = TempScript::new("status-bad", &format!("exit {PROBE_RC_NO_PLAYWRIGHT}"));
        assert_eq!(
            exit_code_within(Command::new(&bad.path), Duration::from_secs(10)),
            Some(PROBE_RC_NO_PLAYWRIGHT)
        );
    }

    #[cfg(unix)]
    #[test]
    fn a_wedged_status_probe_is_killed_at_the_deadline() {
        // This is defect 2's core: `Command::status()` waits forever, and cdp_probe runs it
        // in a loop on the boot path. `None` (not `Some(false)`) is also the point — a
        // probe we had to kill says nothing about Chrome, so the caller reads it as
        // `Unknown`, never as a rejected attach.
        let script = TempScript::new("status-wedged", "exec sleep 30");
        let started = Instant::now();
        let got = exit_code_within(Command::new(&script.path), Duration::from_secs(1));
        let elapsed = started.elapsed();
        assert_eq!(got, None);
        assert!(
            elapsed < Duration::from_secs(5),
            "the CDP probe is unbounded again: {elapsed:?} — one wedged interpreter eats the \
             whole of main::CHROME_BOOT_GRACE_SEC"
        );
    }

    // --- the operator copy (defect 1) -----------------------------------------------

    /// The fall-through to system Chrome has to reach a HUMAN, and the only remedy that
    /// works on a packaged box is the in-app download — no interpreter, no CLI, and
    /// "relaunch Chrome" produces the identical system Chrome.
    // --- the sticky degraded-Chrome note (the defect that came back) ---------------
    //
    // On a PACKAGED box `cdp_probe` can only ever return `Unknown`: `venv_python()` has no
    // `AIZU_VENV_PYTHON` and its relative `engine/.venv/...` candidates cannot exist under a
    // Finder-launched .app whose cwd is `/`. So the `Unknown` arm is the only one a second
    // "Launch warmed Chrome" click — or the next app start — can reach, and what it returns
    // IS the operator's whole explanation.

    #[test]
    fn an_unverifiable_attach_keeps_the_degraded_note_alive() {
        let sticky = Some("No Chrome-for-Testing on this box — use Download browser".to_string());
        // The regression in one line: this used to be None, which erased the footer note,
        // painted the Chrome step green, and left `cdp_attachable` red under a remedy that
        // reproduces the identical browser forever.
        assert_eq!(
            attach_branch_degradation(CdpProbe::Unknown, &sticky),
            sticky,
            "an unverifiable probe must not be read as good news"
        );
    }

    #[test]
    fn a_real_attach_is_the_one_thing_that_retires_the_note() {
        let sticky = Some("No Chrome-for-Testing on this box".to_string());
        assert_eq!(attach_branch_degradation(CdpProbe::Attached, &sticky), None);
    }

    #[test]
    fn a_healthy_box_is_never_given_a_note_it_did_not_earn() {
        // The other half: no sticky note means no note, on every branch. A false red on a
        // box whose Chrome is fine is the worst outcome this feature has.
        for probe in [CdpProbe::Attached, CdpProbe::Unknown, CdpProbe::Rejected] {
            assert_eq!(attach_branch_degradation(probe, &None), None);
        }
    }

    #[test]
    fn the_system_chrome_note_names_the_download_action_and_its_size() {
        let note = system_chrome_degradation(Path::new("/Applications/Google Chrome.app"));
        assert!(note.contains("Download browser"), "no way out named: {note}");
        assert!(note.contains(CHROME_DOWNLOAD_SIZE), "the operator is not told the cost: {note}");
        assert!(
            !note.contains("playwright install"),
            "names a remedy a packaged box cannot run: {note}"
        );
    }

    /// The installer's failure copy quotes the installer's LAST word (progress floods
    /// stderr, so anything actionable is said at the end) and still says something useful
    /// when it said nothing at all.
    #[test]
    fn a_failed_download_quotes_the_installer_or_explains_the_silence() {
        assert!(install_failure_copy("  no space left on device \n").contains("no space left"));
        let silent = install_failure_copy("   ");
        assert!(silent.contains("network"), "an empty failure must still be actionable: {silent}");
    }

    #[cfg(unix)]
    #[test]
    fn a_wedged_helper_is_killed_at_the_deadline() {
        // `exec` so the script process IS the sleep: nothing survives the kill, and the
        // 30s here is far longer than any plausible slow-but-working answer.
        let script = TempScript::new("wedged", "exec sleep 30");
        let started = Instant::now();
        let got = helper_path(Command::new(&script.path), Duration::from_secs(1));
        let elapsed = started.elapsed();
        assert_eq!(got, None);
        assert!(
            elapsed < Duration::from_secs(5),
            "the deadline did not bound the wait: {elapsed:?} — a wedged helper on the boot \
             path eats main::CHROME_BOOT_GRACE_SEC"
        );
    }

    #[cfg(unix)]
    #[test]
    fn an_answer_already_in_the_pipe_survives_a_hung_teardown() {
        // Defect 3, and the real Playwright shape: the helper prints its path, then hangs
        // shutting the driver down (the hang that emits `Task was destroyed but it is
        // pending!`). Waiting for the EXIT before reading stdout threw that answer away and
        // dropped the box onto system Chrome for a shutdown race that has nothing to do
        // with the answer. The path still has to exist on disk — the deadline is not a way
        // around `accept_helper_output`.
        // `/bin/echo`, not the builtin: a shell builtin's stdout is block-buffered on a
        // pipe and `exec` need not flush it, which would test the plumbing rather than the
        // rule. An external command exits and flushes, so the line really is in the pipe
        // while the "helper" is still hanging.
        let script = TempScript::new("slow-teardown", "/bin/echo \"$0\"\nexec sleep 30");
        let started = Instant::now();
        // A roomy deadline on purpose: the child has to actually get scheduled and write
        // before it hangs, and the whole suite is spawning subprocesses in parallel. The
        // deadline is what this test wedges against, not what it measures.
        let got = helper_path(Command::new(&script.path), Duration::from_secs(4));
        let elapsed = started.elapsed();
        assert_eq!(got, Some(script.path.clone()));
        assert!(
            elapsed < Duration::from_secs(12),
            "kept the answer but not the deadline: {elapsed:?}"
        );
    }

    /// A throwaway directory, same hand-rolled shape as [`TempScript`] (the crate has no
    /// dev-dependencies, so no `tempfile`).
    struct TempDir {
        path: PathBuf,
    }

    impl TempDir {
        fn new(name: &str) -> Self {
            let path = std::env::temp_dir().join(format!(
                "aizu-brand-{}-{}-{}",
                std::process::id(),
                name,
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_nanos())
                    .unwrap_or(0)
            ));
            std::fs::create_dir_all(&path).expect("create profile dir");
            Self { path }
        }
        /// Make the directory look like one a browser has actually run against.
        fn used(self) -> Self {
            std::fs::create_dir_all(self.path.join(PROFILE_USED_DIR)).expect("create Default");
            self
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.path);
        }
    }

    #[cfg(unix)]
    #[test]
    fn a_hung_helper_that_printed_a_bad_path_still_resolves_to_nothing() {
        // The other half of the same rule: keeping an early answer must not smuggle past
        // the existence stat. Playwright prints a well-formed path for a browser that was
        // never downloaded, which is exactly the box that needs the download.
        let script =
            TempScript::new("slow-ghost", "/bin/echo /no/such/chrome-for-testing\nexec sleep 30");
        let got = helper_path(Command::new(&script.path), Duration::from_secs(1));
        assert_eq!(got, None);
    }
}
