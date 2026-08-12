"""Platform → FeedSource registry (multi-platform plan Part B)."""
import os
import tempfile

import pytest

from aizu.cli import _resolve_platform_credentials
from aizu.core.config import campaign_from_brief
from aizu.dispatch import build_feed
from aizu.secrets import SecretCipher
from aizu.core.store import Store


def test_youtube_requires_seeds():
    with pytest.raises(ValueError, match="youtube needs"):
        build_feed("youtube", cdp_url="x")


def test_youtube_live_needs_api_key(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="YOUTUBE_API_KEY"):
        build_feed("youtube", cdp_url="x", seed_channels=["UC_abc"])


def test_telegram_requires_seed_channels():
    with pytest.raises(ValueError, match="seed_channels"):
        build_feed("telegram", cdp_url="x", seed_channels=[])


def test_telegram_live_needs_user_session(monkeypatch):
    # Telegram now reads arbitrary public channels via an MTProto user session
    # (Telethon), not the Bot API — so a live run needs the user-session creds.
    for var in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError, match="TELEGRAM_API_ID"):
        build_feed("telegram", cdp_url="x", seed_channels=["@tashkent_news"])


def test_unknown_platform_raises_value_error():
    with pytest.raises(ValueError, match="unknown platform"):
        build_feed("myspace", cdp_url="http://127.0.0.1:9222")


# ----- per-org credentials (Phase 1: runs read creds from DB, not env) -----

def test_youtube_uses_explicit_credentials_over_env(monkeypatch):
    # An empty env would make from_env raise — passing credentials must bypass it.
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    feed = build_feed("youtube", cdp_url="x", seed_channels=["UC_abc"],
                      credentials={"api_key": "ORG-KEY"})
    assert feed._client._key == "ORG-KEY"


def test_youtube_falls_back_to_env_when_no_credentials(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "ENV-KEY")
    feed = build_feed("youtube", cdp_url="x", seed_channels=["UC_abc"])
    assert feed._client._key == "ENV-KEY"


def test_youtube_credentials_missing_key_is_loud():
    with pytest.raises(RuntimeError, match="missing api_key"):
        build_feed("youtube", cdp_url="x", seed_channels=["UC_abc"],
                   credentials={"api_key": ""})


def test_telegram_uses_explicit_credentials_over_env(monkeypatch):
    # Avoid the real Telethon connect by faking the client constructor; assert the
    # per-org creds (not env) are what build_feed threads through.
    from aizu.engines.telegram import feed as tg
    captured = {}

    class FakeClient:
        def __init__(self, api_id, api_hash, session):
            captured.update(api_id=api_id, api_hash=api_hash, session=session)

    monkeypatch.setattr(tg.TelethonClient, "from_credentials",
                        classmethod(lambda cls, creds: FakeClient(
                            int(creds["api_id"]), creds["api_hash"], creds["session"])))
    monkeypatch.setattr(tg.TelegramFeed, "attach", lambda self: None)
    build_feed("telegram", cdp_url="x", seed_channels=["@news"],
               credentials={"api_id": 42, "api_hash": "h", "session": "S"})
    assert captured == {"api_id": 42, "api_hash": "h", "session": "S"}


# ----- cli: campaign → org → stored-secret resolution -----

def _store_with_cipher() -> Store:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path, secret_cipher=SecretCipher(SecretCipher.generate_key()))


def _campaign(platform: str, cid: str = "c1"):
    return campaign_from_brief(cid, {"platform": platform})


def test_resolve_credentials_returns_stored_secret_for_youtube():
    store = _store_with_cipher()
    try:
        org_id = store.create_organization(name="Acme")
        store.upsert_campaign_meta("c1", org_id=org_id)
        store.set_integration_secret(org_id, "youtube", {"api_key": "STORED"})
        assert _resolve_platform_credentials(_campaign("youtube"), store) == {"api_key": "STORED"}
    finally:
        store.close()


def test_resolve_credentials_none_for_instagram():
    store = _store_with_cipher()
    try:
        assert _resolve_platform_credentials(_campaign("instagram"), store) is None
    finally:
        store.close()


