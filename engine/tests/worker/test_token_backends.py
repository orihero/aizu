"""Pluggable token backends (token_backends.py) + backend selection (token_store.py).

All keyring paths run against an injected FAKE — never a real OS keychain.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from aizu.secrets import SecretCipherError
from aizu.worker.token_backends import (KeyringBackend, KeyringBackendError,
                                             TokenBackendError)
from aizu.worker.token_store import TokenStore, resolve_backend_kind

from .fakes import FailingFakeKeyring, FakeKeyring


# ----- KeyringBackend against a fake -------------------------------------------

def test_keyring_backend_roundtrips(state_dir: Path):
    b = KeyringBackend("svc", state_dir, keyring_module=FakeKeyring())
    b.save("tok-123")
    assert b.load() == "tok-123"


def test_keyring_backend_load_absent_returns_none(state_dir: Path):
    b = KeyringBackend("svc", state_dir, keyring_module=FakeKeyring())
    assert b.load() is None


def test_keyring_backend_clear_is_idempotent(state_dir: Path):
    fake = FakeKeyring()
    b = KeyringBackend("svc", state_dir, keyring_module=fake)
    b.save("t")
    b.clear()
    b.clear()  # deleting a missing entry must NOT raise
    assert b.load() is None


def test_keyring_backend_failure_raises_keyring_error(state_dir: Path):
    b = KeyringBackend("svc", state_dir, keyring_module=FailingFakeKeyring())
    with pytest.raises(KeyringBackendError):
        b.load()


def test_keyring_error_subclasses_secretciphererror():
    # Load-bearing: sidecar._load_token_safely catches SecretCipherError only.
    assert issubclass(KeyringBackendError, SecretCipherError)
    assert issubclass(TokenBackendError, SecretCipherError)


def test_keyring_backend_uses_machine_id_as_username(state_dir: Path):
    (state_dir / "machine-id").write_text("box-abc", encoding="utf-8")
    fake = FakeKeyring()
    KeyringBackend("svc", state_dir, keyring_module=fake).save("t")
    assert fake.get_password("svc", "box-abc") == "t"


def test_keyring_backend_never_logs_the_token(state_dir: Path, caplog):
    b = KeyringBackend("svc", state_dir, keyring_module=FakeKeyring())
    with caplog.at_level(logging.DEBUG):
        b.save("do-not-log-me")
        b.load()
    assert "do-not-log-me" not in caplog.text


# ----- resolve_backend_kind truth table ----------------------------------------

def test_resolve_file_never_touches_keyring():
    called = {"probe": False}

    def probe():
        called["probe"] = True
        return True
    assert resolve_backend_kind("file", keyring_available=True, probe=probe) == "file"
    assert called["probe"] is False  # keyring never probed when file is explicit


def test_resolve_keyring_explicit_but_unavailable_raises():
    with pytest.raises(KeyringBackendError):
        resolve_backend_kind("keyring", keyring_available=False, probe=lambda: True)


def test_resolve_keyring_explicit_probe_fail_raises():
    with pytest.raises(KeyringBackendError):
        resolve_backend_kind("keyring", keyring_available=True, probe=lambda: False)


def test_resolve_auto_is_always_file_even_when_keyring_healthy():
    # Keyring is OPT-IN only — 'auto' must never auto-select it (unattended-box safety),
    # so it resolves to file regardless of keyring availability/health, without probing.
    probed = {"called": False}

    def probe():
        probed["called"] = True
        return True
    assert resolve_backend_kind("auto", keyring_available=True, probe=probe) == "file"
    assert resolve_backend_kind(None, keyring_available=True, probe=probe) == "file"
    assert probed["called"] is False


def test_resolve_auto_is_file_when_keyring_unavailable():
    assert resolve_backend_kind(None, keyring_available=False,
                                probe=lambda: True) == "file"


def test_resolve_garbage_value_raises_valueerror():
    with pytest.raises(ValueError):
        resolve_backend_kind("nonsense", keyring_available=True, probe=lambda: True)


# ----- TokenStore façade selection ---------------------------------------------

def test_store_explicit_keyring_unavailable_raises(state_dir: Path, monkeypatch):
    monkeypatch.setattr("aizu.worker.token_store.KEYRING_AVAILABLE", False)
    with pytest.raises(KeyringBackendError):
        TokenStore(state_dir, backend_kind_env="keyring")


def test_store_injected_backend_bypasses_resolution(state_dir: Path):
    b = KeyringBackend("svc", state_dir, keyring_module=FakeKeyring())
    store = TokenStore(state_dir, backend=b)
    store.save("via-injected")
    assert store.load() == "via-injected"


def test_store_load_failure_is_caught_by_sidecar_recovery(state_dir: Path):
    """A keyring load failure must be catchable by sidecar's except SecretCipherError."""
    b = KeyringBackend("svc", state_dir, keyring_module=FailingFakeKeyring())
    store = TokenStore(state_dir, backend=b)
    with pytest.raises(SecretCipherError):   # KeyringBackendError IS a SecretCipherError
        store.load()
