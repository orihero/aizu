//! Loopback control-surface client (Phase 6 SCAFFOLD, UNCOMPILED).
//!
//! A small `reqwest` client for the sidecar's loopback-only control surface
//! (`aizu.worker.control_surface`, a stdlib HTTP server bound to 127.0.0.1). This is
//! the **single source of truth** for worker/job/Chrome state in the UI:
//!
//!   - `GET  /status`  → the full `StatusDto` (worker id, per-account health, current job
//!                        incl. its `logFilePath`, control flags, Chrome status).
//!   - `POST /command` → `{"action": "pause"|"resume"|"stopCurrentJob"|"focusWarmedChrome"}`.
//!
//! **This replaces any log-scraping.** The UI does not infer state from logs — it renders
//! `/status`. The per-job log tail (`log_tail.rs`) follows the exact `currentJob.logFilePath`
//! this DTO reports, not a newest-mtime guess.
//!
//! Every request carries `Authorization: Bearer <control_token>` — the same per-spawn token
//! the shell generated and injected into the child's env as `AIZU_CONTROL_TOKEN`. The
//! token lives only in memory here; it is never logged or surfaced to the UI.
//!
//! The DTO field names are camelCase to match `control_state.status_to_wire` verbatim.

use std::sync::Arc;
use std::time::Duration;

use rand::RngCore;
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter};

use crate::errors::DesktopError;

/// Tauri event name carrying each fresh `/status` snapshot to the UI.
pub const STATUS_EVENT: &str = "status-updated";

/// Loopback HTTP is fast; a short timeout keeps a stalled sidecar from wedging the poller.
const REQUEST_TIMEOUT_MS: u64 = 3_000;

/// The `{ok, data, error}` envelope the control surface returns (mirrors server.py idiom).
#[derive(Debug, Deserialize)]
struct Envelope<T> {
    ok: bool,
    #[serde(default)]
    data: Option<T>,
    #[serde(default)]
    error: Option<String>,
}

/// `GET /status` → `data`. Field names/shape mirror `control_state.status_to_wire`.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct StatusDto {
    pub worker_id: Option<String>,
    #[serde(default)]
    pub accounts: Vec<AccountDto>,
    #[serde(default)]
    pub current_job: Option<CurrentJobDto>,
    pub controls: ControlsDto,
    #[serde(default)]
    pub chrome: Option<ChromeDto>,
    #[serde(default)]
    pub generated_at: f64,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AccountDto {
    pub org_id: Option<i64>,
    pub platform: String,
    pub account_handle: Option<String>,
    pub status: String, // "idle" | "busy"
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CurrentJobDto {
    pub job_id: String,
    pub campaign_id: String,
    pub platform: String,
    pub status: String,
    pub run_id: Option<String>,
    /// The per-run log file the killable job child writes. `log_tail.rs` follows THIS path.
    pub log_file_path: Option<String>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ControlsDto {
    pub drain: bool,
    pub halt: bool,
    pub halt_reason: Option<String>,
    pub update_required: bool,
    pub paused: bool,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ChromeDto {
    pub connected: bool,
    pub cdp_url: String,
    pub browser_version: Option<String>,
}

/// The four zero-argument operator intents (mirror `control_state.VALID_COMMANDS`).
#[derive(Debug, Clone, Copy)]
pub enum Command {
    Pause,
    Resume,
    StopCurrentJob,
    FocusWarmedChrome,
}

impl Command {
    fn action(self) -> &'static str {
        match self {
            Command::Pause => "pause",
            Command::Resume => "resume",
            Command::StopCurrentJob => "stopCurrentJob",
            Command::FocusWarmedChrome => "focusWarmedChrome",
        }
    }
}

#[derive(Debug, Serialize)]
struct CommandBody {
    action: &'static str,
}

#[derive(Debug, Default, Deserialize)]
struct AcceptedDto {
    #[serde(default)]
    accepted: bool,
}

/// Thin HTTP client over the loopback control surface. Cheap to clone (`Arc` fields).
pub struct ControlClient {
    base: String,
    token: Arc<String>,
    http: reqwest::Client,
}

impl ControlClient {
    pub fn new(control_port: u16, token: Arc<String>) -> Self {
        let http = reqwest::Client::builder()
            .timeout(Duration::from_millis(REQUEST_TIMEOUT_MS))
            // Never follow redirects on a control surface; loopback only.
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .expect("reqwest client build");
        Self {
            base: format!("http://127.0.0.1:{control_port}"),
            token,
            http,
        }
    }

    /// `GET /status`. On any transport/parse failure returns `ControlSurfaceUnreachable`
    /// (the sidecar is starting, crashed, or the surface is disabled) — never panics.
    pub async fn get_status(&self) -> Result<StatusDto, DesktopError> {
        let resp = self
            .http
            .get(format!("{}/status", self.base))
            .bearer_auth(self.token.as_str())
            .send()
            .await
            .map_err(|e| DesktopError::ControlSurfaceUnreachable(format!("GET /status: {e}")))?;
        let env: Envelope<StatusDto> = resp
            .json()
            .await
            .map_err(|e| DesktopError::ControlSurfaceUnreachable(format!("bad /status JSON: {e}")))?;
        match (env.ok, env.data) {
            (true, Some(data)) => Ok(data),
            _ => Err(DesktopError::ControlSurfaceUnreachable(
                env.error.unwrap_or_else(|| "status not ok".into()),
            )),
        }
    }

    /// `POST /command`. Returns whether the sidecar ACCEPTED the intent (a job existed /
    /// Chrome was reachable) — not whether it has COMPLETED (both are eventual).
    pub async fn send_command(&self, cmd: Command) -> Result<bool, DesktopError> {
        let resp = self
            .http
            .post(format!("{}/command", self.base))
            .bearer_auth(self.token.as_str())
            .json(&CommandBody { action: cmd.action() })
            .send()
            .await
            .map_err(|e| DesktopError::ControlSurfaceUnreachable(format!("POST /command: {e}")))?;
        let env: Envelope<AcceptedDto> = resp
            .json()
            .await
            .map_err(|e| DesktopError::ControlSurfaceUnreachable(format!("bad /command JSON: {e}")))?;
        match (env.ok, env.data) {
            (true, Some(d)) => Ok(d.accepted),
            _ => Err(DesktopError::ControlSurfaceUnreachable(
                env.error.unwrap_or_else(|| "command rejected".into()),
            )),
        }
    }
}

/// Generate a fresh 256-bit hex control-surface token for this app run. Passed to the
/// child as `AIZU_CONTROL_TOKEN` and used as the Bearer for every control call. Held
/// in memory only — never persisted, never logged.
pub fn generate_token() -> String {
    let mut bytes = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut bytes);
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Poll `GET /status` on an interval, emitting a `status-updated` event for the UI each
/// beat. A single failed poll is swallowed (emits nothing) — the sidecar may just be
/// starting; the next beat retries. This loop is the UI's ONLY state source (no logs).
pub async fn run_status_poller(client: Arc<ControlClient>, app: AppHandle, interval_ms: u64) {
    let mut ticker = tokio::time::interval(Duration::from_millis(interval_ms));
    loop {
        ticker.tick().await;
        match client.get_status().await {
            Ok(status) => {
                // Emit failures are non-fatal (window may be closing).
                let _ = app.emit(STATUS_EVENT, &status);
            }
            Err(_) => {
                // Surface "disconnected" without spamming: emit a null once could be added
                // here, but a missed beat simply leaves the last snapshot stale in the UI,
                // which the UI renders as "connecting…" past a staleness threshold.
            }
        }
    }
}
