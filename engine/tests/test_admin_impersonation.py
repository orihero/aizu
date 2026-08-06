"""HTTP-level superadmin impersonation (Phase 5c): starting impersonation makes the
existing per-org endpoints serve the TARGET org unchanged; ending restores fail-closed;
every start/end is hash-chained into the admin audit log."""
import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from aizu import admin_auth
from aizu.admin_auth import ADMIN_IP_ALLOWLIST_ENV
from aizu.auth import hash_password
from aizu.core.store import Store
from aizu.secrets import SECRET_KEY_ENV, SecretCipher
from aizu.server import serve

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
_INDEX_HTML = '<!doctype html><html><body><div id="root"></div></body></html>'
KEY = SecretCipher.generate_key()
PW = "longenough1"


def _req(method, base, path, body=None, *, cookie=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or "null"), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or "null"), e.headers


def _signup(base, email, company):
    status, resp, headers = _req("POST", base, "/api/auth/signup",
                                 {"email": email, "password": PW, "companyName": company})
    assert status == 200, resp
    return resp["data"]["user"]["orgId"], headers.get("Set-Cookie").split(";", 1)[0]


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
    # two tenant orgs with distinctly-named campaigns
    org_a, _ = _signup(base, "a@x.io", "AcmeCorp")
    org_b, _ = _signup(base, "b@x.io", "BetaLtd")
    cipher = SecretCipher(KEY)
    store = Store(db_path, secret_cipher=cipher)
    try:
        store.upsert_campaign_meta("camp-a", org_id=org_a, display_name="AlphaCampaign")
        store.upsert_campaign_brief("camp-a", {"platform": "instagram"}, org_id=org_a)
        store.upsert_campaign_meta("camp-b", org_id=org_b, display_name="BetaCampaign")
        store.upsert_campaign_brief("camp-b", {"platform": "instagram"}, org_id=org_b)
        secret = admin_auth.generate_totp_secret()
        store.create_platform_admin(email="ops@x.io", password_hash=hash_password(PW),
                                    mfa_secret=cipher.encrypt({"totp": secret}))
    finally:
        store.close()
    yield {"base": base, "db": db_path, "secret": secret,
           "org_a": org_a, "org_b": org_b}
    httpd.shutdown()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(ADMIN_IP_ALLOWLIST_ENV, "127.0.0.1,::1")
    monkeypatch.setenv(SECRET_KEY_ENV, KEY)


_ADMIN_SEQ = [0]


def _seed_admin(db_path, email):
    cipher = SecretCipher(KEY)
    store = Store(db_path, secret_cipher=cipher)
    try:
        secret = admin_auth.generate_totp_secret()
        store.create_platform_admin(email=email.lower(), password_hash=hash_password(PW),
                                    mfa_secret=cipher.encrypt({"totp": secret}))
        return secret
    finally:
        store.close()


def _admin_cookie(srv):
    """Fresh unique admin per call — TOTP anti-replay forbids reusing one admin's code
    within its window, and several tests log in more than once."""
    _ADMIN_SEQ[0] += 1
    email = f"imp-adm{_ADMIN_SEQ[0]}@x.io"
    secret = _seed_admin(srv["db"], email)
    status, _, headers = _req("POST", srv["base"], "/api/admin/login",
                              {"email": email, "password": PW,
                               "totpCode": admin_auth.totp_now(secret)})
    assert status == 200
    return headers.get("Set-Cookie").split(";", 1)[0]


# ----- fail-closed before impersonation -----

def test_admin_without_impersonation_has_no_org_access(srv):
    cookie = _admin_cookie(srv)
    status, resp, _ = _req("GET", srv["base"], "/api/campaigns", cookie=cookie)
    assert status == 401  # an admin who has not started impersonating is not an org user


# ----- impersonation serves the target org unchanged -----

def test_impersonate_org_serves_that_orgs_data(srv):
    cookie = _admin_cookie(srv)
    status, resp, _ = _req("POST", srv["base"], "/api/admin/impersonate",
                           {"orgId": srv["org_a"], "reason": "support ticket 7"},
                           cookie=cookie)
    assert status == 200, resp
    assert resp["data"]["impersonating"]["orgId"] == srv["org_a"]

    # whoami reflects the impersonation
    _, who, _ = _req("GET", srv["base"], "/api/admin/whoami", cookie=cookie)
    assert who["data"]["admin"]["impersonating"] is True
    assert who["data"]["admin"]["effectiveOrgId"] == srv["org_a"]

    # the ORG plane now serves org A's data — and ONLY org A's
    status, camps, _ = _req("GET", srv["base"], "/api/campaigns", cookie=cookie)
    assert status == 200, camps
    blob = json.dumps(camps)
    assert "AlphaCampaign" in blob
    assert "BetaCampaign" not in blob


