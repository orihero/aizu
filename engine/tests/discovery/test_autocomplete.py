"""Autocomplete mining (Remedy Sheet #1 / Remedy A.1).

This is an UNOFFICIAL endpoint, so the contract under test is mostly about how it
degrades: a throttled or malformed response must cost candidates, never raise, and
a sustained throttle must stop the sweep instead of grinding through an alphabet
collecting empty bodies.
"""
import json
import urllib.error

import pytest

from aizu.discovery.autocomplete import (LATIN_ALPHABET, SuggestClient, _parse,
                                         mine, mine_many)


def _body(query, completions, scores=None):
    meta = {"google:suggestrelevance": scores} if scores else {}
    return json.dumps([query, completions, [], [], meta]).encode()


def _client(responses, **kw):
    """A client whose opener replays canned bodies (or raises them)."""
    calls = []

    def opener(url):
        calls.append(url)
        item = responses.pop(0) if responses else _body("", [])
        if isinstance(item, Exception):
            raise item
        return item

    c = SuggestClient(opener=opener, sleep=lambda _s: None, **kw)
    c.calls = calls
    return c


def test_parses_completions_and_relevance():
    c = _client([_body("remont", ["remont narxi", "remont uz"], [900, 600])])
    out = c.suggest("remont")
    assert [(s.query, s.relevance) for s in out] == [
        ("remont narxi", 900), ("remont uz", 600)]
    assert all(s.source == "remont" for s in out)


def test_locale_params_are_sent():
    c = _client([_body("q", [])], hl="uz", gl="UZ", ds="yt")
    c.suggest("remont")
    url = c.calls[0]
    assert "hl=uz" in url and "gl=UZ" in url and "ds=yt" in url


def test_missing_relevance_array_is_not_an_error():
    c = _client([_body("q", ["a", "b"])])
    assert [s.relevance for s in c.suggest("q")] == [0, 0]


@pytest.mark.parametrize("raw", [b"", b"not json", b"{}", b"[]", b'["q"]',
                                 b'["q", "not-a-list"]'])
def test_malformed_bodies_yield_no_suggestions(raw):
    assert _parse(raw, "q") == []


def test_non_string_completions_are_skipped():
    assert [s.query for s in _parse(_body("q", ["a", None, 7, " ", "b"]), "q")] \
        == ["a", "b"]


def test_a_network_error_returns_empty_and_never_raises():
    c = _client([OSError("dns is down")])
    assert c.suggest("remont") == []


def test_repeated_throttling_exhausts_the_client():
    err = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
    c = _client([err, err])
    c.suggest("a")
    assert not c.exhausted
    c.suggest("b")
    assert c.exhausted
    assert c.suggest("c") == []          # no further requests are even attempted
    assert len(c.calls) == 2


def test_a_success_resets_the_failure_streak():
    err = urllib.error.HTTPError("u", 503, "unavailable", {}, None)
    c = _client([err, _body("q", ["ok"]), err])
    c.suggest("a")
    c.suggest("b")
    c.suggest("c")
    assert not c.exhausted


def test_mine_sweeps_the_alphabet_and_keeps_the_best_score():
    # The same completion under two probes keeps its highest relevance.
    responses = [_body("remont", ["remont narxi"], [300])] \
        + [_body("remont", ["remont narxi"], [900])] \
        + [_body("remont", []) for _ in LATIN_ALPHABET]
    c = _client(responses)
    out = mine("remont", client=c)
    assert [(s.query, s.relevance) for s in out] == [("remont narxi", 900)]
    assert len(c.calls) == 1 + len(LATIN_ALPHABET)


def test_mine_stops_early_when_throttled():
    err = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
    c = _client([err, err] + [_body("q", ["late"]) for _ in range(30)])
    assert mine("remont", client=c) == []
    assert len(c.calls) == 2             # did not grind through the alphabet


def test_mine_strips_a_leading_hash_and_ignores_blanks():
    c = _client([_body("q", ["hit"])] + [_body("q", []) for _ in range(40)])
    assert [s.query for s in mine("#remont", client=c)] == ["hit"]
    assert mine("  ", client=c) == []


def test_prefixes_produce_question_probes():
    c = _client([_body("q", []) for _ in range(40)])
    mine("remont", client=c, alphabets=(), prefixes=("qancha",))
    assert any("qancha+remont" in u or "qancha%20remont" in u for u in c.calls)


def test_mine_many_shares_one_failure_budget():
    err = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
    c = _client([err, err] + [_body("q", ["late"]) for _ in range(50)])
    assert mine_many(["a", "b", "c"], client=c, alphabets=()) == []
    assert len(c.calls) == 2
