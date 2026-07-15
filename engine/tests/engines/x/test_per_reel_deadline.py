"""FIX 1 (Layer C, X): the per-reel wall-clock backstop skips a post that overruns
per_reel_seconds and STILL processes the next post — never halts. Injected clock
keeps it deterministic and fast."""
import os
import tempfile

from reelradar.core.config import campaign_from_brief
from reelradar.core.feed import Comment, FakeFeed, Reel
from reelradar.core.pacing import Pacer, PacingConfig
from reelradar.core.router import Decision
from reelradar.core.store import Store
from reelradar.engines.x.session import SessionConfig, XSession


class SpyRouter:
    def classify_text(self, *, instruction, content, campaign_id, stage,
                      session_id=None, system=None):
        low = content.lower()
        if stage == "relevance":
            rel = "acme" in low
            return Decision(label="relevant" if rel else "irrelevant",
                            score=0.9 if rel else 0.1, confidence=0.97)
        lead = "pricing" in low
        return Decision(label="yes" if lead else "no",
                        score=0.93 if lead else 0.1, confidence=0.97,
                        extracted={"phone": "+14155550142"} if lead else {})

    def classify_image(self, *, instruction, images_b64, campaign_id, stage,
                       session_id=None, system=None):
        return Decision(label="irrelevant", score=0.0, confidence=1.0)


def _store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path)


def _campaign():
    return campaign_from_brief("x-leadgen", {
        "platform": "x", "threshold": 0.7,
        "relevance_def": "acme posts",
        "match_def": "a replier asking price",
        "extract_def": "- phone"})


def _pacer():
    return Pacer(cfg=PacingConfig(enforce_daytime=False), sleep=lambda _t: None)


def test_slow_post_skipped_next_post_still_processed():
    store = _store()
    reels = [
        Reel("slow", caption="acme", comments=[Comment("c1", "u1", "pricing?")]),
        Reel("fast", caption="acme", comments=[Comment("c2", "u2", "pricing?")]),
    ]
    ticks = iter([0.0, 100.0, 100.0, 101.0, 102.0])
    out = XSession(store=store, router=SpyRouter(), feed=FakeFeed(reels), soul=None,
                   campaign=_campaign(), pacer=_pacer(), cfg=SessionConfig(),
                   clock=lambda: next(ticks)).run()
    assert out["reels_seen"] == 2
    assert out["halt_reason"] is None
    assert out["matches"] == 1     # only the fast post reached comment scoring
