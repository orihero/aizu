"""Extended ActionBudget — P1 warming writes (warming-writes PRD §3.2, O6).

Per-action probabilities + per-day caps for like/save/follow/share layered onto
the shipped P0 dwell-only budget. The `observe` stage stays read_only with every
cap at 0; Instagram caps clamp the stage values by `_PLATFORM_CAPS`.
"""
from reelradar.engines.warming.ramp import ActionBudget, budget_for_day


# ---- P0 fields untouched (additive guarantee) ----

def test_observe_stage_is_read_only_with_zero_writes():
    b = budget_for_day(0, "instagram")
    assert b.stage == "observe"
    assert b.read_only is True
    assert b.likes == 0 and b.saves == 0 and b.follows == 0 and b.shares == 0
    assert b.p_like == 0.0 and b.p_save == 0.0
    assert b.p_follow == 0.0 and b.p_share == 0.0


def test_existing_p0_fields_still_present():
    b = budget_for_day(10, "instagram")
    # P0 fields keep working for existing call sites.
    assert b.stage == "ramp"
    assert b.dwell_windows == 3
    assert b.connects == 0          # instagram cap zeroes connects


# ---- O6 per-action probabilities (shared across stages) ----

def test_active_stage_carries_action_probabilities():
    b = budget_for_day(5, "instagram")        # light
    assert b.p_like == 0.70
    assert b.p_save == 0.30
    assert b.p_follow == 0.12
    # share is double-barrel gated OFF at light (O-share-ship): cap=0 AND p=0.
    assert b.p_share == 0.0


def test_share_probability_active_from_ramp_stage():
    for day in (10, 20):                       # ramp, sustain
        b = budget_for_day(day, "instagram")
        assert b.p_share == 0.05


# ---- O6 per-day caps by stage, clamped by instagram ceilings ----

def test_light_stage_caps():
    b = budget_for_day(5, "instagram")        # light (4-7)
    assert b.likes == 15
    assert b.saves == 8
    assert b.follows == 1
    assert b.shares == 0                       # share fires only from ramp on


def test_ramp_stage_caps():
    b = budget_for_day(10, "instagram")       # ramp (8-14)
    assert b.likes == 30
    assert b.saves == 15
    assert b.follows == 3
    assert b.shares == 1                       # share unlocks at ramp


def test_sustain_stage_caps_clamped_by_platform_ceilings():
    b = budget_for_day(99, "instagram")       # sustain (15+)
    # Stage values like(50)/save(25)/follow(5)/share(2) at the IG ceilings.
    assert b.likes == 50
    assert b.saves == 25
    assert b.follows == 5
    assert b.shares == 2


def test_instagram_platform_ceilings_are_the_backstop():
    # Sustain stage requests exactly the ceilings; nothing exceeds them.
    b = budget_for_day(99, "instagram")
    assert b.likes <= 50 and b.saves <= 25
    assert b.follows <= 5 and b.shares <= 2


def test_share_zero_below_ramp_stage():
    assert budget_for_day(0, "instagram").shares == 0     # observe
    assert budget_for_day(5, "instagram").shares == 0     # light
    assert budget_for_day(10, "instagram").shares == 1    # ramp
    assert budget_for_day(99, "instagram").shares == 2    # sustain


def test_unknown_platform_returns_stage_unclamped():
    b = budget_for_day(10, "reddit")          # no reddit cap row in this build
    assert b.stage == "ramp"
    # Falls through to the raw stage budget (no platform clamp applied).
    assert isinstance(b, ActionBudget)


# ---- Telegram per-day join/react caps (warming-writes PRD §7) ----

def test_telegram_observe_is_read_only_with_zero_join_react():
    b = budget_for_day(0, "telegram")
    assert b.stage == "observe"
    assert b.read_only is True
    assert b.joins == 0 and b.reacts == 0
    assert b.p_react == 0.0


def test_telegram_join_react_caps_by_stage():
    # joins observe/light/ramp/sustain = 0/1/2/3; reacts = 0/3/5/8 (DECISIONS).
    expected = {0: (0, 0), 5: (1, 3), 10: (2, 5), 99: (3, 8)}
    for day, (joins, reacts) in expected.items():
        b = budget_for_day(day, "telegram")
        assert b.joins == joins, day
        assert b.reacts == reacts, day


def test_telegram_react_probability_on_active_stages():
    assert budget_for_day(0, "telegram").p_react == 0.0    # observe
    for day in (5, 10, 99):                                # light, ramp, sustain
        assert budget_for_day(day, "telegram").p_react == 0.40


def test_telegram_has_no_ig_write_surface():
    # Telegram never gets like/follow/save/share — only join + react.
    b = budget_for_day(99, "telegram")
    assert b.likes == 0 and b.follows == 0
    assert b.saves == 0 and b.shares == 0


def test_instagram_budget_carries_no_join_react_counts():
    # Additive guarantee: IG/x/linkedin daily join/react CEILINGS stay 0 (the
    # platform cap zeroes them), so no join/react can ever fire — exactly how the
    # share probability rides on every stage but the count is the hard gate.
    for day in (0, 5, 10, 99):
        b = budget_for_day(day, "instagram")
        assert b.joins == 0 and b.reacts == 0


def test_delay_envelope_defaults_present():
    b = budget_for_day(10, "instagram")
    assert b.delay_min == 2.0
    assert b.delay_max == 8.0
    assert 0.0 <= b.delay_long_p <= 1.0
    assert b.delay_long_max >= b.delay_max
