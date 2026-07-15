//! Managed Chrome lifecycle for the desktop shell (Phase 6 SCAFFOLD, UNCOMPILED).
//!
//! Rust mirror of `reelradar.worker.chrome_manager.ChromeManager` and
//! `engine/scripts/warm_chrome.sh`. Owns exactly ONE warmed, logged-in Chrome per box:
//!
//!   - **`ensure_running`**: reuse an existing attachable Chrome if one is up, else launch
//!     ONE. **Never a second Chrome.**
//!   - **`kill_on_app_exit`**: terminate a Chrome ONLY if WE launched it (`owned == false`
//!     → no-op). A Chrome we merely attached to must outlive us so the warmed login
//!     survives a sidecar restart.
//!
//! # Detection is a REAL connect_over_cdp probe, not a bare HTTP 200
//! `warm_chrome.sh` proved that a stale/degraded Chrome (or system Chrome 149+) answers
//! `/json/version` with HTTP 200 while REJECTING `connect_over_cdp` ("Browser context
//! management is not supported"). So an HTTP 200 is only a cheap pre-check; the source of
//! truth is a real Playwright `connect_over_cdp(no_defaults=True)` attach. We run that
//! probe by shelling to the venv's Python (the SAME resolution `warm_chrome.sh` uses),
//! not by hitting HTTP from Rust.
//!
//! # Port
//! The CDP port comes from `DesktopConfig` (default **9333** — the live-proven port, NOT
//! 9222). There is no hardcoded port here.
//!
//! # Binary resolution (ported from warm_chrome.sh precedence)
//! explicit `CHROME_BIN` override → Playwright's bundled Chrome-for-Testing
//! (protocol-matched, the safe default) → system Chrome (last resort; known-broken on 149+).
//! Launch flags mirror the shell: `--remote-debugging-port`, a DEDICATED `--user-data-dir`,
//! `--no-first-run`, `--no-default-browser-check`.

use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use crate::config::SharedConfig;
use crate::errors::DesktopError;

/// Wait for a freshly launched Chrome to become CDP-attachable.
const LAUNCH_TIMEOUT_SEC: u64 = 15;
/// Poll interval while waiting for the launch to attach.
const LAUNCH_POLL_INTERVAL_MS: u64 = 500;
/// Timeout for a single CDP-attach probe.
const CDP_PROBE_TIMEOUT_SEC: u64 = 5;
/// Grace between terminate and kill when we stop a Chrome we own.
const STOP_GRACE_SEC: u64 = 5;

/// Fixed launch flags — never magic strings scattered across call sites.
const NO_FIRST_RUN: &str = "--no-first-run";
const NO_DEFAULT_BROWSER_CHECK: &str = "--no-default-browser-check";

pub struct ChromeManager {
    config: SharedConfig,
    /// The handle to a Chrome WE launched, or None if we attached to an existing one.
    handle: Option<Child>,
    /// True only when this manager launched the Chrome (governs `kill_on_app_exit`).
    owned: bool,
}

impl ChromeManager {
    pub fn new(config: SharedConfig) -> Self {
        Self {
            config,
            handle: None,
            owned: false,
        }
    }

    /// Reconnect to an attachable Chrome if present, else launch ONE. Idempotent — safe to
    /// call on every app start. Errors:
    ///   - `ChromeAttachFailed` if a Chrome is listening but rejects CDP (we will NOT kill
    ///     a process we did not launch — the operator must quit the stale Chrome), OR if a
    ///     launch never becomes attachable in time.
    pub fn ensure_running(&mut self) -> Result<(), DesktopError> {
        let cdp_url = self.config.cdp_url();

        // Cheap pre-check: is anything listening at all? If yes, gate on the REAL attach.
        if http_precheck(&cdp_url) {
            if cdp_attaches(&cdp_url, CDP_PROBE_TIMEOUT_SEC) {
                self.owned = false;
                self.handle = None;
                eprintln!("[chrome] attached to existing Chrome at {cdp_url}");
                return Ok(());
            }
            return Err(DesktopError::ChromeAttachFailed(format!(
                "Chrome at {cdp_url} answers HTTP but rejects connect_over_cdp \
                 (stale/degraded Chrome or system Chrome 149+). Quit that Chrome and \
                 relaunch — this manager will not kill a browser it did not launch."
            )));
        }

        self.launch(&cdp_url)
    }

