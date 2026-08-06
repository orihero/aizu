"""Worker configuration and the leased-job spec (BUILD-PLAN Phase 1).

Two immutable value types:
  - ``WorkerConfig`` — env-driven, frozen; how this box reaches dispatch and runs jobs.
  - ``JobSpec``      — one leased job, validated at the lease boundary (never trust the
                       dispatch payload: a malformed job is rejected, not run).

``WorkerConfig.run_args`` builds a fresh ``argparse.Namespace`` per job so the worker
reuses ``cli._run_session_loop`` verbatim (the same surface a CLI ``aizu run``
drives) without ever mutating shared state.
"""
from __future__ import annotations

import argparse
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..core.config import SUPPORTED_PLATFORMS
from . import DEFAULT_CONTROL_SURFACE_PORT, DEFAULT_HEARTBEAT_INTERVAL_SEC

# A job whose duration is unbounded by the spec still gets a worst-case wall-clock
# brake so a stuck session can't hold a lease forever (BUILD-PLAN risk #9). The
# operator/dispatch overrides per platform later; this is the safety floor.
DEFAULT_MAX_JOB_MINUTES = 90

# No-progress watchdog: the supervisor kills a child that has written NOTHING to its
# job log for this long. A healthy run emits a scan-progress heartbeat every ~45s
# (PROGRESS_EVENT_INTERVAL_SEC) plus per-reel relevance/CDP lines, so 300s is far above
# any legitimate quiet stretch (slow LLM call, dwell) yet catches a wedged CDP call that
# the engine's own cooperative per-reel deadline cannot preempt (a blocked sync-Playwright
# evaluate never yields to the loop that checks the deadline). Root-caused live 2026-07-06:
# a reel opened from a swipeable /reels/ surface hung open_reel for 32 min.
DEFAULT_STALL_TIMEOUT_SEC = 300.0

# Engine modes the worker will run. 'warming' is gated by the kill-switch inside the
# engine; 'harvest' is the default read-only lead discovery.
_VALID_ENGINE_MODES = ("harvest", "warming")


def _coerce_org_id(value: Any) -> Optional[int]:
    """Coerce a capability's org field to int-or-None. Blank/None/'*'/'null' → None
    (pool-wide). NEVER raises — a garbage value falls back to pool-wide."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in ("", "*", "null", "none", "any"):
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _coerce_handle(value: Any) -> Optional[str]:
    """Coerce a capability's account-handle field to str-or-None (blank/'*' → None =
    unpinned, serves any account for that platform)."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None if s not in ("*", "null", "none", "any") else None


def _parse_capabilities_env() -> tuple:
    """Build the worker's declared capabilities from the environment. A capability is
    ``[org_id, platform, account_handle]`` (org/handle None = pool-wide / unpinned);
    the dispatch/lease matcher requires the platform to match exactly. Two env forms,
    in priority order, both TOLERANT (a malformed entry is dropped, never raises — a
    bad config must not stop the worker from registering):

      1. ``AIZU_WORKER_CAPABILITIES`` — JSON array of ``[org, platform, handle]``
         (explicit; org/handle may be null). Entries whose platform is unsupported are
         dropped.
      2. ``AIZU_WORKER_PLATFORMS`` — comma-separated platform list, or ``all``/``*``
         → every supported platform. Each becomes a pool-wide ``[null, platform, null]``.

    Neither set → ``()`` (the box advertises nothing and can't be dispatched to — the
    prior default, preserved so a bare/headless sidecar is unchanged)."""
    raw = os.environ.get("AIZU_WORKER_CAPABILITIES")
    if raw:
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            decoded = None
        caps = []
        for entry in decoded or []:
            if not isinstance(entry, (list, tuple)) or len(entry) != 3:
                continue
            org, platform, handle = entry
            platform = str(platform or "").strip().lower()
            if platform not in SUPPORTED_PLATFORMS:
                continue
            caps.append([_coerce_org_id(org), platform, _coerce_handle(handle)])
        return tuple(caps)

    plats_raw = os.environ.get("AIZU_WORKER_PLATFORMS", "").strip()
    if not plats_raw:
        return ()
    if plats_raw.lower() in ("all", "*"):
        names = list(SUPPORTED_PLATFORMS)
    else:
        names = [p.strip().lower() for p in plats_raw.split(",")]
    return tuple([None, p, None] for p in names if p in SUPPORTED_PLATFORMS)


class JobSpecError(ValueError):
    """A leased job payload failed validation — reject (hard nack), never run."""


