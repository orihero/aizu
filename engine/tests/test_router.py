import os
import tempfile

from aizu.core.router import (OpenRouterRouter, _content_or_none,
                              _extract_json, _decision_from_payload)
from aizu.core.store import Store
from aizu.worker.job_runner import _effective_spend_cap


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
    monkeypatch.setattr(r, "_post", lambda payload, **_budget: {"error": {"message": "no choices"}})
    d = r.classify_text(instruction="i", content="t", campaign_id="c", stage="relevance")
    assert d.tier == "degraded" and d.confidence == 0.0
    assert any(f["kind"] == "cloud_degraded" for f in store.open_flags())


def test_decision_clamps():
    d = _decision_from_payload({"label": "m", "score": 5, "confidence": -1}, "cloud", 0.0, "")
    assert d.score == 1.0 and d.confidence == 0.0


def test_decision_carries_the_models_intent(monkeypatch):
    """v27: the MATCH contract asks for an `intent` key, and the Decision is the only
    thing standing between the reply and `matching.derive_intent`. While `Decision`
    had no such field the key was dropped on the floor here, and EVERY lead on every
    platform silently fell through to the deterministic fallback — the feature was
    inert on the wire while all of its own unit tests passed."""
    d = _decision_from_payload(
        {"label": "match", "score": 0.9, "intent": "Wants a price for the red pair"},
        "cloud", 0.0, "")
    assert d.intent == "Wants a price for the red pair"


def test_decision_intent_defaults_to_empty_for_a_prompt_that_omits_it():
    """Campaign-authored match prompts written before v27 do not emit the key. That
    is the case `derive_intent`'s fallback exists for, so it must arrive as "" — not
    None, and never as a TypeError that would take the whole classification down."""
    assert _decision_from_payload({"label": "match", "score": 0.9},
                                  "cloud", 0.0, "").intent == ""
    # A flaky model answering with a non-string must not blow up the parse either.
    assert isinstance(_decision_from_payload(
        {"label": "match", "score": 0.9, "intent": {"oops": 1}}, "cloud", 0.0, "").intent,
        str)


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


def test_spend_guard_honours_a_b9_adjusted_cap():
    """B9: the guard compares against the BOX-LOCAL total, so the effective cap handed
    to a run is re-based (`local + headroom`). A box that has spent $1 locally on a
    campaign already $18 into a $20 ceiling gets an effective cap of $2 — one more
    dollar of headroom — and the guard must trip on the very next dollar, not at $20."""
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    store = Store(path)
    store.log_spend("c", "match", 1.0)                 # this box's own local spend
    effective = _effective_spend_cap(1.0, 18.0, 20.0)  # = 1.0 + (20 - 18) = 3.0
    r = OpenRouterRouter(store=store, api_key="x", spend_cap_usd=effective,
                         sleep=lambda _t: None)
    assert r._spend_guard("c") is True                 # $1 local < $3 effective
    store.log_spend("c", "match", 2.0)                 # local now $3 == effective
    assert r._spend_guard("c") is False
    assert any(f["kind"] == "spend_cap" for f in store.open_flags())


def test_spend_guard_blocks_immediately_on_an_exhausted_budget():
    """An effective cap of exactly 0.0 must BLOCK, not read as 'uncapped' — the guard
    short-circuits only on `spend_cap_usd is None`."""
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    store = Store(path)
    effective = _effective_spend_cap(0.0, 25.0, 20.0)
    assert effective == 0.0
    r = OpenRouterRouter(store=store, api_key="x", spend_cap_usd=effective,
                         sleep=lambda _t: None)
    assert r._spend_guard("c") is False                # fresh box, $0 spent, still blocked


def test_post_retries_malformed_200_then_succeeds(monkeypatch):
    import aizu.core.router as R

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

    def fake_post(payload, **_budget):
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

    def fake_post(payload, **_budget):
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

    def fake_post(payload, **_budget):
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
    import aizu.core.router as R

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

    def fake_post(payload, **_budget):
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
    monkeypatch.setattr(r, "_post", lambda payload, **_budget: {
        "choices": [{"message": {"content": '```json\n{"name":"Acme","threshold":0.7}\n```'}}]})
    out = r.generate_json(system="s", user="u")
    assert out == {"name": "Acme", "threshold": 0.7}     # tolerant parse of fenced JSON


