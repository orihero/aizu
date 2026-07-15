"""Security-hardening coverage (v9): the immutable audit_log + the invite-creation
rate limiter.

Two layers:
- Store layer: audit_log exists on a fresh DB AND after re-opening an existing DB
  (additive CREATE TABLE IF NOT EXISTS, no migration); record_audit / audit_entries
  round-trip; reads are org-scoped (org A cannot read org B).
- HTTP layer: each wired mutation (role change, member add/remove, invite create,
  invite accept, integration disconnect) writes exactly one correctly-shaped row;
  the InviteThrottle trips at the limit and leaves an unrelated actor unaffected.
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

from reelradar.secrets import SecretCipher
from reelradar.server import (InviteThrottle, MAX_INVITE_CREATES, PanelHandler,
                              serve)
from reelradar.core.store import Store


class _StubTelegramLogin:
    """Stands in for the real Telegram login so verify() reports a connected session
    without touching Telegram — lets the connect-audit path run end-to-end."""

    def verify(self, token, code, password):  # noqa: D401 - matches the real signature
        return {"connected": True, "session": {"auth": "stub-session"}}

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
_INDEX_HTML = '<!doctype html><html><body><div id="root"></div></body></html>'
PW = "longenough1"


# --------------------------------------------------------------------------- #
# Store layer
# --------------------------------------------------------------------------- #
def _fresh_store():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(db_path), db_path


def _audit_table_present(store: Store) -> bool:
    return store._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_log'"
    ).fetchone() is not None


def test_audit_log_table_exists_after_fresh_init():
    # Arrange / Act
    store, db_path = _fresh_store()
    try:
        # Assert
        assert _audit_table_present(store) is True
    finally:
        store.close()
        os.unlink(db_path)


def test_audit_log_table_exists_after_reopening_existing_db():
    # Arrange — an existing DB created once, then re-opened (the upgrade path).
    store, db_path = _fresh_store()
    store.close()
    try:
        # Act
        reopened = Store(db_path)
        try:
            # Assert
            assert _audit_table_present(reopened) is True
            # Re-opening must be idempotent: write + read still work.
            reopened.record_audit(1, 7, "role_changed", target="9",
                                  detail=json.dumps({"from": "member", "to": "admin"}))
            assert len(reopened.audit_entries(1)) == 1
        finally:
            reopened.close()
    finally:
        os.unlink(db_path)


def test_record_audit_persists_one_row_with_correct_fields():
    # Arrange
    store, db_path = _fresh_store()
    try:
        # Act
        store.record_audit(42, 5, "member_added", target="11",
                           detail=json.dumps({"role": "viewer"}))
        # Assert
        rows = store.audit_entries(42)
        assert len(rows) == 1
        row = rows[0]
        assert row["orgId"] == 42
        assert row["actorUserId"] == 5
        assert row["action"] == "member_added"
        assert row["target"] == "11"
        assert json.loads(row["detail"]) == {"role": "viewer"}
        assert isinstance(row["createdAt"], str) and row["createdAt"]
    finally:
        store.close()
        os.unlink(db_path)


def test_audit_entries_is_org_scoped():
    # Arrange — two orgs each get one entry.
    store, db_path = _fresh_store()
    try:
        store.record_audit(1, 100, "invite_created", detail=json.dumps({"role": "member"}))
        store.record_audit(2, 200, "invite_created", detail=json.dumps({"role": "admin"}))
        # Act
        org1 = store.audit_entries(1)
        org2 = store.audit_entries(2)
        # Assert — org 1 cannot read org 2's row and vice-versa.
        assert [r["actorUserId"] for r in org1] == [100]
        assert [r["actorUserId"] for r in org2] == [200]
    finally:
        store.close()
        os.unlink(db_path)


def test_audit_entries_newest_first():
    # Arrange
    store, db_path = _fresh_store()
    try:
        store.record_audit(1, 1, "member_added")
        store.record_audit(1, 1, "member_removed")
        # Act
        rows = store.audit_entries(1)
        # Assert — id DESC => most recent first.
        assert [r["action"] for r in rows] == ["member_removed", "member_added"]
    finally:
        store.close()
        os.unlink(db_path)


# --------------------------------------------------------------------------- #
# InviteThrottle (unit)
# --------------------------------------------------------------------------- #
def test_invite_throttle_trips_at_limit_and_isolates_actors():
    # Arrange — a tiny window so the test is deterministic.
    throttle = InviteThrottle(max_creates=3, window=1000.0)
    actor, other = "7", "8"
    # Act — record up to the limit for one actor.
    for _ in range(3):
        assert throttle.is_throttled(actor) is False
        throttle.record_create(actor)
    # Assert — the (N+1)th is rejected, but an unrelated actor is unaffected.
    assert throttle.is_throttled(actor) is True
    assert throttle.is_throttled(other) is False


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #
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


def _signup(base, email, company="Co", invite=None):
    body = {"email": email, "password": PW}
    if invite:
        body["inviteToken"] = invite
    else:
        body["companyName"] = company
    code, resp, set_cookie = _post(base, "/api/auth/signup", body)
    return code, resp, (_cookie(set_cookie) if set_cookie else None)


def _audit_actions(db, org_id):
    store = Store(db)
    try:
        return [r["action"] for r in store.audit_entries(org_id)]
    finally:
        store.close()


def _audit_rows(db, org_id):
    store = Store(db)
    try:
        return store.audit_entries(org_id)
    finally:
        store.close()


@pytest.fixture
def srv():
    """A fresh server per test so audit-row counts start from a known state and the
    invite throttle (a class attribute) does not leak between tests."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    panel_dir = tempfile.mkdtemp(suffix="-spa")
    (Path(panel_dir) / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
    httpd = serve(db_path, panel_dir, str(CONFIG), port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    _, resp, owner = _signup(base, "owner@x.io", company="Acme")
    org_id = resp["data"]["user"]["orgId"]
    yield {"base": base, "db": db_path, "owner": owner, "org_id": org_id}
    httpd.shutdown()
    os.unlink(db_path)
    shutil.rmtree(panel_dir, ignore_errors=True)


def test_member_add_writes_one_audit_row(srv):
    # Arrange / Act
    code, resp, _ = _post(srv["base"], "/api/team",
                          {"op": "create", "email": "m@x.io", "password": PW,
                           "role": "member"}, srv["owner"])
    assert code == 200, resp
    new_id = resp["data"]["id"]
    # Assert — exactly one member_added row, correctly shaped.
    rows = [r for r in _audit_rows(srv["db"], srv["org_id"]) if r["action"] == "member_added"]
    assert len(rows) == 1
    assert rows[0]["target"] == str(new_id)
    assert json.loads(rows[0]["detail"]) == {"role": "member"}


def test_role_change_writes_one_audit_row(srv):
    # Arrange — add a member to promote.
    _, resp, _ = _post(srv["base"], "/api/team",
                       {"op": "create", "email": "m@x.io", "password": PW,
                        "role": "member"}, srv["owner"])
    user_id = int(resp["data"]["id"])
    # Act
    code, _, _ = _post(srv["base"], "/api/team",
                       {"op": "updateRole", "userId": user_id, "role": "admin"}, srv["owner"])
    assert code == 200
    # Assert
    rows = [r for r in _audit_rows(srv["db"], srv["org_id"]) if r["action"] == "role_changed"]
    assert len(rows) == 1
    assert rows[0]["target"] == str(user_id)
    assert json.loads(rows[0]["detail"]) == {"from": "member", "to": "admin"}


def test_member_remove_writes_one_audit_row(srv):
    # Arrange
    _, resp, _ = _post(srv["base"], "/api/team",
                       {"op": "create", "email": "m@x.io", "password": PW,
                        "role": "member"}, srv["owner"])
    user_id = int(resp["data"]["id"])
    # Act
    code, _, _ = _post(srv["base"], "/api/team",
                       {"op": "remove", "userId": user_id}, srv["owner"])
    assert code == 200
    # Assert
    rows = [r for r in _audit_rows(srv["db"], srv["org_id"]) if r["action"] == "member_removed"]
    assert len(rows) == 1
    assert rows[0]["target"] == str(user_id)


def test_invite_create_writes_one_audit_row(srv):
    # Arrange / Act
    code, _, _ = _post(srv["base"], "/api/invite",
                       {"op": "create", "role": "member"}, srv["owner"])
    assert code == 200
    # Assert
    rows = [r for r in _audit_rows(srv["db"], srv["org_id"]) if r["action"] == "invite_created"]
    assert len(rows) == 1
    assert json.loads(rows[0]["detail"]) == {"role": "member"}


def test_invite_accept_writes_one_audit_row(srv):
    # Arrange — owner mints an invite link.
    _, resp, _ = _post(srv["base"], "/api/invite",
                       {"op": "create", "role": "member"}, srv["owner"])
    token = resp["data"]["token"]
    # Act — a new user accepts it via invite-based signup.
    code, _, _ = _signup(srv["base"], "joiner@x.io", invite=token)
    assert code == 200
    # Assert — exactly one invite_accepted row in the org.
    rows = [r for r in _audit_rows(srv["db"], srv["org_id"]) if r["action"] == "invite_accepted"]
    assert len(rows) == 1
    assert json.loads(rows[0]["detail"]) == {"role": "member"}


def test_integration_disconnect_writes_one_audit_row(srv):
    # Arrange / Act — an explicit disconnect (no secret key / live call needed).
    code, _, _ = _post(srv["base"], "/api/integration",
                       {"platform": "telegram", "connected": False}, srv["owner"])
    assert code == 200
    # Assert
    rows = [r for r in _audit_rows(srv["db"], srv["org_id"])
            if r["action"] == "integration_disconnected"]
    assert len(rows) == 1
    assert rows[0]["target"] == "telegram"


def test_youtube_connect_writes_one_audit_row(srv, monkeypatch):
    # Arrange — a configured cipher (connect stores an encrypted secret) and a stubbed
    # live key-check so no network call is made. Closes the connect-audit coverage gap.
    monkeypatch.setenv("REELRADAR_SECRET_KEY", SecretCipher.generate_key())
    monkeypatch.setattr("reelradar.connections.validate_youtube_api_key", lambda key: None)
    # Act
    code, resp, _ = _post(srv["base"], "/api/integration",
                          {"platform": "youtube", "apiKey": "AIza-test-key"}, srv["owner"])
    assert code == 200, resp
    # Assert
    rows = [r for r in _audit_rows(srv["db"], srv["org_id"])
            if r["action"] == "integration_connected"]
    assert len(rows) == 1
    assert rows[0]["target"] == "youtube"


def test_telegram_connect_writes_one_audit_row(srv, monkeypatch):
    # Arrange — a stub Telegram login + a configured cipher so the verified session can
    # be persisted. Regression for the audit gap on the Telegram connect path.
    monkeypatch.setenv("REELRADAR_SECRET_KEY", SecretCipher.generate_key())
    prev = PanelHandler.telegram_login
    PanelHandler.telegram_login = _StubTelegramLogin()
    try:
        # Act
        code, resp, _ = _post(srv["base"], "/api/integration/telegram/verify",
                              {"token": "tok", "code": "12345"}, srv["owner"])
        assert code == 200, resp
        assert resp["data"]["needsPassword"] is False
        # Assert — exactly one integration_connected row, for telegram.
        rows = [r for r in _audit_rows(srv["db"], srv["org_id"])
                if r["action"] == "integration_connected"]
        assert len(rows) == 1
        assert rows[0]["target"] == "telegram"
    finally:
        PanelHandler.telegram_login = prev


def test_invite_creation_is_rate_limited_at_the_limit(srv):
    # Arrange — shrink the live throttle so the limit is hit quickly. The handler
    # reads PanelHandler.invite_throttle; swap it for a tiny one for this test.
    PanelHandler.invite_throttle = InviteThrottle(max_creates=3, window=1000.0)
    try:
        # Act — the first 3 creates succeed; the 4th rapid create is rejected.
        for _ in range(3):
            code, _, _ = _post(srv["base"], "/api/invite",
                               {"op": "create", "role": "member"}, srv["owner"])
            assert code == 200
        code, resp, _ = _post(srv["base"], "/api/invite",
                              {"op": "create", "role": "member"}, srv["owner"])
        # Assert — 429 too-many, and no 4th invite_created audit row was written.
        assert code == 429, resp
        actions = _audit_actions(srv["db"], srv["org_id"])
        assert actions.count("invite_created") == 3
    finally:
        # Restore a default throttle so other tests are unaffected.
        PanelHandler.invite_throttle = InviteThrottle()
