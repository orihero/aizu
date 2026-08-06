"""Secure persistence for the per-worker bearer token (BUILD-PLAN review #2 + Phase 6).

The worker token authenticates this box to the dispatch plane; a leak lets an attacker
impersonate the worker. It is NEVER stored plaintext and NEVER logged. ``TokenStore`` is
a thin façade with an unchanged public API (``save``/``load``/``clear``) that delegates to
one backend, resolved ONCE at construction from ``AIZU_TOKEN_BACKEND``:

  - ``file``    — the Phase-1 Fernet-encrypted 0600 file (:class:`FernetFileBackend`).
  - ``keyring`` — the OS keychain (:class:`KeyringBackend`); loud error if unavailable.
  - ``auto``    — ALWAYS the Fernet file (default). Keyring is strictly OPT-IN because an
    unattended worker box must never risk a blocking OS-keychain unlock prompt at startup;
    set ``AIZU_TOKEN_BACKEND=keyring`` on a box whose keychain is pre-authorized.

``sidecar.py`` needs NO change: :class:`KeyringBackendError` subclasses
``SecretCipherError``, so ``_load_token_safely``'s existing except-clause still recovers a
corrupt keychain entry by clearing + re-registering. Survives a sidecar crash: the token
is read back on the next start, so a restart re-uses the bound identity.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Literal, Optional

from ..core.logsetup import get_logger
from ..secrets import SecretCipher
from .token_backends import (KEYRING_AVAILABLE, FernetFileBackend, KeyringBackend,
                             KeyringBackendError, _TokenBackend)

log = get_logger("aizu.worker.token_store")

_TOKEN_BACKEND_ENV = "AIZU_TOKEN_BACKEND"
_KEYRING_SERVICE = "aizu-worker-token"
_VALID_BACKENDS = ("keyring", "file", "auto")
# A short, unique value written+read+deleted to confirm a real working keychain exists
# (import success alone does not — e.g. headless Linux with no secret service).
_PROBE_TOKEN = "aizu-keyring-probe"


class TokenStore:
    """Encrypted token persistence with a pluggable backend."""

    def __init__(self, state_dir: Path, *, cipher: Optional[SecretCipher] = None,
                 backend: Optional[_TokenBackend] = None,
                 backend_kind_env: Optional[str] = None):
        self._state_dir = state_dir
        if backend is not None:
            self._backend = backend  # explicit override (test seam / advanced wiring)
            return
        env_value = (backend_kind_env if backend_kind_env is not None
                     else os.environ.get(_TOKEN_BACKEND_ENV))
        kind = resolve_backend_kind(
            env_value, keyring_available=KEYRING_AVAILABLE,
            probe=lambda: _keyring_health_ok(state_dir))
        if kind == "keyring":
            self._backend = KeyringBackend(_KEYRING_SERVICE, state_dir)
        else:
            self._backend = FernetFileBackend(state_dir, cipher=cipher)

    def save(self, token: str) -> None:
        if not token:
            raise ValueError("refusing to persist an empty worker token")
        self._backend.save(token)

    def load(self) -> Optional[str]:
        return self._backend.load()

    def clear(self) -> None:
        self._backend.clear()


def resolve_backend_kind(env_value: Optional[str], *, keyring_available: bool,
                         probe: Callable[[], bool]) -> Literal["keyring", "file"]:
    """Pure backend-selection decision (no I/O beyond the injected ``probe``).

    ``file`` → always file (keyring never touched). ``keyring`` → keyring iff available
    AND probe() passes, else a loud KeyringBackendError (explicit request never silently
    downgrades). ``auto`` (default) → ALWAYS file: keyring is opt-in only, because an
    unattended worker box must never risk a blocking keychain unlock prompt at startup (the
    health probe itself can prompt on a GUI-logged-in box). Any other value → ValueError."""
    choice = (env_value or "auto").strip().lower()
    if choice not in _VALID_BACKENDS:
        raise ValueError(
            f"{_TOKEN_BACKEND_ENV}={env_value!r} invalid — expected one of {_VALID_BACKENDS}")
    if choice == "keyring":
        if keyring_available and probe():
            return "keyring"
        raise KeyringBackendError(
            "AIZU_TOKEN_BACKEND=keyring but no working OS keychain is available")
    # "file" and "auto" both resolve to the Fernet file (keyring is opt-in only).
    return "file"


def _keyring_health_ok(state_dir: Path) -> bool:
    """Round-trip a throwaway probe entry to confirm a functioning keychain. Never
    raises — a failed probe just means 'use file'. Only called when keyring imported."""
    try:
        backend = KeyringBackend(_KEYRING_SERVICE + "-probe", state_dir)
        backend.save(_PROBE_TOKEN)
        ok = backend.load() == _PROBE_TOKEN
        backend.clear()
        return ok
    except Exception:  # noqa: BLE001 — any probe failure → fall back to file
        return False
