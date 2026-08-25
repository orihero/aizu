"""Phase 1 — billing core (provider seam + PolarClient + v13 subscriptions).

UNIT coverage for billing.py (signature verify incl. rotation header, tolerant
parse, tier→cap map, config fail-fast) and the new Store methods (FREE default,
effective cap, monotonic upsert, period anchor, lead metering). The signed-route
integration tests live in test_multitenancy_server.py.

Webhook signing is the #1 silent-failure risk; the verify tests below are
self-consistent (we sign, then verify) — the REAL Polar sandbox vector must be
pinned here before production cutover (see docs/integrations/billing-polar.md §6).
"""
import base64
import json
import os
import tempfile
import time

import pytest

from aizu import billing
from aizu.billing import (
    CanonicalBillingEvent, ParseError, PolarClient, PolarConfig,
    BillingConfigError, tier_lead_cap, tier_campaign_cap, tier_max_run_leads,
)
from aizu.core.store import SCHEMA_VERSION, Store


# ----- fixtures / helpers -----

def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def _config() -> PolarConfig:
    # A whsec_-prefixed secret whose body is base64 (exercises both key candidates).
    secret = "whsec_" + base64.b64encode(b"polar-sandbox-signing-secret-001").decode()
    return PolarConfig(
        access_token="polar_at_test", webhook_secret=secret, server="sandbox",
        products={
            "lite":    {"month": "p_lite_m",    "year": "p_lite_y"},
            "starter": {"month": "p_starter_m", "year": "p_starter_y"},
            "pro":     {"month": "p_pro_m",      "year": "p_pro_y"},
        })


def _client() -> PolarClient:
    return PolarClient(_config())


def _signed_headers(client: PolarClient, body: bytes, *, ts: int | None = None,
                    rotation: bool = False) -> dict[str, str]:
    wid = "msg_test_1"
    wts = str(ts if ts is not None else int(time.time()))
    signed = wid.encode() + b"." + wts.encode() + b"." + body
    sig = billing._b64_hmac(client._sig_keys[0], signed)
    header = f"v1,bogus v1,{sig}" if rotation else f"v1,{sig}"
    return {"webhook-id": wid, "webhook-timestamp": wts, "webhook-signature": header}


def _sub_event_body(**overrides) -> bytes:
    data = {
        "id": "sub_abc", "status": "active", "product_id": "p_starter_m",
        "external_customer_id": "42", "customer_id": "cus_xyz",
        "current_period_start": "2026-06-01T00:00:00Z",
        "current_period_end": "2026-07-01T00:00:00Z",
        "modified_at": "2026-06-15T12:00:00Z", "cancel_at_period_end": False,
    }
    data.update(overrides)
    return json.dumps({"type": "subscription.updated", "data": data}).encode()


# ===== TIERS catalogue =====

def test_tier_caps_are_interval_independent_and_correct():
    assert tier_lead_cap("free") == 10
    assert tier_lead_cap("lite") == 50
    assert tier_lead_cap("starter") == 250
    assert tier_lead_cap("pro") == 2000
    # Unknown tier falls back to the Free cap (never a paid grant).
    assert tier_lead_cap("nonsense") == 10


def test_tiers_have_month_and_year_prices():
    for tier in ("lite", "starter", "pro"):
        prices = billing.TIERS[tier]["prices"]
        assert prices["month"] is not None and prices["year"] is not None
    # No tier carries a run-allowance field (deleted by design).
    assert all("run_allowance" not in t for t in billing.TIERS.values())


def test_every_tier_declares_a_campaign_cap():
    # The key must exist on EVERY tier: `tier_campaign_cap` subscripts it
    # directly, so a tier missing it would KeyError at the campaign-create gate
    # rather than fail closed.
    assert all("campaign_cap" in t for t in billing.TIERS.values())


def test_campaign_caps_match_the_published_plans():
    assert tier_campaign_cap("free") == 1
    assert tier_campaign_cap("lite") == 3
    # None = unlimited, NOT "unset". Assert `is None` so a future 0 (which would
    # block every campaign) can never pass this test.
    assert tier_campaign_cap("starter") is None
    assert tier_campaign_cap("pro") is None
    assert tier_campaign_cap("scale") is None


def test_unknown_tier_gets_the_free_campaign_cap():
    # Fail closed, mirroring tier_lead_cap: a garbled/stale tier string must
    # never read as unlimited.
    assert tier_campaign_cap("nonsense") == 1
    assert tier_campaign_cap("") == 1


