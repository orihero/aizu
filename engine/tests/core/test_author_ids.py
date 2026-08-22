"""Seed-shaped author ids (schema v25, Campaign Lab Remedy Sheet #2/A).

Every one of these fields was already in the payload and thrown away. `author` is
a DISPLAY NAME — it changes on a rename, so an account mined as a seed candidate
by handle silently becomes a different (or dead) account later. The stable id is
what a seed must be keyed on, and what a 404 can be trusted to mean.
"""
from aizu.core.parsers import parse_reels
from aizu.engines.linkedin.parsers import parse_posts as parse_li
from aizu.engines.x.parsers import parse_posts as parse_x


def test_instagram_keeps_the_user_pk_alongside_the_handle():
    (reel,) = parse_reels({"items": [{
        "code": "Cabc", "media_type": 1, "caption": {"text": "hi"},
        "user": {"username": "acme", "pk": "1785551234"}}]})
    assert (reel.author, reel.author_id) == ("acme", "1785551234")


def test_instagram_pk_userid_form_is_split_to_the_pk():
    (reel,) = parse_reels({"items": [{
        "code": "Cxyz", "media_type": 1, "caption": {"text": "x"},
        "user": {"username": "b", "id": "98765_4321"}}]})
    assert reel.author_id == "98765"


def test_instagram_without_a_pk_degrades_to_an_empty_id_not_a_crash():
    (reel,) = parse_reels({"items": [{
        "code": "Cnone", "media_type": 1, "caption": {"text": "x"},
        "user": {"username": "b"}}]})
    assert (reel.author, reel.author_id) == ("b", "")


def test_x_takes_the_authors_rest_id_not_the_posts():
    """The tweet's OWN rest_id sits at the top level. Confusing the two keys every
    mined seed to a post id, which resolves to nothing."""
    (post,) = parse_x({"data": {"t": {
        "rest_id": "1900000000000000001",
        "core": {"user_results": {"result": {
            "rest_id": "44196397", "legacy": {"screen_name": "elonmusk"}}}},
        "legacy": {"full_text": "hello world"}}}})
    assert post.reel_id == "1900000000000000001"
    assert (post.author, post.author_id) == ("elonmusk", "44196397")


def test_x_without_a_user_subtree_has_no_author_id():
    (post,) = parse_x({"data": {"t": {
        "rest_id": "1900000000000000002",
        "legacy": {"full_text": "no user node", "screen_name": "fallback"}}}})
    assert post.author_id == ""


def test_linkedin_keeps_the_canonical_profile_url():
    """`actor.navigationContext.actionTarget` is the only field in the payload
    that `seed_activity_url()` can consume directly."""
    posts = parse_li({"elements": [{
        "entityUrn": "urn:li:activity:7100000000000000000",
        "commentary": {"text": {"text": "we are hiring"}},
        "actor": {"name": {"text": "Acme Inc"},
                  "navigationContext": {
                      "actionTarget": "https://www.linkedin.com/company/acme/"}}}]})
    assert posts and posts[0].author == "Acme Inc"
    assert posts[0].author_id == "https://www.linkedin.com/company/acme/"


def test_a_linkedin_author_id_round_trips_through_the_seed_url_builder():
    from aizu.engines.linkedin.cdp import seed_activity_url
    assert seed_activity_url("https://www.linkedin.com/company/acme/") == \
        "https://www.linkedin.com/company/acme/posts/"


def test_linkedin_without_a_navigation_context_has_no_author_id():
    posts = parse_li({"elements": [{
        "entityUrn": "urn:li:activity:7100000000000000001",
        "commentary": {"text": {"text": "x"}},
        "actor": {"name": {"text": "No Target"}}}]})
    assert posts and posts[0].author_id == ""