def test_me_flags_impersonation_for_the_cross_plane_handoff(srv):
    # Gap F: the org bootstrap (/api/me) served under an active impersonation reports
    # impersonated:true so the org app can banner it + offer an exit. A real user is false.
    cookie = _admin_cookie(srv)
    _req("POST", srv["base"], "/api/admin/impersonate",
         {"orgId": srv["org_a"], "reason": "support"}, cookie=cookie)
    status, me, _ = _req("GET", srv["base"], "/api/auth/me", cookie=cookie)
    assert status == 200, me
    assert me["data"]["user"]["impersonated"] is True
    assert me["data"]["user"]["orgId"] == srv["org_a"]

    # A genuinely signed-in org user is never flagged impersonated.
    _, org_cookie = _signup(srv["base"], "real@user.test", "RealCo")
    _, me2, _ = _req("GET", srv["base"], "/api/auth/me", cookie=org_cookie)
    assert me2["data"]["user"]["impersonated"] is False


def test_impersonation_end_restores_fail_closed(srv):
    cookie = _admin_cookie(srv)
    _req("POST", srv["base"], "/api/admin/impersonate",
         {"orgId": srv["org_a"], "reason": "x"}, cookie=cookie)
    status, resp, _ = _req("POST", srv["base"], "/api/admin/impersonate/end", cookie=cookie)
    assert status == 200 and resp["data"]["impersonating"] is None
    status, _, _ = _req("GET", srv["base"], "/api/campaigns", cookie=cookie)
    assert status == 401  # back to no org identity


def test_switching_target_org_reserves_isolation(srv):
    cookie = _admin_cookie(srv)
    _req("POST", srv["base"], "/api/admin/impersonate",
         {"orgId": srv["org_b"], "reason": "audit"}, cookie=cookie)
    status, camps, _ = _req("GET", srv["base"], "/api/campaigns", cookie=cookie)
    blob = json.dumps(camps)
    assert "BetaCampaign" in blob and "AlphaCampaign" not in blob
    _req("POST", srv["base"], "/api/admin/impersonate/end", cookie=cookie)


# ----- validation + missing targets -----

def test_impersonate_unknown_org_is_404(srv):
    cookie = _admin_cookie(srv)
    status, resp, _ = _req("POST", srv["base"], "/api/admin/impersonate",
                           {"orgId": 999999, "reason": "x"}, cookie=cookie)
    assert status == 404


def test_impersonate_requires_reason_and_one_target(srv):
    cookie = _admin_cookie(srv)
    status, _, _ = _req("POST", srv["base"], "/api/admin/impersonate",
                        {"orgId": srv["org_a"]}, cookie=cookie)
    assert status == 400  # missing reason
    status, _, _ = _req("POST", srv["base"], "/api/admin/impersonate",
                        {"orgId": srv["org_a"], "userId": 1, "reason": "x"}, cookie=cookie)
    assert status == 400  # both targets


def test_impersonate_requires_admin_session(srv):
    # a plain org cookie cannot reach the impersonate route
    _, org_cookie = _signup(srv["base"], "c@x.io", "Gamma")
    status, _, _ = _req("POST", srv["base"], "/api/admin/impersonate",
                        {"orgId": srv["org_a"], "reason": "x"}, cookie=org_cookie)
    assert status == 401


# ----- audit -----

def test_impersonation_is_fully_audited_and_chain_holds(srv):
    cookie = _admin_cookie(srv)
    _req("POST", srv["base"], "/api/admin/impersonate",
         {"orgId": srv["org_a"], "reason": "reconstruct me"}, cookie=cookie)
    _req("POST", srv["base"], "/api/admin/impersonate/end", cookie=cookie)
    status, resp, _ = _req("GET", srv["base"], "/api/admin/audit/verify", cookie=cookie)
    assert status == 200 and resp["data"]["ok"] is True
    status, listed, _ = _req("GET", srv["base"], "/api/admin/audit?limit=200", cookie=cookie)
    entries = listed["data"]["entries"]
    starts = [e for e in entries if e["action"] == "impersonate.start"
              and e["reason"] == "reconstruct me"]
    assert starts and starts[0]["target_org_id"] == srv["org_a"]
    assert any(e["action"] == "impersonate.end" for e in entries)
