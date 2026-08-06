"""Store-level multi-tenancy (v7): v6→v7 migration, org-scoped CRUD, isolation,
signup atomicity, and the invite lifecycle."""
import os
import sqlite3
import tempfile

import pytest

from aizu.core.store import SCHEMA_VERSION, Store


def _tmp() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def _make_pre_v7_db(path: str) -> None:
    """A minimal v6-shaped DB (campaign_id-only scoping, no org dimension)."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT, updated_at REAL NOT NULL);
        CREATE TABLE integrations(platform TEXT PRIMARY KEY, connected INTEGER NOT NULL DEFAULT 0,
            detail TEXT, updated_at REAL NOT NULL);
        CREATE TABLE matches(campaign_id TEXT NOT NULL, platform TEXT NOT NULL DEFAULT 'instagram',
            reel_id TEXT, comment_id TEXT, status TEXT NOT NULL DEFAULT 'new',
            captured_at REAL NOT NULL, updated_at REAL NOT NULL,
            PRIMARY KEY(campaign_id, platform, comment_id));
        CREATE TABLE campaign_meta(campaign_id TEXT PRIMARY KEY, display_name TEXT,
            status TEXT NOT NULL DEFAULT 'live', budget_cap REAL, goal_target INTEGER,
            created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE campaign_briefs(campaign_id TEXT PRIMARY KEY, brief TEXT NOT NULL,
            updated_at REAL NOT NULL);
        CREATE TABLE health_flags(id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT,
            session_id TEXT, kind TEXT NOT NULL, severity TEXT NOT NULL, detail TEXT,
            created_at REAL NOT NULL, resolved_at REAL);
        """
    )
    conn.execute("INSERT INTO meta VALUES('schema_version','6')")
    conn.execute("INSERT INTO users(email,password_hash,created_at,updated_at) "
                 "VALUES('legacy@x.io','h',1,1)")
    conn.execute("INSERT INTO users(email,password_hash,created_at,updated_at) "
                 "VALUES('second@x.io','h',2,2)")
    conn.execute("INSERT INTO settings(key,value,updated_at) "
                 "VALUES('productName','\"Legacy Co\"',1)")
    conn.execute("INSERT INTO integrations(platform,connected,detail,updated_at) "
                 "VALUES('instagram',1,'@a',1)")
    conn.execute("INSERT INTO campaign_meta(campaign_id,status,created_at,updated_at) "
                 "VALUES('c1','live',1,1)")
    conn.execute("INSERT INTO campaign_briefs(campaign_id,brief,updated_at) VALUES('c1','{}',1)")
    conn.execute("INSERT INTO matches(campaign_id,reel_id,comment_id,status,captured_at,updated_at) "
                 "VALUES('c1','r','cm','new',1,1)")
    conn.commit()
    conn.close()


# ----- migration -----

def test_v6_to_v7_migration_folds_data_into_default_org():
    path = _tmp()
    _make_pre_v7_db(path)
    store = Store(path)
    try:
        ver = store._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        assert ver == str(SCHEMA_VERSION)      # migrated to the current schema
        orgs = [dict(r) for r in store._conn.execute(
            "SELECT id, name, created_by_user_id FROM organizations").fetchall()]
        assert len(orgs) == 1
        org = orgs[0]
        assert org["name"] == "Legacy Co"           # named from legacy productName
        assert org["created_by_user_id"] == 1        # earliest account
        org_id = org["id"]
        # every existing user becomes an owner of the default org
        users = store.list_org_users(org_id)
        assert {u["email"] for u in users} == {"legacy@x.io", "second@x.io"}
        assert all(u["role"] == "owner" for u in users)
        # data + registry backfilled
        assert store.org_for_campaign("c1") == org_id
        assert store._conn.execute(
            "SELECT org_id FROM matches WHERE comment_id='cm'").fetchone()[0] == org_id
        # workspace singletons re-keyed per org
        assert store.get_settings(org_id)["productName"] == "Legacy Co"
        assert store.list_integrations(org_id)[0]["platform"] == "instagram"
        # legacy aside-tables cleaned up
        leftovers = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '%legacy%'").fetchall()
        assert leftovers == []
    finally:
        store.close()


