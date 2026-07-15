"""TelegramWarmingPort contract + flood-error mapping (warming-writes PRD §7.2, §8.3).

The Telethon write surface (search / join / react) is isolated behind
``TelegramWarmingPort`` so the warming routine depends on the interface, not
Telethon. These tests drive a ``FakeTelegramWarmingPort`` (records joins/reactions,
can be told to raise flood) and assert the port contract + the flood-error
classification (``TelegramFloodError`` carrying ``is_hard``/``retry_seconds``).
"""
import pytest

from reelradar.engines.telegram.warming_writes import (
    TelegramFloodError,
    TgChannel,
)
from tests.fakes.telegram_warming import FakeTelegramWarmingPort


def test_search_returns_candidate_channels():
    port = FakeTelegramWarmingPort(search_results={
        "saas leads": [
            TgChannel(username="@growthlab", title="Growth Lab",
                      participants=4200, is_channel=True),
        ],
    })
    out = port.search("saas leads", limit=10)
    assert [c.username for c in out] == ["@growthlab"]
    assert out[0].participants == 4200
    assert out[0].is_channel is True


def test_search_unknown_query_returns_empty():
    port = FakeTelegramWarmingPort(search_results={})
    assert port.search("nothing here", limit=10) == []


def test_join_records_channel_and_returns_true():
    port = FakeTelegramWarmingPort()
    assert port.join("@growthlab") is True
    assert port.joined == ["@growthlab"]


def test_join_already_participant_is_idempotent_noop_no_spend():
    # An already-joined channel is a zero-growth no-op: it returns False so the
    # caller spends NO join budget, and it is NOT re-recorded as a spend.
    port = FakeTelegramWarmingPort(already_joined={"@growthlab"})
    assert port.join("@growthlab") is False
    assert port.joined == []   # no budget-spending record for an idempotent rejoin


def test_react_records_reaction_and_returns_true():
    port = FakeTelegramWarmingPort()
    assert port.react("@growthlab", 42, "👍") is True
    assert port.reactions == [("@growthlab", 42, "👍")]


def test_react_invalid_emoji_soft_skips_returns_false():
    # A disallowed emoji is a soft skip (succeeded=False), never a raise.
    port = FakeTelegramWarmingPort(invalid_emojis={"🦄"})
    assert port.react("@growthlab", 42, "🦄") is False
    assert port.reactions == []


def test_join_raises_hard_flood_on_peer_flood():
    port = FakeTelegramWarmingPort(raise_on_join=TelegramFloodError(
        "peer_flood", is_hard=True))
    with pytest.raises(TelegramFloodError) as exc:
        port.join("@growthlab")
    assert exc.value.is_hard is True
    assert exc.value.retry_seconds is None


def test_join_raises_soft_flood_with_retry_seconds():
    port = FakeTelegramWarmingPort(raise_on_join=TelegramFloodError(
        "flood_wait", is_hard=False, retry_seconds=12))
    with pytest.raises(TelegramFloodError) as exc:
        port.join("@growthlab")
    assert exc.value.is_hard is False
    assert exc.value.retry_seconds == 12


def test_flood_error_carries_op_and_str():
    err = TelegramFloodError("peer_flood", is_hard=True, op="join")
    assert err.kind == "peer_flood"
    assert err.op == "join"
    assert "peer_flood" in str(err)
