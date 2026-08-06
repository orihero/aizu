"""Superadmin (platform-admin) plane auth primitives — stdlib only.

The platform-admin plane is a SEPARATE, higher-privilege auth surface from the org
`/api/*` cookie plane (PRD §10). It is the one sanctioned, audited BOLA bypass, so it
carries two controls the org plane does not:

- **TOTP MFA** (RFC 6238, SHA-1, 6 digits, 30s step) — a second factor on every admin
  login. The shared secret is generated at admin creation, shown once as an otpauth://
  provisioning URI, and stored Fernet-encrypted at rest (aizu.secrets).
- **IP-allowlist** — the admin plane is reachable only from operator networks. The
  allowlist is env-driven and **fails closed**: an empty/unset allowlist admits no one.

Password hashing, opaque session tokens, and hash-at-rest are REUSED from
`aizu.auth` — the admin plane must not fork crypto primitives. Only the
admin-specific pieces (TOTP, IP-allowlist, admin session TTL) live here.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets as _secrets
import struct
import time
from typing import Optional
from urllib.parse import quote, urlencode

# Admin sessions are SHORTER-lived than the 30-day human org session: a superadmin
# holds a sanctioned cross-org bypass, so a stolen cookie must expire fast.
ADMIN_SESSION_TTL_SECONDS = 12 * 60 * 60  # 12h
ADMIN_SESSION_COOKIE = "rr_admin_session"

# Comma-separated IPs / CIDRs (v4 or v6). EMPTY/UNSET ⇒ FAIL CLOSED (no admin access).
# To admit any host (dev only) set explicitly to "0.0.0.0/0,::/0".
ADMIN_IP_ALLOWLIST_ENV = "AIZU_ADMIN_IP_ALLOWLIST"

# Comma-separated IPs / CIDRs of REVERSE PROXIES we trust to set X-Forwarded-For.
# UNSET/EMPTY ⇒ XFF is ignored entirely and the transport peer is authoritative (the
# safe default: XFF is client-spoofable, so it is trusted ONLY when the peer is a
# configured proxy). Set this when the admin plane runs behind a known reverse proxy.
ADMIN_TRUSTED_PROXIES_ENV = "AIZU_TRUSTED_PROXIES"

# ----- TOTP (RFC 6238) -----
_TOTP_STEP_SECONDS = 30
_TOTP_DIGITS = 6
# Accept the code from the adjacent steps too, to tolerate clock skew / a code typed
# just as it rolls over. ±1 step = a ~90s acceptance window.
_TOTP_WINDOW = 1
_TOTP_SECRET_BYTES = 20  # 160-bit, the RFC 6238 / authenticator-app norm


def generate_totp_secret() -> str:
    """A fresh base32 TOTP shared secret (no padding), ready for an authenticator app."""
    return base64.b32encode(_secrets.token_bytes(_TOTP_SECRET_BYTES)).decode("ascii").rstrip("=")


def _b32_decode(secret: str) -> bytes:
    """Decode a possibly-unpadded, mixed-case base32 secret. Raises ValueError on junk."""
    cleaned = (secret or "").strip().replace(" ", "").upper()
    if not cleaned:
        raise ValueError("empty TOTP secret")
    pad = (-len(cleaned)) % 8
    return base64.b32decode(cleaned + ("=" * pad))


def _hotp(secret: str, counter: int, digits: int = _TOTP_DIGITS) -> str:
    key = _b32_decode(secret)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = ((digest[offset] & 0x7F) << 24
              | (digest[offset + 1] & 0xFF) << 16
              | (digest[offset + 2] & 0xFF) << 8
              | (digest[offset + 3] & 0xFF))
    return str(binary % (10 ** digits)).zfill(digits)


def totp_now(secret: str, *, at: Optional[float] = None) -> str:
    """The current TOTP code for `secret` (used by the bootstrap tool / tests)."""
    counter = int((at if at is not None else time.time()) // _TOTP_STEP_SECONDS)
    return _hotp(secret, counter)


def matched_totp_counter(secret: str, code: Optional[str], *,
                         at: Optional[float] = None) -> Optional[int]:
    """The TOTP step counter `code` matches for `secret` across the ±window, else None.

    Returned so the caller can record the consumed counter and reject a REPLAY within
    the acceptance window (a valid code must authenticate at most once). Constant-time:
    every candidate is compared, no short-circuit. Never raises — a malformed secret or
    non-numeric code simply yields None.
    """
    if not isinstance(code, str):
        return None
    code = code.strip()
    if len(code) != _TOTP_DIGITS or not code.isdigit():
        return None
    now = at if at is not None else time.time()
    base = int(now // _TOTP_STEP_SECONDS)
    try:
        matched: Optional[int] = None
        for delta in range(-_TOTP_WINDOW, _TOTP_WINDOW + 1):
            counter = base + delta
            if hmac.compare_digest(_hotp(secret, counter), code):
                matched = counter  # keep scanning (constant time), remember the match
        return matched
    except (ValueError, TypeError):
        return None


def verify_totp(secret: str, code: Optional[str], *, at: Optional[float] = None) -> bool:
    """Constant-time verify `code` against `secret` across the ±window steps. Never
    raises. For replay protection prefer matched_totp_counter + a used-counter record."""
    return matched_totp_counter(secret, code, at=at) is not None


def provisioning_uri(secret: str, *, email: str, issuer: str = "AIZU Admin") -> str:
    """The otpauth:// URI an authenticator app scans to enrol `secret`."""
    label = quote(f"{issuer}:{email}")
    params = urlencode({"secret": secret, "issuer": issuer,
                        "algorithm": "SHA1", "digits": _TOTP_DIGITS,
                        "period": _TOTP_STEP_SECONDS})
    return f"otpauth://totp/{label}?{params}"


