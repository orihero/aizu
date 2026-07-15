"""TelethonWarmingClient — the real Telethon write adapter's mapping logic
(warming-writes PRD §7.2, §8.3), exercised without a live connection.

The adapter wraps an injectable raw Telethon client (like ``TelegramBotClient``
injects ``http_get``), so the RPC-result→TgChannel mapping and the Telethon-error
→``TelegramFloodError`` classification are unit-tested with a fake raw client.
"""
import pytest

from reelradar.engines.telegram.warming_writes import (
    TelegramFloodError,
    TelethonWarmingClient,
)


class _Chat:
    """A minimal stand-in for a Telethon chat in SearchGlobalRequest results."""
    def __init__(self, username=None, title="", participants_count=0, broadcast=False):
        self.username = username
        self.title = title
        self.participants_count = participants_count
        self.broadcast = broadcast


class _SearchResult:
    def __init__(self, chats):
        self.chats = chats


class _FakeRaw:
    """A fake raw Telethon client: ``__call__(request)`` returns/raises per the
    request type's class name, and resolve/peer helpers are no-ops."""
    def __init__(self, *, search=None, join_result=True, react_result=True,
                 raise_on_join=None, raise_on_react=None, connected=True):
        self._search = search if search is not None else _SearchResult([])
        self._join_result = join_result
        self._react_result = react_result
        self._raise_on_join = raise_on_join
        self._raise_on_react = raise_on_react
        self._connected = connected
        self.calls = []

    def __call__(self, request):
        name = type(request).__name__
        self.calls.append(name)
        if name == "SearchGlobalRequest":
            return self._search
        if name == "JoinChannelRequest":
            if self._raise_on_join is not None:
                raise self._raise_on_join
            return self._join_result
        if name == "SendReactionRequest":
            if self._raise_on_react is not None:
                raise self._raise_on_react
            return self._react_result
        raise AssertionError(f"unexpected request {name}")

    def is_connected(self):
        return self._connected

    def get_input_entity(self, channel):
        return channel


def _client(raw):
    return TelethonWarmingClient(raw=raw)


def test_search_maps_chats_to_channels_skipping_usernameless():
    raw = _FakeRaw(search=_SearchResult([
        _Chat(username="growthlab", title="Growth Lab",
              participants_count=4200, broadcast=True),
        _Chat(username=None, title="private", participants_count=9),  # skipped
    ]))
    out = _client(raw).search("saas", limit=10)
    assert [c.username for c in out] == ["@growthlab"]
    assert out[0].participants == 4200 and out[0].is_channel is True


def test_search_degrades_when_fields_missing():
    raw = _FakeRaw(search=_SearchResult([_Chat(username="x")]))
    out = _client(raw).search("q", limit=5)
    assert out[0].participants == 0 and out[0].title == ""


def test_join_returns_true_on_success():
    raw = _FakeRaw(join_result=object())
    assert _client(raw).join("@growthlab") is True
    assert "JoinChannelRequest" in raw.calls


def test_join_already_participant_is_idempotent_noop_no_spend():
    # An already-joined channel is a zero-growth no-op: returns False so the caller
    # spends NO join budget (never raises).
    raw = _FakeRaw(raise_on_join=_named_exc("UserAlreadyParticipantError"))
    assert _client(raw).join("@growthlab") is False


def test_join_maps_peer_flood_to_hard_flood_error():
    raw = _FakeRaw(raise_on_join=_named_exc("PeerFloodError"))
    with pytest.raises(TelegramFloodError) as exc:
        _client(raw).join("@growthlab")
    assert exc.value.is_hard is True and exc.value.op == "join"


def test_join_maps_channels_too_much_to_hard_flood_error():
    raw = _FakeRaw(raise_on_join=_named_exc("ChannelsTooMuchError"))
    with pytest.raises(TelegramFloodError) as exc:
        _client(raw).join("@growthlab")
    assert exc.value.is_hard is True


def test_join_large_flood_wait_is_hard():
    raw = _FakeRaw(raise_on_join=_flood_wait(3600))
    with pytest.raises(TelegramFloodError) as exc:
        _client(raw).join("@growthlab")
    assert exc.value.is_hard is True and exc.value.retry_seconds == 3600


def test_join_small_flood_wait_is_soft():
    raw = _FakeRaw(raise_on_join=_flood_wait(20))
    with pytest.raises(TelegramFloodError) as exc:
        _client(raw).join("@growthlab")
    assert exc.value.is_hard is False and exc.value.retry_seconds == 20


def test_react_returns_true_on_success():
    raw = _FakeRaw(react_result=object())
    assert _client(raw).react("@growthlab", 42, "👍") is True
    assert "SendReactionRequest" in raw.calls


def test_react_invalid_reaction_soft_skips():
    raw = _FakeRaw(raise_on_react=_named_exc("ReactionInvalidError"))
    assert _client(raw).react("@growthlab", 42, "🦄") is False


def test_react_peer_flood_raises_hard():
    raw = _FakeRaw(raise_on_react=_named_exc("PeerFloodError"))
    with pytest.raises(TelegramFloodError) as exc:
        _client(raw).react("@growthlab", 42, "👍")
    assert exc.value.is_hard is True and exc.value.op == "react"


def test_connected_reflects_raw():
    assert _client(_FakeRaw(connected=False)).connected() is False
    assert _client(_FakeRaw(connected=True)).connected() is True


# --- helpers: synthesize Telethon-shaped errors by class name ---

def _named_exc(class_name: str) -> Exception:
    return type(class_name, (Exception,), {})()


def _flood_wait(seconds: int) -> Exception:
    cls = type("FloodWaitError", (Exception,), {})
    err = cls()
    err.seconds = seconds
    return err
