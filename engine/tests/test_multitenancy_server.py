"""HTTP-level multi-tenancy + RBAC: the real enforcement gate. Covers the per-route
authorization matrix, cross-org isolation, the invite→accept flow, last-owner and
owner-only-admin guards, and role-based /api/state pruning."""
import json
import os
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from reelradar import billing
from reelradar.billing import CheckoutResult, PolarClient, PolarConfig, PortalResult
from reelradar.server import serve
from reelradar.core.store import Store

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


def _get(base, path, cookie=None):
    code, resp, _ = _req("GET", base, path, None, cookie)
    return code, resp


def _cookie(set_cookie):
    return set_cookie.split(";", 1)[0]


def _lead_status(db, campaign_id, comment_id):
    """The persisted status of a single lead, read straight from the store."""
    store = Store(db)
    try:
        match = next(m for m in store.matches(campaign_id) if m["comment_id"] == comment_id)
        return match["status"]
    finally:
        store.close()


def _note_count(db, campaign_id, comment_id):
    store = Store(db)
    try:
        return len(store.notes_for(campaign_id, comment_id))
    finally:
        store.close()


def _campaign_status(db, campaign_id):
    store = Store(db)
    try:
        meta = store.get_campaign_meta(campaign_id)
        return meta["status"] if meta else None
    finally:
        store.close()


def _setting(db, org_id, key):
    store = Store(db)
    try:
        return store.get_settings(org_id).get(key)
    finally:
        store.close()


def _integration_connected(db, org_id, platform):
    store = Store(db)
    try:
        row = next((i for i in store.list_integrations(org_id) if i["platform"] == platform), None)
        return None if row is None else row.get("connected")
    finally:
        store.close()


def _org_user_count(db, org_id):
    store = Store(db)
    try:
        return len(store.list_org_users(org_id))
    finally:
        store.close()


def _signup(base, email, company="Co", invite=None):
    body = {"email": email, "password": PW}
    if invite:
        body["inviteToken"] = invite
    else:
        body["companyName"] = company
    code, resp, set_cookie = _post(base, "/api/auth/signup", body)
    return code, resp, (_cookie(set_cookie) if set_cookie else None)


def _login(base, email):
    code, resp, set_cookie = _post(base, "/api/auth/login", {"email": email, "password": PW})
    assert code == 200, resp
    return _cookie(set_cookie)


class _StubRunManager:
    """A run_manager that records launches instead of spawning the engine. Lets the
    /api/run handler run past its `run_manager is None` 503 guard so the cross-org
    and role gates are the thing actually under test; `launched` must stay empty
    whenever a request is rejected."""

    def __init__(self):
        self.launched: list = []

    def launch(self, spec):
        self.launched.append(spec)
        return object(), None

    def status(self, org_id=None):  # /api/state reads this; no active run in tests
        return {"active": None, "recent": []}

    def sweep_orphan_pause_files(self):  # called once by serve() at startup
        return None


class _FakePolar(PolarClient):
    """Real Polar verify/parse (exercises the actual Standard-Webhooks crypto), but
    create_checkout/create_portal are stubbed so tests never hit the network."""

    def create_checkout(self, tier, interval, org_id, email, success_url):
        return CheckoutResult(url=f"https://checkout.test/{tier}/{interval}?org={org_id}")

    def create_portal(self, org_id):
        return PortalResult(url=f"https://portal.test/{org_id}", has_account=True)


# A known signing secret + product map so tests can sign their own webhooks.
_WEBHOOK_SECRET = "whsec_dGVzdC1wb2xhci1zaWduaW5nLXNlY3JldC0wMDE="
_FAKE_POLAR = _FakePolar(PolarConfig(
    access_token="polar_at_test", webhook_secret=_WEBHOOK_SECRET, server="sandbox",
    products={"lite": {"month": "p_lite_m", "year": "p_lite_y"},
              "starter": {"month": "p_starter_m", "year": "p_starter_y"},
              "pro": {"month": "p_pro_m", "year": "p_pro_y"}}))


