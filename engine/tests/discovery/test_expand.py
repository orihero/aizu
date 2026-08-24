"""Seed expansion end to end (Remedy Sheet #1 / Remedy A).

The generator's job shrinks to naming nouns; everything else must come from an
oracle. The load-bearing guarantees: nothing is invented, the locale layers only
fire for locales the brief actually declared, and a dead network degrades the
result instead of the run.
"""
import json

from aizu.discovery.autocomplete import SuggestClient
from aizu.discovery.expand import expand_seeds


def _client(completions, scores=None):
    body = json.dumps(["q", completions, [], [],
                       {"google:suggestrelevance": scores or []}]).encode()
    return SuggestClient(opener=lambda _u: body, sleep=lambda _s: None)


def test_offline_expansion_is_deterministic_and_flagged_degraded():
    r = expand_seeds(["remont"], langs=["uz"], online=False)
    assert r.degraded is True
    assert "remont" in r.hashtags()
    assert all("autocomplete" not in c.origins for c in r.candidates)


def test_script_variants_only_fire_for_cyrillic_audiences():
    """An English brief must not acquire a transliterated tag no human types."""
    en = expand_seeds(["marathon"], langs=["en"], online=False)
    assert en.hashtags() == ["marathon"]
    uz = expand_seeds(["remont"], langs=["uz"], online=False)
    assert "ремонт" in uz.hashtags()


def test_a_brief_with_no_declared_languages_gets_no_locale_guessing():
    r = expand_seeds(["remont"], langs=[], online=False)
    assert r.hashtags() == ["remont"]
    assert r.queries == []


def test_the_seed_nouns_always_outrank_everything_derived():
    r = expand_seeds(["remont"], langs=["uz"], online=False)
    assert r.candidates[0].term == "remont"
    assert r.candidates[0].origins == ["seed"]


def test_evidence_from_two_layers_beats_evidence_from_one():
    # "ремонт" is both a script variant AND a live completion; a completion that
    # is only a completion must rank below it.
    client = _client(["ремонт", "stranger"], [900, 1000])
    r = expand_seeds(["remont"], langs=["uz"], client=client)
    terms = [c.term for c in r.candidates]
    assert terms.index("ремонт") < terms.index("stranger")


def test_multi_word_completions_are_queries_not_hashtags():
    client = _client(["remont narxi qancha"], [900])
    r = expand_seeds(["remont"], langs=["uz"], client=client)
    assert "remont narxi qancha" not in r.hashtags()
    assert any(c.term == "remont narxi qancha" for c in r.candidates)


def test_request_patterns_become_search_queries():
    r = expand_seeds(["videograf"], langs=["uz"], online=False)
    assert "videograf kerak" in r.queries


def test_a_dead_endpoint_degrades_rather_than_raising():
    boom = SuggestClient(opener=lambda _u: (_ for _ in ()).throw(OSError("down")),
                         sleep=lambda _s: None)
    r = expand_seeds(["remont"], langs=["uz"], client=boom)
    assert r.degraded is True
    assert "remont" in r.hashtags()      # deterministic layers still delivered


def test_empty_input_is_not_an_error():
    r = expand_seeds([], langs=["uz"])
    assert r.candidates == [] and r.queries == []


def test_hashtag_limit_is_honoured():
    client = _client([f"t{i}" for i in range(50)], [900] * 50)
    assert len(expand_seeds(["remont"], langs=["uz"], client=client)
               .hashtags(limit=5)) == 5


def test_result_serializes_for_the_api_boundary():
    d = expand_seeds(["remont"], langs=["uz"], online=False).as_dict()
    assert set(d) == {"candidates", "queries", "degraded"}
    assert set(d["candidates"][0]) == {"term", "origins", "relevance", "lang", "score"}
    json.dumps(d)      # must be plain-JSON serializable
