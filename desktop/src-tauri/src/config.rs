//! Desktop-shell configuration (Phase 6 SCAFFOLD, UNCOMPILED).
//!
//! Loaded and validated from a TOML file at `app_config_dir()/config.toml`.
//!
//! # Mutability changed with the setup wizard (2026-08)
//! This used to be load-once-and-never-mutate. The first-run wizard writes config as the
//! operator walks it (dispatch URL → platforms → cdp port → `setup_complete` /
//! `setup_deferred`) and the
//! sidecar must pick each write up WITHOUT an app relaunch, so the live config now lives
//! behind a [`ConfigCell`] that `update_config` swaps atomically. Every reader takes a
//! cheap `Arc<DesktopConfig>` snapshot via [`ConfigCell::get`] and works from that — no
//! reader ever observes a half-applied write, and the `Arc` is still immutable.
//!
//! # Secrets are NEVER in config.toml
//! The TOML holds only non-sensitive wiring (URLs, ports, paths). The worker bootstrap /
//! enrolment token lives in the 0600 `dispatch-token.secret`; engine + provider keys
//! (`OPENROUTER_API_KEY`, `AIZU_SECRET_KEY`, …) live in the 0600 `worker-secrets.env`,
//! which the supervisor injects into the sidecar's env at spawn. The loopback
//! control-surface token is GENERATED per spawn (`control_client::generate_token`). None
//! of these is ever written to the TOML, returned to the UI, or logged. The UI can learn
//! that a secret is SET (see [`read_worker_secret_names`]) but never what it is.
//!
//! Mirrors the Python `WorkerConfig` field names where they cross the env boundary, so the
//! env we hand the child (`AIZU_*`) lines up with `aizu.worker.config`.

use std::path::PathBuf;
use std::sync::{Arc, RwLock};

use serde::Deserialize;
use tauri::{AppHandle, Manager};

use crate::errors::DesktopError;

/// The canonical CDP port. Resolved 2026-08 (FROZEN SPEC §1): **9333 is canonical
/// everywhere** — `warm_chrome.sh` binds it, `engines.md §9` documents it, and every live
/// run in this repo has used it. The worker sidecar's own preflight (`preflight.resolve_cdp_url`)
/// still ADOPTS a Chrome found on 9222 when nothing answers 9333 and the port was not set
/// explicitly, so an older hand-provisioned box keeps working and says so out loud.
pub const DEFAULT_CDP_PORT: u16 = 9333;

/// The sidecar control-surface default. Deliberately NOT 8799 — the dev panel
/// (`aizu.cli panel --port 8799`) commonly runs on 8799 on a dev box, so the worker
/// control surface uses a distinct port to avoid the collision.
pub const DEFAULT_CONTROL_PORT: u16 = 8788;

/// The TOML filename under `app_config_dir()`.
const CONFIG_FILENAME: &str = "config.toml";

/// The worker bootstrap/enrolment token is a SECRET, so it is NEVER written to config.toml.
/// Setup → Connect stores it in this sibling 0600 file, which the supervisor reads and
/// passes to the child as `AIZU_WORKER_BOOTSTRAP_TOKEN`. (A future upgrade moves this to
/// the OS keychain; a 0600 file is the pragmatic local-fleet store for now.)
const TOKEN_FILENAME: &str = "dispatch-token.secret";

/// Provider / engine secrets the SIDECAR's engine subprocess needs for a live run
/// (`OPENROUTER_API_KEY`, `AIZU_SECRET_KEY`, model overrides, `YOUTUBE_API_KEY`,
/// `TELEGRAM_*`, …). A GUI-launched worker inherits only a minimal launchd environment, so
/// these can NOT be assumed present in the app's env — without them the sidecar's preflight
/// fails `llm_backend` / `token_persistence` (ledger F9.1/F9.3) and the box parks instead of
/// leasing. Setup → Credentials writes this file; the supervisor reads it at spawn and
/// injects each var into the child's env. Kept OUT of config.toml (secrets) and off the wire
/// (never shipped in the job spec — each worker box owns its own provider credentials).
const SECRETS_FILENAME: &str = "worker-secrets.env";