def test_v7_migration_idempotent():
    path = _tmp()
    _make_pre_v7_db(path)
    Store(path).close()             # migrate v6 -> v7
    store = Store(path)             # re-open an already-v7 DB
    try:
        assert store._conn.execute(
            "SELECT COUNT(*) FROM organizations").fetchone()[0] == 1  # no duplicate org
        assert store._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] \
            == str(SCHEMA_VERSION)
    finally:
        store.close()


def test_interrupted_migration_recovers_legacy_data(monkeypatch):
    """A kill between the (committed) rename-aside and the copy-back must NOT lose the
    legacy settings/integrations — the next open self-heals."""
    path = _tmp()
    _make_pre_v7_db(path)  # settings.productName='Legacy Co' + an integrations row

    def boom(self, c):
        raise RuntimeError("simulated crash mid-migration")

    monkeypatch.setattr(Store, "_migrate_to_v7", boom)
    with pytest.raises(RuntimeError):
        Store(path)
    monkeypatch.undo()
    # The rename was committed by executescript → legacy table is orphaned on disk.
    conn = sqlite3.connect(path)
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE '%legacy_v6'").fetchall()]
    conn.close()
    assert "settings__legacy_v6" in names
    # Second open: the migration self-heals and recovers the data.
    store = Store(path)
    try:
        org_id = store._conn.execute("SELECT id FROM organizations LIMIT 1").fetchone()[0]
        assert store.get_settings(org_id)["productName"] == "Legacy Co"   # recovered
        assert store.list_integrations(org_id)[0]["platform"] == "instagram"
        leftover = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '%legacy_v6'").fetchall()
        assert leftover == []                                              # cleaned up
    finally:
        store.close()


