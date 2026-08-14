//! Tauri commands invoked from the UI (Phase 6 SCAFFOLD, UNCOMPILED).
//!
//! Thin adapters over the control-surface client, the supervisor, the Chrome manager and
//! the on-disk config. Every command:
//!   - returns `Result<T, String>` (the JS side gets a plain string on error),
//!   - NEVER leaks a secret into the returned string (config/log paths and the control
//!     token stay server-side; secret VALUES are write-only — the UI may learn a secret's
//!     NAME and nothing else).
//!
//! # Two families
//! **Steady-state controls** (`pause`/`resume`/`stop_current_job`/`focus_chrome`/
//! `restart_sidecar`/`set_capacity_override`) are the SAME control-surface actions the
//! sidecar understands. `stop_current_job` is the mid-run hard-stop — it does NOT kill the
//! sidecar; the Python layer force-terminates the killable job child.
//!
//! **Setup** (`get_setup_state` … `finish_setup` / `defer_setup`) backs the first-run wizard. The design
//! rule there is that the Rust is DUMB: it writes config/secrets, restarts the child, and
//! forwards the sidecar's own preflight report to the UI verbatim. It never re-implements
//! a check, and it never decides that the box is healthy — [`derive_step`] only reads
//! statuses out of a report the Python produced. A check id the Rust has never heard of
//! costs nothing.

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, State};

use crate::config::ConfigPatch;
use crate::control_client::{Command, StatusDto};
use crate::AppState;

// ---------------------------------------------------------------- platform mirrors
//
// Mirrors `engine/aizu/core/config.py`. These exist ONLY to render checkboxes and to
// decide whether the wizard shows its two Chrome steps; the server re-validates every
// platform name it is given (`config._parse_capabilities_env`, `control_state`
// `validate_command`), so a drift here is a cosmetic bug, never a security one. Keep them
// in this order — it is the order the checkboxes render in.

/// Every platform a worker box can advertise (`SUPPORTED_PLATFORMS`).
const SUPPORTED_PLATFORMS: [&str; 6] =
    ["instagram", "youtube", "telegram", "reddit", "linkedin", "x"];

/// The subset driven through the warmed Chrome (`CDP_PLATFORMS`). Selecting NONE of these
/// makes the wizard skip its Chrome and Sign-in steps entirely — and makes the sidecar's
/// own report mark every Chrome check `skip`, which is what the skip is derived from.
const CDP_PLATFORMS: [&str; 3] = ["instagram", "linkedin", "x"];

/// The literal `"all"` sentinel `AIZU_WORKER_PLATFORMS` understands.
const ALL_PLATFORMS: &str = "all";

/// Non-secret config surfaced to the dev menu for prefill. NEVER carries the bootstrap
/// token (write-only from the UI's perspective).
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ConfigDto {
    pub dispatch_base_url: String,
    pub control_port: u16,
    /// Whether an enrolment token is currently stored (so the UI can show "set" without
    /// ever revealing the value).
    pub has_token: bool,
}

/// Read the current wiring to prefill the dev menu (no secret leaves the backend).
#[tauri::command]
pub async fn get_config(app: AppHandle, state: State<'_, AppState>) -> Result<ConfigDto, String> {
    let cfg = state.config.get();
    Ok(ConfigDto {
        dispatch_base_url: cfg.dispatch_base_url.clone(),
        control_port: cfg.control_port,
        has_token: crate::config::read_bootstrap_token(&app).is_some(),
    })
}

