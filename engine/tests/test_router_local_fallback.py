"""Local-first models + local→cloud fallback (Phase 5).

Covers: AIZU_* local model resolution, per-instance endpoint override, the
Ollama num_ctx applied ONLY on a local endpoint, the FallbackRouter degrade
handoff, and the build_router factory's local/cloud/fallback branch selection.
No network — payloads are inspected directly and routers are stubbed.
"""
import os

from aizu.core.router import (
    OPENROUTER_URL,
    FallbackRouter,
    OpenRouterRouter,
    build_router,
    Decision,
)


def _clear_llm_env(monkeypatch):
    for var in ("AIZU_LLM_BASE_URL", "AIZU_TEXT_MODEL", "AIZU_VISION_MODEL",
                "AIZU_NUM_CTX", "AIZU_LLM_FALLBACK_ENABLED",
                "AIZU_LLM_FALLBACK_TEXT_MODEL", "AIZU_LLM_FALLBACK_VISION_MODEL",
                "OPENROUTER_API_KEY", "OPENROUTER_TEXT_MODEL",
                "OPENROUTER_VISION_MODEL"):
        monkeypatch.delenv(var, raising=False)


# ---- model resolution ----

def test_aizu_model_envs_take_priority_over_openrouter(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_TEXT_MODEL", "openai/gpt-4o-mini")
    monkeypatch.setenv("AIZU_TEXT_MODEL", "qwen3-vl:30b-a3b-instruct")
    monkeypatch.setenv("AIZU_VISION_MODEL", "qwen3-vl:30b-a3b-instruct")
    r = OpenRouterRouter(api_key="x")
    assert r.text_model == "qwen3-vl:30b-a3b-instruct"     # AIZU_* wins
    assert r.vision_model == "qwen3-vl:30b-a3b-instruct"


# ---- per-instance endpoint ----

def test_base_url_override_is_per_instance(monkeypatch):
    _clear_llm_env(monkeypatch)
    local = OpenRouterRouter(api_key="x", base_url="http://localhost:11434/v1")
    cloud = OpenRouterRouter(api_key="x", base_url="https://openrouter.ai/api/v1")
    assert local._endpoint() == "http://localhost:11434/v1/chat/completions"
    assert local._is_local() is True
    assert cloud._endpoint() == OPENROUTER_URL
    assert cloud._is_local() is False


def test_headers_omit_authorization_without_key(monkeypatch):
    _clear_llm_env(monkeypatch)
    # A key-less local build must NOT send "Authorization: Bearer " — httpx raises
    # "Illegal header value" on the empty token (surfaced running local-first).
    local = OpenRouterRouter(api_key="", base_url="http://localhost:11434/v1")
    assert "Authorization" not in local._headers()
    withkey = OpenRouterRouter(api_key="sk-abc")
    assert withkey._headers()["Authorization"] == "Bearer sk-abc"


def test_endpoint_falls_back_to_env_when_no_override(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("AIZU_LLM_BASE_URL", "http://localhost:11434/v1")
    r = OpenRouterRouter(api_key="x")
    assert r._is_local() is True                            # env-resolved, per call


# ---- num_ctx: local only ----

def test_num_ctx_applied_on_local_payload(monkeypatch):
    _clear_llm_env(monkeypatch)
    r = OpenRouterRouter(api_key="x", base_url="http://localhost:11434/v1", num_ctx=16384)
    payload = r._build_text_payload(instruction="i", content="c",
                                    system=None, model="qwen3-vl")
    assert payload["num_ctx"] == 16384
    assert payload["options"]["num_ctx"] == 16384


def test_num_ctx_not_sent_to_cloud(monkeypatch):
    _clear_llm_env(monkeypatch)
    # Cloud endpoint (no base_url, no AIZU_LLM_BASE_URL) must NOT carry num_ctx —
    # OpenRouter 400s on unknown top-level fields.
    r = OpenRouterRouter(api_key="x", num_ctx=16384)
    payload = r._build_text_payload(instruction="i", content="c",
                                    system=None, model="openai/gpt-4o-mini")
    assert "num_ctx" not in payload
    assert "options" not in payload


def test_num_ctx_defaults_from_env(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("AIZU_NUM_CTX", "8192")
    r = OpenRouterRouter(api_key="x", base_url="http://localhost:11434/v1")
    assert r.num_ctx == 8192


# ---- FallbackRouter ----

class _StubRouter:
    """Records calls and returns a scripted Decision / dict."""

    def __init__(self, decision=None, gen=None, name="stub"):
        self._decision = decision or Decision(label="relevant", score=0.9,
                                              confidence=0.9, tier="cloud")
        self._gen = gen if gen is not None else {"ok": True}
        self.name = name
        self.calls = []
        self.text_model = f"{name}-text"

    def classify_text(self, **kw):
        self.calls.append(("classify_text", kw))
        return self._decision

    def classify_image(self, **kw):
        self.calls.append(("classify_image", kw))
        return self._decision

    def generate_json(self, **kw):
        self.calls.append(("generate_json", kw))
        return self._gen


def test_fallback_uses_secondary_when_primary_degrades():
    primary = _StubRouter(decision=Decision(label="unknown", score=0.0,
                                            confidence=0.0, tier="degraded"),
                          name="local")
    secondary = _StubRouter(decision=Decision(label="relevant", score=0.8,
                                             confidence=0.8, tier="cloud"),
                            name="cloud")
    fr = FallbackRouter(primary, secondary)
    d = fr.classify_text(instruction="i", content="c", campaign_id="c", stage="relevance")
    assert d.tier == "cloud" and d.score == 0.8            # cloud leg answered
    assert len(primary.calls) == 1 and len(secondary.calls) == 1


def test_fallback_keeps_primary_when_local_healthy():
    primary = _StubRouter(name="local")   # default healthy cloud-tier decision
    secondary = _StubRouter(name="cloud")
    fr = FallbackRouter(primary, secondary)
    d = fr.classify_image(instruction="i", images_b64=["z"], campaign_id="c",
                          stage="video_analysis")
    assert d.score == 0.9
    assert len(primary.calls) == 1 and secondary.calls == []   # cloud never touched


def test_fallback_generate_json_empty_triggers_secondary():
    primary = _StubRouter(gen={}, name="local")
    secondary = _StubRouter(gen={"from": "cloud"}, name="cloud")
    fr = FallbackRouter(primary, secondary)
    out = fr.generate_json(system="s", user="u")
    assert out == {"from": "cloud"}
    assert len(secondary.calls) == 1


def test_fallback_proxies_unknown_attrs_to_primary():
    primary = _StubRouter(name="local")
    fr = FallbackRouter(primary, _StubRouter(name="cloud"))
    assert fr.text_model == "local-text"                  # proxied to primary


# ---- build_router factory ----

def test_build_router_single_backend_by_default(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "cloud-key")
    r = build_router()
    assert isinstance(r, OpenRouterRouter)                # no local → single cloud


def test_build_router_local_only_no_cloud_key(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("AIZU_LLM_BASE_URL", "http://localhost:11434/v1")
    r = build_router()
    assert isinstance(r, OpenRouterRouter)                # local single, no fallback
    assert r._is_local() is True


def test_build_router_fallback_when_local_cloud_and_flag(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("AIZU_LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "cloud-key")
    monkeypatch.setenv("AIZU_LLM_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("AIZU_TEXT_MODEL", "qwen3-vl")
    monkeypatch.setenv("AIZU_LLM_FALLBACK_TEXT_MODEL", "openai/gpt-4o-mini")
    r = build_router()
    assert isinstance(r, FallbackRouter)
    assert r.primary._is_local() is True                  # local primary
    assert r.secondary._is_local() is False               # cloud secondary
    assert r.primary.text_model == "qwen3-vl"
    assert r.secondary.text_model == "openai/gpt-4o-mini"


def test_build_router_no_fallback_without_flag(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("AIZU_LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "cloud-key")
    # flag not set → single local router, no cloud leg
    r = build_router()
    assert isinstance(r, OpenRouterRouter)
