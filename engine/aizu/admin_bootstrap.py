"""Seed a platform (superadmin) admin — the one way an admin account is created.

There is deliberately NO self-serve admin signup route (the admin plane is the
sanctioned BOLA bypass; admins are provisioned out-of-band). This CLI mints one:

    AIZU_SECRET_KEY=... python -m aizu.admin_bootstrap \
        --db aizu.db --email ops@you.co

It generates a TOTP secret, stores it Fernet-encrypted, and prints the otpauth://
provisioning URI ONCE so you can enrol an authenticator app. The secret is never
persisted in plaintext and never logged. A password is read interactively (never
via argv, so it can't leak into shell history) unless piped on stdin.

The same CLI resets a forgotten password, because the admin plane has no
"forgot password" route by design (an emailed reset link would be a second,
unaudited way into the highest-privilege surface):

    AIZU_SECRET_KEY=... python -m aizu.admin_bootstrap \
        --db aizu.db --email ops@you.co --reset-password

That keeps the existing TOTP enrolment — the alternative, deleting the row and
re-minting, silently costs the operator their authenticator too — and revokes
every live admin session, so a cookie minted under the old password dies with it.
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


def _read_password(interactive: bool, *, prompt: str = "Admin password: ") -> str:
    if interactive:
        pw = getpass.getpass(prompt)
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


def _reset_password(args: argparse.Namespace, cipher: SecretCipher) -> int:
    """`--reset-password`: new password for an existing admin, TOTP kept, sessions cut."""
    email = args.email.strip().lower()
    store = Store(str(Path(args.db).resolve()), secret_cipher=cipher)
    try:
        admin = store.get_platform_admin_by_email(email)
        if admin is None:
            print(f"error: no platform admin with email {email} — run without "
                  "--reset-password to create one", file=sys.stderr)
            return 1
        admin_id = int(admin["id"])
        # Prove THIS shell's AIZU_SECRET_KEY is the one that minted the MFA secret,
        # BEFORE taking a new password. Reset under a mismatched key and login still
        # fails — as a 500 on TOTP decrypt, not a 401 — which reads as "the reset
        # didn't work" and sends the operator hunting the wrong thing.
        try:
            totp = store.get_admin_totp_secret(admin_id)
        except SecretCipherError as e:
            print(f"error: this AIZU_SECRET_KEY cannot decrypt {email}'s MFA secret "
                  f"({e}). Reset with the key the account was created under, or delete "
                  "the row and re-mint the admin.", file=sys.stderr)
            return 2
        if not totp:
            print(f"error: {email} has no usable TOTP secret — delete the row and "
                  "re-mint the admin.", file=sys.stderr)
            return 2

        password = _read_password(not args.non_interactive,
                                  prompt="New admin password: ")
        store.set_platform_admin_password(admin_id, hash_password(password))
        revoked = store.delete_admin_sessions_for_admin(admin_id)
        store.append_admin_audit(
            acting_admin_id=admin_id, action="admin.password.reset",
            target_resource="cli",
            reason="out-of-band reset via admin_bootstrap --reset-password")
    finally:
        store.close()

    print(f"\nPassword reset for platform admin #{admin_id}: {email}")
    print(f"Revoked {revoked} live admin session(s).")
    print("The TOTP enrolment is unchanged — keep using the same authenticator entry.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aizu.admin_bootstrap",
        description="Provision a platform (superadmin) admin with TOTP MFA.")
    parser.add_argument("--db", default="aizu.db", help="path to the SQLite DB")
    parser.add_argument("--email", required=True, help="admin login email")
    parser.add_argument("--issuer", default="AIZU Admin",
                        help="issuer label shown in the authenticator app")
    parser.add_argument("--non-interactive", action="store_true",
                        help="read the password from stdin instead of prompting")
    parser.add_argument("--reset-password", action="store_true",
                        help="set a new password for an EXISTING admin, keeping their "
                             "TOTP enrolment and revoking their live sessions")
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

    if args.reset_password:
        return _reset_password(args, cipher)

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
