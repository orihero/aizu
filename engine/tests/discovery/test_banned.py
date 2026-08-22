"""The zero-request tag prefilter (Remedy Sheet #1 / Remedy C).

Seeding a banned tag costs a navigation plus four empty-scroll rounds every
session and returns nothing. This is the cheapest possible way to not do that —
and the guarantee that matters most is the inverse one: it must not silently
delete working seeds.
"""
from aizu.discovery.banned import (BANNED_TAGS_FILE_ENV, blocked_tags, prefilter,
                                   reason_to_skip)


def test_a_normal_niche_tag_passes():
    assert reason_to_skip("tashkentremont") is None
    assert reason_to_skip("#Remont") is None       # leading # and case are normalised


def test_the_campaigns_own_evidence_outranks_every_list():
    reason = reason_to_skip("remont", known_dead=["#Remont"])
    assert reason is not None and "own runs" in reason


def test_generic_and_short_and_blocked_are_reported_distinctly():
    assert "generic" in reason_to_skip("love")
    assert "short" in reason_to_skip("ab")
    assert "banned" in reason_to_skip("dm")


def test_prefilter_returns_survivors_and_reasons():
    keep, dropped = prefilter(["remont", "love", "dm", ""], known_dead=[])
    assert keep == ["remont"]
    assert set(dropped) == {"love", "dm", ""}


def test_operator_list_extends_the_builtin_one(tmp_path, monkeypatch):
    f = tmp_path / "banned.txt"
    f.write_text("#localnoise\n\n// a comment line\nother // trailing comment\n")
    monkeypatch.setenv(BANNED_TAGS_FILE_ENV, str(f))
    blocked = blocked_tags()
    assert "localnoise" in blocked and "other" in blocked
    assert "dm" in blocked                       # built-ins are not replaced
    assert "a comment line" not in blocked


def test_an_unreadable_operator_list_degrades_to_the_builtin(monkeypatch):
    monkeypatch.setenv(BANNED_TAGS_FILE_ENV, "/nope/does/not/exist.txt")
    assert "dm" in blocked_tags()                # no raise, built-ins intact
