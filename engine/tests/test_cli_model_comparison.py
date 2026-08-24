"""cli._build_run_io resolves the model-comparison fan-out flags exactly once per
live run: the superadmin DB toggle (in-process) OR a box-local env override
(distributed workers, which never see the cloud's platform_settings row) — models
are always env-declared. Off by default so an untouched .env/.db is unchanged."""
import argparse
import os
import tempfile

import pytest

import aizu.cli as cli
from aizu.core.feed import FeedSource
from aizu.core.store import Store


class _Campaign:
    campaign_id = "c-test"
    platform = "instagram"
    seed_hashtags = ()
    seed_accounts = ()
    seed_channels = ()
    include_home_feed = False


def _args():
    return argparse.Namespace(spend_cap=20.0, text_model=None, vision_model=None,
                              cdp_url="http://127.0.0.1:9222")


@pytest.fixture(autouse=True)
def _stub_feed_and_creds(monkeypatch):
    monkeypatch.setattr(cli, "_resolve_platform_credentials", lambda *_a, **_k: None)
    # A real FeedSource, not a bare object(): _build_run_io attaches the
    # per-source accounting sink to the feed it just built, so the stub has to
    # honour the interface it is standing in for.
    monkeypatch.setattr("aizu.dispatch.build_feed", lambda *_a, **_k: FeedSource())
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")


@pytest.fixture
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = Store(path)
    yield s
    s.close()


def test_comparison_off_by_default(store, monkeypatch):
    monkeypatch.delenv("MODEL_COMPARISON_ENABLED", raising=False)
    monkeypatch.delenv("MODEL_COMPARISON_MODELS", raising=False)
    router, _, _ = cli._build_run_io(_Campaign(), store, dry_run=False, args=_args())
    assert router.enable_comparison is False
    assert router.compare_models == []


def test_superadmin_toggle_enables_for_in_process_run(store, monkeypatch):
    monkeypatch.setenv("MODEL_COMPARISON_MODELS", "candidate-a,candidate-b")
    store.set_model_comparison_enabled(True)
    router, _, _ = cli._build_run_io(_Campaign(), store, dry_run=False, args=_args())
    assert router.enable_comparison is True
    assert router.compare_models == ["candidate-a", "candidate-b"]


def test_env_override_enables_when_store_flag_is_off(store, monkeypatch):
    """Distributed-worker box: its local Store's platform_settings row was never
    written by the superadmin panel (that's the cloud DB), so the env override is
    the only way that box can opt in."""
    monkeypatch.setenv("MODEL_COMPARISON_ENABLED", "1")
    monkeypatch.setenv("MODEL_COMPARISON_MODELS", "candidate-a")
    assert store.model_comparison_enabled() is False   # confirm the DB side is off
    router, _, _ = cli._build_run_io(_Campaign(), store, dry_run=False, args=_args())
    assert router.enable_comparison is True


def test_no_store_falls_back_to_env_only(monkeypatch):
    monkeypatch.setenv("MODEL_COMPARISON_ENABLED", "true")
    monkeypatch.setenv("MODEL_COMPARISON_MODELS", "candidate-a")
    router, _, _ = cli._build_run_io(_Campaign(), None, dry_run=False, args=_args())
    assert router.enable_comparison is True
    assert router.compare_models == ["candidate-a"]


# ----- model resolution: the CLI must not shadow the router's chain -----
# (memory/known-issues.md C0 — both CLI literals were dead ids on OpenRouter,
# and the explicit pass made AIZU_TEXT_MODEL unreachable on the CLI path.)

def test_the_cli_supplies_no_model_default_of_its_own():
    from aizu.cli import build_parser
    args = build_parser().parse_args(["run", "--campaign", "c"])
    assert args.text_model is None
    assert args.vision_model is None


def test_an_explicit_cli_flag_still_wins(monkeypatch):
    from aizu.cli import build_parser
    args = build_parser().parse_args(
        ["run", "--campaign", "c", "--text-model", "pinned/model"])
    assert args.text_model == "pinned/model"


def test_the_local_ollama_knob_is_reachable_on_the_cli_path(monkeypatch):
    """`AIZU_TEXT_MODEL` sits ABOVE `OPENROUTER_TEXT_MODEL` in the router's chain
    (core/router.py:364-370) precisely so a run can point at a local VL model
    without disturbing the cloud defaults. The CLI's literal default made it
    unreachable, because an explicit argument outranks the entire chain."""
    from aizu.core.router import OpenRouterRouter
    monkeypatch.setenv("AIZU_TEXT_MODEL", "local/qwen3-vl")
    monkeypatch.setenv("OPENROUTER_TEXT_MODEL", "cloud/other")
    assert OpenRouterRouter(api_key="x", text_model=None).text_model == "local/qwen3-vl"


def test_no_dead_model_literal_survives_in_the_cli(monkeypatch):
    import pathlib
    src = pathlib.Path(__file__).parents[1] / "aizu" / "cli.py"
    body = src.read_text()
    for dead in ("openrouter/owl-alpha", "nex-agi/nex-n2-pro:free"):
        assert dead not in body, f"cli.py still hardcodes the dead id {dead}"