    fn launch(&mut self, cdp_url: &str) -> Result<(), DesktopError> {
        let binary = self.resolve_chrome_binary()?;
        let argv = self.build_launch_args();
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
        let deadline = Instant::now() + Duration::from_secs(LAUNCH_TIMEOUT_SEC);
        while Instant::now() < deadline {
            if cdp_attaches(cdp_url, CDP_PROBE_TIMEOUT_SEC) {
                eprintln!("[chrome] CDP up and attachable at {cdp_url}");
                return Ok(());
            }
            std::thread::sleep(Duration::from_millis(LAUNCH_POLL_INTERVAL_MS));
        }
        Err(DesktopError::ChromeAttachFailed(format!(
            "Chrome launched but did not become CDP-attachable within {LAUNCH_TIMEOUT_SEC}s at {cdp_url}"
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

    /// argv mirroring warm_chrome.sh: dedicated profile + the two anti-nag flags.
    fn build_launch_args(&self) -> Vec<String> {
        vec![
            format!("--remote-debugging-port={}", self.config.cdp_port),
            format!("--user-data-dir={}", self.config.chrome_profile_dir.display()),
            NO_FIRST_RUN.to_string(),
            NO_DEFAULT_BROWSER_CHECK.to_string(),
        ]
    }

    /// Resolve the Chrome binary (precedence ported from warm_chrome.sh / chrome_manager.py):
    /// `CHROME_BIN` override → Playwright Chrome-for-Testing (protocol-matched) → system
    /// Chrome. The CfT path is resolved by shelling to the venv Python, exactly as the
    /// shell script does.
    fn resolve_chrome_binary(&self) -> Result<PathBuf, DesktopError> {
        if let Ok(explicit) = std::env::var("CHROME_BIN") {
            let p = PathBuf::from(&explicit);
            if p.exists() {
                return Ok(p);
            }
            return Err(DesktopError::ChromeAttachFailed(format!(
                "CHROME_BIN does not exist: {explicit}"
            )));
        }
        if let Some(cft) = resolve_chrome_for_testing() {
            if cft.exists() {
                return Ok(cft);
            }
        }
        for candidate in system_chrome_candidates() {
            if candidate.exists() {
                return Ok(candidate);
            }
        }
        Err(DesktopError::ChromeAttachFailed(
            "no Chrome binary found — install Playwright's Chrome-for-Testing or set CHROME_BIN".into(),
        ))
    }
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
/// NOT sufficient (a degraded Chrome answers HTTP but rejects CDP). Returns false on any
/// failure (missing venv, missing Playwright, rejected attach).
fn cdp_attaches(cdp_url: &str, timeout_sec: u64) -> bool {
    let Some(py) = venv_python() else {
        eprintln!("[chrome] no venv python found — cannot CDP-probe");
        return false;
    };
    // Mirrors warm_chrome.sh's inline probe: connect_over_cdp(no_defaults=True).
    let script = format!(
        "import sys\n\
         from playwright.sync_api import sync_playwright\n\
         pw = sync_playwright().start()\n\
         try:\n\
             b = pw.chromium.connect_over_cdp('{cdp_url}', no_defaults=True, timeout={ms})\n\
             b.close()\n\
         finally:\n\
             pw.stop()\n",
        cdp_url = cdp_url,
        ms = timeout_sec * 1000
    );
    Command::new(py)
        .arg("-c")
        .arg(script)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

/// Locate the venv interpreter, mirroring warm_chrome.sh's `../.venv/bin/python`. The
/// engineer wires the exact relative location at packaging time (the frozen sidecar ships
/// its own Playwright; for a dev build this points at engine/.venv).
fn venv_python() -> Option<PathBuf> {
    if let Ok(explicit) = std::env::var("REELRADAR_VENV_PYTHON") {
        let p = PathBuf::from(explicit);
        if p.exists() {
            return Some(p);
        }
    }
    None
}

/// Ask Playwright (via the venv Python) for its bundled Chrome-for-Testing path — the
/// protocol-matched binary warm_chrome.sh prefers over system Chrome.
fn resolve_chrome_for_testing() -> Option<PathBuf> {
    let py = venv_python()?;
    let out = Command::new(py)
        .arg("-c")
        .arg("from playwright.sync_api import sync_playwright; p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()")
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let path = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if path.is_empty() {
        None
    } else {
        Some(PathBuf::from(path))
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

#[cfg(unix)]
const SIGTERM: i32 = 15;

#[cfg(unix)]
extern "C" {
    #[link_name = "kill"]
    fn libc_kill(pid: i32, sig: i32) -> i32;
}
