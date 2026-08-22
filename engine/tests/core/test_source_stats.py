"""The per-source discovery ledger (schema v24, Campaign Lab Remedy Sheet #1/D).

`walk()` has computed per-source yield on every run since the source stamp landed
and dropped it at a debug line; these lock the contract now that it is persisted:
attribution survives the carry-over case that produced the 2026-08-19 live
mis-report, park/ban verdicts are reversible, and the park rule can never disarm
a campaign by retiring its last seeds.
"""
import os
import tempfile
import time

import pytest

from aizu.core.store import Store


@pytest.fixture
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = Store(path)
    yield s
    s.close()


def _row(store, source, campaign="c1", platform="instagram"):
    return next(r for r in store.source_stats(campaign, platform)
                if r["source"] == source)


def test_counters_accumulate_across_walks(store):
    store.record_source_walk("c1", "remont", kind="hashtag", yielded=5,
                             carried_over=2, seconds=30.0)
    store.record_source_walk("c1", "remont", kind="hashtag", yielded=3, seconds=10.0)
    row = _row(store, "remont")
    assert row["navigations"] == 2
    assert row["yielded"] == 8
    assert row["carried_over"] == 2
    assert row["seconds"] == pytest.approx(40.0)
    assert row["kind"] == "hashtag"


def test_carried_over_is_not_credited_as_yield(store):
    """The live 2026-08-19 shape: six tag pages redirected and their reels drained
    under a seed account. The account must NOT be credited with them."""
    store.record_source_walk("c1", "tag-a", kind="hashtag", yielded=0, redirected=True)
    store.record_source_walk("c1", "acme", kind="account", yielded=0, carried_over=12)
    assert _row(store, "acme")["yielded"] == 0
    assert _row(store, "acme")["carried_over"] == 12
    assert _row(store, "tag-a")["redirects"] == 1


def test_relevance_and_leads_are_derived_from_the_reel_rows(store):
    store.record_source_walk("c1", "remont", kind="hashtag", yielded=2)
    store.mark_seen("c1", "r1", relevant=True, source="remont")
    store.mark_seen("c1", "r2", relevant=False, source="remont")
    store.upsert_match(campaign_id="c1", reel_id="r1", comment_id="k1",
                       username="u", text="narxi qancha?", lang="uz", score=0.9,
                       reason="asks price", extracted=None, tier="text")
    row = _row(store, "remont")
    assert row["relevant_reels"] == 1
    assert row["leads"] == 1


def test_match_source_is_derived_from_seen_reels(store):
    """No engine passes `source` to upsert_match — mark_seen already stamped it, so
    the store derives it. That is also what makes a watchlist re-poll (which builds
    a bare Reel with no source) attribute correctly."""
    store.mark_seen("c1", "r1", relevant=True, source="remont")
    store.upsert_match(campaign_id="c1", reel_id="r1", comment_id="k1",
                       username="u", text="qancha?", lang="uz", score=0.9,
                       reason="r", extracted=None, tier="text")
    row = store._conn.execute(
        "SELECT source FROM matches WHERE comment_id='k1'").fetchone()
    assert row["source"] == "remont"


def test_first_sighting_owns_provenance(store):
    store.mark_seen("c1", "r1", relevant=None, source="remont")
    store.mark_seen("c1", "r1", relevant=True, source="dizayn")   # re-poll
    row = store._conn.execute(
        "SELECT source FROM seen_reels WHERE reel_id='r1'").fetchone()
    assert row["source"] == "remont"


def test_two_dead_hits_ban_and_a_yield_clears_it(store):
    store.record_source_walk("c1", "gone", kind="hashtag", unavailable=True)
    assert _row(store, "gone")["banned_at"] is not None    # recorded on the first hit
    store.record_source_walk("c1", "gone", kind="hashtag", unavailable=True)
    assert _row(store, "gone")["dead_hits"] == 2
    # …and a single productive walk fully rehabilitates it: the lifecycle columns
    # are a running verdict, never a tombstone.
    store.record_source_walk("c1", "gone", kind="hashtag", yielded=4)
    row = _row(store, "gone")
    assert row["banned_at"] is None and row["dead_hits"] == 0


