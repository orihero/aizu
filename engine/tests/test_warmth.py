"""Warmth read-model — pure scoring (warming PRD §5) + Store integration."""
import os
import tempfile

from reelradar.core import warmth as W
from reelradar.core.store import Store
from reelradar.core.accounts import WARMING, READY


def fresh_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path), path


NOW = 1_700_000_000.0


def _inputs(**kw):
    base = dict(platform="x", age_days=0.0, ramp_completed_days=0,
                network_successes=0, detail=None, open_challenge_flags=[])
    base.update(kw)
    return W.WarmthInputs(**base)


# ---- pure compute ----

def test_neutral_default_is_50_and_not_blocking():
    n = W.neutral_default()
    assert n.score == 50 and n.state == "warming" and n.meets_gate is True
    assert n.components == {}


def test_cold_account_scores_low_and_below_gate():
    # age0/ramp0/network0/profile0.5(unknown)/trust1.0; x weights → 10*.5 + 25 = 30.
    s = W.compute(_inputs(), now=NOW)
    assert s.components["profile"] == 0.5 and s.components["trust"] == 1.0
    assert s.base == 30.0 and s.score == 30
    assert s.state == "warming" and s.meets_gate is False


def test_fully_warm_account_maxes_out():
    s = W.compute(_inputs(age_days=30, ramp_completed_days=14, network_successes=20,
                          detail={"login_status": "ok"}), now=NOW)
    assert all(s.components[c] == 1.0 for c in ("age", "ramp", "network", "profile", "trust"))
    assert s.base == 100.0 and s.score == 100
    assert s.state == "full" and s.meets_gate is True


def test_linkedin_uses_its_own_weight_row():
    # profile weight 20 on linkedin vs 10 on x — same maxed signal, different base.
    detail = {"login_status": "ok"}
    li = W.compute(_inputs(platform="linkedin", detail=detail), now=NOW)
    x = W.compute(_inputs(platform="x", detail=detail), now=NOW)
    assert li.components["profile"] == 1.0 and x.components["profile"] == 1.0
    # distinct weight rows → distinct base (li: profile20+trust10=30; x: profile10+trust25=35)
    assert li.base == 30.0 and x.base == 35.0


def test_default_weights_for_instagram():
    assert W.weights_for("instagram") == W.PLATFORM_WEIGHTS["_default"]
    assert W.weights_for("x") == W.PLATFORM_WEIGHTS["x"]


def test_every_weight_row_sums_to_100():
    for platform, row in W.PLATFORM_WEIGHTS.items():
        assert sum(row.values()) == 100, platform


def test_telegram_has_explicit_weight_row_not_default():
    tg = W.weights_for("telegram")
    assert tg is W.PLATFORM_WEIGHTS["telegram"]
    assert tg != W.PLATFORM_WEIGHTS["_default"]
    # Locked TG weights (DECISIONS): network weighted highest, profile lowest.
    assert tg == {"age": 25, "ramp": 20, "network": 30, "profile": 10, "trust": 15}


def test_telegram_network_divisor_is_8_not_target_connects():
    assert W.network_divisor("telegram") == 8
    assert W.network_divisor("instagram") == W.TARGET_CONNECTS
    assert W.network_divisor("x") == W.TARGET_CONNECTS
    # Unknown platforms default to the IG/X divisor.
    assert W.network_divisor("reddit") == W.TARGET_CONNECTS


def test_telegram_network_saturates_at_its_divisor():
    # 8 successful joins maxes TG network; the same count under-saturates IG.
    tg = W.compute(_inputs(platform="telegram", network_successes=8), now=NOW)
    ig = W.compute(_inputs(platform="instagram", network_successes=8), now=NOW)
    assert tg.components["network"] == 1.0
    assert ig.components["network"] == 8 / W.TARGET_CONNECTS


def test_peer_flood_is_a_warming_challenge_kind():
    assert "peer_flood" in W.WARMING_CHALLENGE_KINDS


def test_fresh_peer_flood_craters_score_like_a_challenge():
    # DECISIONS: peer_flood is a challenge-group kind, so a fresh flood block
    # craters warmth (PENALTY_CHALLENGE_FRESH), NOT the milder rate-limit factor.
    flags = [{"kind": "peer_flood", "severity": "halt", "created_at": NOW - 3600}]
    s = W.compute(_inputs(platform="telegram", age_days=30, ramp_completed_days=14,
                          network_successes=8, detail={"login_status": "ok"},
                          open_challenge_flags=flags), now=NOW)
    assert s.penalty_factor == W.PENALTY_CHALLENGE_FRESH
    assert s.state != "throttled"
    assert s.score <= 10


