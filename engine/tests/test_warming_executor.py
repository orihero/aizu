"""WarmingActionExecutor — probability rolls, per-day caps, relevance gate,
right-skewed delays, action-block halt (warming-writes PRD §3.3–§3.7, §4)."""
import os
import random
import tempfile

import pytest

from reelradar.core.accounts import warming_sentinel_campaign
from reelradar.core.config import Campaign
from reelradar.core.feed import FeedSource, Reel
from reelradar.core.pacing import Pacer, PacingConfig
from reelradar.core.store import Store
from reelradar.engines.base import HaltSession
from reelradar.engines.warming.executor import WarmingActionExecutor
from reelradar.engines.warming.ramp import budget_for_day


def fresh_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path), path


def _campaign(*, seed_hashtags=None, seed_accounts=None, relevance_def="",
              share_target=None):
    return Campaign(
        campaign_id="camp1", goal="g", threshold=0.7, escalate_band=(0.4, 0.75),
        language_mix=[], relevance_def=relevance_def, match_def="", extract_def="",
        seed_direction="", raw="", path=__import__("pathlib").Path("<test>"),
        platform="instagram", engine_mode="warming",
        seed_hashtags=seed_hashtags or [], seed_accounts=seed_accounts or [],
        share_target=share_target)


class RecordingFeed(FeedSource):
    """Records every write helper call; engagement succeeds. detect_action_block
    is configurable to drive the halt path."""

    def __init__(self, *, block=False):
        self.likes: list[str] = []
        self.follows: list[str] = []
        self.saves: list[str] = []
        self.shares: list[tuple] = []
        self._block = block
        self.block_checks = 0

    def walk(self):
        yield from ()

    def like_reel(self, reel):
        self.likes.append(reel.reel_id)
        return True

    def follow_author(self, reel):
        self.follows.append(reel.author)
        return True

    def save_reel(self, reel):
        self.saves.append(reel.reel_id)
        return True

    def share_reel(self, reel, target=None):
        self.shares.append((reel.reel_id, target))
        return True

    def detect_action_block(self) -> bool:
        self.block_checks += 1
        return self._block


def _no_sleep_pacer(seed=0):
    return Pacer(cfg=PacingConfig(enforce_daytime=False),
                 rng=random.Random(seed), sleep=lambda _t: None)


def _executor(store, feed, campaign, *, account_id, pacer=None, rng=None,
              now=None):
    acct = store.get_account(account_id)
    return WarmingActionExecutor(
        feed=feed, store=store,
        sentinel_campaign=warming_sentinel_campaign(acct["org_id"]),
        account=acct, account_id=account_id, session_id="sess1",
        pacer=pacer or _no_sleep_pacer(), platform="instagram",
        campaign=campaign, rng=rng or random.Random(0), now=now)


def _relevant_reel(rid="r1"):
    return Reel(reel_id=rid, author="growthlab",
                caption="tips on #marketing and growth", ocr_text="")


# ---- relevance gate ----

def test_relevance_gate_passes_on_hashtag_token_overlap():
    store, _ = fresh_store()
    aid = store.add_account(1, "instagram", "acct")
    c = _campaign(seed_hashtags=["marketing"])
    ex = _executor(store, RecordingFeed(), c, account_id=aid)
    assert ex._is_relevant(_relevant_reel()) is True


def test_relevance_gate_passes_on_author_overlap():
    store, _ = fresh_store()
    aid = store.add_account(1, "instagram", "acct")
    c = _campaign(seed_accounts=["growthlab"])
    ex = _executor(store, RecordingFeed(), c, account_id=aid)
    assert ex._is_relevant(_relevant_reel()) is True


def test_relevance_gate_skips_off_topic_reel():
    store, _ = fresh_store()
    aid = store.add_account(1, "instagram", "acct")
    c = _campaign(seed_hashtags=["marketing"])
    ex = _executor(store, RecordingFeed(), c, account_id=aid)
    off = Reel(reel_id="r9", author="cats", caption="cute kittens playing")
    assert ex._is_relevant(off) is False


def test_relevance_gate_falls_back_to_always_relevant_with_no_seeds():
    """O-relevance-model home-feed fallback: with NO seed_hashtags/seed_accounts
    and an empty relevance_def the campaign cannot steer relevance, so EVERY reel
    (even an off-topic one) is treated as on-topic. Guards against a regression
    that tokenises empty seeds into a non-empty set (silencing all writes)."""
    store, _ = fresh_store()
    aid = store.add_account(1, "instagram", "acct")
    c = _campaign(seed_hashtags=[], seed_accounts=[], relevance_def="")
    ex = _executor(store, RecordingFeed(), c, account_id=aid)
    assert ex._relevance_tokens == set()
    off = Reel(reel_id="r9", author="cats", caption="cute kittens playing")
    assert ex._is_relevant(off) is True


