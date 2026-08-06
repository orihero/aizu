"""Phase 0 — encrypted per-(org, platform) integration secrets (schema v8).

Covers the Fernet cipher round-trip, the store CRUD, the v7→v8 migration
(existing integration rows survive; the new secret table appears), and the
loud behavior when AIZU_SECRET_KEY is absent.
"""
import os
import sqlite3
import tempfile

import pytest

from aizu.secrets import SecretCipher, SecretCipherError
from aizu.core.store import SCHEMA_VERSION, Store


def _tmp() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


# ----- cipher -----

def test_cipher_round_trips_a_json_dict():
    cipher = SecretCipher(SecretCipher.generate_key())
    secret = {"api_key": "AIza-secret", "nested": {"n": 1}}

    blob = cipher.encrypt(secret)

    assert isinstance(blob, str)
    assert "api_key" not in blob          # ciphertext does not leak the plaintext
    assert cipher.decrypt(blob) == secret


def test_cipher_from_env_requires_the_key(monkeypatch):
    monkeypatch.delenv("AIZU_SECRET_KEY", raising=False)
    with pytest.raises(SecretCipherError):
        SecretCipher.from_env()


def test_cipher_from_env_reads_the_key(monkeypatch):
    key = SecretCipher.generate_key()
    monkeypatch.setenv("AIZU_SECRET_KEY", key)
    cipher = SecretCipher.from_env()
    assert cipher.decrypt(cipher.encrypt({"x": 1})) == {"x": 1}


def test_cipher_rejects_a_garbage_key():
    with pytest.raises(SecretCipherError):
        SecretCipher("not-a-valid-fernet-key")


def test_cipher_rejects_a_wrong_length_key():
    # A 44-char passphrase is valid urlsafe-base64 but decodes to !=32 bytes —
    # must be rejected so weak key material can't slip in (security review HIGH #1).
    passphrase = "A" * 44      # valid base64, decodes to 33 bytes
    with pytest.raises(SecretCipherError, match="32 bytes"):
        SecretCipher(passphrase)


def test_decrypt_rejects_a_tampered_blob():
    cipher = SecretCipher(SecretCipher.generate_key())
    with pytest.raises(SecretCipherError):
        cipher.decrypt("garbage-not-a-token")


def test_a_different_key_cannot_decrypt():
    a = SecretCipher(SecretCipher.generate_key())
    b = SecretCipher(SecretCipher.generate_key())
    blob = a.encrypt({"api_key": "x"})
    with pytest.raises(SecretCipherError):
        b.decrypt(blob)


# ----- store CRUD -----

def _store_with_cipher(path: str) -> Store:
    return Store(path, secret_cipher=SecretCipher(SecretCipher.generate_key()))


def test_set_get_delete_integration_secret():
    store = _store_with_cipher(_tmp())
    try:
        assert store.get_integration_secret(1, "youtube") is None      # absent → None
        store.set_integration_secret(1, "youtube", {"api_key": "AIza"})
        assert store.get_integration_secret(1, "youtube") == {"api_key": "AIza"}
        store.set_integration_secret(1, "youtube", {"api_key": "rotated"})
        assert store.get_integration_secret(1, "youtube") == {"api_key": "rotated"}
        store.delete_integration_secret(1, "youtube")
        assert store.get_integration_secret(1, "youtube") is None
    finally:
        store.close()


def test_secret_is_encrypted_at_rest():
    store = _store_with_cipher(_tmp())
    try:
        store.set_integration_secret(7, "telegram", {"session": "TOPSECRET"})
        raw = store._conn.execute(
            "SELECT secret_blob FROM integration_secrets WHERE org_id=7").fetchone()[0]
        assert "TOPSECRET" not in raw      # never stored in plaintext
    finally:
        store.close()


def test_secrets_are_org_scoped():
    store = _store_with_cipher(_tmp())
    try:
        store.set_integration_secret(1, "youtube", {"api_key": "a"})
        store.set_integration_secret(2, "youtube", {"api_key": "b"})
        assert store.get_integration_secret(1, "youtube") == {"api_key": "a"}
        assert store.get_integration_secret(2, "youtube") == {"api_key": "b"}
        store.delete_integration_secret(1, "youtube")
        assert store.get_integration_secret(2, "youtube") == {"api_key": "b"}
    finally:
        store.close()


def test_secret_methods_require_a_cipher(monkeypatch):
    monkeypatch.delenv("AIZU_SECRET_KEY", raising=False)
    store = Store(_tmp())     # no cipher injected, no env key
    try:
        with pytest.raises(SecretCipherError):
            store.set_integration_secret(1, "youtube", {"api_key": "x"})
    finally:
        store.close()


# ----- migration -----

def _make_v7_db(path: str) -> None:
    """A minimal v7-shaped DB: per-org integrations, no integration_secrets table."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE organizations(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            logo TEXT, description TEXT, created_by_user_id INTEGER,
            created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE integrations(org_id INTEGER NOT NULL, platform TEXT NOT NULL,
            connected INTEGER NOT NULL DEFAULT 0, detail TEXT, updated_at REAL NOT NULL,
            PRIMARY KEY(org_id, platform));
        """
    )
    conn.execute("INSERT INTO meta VALUES('schema_version','7')")
    conn.execute("INSERT INTO organizations(name,created_at,updated_at) VALUES('Acme',1,1)")
    conn.execute("INSERT INTO integrations(org_id,platform,connected,detail,updated_at) "
                 "VALUES(1,'youtube',1,'connected',1)")
    conn.commit()
    conn.close()


def test_v7_to_v8_migration_preserves_data_and_adds_secret_table():
    path = _tmp()
    _make_v7_db(path)
    store = _store_with_cipher(path)
    try:
        ver = store._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        # The store always brings an older DB up to the current schema. v8 added
        # the secret table; later additive bumps (v9 audit_log) keep that table,
        # so assert we reached the current version rather than a stale literal.
        assert ver == str(SCHEMA_VERSION)
        # the pre-existing integration row survives the upgrade
        rows = store.list_integrations(1)
        assert [r["platform"] for r in rows] == ["youtube"]
        assert rows[0]["connected"] == 1
        # the new secret table exists and is usable
        store.set_integration_secret(1, "youtube", {"api_key": "k"})
        assert store.get_integration_secret(1, "youtube") == {"api_key": "k"}
    finally:
        store.close()