/// Raw TOML shape (ALL fields with serde defaults so a partial file — including one the
/// wizard wrote before the operator reached the dispatch step — still parses).
/// Validated into `DesktopConfig` by `TomlConfig::into_config`.
#[derive(Debug, Default, Deserialize)]
struct TomlConfig {
    /// Empty is LEGAL here (see `into_config`): it means "not configured yet", which the
    /// wizard fixes. It used to be a hard error, which took the whole app down to a blank
    /// window on a fresh box — the silent-first-run failure this work exists to kill.
    #[serde(default)]
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
    /// to the sidecar as `AIZU_WORKER_PLATFORMS`; without a matching capability the box
    /// registers with `capabilities: []`, can never be leased to, and the preflight's
    /// `capabilities` check parks it (ledger F9.2). Default: `all`.
    #[serde(default = "default_worker_platforms")]
    worker_platforms: String,
    /// Set true by the wizard's Finish. While false the shell opens the setup wizard on
    /// launch. The step the wizard resumes at is still DERIVED from live config +
    /// preflight, never from a stored counter — this and `setup_deferred` are the only two
    /// persisted wizard bits, and neither is a step number.
    #[serde(default)]
    setup_complete: bool,
    /// Set true by "Close and fix later". Without it that button was a lie: it left
    /// `setup_complete` false, so the wizard re-opened over the dashboard on EVERY launch
    /// and an operator whose only red row was an unvalidated LinkedIn/X login classifier
    /// could never get past it (there is no Finish for them to press). Deferring is an
    /// acknowledgement, not a completion: the box still reports every failing check, the
    /// rail's Setup button still says "Finish setup", and Finish still clears this.
    #[serde(default)]
    setup_deferred: bool,
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
    /// EMPTY means "not configured" — the shell opens the wizard and starts nothing.
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
    /// Loopback control-surface port (default 8788).
    pub control_port: u16,
    /// Platforms this box advertises as capabilities (`AIZU_WORKER_PLATFORMS`):
    /// comma-separated or `all`. Empty/`all` → every supported platform, pool-wide.
    pub worker_platforms: String,
    /// The wizard has been finished at least once on this box.
    pub setup_complete: bool,
    /// The operator dismissed the wizard with "Close and fix later". Setup is NOT complete
    /// — this only stops the wizard re-opening on top of the dashboard every launch.
    pub setup_deferred: bool,
}

impl DesktopConfig {
    /// The CDP URL we hand the child as `AIZU_CDP_URL` and the Chrome manager binds.
    pub fn cdp_url(&self) -> String {
        format!("http://127.0.0.1:{}", self.cdp_port)
    }

    /// True once there is a dispatch to lease from. `main` starts NOTHING (no Chrome, no
    /// sidecar) while this is false, and the UI opens the wizard instead.
    pub fn is_configured(&self) -> bool {
        !self.dispatch_base_url.trim().is_empty()
    }

    /// The operator is done with the wizard for now — finished it, or explicitly deferred
    /// it. This (not `setup_complete` alone) is what makes the shell take over the Chrome
    /// launch on boot: a deferred box is a box being RUN, and leaving it browserless
    /// because one warn-severity login row is red would strand exactly the operator the
    /// deferral exists for.
    pub fn wizard_settled(&self) -> bool {
        self.setup_complete || self.setup_deferred
    }
}

/// The live config slot, shared by the shell, the supervisor and the Chrome manager.
///
/// A plain `std::sync::RwLock` around an `Arc`: every access is a clone-and-release, so a
/// guard is NEVER held across an `.await` (which would be a deadlock hazard in async
/// commands). Readers get a consistent snapshot; `set` swaps the whole struct at once.
#[derive(Debug)]
pub struct ConfigCell(RwLock<Arc<DesktopConfig>>);