def test_relevance_gate_single_seed_not_in_reel_is_irrelevant():
    """The fallback boundary: ONE non-empty seed that does not appear in the reel
    must NOT pass (the empty-seed fallback only fires when there are zero tokens)."""
    store, _ = fresh_store()
    aid = store.add_account(1, "instagram", "acct")
    c = _campaign(seed_hashtags=["fintech"])
    ex = _executor(store, RecordingFeed(), c, account_id=aid)
    off = Reel(reel_id="r9", author="cats", caption="cute kittens playing")
    assert ex._is_relevant(off) is False


def test_off_topic_reel_gets_no_actions():
    store, _ = fresh_store()
    aid = store.add_account(1, "instagram", "acct")
    feed = RecordingFeed()
    c = _campaign(seed_hashtags=["marketing"])
    ex = _executor(store, feed, c, account_id=aid, rng=random.Random(0))
    budget = budget_for_day(10, "instagram")     # ramp stage, writes enabled
    off = Reel(reel_id="r9", author="cats", caption="cute kittens")
    ex.maybe_act(off, budget)
    assert feed.likes == [] and feed.saves == [] and feed.follows == []


# ---- read-only / observe short-circuit ----

def test_observe_stage_emits_zero_writes():
    store, _ = fresh_store()
    aid = store.add_account(1, "instagram", "acct")
    feed = RecordingFeed()
    c = _campaign(seed_hashtags=["marketing"])
    ex = _executor(store, feed, c, account_id=aid)
    budget = budget_for_day(0, "instagram")      # observe: read_only=True
    ex.maybe_act(_relevant_reel(), budget)
    assert feed.likes == [] and feed.saves == []
    assert store._conn.execute("SELECT COUNT(*) AS n FROM actions").fetchone()["n"] == 0


# ---- probability rolls ----

def test_high_probability_likes_fire_and_are_logged():
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp1", org_id=1, status="live")
    aid = store.add_account(1, "instagram", "acct")
    feed = RecordingFeed()
    c = _campaign(seed_hashtags=["marketing"])
    # rng seeded so the like (p=0.70) fires; share (p=0.05, cap 0 at light) never.
    ex = _executor(store, feed, c, account_id=aid, rng=random.Random(1))
    budget = budget_for_day(5, "instagram")      # light stage
    fired = False
    for i in range(20):
        feed_reel = _relevant_reel(f"r{i}")
        ex.maybe_act(feed_reel, budget)
        if feed.likes:
            fired = True
            break
    assert fired, "a like should fire at p=0.70 within 20 relevant reels"
    rows = store._conn.execute(
        "SELECT campaign_id, action_type, account_id, target FROM actions "
        "WHERE action_type='like'").fetchall()
    assert rows and rows[0]["campaign_id"] == warming_sentinel_campaign(1)
    assert rows[0]["account_id"] == aid


def test_share_never_fires_below_ramp_stage():
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp1", org_id=1, status="live")
    aid = store.add_account(1, "instagram", "acct")
    feed = RecordingFeed()
    c = _campaign(seed_hashtags=["marketing"], share_target="myhandle")
    ex = _executor(store, feed, c, account_id=aid, rng=random.Random(3))
    budget = budget_for_day(5, "instagram")      # light: shares cap 0
    for i in range(50):
        ex.maybe_act(_relevant_reel(f"r{i}"), budget)
    assert feed.shares == []


def test_share_uses_campaign_share_target_when_set():
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp1", org_id=1, status="live")
    aid = store.add_account(1, "instagram", "acct")
    feed = RecordingFeed()
    c = _campaign(seed_hashtags=["marketing"], share_target="myhandle")
    # Force every probability to fire by patching the budget probs to 1.0 via rng.
    ex = _executor(store, feed, c, account_id=aid, rng=random.Random(0))
    from reelradar.engines.warming.ramp import ActionBudget
    budget = ActionBudget("ramp", likes=0, follows=0, connects=0, dwell_windows=3,
                          read_only=False, saves=0, shares=1,
                          p_like=0.0, p_save=0.0, p_follow=0.0, p_share=1.0,
                          delay_min=0.0, delay_max=0.0, delay_long_p=0.0,
                          delay_long_max=0.0)
    ex.maybe_act(_relevant_reel("r1"), budget)
    assert feed.shares == [("r1", "myhandle")]


# ---- per-day cap enforcement ----

def test_per_day_like_cap_enforced_within_session():
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp1", org_id=1, status="live")
    aid = store.add_account(1, "instagram", "acct")
    feed = RecordingFeed()
    c = _campaign(seed_hashtags=["marketing"])
    from reelradar.engines.warming.ramp import ActionBudget
    # like cap 3, p_like=1.0 so it always fires until the cap is hit.
    budget = ActionBudget("ramp", likes=3, follows=0, connects=0, dwell_windows=3,
                          read_only=False, saves=0, shares=0,
                          p_like=1.0, p_save=0.0, p_follow=0.0, p_share=0.0,
                          delay_min=0.0, delay_max=0.0, delay_long_p=0.0,
                          delay_long_max=0.0)
    ex = _executor(store, feed, c, account_id=aid, rng=random.Random(0))
    for i in range(20):
        ex.maybe_act(_relevant_reel(f"r{i}"), budget)
    assert len(feed.likes) == 3


