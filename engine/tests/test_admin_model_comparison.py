"""HTTP-level superadmin model-comparison switch + stats (v17): the on/off toggle
for the LLM fan-out and the aggregate/raw reads the Model Performance page uses.
Mirrors the execution-backend route tests' shape (same real admin plane gate)."""
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
    yield {"base": base, "db": db_path}
    httpd.shutdown()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(ADMIN_IP_ALLOWLIST_ENV, "127.0.0.1,::1")
    monkeypatch.setenv(SECRET_KEY_ENV, KEY)
    monkeypatch.setenv("MODEL_COMPARISON_MODELS", "candidate-a,candidate-b")


_ADMIN_SEQ = [0]


def _admin_cookie(srv):
    """Fresh unique admin per call (TOTP anti-replay forbids code reuse in-window)."""
    _ADMIN_SEQ[0] += 1
    email = f"mc-adm{_ADMIN_SEQ[0]}@x.io"
    secret = _seed_admin(srv["db"], email)
    status, _, headers = _req("POST", srv["base"], "/api/admin/login",
                              {"email": email, "password": PW,
                               "totpCode": admin_auth.totp_now(secret)})
    assert status == 200
    return email, headers.get("Set-Cookie").split(";", 1)[0]


@pytest.fixture(autouse=True)
def _reset_toggle(srv):
    """Every test starts from the off default — tests run against a shared module DB."""
    store = Store(srv["db"])
    try:
        store.set_model_comparison_enabled(False)
    finally:
        store.close()
    yield


# ----- gate -----

def test_get_requires_admin(srv):
    status, _, _ = _req("GET", srv["base"], "/api/admin/model-comparison")
    assert status == 401


def test_set_requires_admin(srv):
    status, _, _ = _req("POST", srv["base"], "/api/admin/model-comparison",
                        {"enabled": True})
    assert status == 401


def test_stats_requires_admin(srv):
    status, _, _ = _req("GET", srv["base"], "/api/admin/model-comparison/stats")
    assert status == 401


# ----- GET: current state + env-configured models -----

def test_get_reports_disabled_by_default_with_env_models_listed(srv):
    _, cookie = _admin_cookie(srv)
    status, resp, _ = _req("GET", srv["base"], "/api/admin/model-comparison",
                           cookie=cookie)
    assert status == 200
    assert resp["data"]["enabled"] is False
    assert resp["data"]["models"] == ["candidate-a", "candidate-b"]


# ----- POST: toggling -----

def test_set_enabled_persists_and_get_reflects_it(srv):
    email, cookie = _admin_cookie(srv)
    status, resp, _ = _req("POST", srv["base"], "/api/admin/model-comparison",
                           {"enabled": True}, cookie=cookie)
    assert status == 200
    assert resp["data"]["enabled"] is True

    status, resp, _ = _req("GET", srv["base"], "/api/admin/model-comparison",
                           cookie=cookie)
    assert resp["data"]["enabled"] is True

    store = Store(srv["db"])
    try:
        assert store.model_comparison_enabled() is True
    finally:
        store.close()


def test_set_enabled_rejects_a_non_boolean_body(srv):
    _, cookie = _admin_cookie(srv)
    status, resp, _ = _req("POST", srv["base"], "/api/admin/model-comparison",
                           {"enabled": "yes"}, cookie=cookie)
    assert status == 400


def test_set_enabled_is_audited(srv):
    email, cookie = _admin_cookie(srv)
    _req("POST", srv["base"], "/api/admin/model-comparison", {"enabled": True},
        cookie=cookie)
    store = Store(srv["db"])
    try:
        rows = store.list_admin_audit(limit=50)
        entry = next(r for r in rows if r["action"] == "model_comparison.set")
        assert entry["target_resource"] == "True"
    finally:
        store.close()


# ----- GET stats -----

def test_stats_returns_aggregated_and_recent_rows(srv):
    _, cookie = _admin_cookie(srv)
    store = Store(srv["db"])
    try:
        store.log_model_comparison(campaign_id="c1", stage="match", model="prod-model",
                                   is_primary=True, score=0.9, latency_ms=400.0, usd=0.002)
        store.log_model_comparison(campaign_id="c1", stage="match", model="candidate-a",
                                   is_primary=False, score=0.8, agreed=True,
                                   latency_ms=900.0, usd=0.0005)
    finally:
        store.close()

    status, resp, _ = _req("GET", srv["base"], "/api/admin/model-comparison/stats",
                           cookie=cookie)
    assert status == 200
    by_model = {s["model"]: s for s in resp["data"]["stats"]}
    assert by_model["prod-model"]["isPrimary"] is True
    assert by_model["candidate-a"]["agreementRate"] == 1.0
    assert len(resp["data"]["recent"]) >= 2
