"""Auth tests: the stdlib hashing/throttle primitives (aizu.auth) and the
bridge's email+password endpoints + the session gate over /api/* (aizu.server).

The panel is a local control plane; once auth is on, every /api/* surface except
/api/auth/* must present a valid session cookie."""
import json
import os
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from aizu.auth import (LoginThrottle, hash_password, new_session_token,
                            verify_password)
from aizu.server import serve
from aizu.core.store import Store

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"

_FAST = {"iterations": 1000}  # keep unit-test hashing snappy; endpoints use the real cost


# ----- unit: password hashing -----

def test_hash_and_verify_roundtrip():
    stored = hash_password("correct horse battery", **_FAST)
    assert verify_password("correct horse battery", stored) is True


def test_verify_rejects_wrong_password():
    stored = hash_password("right-password", **_FAST)
    assert verify_password("wrong-password", stored) is False


def test_hash_is_salted_so_same_password_differs():
    a = hash_password("same-password", **_FAST)
    b = hash_password("same-password", **_FAST)
    assert a != b  # random per-user salt
    assert verify_password("same-password", a)
    assert verify_password("same-password", b)


def test_verify_never_raises_on_malformed_stored_value():
    for bad in ("", "garbage", "pbkdf2_sha256$notanint$ab$cd", "a$b$c"):
        assert verify_password("x", bad) is False


def test_stored_format_is_self_describing():
    stored = hash_password("pw", iterations=1234)
    algo, iters, salt_hex, hash_hex = stored.split("$")
    assert algo == "pbkdf2_sha256" and iters == "1234"
    assert bytes.fromhex(salt_hex) and bytes.fromhex(hash_hex)


# ----- unit: login throttle -----

def test_throttle_locks_after_max_failures_and_reset_clears():
    t = LoginThrottle(max_failures=3, window=100.0, lockout=100.0)
    assert not t.is_locked("a@b.com")
    for _ in range(3):
        t.record_failure("a@b.com")
    assert t.is_locked("a@b.com")
    t.reset("a@b.com")
    assert not t.is_locked("a@b.com")


def test_throttle_is_isolated_per_key():
    t = LoginThrottle(max_failures=2, window=100.0, lockout=100.0)
    t.record_failure("a@b.com")
    t.record_failure("a@b.com")
    assert t.is_locked("a@b.com")
    assert not t.is_locked("other@b.com")


# ----- HTTP endpoint + gate tests -----

@pytest.fixture
def server():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    panel_dir = tempfile.mkdtemp(suffix="-spa")
    (Path(panel_dir) / "index.html").write_text(
        '<!doctype html><div id="root"></div>', encoding="utf-8")
    httpd = serve(db_path, panel_dir, str(CONFIG), port=0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield {"base": f"http://127.0.0.1:{port}", "db": db_path}
    httpd.shutdown()
    os.unlink(db_path)
    shutil.rmtree(panel_dir, ignore_errors=True)


def _post(base, path, body, *, cookie=None, origin=None):
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read()), resp.headers.get("Set-Cookie")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), e.headers.get("Set-Cookie")


def _get(base, path, *, cookie=None):
    req = urllib.request.Request(base + path)
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _cookie_pair(set_cookie: str) -> str:
    return set_cookie.split(";", 1)[0]


def test_signup_creates_user_and_sets_session_cookie(server):
    code, resp, set_cookie = _post(server["base"], "/api/auth/signup",
                                   {"email": "a@b.com", "password": "longenough1", "companyName": "Co"})
    assert code == 200 and resp["ok"] is True
    assert resp["data"]["user"]["email"] == "a@b.com"
    assert resp["data"]["user"]["id"] >= 1
    assert set_cookie and "rr_session=" in set_cookie
    assert "HttpOnly" in set_cookie and "SameSite=Lax" in set_cookie and "Path=/" in set_cookie


