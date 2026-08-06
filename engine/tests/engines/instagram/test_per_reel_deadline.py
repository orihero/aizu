"""FIX 1 (Layer C): the per-reel wall-clock backstop skips a reel that overruns
per_reel_seconds and STILL processes the next reel — the outer guarantee behind
the per-call Playwright timeouts. Driven by an injected clock so it is
deterministic and fast (no real sleeping)."""
import os
import tempfile

from aizu.core.config import campaign_from_brief
from aizu.core.feed import Comment, FakeFeed, Reel
from aizu.core.mock_router import MockRouter
from aizu.core.pacing import Pacer, PacingConfig
from aizu.core.store import Store
from aizu.engines.instagram.session import Session, SessionConfig


def _store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path)


def _campaign():
    return campaign_from_brief("ig-leadgen", {
        "platform": "instagram", "threshold": 0.7,
        "relevance_def": "acme saas app posts",
        "match_def": "a commenter asking to buy / price / contact",
        "extract_def": "- phone",
    })


def _daytime_pacer():
    return Pacer(cfg=PacingConfig(enforce_daytime=False), sleep=lambda _t: None)


def test_slow_reel_is_skipped_and_next_reel_still_processed():
    store = _store()
    reels = [
        Reel("slow", caption="acme saas app dashboard", comments=[Comment("c1", "u1", "pricing?")]),
        Reel("fast", caption="acme saas app dashboard", comments=[Comment("c2", "u2", "pricing?")]),
    ]
    # Clock timeline (per_reel_seconds default 90):
    #   reel "slow":  reel_start=0, before-check=100 (>90 → SKIP, continue)  [2 ticks]
    #   reel "fast":  reel_start=100, before-check=101, after-check=102       [3 ticks]
    ticks = iter([0.0, 100.0, 100.0, 101.0, 102.0])
    session = Session(store=store, router=MockRouter(store=store),
                      feed=FakeFeed(reels), soul=None, campaign=_campaign(),
                      pacer=_daytime_pacer(), cfg=SessionConfig(),
                      clock=lambda: next(ticks))
    out = session.run()

    # Both reels were WALKED (seen), but only the fast one reached open_reel +
    # comment scoring — the slow one was skipped at the deadline, not halted.
    assert out["reels_seen"] == 2
    assert out["halt_reason"] is None
    # The slow reel never opened comments (skipped before the block) → no match;
    # the fast reel's "pricing?" comment matched. So exactly one lead was found.
    assert out["matches"] == 1


def test_no_skip_when_reel_stays_under_deadline():
    store = _store()
    reels = [Reel("r1", caption="acme saas app dashboard",
                  comments=[Comment("c1", "u1", "pricing?")])]
    # All three ticks well under the 90s budget → no skip, comment scored.
    ticks = iter([0.0, 1.0, 2.0])
    session = Session(store=store, router=MockRouter(store=store),
                      feed=FakeFeed(reels), soul=None, campaign=_campaign(),
                      pacer=_daytime_pacer(), cfg=SessionConfig(),
                      clock=lambda: next(ticks))
    out = session.run()
    assert out["reels_seen"] == 1
    assert out["matches"] == 1
    assert out["halt_reason"] is None
