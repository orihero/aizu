"""Mining seed ACCOUNTS from our own results (schema v25, Campaign Lab Sheet #2/A).

Every input already sat in SQLite: `seen_reels.author` plus the `relevant` label
every engine writes, and `matches` for which posts produced leads. Nothing
aggregated any of it, so "which accounts produce our leads" was unanswerable
against a database that had always held the answer.
"""
import os
import tempfile

import pytest

from aizu.core.store import Store


@pytest.fixture
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = Store(path)
    yield s
    s.close()


def _relevant(store, reel_id, author, author_id=None, campaign="c1",
              platform="instagram"):
    store.mark_seen(campaign, reel_id, relevant=True, author=author,
                    author_id=author_id, source="seed", platform=platform)


def _lead(store, reel_id, comment_id, username="buyer", campaign="c1",
          platform="instagram"):
    store.upsert_match(campaign_id=campaign, reel_id=reel_id, comment_id=comment_id,
                       username=username, text="narxi?", lang="uz", score=0.9,
                       reason="asks price", extracted=None, tier="text",
                       platform=platform)


def test_proof_outranks_signal(store):
    """A lead is proof; a relevance pass is only a signal. Three relevant posts
    with no leads must lose to one relevant post that produced a lead."""
    for i in range(3):
        _relevant(store, f"a{i}", "Prolific")
    _relevant(store, "b0", "Converter")
    _lead(store, "b0", "k1")
    ranked = [r["author"] for r in store.seed_candidates("c1")]
    assert ranked == ["Converter", "Prolific"]


def test_the_stable_id_is_the_grouping_key_so_a_rename_stays_one_candidate(store):
    _relevant(store, "r1", "Old Name", author_id="UC_same")
    _relevant(store, "r2", "New Name", author_id="UC_same")
    (row,) = store.seed_candidates("c1")
    assert row["relevant_posts"] == 2
    assert row["seed"] == "UC_same"          # what an operator pastes back


def test_the_display_name_is_the_fallback_key_when_no_stable_id_exists(store):
    _relevant(store, "r1", "Telegramish")
    _relevant(store, "r2", "Telegramish")
    (row,) = store.seed_candidates("c1")
    assert row["relevant_posts"] == 2 and row["seed"] == "Telegramish"


def test_irrelevant_and_ungated_posts_never_propose_an_account(store):
    store.mark_seen("c1", "r1", relevant=False, author="Noise")
    store.mark_seen("c1", "r2", relevant=None, author="Unjudged")
    assert store.seed_candidates("c1") == []


def test_authorless_rows_are_skipped(store):
    store.mark_seen("c1", "r1", relevant=True, author=None)
    store.mark_seen("c1", "r2", relevant=True, author="   ")
    assert store.seed_candidates("c1") == []


def test_current_seeds_are_not_proposed_back(store):
    _relevant(store, "r1", "Already", author_id="UC_have")
    _relevant(store, "r2", "Fresh", author_id="UC_new")
    seeds = [r["seed"] for r in store.seed_candidates("c1", exclude=["UC_have"])]
    assert seeds == ["UC_new"]


def test_exclusion_ignores_case_and_a_leading_at(store):
    _relevant(store, "r1", "acme", author_id="@Acme")
    assert store.seed_candidates("c1", exclude=["acme"]) == []


def test_min_relevant_raises_the_bar(store):
    _relevant(store, "r1", "Once")
    for i in range(2):
        _relevant(store, f"t{i}", "Twice")
    assert [r["author"] for r in store.seed_candidates("c1", min_relevant=2)] == ["Twice"]


def test_platform_scopes_the_query(store):
    _relevant(store, "r1", "OnIG", platform="instagram")
    _relevant(store, "r2", "OnYT", platform="youtube")
    assert [r["author"] for r in store.seed_candidates("c1", platform="youtube")] \
        == ["OnYT"]


def test_leads_are_counted_per_distinct_comment_not_per_row(store):
    _relevant(store, "r1", "A")
    _lead(store, "r1", "k1")
    _lead(store, "r1", "k1")          # re-poll of the SAME comment
    _lead(store, "r1", "k2")
    (row,) = store.seed_candidates("c1")
    assert row["leads"] == 2


# ----- commenter overlap -----

def test_overlap_is_normalized_so_a_giant_generic_account_cannot_win(store):
    """Raw overlap ranks big accounts first purely because they are large."""
    _relevant(store, "small", "Niche")
    _lead(store, "small", "s1", username="ourbuyer1")
    _lead(store, "small", "s2", username="ourbuyer2")
    _relevant(store, "big", "Generic")
    _lead(store, "big", "b1", username="ourbuyer1")
    _lead(store, "big", "b2", username="ourbuyer2")
    for i in range(20):
        _lead(store, "big", f"n{i}", username=f"stranger{i}")
    rows = {r["author"]: r for r in store.co_commenter_overlap("c1", min_shared=2)}
    # Both share the same two commenters, but they are ALL of Niche's audience.
    assert rows["Niche"]["shared_commenters"] == rows["Generic"]["shared_commenters"]
    assert rows["Niche"]["overlap_share"] > rows["Generic"]["overlap_share"]


def test_a_lone_author_overlaps_with_nothing(store):
    """"Shared" means shared with a DIFFERENT author. One author in the corpus
    therefore has nothing to overlap with — not a 100% self-overlap, which is what
    defining the set as "every lead in the campaign" would produce."""
    _relevant(store, "r1", "Only")
    _lead(store, "r1", "k1", username="somebody")
    assert store.co_commenter_overlap("c1", min_shared=1) == []


def test_overlap_respects_the_min_shared_floor(store):
    _relevant(store, "r1", "A")
    _relevant(store, "r2", "B")
    _lead(store, "r1", "k1", username="both")
    _lead(store, "r2", "k2", username="both")
    assert store.co_commenter_overlap("c1", min_shared=2) == []   # only one shared
    assert len(store.co_commenter_overlap("c1", min_shared=1)) == 2


def test_overlap_on_an_empty_campaign_is_not_an_error(store):
    assert store.co_commenter_overlap("nope") == []
    assert store.seed_candidates("nope") == []
