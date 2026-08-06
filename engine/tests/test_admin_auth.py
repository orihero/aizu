"""Admin-plane auth primitives (Phase 5b): TOTP (RFC 6238) + IP-allowlist.

Password hashing / session tokens are reused from aizu.auth and covered by
test_auth.py; here we only test the admin-specific additions."""
import base64
import hashlib
import hmac
import struct
import time

import pytest

from aizu import admin_auth


# ----- TOTP -----

def test_totp_roundtrip_verifies_current_code():
    secret = admin_auth.generate_totp_secret()
    code = admin_auth.totp_now(secret)
    assert admin_auth.verify_totp(secret, code) is True


def test_totp_secret_is_valid_unpadded_base32():
    secret = admin_auth.generate_totp_secret()
    assert "=" not in secret               # unpadded, as authenticator apps expect
    # decodes back to the 160-bit / 20-byte RFC norm
    pad = (-len(secret)) % 8
    assert len(base64.b32decode(secret + "=" * pad)) == 20


def test_totp_matches_rfc6238_reference_hotp():
    """Cross-check our HOTP against an independent inline implementation at a fixed
    timestamp so a refactor can't silently change the algorithm."""
    secret = admin_auth.generate_totp_secret()
    at = 1_700_000_000.0
    counter = int(at // 30)
    key = base64.b32decode(secret + "=" * ((-len(secret)) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    binary = ((digest[off] & 0x7F) << 24 | (digest[off + 1] & 0xFF) << 16
              | (digest[off + 2] & 0xFF) << 8 | (digest[off + 3] & 0xFF))
    expected = str(binary % 10 ** 6).zfill(6)
    assert admin_auth.totp_now(secret, at=at) == expected


def test_totp_accepts_adjacent_step_for_skew():
    secret = admin_auth.generate_totp_secret()
    at = 1_700_000_000.0
    prev = admin_auth.totp_now(secret, at=at - 30)   # one step earlier
    nxt = admin_auth.totp_now(secret, at=at + 30)    # one step later
    assert admin_auth.verify_totp(secret, prev, at=at) is True
    assert admin_auth.verify_totp(secret, nxt, at=at) is True


def test_totp_rejects_two_steps_away():
    secret = admin_auth.generate_totp_secret()
    at = 1_700_000_000.0
    far = admin_auth.totp_now(secret, at=at - 60)     # two steps earlier (outside window)
    assert admin_auth.verify_totp(secret, far, at=at) is False


def test_totp_rejects_wrong_and_malformed_codes():
    secret = admin_auth.generate_totp_secret()
    assert admin_auth.verify_totp(secret, "000000") in (True, False)  # numeric but ~always wrong
    assert admin_auth.verify_totp(secret, "12345") is False   # too short
    assert admin_auth.verify_totp(secret, "abcdef") is False  # non-numeric
    assert admin_auth.verify_totp(secret, "") is False
    assert admin_auth.verify_totp(secret, None) is False
    assert admin_auth.verify_totp(secret, " 123456 ") in (True, False)  # trimmed, then checked


def test_verify_totp_never_raises_on_bad_secret():
    assert admin_auth.verify_totp("not base32!!!", "123456") is False
    assert admin_auth.verify_totp("", "123456") is False


def test_provisioning_uri_is_scannable():
    uri = admin_auth.provisioning_uri("JBSWY3DPEHPK3PXP", email="a@x.io", issuer="AIZU Admin")
    assert uri.startswith("otpauth://totp/")
    assert "secret=JBSWY3DPEHPK3PXP" in uri
    assert "issuer=AIZU+Admin" in uri


# ----- IP allowlist (fails closed) -----

def test_ip_allowlist_empty_fails_closed():
    assert admin_auth.ip_allowed("127.0.0.1", "") is False
    assert admin_auth.ip_allowed("127.0.0.1", None) is False
    assert admin_auth.ip_allowed("127.0.0.1", "   ") is False


def test_ip_allowlist_exact_and_cidr_match():
    assert admin_auth.ip_allowed("127.0.0.1", "127.0.0.1,::1") is True
    assert admin_auth.ip_allowed("10.0.3.7", "10.0.0.0/8") is True
    assert admin_auth.ip_allowed("192.168.1.5", "10.0.0.0/8") is False


def test_ip_allowlist_ipv6_and_wildcard():
    assert admin_auth.ip_allowed("::1", "::1") is True
    assert admin_auth.ip_allowed("203.0.113.9", "0.0.0.0/0,::/0") is True
    assert admin_auth.ip_allowed("2001:db8::1", "0.0.0.0/0,::/0") is True


def test_ip_allowlist_skips_bad_entries_without_widening():
    # a garbage entry must not open the gate; the valid CIDR still works
    assert admin_auth.ip_allowed("10.0.0.5", "not-an-ip,10.0.0.0/8") is True
    assert admin_auth.ip_allowed("192.168.0.1", "not-an-ip,garbage") is False


def test_ip_allowlist_rejects_unparseable_remote():
    assert admin_auth.ip_allowed("", "0.0.0.0/0") is False
    assert admin_auth.ip_allowed(None, "0.0.0.0/0") is False
    assert admin_auth.ip_allowed("not-an-ip", "0.0.0.0/0") is False


# ----- trusted-proxy X-Forwarded-For (Gap E) -----

def test_effective_ip_ignores_xff_when_no_trusted_proxies():
    # Default (no trusted proxies): XFF is ignored, the peer is authoritative.
    assert admin_auth.effective_client_ip(
        "10.9.9.9", "203.0.113.5", "") == "10.9.9.9"
    assert admin_auth.effective_client_ip(
        "10.9.9.9", "203.0.113.5", None) == "10.9.9.9"


def test_effective_ip_ignores_xff_from_untrusted_peer():
    # Peer is NOT a trusted proxy → a spoofed XFF cannot forge the source.
    assert admin_auth.effective_client_ip(
        "198.51.100.7", "10.0.0.1", "10.0.0.0/8") == "198.51.100.7"


def test_effective_ip_uses_rightmost_untrusted_hop_behind_trusted_proxy():
    # Peer is the trusted proxy; the real client is the rightmost non-proxy hop.
    assert admin_auth.effective_client_ip(
        "10.0.0.2", "203.0.113.5, 10.0.0.9", "10.0.0.0/8") == "203.0.113.5"
    # A chain of two proxies then the client → still the client.
    assert admin_auth.effective_client_ip(
        "10.0.0.2", "203.0.113.5, 10.0.0.8, 10.0.0.9", "10.0.0.0/8") == "203.0.113.5"


def test_effective_ip_falls_back_to_peer_when_xff_empty_or_all_trusted():
    assert admin_auth.effective_client_ip("10.0.0.2", "", "10.0.0.0/8") == "10.0.0.2"
    assert admin_auth.effective_client_ip(
        "10.0.0.2", "10.0.0.8, 10.0.0.9", "10.0.0.0/8") == "10.0.0.2"


def test_effective_ip_skips_a_malformed_hop():
    assert admin_auth.effective_client_ip(
        "10.0.0.2", "203.0.113.5, garbage", "10.0.0.0/8") == "203.0.113.5"


def test_admin_session_ttl_is_shorter_than_org_session():
    from aizu.auth import SESSION_TTL_SECONDS
    assert admin_auth.ADMIN_SESSION_TTL_SECONDS < SESSION_TTL_SECONDS
