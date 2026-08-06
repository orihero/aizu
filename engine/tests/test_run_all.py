"""Tests for the `run-all` batch CLI — runs every live campaign sequentially,
skips non-live ones, and stops the batch on a halt."""
import json
import os
import tempfile
from pathlib import Path

import pytest

from aizu import cli
from aizu.core.config import campaign_from_brief, load_campaign
from aizu.core.store import Store

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"

_MIN_BRIEF = {"platform": "instagram", "threshold": 0.7, "relevance_def": "x",
              "match_def": "y", "extract_def": "z"}


@pytest.fixture()
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    os.unlink(path)


def _seed(path: str) -> None:
    """Two live campaigns with briefs, one paused, and the file campaign paused
    so only the two seeded live campaigns are eligible."""
    store = Store(path)
    for cid in ("camp-a", "camp-b"):
        store.upsert_campaign_meta(cid, status="live")
        store.upsert_campaign_brief(cid, _MIN_BRIEF)
    store.upsert_campaign_meta("camp-c", status="paused")
    store.upsert_campaign_brief("camp-c", _MIN_BRIEF)
    store.upsert_campaign_meta(load_campaign(CONFIG / "campaign.md").campaign_id,
                               status="paused")
    store.close()


def test_run_all_runs_only_live(db_path, capsys):
    _seed(db_path)
    args = cli.build_parser().parse_args(
        ["--db", db_path, "run-all", "--config", str(CONFIG), "--dry-run"])
    code = args.func(args)
    out = json.loads(capsys.readouterr().out)

    assert code == 0
    assert out["ran"] == 2 and out["ok"] == 2 and out["halted"] == 0
    ran_ids = {r["campaignId"] for r in out["results"]}
    assert ran_ids == {"camp-a", "camp-b"}      # paused + paused-file excluded
    assert all(r["ok"] for r in out["results"])


def test_run_all_excludes_archived_live_campaign(db_path, capsys):
    """v12: an archived campaign is barred from the batch even while status='live'
    (the centralized runnable predicate — live AND not archived)."""
    _seed(db_path)
    store = Store(db_path)
    store.set_campaign_archived("camp-b", True)   # still status='live', now archived
    store.close()
    args = cli.build_parser().parse_args(
        ["--db", db_path, "run-all", "--config", str(CONFIG), "--dry-run"])
    args.func(args)
    out = json.loads(capsys.readouterr().out)
    ran_ids = {r["campaignId"] for r in out["results"]}
    assert ran_ids == {"camp-a"}                  # archived camp-b excluded


def test_run_all_writes_sessions_for_each(db_path):
    _seed(db_path)
    args = cli.build_parser().parse_args(
        ["--db", db_path, "run-all", "--config", str(CONFIG), "--dry-run"])
    args.func(args)
    store = Store(db_path)
    try:
        assert len(store.all_sessions("camp-a")) == 1
        assert len(store.all_sessions("camp-b")) == 1
        assert store.all_sessions("camp-c") == []   # paused → never ran
    finally:
        store.close()


def test_run_all_excludes_orphan_file_campaign(db_path, capsys):
    # The file campaign (config/campaign.md) has NO meta row here. A batch must
    # NOT auto-run it — only campaigns explicitly registered live should run.
    store = Store(db_path)
    store.upsert_campaign_meta("camp-a", status="live")
    store.upsert_campaign_brief("camp-a", _MIN_BRIEF)
    store.close()
    args = cli.build_parser().parse_args(
        ["--db", db_path, "run-all", "--config", str(CONFIG), "--dry-run"])
    code = args.func(args)
    out = json.loads(capsys.readouterr().out)

    assert code == 0
    ran_ids = {r["campaignId"] for r in out["results"]}
    file_id = load_campaign(CONFIG / "campaign.md").campaign_id
    assert ran_ids == {"camp-a"}            # orphan file campaign NOT auto-run
    assert file_id not in ran_ids


def test_run_all_includes_file_campaign_when_registered_live(db_path, capsys):
    # When the operator registers the file campaign's id as a live meta row (no
    # brief), resolve_campaign still matches it by id, so the batch runs it.
    file_id = load_campaign(CONFIG / "campaign.md").campaign_id
    store = Store(db_path)
    store.upsert_campaign_meta(file_id, status="live")
    store.close()
    args = cli.build_parser().parse_args(
        ["--db", db_path, "run-all", "--config", str(CONFIG), "--dry-run"])
    code = args.func(args)
    out = json.loads(capsys.readouterr().out)

    assert code == 0
    assert file_id in {r["campaignId"] for r in out["results"]}