def _signed_webhook(base, body_obj, *, event_ts="2026-06-15T12:00:00Z"):
    """Sign a webhook body with the fake provider's key and POST it to the bridge."""
    import time as _time
    body = json.dumps(body_obj).encode()
    wid = f"msg_{int(_time.time()*1000)}"
    wts = str(int(_time.time()))
    signed = wid.encode() + b"." + wts.encode() + b"." + body
    sig = billing._b64_hmac(_FAKE_POLAR._sig_keys[0], signed)
    req = urllib.request.Request(
        base + "/api/billing/webhook", data=body, method="POST",
        headers={"Content-Type": "application/json", "webhook-id": wid,
                 "webhook-timestamp": wts, "webhook-signature": f"v1,{sig}"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or "null")


def _sub_event(org_id, *, status="active", tier_product="p_starter_m",
               modified_at="2026-06-15T12:00:00Z", event_type="subscription.updated",
               **extra):
    data = {"id": f"sub_{org_id}", "status": status, "product_id": tier_product,
            "external_customer_id": str(org_id), "customer_id": f"cus_{org_id}",
            "current_period_start": "2026-06-01T00:00:00Z",
            "current_period_end": "2026-07-01T00:00:00Z",
            "modified_at": modified_at, "cancel_at_period_end": False,
            "recurring_interval": "month"}
    data.update(extra)
    return {"type": event_type, "data": data}


@pytest.fixture(scope="module")
def mt():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    panel_dir = tempfile.mkdtemp(suffix="-spa")
    (Path(panel_dir) / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
    run_manager = _StubRunManager()
    httpd = serve(db_path, panel_dir, str(CONFIG), port=0, run_manager=run_manager,
                  billing_providers={"polar": _FAKE_POLAR})
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    # --- Org A: owner + an admin/member/viewer (direct-added by the owner) ---
    _, a_resp, a_owner = _signup(base, "a-owner@x.io", company="Acme")
    org_a = a_resp["data"]["user"]["orgId"]
    cookies = {"owner": a_owner}
    for role in ("admin", "member", "viewer"):
        code, _, _ = _post(base, "/api/team",
                           {"op": "create", "email": f"a-{role}@x.io", "password": PW,
                            "role": role}, a_owner)
        assert code == 200
        cookies[role] = _login(base, f"a-{role}@x.io")

    # --- Org B: a separate company with its own owner + campaign + lead ---
    _, b_resp, b_owner = _signup(base, "b-owner@x.io", company="Beta")
    org_b = b_resp["data"]["user"]["orgId"]

    # Seed a campaign + one lead in each org (directly, stamped with org_id).
    store = Store(db_path)
    for cid, org in (("camp-a", org_a), ("camp-b", org_b)):
        store.upsert_campaign_meta(cid, org_id=org, status="live")
        store.upsert_campaign_brief(cid, {"platform": "instagram", "threshold": 0.7}, org_id=org)
        store.upsert_match(campaign_id=cid, reel_id="r", comment_id=f"cm-{cid}", username="u",
                           text="hi", lang="uz", score=0.9, reason="x", extracted=None, tier="local")
    store.close()

    yield {"base": base, "db": db_path, "cookies": cookies, "b_owner": b_owner,
           "org_a": org_a, "org_b": org_b, "run_manager": run_manager}
    httpd.shutdown()
    os.unlink(db_path)
    shutil.rmtree(panel_dir, ignore_errors=True)


# action → (route, method-payload) and the roles that MUST be allowed (others → 403).
# Payloads target org A's own campaign so the only thing under test is the role gate.
_ROUTE_CASES = [
    ("/api/status", {"campaignId": "camp-a", "commentId": "cm-camp-a", "status": "new"},
     {"owner", "admin", "member"}),
    # Bulk status changes (incl. archive) are owner/admin only — bulk_edit_leads.
    ("/api/status/bulk", {"campaignId": "camp-a", "status": "new",
                          "items": [{"commentId": "cm-camp-a"}]}, {"owner", "admin"}),
    ("/api/lead/note", {"op": "create", "campaignId": "camp-a", "commentId": "cm-camp-a",
                        "body": "hi"}, {"owner", "admin", "member"}),
    ("/api/campaign", {"campaignId": "camp-a", "status": "live"}, {"owner", "admin"}),
    ("/api/run", {"campaignId": "camp-a", "mode": "dry"}, {"owner", "admin"}),
    ("/api/settings", {"settings": {"productName": "X"}}, {"owner", "admin"}),
    ("/api/integration", {"platform": "instagram", "connected": True}, {"owner", "admin"}),
    ("/api/invite", {"op": "create", "role": "viewer"}, {"owner", "admin"}),
    # Billing checkout/portal are manage_billing → owner/admin only.
    ("/api/billing/checkout", {"tier": "starter", "interval": "month"}, {"owner", "admin"}),
    ("/api/billing/portal", {}, {"owner", "admin"}),
]


@pytest.mark.parametrize("path,payload,allowed", _ROUTE_CASES)
def test_route_authorization_matrix(mt, path, payload, allowed):
    for role, cookie in mt["cookies"].items():
        code, _, _ = _post(mt["base"], path, payload, cookie)
        if role in allowed:
            assert code != 403, f"{role} should be allowed on {path} (got {code})"
        else:
            assert code == 403, f"{role} must be forbidden on {path} (got {code})"


def test_unauthenticated_write_is_401(mt):
    code, _, _ = _post(mt["base"], "/api/status",
                       {"campaignId": "camp-a", "commentId": "cm-camp-a", "status": "new"})
    assert code == 401


# ----- cross-org isolation -----

def test_org_cannot_read_another_orgs_campaign(mt):
    # Org B owner asks for org A's campaign by id → 404 (existence not disclosed).
    code, resp = _get(mt["base"], "/api/state?campaign=camp-a", mt["b_owner"])
    assert code == 404


def test_org_cannot_write_another_orgs_leads(mt):
    code, _, _ = _post(mt["base"], "/api/status",
                       {"campaignId": "camp-a", "commentId": "cm-camp-a", "status": "interested",
                        "note": "x"}, mt["b_owner"])
    assert code == 404  # camp-a is not in org B


def test_org_cannot_edit_another_orgs_campaign(mt):
    code, _, _ = _post(mt["base"], "/api/campaign",
                       {"campaignId": "camp-a", "status": "paused"}, mt["b_owner"])
    assert code == 404


def test_settings_are_isolated_per_org(mt):
    _post(mt["base"], "/api/settings", {"settings": {"productName": "AName"}},
          mt["cookies"]["owner"])
    _post(mt["base"], "/api/settings", {"settings": {"productName": "BName"}}, mt["b_owner"])
    a_state = _get(mt["base"], "/api/state", mt["cookies"]["owner"])[1]
    b_state = _get(mt["base"], "/api/state", mt["b_owner"])[1]
    assert a_state["CONFIG"]["productName"] == "AName"
    assert b_state["CONFIG"]["productName"] == "BName"


# ----- role-based /api/state pruning -----

def test_member_state_is_leads_only(mt):
    state = _get(mt["base"], "/api/state", mt["cookies"]["member"])[1]
    assert "MATCHES" in state and "CONFIG" in state
    for hidden in ("DASHBOARD", "REPORTS", "TEAM", "INVITES", "INTEGRATIONS"):
        assert hidden not in state, f"member must not receive {hidden}"


def test_viewer_state_has_dashboard_but_no_team(mt):
    state = _get(mt["base"], "/api/state", mt["cookies"]["viewer"])[1]
    assert "DASHBOARD" in state and "CAMPAIGNS" in state and "REPORTS" in state
    assert "TEAM" not in state and "INTEGRATIONS" not in state


def test_owner_state_has_team_and_integrations(mt):
    state = _get(mt["base"], "/api/state", mt["cookies"]["owner"])[1]
    assert "TEAM" in state and "INTEGRATIONS" in state and "INVITES" in state


# ----- invite flow -----

def test_invite_create_then_accept_joins_org_with_role(mt):
    code, resp, _ = _post(mt["base"], "/api/invite",
                          {"op": "create", "role": "admin", "email": "invited@x.io"},
                          mt["cookies"]["owner"])
    assert code == 200
    token = resp["data"]["token"]
    # Public lookup shows the org branding + role before accepting.
    info_code, info = _get(mt["base"], f"/api/invite?token={token}")
    assert info_code == 200 and info["data"]["role"] == "admin" and info["data"]["valid"] is True
    # Accept by signing up with the invite token (company fields ignored).
    s_code, s_resp, cookie = _signup(mt["base"], "invited@x.io", invite=token)
    assert s_code == 200
    assert s_resp["data"]["user"]["role"] == "admin"
    assert s_resp["data"]["user"]["orgId"] == mt["org_a"]
    # The invite is single-use.
    again, _, _ = _signup(mt["base"], "invited2@x.io", invite=token)
    assert again == 400


# ----- team management guards -----

def test_admin_cannot_edit_another_admin(mt):
    # Add a second admin as the owner, then try to demote it as the first admin.
    _post(mt["base"], "/api/team",
          {"op": "create", "email": "a-admin2@x.io", "password": PW, "role": "admin"},
          mt["cookies"]["owner"])
    store = Store(mt["db"])
    target = next(u for u in store.list_org_users(mt["org_a"]) if u["email"] == "a-admin2@x.io")
    store.close()
    code, _, _ = _post(mt["base"], "/api/team",
                       {"op": "updateRole", "userId": target["id"], "role": "viewer"},
                       mt["cookies"]["admin"])
    assert code == 403  # only an owner manages admins
    # The owner can.
    ok, _, _ = _post(mt["base"], "/api/team",
                     {"op": "updateRole", "userId": target["id"], "role": "viewer"},
                     mt["cookies"]["owner"])
    assert ok == 200


def test_admin_can_add_admin_but_member_cannot_add_anyone(mt):
    code, _, _ = _post(mt["base"], "/api/team",
                       {"op": "create", "email": "a-admin3@x.io", "password": PW, "role": "admin"},
                       mt["cookies"]["admin"])
    assert code == 200  # admins may add admins (per spec)
    denied, _, _ = _post(mt["base"], "/api/team",
                         {"op": "create", "email": "nope@x.io", "password": PW, "role": "viewer"},
                         mt["cookies"]["member"])
    assert denied == 403


def test_admin_cannot_promote_member_to_admin(mt):
    # A throwaway member so we don't disturb the shared fixture roles.
    _post(mt["base"], "/api/team",
          {"op": "create", "email": "promo@x.io", "password": PW, "role": "member"},
          mt["cookies"]["owner"])
    store = Store(mt["db"])
    target = next(u for u in store.list_org_users(mt["org_a"]) if u["email"] == "promo@x.io")
    store.close()
    # Admin may NOT promote a member to admin (would create an admin it can't edit).
    code, _, _ = _post(mt["base"], "/api/team",
                       {"op": "updateRole", "userId": target["id"], "role": "admin"},
                       mt["cookies"]["admin"])
    assert code == 403
    # Admin MAY move a member it manages between member/viewer.
    ok, _, _ = _post(mt["base"], "/api/team",
                     {"op": "updateRole", "userId": target["id"], "role": "viewer"},
                     mt["cookies"]["admin"])
    assert ok == 200
    # Owner MAY promote to admin.
    ok2, _, _ = _post(mt["base"], "/api/team",
                      {"op": "updateRole", "userId": target["id"], "role": "admin"},
                      mt["cookies"]["owner"])
    assert ok2 == 200


# ----- cross-org WRITE is rejected AND leaves org B's data untouched -----

def test_cross_org_status_write_rejected_and_no_state_change(mt):
    before = _lead_status(mt["db"], "camp-a", "cm-camp-a")
    code, _, _ = _post(mt["base"], "/api/status",
                       {"campaignId": "camp-a", "commentId": "cm-camp-a",
                        "status": "interested"}, mt["b_owner"])
    assert code == 404  # org B cannot touch org A's lead; existence hidden
    assert _lead_status(mt["db"], "camp-a", "cm-camp-a") == before


def test_cross_org_bulk_status_write_rejected_and_no_state_change(mt):
    before = _lead_status(mt["db"], "camp-a", "cm-camp-a")
    code, _, _ = _post(mt["base"], "/api/status/bulk",
                       {"campaignId": "camp-a", "status": "interested",
                        "items": [{"commentId": "cm-camp-a"}]}, mt["b_owner"])
    assert code == 404
    assert _lead_status(mt["db"], "camp-a", "cm-camp-a") == before


def test_cross_org_note_create_rejected_and_no_state_change(mt):
    before = _note_count(mt["db"], "camp-a", "cm-camp-a")
    code, _, _ = _post(mt["base"], "/api/lead/note",
                       {"op": "create", "campaignId": "camp-a", "commentId": "cm-camp-a",
                        "body": "x"}, mt["b_owner"])
    assert code == 404
    assert _note_count(mt["db"], "camp-a", "cm-camp-a") == before


def test_cross_org_campaign_edit_rejected_and_no_state_change(mt):
    before = _campaign_status(mt["db"], "camp-a")
    code, _, _ = _post(mt["base"], "/api/campaign",
                       {"campaignId": "camp-a", "status": "paused"}, mt["b_owner"])
    assert code == 404
    assert _campaign_status(mt["db"], "camp-a") == before


# ----- /api/run cross-org + role deny (stub run_manager → handler runs past 503) -----

def test_cross_org_run_rejected_and_no_launch(mt):
    # Org B owner HAS run_campaigns permission but does not own camp-a, so the
    # campaign-ownership boundary must reject it before any launch happens.
    before = len(mt["run_manager"].launched)
    code, _, _ = _post(mt["base"], "/api/run",
                       {"campaignId": "camp-a", "mode": "dry"}, mt["b_owner"])
    assert code in (400, 403, 404)  # not yours → not runnable; never a launch
    assert len(mt["run_manager"].launched) == before


def test_member_run_denied_and_no_launch(mt):
    before = len(mt["run_manager"].launched)
    code, _, _ = _post(mt["base"], "/api/run",
                       {"campaignId": "camp-a", "mode": "dry"}, mt["cookies"]["member"])
    assert code == 403  # role gate rejects before the handler runs
    assert len(mt["run_manager"].launched) == before


def test_viewer_run_denied_and_no_launch(mt):
    before = len(mt["run_manager"].launched)
    code, _, _ = _post(mt["base"], "/api/run",
                       {"campaignId": "camp-a", "mode": "dry"}, mt["cookies"]["viewer"])
    assert code == 403
    assert len(mt["run_manager"].launched) == before


# ----- member denied outside its grant, with no state change -----

def test_member_cannot_edit_campaign_and_no_state_change(mt):
    before = _campaign_status(mt["db"], "camp-a")
    code, _, _ = _post(mt["base"], "/api/campaign",
                       {"campaignId": "camp-a", "status": "paused"}, mt["cookies"]["member"])
    assert code == 403
    assert _campaign_status(mt["db"], "camp-a") == before


def test_member_cannot_toggle_integration_and_no_state_change(mt):
    before = _integration_connected(mt["db"], mt["org_a"], "instagram")
    code, _, _ = _post(mt["base"], "/api/integration",
                       {"platform": "instagram", "connected": True}, mt["cookies"]["member"])
    assert code == 403
    assert _integration_connected(mt["db"], mt["org_a"], "instagram") == before


def test_member_cannot_edit_settings_and_no_state_change(mt):
    before = _setting(mt["db"], mt["org_a"], "productName")
    code, _, _ = _post(mt["base"], "/api/settings",
                       {"settings": {"productName": "MemberHacked"}}, mt["cookies"]["member"])
    assert code == 403
    assert _setting(mt["db"], mt["org_a"], "productName") == before


def test_member_cannot_view_team_in_state(mt):
    state = _get(mt["base"], "/api/state", mt["cookies"]["member"])[1]
    assert "TEAM" not in state


def test_member_cannot_add_teammate_and_no_state_change(mt):
    before = _org_user_count(mt["db"], mt["org_a"])
    code, _, _ = _post(mt["base"], "/api/team",
                       {"op": "create", "email": "member-add@x.io", "password": PW,
                        "role": "viewer"}, mt["cookies"]["member"])
    assert code == 403
    assert _org_user_count(mt["db"], mt["org_a"]) == before


# ----- viewer denied EVERY write, with no state change -----

def test_viewer_cannot_change_lead_status_and_no_state_change(mt):
    before = _lead_status(mt["db"], "camp-a", "cm-camp-a")
    code, _, _ = _post(mt["base"], "/api/status",
                       {"campaignId": "camp-a", "commentId": "cm-camp-a",
                        "status": "interested"}, mt["cookies"]["viewer"])
    assert code == 403
    assert _lead_status(mt["db"], "camp-a", "cm-camp-a") == before


def test_viewer_cannot_add_note_and_no_state_change(mt):
    before = _note_count(mt["db"], "camp-a", "cm-camp-a")
    code, _, _ = _post(mt["base"], "/api/lead/note",
                       {"op": "create", "campaignId": "camp-a", "commentId": "cm-camp-a",
                        "body": "x"}, mt["cookies"]["viewer"])
    assert code == 403
    assert _note_count(mt["db"], "camp-a", "cm-camp-a") == before


def test_viewer_cannot_edit_campaign_and_no_state_change(mt):
    before = _campaign_status(mt["db"], "camp-a")
    code, _, _ = _post(mt["base"], "/api/campaign",
                       {"campaignId": "camp-a", "status": "paused"}, mt["cookies"]["viewer"])
    assert code == 403
    assert _campaign_status(mt["db"], "camp-a") == before


def test_viewer_cannot_edit_settings_and_no_state_change(mt):
    before = _setting(mt["db"], mt["org_a"], "productName")
    code, _, _ = _post(mt["base"], "/api/settings",
                       {"settings": {"productName": "ViewerHacked"}}, mt["cookies"]["viewer"])
    assert code == 403
    assert _setting(mt["db"], mt["org_a"], "productName") == before


def test_viewer_cannot_toggle_integration_and_no_state_change(mt):
    before = _integration_connected(mt["db"], mt["org_a"], "instagram")
    code, _, _ = _post(mt["base"], "/api/integration",
                       {"platform": "instagram", "connected": True}, mt["cookies"]["viewer"])
    assert code == 403
    assert _integration_connected(mt["db"], mt["org_a"], "instagram") == before


def test_viewer_cannot_add_teammate_and_no_state_change(mt):
    before = _org_user_count(mt["db"], mt["org_a"])
    code, _, _ = _post(mt["base"], "/api/team",
                       {"op": "create", "email": "viewer-add@x.io", "password": PW,
                        "role": "viewer"}, mt["cookies"]["viewer"])
    assert code == 403
    assert _org_user_count(mt["db"], mt["org_a"]) == before


def test_last_owner_cannot_be_removed(mt):
    # Org B has exactly one owner — it cannot remove itself.
    store = Store(mt["db"])
    owner = next(u for u in store.list_org_users(mt["org_b"]) if u["role"] == "owner")
    store.close()
    code, _, _ = _post(mt["base"], "/api/team", {"op": "remove", "userId": owner["id"]},
                       mt["b_owner"])
    assert code == 400


def test_run_activity_role_gate(mt):
    """GET /api/run/activity inherits the run_campaigns gate: viewers/members are
    forbidden (403); owner/admin pass the gate (404 here, since no such run exists)."""
    for role, cookie in mt["cookies"].items():
        code, _ = _get(mt["base"], "/api/run/activity?runId=nope", cookie)
        if role in {"owner", "admin"}:
            assert code != 403, f"{role} should pass the gate (got {code})"
        else:
            assert code == 403, f"{role} must be forbidden (got {code})"


# ============================== billing (v13) ===============================

def _billing_org(mt, label):
    """Sign up a fresh org with a live campaign; returns (cookie, org_id, campaign_id).
    Isolated from org A/B so subscription/lead manipulation can't leak into other tests."""
    code, resp, cookie = _signup(mt["base"], f"{label}@x.io", company=label)
    assert code == 200, resp
    org_id = resp["data"]["user"]["orgId"]
    cid = f"camp-{label}"
    store = Store(mt["db"])
    try:
        store.upsert_campaign_meta(cid, org_id=org_id, status="live")
        store.upsert_campaign_brief(cid, {"platform": "instagram", "threshold": 0.7},
                                    org_id=org_id)
    finally:
        store.close()
    return cookie, org_id, cid


def _seed_leads(mt, cid, n):
    store = Store(mt["db"])
    try:
        for i in range(n):
            store.upsert_match(campaign_id=cid, reel_id="r", comment_id=f"lead-{cid}-{i}",
                               username="u", text="hi", lang="uz", score=0.9, reason="x",
                               extracted=None, tier="local")
    finally:
        store.close()


# ----- checkout / portal -----

def test_checkout_returns_url_for_owner(mt):
    code, resp, _ = _post(mt["base"], "/api/billing/checkout",
                          {"tier": "pro", "interval": "year"}, mt["cookies"]["owner"])
    assert code == 200, resp
    assert resp["data"]["checkoutUrl"].startswith("https://checkout.test/pro/year")


def test_checkout_rejects_free_and_scale(mt):
    for bad in ("free", "scale", "nonsense"):
        code, resp, _ = _post(mt["base"], "/api/billing/checkout",
                              {"tier": bad, "interval": "month"}, mt["cookies"]["owner"])
        assert code == 400, f"{bad} should be rejected (got {code})"


def test_checkout_rejects_bad_interval(mt):
    code, _, _ = _post(mt["base"], "/api/billing/checkout",
                       {"tier": "lite", "interval": "weekly"}, mt["cookies"]["owner"])
    assert code == 400


def test_portal_returns_url_for_admin(mt):
    code, resp, _ = _post(mt["base"], "/api/billing/portal", {}, mt["cookies"]["admin"])
    assert code == 200, resp
    assert resp["data"]["portalUrl"].startswith("https://portal.test/")
    assert resp["data"]["hasAccount"] is True


# ----- webhook -----

def test_signed_webhook_upserts_subscription(mt):
    cookie, org_id, _ = _billing_org(mt, "wh-upsert")
    code, resp = _signed_webhook(mt["base"], _sub_event(org_id, tier_product="p_pro_m"))
    assert code == 200, resp
    store = Store(mt["db"])
    try:
        sub = store.get_subscription(org_id)
    finally:
        store.close()
    assert sub["tier"] == "pro"
    assert sub["status"] == "active"
    assert sub["interval"] == "month"
    assert sub["provider"] == "polar"
    assert sub["lead_cap"] == 2000


def test_webhook_bad_signature_is_401(mt):
    _, org_id, _ = _billing_org(mt, "wh-badsig")
    body = json.dumps(_sub_event(org_id)).encode()
    req = urllib.request.Request(
        mt["base"] + "/api/billing/webhook", data=body, method="POST",
        headers={"Content-Type": "application/json", "webhook-id": "msg_x",
                 "webhook-timestamp": "9999999999", "webhook-signature": "v1,wrong"})
    try:
        with urllib.request.urlopen(req) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
    assert code == 401
    # Nothing persisted — the org stays on the implicit Free default.
    store = Store(mt["db"])
    try:
        assert store.get_subscription(org_id)["tier"] == "free"
    finally:
        store.close()


def test_webhook_stale_event_is_dropped(mt):
    _, org_id, _ = _billing_org(mt, "wh-stale")
    # A newer cancellation lands first ...
    code, _ = _signed_webhook(mt["base"], _sub_event(
        org_id, status="canceled", modified_at="2026-06-20T00:00:00Z"))
    assert code == 200
    # ... then a DELAYED 'active' with an OLDER modified_at must NOT re-activate.
    code, _ = _signed_webhook(mt["base"], _sub_event(
        org_id, status="active", modified_at="2026-06-10T00:00:00Z"))
    assert code == 200
    store = Store(mt["db"])
    try:
        assert store.get_subscription(org_id)["status"] == "canceled"
    finally:
        store.close()


# ----- run enforcement (soft 402 + clamp) -----

def test_run_blocked_when_over_lead_cap(mt):
    cookie, org_id, cid = _billing_org(mt, "over-cap")
    _seed_leads(mt, cid, 10)   # Free cap is 10 → remaining 0
    code, resp, _ = _post(mt["base"], "/api/run", {"campaignId": cid, "mode": "dry"}, cookie)
    assert code == 402, resp
    assert "limit" in (resp.get("error") or "").lower()


def test_run_blocked_when_subscription_not_active(mt):
    cookie, org_id, cid = _billing_org(mt, "past-due")
    store = Store(mt["db"])
    try:
        store.upsert_subscription(org_id, last_event_ts=1.0, provider="polar",
                                  tier="starter", status="past_due")
    finally:
        store.close()
    code, resp, _ = _post(mt["base"], "/api/run", {"campaignId": cid, "mode": "dry"}, cookie)
    assert code == 402, resp


def test_run_allowed_under_cap_and_clamps_target(mt):
    cookie, org_id, cid = _billing_org(mt, "clamp")
    _seed_leads(mt, cid, 8)    # Free cap 10 → remaining 2
    mt["run_manager"].launched.clear()
    code, resp, _ = _post(mt["base"], "/api/run",
                          {"campaignId": cid, "mode": "dry", "targetLeadCount": 25}, cookie)
    assert code == 202, resp
    spec = mt["run_manager"].launched[-1]
    assert spec.target_leads == 2, "target must be clamped to cap − used"


def test_run_allowed_for_new_org_on_free_default(mt):
    cookie, org_id, cid = _billing_org(mt, "fresh-free")
    code, resp, _ = _post(mt["base"], "/api/run", {"campaignId": cid, "mode": "dry"}, cookie)
    assert code == 202, resp


# ----- /api/settings exposure -----

def test_settings_billing_present_for_owner(mt):
    code, resp = _get(mt["base"], "/api/settings", mt["cookies"]["owner"])
    assert code == 200
    assert "BILLING" in resp
    billing_block = resp["BILLING"]
    assert billing_block["tier"] == "free"
    assert billing_block["leadCap"] == 10
    assert "tiers" in billing_block and len(billing_block["tiers"]) == 5


def test_settings_unreachable_for_member(mt):
    # A member cannot view_settings at all → 403 (so never sees BILLING).
    code, _ = _get(mt["base"], "/api/settings", mt["cookies"]["member"])
    assert code == 403
