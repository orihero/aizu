"""Store-level model-comparison fan-out (v17): the superadmin switch, the per-call
log, and the aggregate stats the "Model Performance" page reads."""
import os
import tempfile

from reelradar.core.store import MODEL_COMPARISON_ENABLED_KEY, Store


def fresh_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path)


# ----- superadmin switch -----

def test_model_comparison_disabled_by_default():
    store = fresh_store()
    assert store.model_comparison_enabled() is False


def test_set_model_comparison_enabled_roundtrips():
    store = fresh_store()
    store.set_model_comparison_enabled(True, by="admin@example.com")
    assert store.model_comparison_enabled() is True
    row = store._conn.execute(
        "SELECT updated_by FROM platform_settings WHERE key=?",
        (MODEL_COMPARISON_ENABLED_KEY,)).fetchone()
    assert row["updated_by"] == "admin@example.com"
    store.set_model_comparison_enabled(False)
    assert store.model_comparison_enabled() is False


# ----- found_by_models on matches -----

def test_upsert_match_persists_found_by_models():
    store = fresh_store()
    kw = dict(campaign_id="c", reel_id="r", comment_id="cm1", username="u",
              text="hi", lang="en", score=0.8, reason="x", extracted=None, tier="cloud")
    store.upsert_match(found_by_models=["prod-model", "candidate-a"], **kw)
    assert store.matches("c")[0]["found_by_models"] == ["prod-model", "candidate-a"]


def test_upsert_match_found_by_models_refreshes_on_repoll():
    store = fresh_store()
    kw = dict(campaign_id="c", reel_id="r", comment_id="cm1", username="u",
              text="hi", lang="en", score=0.8, reason="x", extracted=None, tier="cloud")
    store.upsert_match(found_by_models=["prod-model"], **kw)
    store.upsert_match(found_by_models=["prod-model", "candidate-b"], **kw)
    assert store.matches("c")[0]["found_by_models"] == ["prod-model", "candidate-b"]


def test_upsert_match_defaults_found_by_models_to_none():
    store = fresh_store()
    store.upsert_match(campaign_id="c", reel_id="r", comment_id="cm1", username="u",
                       text="hi", lang="en", score=0.8, reason="x", extracted=None,
                       tier="cloud")
    assert store.matches("c")[0]["found_by_models"] in (None, [])


# ----- log_model_comparison / model_comparison_stats / _recent -----

def _log(store, model, *, is_primary=False, score=0.7, latency_ms=500.0,
         usd=0.001, agreed=None, error=None, campaign_id="c"):
    store.log_model_comparison(
        campaign_id=campaign_id, stage="match", model=model, is_primary=is_primary,
        session_id="s1", platform="instagram", label="match", score=score,
        confidence=0.9, agreed=agreed, latency_ms=latency_ms, usd=usd, error=error)


def test_model_comparison_stats_aggregates_per_model():
    store = fresh_store()
    _log(store, "prod-model", is_primary=True, score=0.8, latency_ms=400.0, usd=0.002)
    _log(store, "prod-model", is_primary=True, score=0.6, latency_ms=600.0, usd=0.002)
    _log(store, "candidate-a", score=0.7, latency_ms=900.0, usd=0.0005, agreed=True)
    _log(store, "candidate-a", score=0.3, latency_ms=1100.0, usd=0.0005, agreed=False)

    stats = {r["model"]: r for r in store.model_comparison_stats()}

    assert stats["prod-model"]["isPrimary"] is True
    assert stats["prod-model"]["calls"] == 2
    assert stats["prod-model"]["avgLatencyMs"] == 500.0
    assert stats["candidate-a"]["isPrimary"] is False
    assert stats["candidate-a"]["calls"] == 2
    assert stats["candidate-a"]["agreementRate"] == 0.5


def test_model_comparison_stats_counts_errors_and_leads_found():
    store = fresh_store()
    _log(store, "candidate-a", error="timeout")
    _log(store, "candidate-a", error=None)
    store.upsert_match(campaign_id="c", reel_id="r", comment_id="cm1", username="u",
                       text="hi", lang="en", score=0.8, reason="x", extracted=None,
                       tier="cloud", found_by_models=["prod-model", "candidate-a"])

    stats = {r["model"]: r for r in store.model_comparison_stats()}
    assert stats["candidate-a"]["errors"] == 1
    assert stats["candidate-a"]["leadsFound"] == 1


def test_model_comparison_stats_scoped_to_campaign():
    store = fresh_store()
    _log(store, "candidate-a", campaign_id="c1")
    _log(store, "candidate-a", campaign_id="c2")
    _log(store, "candidate-a", campaign_id="c2")

    assert store.model_comparison_stats(campaign_id="c1")[0]["calls"] == 1
    assert store.model_comparison_stats(campaign_id="c2")[0]["calls"] == 2


def test_model_comparison_recent_orders_newest_first():
    store = fresh_store()
    _log(store, "candidate-a")
    _log(store, "candidate-b")

    rows = store.model_comparison_recent(limit=10)
    assert [r["model"] for r in rows] == ["candidate-b", "candidate-a"]


def test_model_comparison_recent_respects_limit():
    store = fresh_store()
    for i in range(5):
        _log(store, f"candidate-{i}")

    assert len(store.model_comparison_recent(limit=2)) == 2
