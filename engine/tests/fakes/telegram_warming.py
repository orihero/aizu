"""FakeTelegramWarmingPort — a record-and-replay double for TelegramWarmingPort.

The warming routine (later slice) depends on ``TelegramWarmingPort``, so tests
drive it with this fake instead of Telethon: it records joins/reactions, serves
canned search results, and can be told to raise a ``TelegramFloodError`` (hard or
soft) or treat a channel as already-joined / an emoji as invalid.
"""
from __future__ import annotations

from typing import Optional

from reelradar.engines.telegram.warming_writes import TelegramFloodError, TgChannel


class FakeTelegramWarmingPort:
    def __init__(self, *,
                 search_results: Optional[dict[str, list[TgChannel]]] = None,
                 already_joined: Optional[set[str]] = None,
                 invalid_emojis: Optional[set[str]] = None,
                 raise_on_search: Optional[TelegramFloodError] = None,
                 raise_on_join: Optional[TelegramFloodError] = None,
                 raise_on_react: Optional[TelegramFloodError] = None,
                 connected: bool = True) -> None:
        self._search_results = search_results or {}
        self._already_joined = set(already_joined or set())
        self._invalid_emojis = set(invalid_emojis or set())
        self._raise_on_search = raise_on_search
        self._raise_on_join = raise_on_join
        self._raise_on_react = raise_on_react
        self._connected = connected
        # Recorded SPENDS — an idempotent rejoin / soft-skipped react is NOT recorded.
        self.joined: list[str] = []
        self.reactions: list[tuple[str, int, str]] = []

    def connected(self) -> bool:
        return self._connected

    def search(self, query: str, *, limit: int = 10) -> list[TgChannel]:
        if self._raise_on_search is not None:
            raise self._raise_on_search
        return list(self._search_results.get(query, []))[:limit]

    def join(self, channel: str) -> bool:
        if self._raise_on_join is not None:
            raise self._raise_on_join
        if channel in self._already_joined:
            return False   # idempotent no-op (zero growth); not a budget spend
        self.joined.append(channel)
        return True

    def react(self, channel: str, message_id: int, emoji: str) -> bool:
        if self._raise_on_react is not None:
            raise self._raise_on_react
        if emoji in self._invalid_emojis:
            return False   # soft skip; not recorded
        self.reactions.append((channel, message_id, emoji))
        return True
