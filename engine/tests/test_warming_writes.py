"""WarmingSession + executor integration (warming-writes PRD §3.3–§3.7, §4).

The observe stage stays dwell-only; from light/ramp/sustain the session dwells on
every observed reel, then (if relevant) lets the executor act under per-day caps.
Action-block halts FLAG the account and raise. Mid-session daytime/kill re-checks
stop further writes.
"""
import os
import random
import tempfile

import pytest

from aizu import dispatch
from aizu.core import accounts as accounts_lib
from aizu.core.accounts import FLAGGED, warming_sentinel_campaign
from aizu.core.config import Campaign
from aizu.core.feed import FeedSource, Reel
from aizu.core.pacing import Pacer, PacingConfig
from aizu.core.store import Store
from aizu.engines.base import HaltSession
from aizu.engines.warming.session import WarmingSession

_DAY = 86400.0


def fresh_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path), path


def _campaign(seed_hashtags=None):
    return Campaign(
        campaign_id="camp1", goal="g", threshold=0.7, escalate_band=(0.4, 0.75),
        language_mix=[], relevance_def="", match_def="", extract_def="",
        seed_direction="", raw="", path=__import__("pathlib").Path("<test>"),
        platform="instagram", engine_mode="warming",
        seed_hashtags=seed_hashtags if seed_hashtags is not None else ["marketing"])


class RecordingFeed(FeedSource):
    def __init__(self, reels, *, block=False, healthy=True):
        self._reels = reels
        self.likes: list[str] = []
        self.saves: list[str] = []
        self.follows: list[str] = []
        self._block = block
        self._healthy = healthy

    def walk(self):
        yield from self._reels

    def healthy(self):
        return self._healthy

    def like_reel(self, reel):
        self.likes.append(reel.reel_id)
        return True

    def save_reel(self, reel):
        self.saves.append(reel.reel_id)
        return True

    def follow_author(self, reel):
        self.follows.append(reel.author)
        return True

    def detect_action_block(self):
        return self._block


def _relevant_reels(n=12):
    return [Reel(reel_id=f"r{i}", author="growthlab",
                 caption="marketing growth tips") for i in range(n)]


def _no_sleep_pacer(seed=0):
    return Pacer(cfg=PacingConfig(enforce_daytime=False),
                 rng=random.Random(seed), sleep=lambda _t: None)


def _ramp_account(store, now):
    """An IG account old enough (10 days) to sit in the ramp stage."""
    aid = store.add_account(1, "instagram", "acct")
    # added_at is set at provision to ~now; rewind it so ramp_day lands in ramp(8-14).
    store._conn.execute("UPDATE accounts SET added_at=? WHERE id=?",
                        (now - 10 * _DAY, aid))
    store._conn.commit()
    return aid


def _run(store, feed, *, now, pacer=None):
    session = WarmingSession(
        store=store, feed=feed, campaign=_campaign(),
        account=store.get_account(_account_id(store)), org_id=1,
        pacer=pacer or _no_sleep_pacer(), now=now)
    return session


def _account_id(store):
    return store.list_accounts(1, "instagram")[0]["id"]


# ---- observe stage stays dwell-only ----

def test_observe_stage_emits_zero_writes():
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp1", org_id=1, status="live")
    now = 1_700_000_000.0
    aid = store.add_account(1, "instagram", "acct")  # added_at≈now → observe stage
    store._conn.execute("UPDATE accounts SET added_at=? WHERE id=?", (now, aid))
    store._conn.commit()
    feed = RecordingFeed(_relevant_reels())
    session = WarmingSession(store=store, feed=feed, campaign=_campaign(),
                             account=store.get_account(aid), org_id=1,
                             pacer=_no_sleep_pacer(), now=now)
    session.run()
    assert feed.likes == [] and feed.saves == [] and feed.follows == []
    n = store._conn.execute("SELECT COUNT(*) AS n FROM actions").fetchone()["n"]
    assert n == 0


# ---- ramp stage writes ----

def test_ramp_stage_fires_and_logs_writes():
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp1", org_id=1, status="live")
    now = 1_700_000_000.0
    aid = _ramp_account(store, now)
    feed = RecordingFeed(_relevant_reels(20))
    session = WarmingSession(store=store, feed=feed, campaign=_campaign(),
                             account=store.get_account(aid), org_id=1,
                             pacer=_no_sleep_pacer(seed=1), now=now)
    summary = session.run()
    assert summary["halt_reason"] is None
    assert len(feed.likes) > 0, "likes (p=0.70) should fire at ramp stage"
    rows = store._conn.execute(
        "SELECT campaign_id, account_id FROM actions WHERE succeeded=1 LIMIT 1"
    ).fetchone()
    assert rows["campaign_id"] == warming_sentinel_campaign(1)
    assert rows["account_id"] == aid