impl ConfigCell {
    pub fn new(cfg: DesktopConfig) -> Self {
        Self(RwLock::new(Arc::new(cfg)))
    }
    /// Snapshot the current config. Cheap (one `Arc` clone); the lock is released before
    /// returning, so callers may hold the result across an await.
    pub fn get(&self) -> Arc<DesktopConfig> {
        match self.0.read() {
            Ok(g) => g.clone(),
            // A poisoned lock means another thread panicked mid-write. The value is still
            // a fully-formed Arc<DesktopConfig> (we only ever store complete values), so
            // recovering is strictly better than propagating a panic into every command.
            Err(poisoned) => poisoned.into_inner().clone(),
        }
    }
    pub fn set(&self, cfg: DesktopConfig) {
        let next = Arc::new(cfg);
        match self.0.write() {
            Ok(mut g) => *g = next,
            Err(poisoned) => *poisoned.into_inner() = next,
        }
    }
}

/// Shared handle to the live config. What `AppState`, the supervisor and the Chrome
/// manager all hold.
pub type SharedConfigCell = Arc<ConfigCell>;

/// A commented template dropped at `app_config_dir()/config.example.toml` on first run.
/// The wizard writes the real file — this exists for the operator who prefers a text editor
/// and for support ("show me your config").
const EXAMPLE_TOML: &str = r#"# AIZU Worker — the Setup wizard in the app writes config.toml for you.
# This example is here for reference / hand-editing. Secrets are NEVER put here: the
# enrolment token lives in dispatch-token.secret, and engine/provider keys such as
# OPENROUTER_API_KEY and AIZU_SECRET_KEY live in worker-secrets.env (see
# worker-secrets.example.env). This file is non-sensitive wiring only.

dispatch_base_url = "https://your-cloud-dispatch.example.com"  # the cloud you lease jobs from
cdp_port = 9333            # managed Chrome remote-debugging port (canonical: 9333)
control_port = 8788        # loopback control surface
chrome_profile_dir = ""    # a DEDICATED profile dir (NOT your default Chrome profile); "" = auto under app data
sidecar_binary_path = ""   # "" = use the aizu-worker bundled inside the app
state_dir = ""             # "" = auto under app data
db_path = ""               # "" = auto under app data
worker_platforms = "all"   # platforms this box can run: "all" or e.g. "instagram,x,linkedin"
setup_complete = false     # set true by the wizard's Finish; false reopens it on launch
setup_deferred = false     # set true by "Close and fix later": keeps setup unfinished but stops the wizard reopening
"#;

/// Load the config from `app_config_dir()/config.toml`.
///
/// On FIRST RUN (no file) this drops the example templates and returns defaults with an
/// EMPTY dispatch URL and `setup_complete = false` — the app opens straight into the setup
/// wizard rather than a dead dashboard. A malformed EXISTING file still errors (the shell
/// surfaces the message in the wizard instead of printing to a stderr no GUI user reads).
pub fn load(app: &AppHandle) -> Result<DesktopConfig, DesktopError> {
    let dir = app_config_dir(app)?;
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

/// Public first-run defaults, for the caller that must keep going after [`load`] failed.
///
/// A malformed `config.toml` must NOT take the app down to a blank window: the shell falls
/// back to these, records the parse error as a `startup_error`, and opens the wizard —
/// which is the only place the operator can repair the file from.
pub fn defaults(app: &AppHandle) -> DesktopConfig {
    default_config(app)
}

/// First-run defaults rooted under the OS app-data dir. Dispatch URL is empty on purpose —
/// `main` treats an empty dispatch as "not configured" and skips Chrome + sidecar startup,
/// and the UI opens the wizard.
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
        setup_complete: false,
        setup_deferred: false,
    }
}