def test_migration_skipped_path_still_adds_org_id_columns():
    """A DB with engine data but none of users/settings/integrations must open without
    'no such column: org_id' on the index step (the org_id columns are ensured)."""
    path = _tmp()
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE matches(campaign_id TEXT NOT NULL, platform TEXT NOT NULL DEFAULT 'instagram',
            reel_id TEXT, comment_id TEXT, status TEXT NOT NULL DEFAULT 'new',
            captured_at REAL NOT NULL, updated_at REAL NOT NULL,
            PRIMARY KEY(campaign_id, platform, comment_id));
        CREATE TABLE campaign_meta(campaign_id TEXT PRIMARY KEY, display_name TEXT,
            status TEXT NOT NULL DEFAULT 'live', budget_cap REAL, goal_target INTEGER,
            created_at REAL NOT NULL, updated_at REAL NOT NULL);
        """
    )
    conn.execute("INSERT INTO meta VALUES('schema_version','6')")
    conn.execute("INSERT INTO matches(campaign_id,reel_id,comment_id,status,captured_at,updated_at) "
                 "VALUES('c1','r','cm','new',1,1)")
    conn.commit()
    conn.close()
    store = Store(path)  # must not raise 'no such column: org_id'
    try:
        assert store._has_column(store._conn, "matches", "org_id")
        assert store._has_column(store._conn, "campaign_meta", "org_id")
    finally:
        store.close()


def test_fresh_db_has_no_org_and_org_id_columns():
    path = _tmp()
    store = Store(path)
    try:
        assert store._conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0] == 0
        assert store._has_column(store._conn, "matches", "org_id")
        assert store._has_column(store._conn, "users", "org_id")
        assert store._has_column(store._conn, "users", "role")
    finally:
        store.close()


# ----- signup / identity -----

def test_signup_creates_org_owner_and_session_atomically():
    store = Store(_tmp())
    try:
        r = store.create_org_with_owner(
            email="boss@acme.io", password_hash="h", token="tok", expires_at=9e12,
            company_name="Acme", logo="L", description="we sell things")
        me = store.get_auth_session_user("tok")
        assert me["email"] == "boss@acme.io"
        assert me["orgId"] == r["orgId"]
        assert me["role"] == "owner"
        assert me["orgName"] == "Acme" and me["orgLogo"] == "L"
    finally:
        store.close()


def test_signup_blank_company_name_rejected():
    store = Store(_tmp())
    try:
        with pytest.raises(ValueError):
            store.create_org_with_owner(email="a@b.io", password_hash="h", token="t",
                                        expires_at=9e12, company_name="   ")
    finally:
        store.close()


def test_duplicate_email_signup_rolls_back_org():
    store = Store(_tmp())
    try:
        store.create_org_with_owner(email="dup@x.io", password_hash="h", token="t1",
                                    expires_at=9e12, company_name="One")
        before = store._conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            store.create_org_with_owner(email="dup@x.io", password_hash="h", token="t2",
                                        expires_at=9e12, company_name="Two")
        after = store._conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0]
        assert after == before  # the failed signup created no orphan org
    finally:
        store.close()


# ----- org isolation -----

def test_settings_integrations_and_users_are_org_scoped():
    store = Store(_tmp())
    try:
        a = store.create_org_with_owner(email="a@x.io", password_hash="h", token="ta",
                                        expires_at=9e12, company_name="A")["orgId"]
        b = store.create_org_with_owner(email="b@x.io", password_hash="h", token="tb",
                                        expires_at=9e12, company_name="B")["orgId"]
        store.set_setting(a, "productName", "Aprod")
        store.set_setting(b, "productName", "Bprod")
        assert store.get_settings(a)["productName"] == "Aprod"
        assert store.get_settings(b)["productName"] == "Bprod"  # no bleed
        store.set_integration(a, "instagram", connected=True)
        assert store.list_integrations(b) == []                 # B sees none of A's
        assert {u["email"] for u in store.list_org_users(a)} == {"a@x.io"}
        assert {u["email"] for u in store.list_org_users(b)} == {"b@x.io"}
    finally:
        store.close()


def test_per_campaign_rollup_is_org_scoped():
    store = Store(_tmp())
    try:
        a = store.create_org_with_owner(email="a@x.io", password_hash="h", token="ta",
                                        expires_at=9e12, company_name="A")["orgId"]
        b = store.create_org_with_owner(email="b@x.io", password_hash="h", token="tb",
                                        expires_at=9e12, company_name="B")["orgId"]
        store.upsert_campaign_meta("ca", org_id=a)
        store.upsert_campaign_brief("ca", {"x": 1}, org_id=a)
        store.upsert_campaign_meta("cb", org_id=b)
        store.upsert_campaign_brief("cb", {"x": 1}, org_id=b)
        store.upsert_match(campaign_id="ca", reel_id="r", comment_id="x1", username="u",
                           text="t", lang="uz", score=0.9, reason="r", extracted=None, tier="local")
        store.upsert_match(campaign_id="cb", reel_id="r", comment_id="x2", username="u",
                           text="t", lang="uz", score=0.9, reason="r", extracted=None, tier="local")
        assert [r["campaignId"] for r in store.per_campaign_rollup(a)] == ["ca"]
        assert [r["campaignId"] for r in store.per_campaign_rollup(b)] == ["cb"]
    finally:
        store.close()


def test_campaign_in_org_is_the_composite_tenant_filter():
    """store.campaign_in_org is the single repository-level ownership gate (PRD §10):
    a campaign matches ONLY its own org; a foreign org, an unknown campaign, and a
    None effective org all fail closed."""
    store = Store(_tmp())
    try:
        a = store.create_org_with_owner(email="a@x.io", password_hash="h", token="ta",
                                        expires_at=9e12, company_name="A")["orgId"]
        b = store.create_org_with_owner(email="b@x.io", password_hash="h", token="tb",
                                        expires_at=9e12, company_name="B")["orgId"]
        store.upsert_campaign_meta("ca", org_id=a)
        store.upsert_campaign_brief("ca", {"x": 1}, org_id=a)
        # owner org matches; the other org does not (cross-org read/write → 404 upstream)
        assert store.campaign_in_org("ca", a) is True
        assert store.campaign_in_org("ca", b) is False
        # unknown campaign never matches a real org
        assert store.campaign_in_org("nope", a) is False
        # fail closed: a None effective org (no-org session / non-impersonating admin)
        # never matches a real campaign — even though org_for_campaign("nope") is also None
        assert store.campaign_in_org("ca", None) is False
        assert store.campaign_in_org("nope", None) is False
        assert store.campaign_in_org(None, a) is False
    finally:
        store.close()


def test_campaign_in_org_matches_a_brief_only_registration():
    """A campaign registered only in campaign_briefs (no campaign_meta row) still
    resolves to its org — the filter mirrors org_for_campaign's two-table lookup."""
    store = Store(_tmp())
    try:
        a = store.create_org_with_owner(email="a@x.io", password_hash="h", token="ta",
                                        expires_at=9e12, company_name="A")["orgId"]
        store.upsert_campaign_brief("brief-only", {"x": 1}, org_id=a)
        assert store.campaign_in_org("brief-only", a) is True
        assert store.campaign_in_org("brief-only", a + 999) is False
    finally:
        store.close()


