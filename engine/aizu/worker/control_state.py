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

from ..core.config import CDP_PLATFORMS

# The operator intents the desktop shell can POST. The first four are zero-argument;
# ``openLoginTab`` is the one that carries a `platform` (the setup wizard's Sign-in step).
VALID_COMMANDS = ("pause", "resume", "stopCurrentJob", "focusWarmedChrome",
                  "runPreflight", "openLoginTab")
# Cap an optional audit reason so a client can't push an unbounded string.
_MAX_REASON_LEN = 500
# Sorted for a STABLE error message (CDP_PLATFORMS is a frozenset, whose repr order is
# not guaranteed — an operator-facing string must not shuffle between runs).
_CDP_PLATFORM_NAMES = tuple(sorted(CDP_PLATFORMS))


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
    # Ledger B10: the dispatch rejected this box's bearer token (401), so the persisted
    # token was cleared and the pull loop stopped. Terminal until an operator re-enrols
    # the box — surfaced here so the desktop app can SAY that instead of showing an idle
    # worker that silently never leases. Defaults False → an untouched snapshot is
    # unchanged for every existing caller.
    reenrolment_required: bool = False
    # The launch preflight's last report, ALREADY SERIALIZED by the caller
    # (``PreflightReport.to_wire()``). Kept as a plain dict on purpose: this module is the
    # pure read model and must stay import-free of ``preflight.py`` (which pulls in
    # readiness/playwright seams), so the DTO shape lives in one place and the dependency
    # stays one-way. ``None`` means "the first run has not finished yet" — both UIs render
    # that as 'checking…', NEVER as healthy (a box whose preflight is still running has
    # not been cleared of anything).
    preflight: Optional[dict] = None
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
    # Both of these are 'accepted', never 'completed': each takes seconds (a CDP probe,
    # a Playwright attach) — far longer than the desktop client's 3s HTTP timeout — so
    # they run detached and the operator's feedback is the NEXT status poll, not this
    # response. See control_surface._dispatch.
    def run_preflight(self) -> bool: ...
    def open_login_tab(self, platform: str) -> bool: ...


def validate_command(payload: Any) -> tuple:
    """Boundary validation (mirrors server.py's _validate_* shape). Returns
    ``(clean_dict, None)`` on success or ``(None, error_message)`` — never raises, never
    trusts the caller.

    ``platform`` is optional in general (the zero-argument commands ignore it) but
    REQUIRED for ``openLoginTab``, and is whitelisted against ``CDP_PLATFORMS`` — the
    value is handed to a browser-driving seam, so an unvalidated string is the one field
    here with any reach at all."""
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
    platform = payload.get("platform")
    if platform is not None and not isinstance(platform, str):
        return (None, "platform must be a string")
    if action == "openLoginTab":
        if not platform:
            return (None, "openLoginTab requires a platform")
        if platform not in CDP_PLATFORMS:
            return (None, f"platform must be one of {_CDP_PLATFORM_NAMES}")
    return ({"action": action, "reason": reason, "platform": platform}, None)


def status_to_wire(snap: StatusSnapshot) -> dict:
    """Serialize a snapshot to the camelCase wire DTO. The SINGLE leak-safety choke
    point — emits only identifiers/status enums/timestamps, never a secret. Confirmed
    safe against the SECURITY REVIEW CRITICAL/HIGH credential-fetch change too:
    CurrentJobInfo (the source of currentJob below) has no platform_credentials field
    at all, so there is nothing here for a fetched job credential to leak through even
    transitively — the control-surface status feed and JobSpec are unrelated shapes."""
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
            "reenrolmentRequired": snap.reenrolment_required,
        },
        "chrome": _chrome_to_wire(snap.chrome),
        # Already-serialized preflight report (see StatusSnapshot.preflight). Leak-safe by
        # construction: every `detail` names a VARIABLE, a path or a URL — never a secret
        # VALUE (preflight rule 8), and this choke point adds nothing of its own.
        "preflight": snap.preflight,
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
