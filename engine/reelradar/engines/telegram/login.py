"""Server-side Telegram MTProto login wizard (Phase 3).

Telegram has no Bot-API path to third-party channels, so connecting a customer
means a one-time MTProto *user* login: phone → code → optional 2FA password.
This module holds the short-lived pending logins (TTL ~5 min) keyed by an opaque
token, drives the send_code → sign_in handshake via Telethon, and yields a
reusable StringSession on success. The code-entry step is Telegram's anti-abuse
control and cannot be automated; everything after the one-time login is
non-interactive.

The bridge is single-process (ThreadingHTTPServer), so an in-memory map guarded
by a lock is sufficient. The Telethon client + session saver are injected so
tests drive the whole handshake with a fake (no network, no telethon import).
"""
from __future__ import annotations

import asyncio
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

PENDING_TTL_SECONDS = 5 * 60
_TOKEN_BYTES = 24
_LOOP_SHUTDOWN_TIMEOUT_SECONDS = 5


class TelegramLoginError(RuntimeError):
    """A wizard step failed (missing app creds, bad phone/code, expired token)."""


@dataclass
class _Pending:
    client: Any
    phone: str
    phone_code_hash: str
    created_at: float
    awaiting_password: bool = False


def _api_from_env() -> tuple[int, str]:
    """App-level TELEGRAM_API_ID / TELEGRAM_API_HASH (yours) drive the handshake;
    the per-org secret stores the resulting session + these ids for the run."""
    api_id = os.environ.get("TELEGRAM_API_ID", "")
    api_hash = os.environ.get("TELEGRAM_API_HASH", "")
    if not (api_id.isdigit() and api_hash):
        raise TelegramLoginError(
            "TELEGRAM_API_ID / TELEGRAM_API_HASH must be set on the server to run "
            "the Telegram login")
    return int(api_id), api_hash


class _LoopBoundClient:
    """Pins one async Telethon client to a single dedicated thread + event loop for
    its whole lifetime and marshals every call back onto that loop.

    Why this exists: the panel bridge is a ThreadingHTTPServer, so the send_code
    step (start) and the sign_in step (verify) are handled on *different* worker
    threads. Telethon binds a live connection to the event loop it connected on and
    refuses to let it migrate ("The asyncio event loop must not change after
    connection"). Owning the loop here makes the calling thread irrelevant."""

    def __init__(self, api_id: int, api_hash: str):
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run_loop, args=(ready,),
            name="telegram-login-loop", daemon=True)
        self._thread.start()
        ready.wait()
        self._client = self._submit(self._connect(api_id, api_hash))

    def _run_loop(self, ready: threading.Event) -> None:
        asyncio.set_event_loop(self._loop)
        ready.set()
        self._loop.run_forever()

    def _submit(self, coro: Any) -> Any:
        """Run a coroutine on the owning loop and block for its result, re-raising
        any exception (so SessionPasswordNeededError reaches the manager intact)."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    async def _connect(self, api_id: int, api_hash: str) -> Any:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        client = TelegramClient(StringSession(), api_id, api_hash)
        await client.connect()
        return client

    @property
    def session(self) -> Any:
        return self._client.session

    def send_code_request(self, phone: str) -> Any:
        return self._submit(self._client.send_code_request(phone))

    def sign_in(self, *args: Any, **kwargs: Any) -> Any:
        return self._submit(self._client.sign_in(*args, **kwargs))

    def disconnect(self) -> None:
        try:
            self._submit(self._client.disconnect())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=_LOOP_SHUTDOWN_TIMEOUT_SECONDS)
            self._loop.close()


def _default_client_factory(api_id: int, api_hash: str) -> Any:
    """A connected (not-yet-authorized) Telethon StringSession client, pinned to its
    own thread + event loop so the handshake survives the ThreadingHTTPServer
    handing start/verify to different threads. Lazy import keeps telethon optional
    until a Telegram login actually runs."""
    try:
        import telethon  # noqa: F401  (import-time check; real import is in _connect)
    except Exception as e:  # pragma: no cover - exercised only on a live server
        raise TelegramLoginError(
            "telethon is required for Telegram login: pip install telethon") from e
    return _LoopBoundClient(api_id, api_hash)


def _default_session_saver(client: Any) -> str:
    from telethon.sessions import StringSession
    return StringSession.save(client.session)


def _is_password_needed(exc: Exception) -> bool:
    """True for Telethon's SessionPasswordNeededError without importing it."""
    return type(exc).__name__ == "SessionPasswordNeededError"