/// Advanced/dev "Save & Restart": switch the worker's dispatch (local ⇄ cloud) at runtime.
///
/// Writes config.toml (non-secret) + the 0600 token file (if a token was entered), then
/// restarts the SIDECAR so it re-reads its env. It no longer restarts the whole APP: the
/// supervisor can now relaunch the child in place, and an app relaunch is a worse
/// experience (window flash, lost log buffer) for the same effect.
#[tauri::command]
pub async fn save_config(
    app: AppHandle,
    state: State<'_, AppState>,
    dispatch_base_url: String,
    control_port: u16,
    bootstrap_token: Option<String>,
) -> Result<(), String> {
    refuse_while_job_running(&state).await?;
    let patch = ConfigPatch {
        dispatch_base_url: Some(dispatch_base_url),
        control_port: Some(control_port),
        ..Default::default()
    };
    let next = crate::config::update_config(&app, patch).map_err(sanitize)?;
    state.config.set(next);
    if let Some(token) = bootstrap_token {
        // Some(...) → set/replace (empty string clears); None → leave the existing token.
        crate::config::write_bootstrap_token(&app, &token).map_err(sanitize)?;
    }
    restart_worker(&state).await;
    Ok(())
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

/// Restart the sidecar PROCESS (crash-recovery affordance, and how every setup write takes
/// effect). This is the ONLY UI action that kills the sidecar; it is NOT how you stop a
/// single job (use `stop_current_job`). Refused while a job is live.
#[tauri::command]
pub async fn restart_sidecar(state: State<'_, AppState>) -> Result<(), String> {
    refuse_while_job_running(&state).await?;
    restart_worker(&state).await;
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

// =====================================================================================
// SETUP WIZARD
// =====================================================================================

/// Everything the wizard renders, recomputed on every call.
///
/// `step` is DERIVED (see [`derive_step`]), never stored. The only persisted wizard state
/// anywhere is `setup_complete` / `setup_deferred` in config.toml — two booleans about the
/// operator's intent, never a step number. That is what makes quitting mid-setup, fixing a
/// box by hand outside the app, and the sidecar restart that a secrets write requires all
/// land the operator in the right place instead of re-asking.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SetupStateDto {
    /// The first step whose gate is not green (1..=7), or 8 when everything passed.
    pub step: u8,
    pub setup_complete: bool,
    /// The operator pressed "Close and fix later". Setup is still NOT complete; this only
    /// stops the wizard opening over the dashboard on every launch.
    pub setup_deferred: bool,
    pub dispatch_base_url: String,
    pub control_port: u16,
    pub cdp_port: u16,
    /// Expanded platform list (the `"all"` sentinel resolved to real names).
    pub worker_platforms: Vec<String>,
    /// The CDP subset of the above — empty means steps 5 and 6 do not apply to this box.
    pub cdp_platforms: Vec<String>,
    pub supported_platforms: Vec<String>,
    pub cdp_platform_names: Vec<String>,
    pub has_dispatch_token: bool,
    /// Browser platforms the operator skipped on the Sign-in step, echoed back so the UI
    /// renders exactly the set the step gate was computed from. In-memory, per-session.
    pub skipped_logins: Vec<String>,
    /// NAMES only. A secret's value never crosses this boundary in either direction after
    /// it is written.
    pub secret_names: Vec<String>,
    /// The control surface answered — i.e. the sidecar process is alive and bound. NOT
    /// "registered": registration can fail all it likes and this stays true, which is the
    /// whole point of binding the control surface before registering.
    pub sidecar_running: bool,
    pub worker_id: Option<String>,
    pub reenrolment_required: bool,
    /// A job is live, so nothing may restart the sidecar right now.
    pub job_active: bool,
    /// The sidecar's preflight report, forwarded verbatim. `null` = still running: the UI
    /// renders "checking…", NEVER healthy.
    pub preflight: Option<serde_json::Value>,
    /// A startup failure the operator would otherwise never see (a malformed config.toml,
    /// a Chrome that would not start). This is the fix for the silent first run: the old
    /// shell wrote these to stderr, which a GUI user has no way to read.
    pub startup_error: Option<String>,
}

/// One `KEY=VALUE` upsert for `worker-secrets.env`. An empty `value` DELETES the key.
#[derive(Debug, Clone, Deserialize)]
pub struct SecretEntryDto {
    pub key: String,
    pub value: String,
}

/// The wizard's partial config write. Mirrors [`ConfigPatch`] but is the deserializable
/// boundary type — `None` means "leave this alone", so one step never clobbers another's
/// field.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SetupPatchDto {
    pub dispatch_base_url: Option<String>,
    pub control_port: Option<u16>,
    pub cdp_port: Option<u16>,
    /// Platform NAMES. An empty list is refused downstream (`update_config`) — a box that
    /// advertises nothing can never be sent a job (F9.2).
    pub worker_platforms: Option<Vec<String>>,
}

/// The result of the wizard's one-shot dispatch reachability probe.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DispatchProbeDto {
    pub reachable: bool,
    /// HTTP status when something answered. A 401/404 still means REACHABLE — the question
    /// is whether this box can talk to that host, not whether the path exists.
    pub status: Option<u16>,
    pub detail: String,
}

