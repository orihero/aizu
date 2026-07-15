//! Tauri commands invoked from the UI (Phase 6 SCAFFOLD, UNCOMPILED).
//!
//! Thin adapters over the control-surface client and the supervisor. Every command:
//!   - returns `Result<T, String>` (the JS side gets a plain string on error),
//!   - NEVER leaks a secret or an absolute path into the returned string (config/log paths
//!     and the control token stay server-side; only sanitized messages cross the boundary).
//!
//! State/commands are the SAME control-surface actions the sidecar understands
//! (`pause`/`resume`/`stopCurrentJob`/`focusWarmedChrome`). `stop_current_job` is the
//! mid-run hard-stop — it does NOT kill the sidecar; the Python layer force-terminates the
//! killable job child. `restart_sidecar` is the only command that touches the process, and
//! only via the supervisor's graceful stop → watchdog relaunch.

use serde::Serialize;
use tauri::{AppHandle, State};

use crate::control_client::{Command, StatusDto};
use crate::AppState;

/// Non-secret config surfaced to the dev menu for prefill. NEVER carries the bootstrap
/// token (write-only from the UI's perspective).
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ConfigDto {
    pub dispatch_base_url: String,
    pub control_port: u16,
    /// Whether a bootstrap token is currently stored (so the UI can show "set" without
    /// ever revealing the value).
    pub has_token: bool,
}

/// Read the current wiring to prefill the dev menu (no secret leaves the backend).
#[tauri::command]
pub async fn get_config(app: AppHandle, state: State<'_, AppState>) -> Result<ConfigDto, String> {
    Ok(ConfigDto {
        dispatch_base_url: state.config.dispatch_base_url.clone(),
        control_port: state.config.control_port,
        has_token: crate::config::read_bootstrap_token(&app).is_some(),
    })
}

/// Dev-menu "Save & Restart": switch the worker's dispatch (local ⇄ cloud) at runtime.
/// Writes config.toml (non-secret) + the 0600 token file (if a token was entered), stops
/// the current sidecar so it can't double-register, then RESTARTS the app so it relaunches
/// cleanly against the new environment. A blank token leaves the stored one untouched only
/// when `null`; an empty string clears it.
#[tauri::command]
pub async fn save_config(
    app: AppHandle,
    state: State<'_, AppState>,
    dispatch_base_url: String,
    control_port: u16,
    bootstrap_token: Option<String>,
) -> Result<(), String> {
    let url = dispatch_base_url.trim().to_string();
    if !url.is_empty() && !url.starts_with("http://") && !url.starts_with("https://") {
        return Err("dispatch URL must start with http:// or https://".into());
    }
    if control_port == 0 {
        return Err("control port must be non-zero".into());
    }
    crate::config::write_config(&app, &url, control_port).map_err(sanitize)?;
    if let Some(token) = bootstrap_token {
        // Some(...) → set/replace (empty string clears); None → leave the existing token.
        crate::config::write_bootstrap_token(&app, &token).map_err(sanitize)?;
    }
    // Stop the current sidecar so it can't keep running against the OLD dispatch after the
    // relaunch (avoids a double-registered box). Then relaunch into the new config.
    state.supervisor.lock().await.stop_gracefully().await;
    app.restart() // returns `!` — relaunches the process into the new config
}

/// Fetch the latest worker/job/Chrome state. This is the UI's truth (no log scraping).
#[tauri::command]
pub async fn get_status(state: State<'_, AppState>) -> Result<StatusDto, String> {
    state.control.get_status().await.map_err(sanitize)
}

/// Operator PAUSE — stop leasing NEW jobs; a running job is untouched. Resumable.
#[tauri::command]
pub async fn pause(state: State<'_, AppState>) -> Result<bool, String> {
    state.control.send_command(Command::Pause).await.map_err(sanitize)
}

/// Clear a pause so the worker leases again.
#[tauri::command]
pub async fn resume(state: State<'_, AppState>) -> Result<bool, String> {
    state.control.send_command(Command::Resume).await.map_err(sanitize)
}

/// Mid-run HARD-STOP of the current job. Sends `stopCurrentJob` to the control surface;
/// the Python layer SIGTERM→SIGKILLs the killable job child and nacks it "operator_stop".
/// Does NOT kill the sidecar process. Returns whether a job was live to stop.
#[tauri::command]
pub async fn stop_current_job(state: State<'_, AppState>) -> Result<bool, String> {
    state
        .control
        .send_command(Command::StopCurrentJob)
        .await
        .map_err(sanitize)
}

/// Bring the warmed Chrome to the foreground for a 2FA/captcha challenge. macOS is real
/// (osascript activate, handled in the sidecar); other OSes are a logged no-op pending
/// real focus strategies. Returns whether the intent was accepted.
#[tauri::command]
pub async fn focus_chrome(state: State<'_, AppState>) -> Result<bool, String> {
    state
        .control
        .send_command(Command::FocusWarmedChrome)
        .await
        .map_err(sanitize)
}

/// Restart the sidecar PROCESS (crash-recovery affordance). Graceful stop → the watchdog
/// relaunches. This is the ONLY UI action that kills the sidecar; it is NOT how you stop a
/// single job (use `stop_current_job`).
#[tauri::command]
pub async fn restart_sidecar(state: State<'_, AppState>) -> Result<(), String> {
    let mut sup = state.supervisor.lock().await;
    sup.stop_gracefully().await;
    // The watchdog loop in `start()` observes `shutting_down` reset on the next `start()`;
    // the scaffold re-enters start on a fresh task. An engineer may prefer an explicit
    // `restart()` method on the supervisor that clears the flag and re-spawns in place.
    Ok(())
}

/// Max worker capacity the operator may set from the UI. The sidecar clamps too, but we
/// clamp here for a clean UI contract. (0 = drain to idle; MAX is a sane per-box ceiling.)
const MAX_CAPACITY: u32 = 8;

/// Set a capacity override (concurrent jobs this box will accept), clamped to `0..=MAX`.
///
/// NOTE: capacity is currently a presentational/soft control — the shipped sidecar enforces
/// one live job per (org, platform, account) via single-flight, and capacity is not yet a
/// control-surface command. This command clamps + validates the input and is wired to send
/// once the sidecar exposes a `setCapacity` action. Documented as a residual gap.
#[tauri::command]
pub async fn set_capacity_override(
    _state: State<'_, AppState>,
    capacity: u32,
) -> Result<u32, String> {
    let clamped = capacity.min(MAX_CAPACITY);
    Ok(clamped)
}

/// Map a `DesktopError` to a UI-safe string. thiserror's Display already avoids secrets by
/// construction (variants carry ports/host context, never tokens); this is the single
/// choke point where an engineer can further redact if a variant ever grows a path.
fn sanitize(e: crate::errors::DesktopError) -> String {
    e.to_string()
}