impl TomlConfig {
    /// Boundary validation of an EXISTING file (never trust the file on disk), filling
    /// blank paths with app-data defaults so a minimal config.toml just works.
    ///
    /// An EMPTY `dispatch_base_url` is explicitly NOT an error (it is the pre-wizard
    /// state); a NON-empty one that is not http(s) still is, because that is a typo the
    /// operator must see rather than a state the wizard can resume from.
    fn into_config(self, app: &AppHandle) -> Result<DesktopConfig, DesktopError> {
        let url = self.dispatch_base_url.trim();
        if !url.is_empty() && !url.starts_with("http://") && !url.starts_with("https://") {
            return Err(DesktopError::ConfigInvalid(format!(
                "dispatch_base_url must be http(s): got {url:?}"
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
            dispatch_base_url: url.trim_end_matches('/').to_string(),
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
            setup_complete: self.setup_complete,
            setup_deferred: self.setup_deferred,
        })
    }
}

fn app_config_dir(app: &AppHandle) -> Result<PathBuf, DesktopError> {
    let dir = app
        .path()
        .app_config_dir()
        .map_err(|e| DesktopError::ConfigInvalid(format!("no app_config_dir: {e}")))?;
    std::fs::create_dir_all(&dir)
        .map_err(|e| DesktopError::ConfigInvalid(format!("cannot create config dir: {e}")))?;
    Ok(dir)
}

/// A partial update from the wizard. Every field is optional and `None` means "leave
/// whatever is on disk alone" — the wizard writes one concern per step and MUST NOT clobber
/// the others.
#[derive(Debug, Default)]
pub struct ConfigPatch {
    pub dispatch_base_url: Option<String>,
    pub control_port: Option<u16>,
    pub cdp_port: Option<u16>,
    pub worker_platforms: Option<String>,
    pub setup_complete: Option<bool>,
    pub setup_deferred: Option<bool>,
}

/// Read-modify-write `config.toml`, apply `patch`, and return the reloaded config.
///
/// This REPLACES the old `write_config`, which rewrote a three-line minimal file and thereby
/// silently dropped `worker_platforms`, `chrome_profile_dir`, `state_dir`, `db_path` and any
/// non-default `cdp_port` every time the dev menu saved. The whole file is now round-tripped:
/// unknown-to-us keys are the only thing lost, and there are none (the struct is total).
pub fn update_config(app: &AppHandle, patch: ConfigPatch) -> Result<DesktopConfig, DesktopError> {
    let dir = app_config_dir(app)?;
    let path = dir.join(CONFIG_FILENAME);

    // Start from what is on disk (or the app-data defaults on a first run).
    let mut raw: TomlConfig = match std::fs::read_to_string(&path) {
        Ok(text) => toml::from_str(&text)
            .map_err(|e| DesktopError::ConfigInvalid(format!("TOML parse error: {e}")))?,
        Err(_) => {
            let d = default_config(app);
            TomlConfig {
                dispatch_base_url: d.dispatch_base_url,
                cdp_port: d.cdp_port,
                chrome_profile_dir: String::new(),
                sidecar_binary_path: String::new(),
                state_dir: String::new(),
                db_path: String::new(),
                control_port: d.control_port,
                worker_platforms: d.worker_platforms,
                setup_complete: false,
                setup_deferred: false,
            }
        }
    };

    if let Some(url) = patch.dispatch_base_url {
        let url = url.trim().to_string();
        if !url.is_empty() && !url.starts_with("http://") && !url.starts_with("https://") {
            return Err(DesktopError::ConfigInvalid(
                "That is not an http(s) URL.".into(),
            ));
        }
        raw.dispatch_base_url = url;
    }
    if let Some(p) = patch.control_port {
        if p == 0 {
            return Err(DesktopError::ConfigInvalid("control port must be non-zero".into()));
        }
        raw.control_port = p;
    }
    if let Some(p) = patch.cdp_port {
        if p == 0 {
            return Err(DesktopError::ConfigInvalid("CDP port must be non-zero".into()));
        }
        raw.cdp_port = p;
    }
    if let Some(plats) = patch.worker_platforms {
        let plats = plats.trim().to_string();
        if plats.is_empty() {
            // F9.2 made unreachable by construction: a box that advertises nothing can
            // never be sent a job, so we refuse to persist "nothing".
            return Err(DesktopError::ConfigInvalid(
                "Pick at least one platform. A box that advertises nothing can never be sent a job."
                    .into(),
            ));
        }
        raw.worker_platforms = plats;
    }
    if let Some(done) = patch.setup_complete {
        raw.setup_complete = done;
    }
    if let Some(deferred) = patch.setup_deferred {
        raw.setup_deferred = deferred;
    }

    std::fs::write(&path, render_toml(&raw))
        .map_err(|e| DesktopError::ConfigInvalid(format!("cannot write config.toml: {e}")))?;
    raw.into_config(app)
}

/// Serialize the whole config back to TOML.
///
/// Hand-rolled rather than `toml::to_string` so the file keeps its comments and field order
/// (an operator reads and hand-edits this file). Paths go through [`toml_str`], which
/// prefers a LITERAL string so a Windows `C:\Users\…` path is not mangled by backslash
/// escaping — the exact bug a naive `format!("{:?}")` would ship.
fn render_toml(raw: &TomlConfig) -> String {
    format!(
        "# AIZU Worker — written by the in-app Setup wizard. Secrets are NOT stored here\n\
         # (see dispatch-token.secret and worker-secrets.env alongside this file).\n\
         dispatch_base_url = {}\n\
         cdp_port = {}\n\
         control_port = {}\n\
         worker_platforms = {}\n\
         chrome_profile_dir = {}\n\
         sidecar_binary_path = {}\n\
         state_dir = {}\n\
         db_path = {}\n\
         setup_complete = {}\n\
         setup_deferred = {}\n",
        toml_str(&raw.dispatch_base_url),
        raw.cdp_port,
        raw.control_port,
        toml_str(&raw.worker_platforms),
        toml_str(&raw.chrome_profile_dir),
        toml_str(&raw.sidecar_binary_path),
        toml_str(&raw.state_dir),
        toml_str(&raw.db_path),
        raw.setup_complete,
        raw.setup_deferred,
    )
}

/// Quote a value as TOML. Prefers a single-quoted LITERAL string (no escape processing —
/// correct for Windows paths); falls back to a basic string with `\` and `"` escaped when
/// the value itself contains a single quote. Control characters are stripped rather than
/// escaped: none of these fields legitimately contains one, and a stray newline would
/// otherwise let a value break out of its own assignment.
fn toml_str(value: &str) -> String {
    let clean: String = value.chars().filter(|c| !c.is_control()).collect();
    if !clean.contains('\'') {
        return format!("'{clean}'");
    }
    let escaped = clean.replace('\\', "\\\\").replace('"', "\\\"");
    format!("\"{escaped}\"")
}

/// Persist the worker bootstrap/enrolment token to the 0600 sibling file. An empty token
/// clears it. Never logged, never returned to the UI.
pub fn write_bootstrap_token(app: &AppHandle, token: &str) -> Result<(), DesktopError> {
    let path = app_config_dir(app)?.join(TOKEN_FILENAME);
    if token.trim().is_empty() {
        let _ = std::fs::remove_file(&path);
        return Ok(());
    }
    std::fs::write(&path, token.trim())
        .map_err(|e| DesktopError::ConfigInvalid(format!("cannot write token file: {e}")))?;
    restrict_permissions(&path);
    Ok(())
}

/// Read the enrolment token Setup → Connect stored, if any. Never logged.
pub fn read_bootstrap_token(app: &AppHandle) -> Option<String> {
    let dir = app.path().app_config_dir().ok()?;
    let text = std::fs::read_to_string(dir.join(TOKEN_FILENAME)).ok()?;
    let trimmed = text.trim();
    if trimmed.is_empty() { None } else { Some(trimmed.to_string()) }
}

/// A commented template dropped at `app_config_dir()/worker-secrets.example.env` on first
/// run so the operator knows which engine secrets to supply. NEVER contains real values.
const SECRETS_EXAMPLE: &str = r#"# AIZU Worker — engine/provider secrets for the sidecar's live runs.
# The Setup wizard (Credentials step) writes worker-secrets.env for you; this example is
# for reference / hand-editing. Format: one KEY=VALUE per line; # comments and blank lines
# are ignored. Read at sidecar-spawn and injected into the engine's environment. NEVER
# shipped over the wire — each worker box owns its own credentials.
#
# REQUIRED for any live (non-warming) run. Without it every job fails at run setup and
# dead-letters at attempt 5, and the worker's preflight parks the box (ledger F9.3):
# OPENROUTER_API_KEY=sk-or-...
#
# REQUIRED so the box can PERSIST its worker identity (ledger F9.1). Without it the sidecar
# crashes AFTER the server minted its token, the fleet row reads "online" while the process
# is dead, and a crash-loop rotates the token every iteration. Generate one in
# Setup -> Credentials:
# AIZU_SECRET_KEY=...
#
# Optional: point the engine at a local model instead of OpenRouter.
# AIZU_LLM_BASE_URL=http://127.0.0.1:11434/v1
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
/// vec when the file is absent or unreadable — the sidecar's preflight then names the
/// missing key in the UI rather than the box failing silently at job time.
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

/// The NAMES of the secrets currently stored — the only thing about them that may reach the
/// UI. Lets Setup → Credentials render "OPENROUTER_API_KEY · stored" without the value ever
/// leaving this process (the inputs themselves are write-only).
pub fn read_worker_secret_names(app: &AppHandle) -> Vec<String> {
    read_worker_secrets(app)
        .into_iter()
        .filter(|(_, v)| !v.is_empty())
        .map(|(k, _)| k)
        .collect()
}

/// Upsert secrets into the 0600 `worker-secrets.env`, PRESERVING every other key and every
/// comment already in the file.
///
/// Read-modify-write is load-bearing: the wizard writes `OPENROUTER_API_KEY` and
/// `AIZU_SECRET_KEY` one step at a time, and a whole-file rewrite would wipe an operator's
/// hand-added `TELEGRAM_*` / `YOUTUBE_*` lines. An empty value DELETES that key.
pub fn write_worker_secrets(
    app: &AppHandle,
    upserts: &[(String, String)],
) -> Result<(), DesktopError> {
    if upserts.is_empty() {
        return Ok(());
    }
    for (key, _) in upserts {
        if !is_env_key(key) {
            return Err(DesktopError::ConfigInvalid(format!(
                "{key:?} is not a valid environment variable name"
            )));
        }
    }
    let path = app_config_dir(app)?.join(SECRETS_FILENAME);
    let existing = std::fs::read_to_string(&path).unwrap_or_default();
    let merged = merge_secret_lines(&existing, upserts);
    std::fs::write(&path, merged)
        .map_err(|e| DesktopError::ConfigInvalid(format!("cannot write worker-secrets.env: {e}")))?;
    restrict_permissions(&path);
    Ok(())
}

/// 0600 on unix. A no-op elsewhere (Windows ACL inheritance from the per-user app-config
/// dir is the practical equivalent; a real DACL edit needs the `windows` crate).
fn restrict_permissions(path: &std::path::Path) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
    #[cfg(not(unix))]
    {
        let _ = path;
    }
}

/// `A-Z a-z 0-9 _` and not starting with a digit — the shape the supervisor can safely pass
/// to `Command::env`. Rejecting here keeps a pasted "OPENROUTER_API_KEY=sk-..." (key AND
/// value in the key box) from being written as a bogus variable.
fn is_env_key(key: &str) -> bool {
    !key.is_empty()
        && !key.starts_with(|c: char| c.is_ascii_digit())
        && key.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
}

/// Pure line-merge for `worker-secrets.env` (extracted so it is unit-testable without an
/// `AppHandle`): every existing line is kept in place, a line whose key is being upserted is
/// REPLACED in place, keys not already present are appended, and an upsert with an empty
/// value DROPS the line.
fn merge_secret_lines(existing: &str, upserts: &[(String, String)]) -> String {
    let mut out: Vec<String> = Vec::new();
    let mut applied: Vec<&str> = Vec::new();

    for line in existing.lines() {
        let trimmed = line.trim();
        let key = if trimmed.is_empty() || trimmed.starts_with('#') {
            None
        } else {
            trimmed.split_once('=').map(|(k, _)| k.trim())
        };
        match key.and_then(|k| upserts.iter().find(|(uk, _)| uk == k)) {
            Some((k, v)) => {
                applied.push(k.as_str());
                if !v.is_empty() {
                    out.push(format!("{k}={v}"));
                }
                // empty value → line dropped (that is how the UI clears a secret)
            }
            None => out.push(line.to_string()),
        }
    }
    for (k, v) in upserts {
        if applied.iter().any(|a| a == k) || v.is_empty() {
            continue;
        }
        out.push(format!("{k}={v}"));
    }
    let mut body = out.join("\n");
    if !body.ends_with('\n') {
        body.push('\n');
    }
    body
}

/// Pure parser for the `worker-secrets.env` body. See [`read_worker_secrets`] for the
/// tolerance contract.
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

/// Generate a Fernet key for `AIZU_SECRET_KEY`: 32 random bytes, urlsafe-base64.
///
/// Byte-identical in shape to `python -c "from aizu.secrets import SecretCipher;
/// print(SecretCipher.generate_key())"`, which is what the operator would otherwise have to
/// run in a terminal they do not have. The encoder is hand-rolled (20 lines below) rather
/// than pulling in a `base64` crate — the desktop shell adds no dependency for this.
pub fn generate_secret_key() -> String {
    use rand::RngCore;
    let mut bytes = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut bytes);
    encode_urlsafe_b64(&bytes)
}

