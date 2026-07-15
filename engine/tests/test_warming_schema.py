"""Schema v11 — account-warming tables + additive column self-heal (warming PRD §3.2)."""
import os
import sqlite3
import tempfile

from reelradar.core.store import SCHEMA_VERSION, Store


def fresh_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path), path


_NEW_TABLES = ("accounts", "account_state_changes", "campaign_accounts", "account_secrets")


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_fresh_db_is_v11_with_all_warming_tables():
    store, path = fresh_store()
    conn = store._conn
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for t in _NEW_TABLES:
        assert t in names, f"missing table {t}"
    ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    assert ver == str(SCHEMA_VERSION)
    assert SCHEMA_VERSION >= 11      # warming landed at v11 (later features may bump it)
    store.close()


def test_fresh_db_has_warming_join_columns():
    store, _ = fresh_store()
    conn = store._conn
    assert {"engine_mode", "account_id"} <= _cols(conn, "sessions")
    assert "account_id" in _cols(conn, "health_flags")
    assert "account_id" in _cols(conn, "actions")
    # engine_mode defaults to 'harvest' so existing harvest sessions read unchanged.
    store.close()


def test_engine_mode_defaults_to_harvest():
    store, _ = fresh_store()
    store.start_session("s1", "c1", "instagram")
    row = store._conn.execute(
        "SELECT engine_mode, account_id FROM sessions WHERE session_id='s1'").fetchone()
    assert row["engine_mode"] == "harvest"
    assert row["account_id"] is None
    store.close()


def test_reopen_is_idempotent():
    store, path = fresh_store()
    store.close()
    store2 = Store(path)        # second open must not crash or re-migrate
    ver = store2._conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    assert ver == str(SCHEMA_VERSION)
    store2.close()


def test_v10_db_self_heals_on_open():
    """A v10-shaped DB (no warming tables, no engine_mode/account_id) gains them
    on the next open via CREATE TABLE IF NOT EXISTS + _add_column_if_missing —
    no _migrate_to_v11 method, the additive idiom (PRD §3.2)."""
    store, path = fresh_store()
    store.close()
    # Strip the v11 surface back to v10 shape.
    conn = sqlite3.connect(path)
    conn.execute("DROP INDEX IF EXISTS idx_actions_account")
    conn.execute("DROP INDEX IF EXISTS idx_health_flags_account")
    for t in _NEW_TABLES:
        conn.execute(f"DROP TABLE {t}")
    conn.execute("ALTER TABLE sessions DROP COLUMN engine_mode")
    conn.execute("ALTER TABLE sessions DROP COLUMN account_id")
    conn.execute("ALTER TABLE health_flags DROP COLUMN account_id")
    conn.execute("ALTER TABLE actions DROP COLUMN account_id")
    conn.execute("UPDATE meta SET value='10' WHERE key='schema_version'")
    conn.commit()
    conn.close()

    store2 = Store(path)        # self-heal
    c = store2._conn
    names = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for t in _NEW_TABLES:
        assert t in names
    assert {"engine_mode", "account_id"} <= _cols(c, "sessions")
    assert "account_id" in _cols(c, "health_flags")
    assert "account_id" in _cols(c, "actions")
    assert c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] \
        == str(SCHEMA_VERSION)
    store2.close()
