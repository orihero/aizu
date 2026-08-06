"""Store-level superadmin plane (v15): platform_admins, admin sessions +
impersonation, DB-backed login throttle, and the hash-chained admin_audit_log."""
import os
import tempfile

import pytest

from aizu.core.store import (ADMIN_LOGIN_MAX_FAILURES, SCHEMA_VERSION, Store)


def _tmp() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def _store() -> Store:
    return Store(_tmp())


# ----- schema -----

def test_v15_tables_exist_and_version_stamped():
    store = _store()
    try:
        names = {r["name"] for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"platform_admins", "platform_admin_sessions", "admin_audit_log",
                "admin_login_throttle", "platform_settings",
                "model_comparison_log"} <= names
        ver = store._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        assert ver == str(SCHEMA_VERSION)
    finally:
        store.close()


# ----- admins -----

def test_create_and_fetch_platform_admin():
    store = _store()
    try:
        aid = store.create_platform_admin(email="Admin@X.io", password_hash="ph",
                                          mfa_secret="blob")
        assert store.count_platform_admins() == 1
        by_email = store.get_platform_admin_by_email("admin@x.io")
        assert by_email["id"] == aid
        assert by_email["email"] == "admin@x.io"          # lowercased
        assert store.get_platform_admin_by_email("ADMIN@X.IO")["id"] == aid  # case-insensitive
        assert store.get_platform_admin_by_id(aid)["email"] == "admin@x.io"
    finally:
        store.close()


def test_create_platform_admin_requires_fields():
    store = _store()
    try:
        with pytest.raises(ValueError):
            store.create_platform_admin(email="", password_hash="ph", mfa_secret="b")
        with pytest.raises(ValueError):
            store.create_platform_admin(email="a@x.io", password_hash="", mfa_secret="b")
    finally:
        store.close()


# ----- sessions + impersonation -----

def test_admin_session_roundtrip_and_expiry():
    store = _store()
    try:
        aid = store.create_platform_admin(email="a@x.io", password_hash="p", mfa_secret="m")
        store.create_admin_session("tok", aid, expires_at=9e12)
        sess = store.get_admin_session("tok")
        assert sess["adminId"] == aid and sess["email"] == "a@x.io"
        assert sess["effectiveOrgId"] is None and sess["effectiveUserId"] is None
        # expired session resolves to None
        store.create_admin_session("old", aid, expires_at=1.0)
        assert store.get_admin_session("old") is None
        assert store.get_admin_session("nope") is None
    finally:
        store.close()


def test_disabled_admin_sessions_are_dead():
    store = _store()
    try:
        aid = store.create_platform_admin(email="a@x.io", password_hash="p", mfa_secret="m")
        store.create_admin_session("tok", aid, expires_at=9e12)
        store._conn.execute("UPDATE platform_admins SET disabled_at=? WHERE id=?",
                            (1.0, aid))
        store._conn.commit()
        assert store.get_admin_session("tok") is None
    finally:
        store.close()


def test_impersonation_set_and_clear():
    store = _store()
    try:
        aid = store.create_platform_admin(email="a@x.io", password_hash="p", mfa_secret="m")
        store.create_admin_session("tok", aid, expires_at=9e12)
        assert store.set_admin_impersonation("tok", effective_org_id=7,
                                             effective_user_id=None,
                                             reason="support ticket 42") is True
        sess = store.get_admin_session("tok")
        assert sess["effectiveOrgId"] == 7
        assert sess["impersonationReason"] == "support ticket 42"
        assert sess["impersonationStartedAt"] is not None
        assert store.clear_admin_impersonation("tok") is True
        sess = store.get_admin_session("tok")
        assert sess["effectiveOrgId"] is None and sess["impersonationReason"] is None
    finally:
        store.close()


def test_impersonation_no_live_session_returns_false():
    store = _store()
    try:
        assert store.set_admin_impersonation("ghost", effective_org_id=1,
                                             effective_user_id=None, reason="x") is False
    finally:
        store.close()


def test_delete_admin_session():
    store = _store()
    try:
        aid = store.create_platform_admin(email="a@x.io", password_hash="p", mfa_secret="m")
        store.create_admin_session("tok", aid, expires_at=9e12)
        assert store.delete_admin_session("tok") is True
        assert store.get_admin_session("tok") is None
        assert store.delete_admin_session("tok") is False
    finally:
        store.close()


# ----- DB-backed login throttle -----

