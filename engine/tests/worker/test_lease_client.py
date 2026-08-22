"""Tolerant dispatch client — never raises, validates the envelope (lease_client.py,
BUILD-PLAN §2.6, external-boundary rule)."""
from __future__ import annotations

import httpx
import pytest

from aizu.worker.lease_client import LeaseClient, Result


def _client(handler) -> LeaseClient:
    transport = httpx.MockTransport(handler)
    return LeaseClient("http://stub.local",
                       client=httpx.Client(transport=transport))


def test_successful_envelope_returns_data():
    lc = _client(lambda req: httpx.Response(200, json={"ok": True, "data": {"x": 1}}))
    res = lc.lease({"workerId": "w"})
    assert res.ok and res.data == {"x": 1} and not res.is_empty


def test_empty_lease_is_successful_but_empty():
    lc = _client(lambda req: httpx.Response(200, json={"ok": True, "data": None}))
    res = lc.lease({"workerId": "w"})
    assert res.ok and res.data is None and res.is_empty


def test_ok_false_envelope_is_a_failure():
    lc = _client(lambda req: httpx.Response(200, json={"ok": False, "error": "nope"}))
    res = lc.lease({})
    assert not res.ok and res.error == "nope"


def test_malformed_json_does_not_raise():
    lc = _client(lambda req: httpx.Response(200, content=b"not json{"))
    res = lc.lease({})
    assert not res.ok and "malformed" in res.error.lower()


def test_non_object_json_does_not_raise():
    lc = _client(lambda req: httpx.Response(200, json=[1, 2, 3]))
    res = lc.lease({})
    assert not res.ok and "not an object" in res.error.lower()


def test_server_error_is_a_failure_not_a_crash():
    lc = _client(lambda req: httpx.Response(503, text="upstream down"))
    res = lc.lease({})
    assert not res.ok and res.status == 503


def test_transport_error_becomes_a_result():
    def boom(req):
        raise httpx.ConnectError("connection refused")
    lc = _client(boom)
    res = lc.lease({})
    assert not res.ok and "transport" in res.error.lower()


def test_bearer_token_is_attached():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers.get("Authorization")
        return httpx.Response(200, json={"ok": True, "data": {}})

    lc = _client(handler).with_token("abc123")
    lc.lease({})
    assert seen["auth"] == "Bearer abc123"


def test_with_token_does_not_mutate_original():
    lc = _client(lambda req: httpx.Response(200, json={"ok": True, "data": {}}))
    bound = lc.with_token("t")
    assert bound is not lc


def test_endpoint_paths_are_correct():
    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        return httpx.Response(200, json={"ok": True, "data": {}})

    lc = _client(handler)
    lc.heartbeat("job-9", {})
    assert seen["path"] == "/api/worker/jobs/job-9/heartbeat"
    lc.ack("job-9", {})
    assert seen["path"] == "/api/worker/jobs/job-9/ack"
    lc.credential("job-9")
    assert seen["path"] == "/api/worker/jobs/job-9/credential"


def test_credential_returns_the_fetched_secret():
    lc = _client(lambda req: httpx.Response(
        200, json={"ok": True, "data": {"credential": {"api_key": "K"}}}))
    res = lc.credential("job-1")
    assert res.ok and res.data == {"credential": {"api_key": "K"}}


# ----- B10: the 401 signal the sidecar's revocation halt keys on -----------------

def test_a_401_is_flagged_unauthorized_on_the_result():
    """The dispatch answers 401 on a revoked/expired bearer. The status must survive the
    envelope parse, because that flag — not the error TEXT — is what the sidecar halts on."""
    lc = _client(lambda req: httpx.Response(
        401, json={"ok": False, "error": "invalid or revoked worker token"}))
    res = lc.lease({})
    assert res.status == 401 and res.is_unauthorized is True


@pytest.mark.parametrize("respond", [
    lambda: httpx.Response(401, text="<html><head><title>401 Authorization "
                                     "Required</title></head><body>nginx</body></html>"),
    lambda: httpx.Response(401, text=""),                      # empty body
    lambda: httpx.Response(401, text="[]"),                    # JSON, not an object
    lambda: httpx.Response(401, json={"message": "Access denied"}),  # no `ok` flag
])
def test_a_401_without_the_dispatch_envelope_is_TRANSIENT_not_revocation(respond):
    """CLAUDE.md ships the bridge behind a reverse proxy, and the dispatch's own contract
    is that every application response carries the `{ok, data, error}` envelope. So a 401
    WITHOUT that envelope came from an intermediary — nginx basic-auth, an SSO/Cloudflare
    Access rule mid-rollout, a captive portal — and must never read as "this box is
    revoked": that direction destroys the box's credential irreversibly, so one ingress
    misconfiguration would disenrol the entire fleet with no way back."""
    lc = _client(lambda req: respond())
    res = lc.lease({})
    assert not res.ok
    assert res.status == 401           # the status is still reported...
    assert res.envelope is False       # ...but the body was not the dispatch's
    assert res.is_unauthorized is False


def test_only_the_dispatch_s_own_401_envelope_counts_as_revocation():
    """The companion of the above: the REAL server's 401 body is the envelope, so the
    revocation signal still fires for the case B10 exists to fix."""
    lc = _client(lambda req: httpx.Response(
        401, json={"ok": False, "data": None,
                   "error": "invalid or revoked worker token"}))
    res = lc.lease({})
    assert res.envelope is True and res.is_unauthorized is True


@pytest.mark.parametrize("make", [
    lambda: httpx.Response(500, text="kaboom"),
    lambda: httpx.Response(403, json={"ok": False, "error": "forbidden"}),
    lambda: httpx.Response(404, json={"ok": False, "error": "unknown job"}),
    lambda: httpx.Response(200, json={"ok": False, "error": "bad body"}),
])
def test_other_failures_are_never_unauthorized(make):
    """The flaky-network guard, at the boundary: nothing but a literal 401 may read as
    revocation, or a transient blip would brick the box (worse than the bug B10 fixes)."""
    lc = _client(lambda req: make())
    assert lc.lease({}).is_unauthorized is False


def test_a_transport_failure_is_never_unauthorized():
    def boom(req):
        raise httpx.ConnectError("connection refused")
    res = _client(boom).lease({})
    assert res.status is None and res.is_unauthorized is False


def test_credential_404_from_a_non_lease_holder_is_a_failure_not_a_crash():
    """SECURITY REVIEW CRITICAL/HIGH: a worker that doesn't hold the job's lease gets
    404, folded into the same never-throw Result envelope as any other failure."""
    lc = _client(lambda req: httpx.Response(404, json={"ok": False, "error": "unknown job"}))
    res = lc.credential("job-not-mine")
    assert not res.ok and res.status == 404 and res.error == "unknown job"
