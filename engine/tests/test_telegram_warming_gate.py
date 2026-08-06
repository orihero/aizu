"""Thin LLM relevance gate for TG warming search hits (warming-writes PRD §7.4).

The gate is the single ML touch on any warming path. It must: degrade to None
(seeded-only) without an API key, parse a JSON {"relevant": bool} reply behind a
tolerant never-throw boundary, and FAIL-CLOSED (relevant=False) on any parse
failure / transport error / missing key.
"""
from pathlib import Path

from aizu.core.config import Campaign
from aizu.engines.telegram.warming_writes import TgChannel
from aizu.engines.warming.tg_relevance import build_relevance_gate


def _campaign():
    return Campaign(
        campaign_id="c", goal="find saas leads", threshold=0.7,
        escalate_band=(0.4, 0.75), language_mix=[], relevance_def="",
        match_def="", extract_def="", seed_direction="", raw="",
        path=Path("<test>"), platform="telegram", engine_mode="warming")


def _channel():
    return TgChannel(username="@growthlab", title="Growth", participants=4200)


def _reply(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def test_no_api_key_degrades_to_none():
    assert build_relevance_gate(api_key="") is None


def test_explicit_post_builds_a_gate_even_without_key():
    gate = build_relevance_gate(api_key="", post=lambda payload: _reply('{"relevant": true}'))
    assert gate is not None


def test_gate_returns_true_on_relevant_reply():
    gate = build_relevance_gate(api_key="k",
                                post=lambda p: _reply('{"relevant": true}'))
    assert gate(_channel(), _campaign()) is True


def test_gate_returns_false_on_irrelevant_reply():
    gate = build_relevance_gate(api_key="k",
                                post=lambda p: _reply('{"relevant": false}'))
    assert gate(_channel(), _campaign()) is False


def test_gate_fail_closed_on_malformed_json():
    gate = build_relevance_gate(api_key="k",
                                post=lambda p: _reply("not json at all"))
    assert gate(_channel(), _campaign()) is False


def test_gate_fail_closed_on_missing_key():
    gate = build_relevance_gate(api_key="k", post=lambda p: _reply('{"other": 1}'))
    assert gate(_channel(), _campaign()) is False


def test_gate_fail_closed_on_transport_error():
    def _boom(payload):
        raise RuntimeError("network down")

    gate = build_relevance_gate(api_key="k", post=_boom)
    assert gate(_channel(), _campaign()) is False


def test_gate_tolerates_fenced_json():
    gate = build_relevance_gate(
        api_key="k",
        post=lambda p: _reply('```json\n{"relevant": true}\n```'))
    assert gate(_channel(), _campaign()) is True


def test_gate_coerces_string_boolean():
    gate = build_relevance_gate(api_key="k",
                                post=lambda p: _reply('{"relevant": "yes"}'))
    assert gate(_channel(), _campaign()) is True
