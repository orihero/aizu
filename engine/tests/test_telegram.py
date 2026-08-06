"""TelegramFeed mapping (PRD: docs/prd/telegram-lead-agent-PRD.md).

Telethon is isolated behind TelegramClientPort, so the message→Reel /
reply→Comment mapping and the forward-only reply cursor are tested with a fake
client — no Telethon install, no live session.
"""
from aizu.engines.telegram.feed import TelegramFeed, TgMessage


class FakeTelegramClient:
    def __init__(self, messages, replies, connected=True):
        # messages: {channel: [TgMessage, ...]}
        # replies:  {(channel, message_id): [TgMessage, ...]}
        self._messages = messages
        self._replies = replies
        self._connected = connected

    def connected(self):
        return self._connected

    def iter_channel_messages(self, channel, limit):
        return list(self._messages.get(channel, []))[:limit]

    def iter_replies(self, channel, message_id, min_id):
        return [r for r in self._replies.get((channel, message_id), []) if r.id > min_id]


def test_walk_maps_channel_messages_to_reels():
    client = FakeTelegramClient(
        messages={"product_chat": [TgMessage(10, "Acme app demo"), TgMessage(11, "Acme pricing")]},
        replies={})
    feed = TelegramFeed(client=client, channels=["product_chat"])
    reels = list(feed.walk())
    assert [r.reel_id for r in reels] == ["product_chat/10", "product_chat/11"]
    assert reels[0].caption == "Acme app demo"
    assert reels[0].author == "product_chat"


def test_fetch_comments_maps_replies_and_advances_cursor():
    client = FakeTelegramClient(
        messages={},
        replies={("product_chat", 10): [
            TgMessage(101, "How much?", sender="dana"),
            TgMessage(102, "+1 415 555 0142", sender="dana"),
        ]})
    feed = TelegramFeed(client=client, channels=["product_chat"])
    comments, cursor = feed.fetch_comments("product_chat/10", since_cursor=None)
    assert [c.comment_id for c in comments] == ["product_chat/10/101", "product_chat/10/102"]
    assert comments[0].username == "dana" and comments[0].is_reply
    assert cursor == "102"   # forward-only watermark = highest reply id


def test_fetch_comments_cursor_pulls_only_newer_replies():
    client = FakeTelegramClient(
        messages={},
        replies={("product_chat", 10): [TgMessage(101, "old"), TgMessage(205, "new")]})
    feed = TelegramFeed(client=client, channels=["product_chat"])
    comments, cursor = feed.fetch_comments("product_chat/10", since_cursor="101")
    assert [c.comment_id for c in comments] == ["product_chat/10/205"]
    assert cursor == "205"


def test_fetch_comments_no_replies_keeps_cursor():
    client = FakeTelegramClient(messages={}, replies={})
    feed = TelegramFeed(client=client, channels=["product_chat"])
    comments, cursor = feed.fetch_comments("product_chat/10", since_cursor="55")
    assert comments == [] and cursor == "55"


def test_healthy_reflects_client_connection():
    feed = TelegramFeed(client=FakeTelegramClient({}, {}, connected=False), channels=["c"])
    assert feed.healthy() is False
    feed2 = TelegramFeed(client=FakeTelegramClient({}, {}, connected=True), channels=["c"])
    assert feed2.healthy() is True


def test_read_only_engagement_is_noop():
    feed = TelegramFeed(client=FakeTelegramClient({}, {}), channels=["c"])
    from aizu.core.feed import Reel
    reel = Reel(reel_id="c/1")
    assert feed.like_reel(reel) is False
    assert feed.follow_author(reel) is False
    assert feed.detect_action_block() is False
    assert feed.capture_frames(reel) == []