class TelegramLoginManager:
    """Owns the pending-login map and the send_code → sign_in handshake."""

    def __init__(self, *,
                 client_factory: Callable[[int, str], Any] = _default_client_factory,
                 session_saver: Callable[[Any], str] = _default_session_saver,
                 api_provider: Callable[[], tuple[int, str]] = _api_from_env,
                 ttl_seconds: float = PENDING_TTL_SECONDS):
        self._factory = client_factory
        self._save_session = session_saver
        self._api = api_provider
        self._ttl = ttl_seconds
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()

    def start(self, phone: str) -> str:
        """Send a login code to `phone`; stash the pending client and return the
        opaque wizard token. Raises TelegramLoginError on a missing creds/phone."""
        phone = (phone or "").strip()
        if not phone:
            raise TelegramLoginError("phone number is required")
        api_id, api_hash = self._api()
        client = self._factory(api_id, api_hash)
        try:
            sent = client.send_code_request(phone)
        except Exception as e:
            _safe_disconnect(client)
            raise TelegramLoginError(f"could not send the login code: {e}") from e
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        with self._lock:
            self._prune_locked()
            self._pending[token] = _Pending(
                client=client, phone=phone,
                phone_code_hash=getattr(sent, "phone_code_hash", "") or "",
                created_at=time.time())
        return token

    def verify(self, token: str, code: str, password: Optional[str] = None) -> dict:
        """Finish the login. Returns one of:
          - {"needs_password": True}  — 2FA required; resubmit with the password
          - {"connected": True, "session": {api_id, api_hash, session}} — success

        The pending entry survives a needs_password response (the code was already
        consumed) and is dropped on success or expiry."""
        with self._lock:
            self._prune_locked()
            pending = self._pending.get(token)
        if pending is None:
            raise TelegramLoginError("login session expired — start again")
        api_id, api_hash = self._api()
        try:
            done = self._sign_in(pending, code, password)
        except TelegramLoginError:
            raise
        except Exception as e:
            raise TelegramLoginError(f"could not verify the login code: {e}") from e
        if not done:
            return {"connected": False, "needs_password": True}
        session = self._save_session(pending.client)
        with self._lock:
            self._pending.pop(token, None)
        _safe_disconnect(pending.client)
        return {"connected": True, "needs_password": False,
                "session": {"api_id": api_id, "api_hash": api_hash, "session": session}}

    def _sign_in(self, pending: _Pending, code: str, password: Optional[str]) -> bool:
        """Drive one sign-in attempt. Returns True when authorized, False when a
        2FA password is still required."""
        if pending.awaiting_password:
            if not password:
                return False
            pending.client.sign_in(password=password)
            return True
        try:
            pending.client.sign_in(pending.phone, code,
                                   phone_code_hash=pending.phone_code_hash)
        except Exception as e:
            if not _is_password_needed(e):
                raise
            pending.awaiting_password = True
            if not password:
                return False
            pending.client.sign_in(password=password)
        return True

    def _prune_locked(self) -> None:
        cutoff = time.time() - self._ttl
        for tok in [t for t, p in self._pending.items() if p.created_at < cutoff]:
            _safe_disconnect(self._pending.pop(tok).client)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)


def _safe_disconnect(client: Any) -> None:
    disconnect = getattr(client, "disconnect", None)
    if callable(disconnect):
        try:
            disconnect()
        except Exception:  # noqa: BLE001 - cleanup must never raise
            pass