def test_ramp_stage_skips_off_topic_reels():
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp1", org_id=1, status="live")
    now = 1_700_000_000.0
    aid = _ramp_account(store, now)
    off = [Reel(reel_id=f"o{i}", author="cats", caption="kittens") for i in range(20)]
    feed = RecordingFeed(off)
    session = WarmingSession(store=store, feed=feed, campaign=_campaign(),
                             account=store.get_account(aid), org_id=1,
                             pacer=_no_sleep_pacer(seed=1), now=now)
    session.run()
    assert feed.likes == [] and feed.saves == [] and feed.follows == []


# ---- action-block halt -> FLAGGED + raise_flag ----

def test_action_block_flags_account_and_raises():
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp1", org_id=1, status="live")
    now = 1_700_000_000.0
    aid = _ramp_account(store, now)
    feed = RecordingFeed(_relevant_reels(20), block=True)
    session = WarmingSession(store=store, feed=feed, campaign=_campaign(),
                             account=store.get_account(aid), org_id=1,
                             pacer=_no_sleep_pacer(seed=1), now=now)
    with pytest.raises(HaltSession):
        session.run()
    assert store.get_account(aid)["state"] == FLAGGED
    flag = store._conn.execute(
        "SELECT kind, severity, account_id FROM health_flags "
        "WHERE account_id=?", (aid,)).fetchone()
    assert flag["kind"] == "account_challenge"
    assert flag["severity"] == "halt"
    sess = store._conn.execute(
        "SELECT status FROM sessions").fetchone()
    assert sess["status"] == "halted"


# ---- mid-session daytime close stops further writes ----

def test_daytime_close_mid_session_stops_writes():
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp1", org_id=1, status="live")
    now = 1_700_000_000.0
    aid = _ramp_account(store, now)
    feed = RecordingFeed(_relevant_reels(20))

    # is_daytime() True on the start guard + window-1 top check (calls 1,2), then
    # False at window-2's top check (call 3) — closing writes after window 1. The
    # feed snapshots its like count the moment daytime flips, so we can prove NO
    # further likes accrue after the close (not just "fewer than a baseline").
    calls = {"n": 0}
    closed_at = {"likes": None}

    class FlipPacer(Pacer):
        def is_daytime(self):
            calls["n"] += 1
            alive = calls["n"] <= 2
            if not alive and closed_at["likes"] is None:
                closed_at["likes"] = len(feed.likes)
            return alive

    pacer = FlipPacer(cfg=PacingConfig(enforce_daytime=False),
                      rng=random.Random(1), sleep=lambda _t: None)
    session = WarmingSession(store=store, feed=feed, campaign=_campaign(),
                             account=store.get_account(aid), org_id=1,
                             pacer=pacer, now=now)
    summary = session.run()
    # The run completes (not a halt) but issues ZERO writes after daytime closed:
    # the like count at the close equals the final like count (frozen thereafter).
    assert summary["halt_reason"] is None
    assert closed_at["likes"] is not None, "daytime must have closed mid-session"
    assert len(feed.likes) == closed_at["likes"], \
        "no likes may fire after the daytime window closes mid-session"


def test_summary_and_detail_report_fired_warming_actions_real_clock():
    """Reviewer-1 gap: under the REAL clock, log_action stamps the same day bucket
    that action_counts_for_account_day reads, so summary['likes'] and the
    accounts.detail warming counters reflect the writes actually fired."""
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp1", org_id=1, status="live")
    import time
    now = time.time()
    aid = _ramp_account(store, now)
    feed = RecordingFeed(_relevant_reels(20))
    # now=None → the session and executor both use the real wall clock.
    session = WarmingSession(store=store, feed=feed, campaign=_campaign(),
                             account=store.get_account(aid), org_id=1,
                             pacer=_no_sleep_pacer(seed=1), now=None)
    summary = session.run()
    assert summary["halt_reason"] is None
    assert summary["likes"] > 0, "fired likes must surface in the run summary"
    assert summary["likes"] == len(feed.likes)
    detail = store.get_account(aid)["detail"]
    assert detail["warming_likes"] == len(feed.likes)