# ----- IP allowlist -----
def ip_allowed(remote_ip: Optional[str], allowlist_raw: Optional[str]) -> bool:
    """True iff `remote_ip` falls inside any allowlisted IP/CIDR. FAILS CLOSED: an
    empty/unset allowlist, an unparseable remote IP, or all-malformed entries → False.
    Malformed individual entries are skipped (a typo can't silently widen access)."""
    entries = [e.strip() for e in (allowlist_raw or "").split(",") if e.strip()]
    if not entries:
        return False  # fail closed
    try:
        addr = ipaddress.ip_address((remote_ip or "").strip())
    except ValueError:
        return False
    for entry in entries:
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue  # skip a bad entry; never widen access on a typo
    return False


def effective_client_ip(peer_ip: Optional[str], xff_header: Optional[str],
                        trusted_proxies_raw: Optional[str]) -> str:
    """Resolve the real client IP for the allowlist, honouring X-Forwarded-For ONLY
    behind a configured trusted proxy.

    - No trusted proxies configured ⇒ return the transport `peer_ip` (XFF ignored). This
      is the safe default: XFF is client-spoofable, so an attacker could otherwise forge
      an allowlisted source.
    - Peer is NOT a trusted proxy ⇒ also return `peer_ip` (never trust XFF from a
      non-proxy).
    - Peer IS a trusted proxy ⇒ walk the XFF chain right-to-left, skip any hop that is
      itself a trusted proxy, and return the first (rightmost) UNTRUSTED hop — the client
      as seen by our trusted edge. If every hop is trusted / the header is empty, fall
      back to the peer.

    Never raises; a malformed hop is skipped. Mirrors nginx realip / Django trusted-proxy
    semantics without a new dependency (reuses `ip_allowed` for CIDR membership)."""
    peer = (peer_ip or "").strip()
    trusted_entries = [e.strip() for e in (trusted_proxies_raw or "").split(",")
                       if e.strip()]
    if not trusted_entries or not ip_allowed(peer, trusted_proxies_raw):
        return peer
    hops = [h.strip() for h in (xff_header or "").split(",") if h.strip()]
    for hop in reversed(hops):
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            continue  # skip a malformed hop rather than trust or crash
        if not ip_allowed(hop, trusted_proxies_raw):
            return hop  # the rightmost non-proxy hop = the real client
    return peer  # all hops trusted (or none) → the proxy itself is the closest we have


def admin_session_expiry(now: Optional[float] = None) -> float:
    return (now if now is not None else time.time()) + ADMIN_SESSION_TTL_SECONDS
