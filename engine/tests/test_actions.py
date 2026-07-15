from pathlib import Path

from reelradar.engines.instagram.actions import ActionPolicy
from reelradar.core.config import campaign_from_brief, load_campaign

CONFIG = Path(__file__).resolve().parents[1] / "config"


def test_policy_caps_likes_and_follows():
    p = ActionPolicy(enabled=True, max_likes=2, max_follows=1)
    assert p.can_like()
    p.record_like(); p.record_like()
    assert not p.can_like()                 # like cap reached
    assert p.can_follow()
    p.record_follow()
    assert not p.can_follow()               # follow cap reached


def test_policy_disabled_blocks_everything():
    p = ActionPolicy(enabled=False, max_likes=5, max_follows=5)
    assert not p.can_like() and not p.can_follow()


def test_policy_from_campaign_maps_fields():
    # The shipped campaign is now read-only (warming PRD §4.3), so build an
    # engagement-enabled campaign to verify from_campaign maps the knobs through.
    c = campaign_from_brief("c", {"enable_actions": True,
                                  "max_likes_per_session": 8,
                                  "max_follows_per_session": 4})
    p = ActionPolicy.from_campaign(c)
    assert p.enabled is True and p.max_likes == 8 and p.max_follows == 4


def test_shipped_campaign_is_read_only():
    p = ActionPolicy.from_campaign(load_campaign(CONFIG / "campaign.md"))
    assert p.enabled is False    # harvest writes nothing (warming PRD C1)