def test_signup_rejects_short_password(server):
    code, resp, _ = _post(server["base"], "/api/auth/signup",
                          {"email": "a@b.com", "password": "short"})
    assert code == 400 and resp["ok"] is False and "at least" in resp["error"]


def test_signup_accepts_eight_char_password(server):
    code, _, _ = _post(server["base"], "/api/auth/signup",
                       {"email": "edge@b.com", "password": "12345678", "companyName": "Co"})
    assert code == 200


def test_signup_rejects_bad_email(server):
    code, resp, _ = _post(server["base"], "/api/auth/signup",
                          {"email": "not-an-email", "password": "longenough1", "companyName": "Co"})
    assert code == 400 and "email" in resp["error"]


def test_signup_duplicate_email_conflicts(server):
    _post(server["base"], "/api/auth/signup", {"email": "dup@b.com", "password": "longenough1", "companyName": "Co"})
    code, resp, _ = _post(server["base"], "/api/auth/signup",
                          {"email": "dup@b.com", "password": "longenough1", "companyName": "Co"})
    assert code == 409 and resp["ok"] is False


def test_signup_normalizes_email_case(server):
    _post(server["base"], "/api/auth/signup", {"email": "Mixed@CASE.com", "password": "longenough1", "companyName": "Co"})
    # Login with a differently-cased address resolves to the same account.
    code, resp, set_cookie = _post(server["base"], "/api/auth/login",
                                   {"email": "mixed@case.com", "password": "longenough1", "companyName": "Co"})
    assert code == 200 and resp["data"]["user"]["email"] == "mixed@case.com"
    assert set_cookie and "rr_session=" in set_cookie


def test_login_success_and_wrong_password(server):
    _post(server["base"], "/api/auth/signup", {"email": "u@b.com", "password": "longenough1", "companyName": "Co"})
    ok_code, ok_resp, set_cookie = _post(server["base"], "/api/auth/login",
                                         {"email": "u@b.com", "password": "longenough1", "companyName": "Co"})
    assert ok_code == 200 and ok_resp["ok"] is True and "rr_session=" in set_cookie

    bad_code, bad_resp, _ = _post(server["base"], "/api/auth/login",
                                  {"email": "u@b.com", "password": "WRONGpassword"})
    assert bad_code == 401 and bad_resp["ok"] is False


def test_login_unknown_email_uses_same_error_as_wrong_password(server):
    code, resp, _ = _post(server["base"], "/api/auth/login",
                          {"email": "ghost@b.com", "password": "longenough1", "companyName": "Co"})
    assert code == 401 and resp["error"] == "invalid email or password"


def test_me_requires_session_then_returns_user(server):
    miss_code, _ = _get(server["base"], "/api/auth/me")
    assert miss_code == 401

    _, _, set_cookie = _post(server["base"], "/api/auth/signup",
                             {"email": "me@b.com", "password": "longenough1", "companyName": "Co"})
    code, resp = _get(server["base"], "/api/auth/me", cookie=_cookie_pair(set_cookie))
    assert code == 200 and resp["data"]["user"]["email"] == "me@b.com"


def test_logout_invalidates_the_session(server):
    _, _, set_cookie = _post(server["base"], "/api/auth/signup",
                             {"email": "out@b.com", "password": "longenough1", "companyName": "Co"})
    cookie = _cookie_pair(set_cookie)
    assert _get(server["base"], "/api/auth/me", cookie=cookie)[0] == 200

    code, resp, clear = _post(server["base"], "/api/auth/logout", {}, cookie=cookie)
    assert code == 200 and resp["data"]["loggedOut"] is True
    assert clear and "Max-Age=0" in clear
    # The token is dead server-side — the stale cookie no longer authorizes.
    assert _get(server["base"], "/api/auth/me", cookie=cookie)[0] == 401


def test_state_requires_auth(server):
    assert _get(server["base"], "/api/state")[0] == 401
    _, _, set_cookie = _post(server["base"], "/api/auth/signup",
                             {"email": "state@b.com", "password": "longenough1", "companyName": "Co"})
    code, _ = _get(server["base"], "/api/state", cookie=_cookie_pair(set_cookie))
    assert code == 200