def test_generate_json_requests_json_mode(monkeypatch):
    r = OpenRouterRouter(api_key="x", sleep=lambda _t: None)
    captured = {}

    def fake_post(payload, **_budget):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": '{"ok":true}'}}]}

    monkeypatch.setattr(r, "_post", fake_post)
    r.generate_json(system="s", user="u")
    assert captured["payload"]["response_format"] == {"type": "json_object"}


def test_generate_json_sends_image_parts(monkeypatch):
    r = OpenRouterRouter(api_key="x", sleep=lambda _t: None)
    captured = {}

    def fake_post(payload, **_budget):
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
    monkeypatch.setattr(r, "_post", lambda payload, **_budget: {"error": {"message": "no choices"}})
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

    def fake_post(payload, **_budget):
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

    def fake_post(payload, **_budget):
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

    def fake_post(payload, **_budget):
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

    def fake_post(payload, **_budget):
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

    def boom(payload, **_budget):
        raise RuntimeError("network down")
    monkeypatch.setattr(r, "_post", boom)
    d = r.classify_text(instruction="i", content="t", campaign_id="c", stage="relevance")
    assert d.tier == "degraded"
    assert any(f["kind"] == "cloud_degraded" for f in store.open_flags())


def test_terminal_status_is_not_retried_and_latches_the_cloud_leg_off(monkeypatch):
    """A 401/402/403/404 cannot succeed until an operator changes something, so it
    must cost ONE request (not max_retries) and must disable the cloud leg for every
    later call. The pre-fix behaviour re-paid the full retry ladder on every single
    classification — ~7s per call on a live run with a dead key."""
    import aizu.core.router as R

    class FakeResp:
        def __init__(self, status):
            self.status_code = status
        def raise_for_status(self):
            raise RuntimeError("Client error '401 Unauthorized'")
        def json(self):
            return {}

    calls = {"n": 0}
    slept = []

    def fake_post(url, headers, json, timeout):
        calls["n"] += 1
        return FakeResp(401)

    monkeypatch.setattr(R.httpx, "post", fake_post)
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    store = Store(path)
    r = R.OpenRouterRouter(store=store, api_key="x", max_retries=3,
                           sleep=lambda t: slept.append(t))

    d1 = r.classify_text(instruction="i", content="c", campaign_id="c", stage="relevance")
    assert d1.tier == "degraded"
    assert calls["n"] == 1          # ONE request, not max_retries
    assert slept == []              # and no backoff sleep at all
    assert r._degraded is True
    assert r._json_mode is True     # a dead key must not be blamed on JSON mode

    # Every later call short-circuits without touching the network.
    d2 = r.classify_text(instruction="i", content="c", campaign_id="c", stage="match")
    assert d2.tier == "degraded"
    assert calls["n"] == 1          # still one — no socket opened
    assert any(f["kind"] == "cloud_degraded" for f in store.open_flags())


def test_rate_limit_and_5xx_are_still_retried(monkeypatch):
    """The latch must not swallow the transient cases backoff exists for."""
    import aizu.core.router as R

    class FakeResp:
        def __init__(self, status, body):
            self.status_code = status
            self._b = body
        def raise_for_status(self):
            pass
        def json(self):
            return self._b

    good = {"choices": [{"message": {"content": '{"label":"relevant","score":0.7,"confidence":0.8}'}}]}
    calls = {"n": 0}

    def fake_post(url, headers, json, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResp(429, {})
        if calls["n"] == 2:
            return FakeResp(503, {})
        return FakeResp(200, good)

    monkeypatch.setattr(R.httpx, "post", fake_post)
    r = R.OpenRouterRouter(api_key="x", max_retries=5, sleep=lambda _t: None)
    d = r.classify_text(instruction="i", content="c", campaign_id="c", stage="relevance")
    assert d.tier == "cloud" and d.score == 0.7   # recovered
    assert calls["n"] == 3
    assert r._degraded is False                    # 429/5xx must NOT latch


class _VerdictResp:
    """Minimal httpx-response stand-in for the verdict-shape tests below."""

    def __init__(self, status, body):
        self.status_code = status
        self._b = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"Client error '{self.status_code}'")

    def json(self):
        return self._b


