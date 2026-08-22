"""LinkedIn seed-account URLs (Campaign Lab, Remedy Sheet #2 — audit bug #1).

BOTH formats the panel tells operators to type used to build dead URLs, and
nothing covered `_sources()` at all. `in/jane-doe` became
`/in/in/jane-doe/recent-activity/all/` and `company/acme` became
`/in/company/acme/...`; the walk then paid a nav plus four empty-scroll rounds on
each 404, every session, and the campaign still reported `completed`.
"""
import pytest

from aizu.engines.linkedin.cdp import (ACTIVITY_URL, FEED_URL, HASHTAG_URL,
                                       LinkedInCDPConfig, LinkedInFeed,
                                       seed_activity_url)


@pytest.mark.parametrize("seed", [
    "jane-doe", "@jane-doe", "in/jane-doe", "/in/jane-doe/",
    "https://www.linkedin.com/in/jane-doe/",
    "https://uz.linkedin.com/in/jane-doe?trk=public_profile",
])
def test_every_person_form_reaches_the_same_activity_feed(seed):
    assert seed_activity_url(seed) == \
        "https://www.linkedin.com/in/jane-doe/recent-activity/all/"


@pytest.mark.parametrize("seed", [
    "company/acme", "/company/acme/", "company/acme/posts/",
    "https://www.linkedin.com/company/acme/",
])
def test_company_pages_use_the_posts_tab_not_the_in_path(seed):
    # The whole bug: a company slug formatted into ACTIVITY_URL yields
    # /in/company/acme/..., which does not exist.
    assert seed_activity_url(seed) == "https://www.linkedin.com/company/acme/posts/"
    assert "/in/company/" not in seed_activity_url(seed)


@pytest.mark.parametrize("kind", ["school", "showcase"])
def test_the_other_org_page_kinds_behave_like_company(kind):
    assert seed_activity_url(f"{kind}/mit") == \
        f"https://www.linkedin.com/{kind}/mit/posts/"


def test_a_bare_slug_is_still_treated_as_a_person():
    # The historical default, and the common case — must not regress.
    assert seed_activity_url("someone") == ACTIVITY_URL.format(slug="someone")


@pytest.mark.parametrize("seed", ["", "   ", "/", "https://www.linkedin.com/"])
def test_an_empty_seed_falls_back_to_the_feed_rather_than_a_broken_url(seed):
    assert seed_activity_url(seed) == FEED_URL


def test_sources_builds_home_then_hashtags_then_accounts():
    """Order is load-bearing: CDPFeedBase._source_seeds labels sources BY POSITION
    (`[home?] + seed_hashtags + seed_accounts`), so a reordering here silently
    mislabels every row in the per-source ledger."""
    feed = LinkedInFeed(LinkedInCDPConfig(
        seed_hashtags=("#remont",), seed_accounts=("in/jane-doe", "company/acme"),
        include_home_feed=True))
    assert feed._sources() == [
        FEED_URL,
        HASHTAG_URL.format(tag="remont"),
        "https://www.linkedin.com/in/jane-doe/recent-activity/all/",
        "https://www.linkedin.com/company/acme/posts/",
    ]


def test_sources_falls_back_to_the_feed_when_nothing_is_seeded():
    feed = LinkedInFeed(LinkedInCDPConfig(include_home_feed=False))
    assert feed._sources() == [FEED_URL]


def test_seed_labels_match_what_the_ledger_will_key_on():
    """`_source_seeds` must still produce one label per source in the right order
    once the URL builder stopped being a plain format()."""
    from aizu.core.feed import SOURCE_ACCOUNT, SOURCE_HASHTAG, SOURCE_HOME
    feed = LinkedInFeed(LinkedInCDPConfig(
        seed_hashtags=("remont",), seed_accounts=("in/jane-doe", "company/acme"),
        include_home_feed=True))
    urls = feed._sources()
    assert feed._source_seeds(urls) == [
        (SOURCE_HOME, "home"), (SOURCE_HASHTAG, "remont"),
        (SOURCE_ACCOUNT, "in/jane-doe"), (SOURCE_ACCOUNT, "company/acme")]