def test_write_endpoint_requires_auth(server):
    code, resp, _ = _post(server["base"], "/api/status",
                          {"campaignId": "x", "commentId": "c1", "status": "confirmed"})
    assert code == 401 and resp["error"] == "authentication required"


def test_login_throttle_locks_out_after_repeated_failures(server):
    _post(server["base"], "/api/auth/signup", {"email": "brute@b.com", "password": "longenough1", "companyName": "Co"})
    seen_429 = False
    for _ in range(6):
        code, _, _ = _post(server["base"], "/api/auth/login",
                           {"email": "brute@b.com", "password": "WRONGpassword"})
        if code == 429:
            seen_429 = True
            break
        assert code == 401
    assert seen_429, "expected a 429 lockout after repeated failed logins"


def test_auth_signup_cross_origin_rejected(server):
    code, resp, _ = _post(server["base"], "/api/auth/signup",
                          {"email": "x@b.com", "password": "longenough1", "companyName": "Co"},
                          origin="https://evil.example")
    assert code == 403 and resp["ok"] is False


def test_loopback_lookalike_origin_is_rejected(server):
    # http://127.0.0.1.evil.com must NOT satisfy the loopback origin check.
    code, resp, _ = _post(server["base"], "/api/auth/signup",
                          {"email": "x@b.com", "password": "longenough1", "companyName": "Co"},
                          origin="http://127.0.0.1.evil.com")
    assert code == 403 and resp["ok"] is False


def test_successful_login_resets_the_failure_lockout_counter(server):
    base = server["base"]
    _post(base, "/api/auth/signup", {"email": "reset@b.com", "password": "longenough1", "companyName": "Co"})
    # Four failures — one short of the 5-failure lockout.
    for _ in range(4):
        code, _, _ = _post(base, "/api/auth/login",
                           {"email": "reset@b.com", "password": "WRONGpass1"})
        assert code == 401
    # A correct login clears the accumulated failures.
    ok_code, _, _ = _post(base, "/api/auth/login",
                          {"email": "reset@b.com", "password": "longenough1", "companyName": "Co"})
    assert ok_code == 200
    # Four MORE failures must still not lock out (4+4 would have, had it not reset).
    for _ in range(4):
        code, _, _ = _post(base, "/api/auth/login",
                           {"email": "reset@b.com", "password": "WRONGpass1"})
        assert code == 401


def test_login_runs_exactly_one_verify_for_unknown_email(server, monkeypatch):
    """Anti-enumeration: an unknown email still runs one (dummy) verify."""
    import aizu.server as srv
    calls = []
    real = srv.verify_password

    def counting(password, stored):
        calls.append(1)
        return real(password, stored)

    monkeypatch.setattr(srv, "verify_password", counting)
    _post(server["base"], "/api/auth/login", {"email": "ghost@b.com", "password": "longenough1", "companyName": "Co"})
    assert len(calls) == 1


def test_logout_without_a_session_still_succeeds(server):
    code, resp, clear = _post(server["base"], "/api/auth/logout", {})
    assert code == 200 and resp["data"]["loggedOut"] is True
    assert clear and "Max-Age=0" in clear


def test_unknown_cookie_token_is_rejected_not_500(server):
    status, _ = _get(server["base"], "/api/state", cookie="rr_session=not-a-real-token")
    assert status == 401


def test_expired_session_cookie_is_rejected(server):
    _, resp, _ = _post(server["base"], "/api/auth/signup",
                       {"email": "exp@b.com", "password": "longenough1", "companyName": "Co"})
    user_id = resp["data"]["user"]["id"]
    store = Store(server["db"])
    store.create_auth_session("expired-raw-token", user_id, time.time() - 1)
    store.close()
    status, _ = _get(server["base"], "/api/state", cookie="rr_session=expired-raw-token")
    assert status == 401