/// Recompute and return the whole setup state. Called on wizard open and after every
/// `status-updated` event.
#[tauri::command]
pub async fn get_setup_state(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<SetupStateDto, String> {
    Ok(build_setup_state(&app, &state).await)
}

/// Write one wizard step's config, then restart the sidecar so the child picks it up.
///
/// Refuses while a job is live — killing a tenant's run to apply a config write is never
/// the right trade, and the check happens BEFORE the write so a refusal leaves nothing
/// half-applied.
#[tauri::command]
pub async fn save_setup_step(
    app: AppHandle,
    state: State<'_, AppState>,
    patch: SetupPatchDto,
) -> Result<SetupStateDto, String> {
    refuse_while_job_running(&state).await?;
    let platforms = match patch.worker_platforms {
        Some(list) => Some(join_platforms(&list)?),
        None => None,
    };
    let next = crate::config::update_config(
        &app,
        ConfigPatch {
            dispatch_base_url: patch.dispatch_base_url,
            control_port: patch.control_port,
            cdp_port: patch.cdp_port,
            worker_platforms: platforms,
            // A step write never touches the operator's intent flags.
            setup_complete: None,
            setup_deferred: None,
        },
    )
    .map_err(sanitize)?;
    state.config.set(next);
    restart_worker(&state).await;
    Ok(build_setup_state(&app, &state).await)
}

/// Upsert engine/provider secrets into the 0600 `worker-secrets.env`, then restart the
/// sidecar so the child's environment actually contains them.
///
/// Read-modify-write: writing `OPENROUTER_API_KEY` must not wipe an operator's hand-added
/// `TELEGRAM_*` lines. Values are never echoed back — the UI learns only NAMES.
#[tauri::command]
pub async fn save_worker_secrets(
    app: AppHandle,
    state: State<'_, AppState>,
    entries: Vec<SecretEntryDto>,
) -> Result<SetupStateDto, String> {
    refuse_while_job_running(&state).await?;
    let upserts: Vec<(String, String)> = entries
        .into_iter()
        .map(|e| (e.key.trim().to_string(), e.value.trim().to_string()))
        .collect();
    crate::config::write_worker_secrets(&app, &upserts).map_err(sanitize)?;
    restart_worker(&state).await;
    Ok(build_setup_state(&app, &state).await)
}

/// Store the per-worker enrolment token from the panel (Fleet → Add worker) in the 0600
/// `dispatch-token.secret`, then restart the sidecar so it registers with it.
#[tauri::command]
pub async fn save_enrolment_token(
    app: AppHandle,
    state: State<'_, AppState>,
    token: String,
) -> Result<SetupStateDto, String> {
    refuse_while_job_running(&state).await?;
    crate::config::write_bootstrap_token(&app, &token).map_err(sanitize)?;
    restart_worker(&state).await;
    Ok(build_setup_state(&app, &state).await)
}

/// Mint an `AIZU_SECRET_KEY` (32 random bytes, urlsafe-base64) for the Credentials step.
///
/// Exists so nobody has to run a Python one-liner in a terminal a worker-PC operator does
/// not have. The value is returned ONCE, straight into the (write-only) input; it is never
/// logged and never persisted by this command — saving it is a separate, explicit act.
#[tauri::command]
pub async fn generate_secret_key() -> Result<String, String> {
    Ok(crate::config::generate_secret_key())
}

/// Setup → Chrome: reuse an attachable Chrome or launch ONE on the dedicated profile.
/// Returns the CDP url it settled on. The error string is the operator's failure copy —
/// this is where `ensure_running`'s real error finally reaches a human instead of stderr.
///
/// Runs on the blocking pool: it shells out to a Playwright probe and can wait up to 15s
/// for a launch, which must not occupy an async runtime thread.
#[tauri::command]
pub async fn launch_chrome(state: State<'_, AppState>) -> Result<String, String> {
    let chrome = state.chrome.clone();
    let joined = tauri::async_runtime::spawn_blocking(move || {
        // A std Mutex, deliberately: every ChromeManager call is blocking and nothing
        // awaits while holding it, so an async mutex would buy nothing and cost a
        // held-guard-across-await hazard.
        let mut guard = match chrome.lock() {
            Ok(g) => g,
            Err(poisoned) => poisoned.into_inner(),
        };
        guard.ensure_running()
    })
    .await;
    match joined {
        Ok(Ok(url)) => Ok(url),
        Ok(Err(e)) => Err(sanitize(e)),
        Err(_) => Err("Chrome launch task did not complete.".into()),
    }
}

/// Ask the sidecar to re-run its launch preflight now. ACCEPTED, not completed — the
/// answer arrives on the next `/status` poll (~1.5s), which is what the UI renders.
#[tauri::command]
pub async fn run_preflight(state: State<'_, AppState>) -> Result<bool, String> {
    state
        .control
        .send_command(Command::RunPreflight)
        .await
        .map_err(sanitize)
}

/// Setup → Sign in: open/focus a login tab for `platform` in the warmed Chrome.
///
/// The operator then signs in **in the real Chrome window**, 2FA and all, and the badge
/// flips green by itself when the next preflight reads the cookie jar. There is no "I'm
/// done" button to lie to.
///
/// This is the local half of the handoff `server._handle_agent_launch_login` has always
/// described in distributed mode ("its login tab is opened from that box's own desktop
/// app") and which nothing implemented until now.
#[tauri::command]
pub async fn open_login_tab(
    state: State<'_, AppState>,
    platform: String,
) -> Result<bool, String> {
    let platform = platform.trim().to_lowercase();
    // Fail here with a readable message rather than sending a string the control surface
    // will reject with a generic 400.
    if !CDP_PLATFORMS.contains(&platform.as_str()) {
        return Err(format!(
            "{platform:?} is not a browser platform — login tabs exist only for {}.",
            CDP_PLATFORMS.join(", ")
        ));
    }
    state
        .control
        .send_command(Command::OpenLoginTab { platform })
        .await
        .map_err(sanitize)
}

/// One-shot "can this box reach your cloud?" for the Connect step. NEVER a gate: a flaky
/// network must not be able to block setup, and it is deliberately not a preflight check
/// for the same reason (a preflight that fails on a blinked VPN parks working boxes).
#[tauri::command]
pub async fn probe_dispatch(url: String) -> Result<DispatchProbeDto, String> {
    match crate::control_client::probe_dispatch(&url).await {
        Ok(code) => Ok(DispatchProbeDto {
            reachable: true,
            status: Some(code),
            detail: format!("reached it (HTTP {code})"),
        }),
        Err(reason) => Ok(DispatchProbeDto {
            reachable: false,
            status: None,
            detail: reason,
        }),
    }
}

/// Record the operator's Sign-in skips so the DERIVED step can see them.
///
/// The UI owns the set and sends the whole list, so there is no merge to get wrong. Nothing
/// is written to disk and nothing is sent upstream: a skip is a statement about a check the
/// operator believes is wrong on THIS screen, not a change to the box. It deliberately does
/// NOT restart the sidecar — there is no new environment to pick up, and a restart here
/// would cost a re-register (and a `worker_token_hash` rotation) for a UI toggle.
#[tauri::command]
pub async fn set_login_skips(
    app: AppHandle,
    state: State<'_, AppState>,
    platforms: Vec<String>,
) -> Result<SetupStateDto, String> {
    let mut wanted = std::collections::BTreeSet::new();
    for raw in &platforms {
        let name = raw.trim().to_lowercase();
        if !CDP_PLATFORMS.contains(&name.as_str()) {
            return Err(format!(
                "{raw:?} is not a browser platform — only {} have a sign-in step.",
                CDP_PLATFORMS.join(", ")
            ));
        }
        wanted.insert(name);
    }
    state.set_login_skips(wanted);
    Ok(build_setup_state(&app, &state).await)
}

/// Mark setup finished and drop the operator on the dashboard.
///
/// There is deliberately no "finish anyway": the wizard's Verify gate is "no fatal check
/// is failing", and letting an operator past that recreates the green-but-dead box this
/// whole feature exists to kill. The escape hatch is [`defer_setup`].
///
/// Finishing clears any earlier deferral, so the flag never outlives the state it described.
#[tauri::command]
pub async fn finish_setup(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<SetupStateDto, String> {
    let next = crate::config::update_config(
        &app,
        ConfigPatch {
            setup_complete: Some(true),
            setup_deferred: Some(false),
            ..Default::default()
        },
    )
    .map_err(sanitize)?;
    state.config.set(next);
    Ok(build_setup_state(&app, &state).await)
}

/// "Close and fix later" — the escape hatch from a Verify gate the operator cannot clear.
///
/// Setup is NOT marked complete: the box stays registered, parked and visible in the fleet
/// console, every failing check still shows in the dashboard strip, and the rail's Setup
/// button still reads "Finish setup". The ONE thing this changes is that the wizard stops
/// re-opening over the dashboard on every launch — without it, an operator whose only red
/// row is a fatal they cannot fix from that PC (or an unvalidated LinkedIn/X login
/// classifier) met the same unexitable modal every single time the app started, which is
/// not an escape hatch at all.
///
/// It also settles the wizard for boot purposes, so the shell takes over the Chrome launch
/// on the next start (`DesktopConfig::wizard_settled`).
#[tauri::command]
pub async fn defer_setup(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<SetupStateDto, String> {
    let next = crate::config::update_config(
        &app,
        ConfigPatch { setup_deferred: Some(true), ..Default::default() },
    )
    .map_err(sanitize)?;
    state.config.set(next);
    Ok(build_setup_state(&app, &state).await)
}

// ---------------------------------------------------------------- setup internals

/// Assemble the DTO from config + the live status snapshot.
async fn build_setup_state(app: &AppHandle, state: &AppState) -> SetupStateDto {
    let cfg = state.config.get();
    // A missing control surface is NORMAL here (the sidecar may not be configured yet, or
    // may be mid-restart), so an unreachable status is `None`, never an error.
    let status = state.control.get_status().await.ok();
    let preflight = status.as_ref().and_then(|s| s.preflight.clone());
    let platforms = expand_platforms(&cfg.worker_platforms);
    let cdp_selected: Vec<String> = platforms
        .iter()
        .filter(|p| CDP_PLATFORMS.contains(&p.as_str()))
        .cloned()
        .collect();

    let skipped_logins = state.login_skips();

    SetupStateDto {
        step: derive_step(
            cfg.is_configured(),
            status.as_ref(),
            preflight.as_ref(),
            &skipped_logins,
        ),
        setup_complete: cfg.setup_complete,
        setup_deferred: cfg.setup_deferred,
        dispatch_base_url: cfg.dispatch_base_url.clone(),
        control_port: cfg.control_port,
        cdp_port: cfg.cdp_port,
        worker_platforms: platforms,
        cdp_platforms: cdp_selected,
        supported_platforms: SUPPORTED_PLATFORMS.iter().map(|s| s.to_string()).collect(),
        cdp_platform_names: CDP_PLATFORMS.iter().map(|s| s.to_string()).collect(),
        has_dispatch_token: crate::config::read_bootstrap_token(app).is_some(),
        skipped_logins,
        secret_names: crate::config::read_worker_secret_names(app),
        sidecar_running: status.is_some(),
        worker_id: status.as_ref().and_then(|s| s.worker_id.clone()),
        reenrolment_required: status
            .as_ref()
            .map(|s| s.controls.reenrolment_required)
            .unwrap_or(false),
        job_active: status.as_ref().map(|s| s.current_job.is_some()).unwrap_or(false),
        preflight,
        startup_error: state.startup_error(),
    }
}

/// The wizard's step machine, as a pure function of live facts.
///
/// Returns the FIRST step whose gate is not green, or `8` when all of them are. Nothing is
/// remembered between calls — that is the property that makes the wizard agree with a box
/// an operator fixed by hand, and that makes a mid-setup quit resume in the right place.
///
/// Every gate above step 1 reads the sidecar's preflight. A MISSING report (`None`) is
/// therefore not green for anything: an unfinished preflight has cleared nothing, so the
/// wizard parks at step 2 and the UI says "checking…".
///
/// `skipped_logins` is the operator's Sign-in skips. It is an INPUT here, not a UI
/// decoration, because this function is the only thing that can open the Finish gate: a
/// skip the derivation never saw left the wizard unexitable behind a login classifier that
/// `readiness.py` itself documents as unvalidated for LinkedIn and X.
fn derive_step(
    configured: bool,
    status: Option<&StatusDto>,
    preflight: Option<&serde_json::Value>,
    skipped_logins: &[String],
) -> u8 {
    // 1 · Connect — a dispatch URL is set AND the sidecar's control surface answers.
    //     Registration deliberately does NOT gate this: the control surface binds before
    //     register precisely so a box with an unreachable cloud can still be configured.
    let sidecar_alive = status.is_some();
    if !configured || !sidecar_alive {
        return 1;
    }
    let Some(pf) = preflight else {
        return 2; // report not in yet — nothing downstream can be called green
    };

    // 2 · Keys — the box can persist its identity (F9.1) and has an LLM backend (F9.3).
    //     `llm_backend` is fatal normally but demoted to warn on a warming-only box, so a
    //     warn-severity failure is allowed through here; the row still shows amber.
    let llm_ok = is_status(pf, "llm_backend", &["pass", "skip"])
        || (is_status(pf, "llm_backend", &["fail"]) && is_severity(pf, "llm_backend", "warn"));
    if !is_status(pf, "token_persistence", &["pass"]) || !llm_ok {
        return 2;
    }

    // 3 · Enrol — the box has an identity from dispatch and is not revoked. B10 outranks
    //     everything: a revoked box needs a NEW token, not a re-run of any later step.
    let enrolled = status.and_then(|s| s.worker_id.as_ref()).is_some();
    let revoked = status.map(|s| s.controls.reenrolment_required).unwrap_or(false);
    if !enrolled || revoked {
        return 3;
    }

    // 4 · Platforms — F9.2, made unreachable by construction (the UI cannot save zero).
    if !is_status(pf, "capabilities", &["pass"]) {
        return 4;
    }

    // 5 · Chrome — reachable AND really attachable (B6/D3: HTTP 200 proves only a socket).
    //     `skip` on either means this box advertises no browser platform, so both Chrome
    //     steps fall away. That skip comes from the REPORT, not from a hardcoded sequence.
    let chrome_applies = !is_status(pf, "cdp_reachable", &["skip"]);
    if chrome_applies
        && !(is_status(pf, "cdp_reachable", &["pass"])
            && is_status(pf, "cdp_attachable", &["pass"]))
    {
        return 5;
    }

    // 6 · Sign in — every advertised browser platform is signed in, or skipped by the
    //     operator. The skip suppresses NOTHING: the row stays `fail` in the report the
    //     fleet console and the dashboard strip both render. It exists because these rows
    //     are warn-severity and their classifier is unvalidated for LinkedIn and X — a
    //     false red there must cost the operator a click, never their only way out.
    if chrome_applies && !all_logins_settled(pf, skipped_logins) {
        return 6;
    }

    // 7 · Verify — no FATAL check is failing. `unknown` and `warn` never block: a fatal
    //     that returned unknown is a contradiction the Python forbids, and blocking on a
    //     warn would trap an operator behind, say, an unvalidated LinkedIn cookie name.
    if has_blocking_failure(pf) {
        return 7;
    }
    8
}

/// True when every `login.<platform>` row is `pass`, `skip` (the API-only / no-Chrome
/// case), or a platform the operator skipped by hand. An `unknown` or `fail` row that
/// nobody skipped keeps them on step 6 — which is the wizard doing its job (rule 2: the
/// wizard may hold a human until Chrome is signed in) right up to the moment the operator
/// says the check is wrong.
fn all_logins_settled(pf: &serde_json::Value, skipped: &[String]) -> bool {
    checks(pf)
        .iter()
        .filter(|c| str_field(c, "id").starts_with("login."))
        .all(|c| {
            matches!(str_field(c, "status"), "pass" | "skip")
                || str_field(c, "id")
                    .strip_prefix("login.")
                    .map(|platform| skipped.iter().any(|p| p == platform))
                    .unwrap_or(false)
        })
}

/// Any `fatal` check with status `fail` — the exact condition that parks the box.
fn has_blocking_failure(pf: &serde_json::Value) -> bool {
    checks(pf)
        .iter()
        .any(|c| str_field(c, "severity") == "fatal" && str_field(c, "status") == "fail")
}

fn checks(pf: &serde_json::Value) -> &[serde_json::Value] {
    pf.get("checks").and_then(|c| c.as_array()).map(|v| v.as_slice()).unwrap_or(&[])
}

fn str_field<'a>(value: &'a serde_json::Value, key: &str) -> &'a str {
    value.get(key).and_then(|v| v.as_str()).unwrap_or("")
}

/// Is check `id` present with one of `wanted` statuses? A check that is ABSENT answers
/// false to everything, which keeps an unknown-to-us report from reading as green.
fn is_status(pf: &serde_json::Value, id: &str, wanted: &[&str]) -> bool {
    checks(pf)
        .iter()
        .find(|c| str_field(c, "id") == id)
        .map(|c| wanted.contains(&str_field(c, "status")))
        .unwrap_or(false)
}

fn is_severity(pf: &serde_json::Value, id: &str, wanted: &str) -> bool {
    checks(pf)
        .iter()
        .find(|c| str_field(c, "id") == id)
        .map(|c| str_field(c, "severity") == wanted)
        .unwrap_or(false)
}

/// `"all"` → every supported platform; otherwise the CSV, lowercased, de-duped, and
/// filtered to names the engine actually knows.
fn expand_platforms(raw: &str) -> Vec<String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() || trimmed.eq_ignore_ascii_case(ALL_PLATFORMS) {
        return SUPPORTED_PLATFORMS.iter().map(|s| s.to_string()).collect();
    }
    let mut out: Vec<String> = Vec::new();
    for part in trimmed.split(',') {
        let name = part.trim().to_lowercase();
        if SUPPORTED_PLATFORMS.contains(&name.as_str()) && !out.contains(&name) {
            out.push(name);
        }
    }
    out
}

