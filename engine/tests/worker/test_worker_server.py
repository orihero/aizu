"""HTTP-level worker plane (v14): the bearer-gated registry + presence endpoints and
the interim allowlist-gated /api/admin/fleet view. Mirrors the live-server harness in
tests/test_multitenancy_server.py — a real ThreadingHTTPServer on an ephemeral port,
exercised over the wire so the dispatch order, the bearer gate, and the fail-closed
allowlist are the things actually under test."""
import io
import json
import logging
import os
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from reelradar.server import (WORKER_BOOTSTRAP_ENV, serve)
from reelradar.admin_auth import ADMIN_IP_ALLOWLIST_ENV
from reelradar.core.store import Store, hash_session_token
from ._admin import admin_cookie, set_admin_env

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config"
_INDEX_HTML = '<!doctype html><html><body><div id="root"></div></body></html>'
PW = "longenough1"
BOOTSTRAP = "boot-secret-xyz"


def _req(method, base, path, body=None, *, cookie=None, bearer=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or "null"), resp.headers.get("Set-Cookie")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or "null"), e.headers.get("Set-Cookie")


def _post(base, path, body, *, cookie=None, bearer=None):
    return _req("POST", base, path, body, cookie=cookie, bearer=bearer)


def _get(base, path, *, cookie=None, bearer=None):
    code, resp, _ = _req("GET", base, path, None, cookie=cookie, bearer=bearer)
    return code, resp


def _cookie(set_cookie):
    return set_cookie.split(";", 1)[0]


def _signup(base, email, company="Co"):
    code, resp, set_cookie = _post(base, "/api/auth/signup",
                                   {"email": email, "password": PW, "companyName": company})
    assert code == 200, resp
    return _cookie(set_cookie)


class _StubRunManager:
    def __init__(self):
        self.launched: list = []

    def launch(self, spec):
        self.launched.append(spec)
        return object(), None

    def status(self, org_id=None):
        return {"active": None, "recent": []}

    def sweep_orphan_pause_files(self):
        return None