def test_update_and_delete_user_are_org_scoped():
    store = Store(_tmp())
    try:
        a = store.create_org_with_owner(email="a@x.io", password_hash="h", token="ta",
                                        expires_at=9e12, company_name="A")["orgId"]
        b = store.create_org_with_owner(email="b@x.io", password_hash="h", token="tb",
                                        expires_at=9e12, company_name="B")["orgId"]
        victim = store.create_user_in_org(org_id=b, email="v@b.io", password_hash="h", role="member")
        # org A cannot touch org B's user
        assert store.update_user_role(a, victim, "admin") is False
        assert store.delete_user(a, victim) is False
        # B can
        assert store.update_user_role(b, victim, "admin") is True
        assert store.delete_user(b, victim) is True
    finally:
        store.close()


def test_count_owners_for_last_owner_guard():
    store = Store(_tmp())
    try:
        org = store.create_org_with_owner(email="o@x.io", password_hash="h", token="t",
                                          expires_at=9e12, company_name="O")["orgId"]
        assert store.count_owners(org) == 1
        store.create_user_in_org(org_id=org, email="o2@x.io", password_hash="h", role="owner")
        assert store.count_owners(org) == 2
    finally:
        store.close()


# ----- invites -----

def test_invite_create_get_and_accept():
    store = Store(_tmp())
    try:
        org = store.create_org_with_owner(email="o@x.io", password_hash="h", token="t",
                                          expires_at=9e12, company_name="O")["orgId"]
        store.create_invite(org_id=org, role="admin", token="raw-tok", expires_at=9e12,
                            invited_by_user_id=1, email="new@x.io")
        info = store.get_invite("raw-tok")
        assert info["valid"] is True and info["role"] == "admin" and info["orgName"] == "O"
        assert len(store.list_invites(org)) == 1
        acc = store.accept_invite(token="raw-tok", email="new@x.io", password_hash="h",
                                  session_token="sess", expires_at=9e12)
        assert acc["role"] == "admin" and acc["orgId"] == org
        assert store.get_auth_session_user("sess")["role"] == "admin"
        assert store.list_invites(org) == []           # no longer pending
        with pytest.raises(ValueError):                  # single-use
            store.accept_invite(token="raw-tok", email="x@x.io", password_hash="h",
                                session_token="s2", expires_at=9e12)
    finally:
        store.close()


def test_expired_invite_is_invalid():
    store = Store(_tmp())
    try:
        org = store.create_org_with_owner(email="o@x.io", password_hash="h", token="t",
                                          expires_at=9e12, company_name="O")["orgId"]
        store.create_invite(org_id=org, role="viewer", token="old", expires_at=1.0)  # past
        assert store.get_invite("old")["valid"] is False
        with pytest.raises(ValueError):
            store.accept_invite(token="old", email="late@x.io", password_hash="h",
                                session_token="s", expires_at=9e12)
    finally:
        store.close()


def test_revoke_invite_is_org_scoped():
    store = Store(_tmp())
    try:
        a = store.create_org_with_owner(email="a@x.io", password_hash="h", token="ta",
                                        expires_at=9e12, company_name="A")["orgId"]
        b = store.create_org_with_owner(email="b@x.io", password_hash="h", token="tb",
                                        expires_at=9e12, company_name="B")["orgId"]
        inv_id = store.create_invite(org_id=a, role="member", token="raw", expires_at=9e12)
        assert store.revoke_invite(b, inv_id) is False   # B can't revoke A's invite
        assert store.revoke_invite(a, inv_id) is True
        assert store.list_invites(a) == []
    finally:
        store.close()