def test_park_rule_needs_visits_and_volume_and_zero_relevance(store):
    for src in ("a", "b", "c", "d"):                 # keep well clear of the floor
        store.record_source_walk("c1", src, kind="hashtag", yielded=40)
    # 'a' has been visited enough AND intercepted enough AND never scored.
    for _ in range(2):
        store.record_source_walk("c1", "a", kind="hashtag", yielded=1)
    # 'b' has the volume but only one visit; 'c'/'d' have a relevance pass each.
    store.mark_seen("c1", "rc", relevant=True, source="c")
    store.mark_seen("c1", "rd", relevant=True, source="d")
    parked = store.park_dry_sources("c1")
    assert [p["source"] for p in parked] == ["a"]
    assert "dry" in parked[0]["park_reason"]
    assert store.parked_sources("c1") == {"a"}


def test_park_rule_never_goes_below_the_floor(store):
    for src in ("a", "b"):
        for _ in range(3):
            store.record_source_walk("c1", src, kind="hashtag", yielded=40)
    assert store.park_dry_sources("c1") == []          # both eligible, floor holds
    assert store.parked_sources("c1") == set()


def test_home_is_never_parked(store):
    for _ in range(3):
        store.record_source_walk("c1", "home", kind="home", yielded=40)
        for src in ("a", "b", "c"):
            store.record_source_walk("c1", src, kind="hashtag", yielded=40)
    parked = {p["source"] for p in store.park_dry_sources("c1")}
    assert "home" not in parked


def test_live_seeds_drops_parked_but_never_empties_the_list(store):
    store.record_source_walk("c1", "gone", kind="hashtag", unavailable=True)
    store.record_source_walk("c1", "gone", kind="hashtag", unavailable=True)
    # Enough live seeds that the floor does not force 'gone' back in.
    assert store.live_seeds("c1", ["gone", "a", "b", "c"]) == ["a", "b", "c"]
    # Every seed dead → the brief's list is returned unchanged, plus a flag. An
    # empty seed list would silently flip the home feed back on.
    assert store.live_seeds("c1", ["gone"]) == ["gone"]
    assert any(f["kind"] == "seeds_all_dead" for f in store.all_flags("c1"))


def test_live_seeds_restores_best_parked_seeds_to_hold_the_floor(store):
    for src in ("x", "y"):
        store.record_source_walk("c1", src, kind="hashtag", unavailable=True)
        store.record_source_walk("c1", src, kind="hashtag", unavailable=True)
    store.record_source_walk("c1", "y", kind="hashtag")   # y has more history
    live = store.live_seeds("c1", ["x", "y", "keeper"])
    assert "keeper" in live and len(live) >= Store.PARK_MIN_ACTIVE
    assert live == [s for s in ["x", "y", "keeper"] if s in live]  # brief order kept


def test_unpark_clears_the_verdict_without_erasing_history(store):
    store.record_source_walk("c1", "gone", kind="hashtag", unavailable=True)
    store.record_source_walk("c1", "gone", kind="hashtag", unavailable=True)
    store.unpark_source("c1", "gone")
    assert store.parked_sources("c1") == set()
    assert _row(store, "gone")["navigations"] == 2


def test_seed_history_reports_proof_and_prefers_it_over_a_park_verdict(store):
    store.record_source_walk("c1", "good", kind="hashtag", yielded=9)
    store.mark_seen("c1", "r1", relevant=True, source="good")
    store.upsert_match(campaign_id="c1", reel_id="r1", comment_id="k1", username="u",
                       text="qancha?", lang="uz", score=0.9, reason="r",
                       extracted=None, tier="text")
    # 'good' is parked on one campaign but has produced a lead — proof wins.
    store.record_source_walk("c2", "good", kind="hashtag", unavailable=True)
    store.record_source_walk("c2", "good", kind="hashtag", unavailable=True)
    store.record_source_walk("c2", "bad", kind="hashtag", unavailable=True)
    store.record_source_walk("c2", "bad", kind="hashtag", unavailable=True)
    hist = store.seed_history()
    assert hist["productive"] == ["good"]
    assert hist["dead"] == ["bad"]


def test_empty_source_is_ignored(store):
    store.record_source_walk("c1", "", kind="hashtag", yielded=3)
    assert store.source_stats("c1") == []


# ----- seed_history: the ORG-SCOPED branch (the only one production calls) -----

def _org_campaigns(store, org_id, campaign_ids):
    """Register campaigns into an org, in the given creation order.

    Creation order is load-bearing: the aggregation bug below was invisible to
    any test that created its productive campaign first."""
    now = time.time()
    for cid in campaign_ids:
        store._conn.execute(
            "INSERT INTO campaign_meta(campaign_id, org_id, created_at, updated_at) "
            "VALUES(?,?,?,?)", (cid, org_id, now, now))
    store._conn.commit()


