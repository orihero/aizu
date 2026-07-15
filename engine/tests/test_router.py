import os
import tempfile

from reelradar.core.router import (OpenRouterRouter, _content_or_none,
                              _extract_json, _decision_from_payload)
from reelradar.core.store import Store


def test_router_model_resolves_explicit_then_env_then_default(monkeypatch):
    """The panel builds the router with NO model arg, so it must honor
    OPENROUTER_TEXT_MODEL / _VISION_MODEL from the environment (regression: it pinned
    the dead `owl-alpha` default and ignored .env). Explicit args still win."""
    monkeypatch.setenv("OPENROUTER_TEXT_MODEL", "openai/gpt-4o-mini")
    monkeypatch.setenv("OPENROUTER_VISION_MODEL", "openai/gpt-4o-mini")
    r = OpenRouterRouter(store=None, api_key="x")          # panel-style: no model
    assert r.text_model == "openai/gpt-4o-mini"
    assert r.vision_model == "openai/gpt-4o-mini"

    explicit = OpenRouterRouter(store=None, api_key="x", text_model="anthropic/claude-3.5-haiku")
    assert explicit.text_model == "anthropic/claude-3.5-haiku"   # explicit arg wins

    monkeypatch.delenv("OPENROUTER_TEXT_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_VISION_MODEL", raising=False)
    fallback = OpenRouterRouter(store=None, api_key="x")
    assert fallback.text_model == OpenRouterRouter._DEFAULT_TEXT_MODEL


def test_extract_json_tolerant():
    assert _extract_json('```json\n{"label":"x","score":0.5}\n```')["label"] == "x"
    assert _extract_json('prefix {"a":1} suffix')["a"] == 1
    assert _extract_json("no json here") == {}


def test_content_or_none_tolerates_malformed_bodies():
    good = {"choices": [{"message": {"content": '{"label":"relevant"}'}}]}
    assert _content_or_none(good) == '{"label":"relevant"}'
    # shapes flaky/free models return with HTTP 200 — must not raise
    assert _content_or_none({"error": {"message": "rate limited"}}) is None
    assert _content_or_none({"choices": []}) is None
    assert _content_or_none({"choices": [{"message": {"content": None}}]}) is None
    assert _content_or_none({"choices": [{"message": {"content": "   "}}]}) is None
    assert _content_or_none({}) is None