def _reply(content):
    return {"choices": [{"message": {"content": content}}]}


_GOOD_REPLY = _reply('{"label":"relevant","score":0.9,"confidence":0.9}')


def test_a_200_whose_content_carries_no_verdict_degrades_instead_of_rejecting(monkeypatch):
    """Live IG shakedown 2026-08-19: 3 of 12 reels came back HTTP 200 with prose
    instead of JSON. _extract_json returned {}, _decision_from_payload turned that
    into label=unknown score=0.00 confidence=0.00 with tier="cloud", it was logged
    as `Cloud relevance ✓`, cascade._unsure could not see it (not degraded,
    confidence under the escalate band, score far from the threshold) and the reel
    was rejected for good behind the TTL-free seen_reels watermark. It must become
    a degraded verdict — the shape the cascade escalates and the operator sees."""
    import aizu.core.router as R

    calls = {"n": 0}

    def fake_post(url, headers, json, timeout):
        calls["n"] += 1
        return _VerdictResp(200, _reply("I'm sorry, I can't judge this reel."))

    monkeypatch.setattr(R.httpx, "post", fake_post)
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    store = Store(path)
    r = R.OpenRouterRouter(store=store, api_key="x", max_retries=5,
                           sleep=lambda _t: None)
    d = r.classify_text(instruction="i", content="c", campaign_id="c", stage="relevance")

    assert d.tier == "degraded"                      # NOT a confident "cloud" reject
    assert "malformed" in d.reason and "sorry" in d.reason.lower()   # raw prefix carried
    assert any(f["kind"] == "cloud_degraded" for f in store.open_flags())
    assert r._json_mode is True                      # not a response_format problem
    assert r._degraded is False                      # and not a terminal provider error


def test_an_empty_json_object_is_treated_as_no_verdict(monkeypatch):
    """`{}` parses fine yet decides nothing — the same silent-reject shape as prose,
    because every field falls back to the unknown/0.00 defaults."""
    import aizu.core.router as R

    monkeypatch.setattr(R.httpx, "post",
                        lambda url, headers, json, timeout: _VerdictResp(200, _reply("{}")))
    r = R.OpenRouterRouter(api_key="x", max_retries=3, sleep=lambda _t: None)
    d = r.classify_text(instruction="i", content="c", campaign_id="c", stage="relevance")
    assert d.tier == "degraded"


def test_a_bare_unknown_label_is_treated_as_no_verdict(monkeypatch):
    """A reply of label="unknown" with no score is byte-identical to the parse
    failure's defaults, so it must escalate rather than reject the reel."""
    import aizu.core.router as R

    monkeypatch.setattr(R.httpx, "post", lambda url, headers, json, timeout:
                        _VerdictResp(200, _reply('{"label":"unknown"}')))
    r = R.OpenRouterRouter(api_key="x", max_retries=3, sleep=lambda _t: None)
    d = r.classify_text(instruction="i", content="c", campaign_id="c", stage="relevance")
    assert d.tier == "degraded"


def test_a_scored_reply_without_a_label_is_still_a_usable_verdict(monkeypatch):
    """The guard must not over-fire: a numeric score decides the reel on its own,
    so an unlabelled but scored reply stays a normal cloud verdict, one call."""
    import aizu.core.router as R

    calls = {"n": 0}

    def fake_post(url, headers, json, timeout):
        calls["n"] += 1
        return _VerdictResp(200, _reply('{"score":0.82,"confidence":0.7}'))

    monkeypatch.setattr(R.httpx, "post", fake_post)
    r = R.OpenRouterRouter(api_key="x", max_retries=3, sleep=lambda _t: None)
    d = r.classify_text(instruction="i", content="c", campaign_id="c", stage="relevance")
    assert d.tier == "cloud" and d.score == 0.82
    assert calls["n"] == 1