def _proven_and_parked(store, order):
    """Seed 'good' produces a lead on z9 and is parked on a1/a2."""
    _org_campaigns(store, 1, order)
    for cid in ("a1", "a2"):
        store.record_source_walk(cid, "good", kind="hashtag", unavailable=True)
        store.record_source_walk(cid, "good", kind="hashtag", unavailable=True)
    store.record_source_walk("z9", "good", kind="hashtag", yielded=9)
    store.mark_seen("z9", "r1", relevant=True, source="good")
    store.upsert_match(campaign_id="z9", reel_id="r1", comment_id="k1",
                       username="u", text="narxi?", lang="uz", score=0.9,
                       reason="asks price", extracted=None, tier="text")


@pytest.mark.parametrize("order", [["z9", "a1", "a2"], ["a1", "a2", "z9"]])
def test_proof_survives_whatever_order_the_tenant_created_their_campaigns_in(
        store, order):
    """`leads` was a correlated subquery over ss.campaign_id/ss.platform, neither
    of which is in the GROUP BY — SQLite evaluated them against one ARBITRARY row
    per group. So a seed proven on one campaign and parked on another flipped
    between productive and dead on nothing but campaign_meta creation order, and
    in the losing case a proven seed was rendered to the generator as
    "Do NOT propose them or close variants"."""
    _proven_and_parked(store, order)
    hist = store.seed_history(1, platform="instagram")
    assert hist["productive"] == ["good"]
    assert hist["dead"] == []


def test_leads_are_summed_across_the_orgs_campaigns_not_taken_from_one(store):
    _org_campaigns(store, 1, ["c1", "c2"])
    for cid in ("c1", "c2"):
        store.record_source_walk(cid, "shared", kind="hashtag", yielded=5)
        store.mark_seen(cid, f"r-{cid}", relevant=True, source="shared")
        store.upsert_match(campaign_id=cid, reel_id=f"r-{cid}",
                           comment_id=f"k-{cid}", username="u", text="narxi?",
                           lang="uz", score=0.9, reason="r", extracted=None,
                           tier="text")
    # Ordered by lead count, so a wrongly-single-campaign count would also
    # mis-rank; here 'shared' must out-rank a one-lead seed.
    store.record_source_walk("c1", "solo", kind="hashtag", yielded=5)
    store.mark_seen("c1", "r-solo", relevant=True, source="solo")
    store.upsert_match(campaign_id="c1", reel_id="r-solo", comment_id="k-solo",
                       username="u", text="narxi?", lang="uz", score=0.9,
                       reason="r", extracted=None, tier="text")
    assert store.seed_history(1, platform="instagram")["productive"][0] == "shared"


def test_the_dead_limit_is_applied_after_the_productive_subtraction(store):
    """The LIMIT used to run in SQL, before proven terms were subtracted — so a
    caller asking for N dead terms could get fewer, or none, whenever the
    alphabetical head of the list happened to be productive."""
    _org_campaigns(store, 1, ["c1"])
    # 'aaa' is parked AND proven; 'bbb'/'ccc' are merely parked.
    for src in ("aaa", "bbb", "ccc"):
        store.record_source_walk("c1", src, kind="hashtag", unavailable=True)
        store.record_source_walk("c1", src, kind="hashtag", unavailable=True)
    store.record_source_walk("c1", "aaa", kind="hashtag", yielded=5)
    store.mark_seen("c1", "r1", relevant=True, source="aaa")
    store.upsert_match(campaign_id="c1", reel_id="r1", comment_id="k1",
                       username="u", text="narxi?", lang="uz", score=0.9,
                       reason="r", extracted=None, tier="text")
    hist = store.seed_history(1, platform="instagram", limit=2)
    assert "aaa" not in hist["dead"]
    assert hist["dead"] == ["bbb", "ccc"]      # a full 2, not 1


def test_org_scoping_does_not_leak_another_tenants_seeds(store):
    _org_campaigns(store, 1, ["mine"])
    _org_campaigns(store, 2, ["theirs"])
    store.record_source_walk("theirs", "their-secret", kind="hashtag", yielded=9)
    store.mark_seen("theirs", "r1", relevant=True, source="their-secret")
    store.upsert_match(campaign_id="theirs", reel_id="r1", comment_id="k1",
                       username="u", text="narxi?", lang="uz", score=0.9,
                       reason="r", extracted=None, tier="text")
    assert store.seed_history(1)["productive"] == []
    assert store.seed_history(2)["productive"] == ["their-secret"]