def test_classify_text_degrades_on_malformed_200(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    store = Store(path)
    r = OpenRouterRouter(store=store, api_key="x", sleep=lambda _t: None)
    # 200 OK but no usable choices (the owl-alpha failure seen on live IG)
    monkeypatch.setattr(r, "_post", lambda payload: {"error": {"message": "no choices"}})
    d = r.classify_text(instruction="i", content="t", campaign_id="c", stage="relevance")
    assert d.tier == "degraded" and d.confidence == 0.0
    assert any(f["kind"] == "cloud_degraded" for f in store.open_flags())


def test_decision_clamps():
    d = _decision_from_payload({"label": "m", "score": 5, "confidence": -1}, "cloud", 0.0, "")
    assert d.score == 1.0 and d.confidence == 0.0


def test_spend_guard_degrades_over_cap():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    store = Store(path)
    store.log_spend("c", "match", 25.0)  # already over cap
    r = OpenRouterRouter(store=store, api_key="x", spend_cap_usd=20.0,
                         sleep=lambda _t: None)
    d = r.classify_text(instruction="i", content="t", campaign_id="c", stage="match")
    assert d.tier == "degraded" and d.confidence == 0.0
    flags = store.open_flags()
    assert any(f["kind"] == "spend_cap" for f in flags)


def test_post_retries_malformed_200_then_succeeds(monkeypatch):
    import reelradar.core.router as R

    class FakeResp:
        def __init__(self, body):
            self._b = body
            self.status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return self._b

    calls = {"n": 0}
    good = {"choices": [{"message": {"content": '{"label":"relevant","score":0.9,"confidence":0.9}'}}]}
    malformed = {"error": {"message": "no choices"}}

    def fake_post(url, headers, json, timeout):
        calls["n"] += 1
        return FakeResp(malformed if calls["n"] < 3 else good)

    monkeypatch.setattr(R.httpx, "post", fake_post)
    r = R.OpenRouterRouter(api_key="x", max_retries=5, sleep=lambda _t: None)
    d = r.classify_text(instruction="i", content="c", campaign_id="c", stage="relevance")
    assert d.tier == "cloud" and d.score == 0.9      # recovered, not degraded
    assert calls["n"] == 3                            # retried past 2 malformed 200s


def test_classify_image_sends_multiple_frames(monkeypatch):
    r = OpenRouterRouter(api_key="x", sleep=lambda _t: None)
    captured = {}

    def fake_post(payload):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": '{"label":"relevant","score":0.8,"confidence":0.9}'}}]}

    monkeypatch.setattr(r, "_post", fake_post)
    d = r.classify_image(instruction="brief", images_b64=["aa", "bb", "cc"],
                         campaign_id="c", stage="relevance")
    parts = captured["payload"]["messages"][1]["content"]
    image_parts = [p for p in parts if p["type"] == "image_url"]
    assert len(image_parts) == 3                     # all frames attached
    assert "data:image/jpeg;base64,bb" in image_parts[1]["image_url"]["url"]
    assert d.score == 0.8 and d.tier == "cloud"


def test_classify_text_requests_json_mode(monkeypatch):
    r = OpenRouterRouter(api_key="x", sleep=lambda _t: None)
    captured = {}

    def fake_post(payload):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": '{"label":"match","score":0.9,"confidence":0.9}'}}]}

    monkeypatch.setattr(r, "_post", fake_post)
    d = r.classify_text(instruction="i", content="c", campaign_id="c", stage="match")
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert d.tier == "cloud"


def test_classify_text_falls_back_when_model_rejects_json_mode(monkeypatch):
    """A model that 400s on response_format must not degrade every match call —
    the call retries once with the param stripped and still succeeds. (Stubs
    `_post`; the amplification/latch behaviour is covered at the httpx layer below.)"""
    r = OpenRouterRouter(api_key="x", sleep=lambda _t: None)
    seen = []

    def fake_post(payload):
        seen.append("response_format" in payload)
        if "response_format" in payload:
            raise RuntimeError("400: response_format not supported by model")
        return {"choices": [{"message": {"content": '{"label":"match","score":0.8,"confidence":0.9}'}}]}

    monkeypatch.setattr(r, "_post", fake_post)
    d = r.classify_text(instruction="i", content="c", campaign_id="c", stage="match")
    assert seen == [True, False]              # tried JSON mode, then without it
    assert d.tier == "cloud" and d.score == 0.8   # recovered, not degraded
    assert r._json_mode is False              # latched off for subsequent calls


def test_json_mode_latches_off_so_rejection_is_paid_once(monkeypatch):
    """At the real httpx layer: a model that always 400s on response_format costs
    one double-retry storm on the FIRST call, then JSON mode is latched off so
    every later call is a single clean request (no per-call amplification)."""
    import reelradar.core.router as R

    class FakeResp:
        def __init__(self, status, body):
            self.status_code = status
            self._b = body
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError("Client error '400 Bad Request'")
        def json(self):
            return self._b

    good = {"choices": [{"message": {"content": '{"label":"match","score":0.8,"confidence":0.9}'}}]}
    calls = []

    def fake_post(url, headers, json, timeout):
        has_rf = "response_format" in json
        calls.append(has_rf)
        return FakeResp(400, {"error": {"message": "response_format unsupported"}}) if has_rf \
            else FakeResp(200, good)

    monkeypatch.setattr(R.httpx, "post", fake_post)
    r = R.OpenRouterRouter(api_key="x", max_retries=3, sleep=lambda _t: None)

    d1 = r.classify_text(instruction="i", content="c", campaign_id="c", stage="match")
    assert d1.tier == "cloud" and d1.score == 0.8   # recovered via stripped retry
    assert calls[-1] is False                        # the call that succeeded had no JSON mode
    assert r._json_mode is False                     # …and it's now latched off

    calls.clear()
    d2 = r.classify_text(instruction="i", content="c", campaign_id="c", stage="match")
    assert d2.score == 0.8
    assert calls == [False]                           # single clean call, no wasted JSON-mode retries


def test_malformed_200_degrades_without_disabling_json_mode(monkeypatch):
    """A flaky malformed-200 (the owl-alpha failure mode) is NOT a param rejection:
    it must degrade as before, NOT trigger a no-JSON retry or latch JSON mode off
    (the next call should try JSON mode again — the failure was transient)."""
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    store = Store(path)
    r = OpenRouterRouter(store=store, api_key="x", sleep=lambda _t: None)
    seen = []

    def fake_post(payload):
        seen.append("response_format" in payload)
        raise RuntimeError("OpenRouter failed after 3 retries: malformed 200: no usable choices")

    monkeypatch.setattr(r, "_post", fake_post)
    d = r.classify_text(instruction="i", content="t", campaign_id="c", stage="match")
    assert d.tier == "degraded"
    assert seen == [True]            # did NOT retry without JSON mode
    assert r._json_mode is True      # not latched off by a transient malformed 200


def test_classify_image_no_frames_degrades():
    r = OpenRouterRouter(api_key="x", sleep=lambda _t: None)
    d = r.classify_image(instruction="b", images_b64=[], campaign_id="c", stage="relevance")
    assert d.tier == "degraded"


def test_generate_json_returns_parsed_dict(monkeypatch):
    r = OpenRouterRouter(api_key="x", sleep=lambda _t: None)
    monkeypatch.setattr(r, "_post", lambda payload: {
        "choices": [{"message": {"content": '```json\n{"name":"Acme","threshold":0.7}\n```'}}]})
    out = r.generate_json(system="s", user="u")
    assert out == {"name": "Acme", "threshold": 0.7}     # tolerant parse of fenced JSON


def test_generate_json_requests_json_mode(monkeypatch):
    r = OpenRouterRouter(api_key="x", sleep=lambda _t: None)
    captured = {}

    def fake_post(payload):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": '{"ok":true}'}}]}

    monkeypatch.setattr(r, "_post", fake_post)
    r.generate_json(system="s", user="u")
    assert captured["payload"]["response_format"] == {"type": "json_object"}


def test_generate_json_sends_image_parts(monkeypatch):
    r = OpenRouterRouter(api_key="x", sleep=lambda _t: None)
    captured = {}

    def fake_post(payload):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": '{"caption":"a shoe"}'}}]}

    monkeypatch.setattr(r, "_post", fake_post)
    out = r.generate_json(system="s", user="describe", images_b64=["zz"],
                          model="vision-x")
    parts = captured["payload"]["messages"][1]["content"]
    image_parts = [p for p in parts if p["type"] == "image_url"]
    assert "data:image/jpeg;base64,zz" in image_parts[0]["image_url"]["url"]
    assert captured["payload"]["model"] == "vision-x"
    assert out == {"caption": "a shoe"}


def test_generate_json_returns_empty_on_malformed_200(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    store = Store(path)
    r = OpenRouterRouter(store=store, api_key="x", sleep=lambda _t: None)
    monkeypatch.setattr(r, "_post", lambda payload: {"error": {"message": "no choices"}})
    assert r.generate_json(system="s", user="u") == {}   # never raises; degrades to {}


def _resp(label, score, confidence=0.9):
    return {"choices": [{"message": {
        "content": f'{{"label":"{label}","score":{score},"confidence":{confidence}}}'}}]}


def test_compare_models_resolves_explicit_then_env(monkeypatch):
    monkeypatch.setenv("MODEL_COMPARISON_MODELS", " model-a, model-b ,model-c")
    r = OpenRouterRouter(api_key="x")
    assert r.compare_models == ["model-a", "model-b", "model-c"]

    explicit = OpenRouterRouter(api_key="x", compare_models=["only-this"])
    assert explicit.compare_models == ["only-this"]

    monkeypatch.delenv("MODEL_COMPARISON_MODELS", raising=False)
    assert OpenRouterRouter(api_key="x").compare_models == []


def test_comparison_off_makes_zero_extra_calls(monkeypatch):
    """The core 'off = unchanged' guarantee: no enable_comparison flag (default
    False) means classify_text never enters the fan-out branch, whatever models
    are configured."""
    r = OpenRouterRouter(api_key="x", compare_models=["model-b"], sleep=lambda _t: None)
    calls = {"n": 0}

    def fake_post(payload):
        calls["n"] += 1
        return _resp("match", 0.9)

    monkeypatch.setattr(r, "_post", fake_post)
    d = r.classify_text(instruction="i", content="c", campaign_id="c", stage="match")
    assert calls["n"] == 1
    assert d.comparisons == []
    assert d.model == r.text_model


def test_comparison_scoped_to_match_stage_only(monkeypatch):
    r = OpenRouterRouter(api_key="x", compare_models=["model-b"],
                         enable_comparison=True, sleep=lambda _t: None)
    calls = {"n": 0}

    def fake_post(payload):
        calls["n"] += 1
        return _resp("relevant", 0.9)

    monkeypatch.setattr(r, "_post", fake_post)
    d = r.classify_text(instruction="i", content="c", campaign_id="c", stage="relevance")
    assert calls["n"] == 1                 # no fan-out on relevance
    assert d.comparisons == []


def test_comparison_on_populates_comparisons_without_touching_primary(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    store = Store(path)
    r = OpenRouterRouter(store=store, api_key="x", text_model="prod-model",
                         compare_models=["model-b", "model-c"],
                         enable_comparison=True, sleep=lambda _t: None)

    def fake_post(payload):
        model = payload["model"]
        if model == "prod-model":
            return _resp("match", 0.9)
        if model == "model-b":
            return _resp("match", 0.8)
        return _resp("no", 0.2)   # model-c disagrees

    monkeypatch.setattr(r, "_post", fake_post)
    d = r.classify_text(instruction="i", content="c", campaign_id="c",
                        stage="match", threshold=0.5)

    assert d.model == "prod-model" and d.score == 0.9 and d.tier == "cloud"
    by_model = {c["model"]: c for c in d.comparisons}
    assert by_model["model-b"]["score"] == 0.8 and by_model["model-b"]["error"] is None
    assert by_model["model-c"]["score"] == 0.2

    stats = {s["model"]: s for s in store.model_comparison_stats()}
    assert stats["prod-model"]["isPrimary"] is True
    assert stats["model-b"]["agreementRate"] == 1.0   # both >= 0.5
    assert stats["model-c"]["agreementRate"] == 0.0   # primary matched, model-c didn't


def test_comparison_model_failure_is_isolated(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    store = Store(path)
    r = OpenRouterRouter(store=store, api_key="x", text_model="prod-model",
                         compare_models=["flaky-model"],
                         enable_comparison=True, sleep=lambda _t: None)

    def fake_post(payload):
        if payload["model"] == "flaky-model":
            raise RuntimeError("network down")
        return _resp("match", 0.9)

    monkeypatch.setattr(r, "_post", fake_post)
    d = r.classify_text(instruction="i", content="c", campaign_id="c", stage="match")

    assert d.tier == "cloud" and d.score == 0.9        # primary unaffected
    assert store.open_flags() == []                    # no degrade flag from the comparison
    assert d.comparisons[0]["error"] is not None
    stats = {s["model"]: s for s in store.model_comparison_stats()}
    assert stats["flaky-model"]["errors"] == 1


def test_retry_then_degrade_on_network_failure(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    store = Store(path)
    r = OpenRouterRouter(store=store, api_key="x", max_retries=2,
                         sleep=lambda _t: None)

    def boom(payload):
        raise RuntimeError("network down")
    monkeypatch.setattr(r, "_post", boom)
    d = r.classify_text(instruction="i", content="t", campaign_id="c", stage="relevance")
    assert d.tier == "degraded"
    assert any(f["kind"] == "cloud_degraded" for f in store.open_flags())
