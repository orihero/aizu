"""Phase 4 — run-time auth errors flip the per-org integration to needs-reconnect.

Covers the auth-error classifier and the _run_one seam that flags the integration
(connected=False, detail="needs reconnect") and re-raises a foldable RuntimeError.
"""
import argparse
import os
import tempfile
import types

import pytest

from reelradar import cli
from reelradar.core.config import campaign_from_brief
from reelradar.secrets import SecretCipher
from reelradar.core.store import Store


# ----- classifier -----

def test_is_auth_error_matches_reconnect_messages():
    assert cli._is_auth_error(RuntimeError("YouTube credentials missing api_key — reconnect"))
    assert cli._is_auth_error(RuntimeError("Telegram credentials missing api_id/api_hash/session"))


def test_is_auth_error_matches_http_403():
    exc = RuntimeError("forbidden")
    exc.response = types.SimpleNamespace(status_code=403)
    assert cli._is_auth_error(exc)


def test_is_auth_error_matches_telethon_class_names():
    class UnauthorizedError(Exception):
        pass
    assert cli._is_auth_error(UnauthorizedError())


def test_is_auth_error_ignores_unrelated_errors():
    assert not cli._is_auth_error(RuntimeError("OPENROUTER_API_KEY not set"))
    assert not cli._is_auth_error(ValueError("bad seed"))


# ----- _run_one flagging -----

def _store() -> tuple[Store, int]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = Store(path, secret_cipher=SecretCipher(SecretCipher.generate_key()))
    org_id = store.create_organization(name="Acme")
    store.upsert_campaign_meta("c1", org_id=org_id, status="live")
    store.set_integration(org_id, "youtube", connected=True, detail="connected")
    store.set_integration_secret(org_id, "youtube", {"api_key": "stale"})
    return store, org_id


def _args() -> argparse.Namespace:
    return argparse.Namespace(spend_cap=1.0, text_model="t", vision_model="v",
                              cdp_url="http://x")


def test_run_one_flags_needs_reconnect_on_live_auth_error(monkeypatch):
    store, org_id = _store()
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")

    # Simulate the feed raising an auth error during construction (invalid key).
    def _boom(*a, **k):
        raise RuntimeError("youtube credentials missing api_key — reconnect")
    monkeypatch.setattr("reelradar.dispatch.build_feed", _boom)

    campaign = campaign_from_brief("c1", {"platform": "youtube", "seed_hashtags": ["q"]})
    try:
        with pytest.raises(RuntimeError, match="needs reconnect"):
            cli._run_one(campaign=campaign, store=store, soul=None,
                         dry_run=False, args=_args())
        row = {i["platform"]: i for i in store.list_integrations(org_id)}["youtube"]
        assert row["connected"] == 0
        assert row["detail"] == "needs reconnect"
    finally:
        store.close()


def test_run_one_does_not_flag_on_non_auth_error(monkeypatch):
    store, org_id = _store()
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")

    def _boom(*a, **k):
        raise RuntimeError("transient network blip")
    monkeypatch.setattr("reelradar.dispatch.build_feed", _boom)

    campaign = campaign_from_brief("c1", {"platform": "youtube", "seed_hashtags": ["q"]})
    try:
        with pytest.raises(RuntimeError, match="network blip"):
            cli._run_one(campaign=campaign, store=store, soul=None,
                         dry_run=False, args=_args())
        row = {i["platform"]: i for i in store.list_integrations(org_id)}["youtube"]
        assert row["connected"] == 1                 # untouched — not an auth error
        assert row["detail"] == "connected"
    finally:
        store.close()