/// The inverse: a checkbox selection → the `worker_platforms` string. A full selection
/// collapses to `"all"` so the box automatically picks up a platform the engine adds
/// later; anything else is stored explicitly.
fn join_platforms(list: &[String]) -> Result<String, String> {
    let mut chosen: Vec<String> = Vec::new();
    for raw in list {
        let name = raw.trim().to_lowercase();
        if !SUPPORTED_PLATFORMS.contains(&name.as_str()) {
            return Err(format!("{raw:?} is not a platform this worker supports."));
        }
        if !chosen.contains(&name) {
            chosen.push(name);
        }
    }
    if chosen.is_empty() {
        // Also refused one layer down; refused here too so the message names the reason.
        return Err("Pick at least one platform. A box that advertises nothing can never be \
                    sent a job, and it makes your team's readiness banner lie."
            .into());
    }
    if chosen.len() == SUPPORTED_PLATFORMS.len() {
        return Ok(ALL_PLATFORMS.to_string());
    }
    Ok(chosen.join(","))
}

/// Apply a config/secret write by restarting the sidecar, starting one first if the box
/// booted unconfigured and therefore has no watchdog yet.
///
/// Both halves matter: `ensure_started` is what makes the FIRST save in the wizard bring
/// the worker up without an app relaunch, and `restart` is what makes every later save
/// take effect (the child reads its environment exactly once, at spawn).
async fn restart_worker(state: &AppState) {
    // The control-surface port can have just changed; the client must follow it or the UI
    // polls a dead port forever while a perfectly healthy worker runs behind it.
    state.control.set_port(state.config.get().control_port);
    crate::sidecar_supervisor::SidecarSupervisor::ensure_started(&state.supervisor);
    state.supervisor.restart().await;
}

