"""Phase 3 — the server-side Telegram MTProto login wizard handshake.

Drives TelegramLoginManager against a FAKE Telethon client (no network, no
telethon import): the happy path, the 2FA password branch, and pending-token
expiry/TTL.
"""
import types

import pytest

from aizu.engines.telegram.login import TelegramLoginError, TelegramLoginManager


class SessionPasswordNeededError(Exception):
    """Same class name Telethon raises; the manager matches on __name__."""


class FakeTelethonClient:
    def __init__(self, *, password_needed: bool = False):
        self._password_needed = password_needed
        self.sign_ins: list[tuple] = []
        self.connected = True

    def send_code_request(self, phone):
        return types.SimpleNamespace(phone_code_hash=f"hash-{phone}")

    def sign_in(self, *args, **kwargs):
        if "password" in kwargs:
            self.sign_ins.append(("password", kwargs["password"]))
            return
        self.sign_ins.append(("code", args[1] if len(args) > 1 else None))
        if self._password_needed:
            raise SessionPasswordNeededError()

    def disconnect(self):
        self.connected = False


def _manager(client, *, ttl_seconds=300.0) -> TelegramLoginManager:
    return TelegramLoginManager(
        client_factory=lambda api_id, api_hash: client,
        session_saver=lambda c: "STRING_SESSION",
        api_provider=lambda: (42, "app-hash"),
        ttl_seconds=ttl_seconds,
    )


def test_start_sends_code_and_stashes_pending():
    client = FakeTelethonClient()
    mgr = _manager(client)
    token = mgr.start("+14155550142")
    assert isinstance(token, str) and token
    assert mgr.pending_count() == 1


def test_start_requires_a_phone():
    mgr = _manager(FakeTelethonClient())
    with pytest.raises(TelegramLoginError, match="phone"):
        mgr.start("  ")


def test_verify_happy_path_yields_the_session_secret():
    client = FakeTelethonClient()
    mgr = _manager(client)
    token = mgr.start("+14155550142")

    result = mgr.verify(token, "12345")

    assert result["connected"] is True
    assert result["session"] == {"api_id": 42, "api_hash": "app-hash", "session": "STRING_SESSION"}
    assert mgr.pending_count() == 0                    # consumed on success
    assert client.sign_ins == [("code", "12345")]


def test_verify_two_factor_branch():
    client = FakeTelethonClient(password_needed=True)
    mgr = _manager(client)
    token = mgr.start("+14155550142")

    # first attempt with just the code → password required, pending survives
    first = mgr.verify(token, "12345")
    assert first == {"connected": False, "needs_password": True}
    assert mgr.pending_count() == 1

    # resubmit with the 2FA password → success
    second = mgr.verify(token, "12345", password="hunter2")
    assert second["connected"] is True
    assert second["session"]["session"] == "STRING_SESSION"
    assert ("password", "hunter2") in client.sign_ins
    assert mgr.pending_count() == 0


def test_verify_unknown_or_expired_token_is_loud():
    client = FakeTelethonClient()
    mgr = _manager(client, ttl_seconds=-1.0)           # any pending is already stale
    token = mgr.start("+14155550142")
    with pytest.raises(TelegramLoginError, match="expired"):
        mgr.verify(token, "12345")
    assert mgr.pending_count() == 0                    # pruned


def test_send_code_failure_surfaces_as_login_error():
    class Boom(FakeTelethonClient):
        def send_code_request(self, phone):
            raise RuntimeError("flood wait")
    mgr = _manager(Boom())
    with pytest.raises(TelegramLoginError, match="could not send"):
        mgr.start("+14155550142")
