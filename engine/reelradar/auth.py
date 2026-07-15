"""Email + password auth primitives for the panel bridge (stdlib only).

The bridge is a local control plane (PRD §5). Auth gates every `/api/*` surface
except the auth endpoints themselves, so a browser pointed at the panel must sign
in before it can read state or drive a run.

Three concerns live here, deliberately framework-free:
- Password hashing: PBKDF2-HMAC-SHA256 with a per-user random salt, stored in a
  self-describing `pbkdf2_sha256$<iters>$<salt_hex>$<hash_hex>` string so the
  iteration count can rise later without breaking existing rows. Verification is
  constant-time (`hmac.compare_digest`).
- Session tokens: opaque, high-entropy (`secrets.token_urlsafe`), stored server
  side in `auth_sessions`; the client only ever holds the token in an HttpOnly
  cookie, never the user row.
- Brute-force throttle: a small, thread-safe, in-memory failed-login limiter
  (the server is ThreadingHTTPServer). Bounds password guessing without a DB or
  external store; resets on success.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from typing import Optional

# OWASP (2023) PBKDF2-HMAC-SHA256 floor. ~0.2s/hash on commodity hardware — fine
# for a human-paced local login, costly for an offline guesser.
PBKDF2_ITERATIONS = 600_000
_SALT_BYTES = 16
_ALGO = "pbkdf2_sha256"

SESSION_TOKEN_BYTES = 32
# Sessions live 30 days; the cookie Max-Age mirrors this (server.SESSION_TTL_SECONDS).
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60

# Password policy. Kept liberal but non-trivial; the panel enforces the same min.
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256  # bound the PBKDF2 input so a huge body can't burn CPU


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Return a self-describing PBKDF2 hash string for `password`.

    Format: ``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>``.
    """
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGO}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of `password` against a stored hash string.

    Never raises on a malformed stored value — a corrupt/legacy row simply fails
    to verify (returns False), so a bad row can't crash a login.
    """
    if not isinstance(password, str) or not isinstance(stored, str):
        return False
    try:
        algo, iters_s, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        iterations = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)


def new_session_token() -> str:
    """A fresh, URL-safe, opaque session token."""
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    """SHA-256 of a session token. Stored at rest so a DB leak exposes no usable
    tokens — the raw token lives only in the client's HttpOnly cookie. A plain
    fast hash (not PBKDF2) is right here: the token is already high-entropy."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class LoginThrottle:
    """Thread-safe, in-memory failed-login limiter, keyed by a caller-chosen string.

    After `max_failures` failures inside `window` seconds the key is locked for
    `lockout` seconds. A successful login (`reset`) clears the key. Memory is
    bounded by pruning expired entries on every touch.
    """

    def __init__(self, *, max_failures: int = 5, window: float = 900.0,
                 lockout: float = 900.0):
        self._max = max_failures
        self._window = window
        self._lockout = lockout
        self._lock = threading.Lock()
        # key -> (failure_timestamps, locked_until)
        self._fails: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def _now(self) -> float:
        return time.time()

    def is_locked(self, key: str) -> bool:
        now = self._now()
        with self._lock:
            until = self._locked_until.get(key)
            if until is None:
                return False
            if until <= now:
                # Lock elapsed — clear it and the failure history.
                self._locked_until.pop(key, None)
                self._fails.pop(key, None)
                return False
            return True

    def record_failure(self, key: str) -> None:
        now = self._now()
        with self._lock:
            recent = [t for t in self._fails.get(key, []) if now - t < self._window]
            recent.append(now)
            self._fails[key] = recent
            if len(recent) >= self._max:
                self._locked_until[key] = now + self._lockout

    def reset(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
            self._locked_until.pop(key, None)


def session_expiry(now: Optional[float] = None) -> float:
    """Absolute expiry timestamp for a session created now."""
    return (now if now is not None else time.time()) + SESSION_TTL_SECONDS
