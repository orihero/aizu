"""Store-level distributed-workers pool (v14): additive migration + self-heal,
token-hashed registry, presence heartbeat, derived status, token auth, fleet
snapshot, and revocation. AAA style (mirrors tests/test_multitenancy_store.py)."""
import json
import os
import sqlite3
import tempfile

from aizu.auth import hash_session_token, new_session_token
from aizu.core.store import (
    SCHEMA_VERSION,
    WORKER_HEARTBEAT_INTERVAL_SEC,
    Store,
    derive_worker_status,
)


def _tmp() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def _register(store: Store, worker_id: str = "w-1", *, token: str | None = None,
              **kw) -> tuple[dict, str]:
    """Arrange helper: mint a token (server's job) and register a worker."""
    token = token or new_session_token()
    row = store.register_worker(worker_id=worker_id, token=token, **kw)
    return row, token


# ----- migration / self-heal -----

def test_schema_v14_fresh_db_has_workers_table():
    # Arrange / Act
    path = _tmp()
    store = Store(path)
    try:
        # Assert: table + both indexes exist, version is v14.
        tbl = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='workers'"
        ).fetchone()
        assert tbl is not None
        idx = {r[0] for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='workers'").fetchall()}
        assert {"idx_workers_org", "idx_workers_token"} <= idx
        ver = store._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        assert ver == str(SCHEMA_VERSION)  # workers landed at v14; version floats forward
    finally:
        store.close()


def _make_v13_db_without_workers(path: str) -> None:
    """A minimal v13-shaped DB that PREDATES the workers table."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE organizations(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
            logo TEXT, description TEXT, created_by_user_id INTEGER,
            created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE matches(campaign_id TEXT NOT NULL, platform TEXT NOT NULL DEFAULT 'instagram',
            reel_id TEXT, comment_id TEXT, status TEXT NOT NULL DEFAULT 'new', org_id INTEGER,
            captured_at REAL NOT NULL, updated_at REAL NOT NULL,
            PRIMARY KEY(campaign_id, platform, comment_id));
        """
    )
    conn.execute("INSERT INTO meta VALUES('schema_version','13')")
    conn.execute("INSERT INTO organizations(name,created_at,updated_at) VALUES('Acme',1,1)")
    conn.execute("INSERT INTO matches(campaign_id,reel_id,comment_id,status,org_id,"
                 "captured_at,updated_at) VALUES('c1','r','cm','new',1,1,1)")
    conn.commit()
    conn.close()


def test_schema_v13_db_self_heals_to_v14():
    # Arrange: a v13 DB with no workers table.
    path = _tmp()
    _make_v13_db_without_workers(path)
    # Act: open with current code.
    store = Store(path)
    try:
        # Assert: workers table injected, version bumped, no data lost.
        tbl = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='workers'"
        ).fetchone()
        assert tbl is not None
        ver = store._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        assert ver == str(SCHEMA_VERSION)
        assert store._conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 1
        assert store._conn.execute(
            "SELECT name FROM organizations").fetchone()[0] == "Acme"
    finally:
        store.close()


# ----- register: hashing + row shape + idempotent upsert -----

def test_register_worker_persists_hash_not_plaintext():
    # Arrange / Act
    store = Store(_tmp())
    try:
        _, token = _register(store, "w-1")
        # Assert: stored hash == SHA-256(token); raw token nowhere in the row.
        row = store._conn.execute(
            "SELECT worker_token_hash FROM workers WHERE id='w-1'").fetchone()
        assert row["worker_token_hash"] == hash_session_token(token)
        full = dict(store._conn.execute(
            "SELECT * FROM workers WHERE id='w-1'").fetchone())
        assert token not in json.dumps(full)
    finally:
        store.close()


