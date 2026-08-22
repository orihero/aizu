//! Loopback control-surface client (BUILD-PLAN Phase 6).
//!
//! A small `reqwest` client for the sidecar's loopback-only control surface
//! (`aizu.worker.control_surface`, a stdlib HTTP server bound to 127.0.0.1). This is
//! the **single source of truth** for worker/job/Chrome state in the UI:
//!
//!   - `GET  /status`  → the full `StatusDto` (worker id, per-account health, current job
//!                        incl. its `logFilePath`, control flags, Chrome status, and the
//!                        launch **preflight** report).
//!   - `POST /command` → `{"action": "pause"|"resume"|"stopCurrentJob"|"focusWarmedChrome"
//!                                  |"runPreflight"|"openLoginTab"}` (+ `platform` for the
//!                        last one).
//!
//! # `runPreflight` / `openLoginTab` are ACCEPTED, not COMPLETED
//! Both answer `200 {"accepted": true}` immediately and run **detached** on the sidecar —
//! each takes seconds (a CDP probe, a Playwright attach), far past this client's 3s
//! timeout. So `send_command` returning `true` means "the sidecar took the intent", NOT
//! "it finished". The operator's actual feedback is the NEXT `/status` poll, 1.5s later.
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
    /// The sidecar's launch preflight report (`preflight.PreflightReport.to_wire()`),
    /// or `null` while the first pass is still running.
    ///
    /// Deliberately an OPAQUE `serde_json::Value` forwarded verbatim to the UI: the Rust
    /// never needs to know a single check id, so a change to the Python check list costs
    /// nothing here, and a wrong guess about the shape cannot blank the whole status
    /// parse. `#[serde(default)]` for the same reason `reenrolment_required` has it — an
    /// older packaged sidecar omits the key entirely, and a missing report must degrade
    /// to "checking…", never fail the parse.
    ///
    /// **`None` is NOT healthy.** The UI renders it as "checking…"; a box whose preflight
    /// has not finished has been cleared of nothing.
    #[serde(default)]
    pub preflight: Option<serde_json::Value>,
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
    /// Ledger B10: dispatch answered 401, the box's token was cleared and the pull loop
    /// stopped — it does NOTHING until an operator re-enrols it. `serde(default)` because
    /// an older sidecar binary (packaged before this field existed) omits it entirely,
    /// and a missing flag must degrade to "not revoked", never fail the whole status
    /// parse and blank the UI.
    #[serde(default)]
    pub reenrolment_required: bool,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ChromeDto {
    pub connected: bool,
    pub cdp_url: String,
    pub browser_version: Option<String>,
}

/// The operator intents (mirror `control_state.VALID_COMMANDS`).
///
/// NOT `Copy` any more: `OpenLoginTab` carries the platform name. Kept as an enum rather
/// than a free-form string so a typo is a compile error, never a `400 unknown action` the
/// operator sees as "nothing happened".
#[derive(Debug, Clone)]
pub enum Command {
    Pause,
    Resume,
    StopCurrentJob,
    FocusWarmedChrome,
    /// Re-run the launch preflight now (the wizard's and the dashboard's "Re-check").
    RunPreflight,
    /// Open/focus a login tab for `platform` in the warmed Chrome so the operator can sign
    /// in **in the real browser**. This is the local half of the handoff
    /// `server._handle_agent_launch_login` has always promised in distributed mode.
    /// `platform` is whitelisted server-side against `CDP_PLATFORMS`.
    OpenLoginTab { platform: String },
}

impl Command {
    fn action(&self) -> &'static str {
        match self {
            Command::Pause => "pause",
            Command::Resume => "resume",
            Command::StopCurrentJob => "stopCurrentJob",
            Command::FocusWarmedChrome => "focusWarmedChrome",
            Command::RunPreflight => "runPreflight",
            Command::OpenLoginTab { .. } => "openLoginTab",
        }
    }

    /// The only per-action argument the control surface accepts today.
    fn platform(&self) -> Option<&str> {
        match self {
            Command::OpenLoginTab { platform } => Some(platform.as_str()),
            _ => None,
        }
    }
}