def test_an_explicit_unknown_with_a_zero_score_is_still_no_verdict(monkeypatch):
    """Regression on the guard itself. Reading label and score as an OR let the
    score branch rescue a STATED abstention: {"label":"unknown","score":0.0} came
    back True and rebuilt the exact Decision the shakedown produced —
    label=unknown score=0.00 confidence=0.00 tier="cloud", which _unsure cannot
    see. A stated label is authoritative; the score is consulted only when the
    reply omitted `label` entirely."""
    import aizu.core.router as R

    assert R._has_usable_verdict({"label": "unknown", "score": 0.0, "confidence": 0.0}) is False
    assert R._has_usable_verdict({"label": "", "score": 0.9}) is False
    assert R._has_usable_verdict({"score": 0.82, "confidence": 0.7}) is True

    def fake_post(url, headers, json, timeout):
        return _VerdictResp(200, _reply('{"label":"unknown","score":0.0,"confidence":0.0}'))

    monkeypatch.setattr(R.httpx, "post", fake_post)
    r = R.OpenRouterRouter(api_key="x", max_retries=3, sleep=lambda _t: None)
    d = r.classify_text(instruction="i", content="c", campaign_id="c", stage="relevance")
    assert d.tier == "degraded"


def test_a_verdictless_200_costs_exactly_one_extra_attempt(monkeypatch):
    """Capped deliberately: text p90 was ~42s on the live run and temperature is 0,
    so a full ladder would burn ~90s per reel re-asking for the same garbage. One
    retry (the truncated/half-streamed case) and then degrade."""
    import aizu.core.router as R

    calls = {"n": 0}

    def fake_post(url, headers, json, timeout):
        calls["n"] += 1
        return _VerdictResp(200, _reply("still not json"))

    monkeypatch.setattr(R.httpx, "post", fake_post)
    r = R.OpenRouterRouter(api_key="x", max_retries=5, sleep=lambda _t: None)
    d = r.classify_text(instruction="i", content="c", campaign_id="c", stage="relevance")
    assert d.tier == "degraded"
    assert calls["n"] == 2          # not 5, and not 1


def test_a_verdictless_200_is_recovered_by_its_one_retry(monkeypatch):
    """The retry exists to save the reel: a second attempt that does come back as
    JSON produces a normal cloud verdict, never a degrade."""
    import aizu.core.router as R

    calls = {"n": 0}

    def fake_post(url, headers, json, timeout):
        calls["n"] += 1
        return _VerdictResp(200, _reply("no json here") if calls["n"] == 1 else _GOOD_REPLY)

    monkeypatch.setattr(R.httpx, "post", fake_post)
    r = R.OpenRouterRouter(api_key="x", max_retries=5, sleep=lambda _t: None)
    d = r.classify_text(instruction="i", content="c", campaign_id="c", stage="relevance")
    assert d.tier == "cloud" and d.score == 0.9
    assert calls["n"] == 2


def test_a_verdictless_200_never_latches_json_mode_off(monkeypatch):
    """The word "malformed" in the message is load-bearing: without it
    _looks_like_param_rejection would read the raw content (which can contain "400"
    or "unsupported") as a response_format rejection and permanently disable JSON
    mode for a model that supports it."""
    import aizu.core.router as R

    seen = []

    def fake_post(url, headers, json, timeout):
        seen.append("response_format" in json)
        return _VerdictResp(200, _reply("Error 400: unsupported request, sorry."))

    monkeypatch.setattr(R.httpx, "post", fake_post)
    r = R.OpenRouterRouter(api_key="x", max_retries=3, sleep=lambda _t: None)
    d = r.classify_text(instruction="i", content="c", campaign_id="c", stage="match")
    assert d.tier == "degraded"
    assert seen == [True, True]     # both attempts kept JSON mode; no stripped retry
    assert r._json_mode is True