def test_bot_client_buckets_posts_and_replies_from_updates():
    """The Bot API adapter drains getUpdates once and buckets channel posts vs
    discussion replies, fulfilling the TelegramClientPort the feed expects."""
    from aizu.engines.telegram.feed import TelegramBotClient

    pages = iter([
        {"ok": True, "result": [
            {"update_id": 1, "channel_post": {
                "message_id": 10, "text": "Acme app demo",
                "chat": {"username": "product_chat", "type": "channel"}}},
            {"update_id": 2, "message": {
                "message_id": 55, "text": "How much?",
                "chat": {"username": "product_chat", "type": "supergroup"},
                "from": {"username": "aziz"},
                "reply_to_message": {"message_id": 10}}},
        ]},
        {"ok": True, "result": []},   # drained
    ])

    def fake_get(url, params):
        return next(pages)

    client = TelegramBotClient("TOKEN", http_get=fake_get)
    posts = list(client.iter_channel_messages("product_chat", 40))
    assert [m.id for m in posts] == [10]
    assert posts[0].text == "Acme app demo"

    replies = list(client.iter_replies("product_chat", 10, min_id=0))
    assert [r.id for r in replies] == [55]
    assert replies[0].sender == "aziz"
    assert client.connected() is True


def test_bot_client_normalizes_at_prefix_and_filters_min_id():
    from aizu.engines.telegram.feed import TelegramBotClient

    body = {"ok": True, "result": [
        {"update_id": 1, "message": {
            "message_id": 7, "text": "old", "chat": {"username": "product_chat", "type": "supergroup"},
            "reply_to_message": {"message_id": 10}}},
        {"update_id": 2, "message": {
            "message_id": 99, "text": "new", "chat": {"username": "product_chat", "type": "supergroup"},
            "reply_to_message": {"message_id": 10}}},
    ]}
    calls = iter([body, {"ok": True, "result": []}])
    client = TelegramBotClient("TOKEN", http_get=lambda url, params: next(calls))
    # '@product_chat' must resolve to the same chat as 'product_chat', and min_id filters.
    replies = list(client.iter_replies("@product_chat", 10, min_id=7))
    assert [r.id for r in replies] == [99]


def test_session_runs_end_to_end_on_telegram(tmp_path):
    """The whole stack (feed → cascade → store) runs on a non-Instagram platform
    with zero Instagram-specific code on the path, and matches land tagged
    platform='telegram' (multi-platform plan verification)."""
    import os
    import tempfile

    from aizu.core.config import load_campaign, load_soul
    from aizu.core.mock_router import MockRouter
    from aizu.core.pacing import PacingConfig, Pacer
    from aizu.engines.instagram.session import Session, SessionConfig
    from aizu.core.store import Store

    campaign_md = tmp_path / "campaign.md"
    campaign_md.write_text(
        "```yaml\ncampaign_id: tg-leadgen\ngoal: lead\nthreshold: 0.7\n"
        "escalate_band: [0.4, 0.75]\nplatform: telegram\n"
        "seed_channels: [product_chat]\n```\n"
        "## Relevance\nsaas product\n## Match\nbuyer intent\n## Extract\n- phone\n",
        encoding="utf-8")
    from pathlib import Path
    soul = load_soul(Path(__file__).resolve().parents[1] / "config" / "soul.md")
    campaign = load_campaign(campaign_md)
    assert campaign.platform == "telegram"

    client = FakeTelegramClient(
        messages={"product_chat": [TgMessage(10, "Acme app demo, free trial")]},
        replies={("product_chat", 10): [
            TgMessage(101, "How much is the Pro plan? +1 415 555 0142", sender="dana"),
        ]})
    feed = TelegramFeed(client=client, channels=["product_chat"])

    fd, db = tempfile.mkstemp(suffix=".db"); os.close(fd)
    store = Store(db)
    Session(store=store, router=MockRouter(store=store), feed=feed,
            soul=soul, campaign=campaign,
            pacer=Pacer(cfg=PacingConfig(enforce_daytime=False), sleep=lambda _t: None),
            cfg=SessionConfig()).run()

    rows = store.matches("tg-leadgen")
    store.close()
    assert rows, "expected at least one telegram lead"
    assert all(r["platform"] == "telegram" for r in rows)
    assert any("555" in (r["extracted"].get("phone") or "") for r in rows)
