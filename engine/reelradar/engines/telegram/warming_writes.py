"""Telegram warming WRITE surface (warming-writes PRD §7.2, §8.3).

Telegram warming is a different paradigm from the IG reel-walk: ``search →
join (capped) → dwell → react (occasional)`` — no like/follow/share, no DMs, no
message sends. This module isolates the three Telethon write RPCs behind a
``TelegramWarmingPort`` Protocol so the (later) warming routine depends on the
interface, not Telethon directly, and tests drive a fake (mirroring how the
read-only ``TelegramClientPort`` in ``feed.py`` is faked).

Flood signals are mapped to ONE classified error type — ``TelegramFloodError``
carrying ``is_hard`` + ``retry_seconds`` — so the routine can decide between
"halt the whole session" (hard: ``PeerFloodError`` / ``ChannelsTooMuchError`` /
a large ``FloodWaitError``) and "skip this one action and continue" (soft: a
small ``FloodWaitError``). The mapping keys on the Telethon error CLASS NAME, so
it holds regardless of the pinned Telethon version and needs no import to test.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

from ...core.logsetup import get_logger

log = get_logger(__name__)

# A FloodWaitError whose backoff is at/above this many seconds is treated as a
# HARD block (halt the session); below it is a soft skip (respect that one wait,
# continue). A fresh warming account at joins<=1/day should never trip flood at
# all, so any large wait means caps/pacing need tightening (PRD §8.3).
HARD_FLOOD_WAIT_SECONDS = 60

# Benign reaction allowlist (PRD §7.5a) — a small random pick keeps reactions
# lightweight and human; a disallowed emoji soft-skips, never halts.
REACTION_ALLOWLIST: tuple[str, ...] = ("👍", "🔥", "❤")

# Telethon error class names → classification. Kept as names (not imported types)
# so the mapping is version-robust and unit-testable without Telethon installed.
_HARD_FLOOD_NAMES = frozenset({"PeerFloodError", "ChannelsTooMuchError"})
_ALREADY_PARTICIPANT_NAMES = frozenset({"UserAlreadyParticipantError"})
_INVALID_REACTION_NAMES = frozenset({"ReactionInvalidError"})
_FLOOD_WAIT_NAMES = frozenset({"FloodWaitError"})


@dataclass(frozen=True)
class TgChannel:
    """The slice of a Telegram channel the warming routine needs — platform-neutral
    so the Telethon shape never leaks upstream (mirrors ``TgMessage`` in feed.py)."""
    username: str
    title: str = ""
    participants: int = 0
    is_channel: bool = False


class TelegramFloodError(RuntimeError):
    """A Telegram flood/rate signal, classified for the warming routine.

    ``is_hard`` True  → halt the whole session (per-account spam-block, joined too
    many channels, or a large rate-limit backoff).
    ``is_hard`` False → respect/skip this single action and continue.
    ``retry_seconds`` carries a FloodWait backoff when Telegram supplied one.
    ``op`` is the write that tripped it (``"join"`` / ``"react"``).
    """

    def __init__(self, kind: str, *, is_hard: bool,
                 retry_seconds: Optional[int] = None, op: Optional[str] = None):
        super().__init__(f"telegram flood: {kind}"
                         + (f" (retry {retry_seconds}s)" if retry_seconds else ""))
        self.kind = kind
        self.is_hard = is_hard
        self.retry_seconds = retry_seconds
        self.op = op


@runtime_checkable
class TelegramWarmingPort(Protocol):
    """The only write surface the warming routine needs from a Telegram client.

    The Telethon adapter (``TelethonWarmingClient``) implements it for live runs;
    tests implement a fake. Separate from the read-only ``TelegramClientPort`` in
    feed.py, which stays untouched."""

    def connected(self) -> bool: ...

    def search(self, query: str, *, limit: int = 10) -> list[TgChannel]: ...

    def join(self, channel: str) -> bool: ...

    def react(self, channel: str, message_id: int, emoji: str) -> bool: ...


def _seconds_of(exc: Exception) -> Optional[int]:
    raw = getattr(exc, "seconds", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _classify_flood(exc: Exception, op: str) -> TelegramFloodError:
    """Map a Telethon RPC error to a classified ``TelegramFloodError``. Caller is
    responsible for only handing flood-shaped errors here (others re-raise)."""
    name = type(exc).__name__
    if name in _HARD_FLOOD_NAMES:
        return TelegramFloodError(_kind_for(name), is_hard=True, op=op)
    seconds = _seconds_of(exc)
    is_hard = (seconds or 0) >= HARD_FLOOD_WAIT_SECONDS
    return TelegramFloodError("flood_wait", is_hard=is_hard,
                              retry_seconds=seconds, op=op)


def _kind_for(name: str) -> str:
    """Stable kind string for the flag the session later raises (PRD §8.3:
    a per-account spam-block / too-many-channels both crater warmth)."""
    if name == "PeerFloodError":
        return "peer_flood"
    if name == "ChannelsTooMuchError":
        return "channels_too_much"
    return "flood_wait"


def _is_flood(exc: Exception) -> bool:
    return type(exc).__name__ in (_HARD_FLOOD_NAMES | _FLOOD_WAIT_NAMES)


class TelethonWarmingClient:
    """Live MTProto write adapter implementing ``TelegramWarmingPort``.

    The raw ``telethon.sync.TelegramClient`` is injectable (``raw=``) so the
    RPC-result→``TgChannel`` mapping and the error classification are unit-tested
    with a fake; the live path builds the same StringSession client the read-only
    ``TelethonClient`` uses (lazy import — no hard Telethon dependency at import).
    """

    def __init__(self, *, raw: Any):
        self._client = raw

    @classmethod
    def from_credentials(cls, credentials: Optional[dict]) -> "TelethonWarmingClient":
        """Build from a per-org stored secret ({api_id, api_hash, session}),
        reusing the read-only adapter's construction so warming and harvest share
        one connection contract."""
        from .feed import TelethonClient
        read = TelethonClient.from_credentials(credentials)
        return cls(raw=read._client)  # share the one connected StringSession client

    def connected(self) -> bool:
        return bool(self._client.is_connected())

    def search(self, query: str, *, limit: int = 10) -> list[TgChannel]:
        """``messages.SearchGlobalRequest`` (read-only RPC; the join is the write).
        Field names vary by Telethon version, so every read is ``getattr``-guarded
        and degrades to a benign default (PRD §7.2 GAP note)."""
        from datetime import datetime
        from telethon.tl.functions.messages import SearchGlobalRequest
        from telethon.tl.types import InputMessagesFilterEmpty, InputPeerEmpty

        res = self._client(SearchGlobalRequest(
            q=query, filter=InputMessagesFilterEmpty(),
            min_date=datetime.fromtimestamp(0), max_date=datetime.now(),
            offset_rate=0, offset_peer=InputPeerEmpty(), offset_id=0, limit=limit))
        return _channels_from_search(res)

    def join(self, channel: str) -> bool:
        """``channels.JoinChannelRequest`` — THE write that grows channels-joined.
        Returns ``True`` only for a NEW join (real network growth). Rejoining an
        already-joined channel is an idempotent no-op that returns ``False`` so the
        caller spends NO join budget on zero growth (``UserAlreadyParticipantError``);
        flood signals map to ``TelegramFloodError`` for the routine to classify."""
        from telethon.tl.functions.channels import JoinChannelRequest

        try:
            entity = self._client.get_input_entity(channel)
            return bool(self._client(JoinChannelRequest(entity)))
        except Exception as e:  # noqa: BLE001 - classify, never swallow
            if type(e).__name__ in _ALREADY_PARTICIPANT_NAMES:
                return False   # idempotent no-op; caller must NOT spend join budget
            if _is_flood(e):
                raise _classify_flood(e, op="join") from e
            raise

    def react(self, channel: str, message_id: int, emoji: str) -> bool:
        """``messages.SendReactionRequest`` with a benign emoji (never a send).
        A disallowed emoji soft-skips (returns False); flood signals raise."""
        from telethon.tl.functions.messages import SendReactionRequest
        from telethon.tl.types import ReactionEmoji

        try:
            peer = self._client.get_input_entity(channel)
            return bool(self._client(SendReactionRequest(
                peer=peer, msg_id=message_id,
                reaction=[ReactionEmoji(emoticon=emoji)], add_to_recent=True)))
        except Exception as e:  # noqa: BLE001 - classify, never swallow
            if type(e).__name__ in _INVALID_REACTION_NAMES:
                log.info("Telegram react soft-skip · %s · %s", channel, emoji)
                return False
            if _is_flood(e):
                raise _classify_flood(e, op="react") from e
            raise


def _channels_from_search(res: Any) -> list[TgChannel]:
    out: list[TgChannel] = []
    for chat in getattr(res, "chats", []) or []:
        uname = getattr(chat, "username", None)
        if not uname:
            continue   # un-joinable without a public username
        out.append(TgChannel(
            username=f"@{uname}",
            title=getattr(chat, "title", "") or "",
            participants=getattr(chat, "participants_count", 0) or 0,
            is_channel=bool(getattr(chat, "broadcast", False))))
    return out