@dataclass(frozen=True)
class JobSpec:
    """One leased job. Built ONLY via :meth:`from_payload`, which validates the
    untrusted dispatch response at the boundary (global input-validation rule)."""

    id: str
    org_id: Optional[int]
    campaign_id: str
    platform: str
    required_account_handle: Optional[str] = None
    target_leads: Optional[int] = None
    duration_minutes: Optional[int] = None
    engine_mode: str = "harvest"
    soul_text: Optional[str] = None
    # Cloud-assigned at enqueue so the org's activity drawer can poll this run live and
    # the worker emits/streams run_events under the SAME id the cloud already knows.
    # Absent for a hand-rolled/legacy job → the worker generates one locally.
    run_id: Optional[str] = None

    @classmethod
    def from_payload(cls, payload: Any) -> "JobSpec":
        """Validate + coerce a dispatch ``job`` object into a JobSpec.

        Accepts both snake_case and camelCase keys (the cloud envelope uses
        camelCase; the stub mirrors it). Raises :class:`JobSpecError` for any
        missing/ill-typed required field so the caller hard-nacks instead of
        crashing mid-run.
        """
        if not isinstance(payload, dict):
            raise JobSpecError(f"job payload is not an object: {type(payload).__name__}")

        def pick(*keys: str) -> Any:
            for k in keys:
                if k in payload and payload[k] is not None:
                    return payload[k]
            return None

        job_id = pick("id", "jobId", "job_id")
        campaign_id = pick("campaignId", "campaign_id")
        platform = pick("platform")
        if not isinstance(job_id, str) or not job_id:
            raise JobSpecError("job.id missing or not a string")
        if not isinstance(campaign_id, str) or not campaign_id:
            raise JobSpecError("job.campaignId missing or not a string")
        if not isinstance(platform, str) or not platform:
            raise JobSpecError("job.platform missing or not a string")
        # Validate against the engine's known platforms at the boundary (same allowlist
        # dispatch routes on) so an unknown platform is rejected here, not deep in a run.
        if platform not in SUPPORTED_PLATFORMS:
            raise JobSpecError(
                f"job.platform {platform!r} not in {sorted(SUPPORTED_PLATFORMS)}")

        engine_mode = pick("engineMode", "engine_mode") or "harvest"
        if engine_mode not in _VALID_ENGINE_MODES:
            raise JobSpecError(
                f"job.engineMode {engine_mode!r} not in {_VALID_ENGINE_MODES}")

        return cls(
            id=job_id,
            org_id=_coerce_optional_int(pick("orgId", "org_id"), "orgId"),
            campaign_id=campaign_id,
            platform=platform,
            required_account_handle=_coerce_optional_str(
                pick("requiredAccountHandle", "required_account_handle")),
            target_leads=_coerce_positive_int(
                pick("targetLeads", "target_leads"), "targetLeads"),
            duration_minutes=_coerce_positive_int(
                pick("durationMinutes", "duration_minutes"), "durationMinutes"),
            engine_mode=engine_mode,
            soul_text=_coerce_optional_str(pick("soulText", "soul_text")),
            run_id=_coerce_optional_str(pick("runId", "run_id")),
        )

    def lock_key(self) -> str:
        """The single-flight identity: one live job per (org, platform, account)."""
        account = self.required_account_handle or "_default"
        return f"{self.org_id}-{self.platform}-{account}"

    def to_payload(self) -> dict:
        """Serialize back to the camelCase wire shape ``from_payload`` accepts, so the
        supervisor can hand a leased job to the child process via a spec file and the
        child rebuilds an IDENTICAL JobSpec through the same validated boundary
        (Phase 6 supervised-subprocess model). Round-trips: ``JobSpec.from_payload(
        spec.to_payload()) == spec``. Only non-None fields are emitted (from_payload
        treats absent == None), keeping the file small and the soul_text out unless
        actually baked."""
        payload: dict[str, Any] = {
            "id": self.id,
            "campaignId": self.campaign_id,
            "platform": self.platform,
            "engineMode": self.engine_mode,
        }
        if self.org_id is not None:
            payload["orgId"] = self.org_id
        if self.required_account_handle is not None:
            payload["requiredAccountHandle"] = self.required_account_handle
        if self.target_leads is not None:
            payload["targetLeads"] = self.target_leads
        if self.duration_minutes is not None:
            payload["durationMinutes"] = self.duration_minutes
        if self.soul_text is not None:
            payload["soulText"] = self.soul_text
        if self.run_id is not None:
            payload["runId"] = self.run_id
        return payload


