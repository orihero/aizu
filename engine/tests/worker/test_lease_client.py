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
