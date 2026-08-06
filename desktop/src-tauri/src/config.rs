//! Immutable desktop-shell configuration (Phase 6 SCAFFOLD, UNCOMPILED).
//!
//! Loaded and validated from a TOML file at `app_config_dir()/config.toml`. The struct is
//! constructed once at startup and never mutated (shared as `Arc<DesktopConfig>`).
//!
//! # Secrets are NEVER in this file
//! The TOML holds only non-sensitive wiring (URLs, ports, paths). Secrets — the worker
//! bootstrap token, provider API keys — are read from the environment or the OS keychain
//! at sidecar-spawn time (see `sidecar_supervisor.rs`), and the loopback control-surface
//! token is GENERATED per spawn (see `control_client::generate_token`). None of these are
//! ever written back to the TOML.
//!
//! Mirrors the Python `WorkerConfig` field names where they cross the env boundary, so the
//! env we hand the child (`AIZU_*`) lines up with `aizu.worker.config`.

use std::path::PathBuf;
use std::sync::Arc;

use serde::Deserialize;
use tauri::{AppHandle, Manager};

use crate::errors::DesktopError;

/// The live-proven CDP port. The engine's own historical default is 9222, but EVERY live
/// run in this repo has used 9333 (per `memory/engine-live-run.md`). We default to 9333 to
/// match reality and avoid the 9222 ECONNREFUSED history.
pub const DEFAULT_CDP_PORT: u16 = 9333;

/// The sidecar control-surface default. Deliberately NOT 8799 — the dev panel
/// (`aizu.cli panel --port 8799`) commonly runs on 8799 on a dev box, so the worker
/// control surface uses a distinct port to avoid the collision.
pub const DEFAULT_CONTROL_PORT: u16 = 8788;

/// The TOML filename under `app_config_dir()`.
const CONFIG_FILENAME: &str = "config.toml";

/// The worker bootstrap token is a SECRET, so it is NEVER written to config.toml. The dev
/// menu stores it in this sibling 0600 file, which the supervisor reads and passes to the
/// child as `AIZU_WORKER_BOOTSTRAP_TOKEN`. (A future upgrade moves this to the OS
/// keychain; a 0600 file is the pragmatic local-fleet store for now.)
const TOKEN_FILENAME: &str = "dispatch-token.secret";

/// Provider / engine secrets the SIDECAR's engine subprocess needs for a live run
/// (`OPENROUTER_API_KEY`, `AIZU_SECRET_KEY`, model overrides, `YOUTUBE_API_KEY`,
/// `TELEGRAM_*`, …). A GUI-launched worker inherits only a minimal launchd environment, so
/// these can NOT be assumed present in the app's env — without them every live run dies at
/// `_build_run_io` with "OPENROUTER_API_KEY not set" and the job completes with zero leads.
/// The operator drops a 0600 `KEY=VALUE` file here; the supervisor reads it at spawn and
/// injects each var into the child's env. Kept OUT of config.toml (secrets) and off the wire
/// (never shipped in the job spec — each worker box owns its own provider credentials).
const SECRETS_FILENAME: &str = "worker-secrets.env";

/// Raw TOML shape (all fields with serde defaults so a minimal file still parses).
/// Validated into `DesktopConfig` by `TomlConfig::validate`.
#[derive(Debug, Deserialize)]
struct TomlConfig {
    dispatch_base_url: String,
    #[serde(default = "default_cdp_port")]
    cdp_port: u16,
    #[serde(default)]
    chrome_profile_dir: String,
    /// Path to the bundled/installed `aizu-worker` binary. If empty, the supervisor
    /// resolves it relative to the app resource dir.
    #[serde(default)]
    sidecar_binary_path: String,
    #[serde(default)]
    state_dir: String,
    #[serde(default)]
    db_path: String,
    #[serde(default = "default_control_port")]
    control_port: u16,
    /// Which platforms this box advertises it can run (capability declaration). Comma-
    /// separated (`instagram,x,linkedin`) or `all` for every supported platform. Passed
    /// to the sidecar as `AIZU_WORKER_PLATFORMS`; without a matching capability the
    /// fleet dispatch rejects a run with "no capable worker". Default: `all`.
    #[serde(default = "default_worker_platforms")]
    worker_platforms: String,
}

fn default_cdp_port() -> u16 {
    DEFAULT_CDP_PORT
}
fn default_control_port() -> u16 {
    DEFAULT_CONTROL_PORT
}
fn default_worker_platforms() -> String {
    "all".to_string()
}