def test_admin_login_throttle_locks_after_max_failures():
    store = _store()
    try:
        key = "a@x.io"
        for _ in range(ADMIN_LOGIN_MAX_FAILURES - 1):
            store.admin_login_record_failure(key)
        assert store.admin_login_is_locked(key) is False
        store.admin_login_record_failure(key)               # crosses the threshold
        assert store.admin_login_is_locked(key) is True
        store.admin_login_reset(key)                         # a success clears it
        assert store.admin_login_is_locked(key) is False
    finally:
        store.close()


def test_admin_login_throttle_survives_reopen():
    """The whole point of the DB throttle: a lockout survives a process restart."""
    path = _tmp()
    store = Store(path)
    try:
        for _ in range(ADMIN_LOGIN_MAX_FAILURES):
            store.admin_login_record_failure("k")
        assert store.admin_login_is_locked("k") is True
    finally:
        store.close()
    reopened = Store(path)
    try:
        assert reopened.admin_login_is_locked("k") is True
    finally:
        reopened.close()


def test_admin_login_throttle_lock_expires():
    store = _store()
    try:
        for _ in range(ADMIN_LOGIN_MAX_FAILURES):
            store.admin_login_record_failure("k", now=1000.0)
        assert store.admin_login_is_locked("k", now=1000.0) is True
        # far in the future, past the lockout window → unlocked (and row cleared)
        assert store.admin_login_is_locked("k", now=1000.0 + 100_000) is False
    finally:
        store.close()


# ----- TOTP anti-replay -----

def test_claim_totp_counter_rejects_replay():
    store = _store()
    try:
        assert store.claim_totp_counter(admin_id=1, counter=42) is True   # first use
        assert store.claim_totp_counter(admin_id=1, counter=42) is False  # replay
        assert store.claim_totp_counter(admin_id=1, counter=43) is True   # next step ok
        assert store.claim_totp_counter(admin_id=2, counter=42) is True   # other admin ok
    finally:
        store.close()


# ----- hash-chained audit -----

def test_audit_chain_appends_and_verifies():
    store = _store()
    try:
        r1 = store.append_admin_audit(acting_admin_id=1, action="login", ip="10.0.0.1")
        r2 = store.append_admin_audit(acting_admin_id=1, action="impersonate.start",
                                      target_org_id=5, reason="ticket")
        assert r1["prevHash"] == "0" * 64                   # genesis
        assert r2["prevHash"] == r1["rowHash"]              # chained
        v = store.verify_admin_audit_chain()
        assert v == {"ok": True, "count": 2, "firstBadId": None}
    finally:
        store.close()


def test_audit_chain_detects_tampering():
    store = _store()
    try:
        store.append_admin_audit(acting_admin_id=1, action="login")
        store.append_admin_audit(acting_admin_id=1, action="impersonate.start",
                                 target_org_id=5)
        store.append_admin_audit(acting_admin_id=1, action="impersonate.end")
        # tamper with the middle row's reason after the fact
        store._conn.execute(
            "UPDATE admin_audit_log SET reason='forged' WHERE action='impersonate.start'")
        store._conn.commit()
        v = store.verify_admin_audit_chain()
        assert v["ok"] is False
        assert v["firstBadId"] == 2
    finally:
        store.close()


def test_audit_empty_chain_is_ok():
    store = _store()
    try:
        assert store.verify_admin_audit_chain() == {"ok": True, "count": 0,
                                                    "firstBadId": None}
    finally:
        store.close()


def test_list_admin_audit_newest_first():
    store = _store()
    try:
        store.append_admin_audit(acting_admin_id=1, action="a")
        store.append_admin_audit(acting_admin_id=1, action="b")
        rows = store.list_admin_audit(limit=10)
        assert [r["action"] for r in rows] == ["b", "a"]
    finally:
        store.close()


# ----- cross-org org index -----

def test_list_organizations_with_member_count():
    store = _store()
    try:
        a = store.create_org_with_owner(email="a@x.io", password_hash="h", token="ta",
                                        expires_at=9e12, company_name="Acme")["orgId"]
        store.create_org_with_owner(email="b@x.io", password_hash="h", token="tb",
                                    expires_at=9e12, company_name="Beta")
        store.create_user_in_org(org_id=a, email="a2@x.io", password_hash="h",
                                 role="member")
        orgs = store.list_organizations()
        by_name = {o["name"]: o for o in orgs}
        assert set(by_name) == {"Acme", "Beta"}
        assert by_name["Acme"]["member_count"] == 2
        assert by_name["Beta"]["member_count"] == 1
    finally:
        store.close()
