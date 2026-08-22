"""HTTP tests for the panel bridge server — SPA static serving, the live
`/api/state` JSON feed, and the v1 status-mark write endpoint (PRD §11:
matches table status-mark is a v1 panel surface)."""
import http.client
import json
import logging
import os
import shutil
import socket
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from aizu.core.config import (MAX_CAMPAIGN_BRIEF_BYTES, campaign_to_brief, load_campaign,
                              load_soul, resolve_campaign)
from aizu.core.feed import Comment, FakeFeed, Reel
from aizu.core.mock_router import MockRouter
from aizu.core.pacing import PacingConfig, Pacer
from aizu.runner import RunManager
from aizu import server
from aizu.secrets import SecretCipher
from aizu.server import serve
from aizu.engines.instagram.session import Session, SessionConfig
from aizu.core.store import Store

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"

# Minimal stand-ins for the built panel_dir (admin-panel/dist) since the aizu.uz
# split: a landing shell at the root, a separate SPA shell under app/, a hashed
# asset, and one real file under landing/ (the landing's own static assets). The
# server serves the HTML shells as-is, falls back to the landing for unknown
# non-API paths, and falls back to the SPA shell for unknown paths under /app/.
_LANDING_HTML = '<!doctype html><html><head></head><body><div id="landing"></div>' \
                '</body></html>'
_APP_HTML = '<!doctype html><html><head></head><body><div id="root"></div>' \
            '<script type="module" src="/assets/index-abc123.js"></script></body></html>'
_ASSET_JS = 'console.log("spa bundle");'
_LANDING_CSS = 'body { color: red; }'

# All /api/* surfaces except /api/auth/* now require a session. The module-scoped
# `panel` fixture signs up once and stashes the cookie here; the shared _get/_post
# helpers attach it so every existing read/write test stays authenticated.
_SESSION_COOKIE: str | None = None


class _FakeProc:
    """A controllable stand-in for a spawned engine process."""
    def __init__(self, returncode: int, gate):
        self.pid = 4242
        self.returncode = None
        self._rc = returncode
        self._gate = gate

    def wait(self) -> int:
        if self._gate is not None:
            self._gate.wait(timeout=5)
        self.returncode = self._rc
        return self._rc

    def terminate(self) -> None:
        self._rc = -15
        if self._gate is not None:
            self._gate.set()


class _FakeSpawner:
    """Injected into RunManager so /api/run tests never spawn a real engine."""
    def __init__(self):
        self.calls = []
        self.next_returncode = 0
        self.next_gate = None

    def __call__(self, argv, cwd, env, log_path):
        self.calls.append(argv)
        return _FakeProc(self.next_returncode, self.next_gate)


def _ready_probe(cdp_url: str, **_kwargs) -> dict:
    """Stand-in for readiness.check_readiness: a reachable, logged-in agent."""
    return {"ready": True, "cdp": "ok", "instagram": "logged_in",
            "checkedAt": 0.0, "cdpUrl": cdp_url, "detail": None}


def _register_and_seed(db_path: str) -> None:
    """Register the file campaign to the (already-created) Default org, then seed a
    session — so the owner's org owns the campaign and its matches carry org_id."""
    store = Store(db_path)
    org_id = store._conn.execute(
        "SELECT id FROM organizations ORDER BY id LIMIT 1").fetchone()[0]
    campaign = load_campaign(CONFIG / "campaign.md")
    store.upsert_campaign_meta(campaign.campaign_id, org_id=org_id, status="live")
    feed = FakeFeed([
        Reel("r1", author="acme.io", caption="Acme app — sprint planning, free trial",
             ocr_text="Pro from $12/seat", comments=[
                 Comment("c1", "dana", "How much is the Pro plan? +1 415 555 0142", "en"),
             ]),
    ])
    Session(store=store, router=MockRouter(store=store), feed=feed,
            soul=load_soul(CONFIG / "soul.md"), campaign=campaign,
            pacer=Pacer(cfg=PacingConfig(enforce_daytime=False), sleep=lambda _t: None),
            cfg=SessionConfig()).run()
    store.close()


@pytest.fixture(scope="module")
def panel():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    panel_dir = tempfile.mkdtemp(suffix="-spa")
    (Path(panel_dir) / "index.html").write_text(_LANDING_HTML, encoding="utf-8")
    app_dir = Path(panel_dir) / "app"
    app_dir.mkdir()
    (app_dir / "index.html").write_text(_APP_HTML, encoding="utf-8")
    assets = Path(panel_dir) / "assets"
    assets.mkdir()
    (assets / "index-abc123.js").write_text(_ASSET_JS, encoding="utf-8")
    landing_css = Path(panel_dir) / "landing" / "css"
    landing_css.mkdir(parents=True)
    (landing_css / "core-hr.css").write_text(_LANDING_CSS, encoding="utf-8")
    spawner = _FakeSpawner()
    manager = RunManager(db_path=db_path, config_dir=str(CONFIG),
                         engine_root=panel_dir, log_dir=Path(panel_dir) / "run-logs",
                         spawner=spawner, python_exe="py")
    # A ready agent: these tests exercise the run control plane, not the readiness
    # gate POST /api/run puts in front of a LIVE run (that gate has its own file,
    # test_agent_readiness.py). Without the stub every live-run case here would be
    # answered 409 agent_not_ready, since CI has no warmed Chrome on :9333.
    httpd = serve(db_path, panel_dir, str(CONFIG), port=0, run_manager=manager,
                  readiness_probe=_ready_probe)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    global _SESSION_COOKIE
    # Signup creates the org + owner; THEN register the file campaign to that org
    # and seed its matches (so the owner's org owns the campaign data).
    _SESSION_COOKIE = _signup_cookie(base, "panel-tester@aizu.test", "test-password-123")
    _register_and_seed(db_path)
    yield {"base": base, "db": db_path, "spawner": spawner, "cookie": _SESSION_COOKIE}
    _SESSION_COOKIE = None
    httpd.shutdown()
    os.unlink(db_path)
    shutil.rmtree(panel_dir, ignore_errors=True)


