"""Telegram session loop — deterministic, text-only, read-only.

Asserts the loop maps channel messages→matches under platform='telegram', NEVER
invokes vision, streams run-activity events when a run_id is set, respects the
lead target and already-seen dedup, and reports a uniform read-only summary.
"""
import os
import sqlite3
import tempfile

from reelradar.core.config import campaign_from_brief
from reelradar.core.router import Decision
from reelradar.engines.telegram.feed import TelegramFeed, TgMessage
from reelradar.engines.telegram.session import run_session


class SpyRouter:
    def __init__(self):
        self.image_calls = []

    def classify_text(self, *, instruction, content, campaign_id, stage,
                      session_id=None, system=None):
        low = content.lower()
        if stage == "relevance":
            relevant = any(k in low for k in ("acme", "app", "saas", "demo"))
            return Decision(label="relevant" if relevant else "irrelevant",
                            score=0.9 if relevant else 0.1, confidence=0.96)
        is_lead = any(k in low for k in ("pricing", "+1", "price", "buy"))
        return Decision(label="yes" if is_lead else "no",
                        score=0.92 if is_lead else 0.1, confidence=0.96,
                        extracted={"phone": "+14155550142"} if is_lead else {})

    def classify_image(self, *, instruction, images_b64, campaign_id, stage,
                       session_id=None, system=None):
        self.image_calls.append(stage)
        return Decision(label="irrelevant", score=0.0, confidence=1.0)


class FakeTgClient:
    """Implements TelegramClientPort with in-memory channels + replies."""

    def __init__(self, messages=None, replies=None):
        self._messages = messages or {}   # {channel: [TgMessage,...]}
        self._replies = replies or {}      # {(channel, message_id): [TgMessage,...]}

    def connected(self):
        return True

    def iter_channel_messages(self, channel, limit):
        return list(self._messages.get(channel, []))[:limit]

    def iter_replies(self, channel, message_id, min_id):
        return [r for r in self._replies.get((channel, message_id), []) if r.id > min_id]


def _store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from reelradar.core.store import Store
    return Store(path), path


def _campaign():
    return campaign_from_brief("tg-leadgen", {
        "platform": "telegram", "threshold": 0.7,
        "relevance_def": "saas product posts",
        "match_def": "a replier asking to buy / price / contact",
        "extract_def": "- phone",
    })


def _feed():
    client = FakeTgClient(
        messages={"@dev": [TgMessage(id=10, text="Acme app demo, free trial", sender="@dev"),
                           TgMessage(id=11, text="funny cats", sender="@dev")]},
        replies={("@dev", 10): [TgMessage(id=3, text="What's the pricing? +1 415 555 0142", sender="sam"),
                                TgMessage(id=4, text="🔥🔥", sender="bot")]},
    )
    return TelegramFeed(client=client, channels=["@dev"])


def _run(store, run_id=None, lead_target=None):
    return run_session(campaign=_campaign(), store=store, router=SpyRouter(),
                       feed=_feed(), soul=None, pacer=None, run_id=run_id,
                       lead_target=lead_target)


def test_session_maps_relevant_message_to_match():
    store, _ = _store()
    summary = _run(store)
    assert summary["reels_seen"] == 2          # message 10 relevant, 11 not
    assert summary["relevance_passes"] == 1
    assert summary["matches"] == 1
    rows = store.matches("tg-leadgen")
    assert rows and all(r["platform"] == "telegram" for r in rows)
    assert rows[0]["comment_id"] == "@dev/10/3"
    assert rows[0]["extracted"].get("phone") == "+14155550142"


def test_never_calls_vision():
    store, _ = _store()
    spy = SpyRouter()
    run_session(campaign=_campaign(), store=store, router=spy, feed=_feed(),
                soul=None, pacer=None)
    assert spy.image_calls == []


def test_summary_shape_is_readonly_and_unhalted():
    store, _ = _store()
    s = _run(store)
    assert s["feed_health_flag"] is False
    assert s["likes"] == 0 and s["follows"] == 0
    assert s["halt_reason"] is None


def test_lead_target_stops_early():
    store, _ = _store()
    client = FakeTgClient(
        messages={"@dev": [TgMessage(id=10, text="acme", sender="@dev"),
                           TgMessage(id=20, text="app", sender="@dev")]},
        replies={("@dev", 10): [TgMessage(id=3, text="price +1 1", sender="a")],
                 ("@dev", 20): [TgMessage(id=5, text="buy +1 2", sender="b")]},
    )
    feed = TelegramFeed(client=client, channels=["@dev"])
    summary = run_session(campaign=_campaign(), store=store, router=SpyRouter(),
                          feed=feed, soul=None, pacer=None, lead_target=1)
    assert summary["matches"] == 1
    assert summary["reels_seen"] == 1


def test_already_seen_dedup():
    store, _ = _store()
    _run(store)
    second = _run(store)
    assert second["already_seen_skips"] == 2
    assert second["relevance_passes"] == 0


def test_run_events_streamed_when_run_id_set():
    store, path = _store()
    _run(store, run_id="run-tg")
    con = sqlite3.connect(path)
    n = con.execute("SELECT count(*) FROM run_events WHERE run_id=?", ("run-tg",)).fetchone()[0]
    con.close()
    assert n >= 2


# ----- crash guard (session_crash_guard) -----------------------------------------

class CrashingTgFeed:
    """A feed whose walk() raises a generic exception mid-run (mirrors a live
    browser/network crash), to prove the crash guard terminalizes the session row."""

    def walk(self):
        raise RuntimeError("boom: chrome closed")

    def fetch_comments(self, reel_id, cursor):  # pragma: no cover - never reached
        return [], cursor


def _running_session_rows(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT session_id, status, halt_reason, ended_at FROM sessions").fetchall()
    con.close()
    return [dict(r) for r in rows]


def test_crash_guard_terminalizes_session_on_unexpected_error():
    store, path = _store()
    try:
        run_session(campaign=_campaign(), store=store, router=SpyRouter(),
                    feed=CrashingTgFeed(), soul=None, pacer=None)
        assert False, "expected the RuntimeError to propagate"
    except RuntimeError as e:
        assert "boom" in str(e)
    rows = _running_session_rows(path)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "halted"
    assert row["ended_at"] is not None
    assert row["halt_reason"].startswith("crashed:")
    assert "RuntimeError" in row["halt_reason"]
