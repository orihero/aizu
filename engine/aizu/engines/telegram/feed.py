"""Telegram FeedSource (PRD: docs/prd/telegram-lead-agent-PRD.md).

Read-only over MTProto. Unlike the algorithmic-feed platforms, Telegram
discovery is deterministic: the operator seeds public channels/groups in
`campaign.md` (`seed_channels`) and the engine walks their recent messages. A
channel **message** ≈ `Reel` (text + author); a discussion **reply** ≈ `Comment`.

The Telethon binding is isolated behind `TelegramClientPort` so the mapping
logic (message→Reel, reply→Comment, cursor) is pure and unit-tested with a fake
client. Telethon itself is a lazy/optional import, exercised only on a live run —
the engine carries no hard dependency on it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional, Protocol, Sequence

from ...core.feed import Comment, FeedSource, Reel
from ...core.logsetup import get_logger

log = get_logger(__name__)

# Recent messages to read per seeded channel each session (PRD §10 pacing:
# bounded window, not full history). Conservative default; tune per campaign.
DEFAULT_PER_CHANNEL = 40

# reel_id encodes the channel + message id so dedupe/cursors stay per-message and
# fetch_comments can recover which channel/message a reply thread belongs to.
_REEL_SEP = "/"


@dataclass
class TgMessage:
    """The slice of a Telegram message the engine needs — platform-neutral, so
    the mapping stays pure and the Telethon shape never leaks upstream."""
    id: int
    text: str = ""
    sender: Optional[str] = None


class TelegramClientPort(Protocol):
    """The only surface TelegramFeed needs from a Telegram client. The Telethon
    adapter implements it for live runs; tests implement a fake."""

    def connected(self) -> bool: ...

    def iter_channel_messages(self, channel: str, limit: int) -> Iterable[TgMessage]: ...

    def iter_replies(self, channel: str, message_id: int,
                     min_id: int) -> Iterable[TgMessage]: ...


def _encode_reel_id(channel: str, message_id: int) -> str:
    return f"{channel}{_REEL_SEP}{message_id}"


def _decode_reel_id(reel_id: str) -> tuple[str, int]:
    channel, _, raw = reel_id.rpartition(_REEL_SEP)
    return channel, int(raw)


class TelegramFeed(FeedSource):
    """Walks seeded channels' recent messages; reads each message's replies as
    comments. Read-only: engagement methods stay the FeedSource no-ops."""

    def __init__(self, *, client: TelegramClientPort, channels: Sequence[str],
                 per_channel: int = DEFAULT_PER_CHANNEL):
        self._client = client
        self._channels = tuple(channels)
        self._per_channel = per_channel

    def attach(self) -> None:
        """No-op hook for symmetry with CDPFeed; the client connects itself
        (the Telethon adapter connects in its constructor / from_env)."""

    def walk(self) -> Iterator[Reel]:
        for channel in self._channels:
            n = 0
            for msg in self._client.iter_channel_messages(channel, self._per_channel):
                n += 1
                yield Reel(reel_id=_encode_reel_id(channel, msg.id),
                           caption=msg.text or "", author=channel)
            log.info("Telegram channel · %s · %d message(s)", channel, n)

    def fetch_comments(self, reel_id: str, since_cursor: Optional[str]
                       ) -> tuple[list[Comment], Optional[str]]:
        channel, message_id = _decode_reel_id(reel_id)
        min_id = int(since_cursor) if since_cursor else 0
        comments: list[Comment] = []
        max_id = min_id
        for reply in self._client.iter_replies(channel, message_id, min_id):
            comments.append(Comment(
                comment_id=f"{reel_id}{_REEL_SEP}{reply.id}",
                username=reply.sender or "",
                text=reply.text or "",
                is_reply=True,
            ))
            max_id = max(max_id, reply.id)
        # Forward-only watermark: the highest reply id seen, so the next poll only
        # pulls newer replies. Unchanged when there were none.
        new_cursor = str(max_id) if max_id != min_id else since_cursor
        log.debug("Telegram replies · reel=%s n=%d", reel_id, len(comments))
        return comments, new_cursor

    def capture_frames(self, reel: Reel, n: int = 3) -> list[str]:
        return []  # text-first platform; vision only matters for image posts (v2)

    def healthy(self) -> bool:
        return self._client.connected()


class TelegramBotClient:
    """Live **Bot API** adapter (the operator's chosen approach).

    A bot reads only chats it is a member/admin of, and only NEW messages via
    `getUpdates` — it cannot browse a public channel's history the way a user
    (MTProto) session can. So the operator adds the bot as admin to the channels
    / discussion groups to monitor; this adapter drains the pending updates once,
    buckets them into channel posts (→ messages) and their replies, and serves
    the `TelegramClientPort` the feed expects.

    HTTP is injectable (`http_get`) so the bucketing logic is unit-tested with a
    fake; the live path uses httpx (lazy import — no hard dependency).
    """

    _API = "https://api.telegram.org"

    def __init__(self, token: str, *, http_get=None, drain_pages: int = 10):
        self._token = token
        self._drain_pages = drain_pages
        self._get = http_get or self._httpx_get
        self._ok = False
        self._posts: dict[str, list[TgMessage]] = {}
        self._replies: dict[tuple[str, int], list[TgMessage]] = {}
        self._drained = False

    @classmethod
    def from_env(cls) -> "TelegramBotClient":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            raise RuntimeError(
                "Telegram live run needs TELEGRAM_BOT_TOKEN in the environment "
                "(.env) — create a bot via @BotFather and add it as admin to the "
                "channels/groups you want to monitor")
        return cls(token)

    def _httpx_get(self, url: str, params: dict) -> dict:  # pragma: no cover - live only
        import httpx
        r = httpx.get(url, params=params, timeout=30.0)
        r.raise_for_status()
        return r.json()

    def _drain(self) -> None:
        """Pull all pending updates once, bucketing posts and replies by chat.

        Bot API updates are forward-only; offset advances past acknowledged
        updates. Re-runs that re-see a message are harmless — matches are
        idempotent on comment_id (chat/message id)."""
        if self._drained:
            return
        offset: Optional[int] = None
        for _ in range(self._drain_pages):
            params = {"timeout": 0, "limit": 100, "allowed_updates": '["message","channel_post"]'}
            if offset is not None:
                params["offset"] = offset
            body = self._get(f"{self._API}/bot{self._token}/getUpdates", params)
            self._ok = bool(body.get("ok"))
            updates = body.get("result") or []
            if not updates:
                break
            for u in updates:
                self._ingest(u)
                offset = u.get("update_id", 0) + 1
            if len(updates) < 100:
                break
        self._drained = True

    def _ingest(self, update: dict) -> None:
        msg = update.get("message") or update.get("channel_post")
        if not isinstance(msg, dict):
            return
        chat = msg.get("chat") or {}
        chat_key = _chat_key(chat)
        tg = TgMessage(id=int(msg.get("message_id", 0)),
                       text=msg.get("text") or msg.get("caption") or "",
                       sender=_bot_sender(msg, chat))
        reply_to = msg.get("reply_to_message")
        if isinstance(reply_to, dict) and reply_to.get("message_id"):
            key = (chat_key, int(reply_to["message_id"]))
            self._replies.setdefault(key, []).append(tg)
        else:
            self._posts.setdefault(chat_key, []).append(tg)

    def connected(self) -> bool:
        self._drain()
        return self._ok

    def iter_channel_messages(self, channel: str, limit: int) -> Iterable[TgMessage]:
        self._drain()
        return list(self._posts.get(_normalize_channel(channel), []))[:limit]

    def iter_replies(self, channel: str, message_id: int,
                     min_id: int) -> Iterable[TgMessage]:
        self._drain()
        replies = self._replies.get((_normalize_channel(channel), message_id), [])
        return [r for r in replies if r.id > min_id]


def _normalize_channel(channel: str) -> str:
    return channel.lstrip("@")


def _chat_key(chat: dict) -> str:
    username = chat.get("username")
    if username:
        return str(username)
    cid = chat.get("id")
    return str(cid) if cid is not None else ""


def _bot_sender(msg: dict, chat: dict) -> Optional[str]:
    frm = msg.get("from") or {}
    if frm.get("username"):
        return str(frm["username"])
    if frm.get("id") is not None:
        return str(frm["id"])
    # channel posts carry no `from` — attribute to the channel itself.
    return chat.get("username") or chat.get("title")


class TelethonClient:
    """Live MTProto adapter. Telethon is imported lazily so the dependency is
    only required for an actual Telegram run, not for import/tests."""

    def __init__(self, api_id: int, api_hash: str, session: str):
        try:
            from telethon.sessions import StringSession
            from telethon.sync import TelegramClient
        except Exception as e:  # pragma: no cover - exercised only on a live run
            raise RuntimeError(
                "telethon is required for live Telegram runs: pip install telethon"
            ) from e
        self._client = TelegramClient(StringSession(session), api_id, api_hash)
        self._client.connect()

    @classmethod
    def from_env(cls) -> "TelethonClient":
        """Build from TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_SESSION.

        Read-only login uses a pre-authorized StringSession (warmed by the
        operator out of band, per PRD §10) — the engine never logs in interactively.
        """
        api_id = os.environ.get("TELEGRAM_API_ID", "")
        api_hash = os.environ.get("TELEGRAM_API_HASH", "")
        session = os.environ.get("TELEGRAM_SESSION", "")
        if not (api_id.isdigit() and api_hash and session):
            raise RuntimeError(
                "Telegram live run needs TELEGRAM_API_ID, TELEGRAM_API_HASH, "
                "TELEGRAM_SESSION in the environment (.env)")
        return cls(int(api_id), api_hash, session)

    @classmethod
    def from_credentials(cls, credentials: Optional[dict]) -> "TelethonClient":
        """Build from a per-org stored secret dict ({api_id, api_hash, session}).
        Loud (and per-campaign foldable) when any field is missing/invalid."""
        creds = credentials or {}
        raw_id = creds.get("api_id")
        try:
            api_id = int(raw_id)
        except (TypeError, ValueError):
            api_id = 0
        api_hash = str(creds.get("api_hash") or "")
        session = str(creds.get("session") or "")
        if not (api_id and api_hash and session):
            raise RuntimeError(
                "Telegram credentials missing api_id/api_hash/session — "
                "reconnect Telegram in Settings")
        return cls(api_id, api_hash, session)

    def connected(self) -> bool:
        return bool(self._client.is_connected())

    def iter_channel_messages(self, channel: str, limit: int) -> Iterable[TgMessage]:
        for m in self._client.iter_messages(channel, limit=limit):
            yield TgMessage(id=m.id, text=getattr(m, "message", "") or "",
                            sender=_sender_handle(m))

    def iter_replies(self, channel: str, message_id: int,
                     min_id: int) -> Iterable[TgMessage]:
        # Replies live in the channel's linked discussion group; Telethon exposes
        # them via reply_to + a forward-only min_id watermark.
        for m in self._client.iter_messages(channel, reply_to=message_id, min_id=min_id):
            yield TgMessage(id=m.id, text=getattr(m, "message", "") or "",
                            sender=_sender_handle(m))


def _sender_handle(message: object) -> Optional[str]:
    """Best-effort human-readable sender for a Telethon message."""
    sender = getattr(message, "sender", None)
    if sender is None:
        return None
    username = getattr(sender, "username", None)
    if username:
        return str(username)
    sid = getattr(sender, "id", None)
    return str(sid) if sid is not None else None