def test_per_day_cap_persists_across_two_sessions():
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp1", org_id=1, status="live")
    aid = store.add_account(1, "instagram", "acct")
    c = _campaign(seed_hashtags=["marketing"])
    from reelradar.engines.warming.ramp import ActionBudget
    budget = ActionBudget("ramp", likes=3, follows=0, connects=0, dwell_windows=3,
                          read_only=False, saves=0, shares=0,
                          p_like=1.0, p_save=0.0, p_follow=0.0, p_share=0.0,
                          delay_min=0.0, delay_max=0.0, delay_long_p=0.0,
                          delay_long_max=0.0)
    # `now` defaults to real time so the seed read buckets the SAME local day
    # store.log_action stamps (it uses time.time() with no injection).
    feed1 = RecordingFeed()
    ex1 = _executor(store, feed1, c, account_id=aid, rng=random.Random(0), now=None)
    for i in range(5):
        ex1.maybe_act(_relevant_reel(f"a{i}"), budget)
    assert len(feed1.likes) == 3
    # A SECOND session the same day starts with the day's tally already at 3.
    feed2 = RecordingFeed()
    ex2 = _executor(store, feed2, c, account_id=aid, rng=random.Random(0), now=None)
    for i in range(5):
        ex2.maybe_act(_relevant_reel(f"b{i}"), budget)
    assert feed2.likes == [], "the per-day cap was already exhausted in session 1"


def test_failed_fire_does_not_consume_cap():
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp1", org_id=1, status="live")
    aid = store.add_account(1, "instagram", "acct")
    c = _campaign(seed_hashtags=["marketing"])

    class FailLikeFeed(RecordingFeed):
        def like_reel(self, reel):
            return False     # like always fails

    feed = FailLikeFeed()
    from reelradar.engines.warming.ramp import ActionBudget
    budget = ActionBudget("ramp", likes=2, follows=0, connects=0, dwell_windows=3,
                          read_only=False, saves=0, shares=0,
                          p_like=1.0, p_save=0.0, p_follow=0.0, p_share=0.0,
                          delay_min=0.0, delay_max=0.0, delay_long_p=0.0,
                          delay_long_max=0.0)
    ex = _executor(store, feed, c, account_id=aid, rng=random.Random(0))
    for i in range(5):
        ex.maybe_act(_relevant_reel(f"r{i}"), budget)
    # 5 attempts all failed, but none consumed the day's like budget (no success row).
    n = store._conn.execute(
        "SELECT COUNT(*) AS n FROM actions WHERE succeeded=1").fetchone()["n"]
    assert n == 0


# ---- action-block halt ----

def test_action_block_raises_halt_session():
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp1", org_id=1, status="live")
    aid = store.add_account(1, "instagram", "acct")
    feed = RecordingFeed(block=True)
    c = _campaign(seed_hashtags=["marketing"])
    from reelradar.engines.warming.ramp import ActionBudget
    budget = ActionBudget("ramp", likes=5, follows=0, connects=0, dwell_windows=3,
                          read_only=False, saves=0, shares=0,
                          p_like=1.0, p_save=0.0, p_follow=0.0, p_share=0.0,
                          delay_min=0.0, delay_max=0.0, delay_long_p=0.0,
                          delay_long_max=0.0)
    ex = _executor(store, feed, c, account_id=aid, rng=random.Random(0))
    with pytest.raises(HaltSession) as exc:
        ex.maybe_act(_relevant_reel(), budget)
    assert exc.value.kind == "account_challenge"


# ---- right-skewed delays ----

def test_delays_are_randomized_and_right_skewed():
    store, _ = fresh_store()
    aid = store.add_account(1, "instagram", "acct")
    c = _campaign(seed_hashtags=["marketing"])
    slept: list[float] = []
    pacer = Pacer(cfg=PacingConfig(enforce_daytime=False),
                  rng=random.Random(42), sleep=slept.append)
    ex = _executor(store, RecordingFeed(), c, account_id=aid, pacer=pacer,
                   rng=random.Random(42))
    from reelradar.engines.warming.ramp import ActionBudget
    budget = ActionBudget("ramp", likes=99, follows=0, connects=0, dwell_windows=3,
                          read_only=False, saves=0, shares=0,
                          p_like=1.0, p_save=0.0, p_follow=0.0, p_share=0.0,
                          delay_min=2.0, delay_max=8.0, delay_long_p=0.12,
                          delay_long_max=25.0)
    for _ in range(200):
        ex._human_delay(budget)
    assert len(set(slept)) > 1, "delays must be randomized, not a fixed constant"
    assert any(t > 8.0 for t in slept), "an occasional longer pause must occur"
    assert all(t <= 25.0 for t in slept)