/// RFC 4648 §5 urlsafe base64 WITH padding (`-`/`_` alphabet) — the exact encoding
/// `base64.urlsafe_b64encode` produces, which is what Python's Fernet requires.
fn encode_urlsafe_b64(input: &[u8]) -> String {
    const ALPHABET: &[u8; 64] =
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::with_capacity((input.len() + 2) / 3 * 4);
    for chunk in input.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(ALPHABET[((n >> 18) & 63) as usize] as char);
        out.push(ALPHABET[((n >> 12) & 63) as usize] as char);
        out.push(if chunk.len() > 1 { ALPHABET[((n >> 6) & 63) as usize] as char } else { '=' });
        out.push(if chunk.len() > 2 { ALPHABET[(n & 63) as usize] as char } else { '=' });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

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

    /// The whole point of read-modify-write: writing ONE key must not wipe the others.
    #[test]
    fn merge_replaces_in_place_and_preserves_everything_else() {
        let existing = "# header\nTELEGRAM_API_ID=123\nOPENROUTER_API_KEY=old\n\n# tail\n";
        let merged = merge_secret_lines(
            existing,
            &[("OPENROUTER_API_KEY".into(), "new".into())],
        );
        assert!(merged.contains("# header"));
        assert!(merged.contains("TELEGRAM_API_ID=123"));
        assert!(merged.contains("OPENROUTER_API_KEY=new"));
        assert!(!merged.contains("OPENROUTER_API_KEY=old"));
        assert!(merged.contains("# tail"));
    }

    #[test]
    fn merge_appends_new_keys_and_drops_emptied_ones() {
        let merged = merge_secret_lines(
            "A=1\nB=2\n",
            &[("B".into(), "".into()), ("C".into(), "3".into())],
        );
        assert_eq!(merged, "A=1\nC=3\n");
    }

    #[test]
    fn merge_on_an_empty_file_writes_just_the_upserts() {
        assert_eq!(merge_secret_lines("", &[("A".into(), "1".into())]), "A=1\n");
    }

    #[test]
    fn env_key_validation_rejects_pasted_key_equals_value() {
        assert!(is_env_key("OPENROUTER_API_KEY"));
        assert!(!is_env_key("OPENROUTER_API_KEY=sk-or-x"));
        assert!(!is_env_key("2FA"));
        assert!(!is_env_key(""));
    }

    /// A Windows path must survive the round trip unmangled — the reason paths are written
    /// as TOML literal strings.
    #[test]
    fn toml_str_uses_literal_strings_for_backslash_paths() {
        assert_eq!(toml_str(r"C:\Users\op\aizu"), r"'C:\Users\op\aizu'");
        assert_eq!(toml_str(""), "''");
        // a value containing a single quote falls back to an escaped basic string
        assert_eq!(toml_str(r"it's\here"), "\"it's\\\\here\"");
        // control characters cannot break out of the assignment
        assert_eq!(toml_str("a\nb = 1"), "'ab = 1'");
    }

    #[test]
    fn rendered_toml_reparses_into_the_same_values() {
        let raw = TomlConfig {
            dispatch_base_url: "https://cloud.example.com".into(),
            cdp_port: 9333,
            chrome_profile_dir: r"C:\aizu\profile".into(),
            sidecar_binary_path: String::new(),
            state_dir: "/var/aizu/state".into(),
            db_path: "/var/aizu/aizu.db".into(),
            control_port: 8788,
            worker_platforms: "instagram,x".into(),
            setup_complete: true,
            setup_deferred: false,
        };
        let back: TomlConfig = toml::from_str(&render_toml(&raw)).expect("re-parse");
        assert_eq!(back.dispatch_base_url, raw.dispatch_base_url);
        assert_eq!(back.cdp_port, 9333);
        assert_eq!(back.chrome_profile_dir, r"C:\aizu\profile");
        assert_eq!(back.worker_platforms, "instagram,x");
        assert!(back.setup_complete);
    }

    /// "Close and fix later" is worthless if the flag it sets does not survive the next
    /// `update_config` — the wizard would re-open on the very next launch, which is the
    /// trap this exists to remove. Round-tripping it through the renderer is the half of
    /// that guarantee testable without an AppHandle.
    #[test]
    fn deferral_survives_the_toml_round_trip() {
        let raw = TomlConfig {
            dispatch_base_url: "https://cloud.example.com".into(),
            cdp_port: 9333,
            chrome_profile_dir: String::new(),
            sidecar_binary_path: String::new(),
            state_dir: String::new(),
            db_path: String::new(),
            control_port: 8788,
            worker_platforms: "all".into(),
            setup_complete: false,
            setup_deferred: true,
        };
        let rendered = render_toml(&raw);
        assert!(rendered.contains("setup_deferred = true"), "{rendered}");
        let back: TomlConfig = toml::from_str(&rendered).expect("re-parse");
        assert!(back.setup_deferred);
        assert!(!back.setup_complete);
    }

    /// A config file written before this field existed must still parse (serde default),
    /// and an old box must NOT read as deferred.
    #[test]
    fn a_pre_deferral_config_file_still_parses_as_not_deferred() {
        let back: TomlConfig = toml::from_str(
            "dispatch_base_url = 'https://c.example.com'\nsetup_complete = true\n",
        )
        .expect("re-parse");
        assert!(back.setup_complete);
        assert!(!back.setup_deferred);
    }

    #[test]
    fn urlsafe_b64_matches_known_vectors_and_fernet_key_shape() {
        assert_eq!(encode_urlsafe_b64(b""), "");
        assert_eq!(encode_urlsafe_b64(b"f"), "Zg==");
        assert_eq!(encode_urlsafe_b64(b"fo"), "Zm8=");
        assert_eq!(encode_urlsafe_b64(b"foo"), "Zm9v");
        assert_eq!(encode_urlsafe_b64(b"foob"), "Zm9vYg==");
        // the two urlsafe substitutions: 0xFB,0xFF -> '-' and '_'
        assert_eq!(encode_urlsafe_b64(&[0xfb, 0xff, 0xbf]), "-_-_");
        // a generated key is exactly what Fernet wants: 44 chars, single trailing pad
        let key = generate_secret_key();
        assert_eq!(key.len(), 44);
        assert!(key.ends_with('='));
        assert!(!key.ends_with("=="));
    }
}