/// The one guard every mutating setup command shares. Checked BEFORE any write, so a
/// refusal never leaves a half-applied config.
///
/// An unreachable control surface is NOT a job — a box whose sidecar is down must remain
/// configurable, or a crash-looping worker becomes unfixable from its own UI.
async fn refuse_while_job_running(state: &AppState) -> Result<(), String> {
    match state.control.get_status().await {
        Ok(status) if status.current_job.is_some() => Err(
            "A job is running on this box. Applying this would restart the worker and kill \
             that run — stop the job first, or wait for it to finish."
                .into(),
        ),
        _ => Ok(()),
    }
}

/// Map a `DesktopError` to a UI-safe string. thiserror's Display already avoids secrets by
/// construction (variants carry ports/host context, never tokens); this is the single
/// choke point where an engineer can further redact if a variant ever grows a path.
fn sanitize(e: crate::errors::DesktopError) -> String {
    e.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// Build a preflight report body with the given (id, severity, status) rows.
    fn report(rows: &[(&str, &str, &str)]) -> serde_json::Value {
        json!({
            "ok": false,
            "blocking": false,
            "enforced": true,
            "checks": rows.iter().map(|(id, sev, st)| json!({
                "id": id, "title": id, "severity": sev, "status": st,
                "detail": null, "remedy": null
            })).collect::<Vec<_>>()
        })
    }

    fn green_rows() -> Vec<(&'static str, &'static str, &'static str)> {
        vec![
            ("state_dir_writable", "fatal", "pass"),
            ("token_persistence", "fatal", "pass"),
            ("dispatch_credential", "warn", "pass"),
            ("capabilities", "fatal", "pass"),
            ("llm_backend", "fatal", "pass"),
            ("playwright", "warn", "pass"),
            ("cdp_reachable", "fatal", "pass"),
            ("cdp_port_drift", "warn", "pass"),
            ("cdp_attachable", "fatal", "pass"),
            ("login.instagram", "warn", "pass"),
        ]
    }

    /// A status snapshot that is enrolled and idle.
    fn enrolled() -> StatusDto {
        let mut s = StatusDto::default();
        s.worker_id = Some("w_1".into());
        s
    }

    /// The common case: nothing skipped.
    const NO_SKIPS: &[String] = &[];

    fn skips(platforms: &[&str]) -> Vec<String> {
        platforms.iter().map(|p| p.to_string()).collect()
    }

    #[test]
    fn unconfigured_or_dead_sidecar_is_step_one() {
        assert_eq!(derive_step(false, None, None, NO_SKIPS), 1);
        // configured but the control surface never answered
        assert_eq!(derive_step(true, None, None, NO_SKIPS), 1);
    }

    /// A report that has not landed yet must NOT read as green anywhere.
    #[test]
    fn missing_preflight_parks_at_keys() {
        assert_eq!(derive_step(true, Some(&enrolled()), None, NO_SKIPS), 2);
    }

    #[test]
    fn f9_1_unstorable_identity_is_step_two() {
        let pf = report(&[("token_persistence", "fatal", "fail"), ("llm_backend", "fatal", "pass")]);
        assert_eq!(derive_step(true, Some(&enrolled()), Some(&pf), NO_SKIPS), 2);
    }

    /// A warming-only box has no LLM and that is fine — the check demotes to warn.
    #[test]
    fn warn_severity_llm_failure_does_not_block_keys() {
        let mut rows = green_rows();
        rows.retain(|(id, _, _)| *id != "llm_backend");
        rows.push(("llm_backend", "warn", "fail"));
        let pf = report(&rows);
        assert_eq!(derive_step(true, Some(&enrolled()), Some(&pf), NO_SKIPS), 8);
    }

    #[test]
    fn no_worker_id_is_step_three_and_revocation_beats_later_steps() {
        let pf = report(&green_rows());
        let blank = StatusDto::default();
        assert_eq!(derive_step(true, Some(&blank), Some(&pf), NO_SKIPS), 3);

        let mut revoked = enrolled();
        revoked.controls.reenrolment_required = true;
        assert_eq!(derive_step(true, Some(&revoked), Some(&pf), NO_SKIPS), 3);
    }

    #[test]
    fn f9_2_no_capabilities_is_step_four() {
        let mut rows = green_rows();
        rows.retain(|(id, _, _)| *id != "capabilities");
        rows.push(("capabilities", "fatal", "fail"));
        assert_eq!(derive_step(true, Some(&enrolled()), Some(&report(&rows)), NO_SKIPS), 4);
    }

    /// B6/D3: a Chrome that answers HTTP but refuses the attach is step 5, not step 6.
    #[test]
    fn attachable_failure_is_step_five_even_when_reachable_passes() {
        let mut rows = green_rows();
        rows.retain(|(id, _, _)| *id != "cdp_attachable");
        rows.push(("cdp_attachable", "fatal", "fail"));
        assert_eq!(derive_step(true, Some(&enrolled()), Some(&report(&rows)), NO_SKIPS), 5);
    }

    #[test]
    fn logged_out_platform_is_step_six() {
        let mut rows = green_rows();
        rows.retain(|(id, _, _)| *id != "login.instagram");
        rows.push(("login.instagram", "warn", "fail"));
        assert_eq!(derive_step(true, Some(&enrolled()), Some(&report(&rows)), NO_SKIPS), 6);
    }

    /// F-4, the unexitable wizard. `readiness.py` classifies a MISSING cookie as
    /// `logged_out`, and the LinkedIn/X session signatures are not validated against a live
    /// account — so this row can be red on a box whose browser is signed in perfectly well.
    /// It never clears on its own. Before the skip reached this function the operator was
    /// held on step 6 forever: Finish is on step 7, and the last pane's Next only enables
    /// when the derived step has moved past it.
    #[test]
    fn a_permanently_logged_out_row_the_operator_skipped_reaches_the_finish_gate() {
        let mut rows = green_rows();
        rows.retain(|(id, _, _)| *id != "login.instagram");
        rows.push(("login.linkedin", "warn", "fail"));
        let pf = report(&rows);

        // Unskipped it holds the operator on Sign-in — the wizard IS allowed to block here.
        assert_eq!(derive_step(true, Some(&enrolled()), Some(&pf), NO_SKIPS), 6);
        // Skipped, it lets them through to Finish.
        assert_eq!(derive_step(true, Some(&enrolled()), Some(&pf), &skips(&["linkedin"])), 8);
        // A skip on a DIFFERENT platform does not clear this one.
        assert_eq!(derive_step(true, Some(&enrolled()), Some(&pf), &skips(&["x"])), 6);
    }

    /// A skip is an escape from a warn-severity login row, NOT from a fatal. Skipping every
    /// login on a box whose Chrome check is red must still hold Verify — otherwise the skip
    /// button becomes the "finish anyway" this wizard deliberately does not have.
    #[test]
    fn skipping_logins_never_clears_a_fatal() {
        let mut rows = green_rows();
        rows.retain(|(id, _, _)| *id != "state_dir_writable" && *id != "login.instagram");
        rows.push(("state_dir_writable", "fatal", "fail"));
        rows.push(("login.instagram", "warn", "fail"));
        assert_eq!(
            derive_step(true, Some(&enrolled()), Some(&report(&rows)), &skips(&["instagram"])),
            7
        );
    }

    /// An API-only box skips BOTH Chrome steps — derived from the report's own `skip`
    /// rows, not from a hardcoded step list.
    #[test]
    fn api_only_box_skips_chrome_and_signin() {
        let rows = vec![
            ("state_dir_writable", "fatal", "pass"),
            ("token_persistence", "fatal", "pass"),
            ("capabilities", "fatal", "pass"),
            ("llm_backend", "fatal", "pass"),
            ("cdp_reachable", "fatal", "skip"),
            ("cdp_attachable", "fatal", "skip"),
        ];
        assert_eq!(derive_step(true, Some(&enrolled()), Some(&report(&rows)), NO_SKIPS), 8);
    }

    /// A warn-level failure must never hold the operator on Verify.
    #[test]
    fn warn_only_failures_finish_but_a_fatal_fail_holds_verify() {
        let mut rows = green_rows();
        rows.push(("dispatch_credential", "warn", "fail"));
        assert_eq!(derive_step(true, Some(&enrolled()), Some(&report(&rows)), NO_SKIPS), 8);

        // A fatal that none of the earlier gates reads (state_dir) still holds Verify —
        // step 7's gate is "no fatal is failing", not "the gates I happen to name".
        let mut fatal = green_rows();
        fatal.retain(|(id, _, _)| *id != "state_dir_writable");
        fatal.push(("state_dir_writable", "fatal", "fail"));
        assert_eq!(derive_step(true, Some(&enrolled()), Some(&report(&fatal)), NO_SKIPS), 7);
    }

    #[test]
    fn platform_round_trip_collapses_a_full_selection_to_all() {
        assert_eq!(expand_platforms("all").len(), SUPPORTED_PLATFORMS.len());
        assert_eq!(expand_platforms(""), expand_platforms("all"));
        assert_eq!(expand_platforms("x, instagram ,x"), vec!["x", "instagram"]);
        assert_eq!(expand_platforms("nope"), Vec::<String>::new());

        let all: Vec<String> = SUPPORTED_PLATFORMS.iter().map(|s| s.to_string()).collect();
        assert_eq!(join_platforms(&all).unwrap(), "all");
        assert_eq!(join_platforms(&["X".into(), "instagram".into()]).unwrap(), "x,instagram");
        assert!(join_platforms(&[]).is_err());
        assert!(join_platforms(&["myspace".into()]).is_err());
    }
}
