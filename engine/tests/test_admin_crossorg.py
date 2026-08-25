"""HTTP-level cross-org read views (Phase 5d): the admin org index and per-org
campaigns/leads reads, gated by the real admin plane (no impersonation, read-only)."""
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
    org_a, org_cookie_a = _signup(base, "a@x.io", "AcmeCorp")
    org_b, _ = _signup(base, "b@x.io", "BetaLtd")
    cipher = SecretCipher(KEY)
    store = Store(db_path, secret_cipher=cipher)
    try:
        store.upsert_campaign_meta("camp-a", org_id=org_a, display_name="AlphaCampaign")
        store.upsert_campaign_brief("camp-a", {"platform": "instagram"}, org_id=org_a)
        store.upsert_campaign_meta("camp-b", org_id=org_b, display_name="BetaCampaign")
        store.upsert_campaign_brief("camp-b", {"platform": "instagram"}, org_id=org_b)
        # One real lead for org B: the leads read used to be asserted on shape alone,
        # against an EMPTY array — which cannot tell a superadmin payload that keeps
        # the identity from one that redacts it like the org plane now does (v27).
        store.upsert_match(campaign_id="camp-b", reel_id="r1", comment_id="b-c1",
                           username="aziz", text="how much for the red ones?",
                           lang="en", score=0.9, reason="asked price", extracted=None,
                           tier="local", platform="instagram",
                           intent="Wants a price for the red sneakers")
        secret = admin_auth.generate_totp_secret()
        store.create_platform_admin(email="ops@x.io", password_hash=hash_password(PW),
                                    mfa_secret=cipher.encrypt({"totp": secret}))
    finally:
        store.close()
    yield {"base": base, "db": db_path, "secret": secret,
           "org_a": org_a, "org_b": org_b, "org_cookie_a": org_cookie_a}
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
    """Fresh unique admin per call (TOTP anti-replay forbids code reuse in-window)."""
    _ADMIN_SEQ[0] += 1
    email = f"cross-adm{_ADMIN_SEQ[0]}@x.io"
    secret = _seed_admin(srv["db"], email)
    status, _, headers = _req("POST", srv["base"], "/api/admin/login",
                              {"email": email, "password": PW,
                               "totpCode": admin_auth.totp_now(secret)})
    assert status == 200
    return headers.get("Set-Cookie").split(";", 1)[0]


# ----- gate -----

def test_orgs_index_requires_admin(srv):
    status, _, _ = _req("GET", srv["base"], "/api/admin/orgs")
    assert status == 401
    status, _, _ = _req("GET", srv["base"], "/api/admin/orgs",
                        cookie=srv["org_cookie_a"])  # org cookie ≠ admin
    assert status == 401


# ----- org index -----

def test_orgs_index_lists_all_with_counts(srv):
    cookie = _admin_cookie(srv)
    status, resp, _ = _req("GET", srv["base"], "/api/admin/orgs", cookie=cookie)
    assert status == 200, resp
    by_name = {o["name"]: o for o in resp["data"]["orgs"]}
    assert {"AcmeCorp", "BetaLtd"} <= set(by_name)
    assert by_name["AcmeCorp"]["member_count"] == 1
    assert by_name["AcmeCorp"]["campaign_count"] == 1


# ----- per-org read views (read-only, no impersonation) -----

def test_org_campaigns_read_is_scoped(srv):
    cookie = _admin_cookie(srv)
    status, resp, _ = _req("GET", srv["base"],
                           f"/api/admin/orgs/{srv['org_a']}/campaigns", cookie=cookie)
    assert status == 200, resp
    blob = json.dumps(resp["data"])
    assert "AlphaCampaign" in blob and "BetaCampaign" not in blob