def test_register_worker_returns_row_shape_without_token():
    # Arrange / Act
    store = Store(_tmp())
    try:
        row, _ = _register(store, "w-1", org_id=3, display_name="box",
                           host="h", os="linux", agent_version="0.1.0",
                           max_sessions=2, capabilities=[[3, "instagram", "acme"]])
        # Assert: documented keys present; token/hash absent.
        assert set(row) == {
            "id", "orgId", "displayName", "host", "os", "agentVersion",
            "maxSessions", "currentSessions", "capabilities", "registeredAt",
            "lastHeartbeatAt", "tokenExpiresAt", "revokedAt"}
        assert row["orgId"] == 3
        assert row["maxSessions"] == 2
        assert row["currentSessions"] == 0
        assert row["capabilities"] == [[3, "instagram", "acme"]]
        assert row["revokedAt"] is None
        assert "token" not in row and "worker_token_hash" not in row
    finally:
        store.close()


def test_register_worker_is_idempotent_upsert_rotates_token():
    # Arrange: register, then re-register the SAME id with a NEW token.
    store = Store(_tmp())
    try:
        r1, t1 = _register(store, "w-1", display_name="old")
        store.revoke_worker("w-1")  # revoke before re-register
        r2, t2 = _register(store, "w-1", display_name="new")
        # Assert: one row; second token authenticates, first does not.
        assert store._conn.execute("SELECT COUNT(*) FROM workers").fetchone()[0] == 1
        assert store.get_worker_by_token(t2) is not None
        assert store.get_worker_by_token(t1) is None
        # registered_at preserved; last_heartbeat refreshed; revoked cleared.
        assert r2["registeredAt"] == r1["registeredAt"]
        assert r2["lastHeartbeatAt"] >= r1["lastHeartbeatAt"]
        row = store._conn.execute(
            "SELECT revoked_at, display_name FROM workers WHERE id='w-1'").fetchone()
        assert row["revoked_at"] is None
        assert row["display_name"] == "new"
    finally:
        store.close()


def test_register_live_worker_rotates_token_without_prior_revoke():
    # Arrange: the PRODUCTION path — re-register a still-active worker (no revoke
    # in between). Guards against the ON CONFLICT clause skipping token rotation
    # when revoked_at IS NULL (would let a leaked old token keep authenticating).
    store = Store(_tmp())
    try:
        r1, t1 = _register(store, "w-live", display_name="v1")
        r2, t2 = _register(store, "w-live", display_name="v2")  # no revoke
        # Assert: old token rotated away, new token live, registration preserved.
        assert store.get_worker_by_token(t1) is None
        assert store.get_worker_by_token(t2) is not None
        assert r2["registeredAt"] == r1["registeredAt"]
        assert store._conn.execute(
            "SELECT COUNT(*) FROM workers").fetchone()[0] == 1
    finally:
        store.close()


def test_heartbeat_interval_constants_agree():
    # The status-derivation cadence (core/store.py) and the worker beat cadence
    # (worker/__init__.py) MUST stay equal; they are duplicated by design (no
    # core→worker import). Catch silent divergence at CI time (code review L).
    from aizu.worker import DEFAULT_HEARTBEAT_INTERVAL_SEC
    assert float(DEFAULT_HEARTBEAT_INTERVAL_SEC) == WORKER_HEARTBEAT_INTERVAL_SEC


# ----- token auth (fail closed) -----

def test_get_worker_by_token_matches_active():
    store = Store(_tmp())
    try:
        _, token = _register(store, "w-1", org_id=7,
                             capabilities=[[7, "youtube", "yt"]], max_sessions=4)
        ident = store.get_worker_by_token(token)
        assert ident == {
            "id": "w-1", "orgId": 7, "capabilities": [[7, "youtube", "yt"]],
            "maxSessions": 4, "currentSessions": 0, "agentVersion": None}
    finally:
        store.close()


def test_get_worker_by_token_none_for_unknown():
    store = Store(_tmp())
    try:
        _register(store, "w-1")
        assert store.get_worker_by_token(new_session_token()) is None
        assert store.get_worker_by_token("") is None
    finally:
        store.close()


def test_get_worker_by_token_fails_closed_on_revoked():
    store = Store(_tmp())
    try:
        _, token = _register(store, "w-1")
        store.revoke_worker("w-1")
        assert store.get_worker_by_token(token) is None
    finally:
        store.close()


