"""Billing layer — subscriptions over the multi-tenant panel (Polar rail).

Aizu/AIZU (the vendor) has ONE Merchant-of-Record account at Polar.sh; each
customer org is a *customer* inside it. Polar credentials are therefore
PLATFORM-level env vars (not per-org `integration_secrets`). Per-org we persist
only the resulting subscription state (see `core/store.py:subscriptions`).

This module is the provider-agnostic seam:

  - `BillingProvider` is the Protocol the server depends on; `PolarClient` is the
    first concrete impl. A future local rail (PayTechUZ) is a new class + a
    registry entry — `server.py` never names `PolarClient`.
  - `parse_event` normalizes a provider payload into ONE `CanonicalBillingEvent`,
    so the webhook→`upsert_subscription` reconciliation is written once and never
    sees a Polar (or Payme/Click/Uzum) field name.
  - `verify_webhook` / `parse_event` are PER-PROVIDER methods (not shared free
    functions): rails differ in signing scheme.

Stdlib only (`urllib`, `hmac`, `hashlib`, `base64`) — no Polar SDK. JSON parsing
of provider payloads goes through a tolerant, never-throw boundary (the
llm-json-output rule applies to any text we did not construct).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Protocol, Union

from .core.logsetup import get_logger

logger = get_logger(__name__)

# --- HTTP / signing constants -------------------------------------------------

_HTTP_TIMEOUT_SECONDS = 20.0
# Polar's API sits behind Cloudflare, which returns HTTP 403 (error 1010,
# "browser signature blocked") to the default `Python-urllib/x.y` User-Agent.
# An explicit product-token UA is enough to pass; without it EVERY outbound API
# call (checkout/portal) fails. Verified against the sandbox 2026-07-01.
_USER_AGENT = "aizu-billing/1.0 (+https://polar.sh)"
# Standard-Webhooks recommends rejecting deliveries whose timestamp is too far
# from "now" to blunt replay. 5 minutes each way matches the spec's default.
_WEBHOOK_MAX_SKEW_SECONDS = 5 * 60
_WHSEC_PREFIX = "whsec_"

_POLAR_BASE_URLS = {
    "sandbox": "https://sandbox-api.polar.sh",
    "production": "https://api.polar.sh",
}

# Statuses that may RUN (subject to the lead cap). A closed-set ALLOW-list so a
# new/unknown Polar status fails safe (blocked), not open. Mirrored by the run
# gate in server.py.
RUNNABLE_STATUSES = frozenset({"active", "trialing"})


# --- Tier catalogue -----------------------------------------------------------

# tier → entitlement. `lead_cap` is the period allowance (rows in `matches`
# scoped by org_id within the billing period); it is INTERVAL-INDEPENDENT — only
# price/renewal differ by month vs year. There is NO separate "run allowance".
# `self_serve` tiers have a hosted Polar checkout; Free is the implicit default
# (no checkout, no Polar customer); Scale is sales-led (cap negotiated per deal
# and stored in subscriptions.lead_cap_override — the catalogue cap below is a
# fail-closed placeholder so an unprovisioned Scale row blocks rather than leaks).
TIERS: dict[str, dict[str, Any]] = {
    "free": {
        "display_name": "Free", "lead_cap": 10, "self_serve": False,
        "prices": {"month": 0.0, "year": 0.0},
    },
    "lite": {
        "display_name": "Lite", "lead_cap": 50, "self_serve": True,
        "prices": {"month": 9.99, "year": 99.0},
    },
    "starter": {
        "display_name": "Starter", "lead_cap": 250, "self_serve": True,
        "prices": {"month": 24.99, "year": 249.0},
    },
    "pro": {
        "display_name": "Pro", "lead_cap": 2000, "self_serve": True,
        "prices": {"month": 149.0, "year": 1490.0},
    },
    "scale": {
        "display_name": "Scale", "lead_cap": 0, "self_serve": False,
        "prices": {"month": None, "year": None},
    },
}

# Only these tiers may be passed to create_checkout (Scale is sales-led; Free has
# no checkout). The order is the display/upgrade order.
SELF_SERVE_TIERS: tuple[str, ...] = ("lite", "starter", "pro")
VALID_INTERVALS: frozenset[str] = frozenset({"month", "year"})


def tier_lead_cap(tier: str) -> int:
    """Catalogue cap for a tier (the per-row `lead_cap_override` wins when set —
    resolved in Store.get_subscription, not here). Unknown tier → Free cap."""
    return int(TIERS.get(tier, TIERS["free"])["lead_cap"])


# --- Errors -------------------------------------------------------------------

class BillingConfigError(RuntimeError):
    """Missing/invalid Polar env configuration — raised loudly at the boundary
    (mirrors SecretCipherError) so a misconfig fails fast at startup."""


class BillingError(RuntimeError):
    """A provider call failed (HTTP error, unexpected payload). Carries a
    user-safe message; the raw provider body is logged, never surfaced."""


# --- Canonical types ----------------------------------------------------------

@dataclass(frozen=True)
class CheckoutResult:
    url: str


@dataclass(frozen=True)
class PortalResult:
    """A portal session, or a typed "no billing account yet" for a Free org with
    no Polar customer (so the panel button degrades cleanly, never a 500)."""
    url: Optional[str]
    has_account: bool


@dataclass(frozen=True)
class CanonicalBillingEvent:
    """ONE internal shape every provider event normalizes to. The
    webhook→upsert reconciliation reads only this — never a raw Polar field."""
    org_id: Optional[int]
    provider: str
    tier: str
    interval: Optional[str]    # month|year — billing cadence (None if unknown)
    status: str
    cancel_at_period_end: bool
    current_period_start: Optional[float]
    current_period_end: Optional[float]
    provider_subscription_id: Optional[str]
    provider_customer_id: Optional[str]
    event_ts: float            # subscription.modified_at — monotonic ordering key
    event_id: str              # webhook-id — exact-redelivery dedup
    event_type: str            # raw provider event type (for logging/branching)


@dataclass(frozen=True)
class ParseError:
    """Never-throw boundary: parse_event returns this instead of raising."""
    error: str


# --- Provider protocol --------------------------------------------------------

class BillingProvider(Protocol):
    """The seam the server depends on. The server holds a PROVIDERS registry and
    resolves a provider per request; it never references a concrete class."""

    name: str

    def create_checkout(self, tier: str, interval: str, org_id: int,
                         email: str, success_url: str) -> CheckoutResult: ...

    def create_portal(self, org_id: int) -> PortalResult: ...

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> bool: ...

    def parse_event(self, raw_body: bytes,
                    headers: Optional[Mapping[str, str]] = None
                    ) -> Union[CanonicalBillingEvent, ParseError]: ...


# --- Polar configuration ------------------------------------------------------

@dataclass(frozen=True)
class PolarConfig:
    access_token: str
    webhook_secret: str
    server: str                                  # 'sandbox' | 'production'
    # tier → interval → product id, self-serve tiers only.
    products: dict[str, dict[str, str]]

    @property
    def base_url(self) -> str:
        return _POLAR_BASE_URLS[self.server]

    @classmethod
    def from_env(cls) -> "PolarConfig":
        """Read + validate at the boundary (fail-fast, clear errors). Models
        secrets.SecretCipher.from_env."""
        token = os.environ.get("POLAR_ACCESS_TOKEN", "").strip()
        if not token:
            raise BillingConfigError(
                "POLAR_ACCESS_TOKEN is not set — required to create checkouts "
                "and portal sessions.")
        secret = os.environ.get("POLAR_WEBHOOK_SECRET", "").strip()
        if not secret:
            raise BillingConfigError(
                "POLAR_WEBHOOK_SECRET is not set — required to verify webhooks.")
        server = os.environ.get("POLAR_SERVER", "sandbox").strip().lower()
        if server not in _POLAR_BASE_URLS:
            raise BillingConfigError(
                f"POLAR_SERVER must be 'sandbox' or 'production' (got {server!r}).")
        products = cls._parse_products(os.environ.get("POLAR_PRODUCTS", ""))
        return cls(access_token=token, webhook_secret=secret, server=server,
                   products=products)

    @staticmethod
    def _parse_products(raw: str) -> dict[str, dict[str, str]]:
        """Tolerant parse of the nested tier→interval→id map, then a strict
        shape check (every self-serve tier must carry BOTH intervals)."""
        if not raw.strip():
            raise BillingConfigError(
                "POLAR_PRODUCTS is not set — a JSON map of "
                '{"lite":{"month":"<id>","year":"<id>"}, ...} for lite/starter/pro.')
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as e:
            raise BillingConfigError(f"POLAR_PRODUCTS is not valid JSON: {e}") from e
        if not isinstance(parsed, dict):
            raise BillingConfigError("POLAR_PRODUCTS must be a JSON object.")
        out: dict[str, dict[str, str]] = {}
        for tier in SELF_SERVE_TIERS:
            tier_map = parsed.get(tier)
            if not isinstance(tier_map, dict):
                raise BillingConfigError(
                    f"POLAR_PRODUCTS is missing the {tier!r} tier (need month+year ids).")
            clean: dict[str, str] = {}
            for interval in ("month", "year"):
                pid = tier_map.get(interval)
                if not isinstance(pid, str) or not pid.strip():
                    raise BillingConfigError(
                        f"POLAR_PRODUCTS[{tier!r}][{interval!r}] must be a non-empty "
                        "product id string.")
                clean[interval] = pid.strip()
            out[tier] = clean
        return out


# --- Polar client -------------------------------------------------------------

class PolarClient:
    """Polar.sh BillingProvider impl. Standard-Webhooks signing; hosted checkout
    and customer portal via the REST API."""

    name = "polar"

    def __init__(self, config: PolarConfig):
        self._cfg = config
        # Candidate HMAC keys derived from the webhook secret. The exact
        # derivation Polar uses (raw UTF-8 bytes vs base64-decoded) must be PINNED
        # to a real sandbox vector before production; until then we accept a match
        # against ANY candidate. This does NOT weaken security: every candidate is
        # derived from the SAME secret, so an attacker still needs that secret.
        self._sig_keys = _signing_key_candidates(config.webhook_secret)

    @classmethod
    def from_env(cls) -> "PolarClient":
        return cls(PolarConfig.from_env())

    # ----- outbound API -----

    def create_checkout(self, tier: str, interval: str, org_id: int,
                        email: str, success_url: str) -> CheckoutResult:
        if tier not in self._cfg.products:
            raise BillingError(f"tier {tier!r} is not a self-serve checkout tier.")
        if interval not in VALID_INTERVALS:
            raise BillingError(f"interval {interval!r} must be 'month' or 'year'.")
        product_id = self._cfg.products[tier][interval]
        # external_customer_id is THE org link (NOT customer_external_id — that
        # 422s). Polar upserts the customer by it, so re-running checkout for the
        # same org reuses the same Polar customer (idempotent). metadata.orgId is
        # a webhook-resolution backstop only. customer_email is prefill.
        body = {
            "products": [product_id],
            "external_customer_id": str(org_id),
            "customer_email": email,
            "success_url": success_url,
            "metadata": {"orgId": str(org_id)},
        }
        data = self._request("POST", "/v1/checkouts/", body)
        url = data.get("url")
        if not isinstance(url, str) or not url:
            raise BillingError("Polar checkout response had no 'url'.")
        return CheckoutResult(url=url)

    def create_portal(self, org_id: int) -> PortalResult:
        body = {"external_customer_id": str(org_id)}
        try:
            data = self._request("POST", "/v1/customer-sessions/", body)
        except BillingError as e:
            # A Free org has no Polar customer yet → Polar 404/422. Degrade to a
            # typed "no account" result rather than a 500.
            if e.__cause__ is not None and isinstance(e.__cause__, urllib.error.HTTPError) \
                    and e.__cause__.code in (404, 422):
                return PortalResult(url=None, has_account=False)
            raise
        url = data.get("customer_portal_url")
        if not isinstance(url, str) or not url:
            raise BillingError("Polar customer-session response had no 'customer_portal_url'.")
        return PortalResult(url=url, has_account=True)

    def _request(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = self._cfg.base_url + path
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, method=method,
            headers={
                "Authorization": f"Bearer {self._cfg.access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": _USER_AGENT,   # avoid Cloudflare 1010 (see constant)
            })
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = _safe_read(e)
            logger.error("Polar %s %s failed: HTTP %s %s", method, path, e.code, detail)
            raise BillingError(f"Polar API error (HTTP {e.code}).") from e
        except urllib.error.URLError as e:
            logger.error("Polar %s %s failed: %s", method, path, e)
            raise BillingError("Could not reach the Polar API.") from e
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as e:
            raise BillingError("Polar API returned a non-JSON body.") from e
        if not isinstance(parsed, dict):
            raise BillingError("Polar API returned an unexpected (non-object) body.")
        return parsed

    # ----- webhook verification (Standard Webhooks) -----

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        """HMAC-SHA256 over `{id}.{timestamp}.{body}`, base64-encoded, verified on
        the RAW bytes before any JSON parse. The `webhook-signature` header may
        carry MULTIPLE space-separated `v1,<sig>` tokens (secret rotation) — accept
        if our computed signature matches ANY token. Never compare the whole header.
        """
        wid = _header(headers, "webhook-id")
        wts = _header(headers, "webhook-timestamp")
        wsig = _header(headers, "webhook-signature")
        if not (wid and wts and wsig):
            return False
        if not _timestamp_within_skew(wts):
            return False
        signed = wid.encode("utf-8") + b"." + wts.encode("utf-8") + b"." + raw_body
        expected = [_b64_hmac(key, signed) for key in self._sig_keys]
        for token in wsig.split(" "):
            token = token.strip()
            if not token:
                continue
            presented = token.split(",", 1)[1] if "," in token else token
            for exp in expected:
                if hmac.compare_digest(exp, presented):
                    return True
        return False

    # ----- webhook parsing (never-throw boundary) -----

    def parse_event(self, raw_body: bytes,
                    headers: Optional[Mapping[str, str]] = None
                    ) -> Union[CanonicalBillingEvent, ParseError]:
        """Normalize a Polar webhook into a CanonicalBillingEvent. Derives ALL
        subscription state from the `data` object on every `subscription.*` event
        (no per-type branching). Returns ParseError instead of raising."""
        try:
            envelope = json.loads(raw_body.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, ValueError) as e:
            return ParseError(f"webhook body is not valid JSON: {e}")
        if not isinstance(envelope, dict):
            return ParseError("webhook body is not a JSON object")
        event_type = str(envelope.get("type", ""))
        data = envelope.get("data")
        if not isinstance(data, dict):
            return ParseError(f"webhook {event_type!r} has no object 'data'")

        event_id = _header(headers or {}, "webhook-id") or str(data.get("id", ""))
        # modified_at is the state-effective ordering key (delivery time is not).
        event_ts = _iso_to_epoch(data.get("modified_at")) or time.time()

        return CanonicalBillingEvent(
            org_id=_resolve_org_id(data),
            provider=self.name,
            tier=self._tier_for_product(data),
            interval=_normalize_interval(data),
            status=str(data.get("status", "")),
            cancel_at_period_end=bool(data.get("cancel_at_period_end", False)),
            current_period_start=_iso_to_epoch(data.get("current_period_start")),
            current_period_end=_iso_to_epoch(data.get("current_period_end")),
            provider_subscription_id=_str_or_none(data.get("id")),
            provider_customer_id=_str_or_none(data.get("customer_id")),
            event_ts=event_ts,
            event_id=event_id,
            event_type=event_type,
        )

    def _tier_for_product(self, data: Mapping[str, Any]) -> str:
        """Reverse-map the subscription's product id to our tier. Unknown id →
        'free' (so a stray product can never grant a paid cap)."""
        product_id = data.get("product_id")
        if not isinstance(product_id, str):
            prod = data.get("product")
            product_id = prod.get("id") if isinstance(prod, dict) else None
        if not isinstance(product_id, str):
            return "free"
        for tier, intervals in self._cfg.products.items():
            if product_id in intervals.values():
                return tier
        return "free"


# --- module-level helpers -----------------------------------------------------

def _signing_key_candidates(secret: str) -> tuple[bytes, ...]:
    """Derive candidate HMAC keys from a `whsec_…` secret. We accept a match
    against ANY candidate — all derive from the SAME secret, so this does not
    weaken security (an attacker still needs the secret). Order:

    1. The FULL secret string, WITH the `whsec_` prefix, as UTF-8 bytes. This is
       what Polar's sandbox actually signs with — PINNED against a real delivery
       2026-07-01 (test_billing.py). Do not remove.
    2. The secret with the prefix stripped, as UTF-8 bytes.
    3. The base64-decode of the stripped body (Standard-Webhooks "classic"),
       padding-tolerant. Kept in case production Polar uses the classic scheme.
    """
    stripped = secret[len(_WHSEC_PREFIX):] if secret.startswith(_WHSEC_PREFIX) else secret
    candidates: list[bytes] = [secret.encode("utf-8"), stripped.encode("utf-8")]
    try:
        padded = stripped + "=" * (-len(stripped) % 4)   # b64decode needs padding
        decoded = base64.b64decode(padded)
        if decoded and decoded not in candidates:
            candidates.append(decoded)
    except (ValueError, binascii.Error):
        pass
    # De-dup while preserving order (stripped == full when no prefix).
    seen: set[bytes] = set()
    return tuple(c for c in candidates if not (c in seen or seen.add(c)))


def _b64_hmac(key: bytes, message: bytes) -> str:
    return base64.b64encode(hmac.new(key, message, hashlib.sha256).digest()).decode("ascii")


def _timestamp_within_skew(ts: str) -> bool:
    try:
        sent = int(ts)
    except (ValueError, TypeError):
        return False
    return abs(time.time() - sent) <= _WEBHOOK_MAX_SKEW_SECONDS


def _header(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header lookup (HTTP header keys are case-insensitive)."""
    lower = name.lower()
    for k, v in headers.items():
        if k.lower() == lower:
            return v or ""
    return ""