def _signup_cookie(base: str, email: str, password: str,
                   company: str = "Test Co") -> str:
    """Create an account+company and return the session cookie ('rr_session=<token>')."""
    req = urllib.request.Request(
        base + "/api/auth/signup",
        data=json.dumps({"email": email, "password": password,
                         "companyName": company}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        return resp.headers["Set-Cookie"].split(";", 1)[0]


def _get(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url)
    if _SESSION_COOKIE:
        req.add_header("Cookie", _SESSION_COOKIE)
    with urllib.request.urlopen(req) as resp:
        return resp.status, resp.read().decode("utf-8")


def _post(url: str, body: bytes) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if _SESSION_COOKIE:
        headers["Cookie"] = _SESSION_COOKIE
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _campaign_id() -> str:
    return load_campaign(CONFIG / "campaign.md").campaign_id


def _owner_org_id(panel) -> int:
    """The org the module-scoped `panel` fixture's owner signed up into. Needed
    wherever a test seeds a campaign row straight into the store: an UNREGISTERED
    brief (org_id NULL) is not the caller's campaign, so a brief-carrying save is an
    ambiguous create, not the edit those tests mean to exercise."""
    store = Store(panel["db"])
    try:
        return int(store._conn.execute(
            "SELECT id FROM organizations ORDER BY id LIMIT 1").fetchone()[0])
    finally:
        store.close()


def test_index_served_as_is(panel):
    # "/" now serves the public marketing landing, not the SPA — the SPA moved to
    # /app/ so the landing's in-page anchor nav (#core-hr etc.) isn't swallowed by
    # createHashRouter.
    status, html = _get(panel["base"] + "/")
    assert status == 200
    assert '<div id="landing"></div>' in html
    assert "__raw__" not in html


def test_static_assets_served(panel):
    status, body = _get(panel["base"] + "/assets/index-abc123.js")
    assert status == 200
    assert "spa bundle" in body


def test_landing_static_asset_served(panel):
    # Landing's own assets (css/js/vendor/fonts/photos) live under /landing/ inside
    # panel_dir and must serve directly, same as /assets/*.
    status, body = _get(panel["base"] + "/landing/css/core-hr.css")
    assert status == 200
    assert "color: red" in body


def test_app_shell_served_at_app_path(panel):
    # Both "/app" (no trailing slash) and "/app/" must serve the SPA shell — no
    # redirect required since the hash fragment (e.g. "#/leads") never reaches the
    # server either way.
    for path in ("/app", "/app/", "/app/index.html"):
        status, html = _get(panel["base"] + path)
        assert status == 200, path
        assert '<div id="root"></div>' in html, path


def test_unknown_app_subpath_falls_back_to_app_shell(panel):
    # A stale/typed deep link under /app/ still resolves to the SPA shell so
    # createHashRouter gets a chance to parse the route client-side.
    status, html = _get(panel["base"] + "/app/somewhere")
    assert status == 200
    assert '<div id="root"></div>' in html


def test_unknown_client_route_falls_back_to_landing(panel):
    # A hard refresh on an unknown non-API, non-/app path falls back to the
    # marketing landing (a soft 404), not the SPA shell — the SPA no longer lives
    # at "/".
    status, html = _get(panel["base"] + "/matches")
    assert status == 200
    assert '<div id="landing"></div>' in html


def test_api_state_returns_raw_json(panel):
    req = urllib.request.Request(panel["base"] + "/api/state",
                                 headers={"Cookie": panel["cookie"]})
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("application/json")
        raw = json.loads(resp.read())
    for key in ["CONFIG", "CAMPAIGNS", "SESSIONS", "REELS", "MATCHES", "PLATFORMS",
                "ESCALATION_LOG", "ALERTS", "HEALTH", "SOUL"]:
        assert key in raw
    assert raw["MATCHES"], "seeded match should flow through to /api/state"
    assert raw["MATCHES"][0]["commentId"] == "c1"


def test_api_state_cors_for_local_dev_origin(panel):
    req = urllib.request.Request(panel["base"] + "/api/state",
                                 headers={"Origin": "http://localhost:5173",
                                          "Cookie": panel["cookie"]})
    with urllib.request.urlopen(req) as resp:
        assert resp.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"


def test_api_state_no_cors_for_foreign_origin(panel):
    req = urllib.request.Request(panel["base"] + "/api/state",
                                 headers={"Origin": "https://evil.example",
                                          "Cookie": panel["cookie"]})
    with urllib.request.urlopen(req) as resp:
        assert resp.headers["Access-Control-Allow-Origin"] is None


def test_status_preflight_allows_local_dev_origin(panel):
    req = urllib.request.Request(
        panel["base"] + "/api/status", method="OPTIONS",
        headers={"Origin": "http://127.0.0.1:5173",
                 "Access-Control-Request-Method": "POST"})
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 204
        assert resp.headers["Access-Control-Allow-Origin"] == "http://127.0.0.1:5173"
        assert "POST" in resp.headers["Access-Control-Allow-Methods"]


def test_status_write_persists(panel):
    code, resp = _post(panel["base"] + "/api/status", json.dumps({
        "campaignId": _campaign_id(), "commentId": "c1", "status": "interested",
    }).encode())
    assert code == 200
    assert resp["ok"] is True and resp["error"] is None
    store = Store(panel["db"])
    try:
        row = [m for m in store.matches(_campaign_id()) if m["comment_id"] == "c1"][0]
        assert row["status"] == "interested"
    finally:
        store.close()


def test_status_write_invalid_status_rejected(panel):
    code, resp = _post(panel["base"] + "/api/status", json.dumps({
        "campaignId": _campaign_id(), "commentId": "c1", "status": "banana",
    }).encode())
    assert code == 400
    assert resp["ok"] is False and "status" in resp["error"]


def test_status_write_unknown_comment_404(panel):
    code, resp = _post(panel["base"] + "/api/status", json.dumps({
        "campaignId": _campaign_id(), "commentId": "nope", "status": "interested",
    }).encode())
    assert code == 404
    assert resp["ok"] is False


def test_status_write_malformed_json_rejected(panel):
    code, resp = _post(panel["base"] + "/api/status", b"{not json")
    assert code == 400
    assert resp["ok"] is False


def test_status_write_missing_fields_rejected(panel):
    code, resp = _post(panel["base"] + "/api/status",
                       json.dumps({"commentId": "c1"}).encode())
    assert code == 400
    assert resp["ok"] is False


def test_status_write_cross_origin_rejected(panel):
    req = urllib.request.Request(
        panel["base"] + "/api/status",
        data=json.dumps({"campaignId": _campaign_id(), "commentId": "c1",
                         "status": "in_progress"}).encode(),
        headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
        method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
    assert code == 403


def test_status_write_local_origin_allowed(panel):
    req = urllib.request.Request(
        panel["base"] + "/api/status",
        data=json.dumps({"campaignId": _campaign_id(), "commentId": "c1",
                         "status": "in_progress"}).encode(),
        headers={"Content-Type": "application/json",
                 "Origin": panel["base"], "Cookie": panel["cookie"]},
        method="POST")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200


# --- AIZU_ALLOWED_ORIGINS: hosted deployments serve the panel off-loopback ---

def test_allowed_origin_env_is_opt_in(monkeypatch):
    """Unset (the local-first default) keeps the loopback-only posture."""
    monkeypatch.delenv(server.ALLOWED_ORIGINS_ENV, raising=False)
    assert server._is_allowed_origin("http://127.0.0.1:5173")
    assert not server._is_allowed_origin("https://aizu.uz")


def test_allowed_origin_env_admits_named_origins(monkeypatch):
    monkeypatch.setenv(server.ALLOWED_ORIGINS_ENV,
                       "https://aizu.uz, http://192.166.228.52:780")
    assert server._is_allowed_origin("https://aizu.uz")
    assert server._is_allowed_origin("http://192.166.228.52:780")
    assert server._is_allowed_origin("https://AIZU.UZ")       # RFC 6454: case-insensitive
    assert server._is_allowed_origin("https://aizu.uz/")      # trailing slash trimmed
    # Loopback still works alongside a configured allowlist.
    assert server._is_allowed_origin("http://localhost:5173")


def test_allowed_origin_env_matches_whole_origin_only(monkeypatch):
    """A lookalike must never satisfy the guard by prefix/suffix or wrong port."""
    monkeypatch.setenv(server.ALLOWED_ORIGINS_ENV, "https://aizu.uz")
    for hostile in ["https://aizu.uz.evil.com", "https://evil.com/https://aizu.uz",
                    "https://notaizu.uz", "http://aizu.uz", "https://aizu.uz:8443"]:
        assert not server._is_allowed_origin(hostile), hostile


def test_post_from_configured_origin_allowed(panel, monkeypatch):
    monkeypatch.setenv(server.ALLOWED_ORIGINS_ENV, "https://panel.example")
    req = urllib.request.Request(
        panel["base"] + "/api/status",
        data=json.dumps({"campaignId": _campaign_id(), "commentId": "c1",
                         "status": "in_progress"}).encode(),
        headers={"Content-Type": "application/json",
                 "Origin": "https://panel.example", "Cookie": panel["cookie"]},
        method="POST")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        assert resp.headers["Access-Control-Allow-Origin"] == "https://panel.example"


def test_post_from_unlisted_origin_still_rejected(panel, monkeypatch):
    monkeypatch.setenv(server.ALLOWED_ORIGINS_ENV, "https://panel.example")
    req = urllib.request.Request(
        panel["base"] + "/api/status",
        data=json.dumps({"campaignId": _campaign_id(), "commentId": "c1",
                         "status": "in_progress"}).encode(),
        headers={"Content-Type": "application/json",
                 "Origin": "https://evil.example", "Cookie": panel["cookie"]},
        method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
    assert code == 403


def test_post_unknown_path_404(panel):
    code, resp = _post(panel["base"] + "/api/nope", b"{}")
    assert code == 404
    assert resp["ok"] is False


def test_auth_me_without_session_is_401(panel):
    # No cookie → 401 (and crucially not a 404, which would mean the route
    # dispatch order regressed and /api/auth/me fell into the /api/* 404 guard).
    try:
        urllib.request.urlopen(panel["base"] + "/api/auth/me")
        raise AssertionError("expected HTTP 401 for /api/auth/me without a session")
    except urllib.error.HTTPError as e:
        assert e.code == 401


def test_auth_me_with_session_returns_user(panel):
    req = urllib.request.Request(panel["base"] + "/api/auth/me",
                                 headers={"Cookie": panel["cookie"]})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    assert data["ok"] is True
    assert data["data"]["user"]["email"] == "panel-tester@aizu.test"


# ----- v3 write endpoints -----

def test_api_state_exposes_v3_keys(panel):
    _, raw = _get(panel["base"] + "/api/state")
    data = json.loads(raw)
    for key in ("DASHBOARD", "REPORTS", "TEAM", "INTEGRATIONS"):
        assert key in data
    assert {"today", "week", "month"} <= set(data["DASHBOARD"])
    assert data["CAMPAIGNS"][0]["status"] in ("live", "paused", "draft", "ended")
    assert "budgetCap" in data["CAMPAIGNS"][0]


def test_api_state_scoped_to_known_campaign(panel):
    # `?campaign=<id>` scopes the whole payload; the requested campaign is primary.
    _, raw = _get(panel["base"] + "/api/state?campaign=" + _campaign_id())
    data = json.loads(raw)
    assert data["CAMPAIGNS"][0]["id"] == _campaign_id()
    assert data["MATCHES"][0]["commentId"] == "c1"


def test_api_state_unknown_campaign_404(panel):
    try:
        _get(panel["base"] + "/api/state?campaign=does-not-exist")
        raise AssertionError("expected HTTP 404 for an unknown campaign")
    except urllib.error.HTTPError as e:
        assert e.code == 404
        assert json.loads(e.read())["ok"] is False


def test_unknown_api_get_returns_json_404_not_spa(panel):
    # A near-miss on the API namespace (trailing slash, unknown endpoint) must
    # answer with a JSON 404 — never fall through to the SPA shell. Returning
    # 200 text/html here is what made a scoped /api/state request silently
    # deliver index.html, which the panel then failed to JSON-parse.
    for path in ("/api/state/?campaign=x", "/api/bogus", "/api"):
        try:
            _get(panel["base"] + path)
            raise AssertionError(f"expected HTTP 404 for {path}")
        except urllib.error.HTTPError as e:
            assert e.code == 404, path
            assert e.headers["Content-Type"].startswith("application/json"), path
            assert json.loads(e.read())["ok"] is False, path


def test_bulk_status_partial_success(panel):
    code, resp = _post(panel["base"] + "/api/status/bulk", json.dumps({
        "campaignId": _campaign_id(), "status": "in_progress",
        "items": [{"commentId": "c1"}, {"commentId": "missing-x"}],
    }).encode())
    assert code == 200 and resp["ok"] is True
    assert resp["data"]["updated"] == 1
    assert resp["data"]["missing"] == ["missing-x"]


def test_bulk_status_rejects_bad_status(panel):
    code, resp = _post(panel["base"] + "/api/status/bulk", json.dumps({
        "campaignId": _campaign_id(), "status": "banana", "items": [{"commentId": "c1"}],
    }).encode())
    assert code == 400 and resp["ok"] is False


# ----- v6 lead Kanban: audit log, forced reason, notes -----

def _post_as(url: str, body: bytes, cookie: str) -> tuple[int, dict]:
    """POST with an explicit session cookie (for multi-user tests)."""
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Cookie": cookie})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_status_write_logs_actor(panel):
    code, _ = _post(panel["base"] + "/api/status", json.dumps({
        "campaignId": _campaign_id(), "commentId": "c1", "status": "interested",
    }).encode())
    assert code == 200
    store = Store(panel["db"])
    try:
        hist = store.status_history(_campaign_id(), "c1")
        assert hist and hist[-1]["by"] == "panel-tester@aizu.test"
    finally:
        store.close()


def test_status_into_closed_requires_reason_400(panel):
    # No reason → 400 (forced-reason status).
    code, resp = _post(panel["base"] + "/api/status", json.dumps({
        "campaignId": _campaign_id(), "commentId": "c1", "status": "closed",
    }).encode())
    assert code == 400 and "reason" in resp["error"]
    # With a reason → 200 and the reason is stored on the audit row.
    code, resp = _post(panel["base"] + "/api/status", json.dumps({
        "campaignId": _campaign_id(), "commentId": "c1", "status": "closed",
        "note": "client went with a competitor",
    }).encode())
    assert code == 200
    store = Store(panel["db"])
    try:
        assert store.status_history(_campaign_id(), "c1")[-1]["reason"] \
            == "client went with a competitor"
    finally:
        store.close()


def test_bulk_status_forced_reason_400(panel):
    code, resp = _post(panel["base"] + "/api/status/bulk", json.dumps({
        "campaignId": _campaign_id(), "status": "archived",
        "items": [{"commentId": "c1"}],
    }).encode())
    assert code == 400 and "reason" in resp["error"]


def test_bulk_status_archive_with_reason_records_audit(panel):
    """Bulk-archiving selected leads with a shared reason succeeds and stamps the
    reason on each lead's status-change audit row."""
    code, resp = _post(panel["base"] + "/api/status/bulk", json.dumps({
        "campaignId": _campaign_id(), "status": "archived",
        "items": [{"commentId": "c1"}], "note": "end of campaign cleanup",
    }).encode())
    assert code == 200 and resp["data"]["updated"] == 1
    store = Store(panel["db"])
    try:
        assert store.status_history(_campaign_id(), "c1")[-1]["reason"] \
            == "end of campaign cleanup"
    finally:
        store.close()


def test_bulk_status_forbidden_for_member(panel):
    """Bulk status changes are owner/admin only (bulk_edit_leads). A member is
    blocked by the route's role gate — proven by the role-gate error message —
    even though single-lead edits (edit_leads) still allow members."""
    member = _signup_cookie(panel["base"], "bulk-member@aizu.test", "test-password-123")
    store = Store(panel["db"])
    try:
        store._conn.execute(
            "UPDATE users SET role='member' WHERE email='bulk-member@aizu.test'")
        store._conn.commit()
    finally:
        store.close()
    code, resp = _post_as(panel["base"] + "/api/status/bulk", json.dumps({
        "campaignId": _campaign_id(), "status": "archived",
        "items": [{"commentId": "c1"}], "note": "bulk cleanup",
    }).encode(), member)
    assert code == 403 and "role" in resp["error"]


def test_lead_note_create_lists_in_state(panel):
    code, resp = _post(panel["base"] + "/api/lead/note", json.dumps({
        "op": "create", "campaignId": _campaign_id(), "commentId": "c1",
        "body": "left a voicemail",
    }).encode())
    assert code == 200 and resp["data"]["body"] == "left a voicemail"
    _, raw = _get(panel["base"] + "/api/state")
    lead = [m for m in json.loads(raw)["MATCHES"] if m["commentId"] == "c1"][0]
    assert any(n["body"] == "left a voicemail" for n in lead["notes"])
    assert lead["notes"][-1]["authorEmail"] == "panel-tester@aizu.test"


def test_lead_note_owner_only_delete(panel):
    # Author (the module user) creates a note.
    _, resp = _post(panel["base"] + "/api/lead/note", json.dumps({
        "op": "create", "campaignId": _campaign_id(), "commentId": "c1",
        "body": "owner-only note",
    }).encode())
    note_id = resp["data"]["id"]
    # A different user cannot delete it.
    other = _signup_cookie(panel["base"], "second-user@aizu.test", "test-password-123")
    code, resp = _post_as(panel["base"] + "/api/lead/note",
                          json.dumps({"op": "delete", "noteId": note_id}).encode(), other)
    assert code == 403
    # The author can.
    code, _ = _post(panel["base"] + "/api/lead/note",
                    json.dumps({"op": "delete", "noteId": note_id}).encode())
    assert code == 200


def test_lead_note_create_rejects_empty_body(panel):
    code, resp = _post(panel["base"] + "/api/lead/note", json.dumps({
        "op": "create", "campaignId": _campaign_id(), "commentId": "c1", "body": "   ",
    }).encode())
    assert code == 400 and resp["ok"] is False


def test_campaign_meta_upsert_persists(panel):
    code, resp = _post(panel["base"] + "/api/campaign", json.dumps({
        "campaignId": _campaign_id(), "status": "paused", "budgetCap": 42.0,
    }).encode())
    assert code == 200 and resp["ok"] is True
    assert resp["data"]["status"] == "paused" and resp["data"]["budget_cap"] == 42.0
    _, raw = _get(panel["base"] + "/api/state")
    assert json.loads(raw)["CAMPAIGNS"][0]["status"] == "paused"


def test_campaign_rejects_negative_budget(panel):
    code, resp = _post(panel["base"] + "/api/campaign", json.dumps({
        "campaignId": _campaign_id(), "budgetCap": -5,
    }).encode())
    assert code == 400 and resp["ok"] is False


def test_campaign_brief_persists_and_surfaces(panel):
    code, resp = _post(panel["base"] + "/api/campaign", json.dumps({
        "campaignId": "ui-brief-test", "displayName": "UI Brief Test", "status": "draft",
        "brief": {"platform": "youtube", "threshold": 0.8, "relevanceDef": "saas product",
                  "matchDef": "buyer intent", "extractDef": "- phone",
                  "seedChannels": ["UC_x"], "languageMix": ["en"]},
    }).encode())
    assert code == 200 and resp["ok"] is True
    # A create allocates a key in the caller's own org namespace — read the id back
    # off the response rather than assuming the requested slug (see
    # _resolve_campaign_target: never reusing the bare id is what removes the
    # cross-tenant existence oracle).
    created_id = resp["data"]["campaign_id"]

    _, raw = _get(panel["base"] + "/api/state")
    camp = next(c for c in json.loads(raw)["CAMPAIGNS"] if c["id"] == created_id)
    assert camp["platform"] == "youtube" and camp["threshold"] == 0.8
    assert camp["briefForm"]["relevanceDef"] == "saas product"
    assert camp["briefForm"]["seedChannels"] == ["UC_x"]


def test_brief_to_snake_includes_home_feed_as_bool():
    from aizu.server import _brief_to_snake
    assert _brief_to_snake({"includeHomeFeed": False})["include_home_feed"] is False
    assert _brief_to_snake({"includeHomeFeed": True})["include_home_feed"] is True
    # Absent → key dropped, so the engine applies its seed-aware default.
    assert "include_home_feed" not in _brief_to_snake({"seedHashtags": ["flutterdev"]})


# ----- v12 campaign lifecycle: archive / pause precedence -----

def _make_campaign(panel, cid: str, status: str = "live") -> None:
    code, _ = _post(panel["base"] + "/api/campaign",
                    json.dumps({"campaignId": cid, "status": status}).encode())
    assert code == 200


def test_campaign_archive_and_unarchive_round_trip(panel):
    cid = "arch-roundtrip"
    _make_campaign(panel, cid, status="live")
    code, resp = _post(panel["base"] + "/api/campaign/archive",
                       json.dumps({"campaignId": cid, "archived": True}).encode())
    assert code == 200 and resp["data"]["archived"] is True
    store = Store(panel["db"])
    try:
        meta = store.get_campaign_meta(cid)
        assert meta["archived_at"] is not None
        assert meta["status"] == "paused"          # archive-while-live parks it
    finally:
        store.close()
    # Un-archive nulls the column (dedicated UPDATE, not the COALESCE upsert).
    code, resp = _post(panel["base"] + "/api/campaign/archive",
                       json.dumps({"campaignId": cid, "archived": False}).encode())
    assert code == 200 and resp["data"]["archived"] is False
    store = Store(panel["db"])
    try:
        assert store.get_campaign_meta(cid)["archived_at"] is None
    finally:
        store.close()


def test_campaign_archive_unknown_campaign_404(panel):
    code, resp = _post(panel["base"] + "/api/campaign/archive",
                       json.dumps({"campaignId": "no-such-campaign", "archived": True}).encode())
    assert code == 404 and resp["ok"] is False


def test_campaign_archive_rejects_non_bool(panel):
    code, resp = _post(panel["base"] + "/api/campaign/archive",
                       json.dumps({"campaignId": _campaign_id(), "archived": "yes"}).encode())
    assert code == 400 and resp["ok"] is False


def test_campaign_archive_forbidden_for_viewer(panel):
    """A viewer cannot archive — the edit_campaigns route gate rejects them."""
    viewer = _signup_cookie(panel["base"], "arch-viewer@aizu.test", "test-password-123")
    # New signups own their own org; downgrade this account to viewer in its own org.
    store = Store(panel["db"])
    try:
        store._conn.execute(
            "UPDATE users SET role='viewer' WHERE email='arch-viewer@aizu.test'")
        store._conn.commit()
    finally:
        store.close()
    code, _ = _post_as(panel["base"] + "/api/campaign/archive",
                       json.dumps({"campaignId": _campaign_id(), "archived": True}).encode(),
                       viewer)
    assert code == 403


def test_campaign_pause_then_user_resume_clears(panel):
    cid = "pause-user-resume"
    _make_campaign(panel, cid, status="live")
    # Pause via the toggle stamps paused_reason='user'.
    _post(panel["base"] + "/api/campaign",
          json.dumps({"campaignId": cid, "status": "paused"}).encode())
    store = Store(panel["db"])
    try:
        assert store.get_campaign_meta(cid)["paused_reason"] == "user"
    finally:
        store.close()
    # Resume via the toggle clears a user pause.
    code, resp = _post(panel["base"] + "/api/campaign",
                       json.dumps({"campaignId": cid, "status": "live"}).encode())
    assert code == 200 and resp["data"]["status"] == "live"
    assert resp["data"]["paused_reason"] is None


def test_campaign_user_resume_does_not_clear_auto_pause(panel):
    """A system 'auto' halt survives an operator resume (precedence guard)."""
    cid = "auto-pause-guard"
    _make_campaign(panel, cid, status="live")
    store = Store(panel["db"])
    try:
        store.set_campaign_paused(cid, paused=True, reason="auto")
    finally:
        store.close()
    code, resp = _post(panel["base"] + "/api/campaign",
                       json.dumps({"campaignId": cid, "status": "live"}).encode())
    assert code == 200
    assert resp["data"]["status"] == "paused"      # still parked by the system
    assert resp["data"]["paused_reason"] == "auto"


def test_campaign_schedule_arm_computes_next_run_and_clears(panel):
    cid = "sched-roundtrip"
    _make_campaign(panel, cid, status="live")
    code, resp = _post(panel["base"] + "/api/campaign/schedule", json.dumps({
        "campaignId": cid, "enabled": True, "kind": "daily", "hour": 9, "minute": 0,
        "targetLeads": 25,
    }).encode())
    assert code == 200 and resp["data"]["scheduleEnabled"] is True
    assert resp["data"]["nextRunAt"] is not None       # server computed it
    store = Store(panel["db"])
    try:
        meta = store.get_campaign_meta(cid)
        assert meta["schedule_enabled"] == 1 and meta["schedule_kind"] == "daily"
        assert meta["schedule_target_leads"] == 25
    finally:
        store.close()
    # Disable clears it.
    code, resp = _post(panel["base"] + "/api/campaign/schedule", json.dumps({
        "campaignId": cid, "enabled": False,
    }).encode())
    assert code == 200 and resp["data"]["scheduleEnabled"] is False
    assert resp["data"]["nextRunAt"] is None


def test_campaign_schedule_rejects_bad_cadence(panel):
    cid = _campaign_id()
    bad_bodies = [
        {"campaignId": cid, "enabled": True, "kind": "hourly", "hour": 9, "minute": 0},
        {"campaignId": cid, "enabled": True, "kind": "daily", "hour": 25, "minute": 0},
        {"campaignId": cid, "enabled": True, "kind": "weekly", "hour": 9, "minute": 0},  # no dow
        {"campaignId": cid, "enabled": True, "kind": "daily", "hour": 9, "minute": 0, "tz": "Mars/Olympus"},
    ]
    for body in bad_bodies:
        code, resp = _post(panel["base"] + "/api/campaign/schedule", json.dumps(body).encode())
        assert code == 400 and resp["ok"] is False, body


def test_campaign_schedule_unknown_campaign_404(panel):
    code, resp = _post(panel["base"] + "/api/campaign/schedule", json.dumps({
        "campaignId": "no-such-campaign", "enabled": True, "kind": "daily",
        "hour": 9, "minute": 0,
    }).encode())
    assert code == 404 and resp["ok"] is False


def test_campaign_schedule_surfaces_in_state(panel):
    cid = "sched-in-state"
    _make_campaign(panel, cid, status="live")
    _post(panel["base"] + "/api/campaign/schedule", json.dumps({
        "campaignId": cid, "enabled": True, "kind": "weekly", "dow": 0,
        "hour": 9, "minute": 30,
    }).encode())
    _, raw = _get(panel["base"] + "/api/state")
    camp = next(c for c in json.loads(raw)["CAMPAIGNS"] if c["id"] == cid)
    assert camp["scheduleEnabled"] is True
    assert camp["scheduleKind"] == "weekly" and camp["scheduleDow"] == 0
    assert camp["scheduleHour"] == 9 and camp["scheduleMinute"] == 30
    assert camp["nextRunAt"] is not None


# ----- AI-first campaign generation (POST /api/campaign/generate) -----

def test_generate_requires_at_least_one_input(panel):
    code, resp = _post(panel["base"] + "/api/campaign/generate", b"{}")
    assert code == 400 and resp["ok"] is False
    assert "at least one" in resp["error"]


def test_generate_missing_api_key_returns_503(panel, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    code, resp = _post(panel["base"] + "/api/campaign/generate",
                       json.dumps({"text": "we sell shoes"}).encode())
    assert code == 503 and resp["ok"] is False


def test_generate_returns_draft(panel, monkeypatch):
    from aizu import campaign_gen
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    draft = {"name": "Acme Shoes", "objective": "lead", "platform": "instagram",
             "relevanceDef": "running shoes", "matchDef": "wants to buy",
             "seedHashtags": "running, shoes"}
    monkeypatch.setattr(campaign_gen, "generate_campaign", lambda **kw: draft)
    code, resp = _post(panel["base"] + "/api/campaign/generate",
                       json.dumps({"text": "we sell running shoes"}).encode())
    assert code == 200 and resp["ok"] is True
    assert resp["data"]["name"] == "Acme Shoes"
    assert resp["data"]["seedHashtags"] == "running, shoes"


def test_generate_campaign_error_returns_422(panel, monkeypatch):
    from aizu import campaign_gen

    def _boom(**kw):
        raise campaign_gen.CampaignGenError("could not draft a campaign")

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(campaign_gen, "generate_campaign", _boom)
    code, resp = _post(panel["base"] + "/api/campaign/generate",
                       json.dumps({"text": "x"}).encode())
    assert code == 422 and resp["error"] == "could not draft a campaign"


def test_generate_accepts_body_over_default_cap(panel, monkeypatch):
    """A base64 screenshot exceeds the 64 KB default body cap — the generate route
    must use its own larger ceiling (regression: images were silently 400ing)."""
    from aizu import campaign_gen
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(campaign_gen, "generate_campaign",
                        lambda **kw: {"name": "From Image"})
    big_image = "A" * (200 * 1024)      # ~200 KB, well over MAX_BODY_BYTES (64 KB)
    code, resp = _post(panel["base"] + "/api/campaign/generate",
                       json.dumps({"imageB64": big_image}).encode())
    assert code == 200 and resp["data"]["name"] == "From Image"


# ----- conversational interview (POST /api/campaign/interview) -----

def test_interview_requires_at_least_one_input(panel):
    code, resp = _post(panel["base"] + "/api/campaign/interview", b"{}")
    assert code == 400 and resp["ok"] is False
    assert "at least one" in resp["error"]


def test_interview_missing_api_key_returns_503(panel, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    code, resp = _post(panel["base"] + "/api/campaign/interview",
                       json.dumps({"text": "we sell shoes"}).encode())
    assert code == 503 and resp["ok"] is False


def test_interview_returns_questions(panel, monkeypatch):
    from aizu import campaign_gen
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    result = campaign_gen.InterviewResult(
        done=False,
        questions=[{"id": "platforms", "type": "platforms", "prompt": "Where?",
                    "suggested": ["instagram"]}],
        product_context="PRODUCT DESCRIPTION:\nshoes")
    monkeypatch.setattr(campaign_gen, "run_interview", lambda **kw: result)
    code, resp = _post(panel["base"] + "/api/campaign/interview",
                       json.dumps({"text": "we sell running shoes"}).encode())
    assert code == 200 and resp["ok"] is True
    assert resp["data"]["done"] is False
    assert resp["data"]["questions"][0]["type"] == "platforms"
    assert resp["data"]["productContext"].startswith("PRODUCT DESCRIPTION")


def test_interview_rejects_bad_round(panel, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    code, resp = _post(panel["base"] + "/api/campaign/interview",
                       json.dumps({"text": "x", "round": 0}).encode())
    assert code == 400 and "round" in resp["error"]


def test_interview_rejects_malformed_history(panel, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    code, resp = _post(panel["base"] + "/api/campaign/interview", json.dumps({
        "productContext": "ctx", "interview": [{"question": "q"}]}).encode())
    assert code == 400 and "answer" in resp["error"]


def test_interview_accepts_product_context_only(panel, monkeypatch):
    """Later rounds carry the echoed productContext (no url/text/image)."""
    from aizu import campaign_gen
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    captured = {}

    def _run(**kw):
        captured.update(kw)
        return campaign_gen.InterviewResult(done=True, questions=[],
                                            product_context=kw["product_context"])

    monkeypatch.setattr(campaign_gen, "run_interview", _run)
    code, resp = _post(panel["base"] + "/api/campaign/interview", json.dumps({
        "productContext": "PRODUCT DESCRIPTION:\nshoes", "round": 2,
        "interview": [{"question": "Who?", "answer": "runners"}]}).encode())
    assert code == 200 and resp["data"]["done"] is True
    assert captured["round"] == 2
    assert captured["interview"] == [{"question": "Who?", "answer": "runners"}]


def test_generate_forwards_interview_and_platforms(panel, monkeypatch):
    from aizu import campaign_gen
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    captured = {}

    def _gen(**kw):
        captured.update(kw)
        return {"name": "Acme", "platform": "x"}

    monkeypatch.setattr(campaign_gen, "generate_campaign", _gen)
    code, resp = _post(panel["base"] + "/api/campaign/generate", json.dumps({
        "productContext": "ctx", "platforms": ["x", "instagram"],
        "interview": [{"question": "Goal?", "answer": "buyers"}]}).encode())
    assert code == 200 and resp["data"]["platform"] == "x"
    assert captured["product_context"] == "ctx"
    assert captured["platforms"] == ["x", "instagram"]
    assert captured["interview"] == [{"question": "Goal?", "answer": "buyers"}]


def test_generate_rejects_too_many_platforms(panel, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    code, resp = _post(panel["base"] + "/api/campaign/generate", json.dumps({
        "text": "x", "platforms": ["a", "b", "c", "d", "e", "f", "g"]}).encode())
    assert code == 400 and "platforms" in resp["error"]


def test_campaign_brief_include_home_feed_round_trips(panel):
    code, resp = _post(panel["base"] + "/api/campaign", json.dumps({
        "campaignId": "homefeed-test", "status": "draft",
        "brief": {"platform": "instagram", "seedHashtags": ["flutterdev"],
                  "includeHomeFeed": True},
    }).encode())
    assert code == 200 and resp["ok"] is True
    created_id = resp["data"]["campaign_id"]

    from aizu.core.store import Store
    store = Store(panel["db"])
    try:
        stored = store.get_campaign_brief(created_id)
    finally:
        store.close()
    assert stored["include_home_feed"] is True   # explicit override persisted as a bool

    _, raw = _get(panel["base"] + "/api/state")
    camp = next(c for c in json.loads(raw)["CAMPAIGNS"] if c["id"] == created_id)
    assert camp["briefForm"]["includeHomeFeed"] is True


# --- Multi-platform channels wire-format (Phase 4) ---------------------------


def test_channel_to_snake_valid_entry():
    from aizu.server import _channel_to_snake
    out = _channel_to_snake({"platform": "YouTube", "seedHashtags": [" saas ", ""],
                             "seedChannels": ["UC1"], "includeHomeFeed": False})
    assert out == {"platform": "youtube", "seed_hashtags": ["saas"],
                   "seed_channels": ["UC1"], "include_home_feed": False}


def test_channel_to_snake_invalid_platform_returns_none():
    from aizu.server import _channel_to_snake
    assert _channel_to_snake({"platform": "tiktok"}) is None
    assert _channel_to_snake({"platform": ""}) is None
    assert _channel_to_snake("not-a-dict") is None


def test_channel_to_snake_absent_optional_fields():
    from aizu.server import _channel_to_snake
    # No seeds / no includeHomeFeed → only platform emitted (seed-aware default later).
    assert _channel_to_snake({"platform": "instagram"}) == {"platform": "instagram"}


def test_brief_to_snake_channels_branch_translates_and_drops_invalid():
    from aizu.server import _brief_to_snake
    out = _brief_to_snake({"channels": [
        {"platform": "instagram", "seedHashtags": ["a"]},
        {"platform": "tiktok"}, "junk"]})
    assert out["channels"] == [{"platform": "instagram", "seed_hashtags": ["a"]}]


def test_brief_to_snake_channels_absent_key_not_emitted():
    # Absent channels ⇒ not emitted ⇒ the shallow merge preserves stored (no-change).
    assert "channels" not in _brief_to_snake_helper({"platform": "instagram"})


def test_brief_to_snake_channels_empty_list_emitted_as_empty():
    # [] ⇒ emitted ⇒ merge overwrites ⇒ clears to single-platform (C3).
    assert _brief_to_snake_helper({"channels": []})["channels"] == []


def test_brief_to_snake_channels_all_invalid_emits_empty():
    out = _brief_to_snake_helper({"channels": [{"platform": "tiktok"}, "x"]})
    assert out["channels"] == []


def _brief_to_snake_helper(brief):
    from aizu.server import _brief_to_snake
    return _brief_to_snake(brief)


def test_campaign_channels_round_trip_via_api(panel):
    from aizu.core.store import Store
    code, resp = _post(panel["base"] + "/api/campaign", json.dumps({
        "campaignId": "multi-rt", "status": "draft",
        "brief": {"platform": "instagram", "channels": [
            {"platform": "instagram", "seedHashtags": ["a"]},
            {"platform": "youtube", "seedChannels": ["UC1"]}]}}).encode())
    assert code == 200 and resp["ok"] is True
    created_id = resp["data"]["campaign_id"]
    store = Store(panel["db"])
    try:
        stored = store.get_campaign_brief(created_id)
    finally:
        store.close()
    assert [c["platform"] for c in stored["channels"]] == ["instagram", "youtube"]
    _, raw = _get(panel["base"] + "/api/state")
    camp = next(c for c in json.loads(raw)["CAMPAIGNS"] if c["id"] == created_id)
    assert camp["platforms"] == ["instagram", "youtube"]           # C6 chips
    assert [c["platform"] for c in camp["briefForm"]["channels"]] == ["instagram", "youtube"]


def test_campaign_channels_absent_is_no_change(panel):
    from aizu.core.store import Store
    store = Store(panel["db"])
    store.upsert_campaign_brief("nochg", {"platform": "instagram", "channels": [
        {"platform": "instagram"}, {"platform": "youtube"}]},
        org_id=_owner_org_id(panel))
    store.close()
    # A save WITHOUT channels must preserve the stored channels (merge sentinel).
    code, resp = _post(panel["base"] + "/api/campaign", json.dumps({
        "campaignId": "nochg", "status": "draft",
        "brief": {"platform": "instagram", "relevanceDef": "edited"}}).encode())
    assert code == 200 and resp["ok"] is True
    store = Store(panel["db"])
    merged = store.get_campaign_brief("nochg")
    store.close()
    assert [c["platform"] for c in merged["channels"]] == ["instagram", "youtube"]


def test_campaign_channels_empty_list_clears_stored(panel):
    from aizu.core.store import Store
    store = Store(panel["db"])
    store.upsert_campaign_brief("clearch", {"platform": "instagram", "channels": [
        {"platform": "instagram"}, {"platform": "youtube"}]},
        org_id=_owner_org_id(panel))
    store.close()
    # An explicit [] clears the multi-platform fan-out back to single-platform.
    code, resp = _post(panel["base"] + "/api/campaign", json.dumps({
        "campaignId": "clearch", "status": "draft",
        "brief": {"platform": "instagram", "channels": []}}).encode())
    assert code == 200 and resp["ok"] is True
    store = Store(panel["db"])
    merged = store.get_campaign_brief("clearch")
    store.close()
    assert merged["channels"] == []


def test_campaign_rejects_bad_brief_platform(panel):
    code, resp = _post(panel["base"] + "/api/campaign", json.dumps({
        "campaignId": "bad-brief", "brief": {"platform": "tiktok"},
    }).encode())
    assert code == 400 and resp["ok"] is False


def test_campaign_brief_save_merges_and_preserves_prompts(panel):
    from aizu.core.store import Store
    # Pre-store a brief with tuned prompts.
    store = Store(panel["db"])
    store.upsert_campaign_brief("merge-test", {
        "platform": "instagram", "threshold": 0.7, "relevance_def": "old",
        "match_prompt": "TUNED MATCH PROMPT", "vision_prompt": "TUNED VISION"},
        org_id=_owner_org_id(panel))
    store.close()
    # A panel save that leaves the prompt fields blank must NOT wipe them
    # (blank ⇒ keep stored; see _BRIEF_BLANK_DROP_KEYS).
    code, resp = _post(panel["base"] + "/api/campaign", json.dumps({
        "campaignId": "merge-test", "status": "draft",
        "brief": {"platform": "youtube", "relevanceDef": "new relevance", "threshold": 0.8},
    }).encode())
    assert code == 200 and resp["ok"] is True
    store = Store(panel["db"])
    merged = store.get_campaign_brief("merge-test")
    store.close()
    assert merged["platform"] == "youtube"           # incoming overrides
    assert merged["relevance_def"] == "new relevance"
    assert merged["threshold"] == 0.8
    assert merged["match_prompt"] == "TUNED MATCH PROMPT"  # preserved, not clobbered
    assert merged["vision_prompt"] == "TUNED VISION"


def _fresh_store() -> Store:
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    return Store(path)


def test_merge_base_seeds_from_file_campaign_when_no_db_brief():
    """Editing the FILE-backed primary campaign (no DB brief yet) must seed the
    merge base from campaign.md's FULL brief, so a save can't drop the YAML-only
    knobs (escalate_band, enable_actions, caps, seed_direction, tuned prompts)."""
    from aizu.server import _campaign_merge_base
    store = _fresh_store()
    try:
        base = _campaign_merge_base(store, str(CONFIG), _campaign_id())
    finally:
        store.close()
    assert base["enable_actions"] is False           # shipped read-only value, preserved
    assert base["escalate_band"] == [0.4, 0.75]
    # seed_direction + match_prompt are the real "not dropped" proof — these
    # YAML/prose-only knobs would be ABSENT if the base weren't seeded from the file.
    assert base["seed_direction"] and base["match_prompt"]
    # A real edit overlays form fields on this base — knobs survive the merge.
    merged = {**base, **{"relevance_def": "edited", "threshold": 0.66}}
    assert merged["relevance_def"] == "edited" and merged["threshold"] == 0.66
    assert merged["seed_direction"] and merged["escalate_band"] == [0.4, 0.75]


def test_merge_base_is_empty_for_brand_new_campaign():
    """A brand-new UI campaign id matches no file campaign → empty base (nothing
    to preserve), so its brief is exactly the submitted form fields."""
    from aizu.server import _campaign_merge_base
    store = _fresh_store()
    try:
        assert _campaign_merge_base(store, str(CONFIG), "totally-new-campaign-xyz") == {}
    finally:
        store.close()


def test_merge_base_prefers_stored_db_brief():
    """When a DB brief already exists it wins over the file campaign — an edit
    overlays on the stored blob (the pre-existing merge-preserve behaviour)."""
    from aizu.server import _campaign_merge_base
    store = _fresh_store()
    try:
        store.upsert_campaign_brief("ui-camp", {
            "platform": "instagram", "match_prompt": "STORED", "threshold": 0.55})
        base = _campaign_merge_base(store, str(CONFIG), "ui-camp")
    finally:
        store.close()
    assert base["match_prompt"] == "STORED" and base["threshold"] == 0.55


def test_brief_to_snake_persists_prompts_and_drops_blanks():
    from aizu.server import _brief_to_snake
    # A non-empty prompt persists; a blank one is dropped (never clobbers a
    # stored tuned prompt on merge).
    assert _brief_to_snake({"matchPrompt": "TUNED"})["match_prompt"] == "TUNED"
    assert "match_prompt" not in _brief_to_snake({"matchPrompt": ""})
    assert "vision_prompt" not in _brief_to_snake({"visionPrompt": "   "})


def test_campaign_brief_persists_prompts_from_form(panel):
    from aizu.core.store import Store
    code, resp = _post(panel["base"] + "/api/campaign", json.dumps({
        "campaignId": "prompted", "status": "draft",
        "brief": {"platform": "instagram", "extractDef": "- phone\n- email",
                  "matchPrompt": "CUSTOM MATCH PROMPT", "relevancePrompt": "CUSTOM REL"},
    }).encode())
    assert code == 200 and resp["ok"] is True
    created_id = resp["data"]["campaign_id"]
    store = Store(panel["db"])
    try:
        stored = store.get_campaign_brief(created_id)
    finally:
        store.close()
    assert stored["match_prompt"] == "CUSTOM MATCH PROMPT"
    assert stored["relevance_prompt"] == "CUSTOM REL"

    # …and the edit form gets them back to round-trip (no silent blanking).
    _, raw = _get(panel["base"] + "/api/state")
    camp = next(c for c in json.loads(raw)["CAMPAIGNS"] if c["id"] == created_id)
    assert camp["briefForm"]["matchPrompt"] == "CUSTOM MATCH PROMPT"
    assert camp["extractFields"] == ["phone", "email"]


def test_team_create_update_delete_and_dup_email(panel):
    # Direct-add a teammate (real account: email + password + role).
    code, resp = _post(panel["base"] + "/api/team", json.dumps({
        "op": "create", "email": "jane@acme.com", "password": "longenough1", "role": "admin",
    }).encode())
    assert code == 200 and resp["ok"] is True
    user_id = int(resp["data"]["id"])

    dup_code, _ = _post(panel["base"] + "/api/team", json.dumps({
        "op": "create", "email": "jane@acme.com", "password": "longenough1", "role": "member",
    }).encode())
    assert dup_code == 409

    upd_code, _ = _post(panel["base"] + "/api/team", json.dumps({
        "op": "updateRole", "userId": user_id, "role": "viewer",
    }).encode())
    assert upd_code == 200

    del_code, _ = _post(panel["base"] + "/api/team", json.dumps({
        "op": "remove", "userId": user_id,
    }).encode())
    assert del_code == 200
    miss_code, _ = _post(panel["base"] + "/api/team", json.dumps({
        "op": "remove", "userId": user_id,
    }).encode())
    assert miss_code == 404


def test_team_rejects_bad_email(panel):
    code, resp = _post(panel["base"] + "/api/team", json.dumps({
        "op": "create", "email": "not-an-email", "password": "longenough1", "role": "member",
    }).encode())
    assert code == 400 and resp["ok"] is False


def test_settings_whitelist_and_persist(panel):
    code, resp = _post(panel["base"] + "/api/settings", json.dumps({
        "settings": {"productName": "LeadFlow", "matchThreshold": 0.8},
    }).encode())
    assert code == 200 and resp["ok"] is True
    assert resp["data"]["productName"] == "LeadFlow"
    _, raw = _get(panel["base"] + "/api/state")
    cfg = json.loads(raw)["CONFIG"]
    assert cfg["productName"] == "LeadFlow" and cfg["matchThreshold"] == 0.8


def test_settings_rejects_a_non_finite_number(panel):
    """Python's json parser accepts the non-standard `NaN`/`Infinity` literals, and a
    type-only isinstance check let them through: `json.dumps` (allow_nan defaults True)
    wrote the bare token into settings.value — invalid JSON for any other reader — and
    the operator's configured value then read back as null forever, scrubbed by the
    response encoder on every request with no error to explain it. Must be a 400 at the
    boundary, and nothing may be persisted."""
    _, before = _get(panel["base"] + "/api/state")
    threshold_before = json.loads(before)["CONFIG"]["matchThreshold"]
    for literal in (b"Infinity", b"-Infinity", b"NaN"):
        body = b'{"settings":{"budgetCapUsd":' + literal + b',"matchThreshold":0.55}}'
        code, resp = _post(panel["base"] + "/api/settings", body)
        assert code == 400, (literal, resp)
        assert resp["error"] == "budgetCapUsd must be a finite number"
    store = Store(panel["db"])
    try:
        rows = store._conn.execute(
            "SELECT value FROM settings WHERE key='budgetCapUsd'").fetchall()
    finally:
        store.close()
    assert [r["value"] for r in rows] == []      # nothing written, not even a token
    # …and the sibling key in the same rejected payload did not land either.
    _, raw = _get(panel["base"] + "/api/state")
    assert json.loads(raw)["CONFIG"]["matchThreshold"] == threshold_before


def test_settings_rejects_unknown_key(panel):
    code, resp = _post(panel["base"] + "/api/settings", json.dumps({
        "settings": {"hackerKey": "x"},
    }).encode())
    assert code == 400 and resp["ok"] is False


def test_integration_toggle_persists(panel):
    code, resp = _post(panel["base"] + "/api/integration", json.dumps({
        "platform": "youtube", "connected": True, "detail": "@chan",
    }).encode())
    assert code == 200 and resp["ok"] is True
    assert resp["data"]["connected"] == 1
    _, raw = _get(panel["base"] + "/api/state")
    yt = next(i for i in json.loads(raw)["INTEGRATIONS"] if i["platform"] == "youtube")
    assert yt["connected"] is True and yt["source"] == "override"


def test_integration_rejects_unknown_platform(panel):
    code, resp = _post(panel["base"] + "/api/integration", json.dumps({
        "platform": "myspace", "connected": True,
    }).encode())
    assert code == 400 and resp["ok"] is False


def test_new_endpoint_cross_origin_rejected(panel):
    req = urllib.request.Request(
        panel["base"] + "/api/campaign",
        data=json.dumps({"campaignId": _campaign_id()}).encode(),
        headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
        method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
    assert code == 403


# ----- /api/run control plane (PRD §5 revision) -----

def _run_block(panel) -> dict:
    _, raw = _get(panel["base"] + "/api/state")
    return json.loads(raw)["RUN"]


def _wait_run_idle(panel, timeout: float = 5.0) -> dict:
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = _run_block(panel)
        if run["active"] is None:
            return run
        time.sleep(0.02)
    raise AssertionError("run never went idle")


def _reset_runner(panel) -> None:
    panel["spawner"].next_returncode = 0
    panel["spawner"].next_gate = None
    _wait_run_idle(panel)
    # The jobs table is shared across the module-scoped DB; a fleet test that leaves a
    # queued job behind would (now that enqueue is deduped per campaign) block a sibling
    # test's dispatch. Clear it so every run test starts from a clean fleet queue.
    store = Store(panel["db"])
    try:
        with store._tx() as c:
            c.execute("DELETE FROM jobs")
    finally:
        store.close()


def test_run_defaults_to_dry_and_accepts(panel):
    _reset_runner(panel)
    code, resp = _post(panel["base"] + "/api/run",
                       json.dumps({"campaignId": _campaign_id()}).encode())
    assert code == 202 and resp["ok"] is True
    assert resp["data"]["accepted"] is True
    assert resp["data"]["scope"] == "campaign" and resp["data"]["mode"] == "dry"
    _wait_run_idle(panel)


def test_run_all_accepts_and_uses_run_all_argv(panel):
    _reset_runner(panel)
    code, resp = _post(panel["base"] + "/api/run",
                       json.dumps({"all": True, "mode": "dry"}).encode())
    assert code == 202 and resp["data"]["scope"] == "all"
    _wait_run_idle(panel)
    assert any("run-all" in argv for argv in panel["spawner"].calls)


def test_run_rejects_both_and_neither(panel):
    _reset_runner(panel)
    both, _ = _post(panel["base"] + "/api/run",
                    json.dumps({"campaignId": "x", "all": True}).encode())
    neither, _ = _post(panel["base"] + "/api/run", json.dumps({"mode": "dry"}).encode())
    assert both == 400 and neither == 400


def test_run_rejects_bad_mode(panel):
    _reset_runner(panel)
    code, resp = _post(panel["base"] + "/api/run",
                       json.dumps({"all": True, "mode": "wet"}).encode())
    assert code == 400 and resp["ok"] is False


def test_run_rejects_not_runnable_campaign(panel):
    _reset_runner(panel)
    code, resp = _post(panel["base"] + "/api/run",
                       json.dumps({"campaignId": "ghost-campaign", "mode": "dry"}).encode())
    assert code == 400 and "not runnable" in resp["error"]


def test_run_conflict_while_active(panel):
    _reset_runner(panel)
    gate = threading.Event()
    panel["spawner"].next_gate = gate
    try:
        first, _ = _post(panel["base"] + "/api/run",
                         json.dumps({"all": True, "mode": "dry"}).encode())
        assert first == 202
        busy, resp = _post(panel["base"] + "/api/run",
                           json.dumps({"all": True, "mode": "dry"}).encode())
        assert busy == 409 and "already active" in resp["error"]
    finally:
        gate.set()
        panel["spawner"].next_gate = None
    _wait_run_idle(panel)


def test_run_accepts_and_passes_duration(panel):
    _reset_runner(panel)
    before = len(panel["spawner"].calls)
    code, resp = _post(panel["base"] + "/api/run",
                       json.dumps({"campaignId": _campaign_id(), "mode": "live",
                                   "durationMinutes": 120}).encode())
    assert code == 202 and resp["ok"] is True
    _wait_run_idle(panel)
    argv = panel["spawner"].calls[before]
    assert "--duration-minutes" in argv
    assert argv[argv.index("--duration-minutes") + 1] == "120"


def test_run_rejects_bad_duration(panel):
    _reset_runner(panel)
    for bad in (0, -5, 9999):
        code, resp = _post(panel["base"] + "/api/run",
                           json.dumps({"campaignId": _campaign_id(), "mode": "live",
                                       "durationMinutes": bad}).encode())
        assert code == 400 and resp["ok"] is False


def _give_high_cap_plan(panel) -> None:
    """Put the panel's org on a high-cap (Pro) plan so the v13 run lead-cap clamp is a
    no-op — these tests verify target_leads flows to the CLI, not billing enforcement."""
    store = Store(panel["db"])
    try:
        org_id = store._conn.execute(
            "SELECT id FROM organizations ORDER BY id LIMIT 1").fetchone()[0]
        store.upsert_subscription(org_id, last_event_ts=1.0, provider="polar",
                                  tier="pro", status="active")
    finally:
        store.close()


def test_run_accepts_and_passes_target_leads(panel):
    _reset_runner(panel)
    _give_high_cap_plan(panel)
    before = len(panel["spawner"].calls)
    code, resp = _post(panel["base"] + "/api/run",
                       json.dumps({"campaignId": _campaign_id(), "mode": "live",
                                   "targetLeadCount": 25}).encode())
    assert code == 202 and resp["ok"] is True
    _wait_run_idle(panel)
    argv = panel["spawner"].calls[before]
    assert "--target-leads" in argv
    assert argv[argv.index("--target-leads") + 1] == "25"


def test_run_rejects_bad_target_leads(panel):
    _reset_runner(panel)
    for bad in (0, -5, 99999, True):
        code, resp = _post(panel["base"] + "/api/run",
                           json.dumps({"campaignId": _campaign_id(), "mode": "live",
                                       "targetLeadCount": bad}).encode())
        assert code == 400 and resp["ok"] is False


def test_run_accepts_target_leads_with_duration_cap(panel):
    _reset_runner(panel)
    _give_high_cap_plan(panel)
    before = len(panel["spawner"].calls)
    code, _ = _post(panel["base"] + "/api/run",
                    json.dumps({"campaignId": _campaign_id(), "mode": "live",
                                "targetLeadCount": 50, "durationMinutes": 120}).encode())
    assert code == 202
    _wait_run_idle(panel)
    argv = panel["spawner"].calls[before]
    assert argv[argv.index("--target-leads") + 1] == "50"
    assert argv[argv.index("--duration-minutes") + 1] == "120"


def test_run_stop_when_idle_returns_409(panel):
    _reset_runner(panel)
    code, resp = _post(panel["base"] + "/api/run/stop", b"{}")
    assert code == 409 and "no run is active" in resp["error"]


def test_run_stop_terminates_active_run(panel):
    _reset_runner(panel)
    gate = threading.Event()
    panel["spawner"].next_gate = gate
    try:
        started, _ = _post(panel["base"] + "/api/run",
                           json.dumps({"campaignId": _campaign_id(), "mode": "live",
                                       "durationMinutes": 60}).encode())
        assert started == 202
        assert _run_block(panel)["active"] is not None
        code, resp = _post(panel["base"] + "/api/run/stop", b"{}")
        assert code == 200 and resp["data"]["stopped"] is True
    finally:
        gate.set()
        panel["spawner"].next_gate = None
    run = _wait_run_idle(panel)
    assert run["recent"][0]["outcome"] == "aborted"


def test_run_pause_resume_when_idle_returns_409(panel):
    _reset_runner(panel)
    pause_code, presp = _post(panel["base"] + "/api/run/pause", b"{}")
    resume_code, rresp = _post(panel["base"] + "/api/run/resume", b"{}")
    assert pause_code == 409 and "no run is active" in presp["error"]
    assert resume_code == 409 and "no run is active" in rresp["error"]


def test_run_pause_then_resume_reflects_in_run_block(panel):
    _reset_runner(panel)
    gate = threading.Event()
    panel["spawner"].next_gate = gate
    try:
        started, _ = _post(panel["base"] + "/api/run",
                           json.dumps({"campaignId": _campaign_id(), "mode": "live",
                                       "durationMinutes": 60}).encode())
        assert started == 202
        # Pause → RUN block shows paused; re-pause is idempotent (200).
        code, resp = _post(panel["base"] + "/api/run/pause", b"{}")
        assert code == 200 and resp["data"]["paused"] is True
        assert _run_block(panel)["active"]["paused"] is True
        again, _ = _post(panel["base"] + "/api/run/pause", b"{}")
        assert again == 200
        # Resume → RUN block clears paused.
        code, resp = _post(panel["base"] + "/api/run/resume", b"{}")
        assert code == 200 and resp["data"]["paused"] is False
        assert _run_block(panel)["active"]["paused"] is False
    finally:
        gate.set()
        panel["spawner"].next_gate = None
    _wait_run_idle(panel)


def test_run_cross_origin_rejected(panel):
    req = urllib.request.Request(
        panel["base"] + "/api/run",
        data=json.dumps({"all": True, "mode": "dry"}).encode(),
        headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
        method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
    assert code == 403


def test_run_block_surfaces_active_then_recent(panel):
    _reset_runner(panel)
    gate = threading.Event()
    panel["spawner"].next_gate = gate
    try:
        code, _ = _post(panel["base"] + "/api/run",
                        json.dumps({"campaignId": _campaign_id(), "mode": "dry"}).encode())
        assert code == 202
        active = _run_block(panel)["active"]
        assert active is not None
        assert active["scope"] == "campaign" and active["campaignId"] == _campaign_id()
        assert active["mode"] == "dry" and active["startedAt"]
    finally:
        gate.set()
        panel["spawner"].next_gate = None
    run = _wait_run_idle(panel)
    assert run["recent"] and run["recent"][0]["outcome"] == "ok"
    assert run["recent"][0]["scope"] == "campaign"


# ----- v10: /api/run/activity live feed -----

def _activity(panel, qs: str) -> tuple[int, dict]:
    """GET the activity endpoint with the panel session cookie; never raises on 4xx."""
    req = urllib.request.Request(panel["base"] + "/api/run/activity" + qs,
                                 headers={"Cookie": panel["cookie"]})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _seed_run_activity(db_path: str, run_id: str = "run-seed") -> None:
    """Seed a finished session + its narrative events for the file campaign (already
    registered to the panel's org by the fixture), so the feed is org-owned."""
    from aizu.core.store import SessionCounters
    store = Store(db_path)
    cid = _campaign_id()
    try:
        store.start_session("sess-seed", cid, "instagram", run_id=run_id)
        steps = [("lifecycle", "info", "Run started — campaign x (instagram)"),
                 ("relevance", "success", "Relevant ✓ — @acme.io"),
                 ("comments", "success", "Match: @aziz (score 0.82)")]
        for i, (phase, level, msg) in enumerate(steps, start=1):
            store.emit_run_event(run_id, i, phase, level, msg,
                                 campaign_id=cid, session_id="sess-seed")
        store.update_counters("sess-seed", SessionCounters(
            reels_seen=5, relevance_passes=2, comments_scored=10, matches=1, spend_usd=0.02))
        store.log_action(cid, "like", reel_id="r1", target="r1",
                         succeeded=True, session_id="sess-seed")
        store.end_session("sess-seed", "completed")
    finally:
        store.close()


def test_run_activity_requires_session(panel):
    req = urllib.request.Request(panel["base"] + "/api/run/activity?runId=x")
    try:
        urllib.request.urlopen(req)
        assert False, "expected 401"
    except urllib.error.HTTPError as e:
        assert e.code == 401


def test_run_activity_requires_run_id(panel):
    code, resp = _activity(panel, "")
    assert code == 400
    assert resp["ok"] is False and "runId" in (resp["error"] or "")


def test_run_activity_unknown_run_is_404(panel):
    code, resp = _activity(panel, "?runId=does-not-exist")
    assert code == 404
    assert resp["ok"] is False


def test_run_activity_returns_events_and_counters(panel):
    _seed_run_activity(panel["db"], run_id="run-act")
    code, resp = _activity(panel, "?runId=run-act")
    assert code == 200 and resp["ok"] is True
    data = resp["data"]
    assert data["runId"] == "run-act"
    assert data["finished"] is True            # session ended → not live
    msgs = [e["message"] for e in data["events"]]
    assert any(m.startswith("Run started") for m in msgs)
    assert any(m.startswith("Match: @") for m in msgs)
    # Each event carries the global id (cursor/key) and the per-session seq.
    assert all("id" in e and "seq" in e for e in data["events"])
    # C7: each event is tagged with its session's platform (LEFT JOIN, no DDL).
    assert all(e["platform"] == "instagram" for e in data["events"])
    c = data["counters"]
    assert c["reelsSeen"] == 5 and c["matches"] == 1 and c["likes"] == 1
    assert data["cursor"] == data["events"][-1]["id"]


def test_run_activity_multi_platform_events_carry_correct_platform(panel):
    """A multi-platform run tags each event with its own session's platform — so the
    activity feed can show which channel produced each line (C7)."""
    from aizu.core.store import Store
    cid = _campaign_id()
    store = Store(panel["db"])
    try:
        store.start_session("sess-ig", cid, "instagram", run_id="run-multi")
        store.emit_run_event("run-multi", 1, "lifecycle", "info", "ig start",
                             campaign_id=cid, session_id="sess-ig")
        store.end_session("sess-ig", "completed")
        store.start_session("sess-yt", cid, "youtube", run_id="run-multi")
        store.emit_run_event("run-multi", 1, "lifecycle", "info", "yt start",
                             campaign_id=cid, session_id="sess-yt")
        store.end_session("sess-yt", "completed")
    finally:
        store.close()
    code, resp = _activity(panel, "?runId=run-multi")
    assert code == 200
    by_msg = {e["message"]: e["platform"] for e in resp["data"]["events"]}
    assert by_msg["ig start"] == "instagram"
    assert by_msg["yt start"] == "youtube"


def test_run_activity_after_cursor_returns_only_newer(panel):
    _seed_run_activity(panel["db"], run_id="run-cur")
    code, resp = _activity(panel, "?runId=run-cur")
    assert code == 200
    first_id = resp["data"]["events"][0]["id"]
    code2, resp2 = _activity(panel, f"?runId=run-cur&after={first_id}")
    ids = [e["id"] for e in resp2["data"]["events"]]
    assert ids and all(i > first_id for i in ids)


# ----- FIX 2: fleet-run lifecycle exposed to the panel ---------------------------

def _org_id(db_path: str) -> int:
    store = Store(db_path)
    try:
        return store._conn.execute(
            "SELECT id FROM organizations ORDER BY id LIMIT 1").fetchone()[0]
    finally:
        store.close()


def _enqueue_fleet_job(db_path: str, *, job_id: str, run_id: str,
                       campaign_id: str, status: str = "queued") -> None:
    """Seed a fleet job (spec carries run_id) for the panel's org, driving it to the
    given status. The module DB is SHARED across tests, so we set the target status
    with a targeted UPDATE (not lease_one_job, which would pick an arbitrary queued
    job) to stay deterministic and order-independent."""
    org_id = _org_id(db_path)
    store = Store(db_path)
    try:
        store.enqueue_job(job_id=job_id, campaign_id=campaign_id, platform="instagram",
                          org_id=org_id, required_account_handle=None,
                          spec={"run_id": run_id, "engine_mode": "harvest"})
        if status != "queued":
            lease = 1e12 if status in ("leased", "running") else None
            store._conn.execute(
                "UPDATE jobs SET status=?, lease_expires_at=? WHERE id=?",
                (status, lease, job_id))
            store._conn.commit()
    finally:
        store.close()


def test_run_activity_includes_fleet_job_for_a_fleet_run(panel):
    cid = _campaign_id()
    _enqueue_fleet_job(panel["db"], job_id="jf1", run_id="run-fleet",
                       campaign_id=cid, status="leased")
    _seed_run_activity(panel["db"], run_id="run-fleet")
    code, resp = _activity(panel, "?runId=run-fleet")
    assert code == 200
    fj = resp["data"]["fleetJob"]
    assert fj is not None
    assert fj["jobId"] == "jf1"
    assert fj["status"] == "leased"
    assert fj["lastEventAt"] is not None  # events were seeded → MAX(created_at)
    assert "leaseExpiresAt" in fj


def test_run_activity_fleet_job_null_for_in_process_run(panel):
    # No job row for this run → in-process run → fleetJob is null.
    _seed_run_activity(panel["db"], run_id="run-inproc")
    code, resp = _activity(panel, "?runId=run-inproc")
    assert code == 200
    assert resp["data"]["fleetJob"] is None


def test_run_activity_finished_override_alive_job_forces_false(panel):
    # A leased (alive) job forces finished False even with no running session:
    # seed only the job (no session), so session-based logic would say finished.
    cid = _campaign_id()
    _enqueue_fleet_job(panel["db"], job_id="jf2", run_id="run-alive",
                       campaign_id=cid, status="leased")
    # Emit one event so the run is org-owned (no session opened).
    store = Store(panel["db"])
    try:
        store.emit_run_event("run-alive", 1, "lifecycle", "info", "queued",
                             campaign_id=cid, org_id=_org_id(panel["db"]))
    finally:
        store.close()
    code, resp = _activity(panel, "?runId=run-alive")
    assert code == 200
    assert resp["data"]["fleetJob"]["status"] == "leased"
    assert resp["data"]["finished"] is False


def test_run_activity_finished_override_done_job_forces_true(panel):
    cid = _campaign_id()
    _enqueue_fleet_job(panel["db"], job_id="jf3", run_id="run-terminal",
                       campaign_id=cid, status="done")
    _seed_run_activity(panel["db"], run_id="run-terminal")
    code, resp = _activity(panel, "?runId=run-terminal")
    assert code == 200
    assert resp["data"]["fleetJob"]["status"] == "done"
    assert resp["data"]["finished"] is True


def test_run_activity_expired_lease_lets_dead_run_finish(panel):
    # A leased job whose lease already expired (worker died mid-run) must NOT pin
    # finished=false forever — it falls back to the session-derived value so the
    # drawer can terminate rather than poll on a dead run.
    cid = _campaign_id()
    _enqueue_fleet_job(panel["db"], job_id="jf-dead", run_id="run-dead",
                       campaign_id=cid, status="leased")
    store = Store(panel["db"])
    try:
        store._conn.execute(
            "UPDATE jobs SET lease_expires_at=? WHERE id=?", (1.0, "jf-dead"))
        store._conn.commit()
    finally:
        store.close()
    _seed_run_activity(panel["db"], run_id="run-dead")  # a finished session exists
    code, resp = _activity(panel, "?runId=run-dead")
    assert code == 200
    assert resp["data"]["fleetJob"]["status"] == "leased"
    assert resp["data"]["finished"] is True


def test_run_activity_done_without_mirrored_session_keeps_polling(panel):
    # ack commits status='done' microseconds before the best-effort session mirror;
    # a poll in that window must keep polling (finished=false) so the drawer shows
    # real counters, not the zeros of an un-mirrored session.
    cid = _campaign_id()
    _enqueue_fleet_job(panel["db"], job_id="jf-race", run_id="run-race",
                       campaign_id=cid, status="done")
    # Only an event (org-ownership), NO session row yet — the mirror hasn't landed.
    store = Store(panel["db"])
    try:
        store.emit_run_event("run-race", 1, "lifecycle", "info", "Run started",
                             campaign_id=cid, org_id=_org_id(panel["db"]))
    finally:
        store.close()
    code, resp = _activity(panel, "?runId=run-race")
    assert code == 200
    assert resp["data"]["fleetJob"]["status"] == "done"
    assert resp["data"]["finished"] is False


def _nack_fleet_job(db_path: str, *, job_id: str, reason: str,
                    poison: bool = False, worker_id: str = "w-nack") -> dict:
    """Drive a seeded job through a REAL nack (lease it to `worker_id` with a targeted
    UPDATE first, mirroring `_enqueue_fleet_job`'s determinism on the shared module DB).
    poison=True dead-letters immediately; otherwise it requeues with backoff."""
    store = Store(db_path)
    try:
        store._conn.execute(
            "UPDATE jobs SET status='leased', leased_by=?, lease_expires_at=? WHERE id=?",
            (worker_id, 1e12, job_id))
        store._conn.commit()
        return store.nack_job(job_id=job_id, worker_id=worker_id, reason=reason,
                              poison=poison)
    finally:
        store.close()


def test_run_activity_surfaces_the_fleet_failure_reason(panel):
    """B6: a fleet run whose worker could not attach Chrome must not read as a blank red
    'Finished on the fleet' — the nack reason has to cross the HTTP boundary."""
    cid = _campaign_id()
    _enqueue_fleet_job(panel["db"], job_id="jf-why", run_id="run-why", campaign_id=cid)
    out = _nack_fleet_job(panel["db"], job_id="jf-why", reason="cdp_unreachable",
                          poison=True)
    assert out["outcome"] == "dead_lettered"
    _seed_run_activity(panel["db"], run_id="run-why")
    code, resp = _activity(panel, "?runId=run-why")
    assert code == 200
    fj = resp["data"]["fleetJob"]
    assert fj["status"] == "failed"
    assert fj["reason"] == "cdp_unreachable"
    assert fj["attempts"] == 1
    assert fj["maxAttempts"] >= 1


def test_run_activity_surfaces_the_reason_of_a_requeued_attempt(panel):
    # A requeued (still-alive) job carries its last failure too, so the drawer can say
    # why the run is stuck retrying instead of showing an unexplained queued job.
    cid = _campaign_id()
    _enqueue_fleet_job(panel["db"], job_id="jf-retry", run_id="run-retry",
                       campaign_id=cid)
    out = _nack_fleet_job(panel["db"], job_id="jf-retry", reason="cdp_unreachable")
    assert out["outcome"] == "requeued"
    _seed_run_activity(panel["db"], run_id="run-retry")
    code, resp = _activity(panel, "?runId=run-retry")
    assert code == 200
    assert resp["data"]["fleetJob"]["status"] == "queued"
    assert resp["data"]["fleetJob"]["reason"] == "cdp_unreachable"


def test_run_activity_reads_halt_reason_off_an_acked_summary(panel):
    # ack overwrites `result` with the engine summary, whose equivalent key is
    # `halt_reason` — surface that too, while `status` stays 'done' so the panel never
    # labels a successful run failed.
    cid = _campaign_id()
    _enqueue_fleet_job(panel["db"], job_id="jf-halt", run_id="run-halt", campaign_id=cid)
    store = Store(panel["db"])
    try:
        store._conn.execute(
            "UPDATE jobs SET status='leased', leased_by='w-ack', lease_expires_at=? "
            "WHERE id=?", (1e12, "jf-halt"))
        store._conn.commit()
        store.ack_job(job_id="jf-halt", worker_id="w-ack",
                      summary={"halt_reason": "daytime", "matches": 0})
    finally:
        store.close()
    _seed_run_activity(panel["db"], run_id="run-halt")
    code, resp = _activity(panel, "?runId=run-halt")
    assert code == 200
    assert resp["data"]["fleetJob"]["status"] == "done"
    assert resp["data"]["fleetJob"]["reason"] == "daytime"


def test_run_activity_reason_is_null_for_a_job_that_never_failed(panel):
    cid = _campaign_id()
    _enqueue_fleet_job(panel["db"], job_id="jf-clean", run_id="run-clean",
                       campaign_id=cid, status="leased")
    _seed_run_activity(panel["db"], run_id="run-clean")
    code, resp = _activity(panel, "?runId=run-clean")
    assert code == 200
    assert resp["data"]["fleetJob"]["reason"] is None


def _register_campaign(db_path: str, campaign_id: str) -> None:
    """Register a bare campaign_meta row so it surfaces as a card on /api/campaigns
    (the module-scoped DB is shared, so each fleetRunId test uses its own campaign to
    stay order-independent)."""
    store = Store(db_path)
    try:
        store.upsert_campaign_meta(campaign_id, org_id=_org_id(db_path), status="live")
    finally:
        store.close()


def test_campaigns_endpoint_includes_fleet_run_id(panel):
    cid = "fix2-card-active"
    _register_campaign(panel["db"], cid)
    _enqueue_fleet_job(panel["db"], job_id="jf4", run_id="run-card",
                       campaign_id=cid, status="running")
    code, body = _get(panel["base"] + "/api/campaigns")
    assert code == 200
    data = json.loads(body)
    card = next(c for c in data["CAMPAIGNS"] if c["id"] == cid)
    assert card["fleetRunId"] == "run-card"


def test_campaigns_endpoint_fleet_run_id_null_without_active_job(panel):
    cid = "fix2-card-idle"
    _register_campaign(panel["db"], cid)  # registered but never dispatched
    code, body = _get(panel["base"] + "/api/campaigns")
    assert code == 200
    data = json.loads(body)
    card = next(c for c in data["CAMPAIGNS"] if c["id"] == cid)
    assert card["fleetRunId"] is None


# ----- v16 execution-backend routing (in-process vs distributed fleet) -----------

def test_run_live_routes_to_fleet_when_backend_is_distributed(panel):
    _reset_runner(panel)
    store = Store(panel["db"])
    try:
        org_id = store._conn.execute(
            "SELECT id FROM organizations ORDER BY id LIMIT 1").fetchone()[0]
        store.register_worker(worker_id="w-fleet", token="tok-fleet", org_id=org_id,
                              capabilities=[[org_id, "instagram", "acme"]])
        store.set_execution_backend("distributed")
    finally:
        store.close()
    before = len(panel["spawner"].calls)
    try:
        code, resp = _post(panel["base"] + "/api/run",
                           json.dumps({"campaignId": _campaign_id(),
                                       "mode": "live"}).encode())
        assert code == 202, resp
        assert resp["data"]["backend"] == "distributed"
        assert len(resp["data"]["jobs"]) == 1
        assert len(panel["spawner"].calls) == before   # NOT spawned in-process
        store = Store(panel["db"])
        try:
            job = store.get_job(resp["data"]["jobs"][0])
            assert job["status"] == "queued" and job["campaignId"] == _campaign_id()
            # B4: the resolved brief is baked into the spec at enqueue time (mirrors
            # soul_text) so a remote worker with no shared DB row can still resolve
            # the campaign — see job_runner._resolve_campaign.
            expected_brief = campaign_to_brief(
                resolve_campaign(store, CONFIG, _campaign_id()))
            assert job["spec"]["campaign_brief"] == expected_brief
        finally:
            store.close()
    finally:
        store = Store(panel["db"])
        try:
            store.set_execution_backend("in_process")  # restore for sibling tests
        finally:
            store.close()


def test_double_run_to_fleet_does_not_duplicate_the_job(panel):
    """A rapid double-click Run must not enqueue two job sets for the same campaign —
    the second dispatch finds the first job still in flight and is deduped (409)."""
    _reset_runner(panel)
    store = Store(panel["db"])
    try:
        org_id = store._conn.execute(
            "SELECT id FROM organizations ORDER BY id LIMIT 1").fetchone()[0]
        store.register_worker(worker_id="w-dup", token="tok-dup", org_id=org_id,
                              capabilities=[[org_id, "instagram", "acme"]])
        store.set_execution_backend("distributed")
    finally:
        store.close()
    body = json.dumps({"campaignId": _campaign_id(), "mode": "live"}).encode()
    try:
        code1, resp1 = _post(panel["base"] + "/api/run", body)
        code2, resp2 = _post(panel["base"] + "/api/run", body)
        assert code1 == 202 and len(resp1["data"]["jobs"]) == 1
        # Second identical run: nothing new enqueued → 409 'already running'.
        assert code2 == 409, resp2
        store = Store(panel["db"])
        try:
            active = store._conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE campaign_id=? AND status IN "
                "('queued','leased','running')", (_campaign_id(),)).fetchone()[0]
            assert active == 1   # exactly one, not two
        finally:
            store.close()
    finally:
        store = Store(panel["db"])
        try:
            store.set_execution_backend("in_process")
        finally:
            store.close()


def test_run_dry_stays_in_process_even_in_distributed_mode(panel):
    _reset_runner(panel)
    store = Store(panel["db"])
    try:
        store.set_execution_backend("distributed")
    finally:
        store.close()
    before = len(panel["spawner"].calls)
    try:
        code, resp = _post(panel["base"] + "/api/run",
                           json.dumps({"campaignId": _campaign_id(),
                                       "mode": "dry"}).encode())
        assert code == 202, resp
        assert resp["data"].get("backend") != "distributed"   # dry → in-process
        _wait_run_idle(panel)
        assert len(panel["spawner"].calls) == before + 1
    finally:
        store = Store(panel["db"])
        try:
            store.set_execution_backend("in_process")
        finally:
            store.close()


# ----- v16 lead-budget split (distributed scope='all' must never N× the cap) -----

def test_split_lead_budget_sums_to_total():
    from aizu.server import _split_lead_budget
    assert _split_lead_budget(250, 4) == [63, 63, 62, 62]
    assert sum(_split_lead_budget(250, 4)) == 250
    assert _split_lead_budget(10, 1) == [10]          # single campaign → full remainder
    assert _split_lead_budget(3, 5) == [1, 1, 1, 0, 0]  # budget < campaigns → tail skipped
    assert _split_lead_budget(0, 3) == [0, 0, 0]
    assert _split_lead_budget(9, 0) == []             # no dispatchable campaigns


def test_run_all_distributed_enqueues_live_campaigns(panel):
    _reset_runner(panel)
    cid = _campaign_id()
    store = Store(panel["db"])
    try:
        org_id = store._conn.execute(
            "SELECT id FROM organizations ORDER BY id LIMIT 1").fetchone()[0]
        store.register_worker(worker_id="w-all", token="tok-all", org_id=org_id,
                              capabilities=[[org_id, "instagram", "acme"]])
        # Force the file campaign live + unarchived (sibling lifecycle tests mutate it on
        # the shared module DB) so the status='live' AND not-archived filter includes it.
        store.upsert_campaign_meta(cid, status="live")
        with store._tx() as c:
            c.execute("UPDATE campaign_meta SET archived_at=NULL WHERE campaign_id=?", (cid,))
        store.set_execution_backend("distributed")
    finally:
        store.close()
    before = len(panel["spawner"].calls)
    try:
        code, resp = _post(panel["base"] + "/api/run",
                           json.dumps({"all": True, "mode": "live"}).encode())
        assert code == 202, resp
        assert resp["data"]["backend"] == "distributed"
        assert len(panel["spawner"].calls) == before  # nothing spawned in-process
        store = Store(panel["db"])
        try:
            campaigns = {store.get_job(j)["campaignId"] for j in resp["data"]["jobs"]}
        finally:
            store.close()
        assert cid in campaigns  # the live file campaign was dispatched to the fleet
    finally:
        store = Store(panel["db"])
        try:
            store.set_execution_backend("in_process")
        finally:
            store.close()


def test_fleet_dispatch_skips_campaign_whose_brief_exceeds_the_cap(panel):
    """A campaign whose resolved brief serializes past MAX_CAMPAIGN_BRIEF_BYTES must
    be skipped (reason 'brief too large'), never crash the dispatch or ship a
    truncated/missing brief. scope='all' alongside the normal file campaign proves a
    single oversized campaign doesn't 409 the whole batch."""
    _reset_runner(panel)
    cid = _campaign_id()
    oversized_cid = "fix-oversized-brief"
    store = Store(panel["db"])
    try:
        org_id = store._conn.execute(
            "SELECT id FROM organizations ORDER BY id LIMIT 1").fetchone()[0]
        store.register_worker(worker_id="w-oversize", token="tok-oversize", org_id=org_id,
                              capabilities=[[org_id, "instagram", "acme"]])
        store.upsert_campaign_meta(cid, status="live")
        with store._tx() as c:
            c.execute("UPDATE campaign_meta SET archived_at=NULL WHERE campaign_id=?", (cid,))
        # A brief whose one seed field alone pushes the JSON encoding past the cap.
        huge_brief = {"platform": "instagram", "goal": "lead",
                      "seed_direction": "x" * (MAX_CAMPAIGN_BRIEF_BYTES + 1)}
        store.upsert_campaign_brief(oversized_cid, huge_brief, org_id=org_id)
        store.upsert_campaign_meta(oversized_cid, org_id=org_id, status="live")
        store.set_execution_backend("distributed")
    finally:
        store.close()
    try:
        code, resp = _post(panel["base"] + "/api/run",
                           json.dumps({"all": True, "mode": "live"}).encode())
        assert code == 202, resp
        skip = next(s for s in resp["data"]["skipped"] if s["campaignId"] == oversized_cid)
        assert skip["reason"] == "brief too large"
        store = Store(panel["db"])
        try:
            campaigns = {store.get_job(j)["campaignId"] for j in resp["data"]["jobs"]}
        finally:
            store.close()
        assert oversized_cid not in campaigns
        assert cid in campaigns  # the normal campaign still ran
    finally:
        store = Store(panel["db"])
        try:
            store.set_execution_backend("in_process")
        finally:
            store.close()


def test_fleet_dispatch_never_bakes_the_orgs_platform_credentials(panel, monkeypatch):
    """SECURITY REVIEW CRITICAL — the regression test for the fix. `_dispatch_run_to_fleet`
    used to call store.get_integration_secret (decrypting the org's Fernet-at-rest secret)
    and write the plaintext into the enqueued job's `spec`, which JSON-serializes
    UNENCRYPTED into the `jobs.spec` TEXT column — and nothing ever scrubs it back out
    (ack_job only touches status/result/session_id/leased_by; no DELETE/prune of `jobs`
    exists anywhere in store.py), so it would sit there in the cloud DB forever, undoing
    core/secrets.py's Fernet-at-rest protection.

    Asserted on the RAW DB column (not store.get_job's decoded dict) so this fails loud
    even if a future change reintroduces the key under a different name, a nested shape,
    or via some other decode path this test doesn't anticipate — the connected secret's
    live value must never appear in that column's text at all."""
    monkeypatch.setenv("AIZU_SECRET_KEY", SecretCipher.generate_key())
    _reset_runner(panel)
    cid = "sec-yt-creds"
    store = Store(panel["db"])
    try:
        org_id = store._conn.execute(
            "SELECT id FROM organizations ORDER BY id LIMIT 1").fetchone()[0]
        store.upsert_campaign_brief(
            cid, {"platform": "youtube", "goal": "lead", "seed_channels": ["UC_abc"]},
            org_id=org_id)
        store.upsert_campaign_meta(cid, org_id=org_id, status="live")
        store.set_integration_secret(org_id, "youtube", {"api_key": "ORG-KEY-PLAINTEXT"})
        store.register_worker(worker_id="w-yt-creds", token="tok-yt-creds", org_id=org_id,
                              capabilities=[[org_id, "youtube", None]])
        store.set_execution_backend("distributed")
    finally:
        store.close()
    try:
        code, resp = _post(panel["base"] + "/api/run",
                           json.dumps({"campaignId": cid, "mode": "live"}).encode())
        assert code == 202, resp
        job_id = resp["data"]["jobs"][0]
        store = Store(panel["db"])
        try:
            raw_spec = store._conn.execute(
                "SELECT spec FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        finally:
            store.close()
        # The negative assertion IS the point of this test: neither the key nor the
        # decrypted value may appear anywhere in the raw column text.
        assert "platform_credentials" not in raw_spec
        assert "platformCredentials" not in raw_spec
        assert "ORG-KEY-PLAINTEXT" not in raw_spec
        # campaign_brief (the actual B4 fix) must still be there — this test must fail
        # if a future edit removes baking entirely rather than just the credential.
        assert "campaign_brief" in raw_spec
    finally:
        store = Store(panel["db"])
        try:
            store.set_execution_backend("in_process")
        finally:
            store.close()


# --- FIX 3: connection-drop hardening ----------------------------------------

def test_client_disconnect_midrequest_does_not_crash_server(panel):
    """A client that opens a connection, sends a partial request line, then slams
    the socket shut (mimicking dev_panel restarting the server mid-long-poll)
    must not take the server down: a subsequent normal request still succeeds."""
    host = "127.0.0.1"
    port = int(panel["base"].rsplit(":", 1)[1])

    # Open a raw socket, dribble a partial request, abort with RST, and close —
    # this triggers BrokenPipe/ConnectionReset inside handle_one_request.
    sock = socket.create_connection((host, port), timeout=5)
    try:
        sock.sendall(b"GET /api/state HTTP/1.1\r\nHost: x\r\n")  # headers unterminated
        # Force an abortive close (RST) so the server's read/write raises.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                        __import__("struct").pack("ii", 1, 0))
    finally:
        sock.close()

    # The server thread must still be alive and serving: a normal request works.
    code, body = _get(panel["base"] + "/api/state")
    assert code == 200, body
    assert json.loads(body)  # well-formed JSON still returned


def test_json_response_always_sets_content_length(panel):
    """Every JSON response path frames its body with Content-Length. This is the
    framing invariant the connection-drop guard relies on (and the prerequisite for
    any future HTTP/1.1 keep-alive). Assert it on both a 200 and a 4xx body by
    reading the raw headers off the socket."""
    host = "127.0.0.1"
    port = int(panel["base"].rsplit(":", 1)[1])
    cookie = panel["cookie"]

    def _raw_headers(request: str) -> bytes:
        sock = socket.create_connection((host, port), timeout=5)
        try:
            sock.sendall(request.encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(512)
                if not chunk:
                    break
                buf += chunk
            return buf.split(b"\r\n\r\n", 1)[0]
        finally:
            sock.close()

    ok_headers = _raw_headers(
        f"GET /api/state HTTP/1.0\r\nHost: {host}\r\nCookie: {cookie}\r\n\r\n")
    assert b"HTTP/1.0 200" in ok_headers, ok_headers
    assert b"Content-Length:" in ok_headers, ok_headers

    err_headers = _raw_headers(
        f"GET /api/does-not-exist HTTP/1.0\r\nHost: {host}\r\nCookie: {cookie}\r\n\r\n")
    assert b"404" in err_headers, err_headers
    assert b"Content-Length:" in err_headers, err_headers


# ----- B9: the enqueue-time spend-cap skip ---------------------------------------

def test_fleet_spend_cap_usd_is_none_when_the_bridge_has_no_cap(monkeypatch):
    """REVIEW FIX: `AIZU_SPEND_CAP` is a WORKER-plane var — on the hosted split topology
    the bridge does not have it. Falling back to a hard-coded 20.0 would have the cloud
    enforce a ceiling no box uses, and since `total_spend` is a lifetime sum that never
    resets, any campaign past $20 of rolled-up fleet spend would 409 forever with no
    operator control able to lift it. Unknown cloud-side cap MUST mean 'do not skip'."""
    from aizu.server import _fleet_spend_cap_usd
    monkeypatch.delenv("AIZU_SPEND_CAP", raising=False)
    assert _fleet_spend_cap_usd() is None
    monkeypatch.setenv("AIZU_SPEND_CAP", "5.5")
    assert _fleet_spend_cap_usd() == 5.5
    for bad in ("not-a-number", "0", "-3", ""):
        monkeypatch.setenv("AIZU_SPEND_CAP", bad)
        assert _fleet_spend_cap_usd() is None


def test_fleet_dispatch_does_not_skip_when_the_bridge_knows_no_cap(panel, monkeypatch):
    """REVIEW FIX (the hosted split): bridge without AIZU_SPEND_CAP, boxes with a real
    one. A campaign well past the old 20.0 guess must still dispatch — the box's own cap
    (re-based by priorSpendUsd, refused pre-spawn by run_one_job when there is no
    headroom) is the only authority."""
    monkeypatch.delenv("AIZU_SPEND_CAP", raising=False)
    _reset_runner(panel)
    rich_cid = "fix-cap-unknown"
    store = Store(panel["db"])
    try:
        org_id = store._conn.execute(
            "SELECT id FROM organizations ORDER BY id LIMIT 1").fetchone()[0]
        store.register_worker(worker_id="w-cap3", token="tok-cap3", org_id=org_id,
                              capabilities=[[org_id, "instagram", "acme"]])
        store.upsert_campaign_brief(rich_cid, {"platform": "instagram", "goal": "lead"},
                                    org_id=org_id)
        store.upsert_campaign_meta(rich_cid, org_id=org_id, status="live")
        store.log_spend(rich_cid, "match", 500.0, model="m1")  # way past the old guess
        store.set_execution_backend("distributed")
    finally:
        store.close()
    try:
        code, resp = _post(panel["base"] + "/api/run",
                           json.dumps({"campaignId": rich_cid, "mode": "live"}).encode())
        assert code == 202, resp
        assert resp["data"]["jobs"], resp
    finally:
        store = Store(panel["db"])
        try:
            store.set_execution_backend("in_process")
        finally:
            store.close()


def test_fleet_dispatch_skips_a_campaign_already_at_its_spend_cap(panel, monkeypatch):
    """B9's sharp edge. Before the cloud spend total rode the lease, a fresh box always
    started at $0, so an over-budget campaign never tripped `router._spend_guard` on call
    one. Now it can — and `_degrade` does NOT stop a run, it returns an abstain-with-low-
    confidence stand-in, so the job would hold a warmed account for a full duration-capped
    run producing nothing but degraded verdicts. Never enqueue it in the first place."""
    monkeypatch.setenv("AIZU_SPEND_CAP", "20.0")
    _reset_runner(panel)
    cid = _campaign_id()
    broke_cid = "fix-over-budget"
    store = Store(panel["db"])
    try:
        org_id = store._conn.execute(
            "SELECT id FROM organizations ORDER BY id LIMIT 1").fetchone()[0]
        store.register_worker(worker_id="w-cap", token="tok-cap", org_id=org_id,
                              capabilities=[[org_id, "instagram", "acme"]])
        store.upsert_campaign_meta(cid, status="live")
        with store._tx() as c:
            c.execute("UPDATE campaign_meta SET archived_at=NULL WHERE campaign_id=?",
                      (cid,))
        store.upsert_campaign_brief(broke_cid, {"platform": "instagram", "goal": "lead"},
                                    org_id=org_id)
        store.upsert_campaign_meta(broke_cid, org_id=org_id, status="live")
        store.log_spend(broke_cid, "match", 20.5, model="m1")   # already past the cap
        store.set_execution_backend("distributed")
    finally:
        store.close()
    try:
        code, resp = _post(panel["base"] + "/api/run",
                           json.dumps({"all": True, "mode": "live"}).encode())
        assert code == 202, resp
        skip = next(s for s in resp["data"]["skipped"] if s["campaignId"] == broke_cid)
        # The reason carries both figures so an operator can see WHY it stopped, rather
        # than a bare string against an invisible ceiling (REVIEW FIX).
        assert skip["reason"] == "spend cap reached ($20.50 spent of $20.00)"
        store = Store(panel["db"])
        try:
            campaigns = {store.get_job(j)["campaignId"] for j in resp["data"]["jobs"]}
        finally:
            store.close()
        assert broke_cid not in campaigns
        assert cid in campaigns   # the within-budget campaign still dispatched
    finally:
        store = Store(panel["db"])
        try:
            store.set_execution_backend("in_process")
        finally:
            store.close()


def test_fleet_dispatch_409s_a_single_over_budget_campaign(panel, monkeypatch):
    monkeypatch.setenv("AIZU_SPEND_CAP", "20.0")
    _reset_runner(panel)
    broke_cid = "fix-over-budget-solo"
    store = Store(panel["db"])
    try:
        org_id = store._conn.execute(
            "SELECT id FROM organizations ORDER BY id LIMIT 1").fetchone()[0]
        store.register_worker(worker_id="w-cap2", token="tok-cap2", org_id=org_id,
                              capabilities=[[org_id, "instagram", "acme"]])
        store.upsert_campaign_brief(broke_cid, {"platform": "instagram", "goal": "lead"},
                                    org_id=org_id)
        store.upsert_campaign_meta(broke_cid, org_id=org_id, status="live")
        store.log_spend(broke_cid, "match", 25.0, model="m1")
        store.set_execution_backend("distributed")
    finally:
        store.close()
    try:
        code, resp = _post(panel["base"] + "/api/run",
                           json.dumps({"campaignId": broke_cid, "mode": "live"}).encode())
        assert code == 409, resp
        assert "spend cap reached" in resp["error"]
    finally:
        store = Store(panel["db"])
        try:
            store.set_execution_backend("in_process")
        finally:
            store.close()


# ----- input-validation / error-handling seam ---------------------------------
# Four defects that were found by driving the REAL bridge over HTTP, so every test
# here goes over the wire too: the bugs were only visible in what the socket
# actually carried (a bare Infinity token, a reset connection, a committed row).

def _hostport(panel) -> tuple[str, int]:
    parsed = urllib.parse.urlparse(panel["base"])
    return parsed.hostname, parsed.port


def _raw_request(panel, method: str, path: str,
                 body: bytes | None = None) -> tuple[int, bytes]:
    """Send a request with http.client (not urllib) and return (status, raw bytes).

    Needed for two reasons urllib cannot serve: the body may contain JSON's
    non-standard `Infinity`/`NaN` literals, which `json.dumps` will not produce; and
    the failure mode under test is the server DROPPING the connection with no HTTP
    response at all, which must surface as an exception here, not be hidden."""
    host, port = _hostport(panel)
    conn = http.client.HTTPConnection(host, port, timeout=30)
    headers = {"Content-Type": "application/json"}
    if _SESSION_COOKIE:
        headers["Cookie"] = _SESSION_COOKIE
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _reject_non_finite(token: str):
    """`json.loads(parse_constant=...)` hook: fail on the Infinity/NaN tokens that
    Python accepts by extension but RFC 8259 — and the panel's parser — do not."""
    raise AssertionError(f"body carries the invalid JSON token {token!r}")


def _strict_loads(raw: str | bytes):
    return json.loads(raw, parse_constant=_reject_non_finite)


# --- unbounded access logging (anonymous remote DoS) ---

def test_log_path_truncates_an_attacker_sized_path():
    from aizu.server import _LOG_PATH_MAX, _log_path
    long_path = "/api/x" + "A" * 64_000
    out = _log_path(long_path)
    assert len(out) < _LOG_PATH_MAX + 60
    assert out.startswith("/api/xAAAA")
    assert "64006 chars total" in out
    # A normal path is passed through byte-for-byte.
    assert _log_path("/api/state?campaign=acme") == "/api/state?campaign=acme"


def test_long_request_path_is_bounded_in_every_log_line(panel):
    """`log_request` used to hand the FULL, attacker-controlled path to the console
    handler, which renders per character while holding the GIL — a few hundred KB of
    such requests froze the whole bridge for anonymous clients. Every line the
    request produces must now carry a bounded prefix instead."""
    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    logger = logging.getLogger("aizu.server")
    handler = _Capture(level=logging.DEBUG)
    logger.addHandler(handler)
    try:
        status, _ = _raw_request(panel, "GET", "/api/x" + "A" * 64_000)
    finally:
        logger.removeHandler(handler)
    assert status == 404
    assert captured, "the long-path request produced no log line to check"
    assert max(len(m) for m in captured) < 1000, \
        "a log line still carries the unbounded request path"
    assert any("chars total" in m for m in captured)


# --- non-finite numbers accepted at 200, bricking the org's panel ---

@pytest.mark.parametrize("literal", [b"Infinity", b"-Infinity", b"NaN", b"1e400"])
def test_campaign_rejects_non_finite_budget_cap(panel, literal):
    """`_opt_number` checked type and `>= 0` but never `math.isfinite` — and every
    comparison against NaN is False, so NaN sailed through the range check."""
    status, raw = _raw_request(
        panel, "POST", "/api/campaign",
        b'{"campaignId": "nonfinite-budget", "budgetCap": ' + literal + b"}")
    assert status == 400, raw
    assert json.loads(raw)["ok"] is False
    store = Store(panel["db"])
    try:
        assert store.get_campaign_meta("nonfinite-budget") is None
    finally:
        store.close()


def test_campaign_rejects_non_finite_brief_threshold(panel):
    status, raw = _raw_request(
        panel, "POST", "/api/campaign",
        b'{"campaignId": "nonfinite-threshold", "brief": '
        b'{"platform": "instagram", "threshold": NaN}}')
    assert status == 400, raw
    assert "finite" in json.loads(raw)["error"]


def test_state_and_campaigns_stay_strict_json_after_a_non_finite_attempt(panel):
    """The whole reason this matters: a stored Infinity/NaN comes back out of
    `json.dumps` as a BARE `Infinity`/`NaN` token — invalid JSON per RFC 8259 — so
    /api/state and /api/campaigns answered 200 with a body the panel's parser
    rejects. Every page for the org went dead with no in-app way to undo it."""
    _raw_request(panel, "POST", "/api/campaign",
                 b'{"campaignId": "brick-test", "budgetCap": Infinity}')
    _raw_request(panel, "POST", "/api/campaign",
                 b'{"campaignId": "brick-test-2", "goalTarget": NaN}')
    for path in ("/api/state", "/api/campaigns", "/api/dashboard"):
        status, raw = _get(panel["base"] + path)
        assert status == 200
        _strict_loads(raw)          # raises if a bare Infinity/NaN made it into the body


def test_json_bytes_refuses_to_emit_invalid_json():
    """The serializer-side backstop, so this class cannot recur through another door
    (an older row, a worker report, a computed ratio): a non-finite is scrubbed to
    null rather than emitted as a token no strict parser accepts."""
    from aizu.server import _json_bytes
    body = _json_bytes({"a": float("inf"), "b": [float("-inf"), float("nan")],
                        "c": 1.5, "d": {"e": float("nan")}})
    assert b"Infinity" not in body and b"NaN" not in body
    assert _strict_loads(body) == {"a": None, "b": [None, None], "c": 1.5,
                                   "d": {"e": None}}


# --- out-of-range number: dead socket + leaked traceback ---

def test_out_of_range_number_answers_a_clean_400_not_a_dead_socket(panel):
    """A 400-digit `budgetCap` OverflowError-ed inside `_validate_campaign`, which
    runs BEFORE `_handle_campaign`'s try-block — the exception escaped do_POST, the
    socket was reset with no HTTP response (curl exit 52 / HTTP 000) and stderr got a
    traceback full of absolute filesystem paths."""
    status, raw = _raw_request(
        panel, "POST", "/api/campaign",
        b'{"campaignId": "overflow-test", "budgetCap": ' + b"9" * 400 + b"}")
    assert status == 400, raw
    assert json.loads(raw)["ok"] is False
    assert "out of range" in json.loads(raw)["error"]


def test_huge_goal_target_is_a_400_not_a_leaked_driver_message(panel):
    """The sibling case: a merely-huge (finite) number reached SQLite, whose own
    error text was echoed to the client as a 500 body."""
    status, raw = _raw_request(
        panel, "POST", "/api/campaign",
        b'{"campaignId": "huge-goal", "goalTarget": 1e30}')
    assert status == 400, raw
    assert "SQLite" not in raw.decode()


def test_unexpected_error_answers_a_generic_500_not_a_dead_socket(panel, monkeypatch):
    """Top-level guard: whatever blows up inside the router, the client gets a
    well-formed generic 500 — never an empty reply, never an internal message."""
    def _boom(_payload):
        raise RuntimeError("driver detail from /Users/someone/aizu/engine/secret.py")

    monkeypatch.setattr(server, "_validate_campaign", _boom)
    status, raw = _raw_request(panel, "POST", "/api/campaign",
                               b'{"campaignId": "boom"}')
    assert status == 500
    body = json.loads(raw)
    assert body["ok"] is False and body["error"] == "internal server error"
    assert "secret.py" not in raw.decode()


def test_unexpected_error_on_a_GET_also_answers_a_generic_500(panel, monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("leaky /Users/someone/aizu/engine/detail.py")

    monkeypatch.setattr(server, "build_campaigns_org", _boom)
    status, raw = _raw_request(panel, "GET", "/api/campaigns")
    assert status == 500
    assert json.loads(raw)["error"] == "internal server error"
    assert "detail.py" not in raw.decode()


# --- a rejected create must not commit a ghost row ---

def test_rejected_campaign_create_leaves_no_ghost_row(panel):
    """`store.upsert_campaign_meta` commits in its own transaction, and only
    afterwards did `campaign_from_brief` throw — so a 400 for an unsupported platform
    still created a brief-less campaign that renders as a full card and can never run
    ('no platforms'). The whole request must now validate before the first write."""
    code, resp = _post(panel["base"] + "/api/campaign", json.dumps({
        "campaignId": "ghost-campaign", "displayName": "Ghost", "status": "live",
        "brief": {"platform": "myspace"}}).encode())
    assert code == 400 and resp["ok"] is False
    store = Store(panel["db"])
    try:
        assert store.get_campaign_meta("ghost-campaign") is None
        assert store.get_campaign_brief("ghost-campaign") is None
    finally:
        store.close()
    _, raw = _get(panel["base"] + "/api/campaigns")
    assert "ghost-campaign" not in raw


def test_rejected_campaign_edit_does_not_apply_the_valid_fields(panel):
    """Same transaction boundary from the other side: an EXISTING campaign whose edit
    carries a good status/budget and a bad brief must come back untouched, not
    half-applied."""
    _, created = _post(panel["base"] + "/api/campaign", json.dumps({
        "campaignId": "half-apply", "status": "draft", "budgetCap": 10.0,
        "brief": {"platform": "instagram"}}).encode())
    stored_id = created["data"]["campaign_id"]
    code, _ = _post(panel["base"] + "/api/campaign", json.dumps({
        "campaignId": stored_id, "status": "live", "budgetCap": 999.0,
        "brief": {"platform": "myspace"}}).encode())
    assert code == 400
    store = Store(panel["db"])
    try:
        meta = store.get_campaign_meta(stored_id)
        assert meta["status"] == "draft" and meta["budget_cap"] == 10.0
    finally:
        store.close()


# ===================================================================================
# Routing / authz / campaign-semantics seam
# ===================================================================================

def _post_json_as(panel, path: str, payload: dict, cookie: str) -> tuple[int, dict]:
    """POST a dict as a SPECIFIC principal (not the module-global owner cookie)."""
    return _post_as(panel["base"] + path, json.dumps(payload).encode(), cookie)


def _get_as(panel, path: str, cookie: str) -> tuple[int, dict]:
    req = urllib.request.Request(panel["base"] + path, headers={"Cookie": cookie})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _second_org(panel, email: str) -> tuple[str, int]:
    """A fresh signup → its own org. Returns (cookie, orgId)."""
    cookie = _signup_cookie(panel["base"], email, "test-password-123",
                            company="Other Co")
    store = Store(panel["db"])
    try:
        row = store._conn.execute(
            "SELECT org_id FROM users WHERE email=?", (email,)).fetchone()
    finally:
        store.close()
    return cookie, row[0]


# --- B6 on the wire: a fleet run that never emitted anything must not 404 ---

def test_run_activity_reports_a_fleet_job_that_produced_no_events(panel):
    """The exact case the B6 reason surfacing was built for: a dispatched run whose
    worker died before it could open a session (e.g. CDP unattachable) has ZERO
    run_events and ZERO sessions, so the 'unknown run' guard fired BEFORE the fleetJob
    block and the operator saw a bare 404 for a run that really is theirs."""
    cid = _campaign_id()
    _enqueue_fleet_job(panel["db"], job_id="jf-silent", run_id="run-silent",
                       campaign_id=cid, status="failed")
    store = Store(panel["db"])
    try:
        store._conn.execute("UPDATE jobs SET result=? WHERE id=?",
                            (json.dumps({"reason": "cdp_unavailable"}), "jf-silent"))
        store._conn.commit()
        assert store.fetch_run_events("run-silent", after_id=0) == []
        assert store.sessions_for_run("run-silent") == []
    finally:
        store.close()
    code, resp = _activity(panel, "?runId=run-silent")
    assert code == 200, resp
    fj = resp["data"]["fleetJob"]
    assert fj is not None and fj["jobId"] == "jf-silent"
    assert fj["status"] == "failed"
    assert fj["reason"] == "cdp_unavailable"
    assert resp["data"]["finished"] is True
    assert resp["data"]["events"] == []


def test_run_activity_does_not_disclose_another_orgs_fleet_run(panel):
    """The 404 must stay a non-disclosure gate for runs that are NOT the caller's:
    a job row owned by another org is still 'unknown run'."""
    cookie, other_org = _second_org(panel, "fleet-oracle@aizu.test")
    store = Store(panel["db"])
    try:
        store.upsert_campaign_meta("other-org-camp", org_id=other_org, status="live")
        store.enqueue_job(job_id="jf-foreign", campaign_id="other-org-camp",
                          platform="instagram", org_id=other_org,
                          required_account_handle=None,
                          spec={"run_id": "run-foreign", "engine_mode": "harvest"})
    finally:
        store.close()
    code, resp = _activity(panel, "?runId=run-foreign")
    assert code == 404 and resp["error"] == "unknown run"


# --- create vs edit: a colliding create must not destroy the existing campaign ---

def test_explicit_create_refuses_to_clobber_an_existing_campaign(panel):
    """CREATE and EDIT were the same payload on the same endpoint, so a second
    campaign whose name slugs to an existing id silently overwrote the first's brief —
    and `matches` being keyed on (campaign_id, platform, comment_id) re-pointed the
    first campaign's whole lead history at it. Data loss with no warning."""
    first = {"campaignId": "same-name", "displayName": "Same Name", "status": "draft",
             "op": "create",
             "brief": {"platform": "youtube", "relevanceDef": "the ORIGINAL brief",
                       "seedChannels": ["UC_original"]}}
    code, created = _post(panel["base"] + "/api/campaign", json.dumps(first).encode())
    assert code == 200
    stored_id = created["data"]["campaign_id"]
    second = dict(first)
    second["brief"] = {"platform": "youtube", "relevanceDef": "a DIFFERENT brief",
                       "seedChannels": ["UC_replacement"]}
    code, resp = _post(panel["base"] + "/api/campaign", json.dumps(second).encode())
    assert code == 409, resp
    assert resp["ok"] is False and resp.get("code") == "campaign_exists"
    store = Store(panel["db"])
    try:
        brief = store.get_campaign_brief(stored_id)
        assert brief["relevance_def"] == "the ORIGINAL brief"
        assert brief["seed_channels"] == ["UC_original"]
    finally:
        store.close()


def test_explicit_edit_of_an_existing_campaign_still_works(panel):
    _, created = _post(panel["base"] + "/api/campaign", json.dumps({
        "campaignId": "edit-me", "displayName": "Edit Me", "status": "draft",
        "brief": {"platform": "youtube", "seedChannels": ["UC_a"]}}).encode())
    stored_id = created["data"]["campaign_id"]
    code, resp = _post(panel["base"] + "/api/campaign", json.dumps({
        "campaignId": stored_id, "displayName": "Edited", "status": "live",
        "op": "edit", "brief": {"platform": "youtube", "seedChannels": ["UC_b"]},
    }).encode())
    assert code == 200 and resp["data"]["status"] == "live"
    store = Store(panel["db"])
    try:
        assert store.get_campaign_brief(stored_id)["seed_channels"] == ["UC_b"]
    finally:
        store.close()


def test_explicit_edit_of_another_orgs_campaign_is_a_404(panel):
    """`op="edit"` may only ever reach the caller's OWN row — a cross-org id stays
    undisclosed. (An id nobody has registered is a different case; see
    test_explicit_edit_of_the_file_backed_campaign_matches_the_legacy_path.)"""
    cookie, _ = _second_org(panel, "edit-404@aizu.test")
    _, theirs = _post_json_as(panel, "/api/campaign", {
        "campaignId": "theirs-only", "op": "create", "status": "draft",
        "brief": {"platform": "youtube", "seedChannels": ["UC_a"]}}, cookie)
    code, resp = _post(panel["base"] + "/api/campaign", json.dumps({
        "campaignId": theirs["data"]["campaign_id"], "op": "edit",
        "status": "live"}).encode())
    assert code == 404 and resp["error"] == "unknown campaign"


def test_explicit_edit_of_the_file_backed_campaign_matches_the_legacy_path(panel):
    """`op="edit"` used to demand a campaign_meta/campaign_briefs row, which the
    file-backed campaign from config/campaign.md does not have until its first write —
    so the panel's own primary card (rendered from resolve_campaign) 404'd the moment
    the form started naming its intent, while the same edit with no `op` succeeded.
    The two paths must agree on what is editable."""
    unregistered = "unregistered-file-campaign"
    code, resp = _post(panel["base"] + "/api/campaign", json.dumps({
        "campaignId": unregistered, "op": "edit", "status": "paused"}).encode())
    assert code == 200, resp
    assert resp["data"]["campaign_id"] == unregistered   # no new key allocated
    assert resp["data"]["status"] == "paused"
    store = Store(panel["db"])
    try:                                   # …and it is stamped to the caller's org
        assert store.get_campaign_meta(unregistered)["status"] == "paused"
        assert store.org_for_campaign(unregistered) == _owner_org_id(panel)
    finally:
        store.close()


def test_campaign_op_must_be_create_or_edit(panel):
    code, resp = _post(panel["base"] + "/api/campaign", json.dumps({
        "campaignId": "bad-op", "op": "upsert"}).encode())
    assert code == 400 and "op" in resp["error"]


# --- campaign ids are per-org, not a global namespace ---

def test_two_orgs_can_each_create_a_campaign_with_the_same_name(panel):
    """Org B creating an ordinarily-named campaign that org A already has used to get
    `404 unknown campaign` on a CREATE — nonsensical, with no remedy — and doubled as
    a cross-tenant existence oracle."""
    cookie, _ = _second_org(panel, "q4-collision@aizu.test")
    payload = {"campaignId": "q4-outbound", "displayName": "Q4 Outbound",
               "status": "draft", "op": "create",
               "brief": {"platform": "youtube", "seedChannels": ["UC_a"]}}
    code, mine = _post(panel["base"] + "/api/campaign", json.dumps(payload).encode())
    assert code == 200, mine
    code, theirs = _post_json_as(panel, "/api/campaign", payload, cookie)
    assert code == 200, theirs
    assert theirs["data"]["campaign_id"] != mine["data"]["campaign_id"]
    # Org A's campaign is untouched and org B cannot see org A's.
    _, a_state = _get_as(panel, "/api/campaigns", panel["cookie"])
    _, b_state = _get_as(panel, "/api/campaigns", cookie)
    a_ids = {c["id"] for c in a_state["CAMPAIGNS"]}
    b_ids = {c["id"] for c in b_state["CAMPAIGNS"]}
    assert mine["data"]["campaign_id"] in a_ids
    assert theirs["data"]["campaign_id"] in b_ids
    assert not (a_ids & b_ids)


def test_create_allocates_the_same_id_shape_whether_or_not_another_org_holds_it(panel):
    """No existence oracle: an explicit create must not let the caller tell a
    globally-free id from one another tenant already owns."""
    from aizu.server import _org_scoped_campaign_id
    cookie, org_b = _second_org(panel, "oracle-probe@aizu.test")
    taken = {"campaignId": "oracle-taken", "displayName": "Oracle Taken",
             "status": "draft", "op": "create"}
    code, _ = _post(panel["base"] + "/api/campaign", json.dumps(taken).encode())
    assert code == 200                      # org A takes the bare id first
    code, probe_taken = _post_json_as(panel, "/api/campaign", taken, cookie)
    free = {"campaignId": "oracle-free", "displayName": "Oracle Free",
            "status": "draft", "op": "create"}
    code2, probe_free = _post_json_as(panel, "/api/campaign", free, cookie)
    assert code == code2 == 200             # identical status either way
    # …and an identical key SHAPE: the prefix is derived from the caller's own org,
    # so nothing in the response says whether another tenant held the base id.
    assert probe_taken["data"]["campaign_id"] == _org_scoped_campaign_id(
        org_b, "oracle-taken")
    assert probe_free["data"]["campaign_id"] == _org_scoped_campaign_id(
        org_b, "oracle-free")


def test_legacy_create_without_op_no_longer_404s_on_another_orgs_id(panel):
    """The current panel sends no `op`. A brief-carrying create for an id another
    tenant owns used to answer `404 unknown campaign` with no remedy; it now lands in
    the caller's own namespace instead."""
    cookie, _ = _second_org(panel, "legacy-collide@aizu.test")
    _post(panel["base"] + "/api/campaign", json.dumps({
        "campaignId": "legacy-shared", "displayName": "Legacy Shared",
        "status": "draft"}).encode())
    code, resp = _post_json_as(panel, "/api/campaign", {
        "campaignId": "legacy-shared", "displayName": "Legacy Shared",
        "status": "draft",
        "brief": {"platform": "youtube", "seedChannels": ["UC_b"]}}, cookie)
    assert code == 200, resp
    assert resp["data"]["campaign_id"] != "legacy-shared"
    store = Store(panel["db"])
    try:  # org A's row is untouched
        assert store.get_campaign_brief("legacy-shared") is None
    finally:
        store.close()


def test_legacy_create_allocates_the_same_id_shape_for_a_free_and_a_taken_slug(panel):
    """The oracle from the other side, over the payload the panel ACTUALLY sends (no
    `op`). Allocating the bare id when it happened to be free and a scoped one when
    another tenant held it made the returned id a per-request answer to "does anyone
    else own this name?" — enumerable one slug at a time. A create must allocate the
    same shape either way."""
    from aizu.server import _org_scoped_campaign_id
    cookie, org_b = _second_org(panel, "legacy-oracle@aizu.test")
    _post(panel["base"] + "/api/campaign", json.dumps({
        "campaignId": "legacy-taken", "displayName": "Legacy Taken", "status": "draft",
        "brief": {"platform": "youtube", "seedChannels": ["UC_a"]}}).encode())
    body = {"displayName": "Probe", "status": "draft",
            "brief": {"platform": "youtube", "seedChannels": ["UC_b"]}}
    code, taken = _post_json_as(
        panel, "/api/campaign", {**body, "campaignId": "legacy-taken"}, cookie)
    code2, free = _post_json_as(
        panel, "/api/campaign", {**body, "campaignId": "legacy-free"}, cookie)
    assert code == code2 == 200
    assert taken["data"]["campaign_id"] == _org_scoped_campaign_id(org_b, "legacy-taken")
    assert free["data"]["campaign_id"] == _org_scoped_campaign_id(org_b, "legacy-free")


def test_another_org_cannot_squat_the_callers_campaign_key_namespace(panel):
    """The scoped key still lives in one global campaign_meta PK, so the namespace has
    to be reserved: a tenant that could pre-register `o<victimOrg>.<slug>` would lock
    the victim out of that campaign name for good — 409 on create, 404 on edit, no
    operator remedy."""
    from aizu.server import _org_scoped_campaign_id
    victim_org = _owner_org_id(panel)
    cookie, _ = _second_org(panel, "squatter@aizu.test")
    squat_key = _org_scoped_campaign_id(victim_org, "brand-new")
    # Every shape of squat: with a brief, and the field-only write that used to
    # register any free id verbatim.
    code, resp = _post_json_as(panel, "/api/campaign", {
        "campaignId": squat_key, "status": "live",
        "brief": {"platform": "youtube", "seedChannels": ["UC_z"]}}, cookie)
    assert code == 200 and resp["data"]["campaign_id"] != squat_key
    code, resp = _post_json_as(panel, "/api/campaign", {
        "campaignId": squat_key, "status": "live"}, cookie)
    assert code == 404 and resp["error"] == "unknown campaign"
    code, resp = _post_json_as(panel, "/api/campaign", {
        "campaignId": squat_key, "op": "edit", "status": "live"}, cookie)
    assert code == 404 and resp["error"] == "unknown campaign"
    # The victim can still create — and then edit — that campaign name.
    code, mine = _post(panel["base"] + "/api/campaign", json.dumps({
        "campaignId": "brand-new", "op": "create", "displayName": "Brand New",
        "status": "draft", "brief": {"platform": "youtube",
                                     "seedChannels": ["UC_a"]}}).encode())
    assert code == 200, mine
    assert mine["data"]["campaign_id"] == squat_key
    code, edited = _post(panel["base"] + "/api/campaign", json.dumps({
        "campaignId": squat_key, "op": "edit", "status": "paused"}).encode())
    assert code == 200 and edited["data"]["status"] == "paused"


def test_legacy_create_of_a_slug_the_caller_already_used_is_refused(panel):
    """The data-loss case on the wire the shipped panel uses (no `op`): a second
    campaign whose name slugs onto one the operator already has must be refused, not
    silently written over the first one's brief — `matches` is keyed on campaign_id,
    so the first campaign's whole lead history would follow."""
    first = {"campaignId": "legacy-same-name", "displayName": "Same Name",
             "status": "draft",
             "brief": {"platform": "youtube", "relevanceDef": "FIRST CAMPAIGN",
                       "seedChannels": ["UC_first"]}}
    code, created = _post(panel["base"] + "/api/campaign", json.dumps(first).encode())
    assert code == 200
    stored_id = created["data"]["campaign_id"]
    second = {**first, "brief": {"platform": "youtube",
                                 "relevanceDef": "SECOND CAMPAIGN",
                                 "seedChannels": ["UC_second"]}}
    code, resp = _post(panel["base"] + "/api/campaign", json.dumps(second).encode())
    assert code == 409, resp
    assert resp["ok"] is False and resp.get("code") == "campaign_exists"
    store = Store(panel["db"])
    try:
        brief = store.get_campaign_brief(stored_id)
        assert brief["relevance_def"] == "FIRST CAMPAIGN"
        assert brief["seed_channels"] == ["UC_first"]
    finally:
        store.close()


# --- /api/state must not leak run history + spend to a member ---

def _member_cookie(panel, email: str) -> str:
    """A signed-up principal demoted to `member` in its own org."""
    cookie = _signup_cookie(panel["base"], email, "test-password-123", company="Mem Co")
    store = Store(panel["db"])
    try:
        store._conn.execute("UPDATE users SET role='member' WHERE email=?", (email,))
        store._conn.commit()
    finally:
        store.close()
    return cookie


def test_state_does_not_expose_the_run_block_to_a_member(panel):
    """panel.py deliberately prunes a member's state to CONFIG + campaign stubs +
    MATCHES; the handler then bolted RUN (run history + spend) back on with an org
    check but no ROLE check — while /api/dashboard, /api/campaigns and
    /api/run/activity all correctly 403 that same member."""
    cookie = _member_cookie(panel, "member-state@aizu.test")
    code, state = _get_as(panel, "/api/state", cookie)
    assert code == 200
    assert state["CONFIG"]["role"] == "member"
    assert "RUN" not in state
    # The sibling endpoints agree.
    assert _get_as(panel, "/api/dashboard", cookie)[0] == 403
    assert _get_as(panel, "/api/campaigns", cookie)[0] == 403


def test_state_still_exposes_the_run_block_to_an_owner(panel):
    _, state = _get_as(panel, "/api/state", panel["cookie"])
    assert "RUN" in state


# --- HEAD must route exactly like GET ---

def _head(panel, path: str) -> tuple[int, dict]:
    host, port = _hostport(panel)
    conn = http.client.HTTPConnection(host, port, timeout=30)
    try:
        conn.request("HEAD", path, headers={"Cookie": _SESSION_COOKIE or ""})
        resp = conn.getresponse()
        body = resp.read()
        assert body == b"", f"HEAD {path} must have no body"
        return resp.status, dict(resp.getheaders())
    finally:
        conn.close()


@pytest.mark.parametrize("path", ["/app/campaigns", "/app", "/app/", "/",
                                  "/assets/index-abc123.js"])
def test_head_matches_get_status(panel, path):
    """No do_HEAD meant HEAD fell through to SimpleHTTPRequestHandler's raw
    filesystem mapping, bypassing every route: /app/campaigns GET 200 / HEAD 404,
    /app GET 200 / HEAD 301."""
    get_status, _ = _get(panel["base"] + path)
    head_status, _ = _head(panel, path)
    assert head_status == get_status, path


def test_head_on_an_api_path_answers_json_like_get(panel):
    head_status, headers = _head(panel, "/api/state")
    assert head_status == 200
    assert headers["Content-Type"].startswith("application/json")


def test_head_on_an_unauthenticated_api_path_is_a_json_401(panel):
    host, port = _hostport(panel)
    conn = http.client.HTTPConnection(host, port, timeout=30)
    try:
        conn.request("HEAD", "/api/state")   # no cookie
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 401
        assert resp.getheader("Content-Type").startswith("application/json")
    finally:
        conn.close()


# --- a nested unknown URL must 404, not render the landing at 200 ---

def test_nested_unknown_path_is_a_404_not_the_landing(panel):
    """/pricing/enterprise served index.html at 200, so the landing rendered
    unstyled (its relative asset URLs resolved under /pricing/ and answered HTML
    with a 200 and no nosniff)."""
    host, port = _hostport(panel)
    conn = http.client.HTTPConnection(host, port, timeout=30)
    try:
        conn.request("GET", "/pricing/enterprise")
        resp = conn.getresponse()
        body = resp.read()
        assert resp.status == 404
        assert b'id="landing"' not in body
        assert resp.getheader("X-Content-Type-Options") == "nosniff"
    finally:
        conn.close()


def test_missing_asset_under_a_nested_path_is_a_404(panel):
    host, port = _hostport(panel)
    conn = http.client.HTTPConnection(host, port, timeout=30)
    try:
        conn.request("GET", "/pricing/landing/css/core-hr.css")
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 404
    finally:
        conn.close()


# --- server identity + transport headers ---

def test_server_header_does_not_advertise_the_stdlib_and_python_version(panel):
    host, port = _hostport(panel)
    conn = http.client.HTTPConnection(host, port, timeout=30)
    try:
        conn.request("GET", "/api/state", headers={"Cookie": _SESSION_COOKIE or ""})
        resp = conn.getresponse()
        resp.read()
        server_header = resp.getheader("Server") or ""
        assert "SimpleHTTP" not in server_header
        assert "Python" not in server_header
        assert server_header.startswith("aizu")
        assert resp.getheader("X-Content-Type-Options") == "nosniff"
    finally:
        conn.close()


# --- session cookie Secure flag ---

def test_session_cookie_is_not_secure_over_plain_local_http(panel):
    cookie_header = _raw_signup_set_cookie(panel, "secure-off@aizu.test", headers={})
    assert "HttpOnly" in cookie_header and "SameSite=Lax" in cookie_header
    assert "Secure" not in cookie_header


def test_session_cookie_is_secure_behind_an_https_terminating_proxy(panel, monkeypatch):
    """The 30-day session cookie carried no Secure flag, justified by a comment
    claiming the bridge is loopback-only — which contradicts the documented hosted
    deployment behind a reverse proxy."""
    monkeypatch.setenv("AIZU_TRUSTED_PROXIES", "127.0.0.1")
    cookie_header = _raw_signup_set_cookie(
        panel, "secure-on@aizu.test", headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in cookie_header


def test_session_cookie_ignores_forwarded_proto_from_an_untrusted_peer(panel):
    """X-Forwarded-Proto is client-spoofable: honour it only from a trusted proxy."""
    cookie_header = _raw_signup_set_cookie(
        panel, "secure-spoof@aizu.test", headers={"X-Forwarded-Proto": "https"})
    assert "Secure" not in cookie_header


def _raw_signup_set_cookie(panel, email: str, headers: dict) -> str:
    host, port = _hostport(panel)
    conn = http.client.HTTPConnection(host, port, timeout=30)
    try:
        conn.request("POST", "/api/auth/signup",
                     body=json.dumps({"email": email, "password": "test-password-123",
                                      "companyName": "Cookie Co"}).encode(),
                     headers={"Content-Type": "application/json", **headers})
        resp = conn.getresponse()
        resp.read()
        return resp.getheader("Set-Cookie") or ""
    finally:
        conn.close()


# --- starting on a busy port ---

def test_serve_on_a_busy_port_raises_a_clean_error_and_leaves_no_db(tmp_path):
    """A busy port dumped a raw OSError traceback (Errno 48) out of the CLI and left
    a freshly-migrated DB behind — inconsistent with the clean `error: …`/rc=2 style
    the same command uses three lines above."""
    from aizu.server import PortInUseError
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    port = busy.getsockname()[1]
    panel_dir = tmp_path / "dist"
    (panel_dir / "app").mkdir(parents=True)
    (panel_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    (panel_dir / "app" / "index.html").write_text("<html></html>", encoding="utf-8")
    db = tmp_path / "never-created.db"
    try:
        with pytest.raises(PortInUseError) as excinfo:
            serve(str(db), str(panel_dir), str(CONFIG), port=port)
        assert str(port) in str(excinfo.value)
        assert "already in use" in str(excinfo.value)
        assert not db.exists(), "a failed bind must not leave a migrated DB behind"
    finally:
        busy.close()


def test_cli_panel_on_a_busy_port_prints_one_error_line(tmp_path, capsys):
    """The other half of the same defect: serve() raised a clean typed error but
    nothing caught it, so `aizu panel` on an occupied port — the most common failure
    of the documented start command — still crashed with a traceback full of absolute
    filesystem paths instead of the one-line `error: …`/rc=2 the same command already
    prints for a missing panel build."""
    import argparse
    from aizu.cli import cmd_panel
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    port = busy.getsockname()[1]
    panel_dir = tmp_path / "dist"
    (panel_dir / "app").mkdir(parents=True)
    (panel_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    (panel_dir / "app" / "index.html").write_text("<html></html>", encoding="utf-8")
    db = tmp_path / "never-created.db"
    try:
        rc = cmd_panel(argparse.Namespace(
            db=str(db), panel_dir=str(panel_dir), config=str(CONFIG),
            host="127.0.0.1", port=port))
    finally:
        busy.close()
    assert rc == 2
    err = capsys.readouterr().err
    assert err.strip().startswith("error: ")
    assert "already in use" in err and "--port" in err
    assert "Traceback" not in err and __file__ not in err
    assert not db.exists()