def test_run_all_halt_stops_batch(db_path, capsys, monkeypatch):
    _seed(db_path)
    seen: list[str] = []

    def fake_run_one(*, campaign, store, soul, dry_run, args):
        seen.append(campaign.campaign_id)
        # Halt on the first campaign; the rest must be skipped, not run. An
        # action_block is a CDP account-halt — it poisons the shared browser, so the
        # remaining CDP campaigns are skipped (a daytime/API halt would NOT poison).
        if len(seen) == 1:
            return {"session_id": "x", "halt_reason": "action-block detected",
                    "halt_kind": "action_block"}
        return {"matches": 1, "spend_usd": 0.0}

    monkeypatch.setattr(cli, "_run_one", fake_run_one)
    args = cli.build_parser().parse_args(
        ["--db", db_path, "run-all", "--config", str(CONFIG), "--dry-run"])
    code = args.func(args)
    out = json.loads(capsys.readouterr().out)

    assert code == 3                       # any halt → exit 3
    assert out["halted"] == 1
    assert len(seen) == 1                  # only the first was attempted
    halted = [r for r in out["results"] if r.get("halted")]
    skipped = [r for r in out["results"] if r.get("skipped")]
    assert len(halted) == 1 and len(skipped) == 1
    assert halted[0]["haltKind"] == "action_block"      # G2: entries carry haltKind
    assert skipped[0]["reason"] == "cdp batch halted"


def test_run_all_cdp_campaign_after_continued_api_halt(db_path, capsys, monkeypatch):
    """An API-platform halt (rate-limit, halt_kind None) is recorded and the batch
    CONTINUES — it must NOT poison the shared CDP browser, so a CDP campaign after it
    still runs. (CDP-first ordering is bypassed here to force the API-then-CDP order.)"""
    yt = campaign_from_brief("camp-yt", {"platform": "youtube", "seed_channels": ["UC1"]})
    ig = campaign_from_brief("camp-ig", {"platform": "instagram"})
    monkeypatch.setattr(cli, "_live_campaigns", lambda *_a, **_k: [yt, ig])

    seen: list[str] = []

    def fake_run_one(*, campaign, store, soul, dry_run, args):
        seen.append(campaign.campaign_id)
        if campaign.platform == "youtube":
            return {"session_id": "y", "halt_reason": "youtube quota", "halt_kind": None}
        return {"matches": 2, "spend_usd": 0.0}

    monkeypatch.setattr(cli, "_run_one", fake_run_one)
    args = cli.build_parser().parse_args(
        ["--db", db_path, "run-all", "--config", str(CONFIG), "--dry-run"])
    code = args.func(args)
    out = json.loads(capsys.readouterr().out)

    assert seen == ["camp-yt", "camp-ig"]      # API halt did NOT skip the CDP campaign
    assert code == 3                           # the API halt still flags the batch
    ig_result = next(r for r in out["results"] if r["campaignId"] == "camp-ig")
    assert ig_result["ok"] is True             # instagram ran to completion


def test_live_campaigns_cdp_ordered_before_api(db_path):
    """_live_campaigns runs CDP (warmed-Chrome) platforms before API platforms, so a
    poison halt skips only later CDP campaigns — never an API one it can't affect."""
    store = Store(db_path)
    for cid, plat, extra in [
        ("api-yt", "youtube", {"seed_channels": ["UC1"]}),
        ("cdp-ig", "instagram", {}),
        ("api-rd", "reddit", {"seed_channels": ["uzbekistan"]}),
        ("cdp-x", "x", {}),
    ]:
        store.upsert_campaign_meta(cid, status="live")
        store.upsert_campaign_brief(cid, {**_MIN_BRIEF, "platform": plat, **extra})
    store.close()
    store = Store(db_path)
    try:
        live = cli._live_campaigns(store, CONFIG)
    finally:
        store.close()
    platforms = [c.platform for c in live]
    cdp = {"instagram", "x", "linkedin"}
    cut = max(i for i, p in enumerate(platforms) if p in cdp)
    assert all(platforms[i] in cdp for i in range(cut + 1))   # CDP block first
    assert all(platforms[i] not in cdp for i in range(cut + 1, len(platforms)))
