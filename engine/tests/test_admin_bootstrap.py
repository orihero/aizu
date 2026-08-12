"""The out-of-band superadmin CLI (`python -m aizu.admin_bootstrap`): minting an
admin and — the path with no in-app equivalent — resetting a forgotten password."""
import os
import tempfile
import time

import pytest

from aizu import admin_bootstrap
from aizu.admin_auth import ADMIN_SESSION_TTL_SECONDS
from aizu.auth import verify_password
from aizu.core.store import Store
from aizu.secrets import SECRET_KEY_ENV, SecretCipher

KEY = SecretCipher.generate_key()
EMAIL = "ops@you.co"
OLD_PW = "correct horse battery"
NEW_PW = "staple donkey lantern"


def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setenv(SECRET_KEY_ENV, KEY)
    return _tmp_db()


def _run(db_path: str, *extra: str, password: str) -> int:
    """Drive main() the way an operator does, with the password piped (the
    --non-interactive branch) so no test ever touches a tty."""
    import io
    import sys
    stdin = sys.stdin
    sys.stdin = io.StringIO(password + "\n")
    try:
        return admin_bootstrap.main(
            ["--db", db_path, "--email", EMAIL, "--non-interactive", *extra])
    finally:
        sys.stdin = stdin


def _admin(db_path: str) -> dict:
    store = Store(db_path, secret_cipher=SecretCipher(KEY))
    try:
        admin = store.get_platform_admin_by_email(EMAIL)
        assert admin is not None
        return admin
    finally:
        store.close()


# ----- create -----

def test_create_then_duplicate_is_refused(db):
    assert _run(db, password=OLD_PW) == 0
    assert verify_password(OLD_PW, _admin(db)["password_hash"])
    assert _run(db, password=OLD_PW) == 1  # same email twice


# ----- reset -----

def test_reset_replaces_password_and_keeps_totp(db):
    assert _run(db, password=OLD_PW) == 0
    before = _admin(db)

    assert _run(db, "--reset-password", password=NEW_PW) == 0

    after = _admin(db)
    assert verify_password(NEW_PW, after["password_hash"])
    assert not verify_password(OLD_PW, after["password_hash"])
    # The whole point of the flag over delete + re-mint: the authenticator survives.
    assert after["mfa_secret"] == before["mfa_secret"]
    assert after["id"] == before["id"]


def test_reset_revokes_live_sessions_and_audits(db):
    assert _run(db, password=OLD_PW) == 0
    admin_id = int(_admin(db)["id"])
    store = Store(db, secret_cipher=SecretCipher(KEY))
    try:
        store.create_admin_session("tok-live", admin_id,
                                   time.time() + ADMIN_SESSION_TTL_SECONDS)
        assert store.get_admin_session("tok-live") is not None
    finally:
        store.close()

    assert _run(db, "--reset-password", password=NEW_PW) == 0

    store = Store(db, secret_cipher=SecretCipher(KEY))
    try:
        # A cookie minted under the old password must not outlive it.
        assert store.get_admin_session("tok-live") is None
        actions = [r["action"] for r in store.list_admin_audit(limit=10)]
        assert "admin.password.reset" in actions
        assert store.verify_admin_audit_chain()["ok"] is True
    finally:
        store.close()


def test_reset_unknown_email_is_an_error(db):
    assert _run(db, "--reset-password", password=NEW_PW) == 1


def test_reset_under_a_different_secret_key_refuses(db, monkeypatch):
    """A reset run with the wrong AIZU_SECRET_KEY would 'succeed' and still leave
    login broken (500 on TOTP decrypt), so it is refused up front."""
    assert _run(db, password=OLD_PW) == 0
    monkeypatch.setenv(SECRET_KEY_ENV, SecretCipher.generate_key())

    assert _run(db, "--reset-password", password=NEW_PW) == 2
    monkeypatch.setenv(SECRET_KEY_ENV, KEY)
    assert verify_password(OLD_PW, _admin(db)["password_hash"])  # untouched
