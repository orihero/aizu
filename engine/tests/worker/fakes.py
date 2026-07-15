"""Shared test doubles for the worker plane. Currently: in-memory keyring fakes so the
token-backend tests never touch a real OS keychain (headless CI has none, and a real
backend can block on an unlock prompt)."""
from __future__ import annotations

from typing import Optional


class FakeKeyring:
    """Dict-backed stand-in for the ``keyring`` module (set/get/delete_password)."""

    def __init__(self):
        self._store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def get_password(self, service: str, username: str) -> Optional[str]:
        return self._store.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self._store[(service, username)]
        except KeyError:
            raise PasswordDeleteError("not found")


class PasswordDeleteError(Exception):
    """Mirror keyring.errors.PasswordDeleteError's NAME (matched by _is_not_found)."""


class FailingFakeKeyring:
    """Raises on every operation — exercises the KeyringBackendError path."""

    def set_password(self, *a):
        raise RuntimeError("keychain locked")

    def get_password(self, *a):
        raise RuntimeError("keychain locked")

    def delete_password(self, *a):
        raise RuntimeError("keychain locked")