/// Immutable, validated configuration. Cloned cheaply behind an `Arc`.
#[derive(Debug, Clone)]
pub struct DesktopConfig {
    /// Cloud dispatch base URL the sidecar leases from (outbound HTTPS). NOT loopback.
    pub dispatch_base_url: String,
    /// CDP port for the managed Chrome (default 9333).
    pub cdp_port: u16,
    /// Dedicated Chrome user-data-dir (NEVER the default profile — Chrome refuses
    /// --remote-debugging-port on the default profile).
    pub chrome_profile_dir: PathBuf,
    /// Absolute path to the `aizu-worker` binary, or empty to resolve from resources.
    pub sidecar_binary_path: PathBuf,
    /// Worker state dir (`AIZU_WORKER_STATE`) — machine-id, single-flight locks, logs/.
    pub state_dir: PathBuf,
    /// SQLite DB path (`AIZU_DB`).
    pub db_path: PathBuf,
    /// Loopback control-surface port (default 8799).
    pub control_port: u16,
    /// Platforms this box advertises as capabilities (`AIZU_WORKER_PLATFORMS`):
    /// comma-separated or `all`. Empty/`all` → every supported platform, pool-wide.
    pub worker_platforms: String,
}

impl DesktopConfig {
    /// The CDP URL we hand the child as `AIZU_CDP_URL` and the Chrome manager binds.
    pub fn cdp_url(&self) -> String {
        format!("http://127.0.0.1:{}", self.cdp_port)
    }
}

/// A commented template dropped at `app_config_dir()/config.example.toml` on first run so
/// the operator knows exactly what to fill in.
const EXAMPLE_TOML: &str = r#"# AIZU Worker — copy this to config.toml and fill in, then relaunch.
# Secrets (worker bootstrap token, provider keys) are NEVER put here. The bootstrap token
# lives in dispatch-token.secret (dev menu writes it); engine/provider secrets such as
# OPENROUTER_API_KEY go in worker-secrets.env (see worker-secrets.example.env). This file
# is non-sensitive wiring only.

dispatch_base_url = "https://your-cloud-dispatch.example.com"  # the cloud you lease jobs from
cdp_port = 9333            # managed Chrome remote-debugging port
control_port = 8799        # loopback control surface
chrome_profile_dir = ""    # a DEDICATED profile dir (NOT your default Chrome profile); "" = auto under app data
sidecar_binary_path = ""   # "" = use the aizu-worker bundled inside the app
state_dir = ""             # "" = auto under app data
db_path = ""               # "" = auto under app data
worker_platforms = "all"   # platforms this box can run: "all" or e.g. "instagram,x,linkedin"
"#;

/// Load the config from `app_config_dir()/config.toml`. On FIRST RUN (no file) this drops an
/// example template and returns a defaults-only config with an EMPTY dispatch URL — the app
/// still opens (in a "not configured" state) rather than crashing, and no sidecar is spawned
/// until the operator fills in `config.toml`. A malformed/invalid EXISTING file still errors
/// (surfaced in the UI, non-fatal to the window).
pub fn load(app: &AppHandle) -> Result<DesktopConfig, DesktopError> {
    let dir = app
        .path()
        .app_config_dir()
        .map_err(|e| DesktopError::ConfigInvalid(format!("no app_config_dir: {e}")))?;
    let _ = std::fs::create_dir_all(&dir);
    let path = dir.join(CONFIG_FILENAME);
    // Always refresh the example templates so an existing install also learns about
    // worker-secrets.env (the engine-secrets file). Best-effort; never fatal.
    let _ = std::fs::write(dir.join("config.example.toml"), EXAMPLE_TOML);
    let _ = std::fs::write(dir.join("worker-secrets.example.env"), SECRETS_EXAMPLE);
    if !path.exists() {
        return Ok(default_config(app));
    }
    let text = std::fs::read_to_string(&path).map_err(|e| {
        DesktopError::ConfigInvalid(format!("cannot read {}: {e}", path.display()))
    })?;
    let raw: TomlConfig = toml::from_str(&text)
        .map_err(|e| DesktopError::ConfigInvalid(format!("TOML parse error: {e}")))?;
    raw.into_config(app)
}