def test_org_campaigns_read_matches_admin_contract(srv):
    """The panel's adminOrgCampaignSchema requires a `campaigns` array whose rows
    carry displayName/platform/status/archived — NOT the org-plane CAMPAIGNS shape."""
    cookie = _admin_cookie(srv)
    status, resp, _ = _req("GET", srv["base"],
                           f"/api/admin/orgs/{srv['org_a']}/campaigns", cookie=cookie)
    assert status == 200, resp
    data = resp["data"]
    assert set(data.keys()) == {"campaigns"}, data  # no CAMPAIGNS/SESSIONS leak
    row = next(c for c in data["campaigns"] if c["id"] == "camp-a")
    assert row["displayName"] == "AlphaCampaign"
    assert row["platform"] == "instagram"
    assert isinstance(row["status"], str)
    assert row["archived"] is False
    assert isinstance(row["createdAt"], (int, float))


def test_org_leads_read_returns_paginated_shape(srv):
    cookie = _admin_cookie(srv)
    status, resp, _ = _req("GET", srv["base"],
                           f"/api/admin/orgs/{srv['org_b']}/leads?page=1&pageSize=10",
                           cookie=cookie)
    assert status == 200, resp
    assert resp["ok"] is True and resp["data"] is not None


def test_org_leads_read_matches_admin_contract(srv):
    """The panel's adminOrgLeadsResponseSchema requires a flat `leads` array plus
    page/pageSize/total — NOT the org-plane `items`/stats/CONFIG shape."""
    cookie = _admin_cookie(srv)
    status, resp, _ = _req("GET", srv["base"],
                           f"/api/admin/orgs/{srv['org_b']}/leads?page=1&pageSize=10",
                           cookie=cookie)
    assert status == 200, resp
    data = resp["data"]
    assert set(data.keys()) == {"leads", "page", "pageSize", "total"}, data
    assert isinstance(data["leads"], list)
    assert data["page"] == 1 and data["pageSize"] == 10
    assert isinstance(data["total"], int)


def test_admin_org_leads_keep_the_identity_the_org_plane_hides(srv):
    """v27 splits the two lead views apart, and this is the half that must NOT be
    redacted: the superadmin sees the handle and the raw comment BESIDE the derived
    intent. Without the raw evidence there is no way to tell a good intent line from
    one that quietly leaked the comment, which is the only reason this plane exists.
    """
    cookie = _admin_cookie(srv)
    status, resp, _ = _req("GET", srv["base"],
                           f"/api/admin/orgs/{srv['org_b']}/leads?page=1&pageSize=10",
                           cookie=cookie)
    assert status == 200, resp
    lead = next(m for m in resp["data"]["leads"] if m["commentId"] == "b-c1")
    assert lead["username"] == "aziz"
    assert lead["text"] == "how much for the red ones?"
    assert lead["intent"] == "Wants a price for the red sneakers"


def test_admin_org_leads_search_still_spans_the_handle(srv):
    """The org plane's search deliberately no longer answers for a username (it would
    be an oracle for the identity the payload hides). The superadmin plane must keep
    it — a support request usually arrives AS a handle."""
    cookie = _admin_cookie(srv)
    status, resp, _ = _req("GET", srv["base"],
                           f"/api/admin/orgs/{srv['org_b']}/leads?q=aziz",
                           cookie=cookie)
    assert status == 200, resp
    assert [m["commentId"] for m in resp["data"]["leads"]] == ["b-c1"]


def test_org_read_unknown_org_is_404(srv):
    cookie = _admin_cookie(srv)
    status, _, _ = _req("GET", srv["base"], "/api/admin/orgs/999999/campaigns",
                        cookie=cookie)
    assert status == 404


def test_cross_org_read_does_not_start_impersonation(srv):
    cookie = _admin_cookie(srv)
    _req("GET", srv["base"], f"/api/admin/orgs/{srv['org_a']}/campaigns", cookie=cookie)
    _, who, _ = _req("GET", srv["base"], "/api/admin/whoami", cookie=cookie)
    assert who["data"]["admin"]["impersonating"] is False


def test_org_read_bad_subresource_is_404(srv):
    cookie = _admin_cookie(srv)
    status, _, _ = _req("GET", srv["base"], f"/api/admin/orgs/{srv['org_a']}/secrets",
                        cookie=cookie)
    assert status == 404
