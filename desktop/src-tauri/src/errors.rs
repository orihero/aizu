//! Error types for the AIZU Worker desktop shell (BUILD-PLAN Phase 6).
//!
//! One `thiserror` enum for the whole shell. Every fallible boundary — Chrome lifecycle,
//! sidecar spawn, config load, control-surface HTTP, log read — maps to a variant so the
//! UI can render a specific, actionable message instead of a generic failure.
//!
//! Leak-safety: variant messages carry human context (which port, which path) but NEVER a
//! secret (the control token, provider API keys). `commands.rs` further maps these to
//! plain `String` for the UI, stripping anything path-sensitive.

use thiserror::Error;

#[derive(Debug, Error)]
pub enum DesktopError {
    /// A Chrome is listening on the CDP port but rejects `connect_over_cdp` (a degraded /
    /// incompatible Chrome), OR no Chrome could be attached and launch failed. We do NOT
    /// kill a Chrome we did not launch — the operator must quit the stale one and retry.
    #[error("Chrome CDP attach failed: {0}")]
    ChromeAttachFailed(String),

    /// The `aizu-worker` sidecar binary could not be spawned (missing binary,
    /// permission denied, bad path).
    #[error("sidecar spawn failed: {0}")]
    SidecarSpawnFailed(String),

    /// The on-disk config TOML is missing, unparseable, or fails validation
    /// (e.g. empty dispatch_base_url, non-loopback control host, out-of-range port).
    #[error("desktop config invalid: {0}")]
    ConfigInvalid(String),

    /// The loopback control surface (127.0.0.1:<control_port>) could not be reached —
    /// the sidecar is starting, crashed, or the control surface is not enabled.
    #[error("control surface unreachable: {0}")]
    ControlSurfaceUnreachable(String),

    /// The per-run log file could not be opened/read for tailing (not yet created,
    /// rotated away, or a permissions problem).
    #[error("log read failed: {0}")]
    LogReadFailed(String),
}

/// Convenience alias for shell-internal fallible calls.
pub type Result<T> = std::result::Result<T, DesktopError>;