def test_fresh_challenge_craters_score():
    flags = [{"kind": "checkpoint", "severity": "halt", "created_at": NOW - 3600}]
    s = W.compute(_inputs(age_days=30, ramp_completed_days=14, network_successes=20,
                          detail={"login_status": "ok"}, open_challenge_flags=flags),
                  now=NOW)
    # base would be high but trust drops (open halt flag) and penalty = 0.10.
    assert s.penalty_factor == W.PENALTY_CHALLENGE_FRESH
    assert s.score <= 10


def test_rate_limit_marks_throttled():
    flags = [{"kind": "rate_limit", "severity": "soft", "created_at": NOW - 3600}]
    s = W.compute(_inputs(age_days=30, ramp_completed_days=14, network_successes=20,
                          detail={"login_status": "ok"}, open_challenge_flags=flags),
                  now=NOW)
    assert s.state == "throttled" and s.penalty_factor == W.PENALTY_RATE_LIMITED


def test_stale_challenge_does_not_penalize():
    flags = [{"kind": "checkpoint", "severity": "soft", "created_at": NOW - 100 * 3600}]
    f, reason, throttled = W.compute_penalty(flags, NOW)
    assert f == 1.0 and reason is None and not throttled


def test_checkpoint_detected_zeroes_profile():
    s = W.compute(_inputs(detail={"login_status": "ok", "checkpoint_detected": True}),
                  now=NOW)
    assert s.components["profile"] == 0.5   # login ok (1.0) + checkpoint bad (0.0) /2


def test_as_payload_shape():
    p = W.compute(_inputs(), now=NOW).as_payload("2026-06-29T00:00:00Z")
    assert set(p) == {"score", "state", "gateMin", "gateFull", "meetsGate",
                      "components", "trend", "etaHours", "checkedAt"}
    assert set(p["components"]) == {"age", "ramp", "network", "profile", "trust"}
    assert p["etaHours"] is None and p["trend"] == []


# ---- Store integration ----

def test_account_warmth_counts_distinct_ramp_days_and_network():
    store, _ = fresh_store()
    aid = store.add_account(1, "x", "a")
    # 3 completed warming sessions on 3 distinct days.
    for i, day in enumerate((0, 1, 2)):
        ts = NOW - day * 86400
        store._conn.execute(
            """INSERT INTO sessions(session_id, campaign_id, platform, started_at,
                                    status, engine_mode, account_id, org_id)
               VALUES(?,?,?,?, 'completed', 'warming', ?, 1)""",
            (f"w{i}", "__warming__:1", "x", ts, aid))
    store._conn.commit()
    store.log_action("__warming__:1", "follow", target="@x", succeeded=True, account_id=aid)
    store.log_action("__warming__:1", "follow", target="@y", succeeded=False, account_id=aid)

    s = store.account_warmth(1, "x", aid, now=NOW)
    assert s.components["ramp"] == 3 / W.RAMP_WINDOW_DAYS
    assert s.components["network"] == 1 / W.TARGET_CONNECTS   # only the succeeded one


def test_account_warmth_counts_join_as_network_for_telegram():
    store, _ = fresh_store()
    aid = store.add_account(1, "telegram", "chan")
    store.log_action("__warming__:1", "join", target="@a", succeeded=True, account_id=aid)
    store.log_action("__warming__:1", "join", target="@b", succeeded=True, account_id=aid)
    store.log_action("__warming__:1", "join", target="@c", succeeded=False, account_id=aid)
    s = store.account_warmth(1, "telegram", aid, now=NOW)
    # 2 successful joins / TG divisor(8); the failed one does not count.
    assert s.components["network"] == 2 / W.network_divisor("telegram")


def test_account_warmth_neutral_for_foreign_account():
    store, _ = fresh_store()
    aid = store.add_account(1, "x", "a")
    assert store.account_warmth(999, "x", aid, now=NOW).score == 50   # wrong org


def test_warmth_for_campaign_resolves_backing_account():
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp1", org_id=1, status="live")
    aid = store.add_account(1, "x", "a")
    store.assign_account("camp1", "x", aid)
    s = store.warmth_for_campaign("camp1", now=NOW, platform="x")
    assert s.score == store.account_warmth(1, "x", aid, now=NOW).score


def test_warmth_for_campaign_neutral_when_non_warmable():
    store, _ = fresh_store()
    store.upsert_campaign_meta("yt1", org_id=1, status="live")
    assert store.warmth_for_campaign("yt1", now=NOW, platform="youtube").score == 50


def test_warmth_for_campaign_neutral_when_no_account():
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp1", org_id=1, status="live")
    assert store.warmth_for_campaign("camp1", now=NOW, platform="x").score == 50


def test_warmth_for_campaign_derives_platform_from_brief():
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp1", org_id=1, status="live")
    store.upsert_campaign_brief("camp1", {"platform": "x"}, org_id=1)
    aid = store.add_account(1, "x", "a")
    store.assign_account("camp1", "x", aid)
    # no explicit platform — derived from the brief
    assert store.warmth_for_campaign("camp1", now=NOW).score == \
        store.account_warmth(1, "x", aid, now=NOW).score
