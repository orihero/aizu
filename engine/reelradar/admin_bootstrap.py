"""Seed a platform (superadmin) admin — the one way an admin account is created.

There is deliberately NO self-serve admin signup route (the admin plane is the
sanctioned BOLA bypass; admins are provisioned out-of-band). This CLI mints one:

    REELRADAR_SECRET_KEY=... python -m reelradar.admin_bootstrap \
        --db reelradar.db --email ops@you.co

It generates a TOTP secret, stores it Fernet-encrypted, and prints the otpauth://
provisioning URI ONCE so you can enrol an authenticator app. The secret is never
persisted in plaintext and never logged. A password is read interactively (never
via argv, so it can't leak into shell history) unless piped on stdin.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from . import admin_auth
from .auth import MIN_PASSWORD_LENGTH, hash_password
from .core.store import Store
from .secrets import SecretCipher, SecretCipherError


def _read_password(interactive: bool) -> str:
    if interactive:
        pw = getpass.getpass("Admin password: ")
        again = getpass.getpass("Confirm password: ")
        if pw != again:
            print("error: passwords do not match", file=sys.stderr)
            raise SystemExit(2)
    else:  # piped (e.g. CI): first line of stdin
        pw = sys.stdin.readline().rstrip("\n")
    if len(pw) < MIN_PASSWORD_LENGTH:
        print(f"error: password must be at least {MIN_PASSWORD_LENGTH} characters",
              file=sys.stderr)
        raise SystemExit(2)
    return pw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reelradar.admin_bootstrap",
        description="Provision a platform (superadmin) admin with TOTP MFA.")
    parser.add_argument("--db", default="reelradar.db", help="path to the SQLite DB")
    parser.add_argument("--email", required=True, help="admin login email")
    parser.add_argument("--issuer", default="AIZU Admin",
                        help="issuer label shown in the authenticator app")
    parser.add_argument("--non-interactive", action="store_true",
                        help="read the password from stdin instead of prompting")
    args = parser.parse_args(argv)

    email = args.email.strip().lower()
    if "@" not in email:
        print("error: invalid email", file=sys.stderr)
        return 2

    # Encrypt the TOTP secret with the same key the app uses for all secrets. Fail loudly
    # if it's missing — an admin with an undecryptable MFA secret could never log in.
    try:
        cipher = SecretCipher.from_env()
    except SecretCipherError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    password = _read_password(not args.non_interactive)

    store = Store(str(Path(args.db).resolve()), secret_cipher=cipher)
    try:
        if store.get_platform_admin_by_email(email) is not None:
            print(f"error: a platform admin with email {email} already exists",
                  file=sys.stderr)
            return 1
        totp_secret = admin_auth.generate_totp_secret()
        mfa_blob = cipher.encrypt({"totp": totp_secret})
        admin_id = store.create_platform_admin(
            email=email, password_hash=hash_password(password), mfa_secret=mfa_blob)
    finally:
        store.close()

    uri = admin_auth.provisioning_uri(totp_secret, email=email, issuer=args.issuer)
    print(f"\nCreated platform admin #{admin_id}: {email}")
    print("\nScan this in your authenticator app (shown ONCE):")
    print(f"  {uri}")
    print(f"\nManual entry secret: {totp_secret}")
    print("\nStore nothing from this output; the secret is not recoverable later.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
