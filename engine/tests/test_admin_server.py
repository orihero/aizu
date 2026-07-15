"""HTTP-level superadmin plane auth (Phase 5b): admin login (password + TOTP MFA +
IP-allowlist), logout, whoami, DB-backed throttle, and the audit trail — exercised over
a real ThreadingHTTPServer so the separate gate and wire shapes are what is tested."""
import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from reelradar import admin_auth
from reelradar.admin_auth import ADMIN_IP_ALLOWLIST_ENV
from reelradar.auth import hash_password
from reelradar.core.store import ADMIN_LOGIN_MAX_FAILURES, Store
from reelradar.secrets import SECRET_KEY_ENV, SecretCipher
from reelradar.server import serve

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


def _seed_admin(db_path, email):
    """Create a platform admin directly in the DB, returning its TOTP secret so the
    test can compute valid codes."""
    cipher = SecretCipher(KEY)
    store = Store(db_path, secret_cipher=cipher)
    try:
        secret = admin_auth.generate_totp_secret()
        store.create_platform_admin(email=email.lower(),
                                    password_hash=hash_password(PW),
                                    mfa_secret=cipher.encrypt({"totp": secret}))
        return secret
    finally:
        store.close()


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
    secret = _seed_admin(db_path, "ops@x.io")
    yield {"base": base, "db": db_path, "secret": secret, "email": "ops@x.io"}
    httpd.shutdown()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # Loopback is the test peer; allow it, and provide the MFA decryption key.
    monkeypatch.setenv(ADMIN_IP_ALLOWLIST_ENV, "127.0.0.1,::1")
    monkeypatch.setenv(SECRET_KEY_ENV, KEY)


def _login(srv, *, email=None, password=PW, code=None, secret=None):
    secret = secret or srv["secret"]
    body = {"email": email or srv["email"], "password": password,
            "totpCode": code if code is not None else admin_auth.totp_now(secret)}
    return _req("POST", srv["base"], "/api/admin/login", body)


_ADMIN_SEQ = [0]


def _fresh_login(srv):
    """Seed a UNIQUE admin and log it in. TOTP anti-replay forbids reusing one admin's
    code twice inside its window, so every success-path test needs its own admin."""
    _ADMIN_SEQ[0] += 1
    email = f"ops-s{_ADMIN_SEQ[0]}@x.io"
    secret = _seed_admin(srv["db"], email)
    status, _, headers = _req("POST", srv["base"], "/api/admin/login",
                              {"email": email, "password": PW,
                               "totpCode": admin_auth.totp_now(secret)})
    assert status == 200
    return email, headers.get("Set-Cookie").split(";", 1)[0]


# ----- login happy path -----

def test_login_success_sets_cookie_and_whoami_works(srv):
    email = f"ops-hp{_ADMIN_SEQ[0] + 1}@x.io"
    _ADMIN_SEQ[0] += 1
    secret = _seed_admin(srv["db"], email)
    status, resp, headers = _login(srv, email=email, secret=secret)
    assert status == 200, resp
    cookie_hdr = headers.get("Set-Cookie")
    assert admin_auth.ADMIN_SESSION_COOKIE in cookie_hdr and "HttpOnly" in cookie_hdr
    cookie = cookie_hdr.split(";", 1)[0]
    status, resp, _ = _req("GET", srv["base"], "/api/admin/whoami", cookie=cookie)
    assert status == 200
    assert resp["data"]["admin"]["email"] == email
    assert resp["data"]["admin"]["impersonating"] is False


def test_totp_code_cannot_be_replayed(srv):
    _ADMIN_SEQ[0] += 1
    email = f"replay{_ADMIN_SEQ[0]}@x.io"
    secret = _seed_admin(srv["db"], email)
    code = admin_auth.totp_now(secret)
    s1, _, _ = _req("POST", srv["base"], "/api/admin/login",
                    {"email": email, "password": PW, "totpCode": code})
    assert s1 == 200
    # same code, same acceptance window → the second use is a replay and is rejected
    s2, r2, _ = _req("POST", srv["base"], "/api/admin/login",
                     {"email": email, "password": PW, "totpCode": code})
    assert s2 == 401 and r2["error"] == "invalid credentials"


def test_login_wrong_password_rejected(srv):
    status, resp, _ = _login(srv, email="ops@x.io", password="wrongpass1")
    assert status == 401
    assert resp["error"] == "invalid credentials"


def test_login_wrong_totp_rejected(srv):
    status, resp, _ = _login(srv, code="000001")
    assert status == 401
    assert resp["error"] == "invalid credentials"


def test_login_missing_totp_is_400(srv):
    status, resp, _ = _req("POST", srv["base"], "/api/admin/login",
                           {"email": srv["email"], "password": PW})
    assert status == 400
    assert "totpCode" in resp["error"]


def test_login_unknown_admin_rejected(srv):
    status, resp, _ = _login(srv, email="ghost@x.io", secret=srv["secret"])
    assert status == 401
    assert resp["error"] == "invalid credentials"


# ----- IP allowlist (fails closed) -----

def test_off_allowlist_login_forbidden(srv, monkeypatch):
    monkeypatch.setenv(ADMIN_IP_ALLOWLIST_ENV, "10.0.0.0/8")   # excludes loopback
    status, resp, _ = _login(srv)
    assert status == 403
    assert resp["error"] == "forbidden"


def test_whoami_off_allowlist_is_unauthenticated(srv, monkeypatch):
    # First get a valid cookie while allowlisted…
    _, cookie = _fresh_login(srv)
    # …then replay it from an off-allowlist perspective → the gate fails closed.
    monkeypatch.setenv(ADMIN_IP_ALLOWLIST_ENV, "10.0.0.0/8")
    status, resp, _ = _req("GET", srv["base"], "/api/admin/whoami", cookie=cookie)
    assert status == 401


def test_unset_allowlist_fails_closed(srv, monkeypatch):
    monkeypatch.delenv(ADMIN_IP_ALLOWLIST_ENV, raising=False)
    status, _, _ = _login(srv)
    assert status == 403


# ----- session lifecycle -----

def test_whoami_without_cookie_is_401(srv):
    status, resp, _ = _req("GET", srv["base"], "/api/admin/whoami")
    assert status == 401


def test_logout_kills_the_session(srv):
    _, cookie = _fresh_login(srv)
    status, resp, out = _req("POST", srv["base"], "/api/admin/logout", cookie=cookie)
    assert status == 200 and resp["data"]["loggedOut"] is True
    assert "Max-Age=0" in out.get("Set-Cookie")
    status, _, _ = _req("GET", srv["base"], "/api/admin/whoami", cookie=cookie)
    assert status == 401


# ----- DB-backed throttle -----

def test_throttle_locks_after_max_failures(srv):
    email = "throttle@x.io"
    _seed_admin(srv["db"], email)
    for _ in range(ADMIN_LOGIN_MAX_FAILURES):
        status, _, _ = _login(srv, email=email, password="wrongpass1")
        assert status == 401
    # even a correct login is now locked out (429)
    status, resp, _ = _login(srv, email=email, secret=srv["secret"])
    assert status == 429


# ----- audit trail -----

def test_login_is_audited_and_chain_verifies(srv):
    _fresh_login(srv)  # a success + whatever failures other tests logged
    store = Store(srv["db"])
    try:
        assert store.verify_admin_audit_chain()["ok"] is True
        actions = {r["action"] for r in store.list_admin_audit(limit=100)}
        assert "admin.login" in actions
    finally:
        store.close()