def test_a_terminal_status_still_wins_over_the_parse_retry(monkeypatch):
    """The verdict guard must compose with the terminal-error latch: a 401 arriving
    on the parse retry still halts immediately and disables the cloud leg."""
    import aizu.core.router as R

    calls = {"n": 0}

    def fake_post(url, headers, json, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return _VerdictResp(200, _reply("not json"))
        return _VerdictResp(401, {})

    monkeypatch.setattr(R.httpx, "post", fake_post)
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    store = Store(path)
    r = R.OpenRouterRouter(store=store, api_key="x", max_retries=5,
                           sleep=lambda _t: None)
    d = r.classify_text(instruction="i", content="c", campaign_id="c", stage="relevance")
    assert d.tier == "degraded"
    assert calls["n"] == 2          # stopped at the 401, no third attempt
    assert r._degraded is True      # cloud leg latched off for the rest of the run


def test_generate_json_still_accepts_a_reply_that_is_not_a_verdict(monkeypatch):
    """Scope guard: campaign generation returns arbitrary objects with no
    label/score, so the verdict requirement must apply to classify_text only —
    one call, parsed dict, no retry and no degrade."""
    import aizu.core.router as R

    calls = {"n": 0}

    def fake_post(url, headers, json, timeout):
        calls["n"] += 1
        return _VerdictResp(200, _reply('{"name":"Acme","threshold":0.7}'))

    monkeypatch.setattr(R.httpx, "post", fake_post)
    r = R.OpenRouterRouter(api_key="x", max_retries=3, sleep=lambda _t: None)
    assert r.generate_json(system="s", user="u") == {"name": "Acme", "threshold": 0.7}
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Wall-clock budget (fleet run 2026-08-20: job-2099fb29e88b dead-lettered 5/5,
# every attempt "stalled: no activity for over 180s"). One classification could
# issue up to _PARSE_RETRY_LIMIT x max_retries = 6 requests, each with a scalar
# timeout=60.0 that httpx applies per PHASE. Measured against the pre-change
# router with these very scenarios: 127s always-malformed-200, 187s hanging
# provider, 254s slow response_format rejection (two full ladders) — versus a
# 180s watchdog. Everything below is driven through an injected clock: no test
# sleeps, and every assertion is on SIMULATED elapsed time.
# ---------------------------------------------------------------------------

class _FakeClock:
    """Simulated monotonic clock. Nothing sleeps; the fake `sleep` and the fake
    transport below move it forward exactly as the real ones would."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _slow_server(clock, *, takes, body, calls, status=200):
    """An `httpx.post` stand-in for a server that needs `takes` seconds to answer.

    It HONOURS the timeout the router hands it: if the reply would land after the
    read timeout, the clock advances by that timeout and the request fails, which
    is what httpx does. `getattr(timeout, "read", timeout)` so the same stub also
    describes the pre-fix scalar `timeout=60.0`."""

    def fake_post(url, headers, json, timeout):
        calls.append(timeout)
        read = getattr(timeout, "read", timeout)
        if takes > read:
            clock.advance(read)
            raise RuntimeError("read timeout")
        clock.advance(takes)
        return _VerdictResp(status, body)

    return fake_post


def test_one_classification_can_never_outlast_its_budget(monkeypatch):
    """THE regression. A provider that always 200s with a malformed body — the
    free-tier failure mode that is hot, not theoretical — used to run `max_retries`
    requests with a 60s-per-phase timeout and no ceiling above them: 3x40 + the
    1+2+4 backoff = 127s measured against the pre-change router, ~70% of the
    watchdog spent inside ONE gate call that emits no heartbeat. With one budget
    shared down the stack the same provider costs 80s and 2 requests, and ends
    DEGRADED rather than raising."""
    import aizu.core.router as R

    clock = _FakeClock()
    calls = []
    monkeypatch.setattr(R.httpx, "post",
                        _slow_server(clock, takes=40.0, calls=calls,
                                     body={"error": {"message": "no choices"}}))
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    store = Store(path)
    r = R.OpenRouterRouter(store=store, api_key="x", max_retries=3,
                           sleep=clock.advance, clock=clock)

    d = r.classify_text(instruction="i", content="c", campaign_id="c", stage="relevance")

    assert d.tier == "degraded"                    # degraded, never raised
    assert clock.now <= R._CLASSIFY_BUDGET_SEC     # 80.0s — no request issued after that
    assert len(calls) == 2                         # not 6
    # The provable ceiling, and the margin the whole workflow exists for.
    ceiling = (R._CLASSIFY_BUDGET_SEC + R._CONNECT_TIMEOUT_SEC
               + R._WRITE_TIMEOUT_SEC + R._POOL_TIMEOUT_SEC)
    assert clock.now < ceiling < 180 / 1.5         # 80 < 100 < 120
    assert any(f["kind"] == "cloud_degraded" for f in store.open_flags())


def test_a_hanging_provider_is_cut_off_by_the_budget_not_by_six_timeouts(monkeypatch):
    """The shape actually measured live on 2026-08-20: 142s between "OpenRouter
    retry 1/3" and "retry 2/3", because a scalar `timeout=60.0` bounds each httpx
    PHASE, not the request. Three of those plus backoff is 187s against the
    pre-change router — a single classification outlasting the 180s watchdog by
    itself. The budget now ends the call at 80s, and the last attempt's read is
    clamped to the 19s that were left rather than starting a fresh 60s wait."""
    import aizu.core.router as R

    clock = _FakeClock()
    calls = []
    monkeypatch.setattr(R.httpx, "post",
                        _slow_server(clock, takes=1e9, body=_GOOD_REPLY, calls=calls))
    r = R.OpenRouterRouter(api_key="x", max_retries=3, sleep=clock.advance, clock=clock)

    d = r.classify_text(instruction="i", content="c", campaign_id="c", stage="relevance")

    assert d.tier == "degraded"
    assert clock.now == R._CLASSIFY_BUDGET_SEC     # 60 read + 1 backoff + 19 read
    assert [t.read for t in calls] == [60.0, 19.0]
    assert len(calls) == 2                         # not 6


def test_a_healthy_classification_still_costs_exactly_one_request(monkeypatch):
    """The bound must be invisible to a good call: one request, one verdict, and a
    slow-but-healthy reply (p90 was ~42s live) still fits inside a single read
    timeout rather than being cut short and degraded."""
    import aizu.core.router as R

    clock = _FakeClock()
    calls = []
    monkeypatch.setattr(R.httpx, "post",
                        _slow_server(clock, takes=42.0, body=_GOOD_REPLY, calls=calls))
    r = R.OpenRouterRouter(api_key="x", max_retries=3, sleep=clock.advance, clock=clock)

    d = r.classify_text(instruction="i", content="c", campaign_id="c", stage="relevance")

    assert d.tier == "cloud" and d.score == 0.9
    assert len(calls) == 1
    assert clock.now == 42.0                       # no retry, no backoff, no waste


def test_the_request_timeout_is_split_by_phase_and_clamped_to_what_is_left(monkeypatch):
    """The old call site passed a bare `timeout=60.0`, and httpx applies a scalar to
    connect/read/write/pool SEPARATELY — which is how one request took 142s on the
    failing run. Now: named per-phase constants, and the read phase (the only one
    that legitimately runs long) is clamped to the budget's remaining time, so the
    second attempt cannot start a 60s read with 50s of budget left."""
    import aizu.core.router as R

    clock = _FakeClock()
    calls = []
    monkeypatch.setattr(R.httpx, "post",
                        _slow_server(clock, takes=30.0, body=_reply("prose, not json"),
                                     calls=calls))
    r = R.OpenRouterRouter(api_key="x", max_retries=3, sleep=clock.advance, clock=clock)

    d = r.classify_text(instruction="i", content="c", campaign_id="c", stage="relevance")

    assert d.tier == "degraded"
    assert [t.connect for t in calls] == [R._CONNECT_TIMEOUT_SEC] * 2
    assert [t.write for t in calls] == [R._WRITE_TIMEOUT_SEC] * 2
    assert [t.pool for t in calls] == [R._POOL_TIMEOUT_SEC] * 2
    assert calls[0].read == R._READ_TIMEOUT_SEC    # full read while the budget is fresh
    assert calls[1].read == 50.0                   # 80 budget - 30 spent
    # A single request must never be able to eat the whole budget: there has to be
    # room left to notice and degrade.
    assert R._READ_TIMEOUT_SEC < R._CLASSIFY_BUDGET_SEC


def test_the_json_mode_stripped_retry_shares_the_same_budget(monkeypatch):
    """`classify_text` re-asks once without `response_format` when the provider
    rejects the param — correct, and kept. But that retry is part of the SAME
    logical classification, so it draws on the same clock: a provider that rejects
    slowly degrades at the budget instead of buying a second full ladder — 6
    requests and 254s measured against the pre-change router. The latch itself is
    unaffected on any call
    whose rejection comes back in time — see
    `test_json_mode_latches_off_so_rejection_is_paid_once`."""
    import aizu.core.router as R

    clock = _FakeClock()
    calls = []
    monkeypatch.setattr(R.httpx, "post",
                        _slow_server(clock, takes=40.0, body={}, calls=calls, status=400))
    r = R.OpenRouterRouter(api_key="x", max_retries=3, sleep=clock.advance, clock=clock)

    d = r.classify_text(instruction="i", content="c", campaign_id="c", stage="match")

    assert d.tier == "degraded"
    assert clock.now <= R._CLASSIFY_BUDGET_SEC
    assert len(calls) == 2                         # one ladder, not one per JSON mode
    assert R._BUDGET_MARKER in d.reason            # loud about WHY it degraded
    assert r._json_mode is True                    # out of clock ≠ a param rejection


def test_running_out_of_budget_is_never_read_as_a_param_rejection():
    """`_looks_like_param_rejection` must ignore a budget error the same way it
    ignores the terminal latch: retrying without JSON mode would share the same
    spent budget (a no-op request) and would permanently disable JSON mode for a
    model whose only sin was being slow."""
    import aizu.core.router as R

    budget = R._CallBudget(limit=R._CLASSIFY_BUDGET_SEC, clock=lambda: 0.0)
    slow_400 = budget.error("Client error '400 Bad Request'")
    assert R._BUDGET_MARKER in str(slow_400)
    assert R.OpenRouterRouter._looks_like_param_rejection(slow_400) is False
    # …while a plain rejection inside the budget still latches JSON mode off.
    assert R.OpenRouterRouter._looks_like_param_rejection(
        RuntimeError("Client error '400 Bad Request'")) is True


def test_the_budget_leaves_real_margin_under_the_session_watchdog():
    """The invariant this whole change serves, stated numerically: the worst case
    for one classification is budget + the non-read phases of a final in-flight
    request. `_post` never opens a socket once the budget is spent, and only the
    read phase is clamped to what is left, so the ceiling is 80 + 8 + 8 + 4 = 100s
    against `session_watchdog.STALL_TIMEOUT_SEC` = 180 — 80s of margin."""
    import aizu.core.router as R
    from aizu.session_watchdog import STALL_TIMEOUT_SEC

    ceiling = (R._CLASSIFY_BUDGET_SEC + R._CONNECT_TIMEOUT_SEC
               + R._WRITE_TIMEOUT_SEC + R._POOL_TIMEOUT_SEC)
    assert ceiling == 100.0
    assert ceiling < STALL_TIMEOUT_SEC / 1.5       # not a hair's-breadth margin
    # The pre-fix arithmetic, for the record: 2 parse-retries x 3 transport
    # retries x a 60s-per-phase scalar was multiples of the watchdog.
    assert (OpenRouterRouter._PARSE_RETRY_LIMIT * 3 * 60.0) > STALL_TIMEOUT_SEC
