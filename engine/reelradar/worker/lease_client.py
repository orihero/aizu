"""Tolerant HTTP client for the dispatch worker-plane (BUILD-PLAN Phase 1, §2.6).

Every call returns a typed :class:`Result` and NEVER raises — a malformed dispatch
reply, a 500, a dropped connection, or non-JSON body becomes ``Result(ok=False,
error=...)`` so the pull loop logs it and backs off rather than crashing (the
external-boundary discipline from the global rules: parse behind a never-throw
boundary, validate the ``{ok, data, error}`` envelope, never bare-cast a payload we
did not construct).

Auth is a per-worker bearer token. The dispatch contract (the real ``server.py`` worker
plane, Phase 3): all responses are HTTP 200 with the ``{ok, data, error}`` envelope; an
empty lease is ``{ok: true, data: null}`` (never HTTP 204).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from ..core.logsetup import get_logger

log = get_logger("reelradar.worker.lease_client")


@dataclass(frozen=True)
class Result:
    """The outcome of one dispatch call. ``ok`` reflects the envelope's success flag
    AND transport success; ``data`` is the validated payload (may be ``None`` — an
    empty lease is a *successful* empty result)."""

    ok: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    status: Optional[int] = None

    @property
    def is_empty(self) -> bool:
        """A successful call that returned no payload (e.g. lease found no job)."""
        return self.ok and self.data is None


class LeaseClient:
    """Outbound-only client to the dispatch base URL. One per worker process."""

    def __init__(self, base_url: str, *, token: Optional[str] = None,
                 timeout: float = 35.0,
                 client: Optional[httpx.Client] = None):
        self._base = base_url.rstrip("/")
        self._token = token
        # Injectable for tests; owns its own client otherwise.
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None

    def with_token(self, token: str) -> "LeaseClient":
        """Return a client bound to ``token`` (set after register). New object — the
        original is left unchanged (no mutation)."""
        clone = LeaseClient.__new__(LeaseClient)
        clone._base = self._base
        clone._token = token
        clone._client = self._client
        clone._owns_client = False
        return clone

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # --- worker-plane endpoints ------------------------------------------------

    def register(self, body: dict) -> Result:
        return self._post("/api/worker/register", body)

    def lease(self, body: dict) -> Result:
        return self._post("/api/worker/lease", body)

    def presence(self, body: dict) -> Result:
        """Worker-LEVEL presence heartbeat → POST /api/worker/heartbeat (NOT the
        job-scoped /jobs/{id}/heartbeat). Same never-throw boundary as the rest:
        a 500/non-JSON/transport error becomes Result(ok=False), never an exception."""
        return self._post("/api/worker/heartbeat", body)

    def heartbeat(self, job_id: str, body: dict) -> Result:
        return self._post(f"/api/worker/jobs/{job_id}/heartbeat", body)

    def ack(self, job_id: str, body: dict) -> Result:
        return self._post(f"/api/worker/jobs/{job_id}/ack", body)

    def nack(self, job_id: str, body: dict) -> Result:
        return self._post(f"/api/worker/jobs/{job_id}/nack", body)

    # --- the never-throw boundary ----------------------------------------------

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _post(self, path: str, body: dict) -> Result:
        url = f"{self._base}{path}"
        try:
            resp = self._client.post(url, json=body, headers=self._headers())
        except httpx.HTTPError as e:
            log.warning("dispatch %s unreachable: %s", path, e)
            return Result(ok=False, error=f"transport: {e}")
        return _parse_envelope(resp, path)


def _parse_envelope(resp: "httpx.Response", path: str) -> Result:
    """Validate an ``{ok, data, error}`` response without ever raising.

    Logs the raw body + status on any parse/shape failure so the next bad payload is
    diagnosable in seconds (external-boundary rule)."""
    status = resp.status_code
    raw = resp.text or ""
    if status >= 500:
        log.warning("dispatch %s → HTTP %s (body=%.200s)", path, status, raw)
        return Result(ok=False, error=f"server error {status}", status=status)
    try:
        body = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        log.warning("dispatch %s → non-JSON body (HTTP %s): %.200s | %s",
                    path, status, raw, e)
        return Result(ok=False, error="malformed JSON response", status=status)
    if not isinstance(body, dict):
        log.warning("dispatch %s → JSON is not an object (HTTP %s): %.200s",
                    path, status, raw)
        return Result(ok=False, error="response envelope is not an object", status=status)
    if body.get("ok") is True:
        return Result(ok=True, data=body.get("data"), status=status)
    err = body.get("error") or f"HTTP {status}"
    log.warning("dispatch %s → ok=false (HTTP %s): %s", path, status, err)
    return Result(ok=False, error=str(err), status=status)
