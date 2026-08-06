"""HTTP-level lifecycle controls (BUILD-PLAN Phase 4): the admin control-flags plane,
resolved drain/halt/updateRequired on the worker + job heartbeats, the agent-version
gate, and the admin worker-revoke route. Exercised over a real ThreadingHTTPServer so
the interim platform-admin gate and the wire shapes are what is under test."""
import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import sqlite3
import time

from aizu.core.store import WORKER_TOKEN_TTL_SEC
from aizu.admin_auth import ADMIN_IP_ALLOWLIST_ENV
from aizu.secrets import SECRET_KEY_ENV
from aizu.server import (MIN_AGENT_VERSION_ENV, WORKER_BOOTSTRAP_ENV, serve)
from ._admin import ADMIN_KEY, admin_cookie, set_admin_env

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config"
_INDEX_HTML = '<!doctype html><html><body><div id="root"></div></body></html>'
PW = "longenough1"
BOOTSTRAP = "boot-secret-lc"
ADMIN_EMAIL = "admin-lc@x.io"


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
            return resp.status, json.loads(resp.read() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or "null")


def _post(base, path, body, **kw):
    return _req("POST", base, path, body, **kw)


def _get(base, path, **kw):
    return _req("GET", base, path, **kw)


def _cookie_of(base, email, company="Co"):
    data = json.dumps({"email": email, "password": PW, "companyName": company}).encode()
    req = urllib.request.Request(base + "/api/auth/signup", data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        return resp.headers.get("Set-Cookie").split(";", 1)[0]


@pytest.fixture(scope="module")
def srv():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    panel_dir = tempfile.mkdtemp(suffix="-spa")
    (Path(panel_dir) / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
    httpd = serve(db_path, panel_dir, str(CONFIG), port=0, billing_providers={})
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    normal_cookie = _cookie_of(base, "normal-lc@x.io", company="Beta")
    os.environ[ADMIN_IP_ALLOWLIST_ENV] = "127.0.0.1,::1"
    os.environ[SECRET_KEY_ENV] = ADMIN_KEY
    admin = admin_cookie(base, db_path, ADMIN_EMAIL)
    yield {"base": base, "db": db_path,
           "admin_cookie": admin, "normal_cookie": normal_cookie}
    httpd.shutdown()
    os.environ.pop(ADMIN_IP_ALLOWLIST_ENV, None)
    os.environ.pop(SECRET_KEY_ENV, None)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    set_admin_env(monkeypatch)
    monkeypatch.delenv(MIN_AGENT_VERSION_ENV, raising=False)


def _register(base, machine_id, caps, *, org_id=1, agent_version="1.0.0"):
    code, resp = _post(base, "/api/worker/register",
                       {"machineId": machine_id, "orgId": org_id, "capabilities": caps,
                        "agentVersion": agent_version}, bearer=BOOTSTRAP)
    assert code == 200, resp
    return resp["data"]["token"]


def _set_flag(srv, cookie=None, **body):
    return _post(srv["base"], "/api/admin/control-flags", body,
                 cookie=cookie or srv["admin_cookie"])


def _heartbeat(base, token):
    return _post(base, "/api/worker/heartbeat", {"currentSessions": 0}, bearer=token)


# ----- admin gate ----------------------------------------------------------------

def test_control_flags_requires_platform_admin(srv):
    # An org user session is not an admin session — the real gate rejects it (401).
    code, resp = _set_flag(srv, cookie=srv["normal_cookie"], scope="global", halt=True)
    assert code == 401, resp


def test_control_flags_rejects_unknown_scope(srv):
    code, resp = _set_flag(srv, scope="galaxy", halt=True)
    assert code == 400, resp


def test_control_flags_requires_scope_key_for_non_global(srv):
    code, resp = _set_flag(srv, scope="platform", halt=True)  # no scopeKey
    assert code == 400, resp


def test_control_flags_requires_a_flag_or_clear(srv):
    code, resp = _set_flag(srv, scope="global")
    assert code == 400, resp


# ----- presence heartbeat resolves flags ----------------------------------------

def test_global_halt_reaches_worker_presence_heartbeat(srv):
    token = _register(srv["base"], "m-glob-halt", [[1, "instagram", "h-glob"]])
    try:
        code, resp = _set_flag(srv, scope="global", halt=True, reason="maint")
        assert code == 200, resp
        code, resp = _heartbeat(srv["base"], token)
        assert code == 200 and resp["data"]["halt"] is True
    finally:
        _set_flag(srv, scope="global", clear=True)


def test_platform_drain_reaches_only_matching_worker(srv):
    ig = _register(srv["base"], "m-ig-drain", [[1, "instagram", "h-igd"]])
    yt = _register(srv["base"], "m-yt-drain", [[1, "youtube", "h-ytd"]])
    try:
        _set_flag(srv, scope="platform", scopeKey="instagram", drain=True)
        _, ig_resp = _heartbeat(srv["base"], ig)
        _, yt_resp = _heartbeat(srv["base"], yt)
        assert ig_resp["data"]["drain"] is True
        assert yt_resp["data"]["drain"] is False
    finally:
        _set_flag(srv, scope="platform", scopeKey="instagram", clear=True)


def test_clear_flag_restores_false(srv):
    token = _register(srv["base"], "m-clear", [[1, "instagram", "h-clear"]])
    _set_flag(srv, scope="global", halt=True)
    _set_flag(srv, scope="global", clear=True)
    _, resp = _heartbeat(srv["base"], token)
    assert resp["data"]["halt"] is False


def test_control_flags_list_returns_rows(srv):
    _set_flag(srv, scope="org", scopeKey="99", drain=True, reason="listtest")
    try:
        code, resp = _get(srv["base"], "/api/admin/control-flags",
                          cookie=srv["admin_cookie"])
        assert code == 200, resp
        keys = {(r["scope"], r["scopeKey"]) for r in resp["data"]["flags"]}
        assert ("org", "99") in keys
    finally:
        _set_flag(srv, scope="org", scopeKey="99", clear=True)


# ----- agent-version gate --------------------------------------------------------

def test_version_gate_sets_update_required(srv, monkeypatch):
    token = _register(srv["base"], "m-oldver", [[1, "instagram", "h-old"]],
                      agent_version="0.9.0")
    monkeypatch.setenv(MIN_AGENT_VERSION_ENV, "1.0.0")
    _, resp = _heartbeat(srv["base"], token)
    assert resp["data"]["updateRequired"] is True


def test_version_gate_passes_current_worker(srv, monkeypatch):
    token = _register(srv["base"], "m-curver", [[1, "instagram", "h-cur"]],
                      agent_version="1.2.0")
    monkeypatch.setenv(MIN_AGENT_VERSION_ENV, "1.0.0")
    _, resp = _heartbeat(srv["base"], token)
    assert resp["data"]["updateRequired"] is False


# ----- worker revoke -------------------------------------------------------------

def test_admin_revoke_kills_worker_token(srv):
    token = _register(srv["base"], "m-revoke", [[1, "instagram", "h-rev"]])
    assert _heartbeat(srv["base"], token)[0] == 200
    code, resp = _post(srv["base"], "/api/admin/workers/revoke",
                       {"workerId": "m-revoke"}, cookie=srv["admin_cookie"])
    assert code == 200 and resp["data"]["revoked"] is True
    # Next request with the revoked token fails auth (request-time revocation).
    assert _heartbeat(srv["base"], token)[0] == 401


def test_admin_revoke_requires_platform_admin(srv):
    code, resp = _post(srv["base"], "/api/admin/workers/revoke",
                       {"workerId": "whatever"}, cookie=srv["normal_cookie"])
    assert code == 401, resp


# ----- token TTL (Phase 4: long-lived, ~1 year) ---------------------------------

def test_register_stamps_one_year_token_ttl(srv):
    before = time.time()
    _register(srv["base"], "m-ttl", [[1, "instagram", "h-ttl"]])
    con = sqlite3.connect(srv["db"])
    try:
        row = con.execute(
            "SELECT token_expires_at FROM workers WHERE id=?", ("m-ttl",)).fetchone()
    finally:
        con.close()
    assert row and row[0] is not None
    # Within a day of now + 1 year (the exact server clock differs from `before`).
    assert abs(row[0] - (before + WORKER_TOKEN_TTL_SEC)) < 86400


# ----- job heartbeat resolves flags for THIS job's scope ------------------------

def _enqueue(srv, account, *, org_id=1, platform="instagram"):
    code, resp = _post(srv["base"], "/api/admin/jobs/enqueue",
                       {"campaignId": "c-acme", "platform": platform,
                        "requiredAccountHandle": account, "orgId": org_id,
                        "targetLeads": 1, "soulText": "be helpful"},
                       cookie=srv["admin_cookie"])
    assert code == 200, resp
    return resp["data"]["job"]["id"]


def _lease(base, token):
    code, resp = _post(base, "/api/worker/lease", {"leasePollTimeoutSec": 0},
                       bearer=token)
    assert code == 200, resp
    return resp["data"]


def test_job_heartbeat_reflects_platform_halt(srv):
    account = "h-jobhalt"
    token = _register(srv["base"], "m-jobhalt", [[1, "instagram", account]])
    job_id = _enqueue(srv, account)
    lease = _lease(srv["base"], token)
    assert lease and lease["job"]["id"] == job_id
    try:
        _set_flag(srv, scope="platform", scopeKey="instagram", halt=True)
        code, resp = _post(srv["base"], f"/api/worker/jobs/{job_id}/heartbeat",
                           {}, bearer=token)
        assert code == 200 and resp["data"]["halt"] is True
    finally:
        _set_flag(srv, scope="platform", scopeKey="instagram", clear=True)


def test_job_heartbeat_reflects_org_halt(srv):
    # Regression: get_job returns camelCase orgId — an org-scoped halt must still reach
    # the job heartbeat (a snake_case key lookup would silently drop it).
    account = "h-orghalt"
    token = _register(srv["base"], "m-orghalt", [[1, "instagram", account]], org_id=1)
    job_id = _enqueue(srv, account, org_id=1)
    lease = _lease(srv["base"], token)
    assert lease and lease["job"]["id"] == job_id
    try:
        _set_flag(srv, scope="org", scopeKey="1", halt=True)
        code, resp = _post(srv["base"], f"/api/worker/jobs/{job_id}/heartbeat",
                           {}, bearer=token)
        assert code == 200 and resp["data"]["halt"] is True
    finally:
        _set_flag(srv, scope="org", scopeKey="1", clear=True)


def test_job_heartbeat_clean_when_no_flags(srv):
    account = "h-jobclean"
    token = _register(srv["base"], "m-jobclean", [[1, "instagram", account]])
    job_id = _enqueue(srv, account)
    lease = _lease(srv["base"], token)
    assert lease and lease["job"]["id"] == job_id
    code, resp = _post(srv["base"], f"/api/worker/jobs/{job_id}/heartbeat",
                       {}, bearer=token)
    assert code == 200
    assert resp["data"]["halt"] is False and resp["data"]["drain"] is False