def test_get_worker_by_token_fails_closed_on_expired():
    store = Store(_tmp())
    try:
        import time as _t
        _, token = _register(store, "w-1", token_expires_at=_t.time() - 1)
        assert store.get_worker_by_token(token) is None
    finally:
        store.close()


# ----- presence heartbeat -----

def test_record_worker_heartbeat_updates_only_own_row():
    store = Store(_tmp())
    try:
        _register(store, "w-a")
        _register(store, "w-b")
        before_b = store._conn.execute(
            "SELECT last_heartbeat_at FROM workers WHERE id='w-b'").fetchone()[0]
        a_old = store._conn.execute(
            "SELECT last_heartbeat_at FROM workers WHERE id='w-a'").fetchone()[0]
        # Act
        assert store.record_worker_heartbeat(worker_id="w-a") is True
        # Assert: A advanced, B unchanged.
        a_new = store._conn.execute(
            "SELECT last_heartbeat_at FROM workers WHERE id='w-a'").fetchone()[0]
        after_b = store._conn.execute(
            "SELECT last_heartbeat_at FROM workers WHERE id='w-b'").fetchone()[0]
        assert a_new >= a_old
        assert after_b == before_b
    finally:
        store.close()


def test_record_worker_heartbeat_updates_current_sessions_when_given():
    store = Store(_tmp())
    try:
        _register(store, "w-1", max_sessions=5)
        store.record_worker_heartbeat(worker_id="w-1", current_sessions=3)
        cs = store._conn.execute(
            "SELECT current_sessions FROM workers WHERE id='w-1'").fetchone()[0]
        assert cs == 3
    finally:
        store.close()


def test_record_worker_heartbeat_leaves_sessions_when_omitted():
    store = Store(_tmp())
    try:
        _register(store, "w-1")
        store.record_worker_heartbeat(worker_id="w-1", current_sessions=2)
        store.record_worker_heartbeat(worker_id="w-1")  # omit → unchanged
        cs = store._conn.execute(
            "SELECT current_sessions FROM workers WHERE id='w-1'").fetchone()[0]
        assert cs == 2
    finally:
        store.close()


def test_record_worker_heartbeat_false_for_unknown_or_revoked():
    store = Store(_tmp())
    try:
        assert store.record_worker_heartbeat(worker_id="ghost") is False
        _register(store, "w-1")
        store.revoke_worker("w-1")
        assert store.record_worker_heartbeat(worker_id="w-1") is False
    finally:
        store.close()


# ----- revoke -----

def test_revoke_worker_true_then_false():
    store = Store(_tmp())
    try:
        _register(store, "w-1")
        assert store.revoke_worker("w-1") is True       # first revoke
        assert store.revoke_worker("w-1") is False       # already revoked
        assert store.revoke_worker("unknown") is False   # unknown id
    finally:
        store.close()


# ----- derived status + fleet snapshot -----

def test_derive_worker_status_unit():
    # online ≤ 2×20=40; stale ≤ 6×20=120; offline > 120; None → offline.
    now = 1000.0
    assert derive_worker_status(now - 40.0, now) == "online"
    assert derive_worker_status(now - 40.001, now) == "stale"
    assert derive_worker_status(now - 120.0, now) == "stale"
    assert derive_worker_status(now - 120.001, now) == "offline"
    assert derive_worker_status(None, now) == "offline"


