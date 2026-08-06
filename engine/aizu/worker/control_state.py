"""Read model + command validation for the worker's local control surface (Phase 6, D3).

Pure data + boundary validation — NO http, NO threading. Kept separate from the transport
(``control_surface.py``) so the DTO shape and leak-safety live in one auditable place, and
so ``sidecar.py`` can build a snapshot without importing http.server.

Leak-safety contract: the wire DTO NEVER carries a secret — no worker token, no lead
content, no soul_text. ``status_to_wire`` is the single choke point a reviewer audits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

# The four zero-argument operator intents the desktop shell can POST.
VALID_COMMANDS = ("pause", "resume", "stopCurrentJob", "focusWarmedChrome")
# Cap an optional audit reason so a client can't push an unbounded string.
_MAX_REASON_LEN = 500


@dataclass(frozen=True)
class AccountHealth:
    org_id: Optional[int]
    platform: str
    account_handle: Optional[str]
    status: str  # 'idle' | 'busy'


@dataclass(frozen=True)
class CurrentJobInfo:
    job_id: str
    campaign_id: str
    platform: str
    status: str
    run_id: Optional[str]
    log_file_path: Optional[str]


@dataclass(frozen=True)
class ChromeStatusView:
    connected: bool
    cdp_url: str
    browser_version: Optional[str] = None


@dataclass(frozen=True)
class StatusSnapshot:
    worker_id: Optional[str]
    accounts: tuple = ()
    current_job: Optional[CurrentJobInfo] = None
    drain: bool = False
    halt: bool = False
    halt_reason: Optional[str] = None
    update_required: bool = False
    chrome: Optional[ChromeStatusView] = None
    paused: bool = False
    generated_at: float = 0.0


class ControlSurfaceSource(Protocol):
    """What the HTTP handler depends on — never on Sidecar directly, so tests inject a
    trivial fake. The mutating commands return bool = 'accepted' (a job existed / Chrome
    was reachable), not 'completed' (both are eventual)."""

    def get_status(self) -> StatusSnapshot: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def stop_current_job(self) -> bool: ...
    def focus_warmed_chrome(self) -> bool: ...


def validate_command(payload: Any) -> tuple:
    """Boundary validation (mirrors server.py's _validate_* shape). Returns
    ``(clean_dict, None)`` on success or ``(None, error_message)`` — never raises, never
    trusts the caller."""
    if not isinstance(payload, dict):
        return (None, "body must be a JSON object")
    action = payload.get("action")
    if not isinstance(action, str) or action not in VALID_COMMANDS:
        return (None, f"action must be one of {VALID_COMMANDS}")
    reason = payload.get("reason")
    if reason is not None:
        if not isinstance(reason, str):
            return (None, "reason must be a string")
        reason = reason[:_MAX_REASON_LEN]
    return ({"action": action, "reason": reason}, None)


def status_to_wire(snap: StatusSnapshot) -> dict:
    """Serialize a snapshot to the camelCase wire DTO. The SINGLE leak-safety choke
    point — emits only identifiers/status enums/timestamps, never a secret."""
    return {
        "workerId": snap.worker_id,
        "accounts": [
            {"orgId": a.org_id, "platform": a.platform,
             "accountHandle": a.account_handle, "status": a.status}
            for a in snap.accounts
        ],
        "currentJob": _job_to_wire(snap.current_job),
        "controls": {
            "drain": snap.drain, "halt": snap.halt, "haltReason": snap.halt_reason,
            "updateRequired": snap.update_required, "paused": snap.paused,
        },
        "chrome": _chrome_to_wire(snap.chrome),
        "generatedAt": snap.generated_at,
    }


def _job_to_wire(job: Optional[CurrentJobInfo]) -> Optional[dict]:
    if job is None:
        return None
    return {"jobId": job.job_id, "campaignId": job.campaign_id, "platform": job.platform,
            "status": job.status, "runId": job.run_id, "logFilePath": job.log_file_path}


def _chrome_to_wire(chrome: Optional[ChromeStatusView]) -> Optional[dict]:
    if chrome is None:
        return None
    return {"connected": chrome.connected, "cdpUrl": chrome.cdp_url,
            "browserVersion": chrome.browser_version}
