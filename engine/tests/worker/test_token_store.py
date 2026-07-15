"""Encrypted worker-token persistence (token_store.py, BUILD-PLAN §2.4 / review #2)."""
from __future__ import annotations

import stat
from pathlib import Path

import pytest

from reelradar.secrets import SecretCipher, SecretCipherError
from reelradar.worker.token_store import TokenStore


def test_save_then_load_roundtrips(state_dir: Path, cipher: SecretCipher):
    store = TokenStore(state_dir, cipher=cipher)
    store.save("super-secret-token")
    assert store.load() == "super-secret-token"


def test_token_is_not_stored_in_plaintext(state_dir: Path, cipher: SecretCipher):
    store = TokenStore(state_dir, cipher=cipher)
    store.save("plaintext-leak-check")
    blob = (state_dir / "worker-token.enc").read_text(encoding="ascii")
    assert "plaintext-leak-check" not in blob  # encrypted at rest


def test_token_file_is_mode_0600(state_dir: Path, cipher: SecretCipher):
    store = TokenStore(state_dir, cipher=cipher)
    store.save("t")
    mode = stat.S_IMODE((state_dir / "worker-token.enc").stat().st_mode)
    assert mode == 0o600


def test_load_returns_none_when_absent(state_dir: Path, cipher: SecretCipher):
    assert TokenStore(state_dir, cipher=cipher).load() is None


def test_clear_removes_token(state_dir: Path, cipher: SecretCipher):
    store = TokenStore(state_dir, cipher=cipher)
    store.save("t")
    store.clear()
    assert store.load() is None


def test_save_rejects_empty_token(state_dir: Path, cipher: SecretCipher):
    with pytest.raises(ValueError):
        TokenStore(state_dir, cipher=cipher).save("")


def test_tampered_blob_raises_not_silently_ignored(state_dir: Path, cipher: SecretCipher):
    store = TokenStore(state_dir, cipher=cipher)
    store.save("t")
    (state_dir / "worker-token.enc").write_text("tampered", encoding="ascii")
    with pytest.raises(SecretCipherError):
        store.load()  # a tampered token is a loud error, never treated as 'no token'