@pytest.fixture(scope="module")
def srv():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    panel_dir = tempfile.mkdtemp(suffix="-spa")
    (Path(panel_dir) / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
    httpd = serve(db_path, panel_dir, str(CONFIG), port=0,
                  run_manager=_StubRunManager(), billing_providers={})
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    admin_cookie = _signup(base, "admin@x.io", company="Acme")
    normal_cookie = _signup(base, "normal@x.io", company="Beta")

    yield {"base": base, "db": db_path,
           "admin_cookie": admin_cookie, "normal_cookie": normal_cookie}
    httpd.shutdown()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Each test owns the bootstrap env; default unset (fail closed). The admin plane
    (IP-allowlist + secret key) is configured by default so fleet tests can log in; the
    unset-allowlist test deletes it explicitly."""
    monkeypatch.delenv(WORKER_BOOTSTRAP_ENV, raising=False)
    set_admin_env(monkeypatch)


def _worker_row(db, worker_id):
    store = Store(db)
    try:
        return next((w for w in store.list_workers() if w["id"] == worker_id), None)
    finally:
        store.close()


def _token_hash_in_db(db, worker_id):
    store = Store(db)
    try:
        row = store._conn.execute(
            "SELECT worker_token_hash FROM workers WHERE id=?", (worker_id,)).fetchone()
        return row["worker_token_hash"] if row else None
    finally:
        store.close()


# ----- register --------------------------------------------------------------

def test_register_first_requires_bootstrap_token(srv, monkeypatch):
    # Unset bootstrap ⇒ first register is closed.
    code, resp = _post(srv["base"], "/api/worker/register", {"machineId": "m-none"})[:2]
    assert code == 401, resp

    # Wrong bootstrap secret ⇒ 401.
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-wrong"}, bearer="not-the-secret")
    assert code == 401, resp

    # Correct bootstrap ⇒ 200 with a token + the heartbeat interval.
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-ok", "displayName": "Box1",
                           "host": "host1", "os": "linux", "agentVersion": "0.1.0"},
                          bearer=BOOTSTRAP)
    assert code == 200, resp
    assert resp["data"]["workerId"] == "m-ok"
    assert isinstance(resp["data"]["token"], str) and resp["data"]["token"]
    assert resp["data"]["heartbeatIntervalSec"] == 20


def test_register_persists_hash_not_plaintext(srv, monkeypatch):
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-hash"}, bearer=BOOTSTRAP)
    assert code == 200, resp
    token = resp["data"]["token"]
    stored = _token_hash_in_db(srv["db"], "m-hash")
    assert stored == hash_session_token(token)
    assert stored != token
    # A row really persisted with derived presence fields.
    row = _worker_row(srv["db"], "m-hash")
    assert row is not None and row["status"] in ("online", "stale", "offline")


def test_register_returns_token_exactly_once_and_rotates(srv, monkeypatch):
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    _, resp1, _ = _post(srv["base"], "/api/worker/register",
                        {"machineId": "m-rot"}, bearer=BOOTSTRAP)
    token1 = resp1["data"]["token"]
    # Re-register via the EXISTING bearer token rotates it (no bootstrap needed).
    code, resp2, _ = _post(srv["base"], "/api/worker/register",
                           {"machineId": "ignored"}, bearer=token1)
    assert code == 200, resp2
    token2 = resp2["data"]["token"]
    assert token2 != token1
    # Old token no longer authenticates; new one does.
    assert _post(srv["base"], "/api/worker/heartbeat", {}, bearer=token1)[0] == 401
    assert _post(srv["base"], "/api/worker/heartbeat", {}, bearer=token2)[0] == 200


def test_register_token_not_logged(srv, monkeypatch):
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    logger = logging.getLogger("reelradar.server")
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    prev_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        _, resp, _ = _post(srv["base"], "/api/worker/register",
                           {"machineId": "m-log"}, bearer=BOOTSTRAP)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
    token = resp["data"]["token"]
    assert token not in buf.getvalue()


def test_register_validation_rejects_bad_capabilities(srv, monkeypatch):
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    # Not a 3-list.
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-cap1", "capabilities": [["a", "b"]]},
                          bearer=BOOTSTRAP)
    assert code == 400, resp
    # Unknown platform.
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-cap2", "capabilities": [[1, "myspace", "h"]]},
                          bearer=BOOTSTRAP)
    assert code == 400, resp
    # Valid trio round-trips.
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-cap3",
                           "capabilities": [[1, "instagram", "acme"]]},
                          bearer=BOOTSTRAP)
    assert code == 200, resp
    row = _worker_row(srv["db"], "m-cap3")
    assert row["capabilities"] == [[1, "instagram", "acme"]]


def test_register_accepts_pool_wide_capabilities_with_null_handle(srv, monkeypatch):
    """A pool-wide/unpinned capability (org None, handle None) MUST register — the lease
    matcher is built to treat a None handle as unpinned, so the register validator must
    not forbid it (else a fleet run is rejected as 'no capable worker')."""
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-cap-pool",
                           "capabilities": [[None, "instagram", None],
                                            [None, "x", None]]},
                          bearer=BOOTSTRAP)
    assert code == 200, resp
    row = _worker_row(srv["db"], "m-cap-pool")
    assert row["capabilities"] == [[None, "instagram", None], [None, "x", None]]


def test_register_first_requires_machine_id(srv, monkeypatch):
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    code, resp, _ = _post(srv["base"], "/api/worker/register", {}, bearer=BOOTSTRAP)
    assert code == 400, resp


# ----- heartbeat -------------------------------------------------------------

def _fresh_worker(srv, monkeypatch, worker_id):
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    _, resp, _ = _post(srv["base"], "/api/worker/register",
                       {"machineId": worker_id, "maxSessions": 5}, bearer=BOOTSTRAP)
    return resp["data"]["token"]


def test_heartbeat_requires_valid_bearer(srv, monkeypatch):
    token = _fresh_worker(srv, monkeypatch, "m-hb")
    # No bearer ⇒ 401.
    assert _post(srv["base"], "/api/worker/heartbeat", {})[0] == 401
    # Garbage bearer ⇒ 401.
    assert _post(srv["base"], "/api/worker/heartbeat", {}, bearer="garbage")[0] == 401
    # Valid bearer ⇒ 200 with the placeholder control flags.
    code, resp, _ = _post(srv["base"], "/api/worker/heartbeat", {}, bearer=token)
    assert code == 200, resp
    assert resp["data"] == {"drain": False, "halt": False, "updateRequired": False}


def test_heartbeat_revoked_token_is_401(srv, monkeypatch):
    token = _fresh_worker(srv, monkeypatch, "m-revoked")
    store = Store(srv["db"])
    try:
        assert store.revoke_worker("m-revoked") is True
    finally:
        store.close()
    assert _post(srv["base"], "/api/worker/heartbeat", {}, bearer=token)[0] == 401


def test_heartbeat_ignores_body_workerid_uses_token(srv, monkeypatch):
    token_a = _fresh_worker(srv, monkeypatch, "m-a")
    _fresh_worker(srv, monkeypatch, "m-b")
    before_b = _worker_row(srv["db"], "m-b")["lastHeartbeatAt"]
    # A's token beats, but the body claims to be B — only A's row may move.
    code, _, _ = _post(srv["base"], "/api/worker/heartbeat",
                       {"workerId": "m-b", "currentSessions": 2}, bearer=token_a)
    assert code == 200
    after_a = _worker_row(srv["db"], "m-a")
    after_b = _worker_row(srv["db"], "m-b")
    assert after_a["currentSessions"] == 2
    assert after_b["lastHeartbeatAt"] == before_b  # untouched


def test_heartbeat_updates_current_sessions(srv, monkeypatch):
    token = _fresh_worker(srv, monkeypatch, "m-load")
    _post(srv["base"], "/api/worker/heartbeat", {"currentSessions": 3}, bearer=token)
    assert _worker_row(srv["db"], "m-load")["currentSessions"] == 3
    # Omitting it leaves the stored value unchanged.
    _post(srv["base"], "/api/worker/heartbeat", {}, bearer=token)
    assert _worker_row(srv["db"], "m-load")["currentSessions"] == 3


# ----- fleet -----------------------------------------------------------------

def test_fleet_401_without_session(srv):
    code, resp = _get(srv["base"], "/api/admin/fleet")
    assert code == 401, resp


def test_fleet_401_for_a_plain_org_cookie(srv):
    # An org user session is not an admin session — the real gate rejects it (401).
    code, resp = _get(srv["base"], "/api/admin/fleet", cookie=srv["normal_cookie"])
    assert code == 401, resp


def test_fleet_401_when_ip_allowlist_unset(srv, monkeypatch):
    # A valid admin cookie, but the IP-allowlist is unset ⇒ the gate fails closed.
    cookie = admin_cookie(srv["base"], srv["db"], email="fleet-a@x.io")
    monkeypatch.delenv(ADMIN_IP_ALLOWLIST_ENV, raising=False)
    code, resp = _get(srv["base"], "/api/admin/fleet", cookie=cookie)
    assert code == 401, resp


def test_fleet_200_for_admin_session(srv, monkeypatch):
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    _post(srv["base"], "/api/worker/register",
          {"machineId": "m-fleet", "displayName": "Fleeter"}, bearer=BOOTSTRAP)
    cookie = admin_cookie(srv["base"], srv["db"], email="fleet-b@x.io")
    code, resp = _get(srv["base"], "/api/admin/fleet", cookie=cookie)
    assert code == 200, resp
    workers = resp["data"]["workers"]
    assert isinstance(workers, list)
    target = next(w for w in workers if w["id"] == "m-fleet")
    assert target["status"] in ("online", "stale", "offline")
    assert "lastSeenAgeSec" in target
    assert target["displayName"] == "Fleeter"


def test_fleet_envelope_shape(srv, monkeypatch):
    cookie = admin_cookie(srv["base"], srv["db"], email="fleet-c@x.io")
    code, resp = _get(srv["base"], "/api/admin/fleet", cookie=cookie)
    assert code == 200, resp
    assert set(resp.keys()) == {"ok", "data", "error"}
    assert resp["ok"] is True and resp["error"] is None
    assert "workers" in resp["data"]


# ----- dispatch ordering -----------------------------------------------------

def test_worker_routes_bypass_cookie_gate(srv, monkeypatch):
    # A worker request with NO cookie still reaches the bearer handler (proves the
    # worker block is matched before the session gate): garbage bearer → 401 from
    # the worker handler, NOT a generic "authentication required".
    code, resp, _ = _post(srv["base"], "/api/worker/heartbeat", {}, bearer="garbage")
    assert code == 401
    assert resp["error"] == "invalid or revoked worker token"


def test_fleet_does_not_require_bearer(srv, monkeypatch):
    # The fleet route is admin-session gated, NOT bearer — an admin cookie alone
    # reaches a 200 with no Authorization header present.
    cookie = admin_cookie(srv["base"], srv["db"], email="fleet-d@x.io")
    code, _ = _get(srv["base"], "/api/admin/fleet", cookie=cookie)
    assert code == 200
