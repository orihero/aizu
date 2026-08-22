"""One dead Telegram channel must not kill the session (Campaign Lab, Remedy
Sheet #2 — audit bug #4).

Entity resolution happens inside `walk()`'s generator, and the session's
`for reel in self.feed.walk()` loop has no try/except — so a single renamed
`@username` in a list of eight raised straight out through the crash guard and
ended the run, harvesting nothing from the other seven.
"""
import pytest

from aizu.core.feed import SOURCE_ACCOUNT
from aizu.engines.telegram.feed import TelegramFeed, TgMessage, _is_session_level


class _Client:
    def __init__(self, by_channel):
        self._by = by_channel

    def iter_channel_messages(self, channel, limit):
        item = self._by.get(channel, [])
        if isinstance(item, Exception):
            raise item
        for i, text in enumerate(item):
            yield TgMessage(id=i + 1, text=text, sender="someone")

    def iter_replies(self, channel, message_id, min_id):
        return []


def _feed(by_channel, channels):
    f = TelegramFeed(client=_Client(by_channel), channels=channels)
    f.outcomes = []
    f.on_source_done = f.outcomes.append
    return f


class UsernameNotOccupiedError(Exception):
    """Telethon's real class name — matched structurally, never imported."""


class AuthKeyUnregisteredError(Exception):
    pass


def test_a_dead_channel_is_skipped_and_the_rest_still_walk():
    feed = _feed({"@gone": UsernameNotOccupiedError("no user has @gone"),
                  "@live": ["hello"]},
                 ("@gone", "@live"))
    assert [r.caption for r in feed.walk()] == ["hello"]
    gone, live = feed.outcomes
    assert (gone.source, gone.kind, gone.unavailable) == ("@gone", SOURCE_ACCOUNT, True)
    assert (live.source, live.yielded, live.unavailable) == ("@live", 1, False)


def test_a_bare_value_error_from_entity_resolution_is_a_dead_channel():
    feed = _feed({"@gone": ValueError(
        "Cannot find any entity corresponding to '@gone'"), "@live": ["hi"]},
        ("@gone", "@live"))
    assert [r.caption for r in feed.walk()] == ["hi"]


def test_a_revoked_session_still_propagates():
    """Not this channel's fault, and `cli._is_auth_error` has to see it to flip
    the integration to needs-reconnect."""
    feed = _feed({"@a": AuthKeyUnregisteredError("auth key unregistered"),
                  "@b": ["never reached"]},
                 ("@a", "@b"))
    with pytest.raises(AuthKeyUnregisteredError):
        list(feed.walk())


def test_a_flood_wait_stops_the_walk_rather_than_burning_every_channel():
    class FloodWaitError(Exception):
        pass
    feed = _feed({"@a": FloodWaitError("A wait of 3600 seconds is required")},
                 ("@a", "@b"))
    with pytest.raises(FloodWaitError):
        list(feed.walk())


def test_messages_already_yielded_before_a_failure_are_kept():
    class _Partial:
        def iter_channel_messages(self, channel, limit):
            yield TgMessage(id=1, text="first", sender="x")
            raise UsernameNotOccupiedError("gone mid-iteration")
        def iter_replies(self, *a):
            return []

    feed = TelegramFeed(client=_Partial(), channels=("@x",))
    feed.outcomes = []
    feed.on_source_done = feed.outcomes.append
    assert [r.caption for r in feed.walk()] == ["first"]
    (out,) = feed.outcomes
    assert (out.yielded, out.unavailable) == (1, True)


def test_all_channels_dead_is_logged_as_an_explicit_error(monkeypatch):
    import aizu.engines.telegram.feed as tfeed
    errors = []
    monkeypatch.setattr(tfeed.log, "error",
                        lambda msg, *a, **k: errors.append(msg % a if a else msg))
    feed = _feed({"@a": UsernameNotOccupiedError("x"),
                  "@b": UsernameNotOccupiedError("y")}, ("@a", "@b"))
    assert list(feed.walk()) == []
    assert errors and "every seed is dead" in errors[0]


def test_a_partially_dead_seed_list_is_not_reported_as_all_dead(monkeypatch):
    import aizu.engines.telegram.feed as tfeed
    errors = []
    monkeypatch.setattr(tfeed.log, "error", lambda msg, *a, **k: errors.append(msg))
    feed = _feed({"@a": UsernameNotOccupiedError("x"), "@b": ["ok"]}, ("@a", "@b"))
    list(feed.walk())
    assert errors == []


@pytest.mark.parametrize("exc,expected", [
    (UsernameNotOccupiedError("nope"), False),
    (ValueError("Cannot find any entity"), False),
    (AuthKeyUnregisteredError("bad"), True),
    (Exception("A wait of 30 seconds is required (FLOOD)"), True),
    (Exception("session revoked"), True),
    (Exception("Not authorized"), True),
])
def test_session_level_classification(exc, expected):
    assert _is_session_level(exc) is expected