@dataclass(frozen=True)
class WorkerConfig:
    """How this box reaches dispatch and runs a leased job. Env-driven, frozen."""

    dispatch_base_url: str
    cfg_dir: Path
    db_path: str
    state_dir: Path
    cdp_url: str = "http://127.0.0.1:9222"
    spend_cap: float = 20.0
    text_model: str = "openrouter/owl-alpha"
    vision_model: str = "nex-agi/nex-n2-pro:free"
    heartbeat_interval_sec: int = DEFAULT_HEARTBEAT_INTERVAL_SEC
    # Worker-LEVEL presence cadence (idle keepalive between jobs). Pre-register
    # default; once register returns heartbeatIntervalSec the sidecar prefers that
    # (clamped) value so server/worker cadence cannot drift. Not clamped here —
    # _PresenceThread clamps via _clamp_interval at construction. Test override hook.
    presence_interval_sec: float = DEFAULT_HEARTBEAT_INTERVAL_SEC
    lease_poll_timeout_sec: int = 30
    max_job_minutes: int = DEFAULT_MAX_JOB_MINUTES
    # Supervisor no-progress watchdog threshold (see DEFAULT_STALL_TIMEOUT_SEC). A
    # value <= 0 disables the watchdog (falls back to duration-cap only). Supervisor-only,
    # so it is NOT projected into the child via to_child_dict.
    stall_timeout_sec: float = DEFAULT_STALL_TIMEOUT_SEC
    request_timeout_sec: float = 35.0
    agent_version: str = "0.1.0"
    capabilities: tuple = ()
    # Shared first-register secret (AIZU_WORKER_BOOTSTRAP_TOKEN). Presented as the
    # bearer ONLY on a first register (no per-worker token persisted yet); the server
    # then mints the real long-lived token. Never persisted by the worker (it is the
    # shared secret, not this box's identity). None → first register is impossible
    # (the box must already hold a token, e.g. provisioned out of band).
    bootstrap_token: Optional[str] = None
    # OPTIONAL loopback-only control surface for the desktop shell (Phase 6, D3). Off by
    # default so a bare/headless sidecar (and the whole existing test suite) is unchanged.
    control_surface_enabled: bool = False
    control_surface_port: int = DEFAULT_CONTROL_SURFACE_PORT
    control_surface_token: Optional[str] = None

    @classmethod
    def from_env(cls, *, dispatch_base_url: Optional[str] = None) -> "WorkerConfig":
        """Build from environment, falling back to the same defaults the CLI uses.

        ``AIZU_DISPATCH_URL`` is required (or pass it explicitly) — the worker
        cannot run without a dispatch endpoint to pull from.
        """
        url = dispatch_base_url or os.environ.get("AIZU_DISPATCH_URL")
        if not url:
            raise ValueError(
                "AIZU_DISPATCH_URL not set — the worker needs a dispatch base URL "
                "to lease jobs from (e.g. https://cloud.example.com or the local stub)")
        cfg_dir = Path(os.environ.get("AIZU_CONFIG", "config"))
        db_path = os.environ.get("AIZU_DB", "aizu.db")
        state_dir = Path(os.environ.get("AIZU_WORKER_STATE", ".worker-state"))
        cs_enabled = _env_flag("AIZU_CONTROL_SURFACE")
        cs_token = os.environ.get("AIZU_CONTROL_TOKEN") or None
        if cs_enabled and not cs_token:
            # An unauthenticated LOCAL control plane (pause/stop/focus) is a privesc
            # surface — fail fast rather than bind it open (boundary-validation rule).
            raise ValueError(
                "AIZU_CONTROL_SURFACE is enabled but AIZU_CONTROL_TOKEN is not "
                "set — refusing to expose an unauthenticated local control surface")
        return cls(
            dispatch_base_url=url.rstrip("/"),
            cfg_dir=cfg_dir,
            db_path=db_path,
            state_dir=state_dir,
            cdp_url=os.environ.get("AIZU_CDP_URL", "http://127.0.0.1:9222"),
            spend_cap=float(os.environ.get("AIZU_SPEND_CAP", "20.0")),
            text_model=os.environ.get("OPENROUTER_TEXT_MODEL", "openrouter/owl-alpha"),
            vision_model=os.environ.get(
                "OPENROUTER_VISION_MODEL", "nex-agi/nex-n2-pro:free"),
            bootstrap_token=os.environ.get("AIZU_WORKER_BOOTSTRAP_TOKEN") or None,
            stall_timeout_sec=float(os.environ.get(
                "AIZU_STALL_TIMEOUT_SEC", DEFAULT_STALL_TIMEOUT_SEC)),
            capabilities=_parse_capabilities_env(),
            control_surface_enabled=cs_enabled,
            control_surface_port=int(os.environ.get(
                "AIZU_CONTROL_PORT", DEFAULT_CONTROL_SURFACE_PORT)),
            control_surface_token=cs_token,
        )

    def to_child_dict(self) -> dict:
        """Project ONLY the fields the job-child process needs to run one job into a
        JSON-safe dict (Phase 6 supervised-subprocess model). The child never leases or
        registers, so dispatch/token/heartbeat fields are deliberately omitted — it
        reconstructs a minimal WorkerConfig via :meth:`from_child_dict`. Paths are
        stringified for JSON. Kept beside ``from_child_dict`` so the write/read shape
        lives in one place (no ad-hoc dict duplicated across job_runner/job_child)."""
        return {
            "cfg_dir": str(self.cfg_dir),
            "db_path": self.db_path,
            "state_dir": str(self.state_dir),
            "cdp_url": self.cdp_url,
            "spend_cap": self.spend_cap,
            "text_model": self.text_model,
            "vision_model": self.vision_model,
            "max_job_minutes": self.max_job_minutes,
        }

    @classmethod
    def from_child_dict(cls, data: Any) -> "WorkerConfig":
        """Rebuild the run-relevant WorkerConfig inside the child process from
        :meth:`to_child_dict` output. Validates the boundary (never trust the spec
        file blindly): a non-dict or a missing/ill-typed required path raises
        ValueError so the child writes a ``spec_unreadable`` result rather than
        crashing deep in a run. Fields the child does not use (dispatch_base_url,
        tokens, heartbeat cadence) are filled with inert defaults."""
        if not isinstance(data, dict):
            raise ValueError(f"child cfg is not an object: {type(data).__name__}")
        try:
            return cls(
                dispatch_base_url="",  # child never leases — inert
                cfg_dir=Path(data["cfg_dir"]),
                db_path=str(data["db_path"]),
                state_dir=Path(data["state_dir"]),
                cdp_url=str(data.get("cdp_url", "http://127.0.0.1:9222")),
                spend_cap=float(data.get("spend_cap", 20.0)),
                text_model=str(data.get("text_model", "openrouter/owl-alpha")),
                vision_model=str(data.get("vision_model", "nex-agi/nex-n2-pro:free")),
                max_job_minutes=int(data.get("max_job_minutes", DEFAULT_MAX_JOB_MINUTES)),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"child cfg missing/invalid field: {e}") from e

    @property
    def machine_id(self) -> str:
        """A stable per-box fingerprint, persisted under ``state_dir`` so a restart
        rebinds to the same worker row (PRD §6 workers.id = stable fingerprint)."""
        path = self.state_dir / "machine-id"
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = uuid.uuid4().hex
        path.write_text(fresh, encoding="utf-8")
        return fresh

    def run_args(self, job: JobSpec) -> argparse.Namespace:
        """A fresh argparse.Namespace for one job — the surface ``_run_session_loop``
        and ``_build_run_io`` read. Built per call; never mutated or shared."""
        return argparse.Namespace(
            dry_run=False,
            target_leads=job.target_leads,
            duration_minutes=job.duration_minutes or self.max_job_minutes,
            engine_mode=job.engine_mode,
            cdp_url=self.cdp_url,
            spend_cap=self.spend_cap,
            text_model=self.text_model,
            vision_model=self.vision_model,
            config=str(self.cfg_dir),
            db=self.db_path,
            verbose=False,
            quiet=False,
        )


def _env_flag(name: str) -> bool:
    """A truthy env flag (1/true/yes/on, case-insensitive). Absent/empty → False."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _coerce_optional_int(value: Any, label: str) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise JobSpecError(f"job.{label} is not an integer: {value!r}") from e


def _coerce_positive_int(value: Any, label: str) -> Optional[int]:
    """Like :func:`_coerce_optional_int` but rejects <= 0 — a negative/zero targetLeads or
    durationMinutes from the untrusted dispatch payload is nonsensical and would produce a
    never-terminating or instantly-halting run, so reject it at the boundary."""
    coerced = _coerce_optional_int(value, label)
    if coerced is not None and coerced <= 0:
        raise JobSpecError(f"job.{label} must be a positive integer, got {coerced}")
    return coerced


def _coerce_optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise JobSpecError(f"expected a string, got {type(value).__name__}")
    return value
