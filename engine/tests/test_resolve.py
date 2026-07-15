"""Tests for `resolve_campaign` — the shared runnability rule used by the CLI
`--campaign` path, the batch runner, and the bridge server's /api/run handler."""
import os
import tempfile
from pathlib import Path

import pytest

from reelradar.core.config import load_campaign, resolve_campaign
from reelradar.core.store import Store

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"


@pytest.fixture()
def store():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = Store(db_path)
    yield s
    s.close()
    os.unlink(db_path)


def _file_campaign_id() -> str:
    return load_campaign(CONFIG / "campaign.md").campaign_id


def test_resolves_db_brief(store):
    # Arrange — a panel-authored brief in the DB.
    store.upsert_campaign_brief("db-camp", {"platform": "youtube", "threshold": 0.8,
                                            "relevance_def": "saas product"})
    # Act
    campaign = resolve_campaign(store, CONFIG, "db-camp")
    # Assert — the brief drives the resolved Campaign.
    assert campaign is not None
    assert campaign.platform == "youtube"
    assert campaign.threshold == 0.8


def test_resolves_file_campaign_by_id(store):
    # The file campaign has no brief row but matches by its own id.
    cid = _file_campaign_id()
    campaign = resolve_campaign(store, CONFIG, cid)
    assert campaign is not None
    assert campaign.campaign_id == cid


def test_unknown_id_is_not_runnable(store):
    assert resolve_campaign(store, CONFIG, "no-such-campaign") is None


def test_malformed_brief_raises(store):
    store.upsert_campaign_brief("bad-camp", {"platform": "tiktok"})  # unsupported platform
    with pytest.raises(ValueError):
        resolve_campaign(store, CONFIG, "bad-camp")