def _resolve_org_id(data: Mapping[str, Any]) -> Optional[int]:
    """Resolve our org from external_customer_id, falling back to metadata.orgId.
    Checks the customer sub-object too (Polar nests external_id there)."""
    candidates: list[Any] = [data.get("external_customer_id")]
    customer = data.get("customer")
    if isinstance(customer, dict):
        candidates.append(customer.get("external_id"))
        candidates.append(customer.get("external_customer_id"))
    meta = data.get("metadata")
    if isinstance(meta, dict):
        candidates.append(meta.get("orgId"))
    for c in candidates:
        if c is None:
            continue
        try:
            return int(c)
        except (ValueError, TypeError):
            continue
    return None


def _normalize_interval(data: Mapping[str, Any]) -> Optional[str]:
    """Pull the billing cadence ('month'|'year') from a Polar subscription. Polar
    exposes `recurring_interval` on the subscription (or nested under price).
    Anything else → None."""
    raw = data.get("recurring_interval")
    if raw is None:
        price = data.get("price")
        if isinstance(price, dict):
            raw = price.get("recurring_interval")
    if not isinstance(raw, str):
        return None
    norm = raw.strip().lower()
    return norm if norm in VALID_INTERVALS else None


def _iso_to_epoch(value: Any) -> Optional[float]:
    """Parse an ISO-8601 timestamp (Polar uses 'Z' or an offset) to epoch
    seconds. None / unparseable → None (never raises)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _str_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text or None


def _safe_read(err: urllib.error.HTTPError) -> str:
    try:
        return err.read().decode("utf-8", errors="replace")[:500]
    except Exception:  # noqa: BLE001 - diagnostics only
        return "<unreadable>"
