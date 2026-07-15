"""Warming kill-switch (warming PRD §9.1).

Three layers gate whether a warming run may start. P0 ships the first two (the
auto circuit-breaker is P1):

  1. **Env hard-stop** ``REELRADAR_WARMING_ENABLED`` — default OFF. Checked
     BEFORE any Chrome is attached, so a disabled warming campaign never opens a
     live browser.
  2. **Per-org / per-platform DB switch** — the settings overlay keys
     ``warmingEnabled`` (bool) and ``warmingDisabledPlatforms`` (list).

``warming_kill_reason`` returns a human reason string when warming must NOT run,
or None when it may proceed. It must be cheap (no Chrome, no network) so the
caller can short-circuit early, and re-checkable each dwell window (§9.1).
"""
from __future__ import annotations

import os
from typing import Any, Optional

WARMING_ENABLED_ENV = "REELRADAR_WARMING_ENABLED"
_TRUTHY = {"1", "true", "yes", "on"}


def warming_enabled_env() -> bool:
    """Layer 1: the env hard-stop. Default OFF — warming is opt-in per host."""
    return os.environ.get(WARMING_ENABLED_ENV, "").strip().lower() in _TRUTHY


def warming_kill_reason(store: Any, *, org_id: Optional[int], platform: str
                        ) -> Optional[str]:
    """None if a warming run may proceed for (org, platform); else a reason string.

    Layer 1 (env) is checked first and unconditionally — a host with warming OFF
    never warms, regardless of per-org settings. Layer 2 (per-org DB switch) only
    applies when the org is known. Layer 3 (distributed-workers control-flag halt,
    BUILD-PLAN Phase 4) is defense in depth: a platform/org/global `halt` set in the
    fleet console ALSO fails the in-engine warming gate, so a halted platform stops
    warming even on a box that leased its job before the halt landed.
    """
    if not warming_enabled_env():
        return f"warming disabled ({WARMING_ENABLED_ENV} not set)"
    if org_id is not None:
        settings = store.get_settings(org_id) or {}
        if settings.get("warmingEnabled") is False:
            return f"warming disabled for org {org_id}"
        disabled = settings.get("warmingDisabledPlatforms") or []
        if isinstance(disabled, (list, tuple)) and platform in disabled:
            return f"warming disabled for platform {platform!r} (org {org_id})"
    resolve = getattr(store, "resolve_control_flags", None)
    if callable(resolve):
        try:
            if resolve(org_id=org_id, platform=platform).get("halt"):
                return f"halted by control flag (platform {platform!r}, org {org_id})"
        except Exception:  # noqa: BLE001 — the gate must never crash a run on a flag read
            pass
    return None
