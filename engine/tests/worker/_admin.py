"""Shared helper for worker-plane server tests to authenticate as a platform admin.

Phase 5d moved the fleet / enqueue / control-flags / worker-revoke routes off the
interim REELRADAR_PLATFORM_ADMINS env allowlist onto the real admin plane (IP-allowlist
+ TOTP + admin session). These helpers seed an admin and mint an rr_admin_session cookie
so those tests keep exercising the routes over the wire."""
from __future__ import annotations

import json
import urllib.request

from reelradar import admin_auth
from reelradar.admin_auth import ADMIN_IP_ALLOWLIST_ENV
from reelradar.auth import hash_password
from reelradar.core.store import Store
from reelradar.secrets import SECRET_KEY_ENV, SecretCipher

# One valid Fernet key shared across the worker-plane test modules (test-only).
ADMIN_KEY = SecretCipher.generate_key()
ADMIN_PW = "adminpass123"


def set_admin_env(monkeypatch) -> None:
    """Allow loopback + provide the MFA-secret decryption key. Call from an autouse env
    fixture in each module so every request sees the admin plane configured."""
    monkeypatch.setenv(ADMIN_IP_ALLOWLIST_ENV, "127.0.0.1,::1")
    monkeypatch.setenv(SECRET_KEY_ENV, ADMIN_KEY)


def seed_admin(db_path: str, email: str) -> str:
    """Create a platform admin directly in the DB; return its TOTP secret."""
    cipher = SecretCipher(ADMIN_KEY)
    store = Store(db_path, secret_cipher=cipher)
    try:
        secret = admin_auth.generate_totp_secret()
        store.create_platform_admin(email=email.lower(),
                                    password_hash=hash_password(ADMIN_PW),
                                    mfa_secret=cipher.encrypt({"totp": secret}))
        return secret
    finally:
        store.close()


def admin_cookie(base: str, db_path: str, email: str = "fleet-admin@x.io") -> str:
    """Seed an admin (if needed) and return a live rr_admin_session cookie string."""
    secret = seed_admin(db_path, email)
    body = json.dumps({"email": email, "password": ADMIN_PW,
                       "totpCode": admin_auth.totp_now(secret)}).encode()
    req = urllib.request.Request(base + "/api/admin/login", data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        return resp.headers.get("Set-Cookie").split(";", 1)[0]
