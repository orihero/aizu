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

ONE exception to "everything is a 200": a rejected bearer answers **HTTP 401** on every
worker-plane route (`server.py` — "invalid or revoked worker token"). That is why every
``Result`` carries the raw ``status``: the sidecar has to tell "this box has been REVOKED"
(terminal — clear the token and stop, ledger B10) from "the network is flaky / the cloud is
down" (transient — keep retrying), and it must do so on a status CODE, never by
string-matching an error message. See :attr:`Result.is_unauthorized`.

The status code alone is NOT enough, though, and the ``envelope`` flag is why: the dispatch
is documented to run behind a reverse proxy, and an intermediary (nginx basic-auth, an SSO
/ Cloudflare-Access rule, a captive portal) answers 401 with HTML or an empty body — never
the ``{ok, data, error}`` envelope. Reading THAT as revocation would let one proxy
misconfiguration disenrol an entire fleet, so only a 401 that is provably the dispatch
APPLICATION speaking counts (see :attr:`Result.is_unauthorized`).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from ..core.logsetup import get_logger

log = get_logger("aizu.worker.lease_client")

# The ONE status that means "this box's bearer token is no longer accepted" — revoked,
# expired, or pointed at a dispatch that never knew it (ledger B10). Every other failure
# mode (transport, timeout, 5xx, a 4xx about the BODY) is transient or job-scoped and must
# NOT be read as revocation.
UNAUTHORIZED_STATUS = 401


@dataclass(frozen=True)
class Result:
    """The outcome of one dispatch call. ``ok`` reflects the envelope's success flag
    AND transport success; ``data`` is the validated payload (may be ``None`` — an
    empty lease is a *successful* empty result)."""

    ok: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    status: Optional[int] = None
    envelope: bool = False
    """True iff the body really was the dispatch's ``{ok, data, error}`` envelope (a JSON
    object carrying a boolean ``ok``). False for HTML, an empty body, a JSON array, or a
    parse failure — i.e. for anything an intermediary in front of the dispatch produced."""

    @property
    def is_empty(self) -> bool:
        """A successful call that returned no payload (e.g. lease found no job)."""
        return self.ok and self.data is None

    @property
    def is_unauthorized(self) -> bool:
        """The dispatch APPLICATION rejected our bearer token: HTTP 401 **and** a genuine
        ``{ok: false, ...}`` envelope. That pair is the sidecar's revocation signal
        (ledger B10) — see the module docstring for why both halves are required.

        Deliberately keyed on the numeric status, not on ``error`` text: the 401 body's
        wording differs per route ("invalid or revoked worker token" vs. "worker
        registration requires a valid token") and string-matching it would rot silently.
        A transport failure carries ``status=None`` and is therefore NEVER unauthorized;
        neither is a proxy's HTML/empty-bodied 401, which carries no envelope. Both stay
        transient, which is what keeps a flaky network or a misconfigured ingress from
        bricking the whole fleet."""
        return (self.status == UNAUTHORIZED_STATUS
                and self.envelope
                and not self.ok)


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

    @property
    def token(self) -> Optional[str]:
        """The bearer this client presents. Read by the sidecar so a 401 can be matched
        against the credential that was ACTUALLY refused (ledger B10) rather than against
        whatever the shared token store happens to hold now."""
        return self._token

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

    def credential(self, job_id: str) -> Result:
        """Decrypt-on-demand fetch of THIS job's per-org platform credential (SECURITY
        REVIEW CRITICAL/HIGH — replaces the server baking a decrypted secret into the
        job spec/lease). The server gates this on the caller CURRENTLY holding the
        job's lease — a stale/lost lease or another worker's job answers 404, folded
        into ``Result(ok=False, ...)`` here like any other envelope failure; never
        raises, and the credential is never written to disk by this client itself."""
        return self._post(f"/api/worker/jobs/{job_id}/credential", {})

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
    diagnosable in seconds (external-boundary rule).

    ``envelope`` is set ONLY on the two paths where the body really was the dispatch's
    ``{ok, data, error}`` object; every earlier return leaves it False, which is what
    keeps an intermediary's 401 out of :attr:`Result.is_unauthorized`."""
    status = resp.status_code
    raw = resp.text or ""
    if status >= 500:
        log.warning("dispatch %s → HTTP %s (body=%.200s)", path, status, raw)
        return Result(ok=False, error=f"server error {status}", status=status)
    if not raw.strip():
        # No body at all — the dispatch always sends an envelope, so this is a proxy,
        # a load balancer, or a dropped response, NEVER the application answering.
        log.warning("dispatch %s → empty body (HTTP %s)", path, status)
        return Result(ok=False, error=f"HTTP {status}", status=status)
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("dispatch %s → non-JSON body (HTTP %s): %.200s | %s",
                    path, status, raw, e)
        return Result(ok=False, error="malformed JSON response", status=status)
    if not isinstance(body, dict):
        log.warning("dispatch %s → JSON is not an object (HTTP %s): %.200s",
                    path, status, raw)
        return Result(ok=False, error="response envelope is not an object", status=status)
    if not isinstance(body.get("ok"), bool):
        log.warning("dispatch %s → JSON object without a boolean 'ok' (HTTP %s): %.200s",
                    path, status, raw)
        return Result(ok=False, error="response envelope has no ok flag", status=status)
    if body["ok"] is True:
        return Result(ok=True, data=body.get("data"), status=status, envelope=True)
    err = body.get("error") or f"HTTP {status}"
    log.warning("dispatch %s → ok=false (HTTP %s): %s", path, status, err)
    return Result(ok=False, error=str(err), status=status, envelope=True)
