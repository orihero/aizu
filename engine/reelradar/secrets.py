"""Encryption boundary for per-(org, platform) integration secrets.

Secrets (a YouTube API key; a Telegram MTProto session + ids) are stored
encrypted at rest in the `integration_secrets` table — `integrations.detail`
is plaintext and must never hold a credential. We use Fernet (AES-128-CBC +
HMAC) keyed by the `REELRADAR_SECRET_KEY` env var.

Rules followed here (global secret-management rule):
  - the key is read from the environment, never hardcoded;
  - its absence is loud (SecretCipherError) — callers surface it, never crash;
  - plaintext is JSON and never logged.
"""
from __future__ import annotations

import json
import os
from typing import Any

SECRET_KEY_ENV = "REELRADAR_SECRET_KEY"


class SecretCipherError(RuntimeError):
    """Raised for a missing/invalid key or an undecryptable/tampered blob.

    Distinct type so callers can treat secret failures as a recoverable
    boundary (flip the integration to "needs reconnect") rather than a crash.
    """


class SecretCipher:
    """Fernet-backed encryptor for JSON-dict secrets."""

    def __init__(self, key: str | bytes):
        try:
            from cryptography.fernet import Fernet
        except Exception as e:  # pragma: no cover - dependency missing
            raise SecretCipherError(
                "cryptography is required for secret storage: pip install cryptography"
            ) from e
        key_bytes = key if isinstance(key, bytes) else key.encode()
        # Pre-validate the decoded length: a 44-char passphrase is valid base64 but
        # gives weak/garbage key material that Fernet may silently accept. Require
        # exactly 32 decoded bytes (security review HIGH #1).
        self._require_32_byte_key(key_bytes)
        try:
            self._fernet = Fernet(key_bytes)
        except Exception as e:
            raise SecretCipherError(
                f"invalid {SECRET_KEY_ENV} — expected a urlsafe-base64 32-byte Fernet "
                "key (generate one with SecretCipher.generate_key())"
            ) from e

    @staticmethod
    def _require_32_byte_key(key_bytes: bytes) -> None:
        import base64
        try:
            decoded = base64.urlsafe_b64decode(key_bytes)
        except Exception as e:
            raise SecretCipherError(
                f"invalid {SECRET_KEY_ENV} — not valid urlsafe-base64") from e
        if len(decoded) != 32:
            raise SecretCipherError(
                f"invalid {SECRET_KEY_ENV} — must decode to exactly 32 bytes "
                f"(got {len(decoded)}); generate one with SecretCipher.generate_key()")

    @classmethod
    def from_env(cls) -> "SecretCipher":
        key = os.environ.get(SECRET_KEY_ENV, "")
        if not key:
            raise SecretCipherError(
                f"{SECRET_KEY_ENV} is not set — required to read/write integration "
                "secrets. Generate one with `python -c \"from reelradar.secrets import "
                "SecretCipher; print(SecretCipher.generate_key())\"` and add it to .env."
            )
        return cls(key)

    @staticmethod
    def generate_key() -> str:
        from cryptography.fernet import Fernet
        return Fernet.generate_key().decode()

    def encrypt(self, data: dict[str, Any]) -> str:
        """Encrypt a JSON-serializable dict to an opaque token string."""
        plaintext = json.dumps(data, ensure_ascii=False).encode("utf-8")
        return self._fernet.encrypt(plaintext).decode("ascii")

    def decrypt(self, blob: str) -> dict[str, Any]:
        """Decrypt a token back to its dict. Raises SecretCipherError on any
        failure (wrong key, tampering, non-object payload) — never a bare crash."""
        from cryptography.fernet import InvalidToken
        try:
            plaintext = self._fernet.decrypt(blob.encode("ascii"))
        except InvalidToken as e:        # wrong key or tampered/corrupted blob
            raise SecretCipherError(
                "decryption failed: invalid or tampered token") from e
        except Exception as e:           # non-ascii / malformed input
            raise SecretCipherError("decryption failed: malformed blob") from e
        try:
            obj = json.loads(plaintext)
        except Exception as e:
            raise SecretCipherError("decrypted secret is not valid JSON") from e
        if not isinstance(obj, dict):
            raise SecretCipherError("decrypted secret is not a JSON object")
        return obj