/// `skip_serializing_if` keeps the zero-argument commands byte-identical to what they
/// have always sent (`{"action":"pause"}`). `validate_command` does tolerate a null
/// `platform` on those actions today, but sending a field an action does not take is how
/// a future tightening of that validator turns Pause into a silent 400.
#[derive(Debug, Serialize)]
struct CommandBody<'a> {
    action: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    platform: Option<&'a str>,
}

#[derive(Debug, Default, Deserialize)]
struct AcceptedDto {
    #[serde(default)]
    accepted: bool,
}

/// Thin HTTP client over the loopback control surface.
///
/// The port is an atomic, not a baked-in base URL: the advanced menu can change
/// `control_port` at runtime and the sidecar is now restarted in place rather than by
/// relaunching the whole app, so a frozen base would leave this client politely polling a
/// port nothing listens on — a UI that shows "connecting…" forever with a perfectly
/// healthy worker behind it.
pub struct ControlClient {
    port: std::sync::atomic::AtomicU16,
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
            port: std::sync::atomic::AtomicU16::new(control_port),
            token,
            http,
        }
    }

    /// Point at a new control-surface port (after a config write). Loopback only, always.
    pub fn set_port(&self, port: u16) {
        if port != 0 {
            self.port.store(port, std::sync::atomic::Ordering::SeqCst);
        }
    }

    fn url(&self, path: &str) -> String {
        let port = self.port.load(std::sync::atomic::Ordering::SeqCst);
        format!("http://127.0.0.1:{port}{path}")
    }

    /// `GET /status`. On any transport/parse failure returns `ControlSurfaceUnreachable`
    /// (the sidecar is starting, crashed, or the surface is disabled) — never panics.
    pub async fn get_status(&self) -> Result<StatusDto, DesktopError> {
        let resp = self
            .http
            .get(self.url("/status"))
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
        let body = CommandBody { action: cmd.action(), platform: cmd.platform() };
        let resp = self
            .http
            .post(self.url("/command"))
            .bearer_auth(self.token.as_str())
            .json(&body)
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

/// How long the wizard's one-shot "can this box reach your cloud?" probe waits.
const DISPATCH_PROBE_TIMEOUT_MS: u64 = 4_000;

/// One-shot reachability probe of the DISPATCH url for the wizard's Connect step.
///
/// **Deliberately not a preflight check, and deliberately not fatal to anything.** A
/// preflight that failed on a flaky network would park working boxes every time a VPN
/// blinked (spec §6, step 1), so reachability is a one-shot piece of operator feedback
/// here and nothing else. Any HTTP answer at all — including 401/404 — proves the box can
/// reach that host, which is the only question being asked; only a transport error is a
/// "no". Returns `Ok(status_code)` or `Err(short reason)`.
///
/// This is the ONE place this client talks to something that is not the loopback control
/// surface, so it builds its own short-lived client rather than reusing `ControlClient`.
pub async fn probe_dispatch(base_url: &str) -> Result<u16, String> {
    let url = base_url.trim().trim_end_matches('/').to_string();
    if !(url.starts_with("http://") || url.starts_with("https://")) {
        return Err("not an http(s) URL".into());
    }
    let http = reqwest::Client::builder()
        .timeout(Duration::from_millis(DISPATCH_PROBE_TIMEOUT_MS))
        .build()
        .map_err(|e| format!("client build: {e}"))?;
    match http.get(&url).send().await {
        Ok(resp) => Ok(resp.status().as_u16()),
        // The transport error string can name the host but never a credential — nothing
        // authenticating is sent on this request at all.
        Err(e) => Err(short_transport_error(&e)),
    }
}

/// Condense a reqwest error into one operator-readable clause. `reqwest::Error`'s Display
/// chains causes into a paragraph; the wizard has one line.
fn short_transport_error(e: &reqwest::Error) -> String {
    if e.is_timeout() {
        "timed out".into()
    } else if e.is_connect() {
        "could not connect".into()
    } else {
        "request failed".into()
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