/// First-run defaults rooted under the OS app-data dir. Dispatch URL is empty on purpose —
/// `main` treats an empty dispatch as "not configured" and skips Chrome + sidecar startup.
fn default_config(app: &AppHandle) -> DesktopConfig {
    let data = app.path().app_data_dir().unwrap_or_else(|_| PathBuf::from("."));
    DesktopConfig {
        dispatch_base_url: String::new(),
        cdp_port: DEFAULT_CDP_PORT,
        chrome_profile_dir: data.join("chrome-profile"),
        sidecar_binary_path: PathBuf::new(),
        state_dir: data.join("worker-state"),
        db_path: data.join("aizu.db"),
        control_port: DEFAULT_CONTROL_PORT,
        worker_platforms: default_worker_platforms(),
    }
}

impl TomlConfig {
    /// Fail-fast boundary validation of an EXISTING file (never trust the file on disk),
    /// filling blank paths with app-data defaults so a minimal config.toml just works.
    fn into_config(self, app: &AppHandle) -> Result<DesktopConfig, DesktopError> {
        if self.dispatch_base_url.trim().is_empty() {
            return Err(DesktopError::ConfigInvalid(
                "dispatch_base_url is empty — set your cloud dispatch URL".into(),
            ));
        }
        if !self.dispatch_base_url.starts_with("http://")
            && !self.dispatch_base_url.starts_with("https://")
        {
            return Err(DesktopError::ConfigInvalid(format!(
                "dispatch_base_url must be http(s): got {:?}",
                self.dispatch_base_url
            )));
        }
        if self.cdp_port == 0 || self.control_port == 0 {
            return Err(DesktopError::ConfigInvalid(
                "cdp_port/control_port must be non-zero".into(),
            ));
        }
        let data = app.path().app_data_dir().unwrap_or_else(|_| PathBuf::from("."));
        let or_default = |v: String, default: PathBuf| {
            if v.trim().is_empty() { default } else { PathBuf::from(v) }
        };
        Ok(DesktopConfig {
            dispatch_base_url: self.dispatch_base_url.trim_end_matches('/').to_string(),
            cdp_port: self.cdp_port,
            chrome_profile_dir: or_default(self.chrome_profile_dir, data.join("chrome-profile")),
            sidecar_binary_path: PathBuf::from(self.sidecar_binary_path),
            state_dir: or_default(self.state_dir, data.join("worker-state")),
            db_path: or_default(self.db_path, data.join("aizu.db")),
            control_port: self.control_port,
            worker_platforms: {
                let p = self.worker_platforms.trim();
                if p.is_empty() { "all".to_string() } else { p.to_string() }
            },
        })
    }
}

/// Small helper the supervisor uses to avoid re-Arc'ing in call sites.
pub type SharedConfig = Arc<DesktopConfig>;

fn app_config_dir(app: &AppHandle) -> Result<PathBuf, DesktopError> {
    let dir = app
        .path()
        .app_config_dir()
        .map_err(|e| DesktopError::ConfigInvalid(format!("no app_config_dir: {e}")))?;
    std::fs::create_dir_all(&dir)
        .map_err(|e| DesktopError::ConfigInvalid(format!("cannot create config dir: {e}")))?;
    Ok(dir)
}

/// Write a minimal `config.toml` from the dev menu (dispatch + ports). Paths are left
/// blank so they resolve to the app-data defaults. The dispatch URL is validated by the
/// caller (`commands::save_config`) — we still hard-reject characters that could break the
/// TOML string as defense-in-depth.
pub fn write_config(app: &AppHandle, dispatch_base_url: &str, control_port: u16)
    -> Result<(), DesktopError> {
    if dispatch_base_url.contains('"') || dispatch_base_url.contains('\n') {
        return Err(DesktopError::ConfigInvalid(
            "dispatch_base_url contains illegal characters".into(),
        ));
    }
    let dir = app_config_dir(app)?;
    let body = format!(
        "# Written by the AIZU Worker dev menu. Secrets are NOT stored here.\n\
         dispatch_base_url = \"{}\"\n\
         cdp_port = {}\n\
         control_port = {}\n",
        dispatch_base_url.trim(), DEFAULT_CDP_PORT, control_port,
    );
    std::fs::write(dir.join(CONFIG_FILENAME), body)
        .map_err(|e| DesktopError::ConfigInvalid(format!("cannot write config.toml: {e}")))
}

