"""HTTP-level per-org connection flows (Phase 2 YouTube + Phase 3 Telegram).

A real bridge server with AIZU_SECRET_KEY set, an authed owner, and a stubbed
live-validation surface (no network). Asserts the secret is captured encrypted and
the integration row flips connected — and that a bad key is rejected with a 400.
"""
import json
import os
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import types

from aizu import connections
from aizu.secrets import SecretCipher
from aizu.server import PanelHandler, serve
from aizu.core.store import Store
from aizu.engines.telegram.login import TelegramLoginManager

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
_INDEX_HTML = '<!doctype html><html><body><div id="root"></div></body></html>'
PW = "longenough1"


def _req(method, base, path, body=None, cookie=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or "null"), resp.headers.get("Set-Cookie")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or "null"), e.headers.get("Set-Cookie")


def _post(base, path, body, cookie=None):
    return _req("POST", base, path, body, cookie)


def _cookie(set_cookie):
    return set_cookie.split(";", 1)[0]


@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setenv("AIZU_SECRET_KEY", SecretCipher.generate_key())
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    panel_dir = tempfile.mkdtemp(suffix="-spa")
    (Path(panel_dir) / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
    httpd = serve(db_path, panel_dir, str(CONFIG), port=0, run_manager=None)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    _, resp, cookie = _post(base, "/api/auth/signup",
                            {"email": "owner@x.io", "password": PW, "companyName": "Acme"})
    org_id = resp["data"]["user"]["orgId"]
    ctx = {"base": base, "db": db_path, "cookie": _cookie(cookie), "org_id": org_id}
    yield ctx
    httpd.shutdown()
    os.unlink(db_path)
    shutil.rmtree(panel_dir, ignore_errors=True)


# ----- YouTube connect (Phase 2) -----

def test_youtube_connect_validates_stores_secret_and_marks_connected(server, monkeypatch):
    seen = {}
    monkeypatch.setattr(connections, "validate_youtube_api_key",
                        lambda key: seen.update(key=key))      # accept (no raise)

    code, resp, _ = _post(server["base"], "/api/integration",
                          {"platform": "youtube", "apiKey": "AIza-good"}, server["cookie"])

    assert code == 200, resp
    assert resp["data"]["connected"] == 1
    assert seen["key"] == "AIza-good"                          # validated live first
    # the secret is captured (encrypted at rest) and decryptable with the org key
    store = Store(server["db"], secret_cipher=SecretCipher.from_env())
    try:
        assert store.get_integration_secret(server["org_id"], "youtube") == {"api_key": "AIza-good"}
    finally:
        store.close()


def test_youtube_connect_rejects_invalid_key_with_400(server, monkeypatch):
    def _reject(_key):
        raise connections.ConnectionValidationError("YouTube rejected the API key")
    monkeypatch.setattr(connections, "validate_youtube_api_key", _reject)

    code, resp, _ = _post(server["base"], "/api/integration",
                          {"platform": "youtube", "apiKey": "AIza-bad"}, server["cookie"])

    assert code == 400
    assert "rejected" in resp["error"]
    store = Store(server["db"], secret_cipher=SecretCipher.from_env())
    try:
        assert store.get_integration_secret(server["org_id"], "youtube") is None  # nothing stored
    finally:
        store.close()


def test_apikey_rejected_for_non_youtube_platform(server):
    code, resp, _ = _post(server["base"], "/api/integration",
                          {"platform": "telegram", "apiKey": "x"}, server["cookie"])
    assert code == 400
    assert "youtube" in resp["error"]


def test_disconnect_revokes_the_stored_secret(server, monkeypatch):
    monkeypatch.setattr(connections, "validate_youtube_api_key", lambda key: None)
    _post(server["base"], "/api/integration",
          {"platform": "youtube", "apiKey": "AIza-good"}, server["cookie"])

    code, resp, _ = _post(server["base"], "/api/integration",
                          {"platform": "youtube", "connected": False}, server["cookie"])

    assert code == 200, resp
    assert resp["data"]["connected"] == 0
    store = Store(server["db"], secret_cipher=SecretCipher.from_env())
    try:
        assert store.get_integration_secret(server["org_id"], "youtube") is None  # revoked
    finally:
        store.close()


# ----- Reddit connect (single-step app-credential) -----

_REDDIT_BODY = {"platform": "reddit", "clientId": "cid", "clientSecret": "sec",
                "userAgent": "aizu:lead-agent:v1 (by /u/me)"}


def test_reddit_connect_validates_stores_secret_and_marks_connected(server, monkeypatch):
    seen = {}
    monkeypatch.setattr(connections, "validate_reddit_credentials",
                        lambda cid, sec, ua: seen.update(cid=cid, sec=sec, ua=ua))

    code, resp, _ = _post(server["base"], "/api/integration", _REDDIT_BODY, server["cookie"])

    assert code == 200, resp
    assert resp["data"]["connected"] == 1
    assert seen == {"cid": "cid", "sec": "sec", "ua": "aizu:lead-agent:v1 (by /u/me)"}
    store = Store(server["db"], secret_cipher=SecretCipher.from_env())
    try:
        assert store.get_integration_secret(server["org_id"], "reddit") == {
            "client_id": "cid", "client_secret": "sec",
            "user_agent": "aizu:lead-agent:v1 (by /u/me)"}
    finally:
        store.close()


def test_reddit_connect_rejects_invalid_credentials_with_400(server, monkeypatch):
    def _reject(_cid, _sec, _ua):
        raise connections.ConnectionValidationError("Reddit rejected the app credentials")
    monkeypatch.setattr(connections, "validate_reddit_credentials", _reject)

    code, resp, _ = _post(server["base"], "/api/integration", _REDDIT_BODY, server["cookie"])

    assert code == 400
    assert "rejected" in resp["error"]
    store = Store(server["db"], secret_cipher=SecretCipher.from_env())
    try:
        assert store.get_integration_secret(server["org_id"], "reddit") is None
    finally:
        store.close()


def test_reddit_credentials_rejected_for_non_reddit_platform(server):
    code, resp, _ = _post(server["base"], "/api/integration",
                          {"platform": "youtube", "clientId": "x", "clientSecret": "y",
                           "userAgent": "z"}, server["cookie"])
    assert code == 400
    assert "reddit" in resp["error"]


def test_reddit_connect_requires_all_three_fields(server, monkeypatch):
    monkeypatch.setattr(connections, "validate_reddit_credentials", lambda *a: None)
    code, resp, _ = _post(server["base"], "/api/integration",
                          {"platform": "reddit", "clientId": "cid", "clientSecret": "sec"},
                          server["cookie"])
    assert code == 400
    assert "userAgent" in resp["error"]


# ----- Telegram connect wizard (Phase 3) -----

class _FakeTelethonClient:
    def __init__(self, *, password_needed=False):
        self._password_needed = password_needed
        self.sign_ins = []

    def send_code_request(self, phone):
        return types.SimpleNamespace(phone_code_hash="h")

    def sign_in(self, *args, **kwargs):
        if "password" in kwargs:
            self.sign_ins.append(("password", kwargs["password"]))
            return
        self.sign_ins.append(("code",))
        if self._password_needed:
            class SessionPasswordNeededError(Exception):
                pass
            raise SessionPasswordNeededError()


def _install_fake_manager(monkeypatch, *, password_needed=False):
    manager = TelegramLoginManager(
        client_factory=lambda api_id, api_hash: _FakeTelethonClient(password_needed=password_needed),
        session_saver=lambda c: "SESSION_STR",
        api_provider=lambda: (7, "app-hash"),
    )
    monkeypatch.setattr(PanelHandler, "telegram_login", manager)
    return manager


def test_telegram_wizard_connects_and_stores_the_session(server, monkeypatch):
    _install_fake_manager(monkeypatch)

    code, resp, _ = _post(server["base"], "/api/integration/telegram/start",
                          {"phone": "+14155550142"}, server["cookie"])
    assert code == 200, resp
    token = resp["data"]["token"]

    code, resp, _ = _post(server["base"], "/api/integration/telegram/verify",
                          {"token": token, "code": "12345"}, server["cookie"])
    assert code == 200, resp
    assert resp["data"]["needsPassword"] is False
    store = Store(server["db"], secret_cipher=SecretCipher.from_env())
    try:
        assert store.get_integration_secret(server["org_id"], "telegram") == {
            "api_id": 7, "api_hash": "app-hash", "session": "SESSION_STR"}
        assert any(i["platform"] == "telegram" and i["connected"]
                   for i in store.list_integrations(server["org_id"]))
    finally:
        store.close()


def test_telegram_wizard_prompts_for_2fa_then_connects(server, monkeypatch):
    _install_fake_manager(monkeypatch, password_needed=True)

    _, start, _ = _post(server["base"], "/api/integration/telegram/start",
                        {"phone": "+14155550142"}, server["cookie"])
    token = start["data"]["token"]

    # code only → server asks for the 2FA password, nothing stored yet
    code, resp, _ = _post(server["base"], "/api/integration/telegram/verify",
                          {"token": token, "code": "12345"}, server["cookie"])
    assert code == 200 and resp["data"]["needsPassword"] is True
    store = Store(server["db"], secret_cipher=SecretCipher.from_env())
    try:
        assert store.get_integration_secret(server["org_id"], "telegram") is None
    finally:
        store.close()

    # resubmit with the password → connected
    code, resp, _ = _post(server["base"], "/api/integration/telegram/verify",
                          {"token": token, "code": "12345", "password": "pw"}, server["cookie"])
    assert code == 200 and resp["data"]["needsPassword"] is False
    store = Store(server["db"], secret_cipher=SecretCipher.from_env())
    try:
        assert store.get_integration_secret(server["org_id"], "telegram")["session"] == "SESSION_STR"
    finally:
        store.close()


def test_telegram_verify_with_expired_token_is_rejected(server, monkeypatch):
    manager = TelegramLoginManager(
        client_factory=lambda api_id, api_hash: _FakeTelethonClient(),
        session_saver=lambda c: "SESSION_STR",
        api_provider=lambda: (7, "app-hash"),
        ttl_seconds=-1.0,                                  # any pending is already stale
    )
    monkeypatch.setattr(PanelHandler, "telegram_login", manager)
    _, start, _ = _post(server["base"], "/api/integration/telegram/start",
                        {"phone": "+14155550142"}, server["cookie"])
    code, resp, _ = _post(server["base"], "/api/integration/telegram/verify",
                          {"token": start["data"]["token"], "code": "12345"}, server["cookie"])
    assert code == 400
    assert "expired" in resp["error"]