def test_resolve_credentials_none_when_unregistered_or_unset():
    store = _store_with_cipher()
    try:
        # unregistered campaign → no org → None (falls back to env in build_feed)
        assert _resolve_platform_credentials(_campaign("youtube"), store) is None
    finally:
        store.close()


# ----- B4 fix review finding #2: baked credentials (JobSpec.platform_credentials) -----

def test_resolve_credentials_prefers_baked_over_local_lookup():
    """A non-None `baked` (server._dispatch_run_to_fleet's per-org secret shipped in
    the job spec) wins outright, without touching the store at all."""
    store = _store_with_cipher()
    try:
        org_id = store.create_organization(name="Acme")
        store.upsert_campaign_meta("c1", org_id=org_id)
        store.set_integration_secret(org_id, "youtube", {"api_key": "LOCAL"})
        assert _resolve_platform_credentials(
            _campaign("youtube"), store, baked={"api_key": "BAKED"}
        ) == {"api_key": "BAKED"}
    finally:
        store.close()


def test_resolve_credentials_baked_survives_an_empty_local_store():
    """The genuinely-remote-worker case this fix targets: NO campaign_meta row and NO
    integration_secrets row on this box at all (store.org_for_campaign would resolve
    None here, exactly like on a real remote worker's empty local DB) — the baked
    credential from the job spec still gets through instead of silently falling back
    to an env lookup that was never going to find a per-org secret."""
    store = _store_with_cipher()  # nothing registered, nothing stored
    try:
        assert _resolve_platform_credentials(
            _campaign("youtube"), store, baked={"api_key": "BAKED"}
        ) == {"api_key": "BAKED"}
    finally:
        store.close()


def test_resolve_credentials_falls_back_to_local_lookup_when_not_baked():
    """No `baked` kwarg at all (a direct CLI run) → unchanged local-lookup behavior."""
    store = _store_with_cipher()
    try:
        org_id = store.create_organization(name="Acme")
        store.upsert_campaign_meta("c1", org_id=org_id)
        store.set_integration_secret(org_id, "youtube", {"api_key": "STORED"})
        assert _resolve_platform_credentials(_campaign("youtube"), store) == {"api_key": "STORED"}
    finally:
        store.close()


def test_instagram_build_feed_threads_include_home_feed(monkeypatch):
    from aizu.engines.instagram.cdp import CDPFeed
    monkeypatch.setattr(CDPFeed, "attach", lambda self: None)  # no real Chrome
    feed = build_feed("instagram", cdp_url="http://127.0.0.1:9222",
                      seed_hashtags=["flutterdev"], include_home_feed=False)
    assert feed.cfg.include_home_feed is False
    assert feed.cfg.seed_hashtags == ("flutterdev",)


def test_cdpfeed_sources_honor_include_home_feed():
    from aizu.engines.instagram.cdp import REELS_URL, CDPConfig, CDPFeed
    on = CDPFeed(CDPConfig(seed_hashtags=("flutterdev",), include_home_feed=True))
    off = CDPFeed(CDPConfig(seed_hashtags=("flutterdev",), include_home_feed=False))
    assert REELS_URL in on._sources()                    # home feed walked first
    assert REELS_URL not in off._sources()               # skipped entirely
    assert any("explore/tags/flutterdev" in u for u in off._sources())


def test_cdpfeed_detects_keyword_search_redirect():
    # A tag source that lands on IG's keyword-search fallback is a no-reels
    # redirect → skip fast; a tag that lands where requested is NOT.
    from aizu.engines.instagram.cdp import CDPFeed, TAG_URL
    feed = CDPFeed()
    tag = TAG_URL.format(tag="leadgen")
    search = "https://www.instagram.com/explore/search/keyword/?q=%23leadgen"
    assert feed._source_redirected(tag, search) is True
    assert feed._source_redirected(tag, tag) is False


def test_cdpfeed_requested_search_url_is_not_a_redirect():
    # If the SOURCE itself was a search URL, landing on search is expected —
    # not a redirect to skip.
    from aizu.engines.instagram.cdp import CDPFeed
    feed = CDPFeed()
    search = "https://www.instagram.com/explore/search/keyword/?q=x"
    assert feed._source_redirected(search, search) is False
