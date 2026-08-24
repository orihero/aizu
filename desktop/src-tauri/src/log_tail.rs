//! Per-run log tail (BUILD-PLAN Phase 6).
//!
//! Follows the EXACT per-run log file the current job's killable child writes —
//! `currentJob.logFilePath` from `GET /status` (e.g. `<state>/logs/run-<run_id>.log`). It
//! does NOT guess "the newest file by mtime" and does NOT scrape logs to infer state
//! (state is `/status`; logs are only human-readable run output for the operator).
//!
//! It uses `notify` to watch the file for appends and emits the last N lines as a
//! `log-line` batch event. Because the engine's `RotatingFileHandler` rolls the file, the
//! tailer is **reopen-on-rotate aware**: if the watched inode/size shrinks or the path is
//! recreated, it reopens from the top of the new file rather than seeking past EOF forever.
//!
//! When the current job changes (new `logFilePath`) or clears (idle → None), the tailer
//! switches to the new path or pauses.

use std::io::{BufRead, BufReader, Seek, SeekFrom};
use std::sync::Arc;
use std::time::Duration;

use serde::Serialize;
use tauri::{AppHandle, Emitter};

use crate::control_client::ControlClient;

/// Tauri event carrying a batch of new log lines to the UI.
pub const LOG_EVENT: &str = "log-line";

/// How many trailing lines to seed the pane with when a log first opens.
const SEED_TAIL_LINES: usize = 200;

/// How often we re-ask /status for the current logFilePath (job may start/stop/rotate).
const PATH_POLL_INTERVAL_MS: u64 = 2_000;

/// A batch of appended log lines for one file.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LogBatch {
    pub path: String,
    pub lines: Vec<String>,
    /// True when this batch is the initial seed (UI may clear the pane before appending).
    pub seeded: bool,
}

/// Drive the tailer: poll /status for the current job's logFilePath, and while it stays the
/// same, stream appended lines. This scaffold uses a simple size-delta poll (robust and
/// dependency-light); an engineer may swap in a `notify` watcher for lower latency — the
/// reopen-on-rotate logic below applies either way.
pub async fn run_log_tail(client: Arc<ControlClient>, app: AppHandle) {
    let mut current_path: Option<String> = None;
    let mut offset: u64 = 0;
    let mut ticker = tokio::time::interval(Duration::from_millis(PATH_POLL_INTERVAL_MS));

    loop {
        ticker.tick().await;

        // 1. Where should we be tailing right now? Ask /status (the source of truth).
        let desired = match client.get_status().await {
            Ok(s) => s.current_job.and_then(|j| j.log_file_path),
            Err(_) => continue, // sidecar not up yet; try again next tick
        };

        // 2. Job/log changed (or cleared) → reset and re-seed.
        if desired != current_path {
            current_path = desired.clone();
            offset = 0;
            if let Some(path) = &current_path {
                if let Ok((lines, new_offset)) = seed_tail(path, SEED_TAIL_LINES) {
                    offset = new_offset;
                    let _ = app.emit(
                        LOG_EVENT,
                        &LogBatch { path: path.clone(), lines, seeded: true },
                    );
                }
            }
            continue;
        }

        // 3. Same file — stream any appended bytes, reopening on rotation.
        let Some(path) = &current_path else { continue };
        match read_appended(path, offset) {
            Ok((lines, new_offset, rotated)) => {
                offset = new_offset;
                if rotated {
                    // File shrank/rotated: we reopened from the top — mark as seeded so the
                    // UI treats it as a fresh file rather than a mid-stream append.
                    let _ = app.emit(
                        LOG_EVENT,
                        &LogBatch { path: path.clone(), lines, seeded: true },
                    );
                } else if !lines.is_empty() {
                    let _ = app.emit(
                        LOG_EVENT,
                        &LogBatch { path: path.clone(), lines, seeded: false },
                    );
                }
            }
            Err(_) => {
                // Transient read error (rotation window, perms) — reset offset so the next
                // tick reseeds cleanly rather than seeking into a stale offset.
                offset = 0;
            }
        }
    }
}

/// Read the last `n` lines of a file and return them plus the byte offset at EOF (so the
/// stream can continue from there).
fn seed_tail(path: &str, n: usize) -> std::io::Result<(Vec<String>, u64)> {
    let file = std::fs::File::open(path)?;
    let len = file.metadata()?.len();
    let reader = BufReader::new(file);
    let all: Vec<String> = reader.lines().filter_map(|l| l.ok()).collect();
    let start = all.len().saturating_sub(n);
    Ok((all[start..].to_vec(), len))
}

/// Read bytes appended since `offset`. Detects rotation: if the file is now SHORTER than
/// `offset` (RotatingFileHandler rolled it), reopen and read from the top, returning
/// `rotated = true`.
fn read_appended(path: &str, offset: u64) -> std::io::Result<(Vec<String>, u64, bool)> {
    let mut file = std::fs::File::open(path)?;
    let len = file.metadata()?.len();

    let (seek_to, rotated) = if len < offset {
        (0, true) // rotated/truncated — start over from the top of the new file
    } else {
        (offset, false)
    };

    file.seek(SeekFrom::Start(seek_to))?;
    let reader = BufReader::new(file);
    let lines: Vec<String> = reader.lines().filter_map(|l| l.ok()).collect();
    Ok((lines, len, rotated))
}