def test_list_workers_derived_status_boundaries():
    # Arrange: seed rows with controlled last_heartbeat_at, freeze now.
    store = Store(_tmp())
    try:
        now = 10_000.0
        interval = WORKER_HEARTBEAT_INTERVAL_SEC  # 20.0
        for wid, age in (("on", 2 * interval), ("just-stale", 2 * interval + 0.001),
                         ("edge-stale", 6 * interval), ("off", 6 * interval + 0.001)):
            _register(store, wid)
            store._conn.execute(
                "UPDATE workers SET last_heartbeat_at=? WHERE id=?",
                (now - age, wid))
        _register(store, "never")
        store._conn.execute("UPDATE workers SET last_heartbeat_at=NULL WHERE id='never'")
        store._conn.commit()
        # Act
        fleet = {w["id"]: w for w in store.list_workers(now=now)}
        # Assert: LOCKED #6 boundaries.
        assert fleet["on"]["status"] == "online"
        assert fleet["just-stale"]["status"] == "stale"
        assert fleet["edge-stale"]["status"] == "stale"
        assert fleet["off"]["status"] == "offline"
        assert fleet["never"]["status"] == "offline"
        assert fleet["never"]["lastSeenAgeSec"] is None
        assert abs(fleet["on"]["lastSeenAgeSec"] - 2 * interval) < 1e-6
    finally:
        store.close()


def test_list_workers_includes_revoked_with_status():
    store = Store(_tmp())
    try:
        _register(store, "w-1")
        store.revoke_worker("w-1", now=5.0)
        fleet = store.list_workers()
        assert len(fleet) == 1
        assert fleet[0]["id"] == "w-1"
        assert fleet[0]["revokedAt"] == 5.0
        assert fleet[0]["status"] in {"online", "stale", "offline"}
    finally:
        store.close()


def test_list_workers_capabilities_roundtrip_and_tolerant():
    store = Store(_tmp())
    try:
        _register(store, "good", capabilities=[[1, "instagram", "a"], [None, "x", "b"]])
        _register(store, "corrupt")
        store._conn.execute(
            "UPDATE workers SET capabilities=? WHERE id='corrupt'", ("{not json",))
        store._conn.commit()
        fleet = {w["id"]: w for w in store.list_workers()}
        assert fleet["good"]["capabilities"] == [[1, "instagram", "a"], [None, "x", "b"]]
        assert fleet["corrupt"]["capabilities"] == []  # tolerant, never raises
    finally:
        store.close()


def test_list_workers_shape_and_order():
    store = Store(_tmp())
    try:
        r1, _ = _register(store, "first")
        store._conn.execute(
            "UPDATE workers SET registered_at=? WHERE id='first'", (1.0,))
        r2, _ = _register(store, "second")
        store._conn.execute(
            "UPDATE workers SET registered_at=? WHERE id='second'", (2.0,))
        store._conn.commit()
        fleet = store.list_workers()
        # newest registration first
        assert [w["id"] for w in fleet] == ["second", "first"]
        keys = set(fleet[0])
        assert keys == {
            "id", "orgId", "displayName", "host", "os", "agentVersion",
            "maxSessions", "currentSessions", "capabilities", "registeredAt",
            "lastHeartbeatAt", "lastSeenAgeSec", "status", "revokedAt", "currentJob"}
    finally:
        store.close()


def test_list_workers_enriches_current_job():
    """The fleet snapshot shows WHAT each worker is running: an idle box has
    currentJob=None; a box holding a leased job carries its {jobId, campaignId,
    platform, status, runId, leaseExpiresAt}."""
    store = Store(_tmp())
    try:
        caps = [[1, "instagram", "acme"]]
        _register(store, "busy", org_id=1, capabilities=caps)
        _register(store, "idle", org_id=1, capabilities=caps)
        store.enqueue_job(job_id="j1", campaign_id="c-acme", platform="instagram",
                          required_account_handle="acme", org_id=1,
                          spec={"engine_mode": "harvest", "run_id": "run-abc"})
        leased = store.lease_one_job(worker_id="busy", capabilities=caps, now=1000.0)
        assert leased is not None

        fleet = {w["id"]: w for w in store.list_workers()}
        assert fleet["idle"]["currentJob"] is None
        cj = fleet["busy"]["currentJob"]
        assert cj["jobId"] == "j1"
        assert cj["campaignId"] == "c-acme"
        assert cj["platform"] == "instagram"
        assert cj["status"] == "leased"
        assert cj["runId"] == "run-abc"
    finally:
        store.close()