/// Persist the worker bootstrap token to the 0600 sibling file. An empty token clears it.
pub fn write_bootstrap_token(app: &AppHandle, token: &str) -> Result<(), DesktopError> {
    let path = app_config_dir(app)?.join(TOKEN_FILENAME);
    if token.trim().is_empty() {
        let _ = std::fs::remove_file(&path);
        return Ok(());
    }
    std::fs::write(&path, token.trim())
        .map_err(|e| DesktopError::ConfigInvalid(format!("cannot write token file: {e}")))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

/// Read the bootstrap token the dev menu stored, if any. Never logged.
pub fn read_bootstrap_token(app: &AppHandle) -> Option<String> {
    let dir = app.path().app_config_dir().ok()?;
    let text = std::fs::read_to_string(dir.join(TOKEN_FILENAME)).ok()?;
    let trimmed = text.trim();
    if trimmed.is_empty() { None } else { Some(trimmed.to_string()) }
}

/// A commented template dropped at `app_config_dir()/worker-secrets.example.env` on first
/// run so the operator knows which engine secrets to supply. NEVER contains real values.
const SECRETS_EXAMPLE: &str = r#"# AIZU Worker — engine/provider secrets for the sidecar's live runs.
# Copy this to worker-secrets.env, fill in real values, then relaunch the app.
# Format: one KEY=VALUE per line. Lines starting with # and blank lines are ignored.
# This file is read at sidecar-spawn and injected into the engine's environment.
# It is NEVER shipped over the wire — each worker box owns its own credentials.
#
# REQUIRED for any live (non-dry) run — without it every run finds 0 leads:
# OPENROUTER_API_KEY=sk-or-...
#
# Needed if this box runs campaigns for orgs with encrypted per-org connections:
# AIZU_SECRET_KEY=...
#
# Optional model / platform overrides (fall back to engine defaults if absent):
# OPENROUTER_TEXT_MODEL=...
# OPENROUTER_VISION_MODEL=...
# YOUTUBE_API_KEY=...
# TELEGRAM_API_ID=...
# TELEGRAM_API_HASH=...
"#;

/// Parse the 0600 `worker-secrets.env` file (if present) into `(KEY, VALUE)` pairs the
/// supervisor injects into the sidecar's environment. TOLERANT (never fails the launch):
/// blank lines and `#` comments are skipped, surrounding quotes are stripped, and a line
/// without `=` or with an empty key is ignored. Values are NEVER logged. Returns an empty
/// vec when the file is absent or unreadable — a live run then fails visibly at the engine's
/// own `OPENROUTER_API_KEY not set` guard rather than being silently misconfigured here.
pub fn read_worker_secrets(app: &AppHandle) -> Vec<(String, String)> {
    let dir = match app.path().app_config_dir() {
        Ok(d) => d,
        Err(_) => return Vec::new(),
    };
    let text = match std::fs::read_to_string(dir.join(SECRETS_FILENAME)) {
        Ok(t) => t,
        Err(_) => return Vec::new(),
    };
    parse_secrets(&text)
}

/// Pure parser for the `worker-secrets.env` body (extracted so it is unit-testable without
/// an `AppHandle`). See [`read_worker_secrets`] for the tolerance contract.
fn parse_secrets(text: &str) -> Vec<(String, String)> {
    let mut pairs = Vec::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, val)) = line.split_once('=') else { continue };
        let key = key.trim();
        if key.is_empty() {
            continue;
        }
        let val = val.trim().trim_matches(|c| c == '"' || c == '\'');
        pairs.push((key.to_string(), val.to_string()));
    }
    pairs
}

#[cfg(test)]
mod tests {
    use super::parse_secrets;

    #[test]
    fn parses_keys_skips_comments_and_blanks_and_strips_quotes() {
        let body = "\
# a comment\n\
\n\
OPENROUTER_API_KEY=sk-or-abc123\n\
  AIZU_SECRET_KEY = \"quoted-value\" \n\
OPENROUTER_TEXT_MODEL='single-quoted'\n\
# trailing comment\n";
        let pairs = parse_secrets(body);
        assert_eq!(
            pairs,
            vec![
                ("OPENROUTER_API_KEY".to_string(), "sk-or-abc123".to_string()),
                ("AIZU_SECRET_KEY".to_string(), "quoted-value".to_string()),
                ("OPENROUTER_TEXT_MODEL".to_string(), "single-quoted".to_string()),
            ]
        );
    }

    #[test]
    fn ignores_malformed_lines_without_equals_or_empty_key() {
        let body = "no_equals_here\n=novalue\nGOOD=1\n";
        let pairs = parse_secrets(body);
        assert_eq!(pairs, vec![("GOOD".to_string(), "1".to_string())]);
    }

    #[test]
    fn empty_body_yields_no_pairs() {
        assert!(parse_secrets("").is_empty());
        assert!(parse_secrets("# only comments\n\n").is_empty());
    }
}
