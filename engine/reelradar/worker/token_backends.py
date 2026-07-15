"""Pluggable persistence backends for the worker bearer token (Phase 6).

Two backends implement one internal protocol behind ``TokenStore``:

  - :class:`FernetFileBackend` — the Phase-1 behavior verbatim: Fernet-encrypted file at
    mode 0600 under the worker state dir, atomic tmp+os.replace write, size-capped read.
  - :class:`KeyringBackend` — the OS keychain (macOS Keychain / Windows Credential Locker
    / Linux SecretService) via the OPTIONAL ``keyring`` package, guarded exactly like
    ``core.cdp`` guards Playwright so this module imports fine when keyring is absent.

Neither ever logs the token value; both raise a loud, typed error on a corrupt/unreadable
entry so ``TokenStore`` can clear + re-register rather than run blind.

CI NOTE: never test against a REAL keyring — headless runners have no secret service and
a real backend can block on an unlock prompt. All keyring tests inject a fake module.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Protocol

from ..core.logsetup import get_logger
from ..secrets import SecretCipher, SecretCipherError

log = get_logger("reelradar.worker.token_backends")

# Optional keyring — mirrors core/cdp.py's PLAYWRIGHT_AVAILABLE guard so importing the
# worker package never requires keyring to be installed.
try:  # pragma: no cover - exercised by whichever path is installed
    import keyring as _keyring
    KEYRING_AVAILABLE = True
except Exception:  # pragma: no cover
    _keyring = None
    KEYRING_AVAILABLE = False

_TOKEN_FILE = "worker-token.enc"
_TOKEN_KEY = "worker_token"
# A legitimate encrypted token blob is a few hundred bytes; cap the read so a tampered /
# symlinked file can't force a large allocation before decrypt fails (security M6).
_MAX_TOKEN_FILE_BYTES = 4096


class TokenBackendError(SecretCipherError):
    """Base for a backend read/write failure. Subclasses SecretCipherError so
    ``sidecar._load_token_safely``'s existing ``except SecretCipherError`` keeps catching
    a keyring failure with ZERO sidecar changes — this subclassing is load-bearing; do
    NOT re-parent it to a plain RuntimeError."""


class KeyringBackendError(TokenBackendError):
    """A keychain get/set/delete failed (permission denied, no secret service, tamper)."""


class _TokenBackend(Protocol):
    """The narrow contract ``TokenStore`` delegates to."""

    def save(self, token: str) -> None: ...
    def load(self) -> Optional[str]: ...
    def clear(self) -> None: ...


class FernetFileBackend:
    """Encrypted-file token persistence — the Phase-1 behavior, extracted verbatim."""

    def __init__(self, state_dir: Path, *, cipher: Optional[SecretCipher] = None):
        self._path = state_dir / _TOKEN_FILE
        self._state_dir = state_dir
        self._cipher = cipher  # lazily resolved so import never needs the key

    def _get_cipher(self) -> SecretCipher:
        if self._cipher is None:
            self._cipher = SecretCipher.from_env()
        return self._cipher

    def save(self, token: str) -> None:
        """Encrypt and atomically write at mode 0600 (tmp file 0600 then os.replace, so
        the mode is correct BEFORE any bytes land and there is no partial-write window)."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        blob = self._get_cipher().encrypt({_TOKEN_KEY: token})
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, blob.encode("ascii"))
        finally:
            os.close(fd)
        os.replace(tmp, self._path)
        log.info("Worker token persisted (encrypted file) to %s", self._path.name)

    def load(self) -> Optional[str]:
        """Return the stored token, or None if none is persisted. A corrupt/tampered
        blob raises SecretCipherError (never silently treated as 'no token')."""
        if not self._path.exists():
            return None
        if self._path.stat().st_size > _MAX_TOKEN_FILE_BYTES:
            raise SecretCipherError("worker token file is suspiciously large")
        blob = self._path.read_text(encoding="ascii").strip()
        if not blob:
            return None
        data = self._get_cipher().decrypt(blob)
        token = data.get(_TOKEN_KEY)
        if not isinstance(token, str) or not token:
            raise SecretCipherError("worker token blob missing the token field")
        return token

    def clear(self) -> None:
        """Remove the persisted token (e.g. after revocation). Idempotent."""
        self._path.unlink(missing_ok=True)


class KeyringBackend:
    """OS-keychain token persistence via the injectable ``keyring`` module.

    ``keyring_module`` is injected (defaults to the real imported package) so tests use an
    in-memory fake and never touch a real OS keychain. ``username`` is resolved lazily per
    operation from the box's machine-id file — by save time (inside ``_register``) that
    file exists, so we avoid a construction-order dependency on WorkerConfig.machine_id."""

    def __init__(self, service: str, state_dir: Path, *, keyring_module=None):
        self._service = service
        self._state_dir = state_dir
        self._keyring = keyring_module if keyring_module is not None else _keyring
        if self._keyring is None:
            raise KeyringBackendError(
                "keyring backend requested but the 'keyring' package is not installed")

    def _username(self) -> str:
        """Stable per-box account name. There is exactly ONE token per state_dir, so a
        fixed fallback is safe when the machine-id file is not written yet."""
        path = self._state_dir / "machine-id"
        try:
            existing = path.read_text(encoding="utf-8").strip()
        except OSError:
            existing = ""
        return existing or "unregistered"

    def save(self, token: str) -> None:
        try:
            self._keyring.set_password(self._service, self._username(), token)
        except Exception as e:  # noqa: BLE001 — backend hierarchy varies by OS
            raise KeyringBackendError(f"keyring set_password failed: {type(e).__name__}") from e
        log.info("Worker token persisted (OS keychain, service=%s)", self._service)

    def load(self) -> Optional[str]:
        try:
            value = self._keyring.get_password(self._service, self._username())
        except Exception as e:  # noqa: BLE001
            raise KeyringBackendError(f"keyring get_password failed: {type(e).__name__}") from e
        if value is not None and not isinstance(value, str):
            raise KeyringBackendError("keyring returned a non-string token")
        return value or None

    def clear(self) -> None:
        """Delete the entry; a missing entry is success (idempotent, like unlink)."""
        try:
            self._keyring.delete_password(self._service, self._username())
        except Exception as e:  # noqa: BLE001
            # Treat "entry not found" as success; everything else is a real failure.
            if _is_not_found(e):
                return
            raise KeyringBackendError(f"keyring delete_password failed: {type(e).__name__}") from e


def _is_not_found(exc: Exception) -> bool:
    """True if a delete failed only because the entry did not exist. keyring raises
    PasswordDeleteError for this on most backends; match by name so we don't hard-depend
    on the exception class being importable."""
    return type(exc).__name__ in ("PasswordDeleteError",) or "not found" in str(exc).lower()