def test_max_run_leads_is_the_period_lead_cap():
    # There is no separate per-run allowance: one run may target the whole
    # period's worth, and the run gate clamps again by what is left.
    for tier in billing.TIERS:
        assert tier_max_run_leads(tier) == tier_lead_cap(tier)
    assert tier_max_run_leads("free") == 10
    assert tier_max_run_leads("pro") == 2000


def test_unknown_tier_gets_the_free_max_run_leads():
    assert tier_max_run_leads("nonsense") == 10


# ===== PolarConfig.from_env — fail-fast boundary =====

def test_config_requires_access_token(monkeypatch):
    monkeypatch.delenv("POLAR_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("POLAR_WEBHOOK_SECRET", "whsec_x")
    with pytest.raises(BillingConfigError, match="POLAR_ACCESS_TOKEN"):
        PolarConfig.from_env()


def test_config_rejects_products_missing_an_interval(monkeypatch):
    monkeypatch.setenv("POLAR_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("POLAR_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setenv("POLAR_SERVER", "sandbox")
    # starter is missing its 'year' price → fail-fast.
    monkeypatch.setenv("POLAR_PRODUCTS", json.dumps({
        "lite": {"month": "a", "year": "b"},
        "starter": {"month": "c"},
        "pro": {"month": "e", "year": "f"},
    }))
    with pytest.raises(BillingConfigError, match="starter"):
        PolarConfig.from_env()


def test_config_rejects_malformed_products_json(monkeypatch):
    monkeypatch.setenv("POLAR_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("POLAR_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setenv("POLAR_PRODUCTS", "{not json")
    with pytest.raises(BillingConfigError, match="valid JSON"):
        PolarConfig.from_env()


def test_config_from_env_happy_path(monkeypatch):
    monkeypatch.setenv("POLAR_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("POLAR_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setenv("POLAR_SERVER", "production")
    monkeypatch.setenv("POLAR_PRODUCTS", json.dumps({
        "lite": {"month": "a", "year": "b"},
        "starter": {"month": "c", "year": "d"},
        "pro": {"month": "e", "year": "f"},
    }))
    cfg = PolarConfig.from_env()
    assert cfg.base_url == "https://api.polar.sh"
    assert cfg.products["pro"]["year"] == "f"


# ===== webhook signature verification =====

def test_verify_accepts_a_valid_signature():
    c = _client()
    body = _sub_event_body()
    assert c.verify_webhook(body, _signed_headers(c, body)) is True


def test_verify_accepts_rotation_multi_signature_header():
    # The header carries two space-separated tokens; only the 2nd is ours.
    c = _client()
    body = _sub_event_body()
    assert c.verify_webhook(body, _signed_headers(c, body, rotation=True)) is True


def test_verify_accepts_polar_full_prefixed_secret_key():
    """REGRESSION (pinned against a real Polar sandbox delivery, 2026-07-01):
    Polar signs with the HMAC key = the FULL secret string INCLUDING the `whsec_`
    prefix, as raw UTF-8 bytes — NOT the prefix-stripped body, NOT its base64
    decode. The prior derivation omitted this key, so every legitimate webhook
    was rejected (401) and no subscription ever activated. Sign here exactly as
    Polar does and assert verification accepts it."""
    c = _client()
    body = _sub_event_body()
    wid, wts = "msg_polar_1", str(int(time.time()))
    signed = wid.encode() + b"." + wts.encode() + b"." + body
    key = c._cfg.webhook_secret.encode("utf-8")   # full string, whsec_ included
    sig = billing._b64_hmac(key, signed)
    headers = {"webhook-id": wid, "webhook-timestamp": wts,
               "webhook-signature": f"v1,{sig}"}
    assert c.verify_webhook(body, headers) is True
    # And guard the candidate set directly: the full-prefixed key must be present.
    assert key in billing._signing_key_candidates(c._cfg.webhook_secret)


def test_verify_rejects_a_bad_signature():
    c = _client()
    body = _sub_event_body()
    headers = _signed_headers(c, body)
    headers["webhook-signature"] = "v1,not-the-real-signature"
    assert c.verify_webhook(body, headers) is False


def test_verify_rejects_a_tampered_body():
    c = _client()
    body = _sub_event_body()
    headers = _signed_headers(c, body)
    assert c.verify_webhook(body + b" ", headers) is False


def test_verify_rejects_a_stale_timestamp():
    c = _client()
    body = _sub_event_body()
    # Sign with a long-ago timestamp → outside the skew window.
    assert c.verify_webhook(body, _signed_headers(c, body, ts=1000)) is False


def test_verify_rejects_missing_headers():
    c = _client()
    body = _sub_event_body()
    assert c.verify_webhook(body, {}) is False


# ===== webhook parsing (never-throw boundary) =====

def test_parse_event_normalizes_a_subscription():
    c = _client()
    body = _sub_event_body()
    ev = c.parse_event(body, {"webhook-id": "msg_9"})
    assert isinstance(ev, CanonicalBillingEvent)
    assert ev.org_id == 42
    assert ev.provider == "polar"
    assert ev.tier == "starter"           # mapped from p_starter_m
    assert ev.status == "active"
    assert ev.provider_subscription_id == "sub_abc"
    assert ev.provider_customer_id == "cus_xyz"
    assert ev.event_id == "msg_9"         # from the webhook-id header
    assert ev.current_period_start is not None
    assert ev.event_ts > 0


def test_parse_event_unknown_product_maps_to_free():
    c = _client()
    ev = c.parse_event(_sub_event_body(product_id="p_unknown"))
    assert isinstance(ev, CanonicalBillingEvent)
    assert ev.tier == "free"


def test_parse_event_resolves_org_from_metadata_fallback():
    c = _client()
    data = {"id": "s", "status": "active", "product_id": "p_pro_y",
            "modified_at": "2026-06-15T12:00:00Z", "metadata": {"orgId": "77"}}
    body = json.dumps({"type": "subscription.active", "data": data}).encode()
    ev = c.parse_event(body)
    assert isinstance(ev, CanonicalBillingEvent)
    assert ev.org_id == 77
    assert ev.tier == "pro"


def test_parse_event_is_tolerant_of_malformed_json():
    c = _client()
    ev = c.parse_event(b"{ not valid json")
    assert isinstance(ev, ParseError)


def test_parse_event_rejects_a_body_without_data():
    c = _client()
    ev = c.parse_event(json.dumps({"type": "subscription.updated"}).encode())
    assert isinstance(ev, ParseError)


# ===== Store: subscriptions (v13) =====

def test_schema_is_v13_with_subscriptions_table():
    store = Store(_tmp_db())
    # subscriptions shipped at v13; the schema only moves forward (now v14: workers).
    assert SCHEMA_VERSION >= 13
    row = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='subscriptions'"
    ).fetchone()
    assert row is not None


def test_get_subscription_returns_free_default_when_no_row():
    store = Store(_tmp_db())
    sub = store.get_subscription(org_id=1)
    assert sub["tier"] == "free"
    assert sub["status"] == "active"
    assert sub["lead_cap"] == 10
    assert sub["current_period_start"] is None
    assert sub["provider"] is None


def test_get_subscription_effective_cap_uses_override_for_scale():
    store = Store(_tmp_db())
    store.upsert_subscription(
        5, last_event_ts=100.0, provider="polar", tier="scale",
        lead_cap_override=12345, status="active")
    sub = store.get_subscription(5)
    assert sub["tier"] == "scale"
    assert sub["lead_cap"] == 12345       # the per-deal override, not the placeholder


def test_get_subscription_effective_cap_falls_back_to_catalogue():
    store = Store(_tmp_db())
    store.upsert_subscription(
        6, last_event_ts=100.0, tier="pro", status="active")  # no override
    assert store.get_subscription(6)["lead_cap"] == 2000


def test_upsert_then_get_round_trips():
    store = Store(_tmp_db())
    wrote = store.upsert_subscription(
        9, last_event_ts=200.0, provider="polar", tier="starter", interval="year",
        status="active", provider_subscription_id="sub_1",
        current_period_start=1000.0, current_period_end=2000.0,
        cancel_at_period_end=1)
    assert wrote is True
    sub = store.get_subscription(9)
    assert sub["tier"] == "starter"
    assert sub["interval"] == "year"
    assert sub["current_period_end"] == 2000.0
    assert sub["cancel_at_period_end"] is True


def test_upsert_monotonic_drops_a_stale_event():
    store = Store(_tmp_db())
    # A revoke at ts=500 ...
    store.upsert_subscription(3, last_event_ts=500.0, tier="pro", status="canceled")
    # ... then a DELAYED 'active' update with an OLDER modified_at must NOT win.
    applied = store.upsert_subscription(3, last_event_ts=400.0, tier="pro",
                                        status="active")
    assert applied is False
    assert store.get_subscription(3)["status"] == "canceled"


def test_upsert_exact_redelivery_is_a_noop():
    store = Store(_tmp_db())
    store.upsert_subscription(4, last_event_ts=700.0, tier="lite", status="active")
    # Same event delivered twice (equal ts) → not strictly newer → dropped.
    again = store.upsert_subscription(4, last_event_ts=700.0, tier="lite",
                                      status="active")
    assert again is False


def test_upsert_newer_event_wins():
    store = Store(_tmp_db())
    store.upsert_subscription(8, last_event_ts=100.0, tier="lite", status="active")
    store.upsert_subscription(8, last_event_ts=200.0, tier="lite", status="past_due")
    assert store.get_subscription(8)["status"] == "past_due"


def test_upsert_rejects_unknown_fields():
    store = Store(_tmp_db())
    with pytest.raises(ValueError, match="unknown fields"):
        store.upsert_subscription(1, last_event_ts=1.0, bogus_column="x")


# ===== Store: lead metering + period anchor =====

def _insert_match(store: Store, *, org_id, comment_id, captured_at,
                  status="new", campaign_id="c1", platform="instagram"):
    """Direct insert so the test controls captured_at / org_id / status (the
    public upsert_match stamps now() and org-from-campaign)."""
    with store._tx() as c:
        c.execute(
            """INSERT INTO matches (campaign_id, org_id, platform, reel_id, comment_id,
                                    score, reason, status, captured_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (campaign_id, org_id, platform, "r1", comment_id, 0.9, "ok", status,
             captured_at, captured_at))


def test_count_leads_counts_all_statuses_including_archived():
    store = Store(_tmp_db())
    _insert_match(store, org_id=1, comment_id="a", captured_at=1000.0, status="new")
    _insert_match(store, org_id=1, comment_id="b", captured_at=1000.0, status="archived")
    _insert_match(store, org_id=1, comment_id="c", captured_at=1000.0, status="closed")
    # All surfaced matches count — no status predicate.
    assert store.count_leads_this_period(1, since=0.0) == 3


def test_count_leads_excludes_null_org_matches():
    store = Store(_tmp_db())
    _insert_match(store, org_id=1, comment_id="a", captured_at=1000.0)
    _insert_match(store, org_id=None, comment_id="b", captured_at=1000.0)  # orphan
    # A NULL-org (orphan-campaign) match never counts toward an org's cap.
    assert store.count_leads_this_period(1, since=0.0) == 1


def test_count_leads_respects_the_since_anchor():
    store = Store(_tmp_db())
    _insert_match(store, org_id=1, comment_id="old", captured_at=500.0)
    _insert_match(store, org_id=1, comment_id="new", captured_at=1500.0)
    assert store.count_leads_this_period(1, since=1000.0) == 1


def test_count_leads_does_not_double_count_a_rescored_lead():
    store = Store(_tmp_db())
    # First capture at t=1000 via the public upsert (stamps org from campaign — but
    # here org is NULL since no campaign registry; use direct insert + verify a
    # second upsert on the same key keeps captured_at and the count at 1).
    _insert_match(store, org_id=1, comment_id="x", captured_at=1000.0)
    with store._tx() as c:
        # Re-score: ON-CONFLICT-style update of score but captured_at must not move.
        c.execute("UPDATE matches SET score=0.99, updated_at=9999 WHERE comment_id='x'")
    assert store.count_leads_this_period(1, since=0.0) == 1


def test_period_since_free_org_anchors_to_calendar_month():
    store = Store(_tmp_db())
    # now = 2026-06-15T10:00:00 UTC → Tashkent (UTC+5) is 2026-06-15T15:00.
    # Month start in Tashkent = 2026-06-01T00:00:00+05:00.
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc).timestamp()
    since = store.period_since(99, now=now)
    expected = datetime(2026, 6, 1, 0, 0, 0,
                        tzinfo=timezone(timedelta(hours=5))).timestamp()
    assert since == expected


def test_period_since_paid_org_uses_persisted_period_start():
    store = Store(_tmp_db())
    store.upsert_subscription(7, last_event_ts=100.0, tier="pro", status="active",
                              current_period_start=1234567.0)
    assert store.period_since(7) == 1234567.0
