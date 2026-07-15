"""Model router — call sites and cloud tier (PRD §5, §6).

Three call sites the rest of the engine talks to:
  - classify_text(...)   relevance gate (caption) + comment match scoring
  - classify_image(...)  on-screen-text / OCR relevance (vision)
  - transcribe(...)      voiceover (v2; stub here)

Per the operator's choice this build is **OpenRouter cloud-only**: there is no
resident local model yet. The interface keeps a `tier` field and an
`escalate`-shaped return so a local tier can be slotted in front later with no
caller changes. Every cloud call:
  - retries with backoff,
  - degrades to a low-confidence local-style verdict + raises a soft flag on
    repeated failure (PRD §9: cloud degraded → degrade-to-local + flag),
  - logs spend per campaign/stage (PRD §6).
"""
from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional, Protocol

from .logsetup import get_logger
from .prompts import SYSTEM_GENERIC, USER_TEMPLATE, VISION_GENERIC

log = get_logger(__name__)

try:  # httpx is a runtime dep but keep import soft for offline/test use
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Rough fallback price table ($/1M tokens) used only when the API response
# carries no usage cost. Real cost is read from the response when present.
_PRICE = {
    "default":        (0.15, 0.60),   # (prompt, completion) per 1M tokens
}


@dataclass
class Decision:
    """Uniform verdict from any call site."""
    label: str                       # e.g. "relevant"/"irrelevant", "match"/"no"
    score: float                     # 0..1 relevance/match strength
    confidence: float                # 0..1 model self-confidence
    reason: str = ""
    extracted: dict[str, Any] = field(default_factory=dict)
    tier: str = "cloud"
    usd: float = 0.0
    raw: Optional[str] = None
    model: str = ""                  # which model produced this verdict
    # Model-comparison fan-out results (empty unless the superadmin switch is on and
    # this was a match-stage call) — one dict per comparison model:
    # {model, label, score, confidence, latency_ms, usd, error}.
    comparisons: list[dict[str, Any]] = field(default_factory=list)


class Router(Protocol):
    def classify_text(self, *, instruction: str, content: str, campaign_id: str,
                      stage: str, session_id: Optional[str] = None,
                      system: Optional[str] = None,
                      threshold: Optional[float] = None) -> Decision: ...
    def classify_image(self, *, instruction: str, images_b64: list[str],
                       campaign_id: str, stage: str,
                       session_id: Optional[str] = None,
                       system: Optional[str] = None) -> Decision: ...
    def transcribe(self, *, audio_path: str, campaign_id: str,
                   session_id: Optional[str] = None) -> Decision: ...


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model reply, tolerant of fences/prose."""
    if not text:
        return {}
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fence.group(1) if fence else None
    if candidate is None:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        candidate = brace.group(0) if brace else None
    if candidate is None:
        return {}
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return {}


def _content_or_none(body: dict[str, Any]) -> Optional[str]:
    """Pull choices[0].message.content from a chat response, tolerating the
    malformed 200-OK bodies flaky/free models emit (no choices, error payload,
    null content). Returns None so the caller can degrade-to-local + flag
    instead of crashing the session (PRD §9)."""
    try:
        choices = body.get("choices")
        if not choices:
            return None
        content = choices[0].get("message", {}).get("content")
        return content if isinstance(content, str) and content.strip() else None
    except (AttributeError, IndexError, TypeError):
        return None


def _decision_from_payload(p: dict[str, Any], tier: str, usd: float,
                           raw: str, model: str = "") -> Decision:
    def _f(key: str, default: float) -> float:
        try:
            return max(0.0, min(1.0, float(p.get(key, default))))
        except (TypeError, ValueError):
            return default
    return Decision(
        label=str(p.get("label", "unknown")),
        score=_f("score", 0.0),
        confidence=_f("confidence", 0.0),
        reason=str(p.get("reason", ""))[:500],
        extracted=p.get("extracted") if isinstance(p.get("extracted"), dict) else {},
        tier=tier,
        usd=usd,
        raw=raw,
        model=model,
    )


def _parse_csv_env(name: str) -> list[str]:
    """Comma-separated env list, tolerant of blanks/whitespace (same idiom as
    `worker/config.py`'s `REELRADAR_WORKER_PLATFORMS` parsing). Empty/unset → []."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    return [m.strip() for m in raw.split(",") if m.strip()]


def env_flag(name: str) -> bool:
    """A truthy env flag (1/true/yes/on, case-insensitive). Absent/empty → False.
    Same idiom as `worker/config.py`'s private `_env_flag` — exported here so
    callers building a router (cli.py) can resolve MODEL_COMPARISON_ENABLED
    without duplicating the check."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


class OpenRouterRouter:
    """Cloud-only router. One model resident assumption does not apply to cloud."""

    # Hardcoded last resort only — env (OPENROUTER_TEXT_MODEL / _VISION_MODEL) is the
    # real default so a dead model can be swapped without touching code.
    _DEFAULT_TEXT_MODEL = "openrouter/owl-alpha"
    _DEFAULT_VISION_MODEL = "nex-agi/nex-n2-pro:free"

    def __init__(self, *, store=None, api_key: Optional[str] = None,
                 text_model: Optional[str] = None,
                 vision_model: Optional[str] = None,
                 max_retries: int = 3, base_delay: float = 1.0,
                 spend_cap_usd: Optional[float] = None,
                 json_mode: bool = True,
                 sleep: Callable[[float], None] = time.sleep,
                 compare_models: Optional[list[str]] = None,
                 enable_comparison: bool = False):
        self.store = store
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        # Resolve the model: explicit arg (CLI flag) > env > hardcoded fallback. So a
        # caller that passes nothing (e.g. the panel's generate/interview handlers)
        # still honors OPENROUTER_TEXT_MODEL instead of pinning the dead default.
        self.text_model = text_model or os.environ.get("OPENROUTER_TEXT_MODEL") or self._DEFAULT_TEXT_MODEL
        self.vision_model = (vision_model or os.environ.get("OPENROUTER_VISION_MODEL")
                             or self._DEFAULT_VISION_MODEL)
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.spend_cap_usd = spend_cap_usd
        # Whether to ask for JSON output mode on text calls. Latches OFF the first
        # time the model rejects `response_format` (see classify_text) so a model
        # that can't do JSON mode pays the double-retry once, not on every call.
        self._json_mode = json_mode
        self._sleep = sleep
        self._degraded = False
        # Model-comparison fan-out (superadmin-switchable — see store.py
        # model_comparison_enabled). `enable_comparison` is resolved by the CALLER
        # (never read from the DB here); explicit `compare_models` > env
        # MODEL_COMPARISON_MODELS. Off (either flag false or no models) means
        # classify_text's match-stage path never enters the fan-out branch at all.
        self.compare_models = (compare_models if compare_models is not None
                               else _parse_csv_env("MODEL_COMPARISON_MODELS"))
        self.enable_comparison = enable_comparison

    # ---- internals ----
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-Title": "AIZU",
        }

    def _cost_from_usage(self, body: dict[str, Any], model: str) -> float:
        usage = body.get("usage") or {}
        if isinstance(usage, dict) and usage.get("cost") is not None:
            try:
                return float(usage["cost"])
            except (TypeError, ValueError):
                pass
        pin, pout = _PRICE.get(model, _PRICE["default"])
        pt = float(usage.get("prompt_tokens", 0) or 0)
        ct = float(usage.get("completion_tokens", 0) or 0)
        return pt / 1e6 * pin + ct / 1e6 * pout

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        if httpx is None:
            raise RuntimeError("httpx not available")
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                r = httpx.post(OPENROUTER_URL, headers=self._headers(),
                               json=payload, timeout=60.0)
                if r.status_code == 429 or r.status_code >= 500:
                    raise RuntimeError(f"retryable status {r.status_code}")
                r.raise_for_status()
                body = r.json()
                # A 200 with no usable choices is the flaky/free-model failure
                # mode — retry it like a 5xx instead of degrading on the first
                # bad roll. This is what keeps a relevant reel from being dropped
                # because one response came back malformed.
                if _content_or_none(body) is None:
                    raise RuntimeError("malformed 200: no usable choices")
                return body
            except Exception as e:  # noqa: BLE001
                last_err = e
                backoff = self.base_delay * (2 ** attempt)
                log.debug("OpenRouter retry %d/%d · backoff=%.1fs · %s",
                          attempt + 1, self.max_retries, backoff, e)
                self._sleep(backoff)
        raise RuntimeError(f"OpenRouter failed after {self.max_retries} retries: {last_err}")

    @staticmethod
    def _looks_like_param_rejection(err: Exception) -> bool:
        """True when an error reads like the provider rejecting the REQUEST (a 4xx
        client error / a complaint about response_format) — as opposed to a flaky
        malformed-200 or a transient network/5xx. Only the former means JSON mode
        is the culprit and worth retrying without (and latching off)."""
        msg = str(err).lower()
        if "malformed 200" in msg:        # the flaky/free-model failure — not our param
            return False
        return ("response_format" in msg or "400" in msg
                or "client error" in msg or "unsupported" in msg)

    def _spend_guard(self, campaign_id: str) -> bool:
        """Return True if the call is allowed under the cap."""
        if self.spend_cap_usd is None or self.store is None:
            return True
        if self.store.total_spend(campaign_id) >= self.spend_cap_usd:
            log.warning("Spend cap reached · cap=$%.2f campaign=%s",
                        self.spend_cap_usd, campaign_id)
            self.store.raise_flag("spend_cap", "soft",
                                  f"cap {self.spend_cap_usd} reached",
                                  campaign_id=campaign_id)
            return False
        return True

    def _record(self, campaign_id: str, stage: str, model: str, usd: float,
                session_id: Optional[str]) -> None:
        if self.store is not None and usd > 0:
            self.store.log_spend(campaign_id, stage, usd, model=model,
                                 session_id=session_id)

    def _degrade(self, campaign_id: str, stage: str, reason: str) -> Decision:
        log.warning("Cloud degraded → local stand-in · stage=%s · %s", stage, reason)
        if self.store is not None:
            self.store.raise_flag("cloud_degraded", "soft",
                                  f"{stage}: {reason}", campaign_id=campaign_id)
        # Degrade-to-local stand-in: abstain with low confidence so the cascade
        # treats it as "unsure" and the operator sees the flag.
        return Decision(label="unknown", score=0.0, confidence=0.0,
                        reason=f"degraded: {reason}", tier="degraded", usd=0.0)

    def _build_text_payload(self, *, instruction: str, content: str,
                            system: Optional[str], model: str) -> dict[str, Any]:
        sys_prompt = system if (system and system.strip()) else SYSTEM_GENERIC
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user",
                 "content": USER_TEMPLATE.format(brief=instruction, content=content)},
            ],
            "temperature": 0,
            "usage": {"include": True},
        }
        # Ask the provider to constrain output to a JSON object so the brief's
        # extract schema comes back as real JSON (the tolerant parser below is
        # still the safety net — JSON mode is best-effort and models drift).
        if self._json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    # ---- call sites ----
    def classify_text(self, *, instruction: str, content: str, campaign_id: str,
                      stage: str, session_id: Optional[str] = None,
                      system: Optional[str] = None,
                      threshold: Optional[float] = None) -> Decision:
        """Match-stage calls (`stage == "match"`) fan out to the model-comparison
        list when the superadmin switch is on (`self.enable_comparison`) and at
        least one comparison model is configured — every other call (relevance,
        vision, generate_json) is completely unaffected. `threshold` is used ONLY
        to compute each comparison model's agreement with the primary verdict for
        the Model Performance page; it never affects the primary decision itself."""
        if stage == "match" and self.enable_comparison and self.compare_models:
            return self._classify_text_with_comparison(
                instruction=instruction, content=content, campaign_id=campaign_id,
                stage=stage, session_id=session_id, system=system, threshold=threshold)
        return self._classify_text_primary(
            instruction=instruction, content=content, campaign_id=campaign_id,
            stage=stage, session_id=session_id, system=system)

    def _classify_text_primary(self, *, instruction: str, content: str,
                               campaign_id: str, stage: str,
                               session_id: Optional[str],
                               system: Optional[str]) -> Decision:
        """Today's exact classify_text body — unchanged whether or not the
        model-comparison fan-out is active, and run on the CALLING thread even when
        it is (comparisons run in background threads instead; see
        `_classify_text_with_comparison`)."""
        if not self._spend_guard(campaign_id):
            return self._degrade(campaign_id, stage, "spend cap")
        payload = self._build_text_payload(instruction=instruction, content=content,
                                           system=system, model=self.text_model)
        t0 = time.time()
        try:
            body = self._post(payload)
        except Exception as e:  # noqa: BLE001
            # A model that rejects `response_format` must not degrade every call.
            # If the failure looks like a request rejection (not a flaky 200/5xx),
            # latch JSON mode OFF and retry once without it — so the wasted retry
            # happens at most once per router, not on every subsequent call.
            if self._json_mode and self._looks_like_param_rejection(e):
                self._json_mode = False
                payload.pop("response_format", None)
                try:
                    body = self._post(payload)
                except Exception as e2:  # noqa: BLE001
                    return self._degrade(campaign_id, stage, str(e2))
            else:
                return self._degrade(campaign_id, stage, str(e))
        text = _content_or_none(body)
        if text is None:  # 200 but no usable choices (flaky/free models do this)
            return self._degrade(campaign_id, stage, f"malformed response: {str(body)[:160]}")
        usd = self._cost_from_usage(body, self.text_model)
        self._record(campaign_id, stage, self.text_model, usd, session_id)
        decision = _decision_from_payload(_extract_json(text), "cloud", usd, text,
                                          model=self.text_model)
        log.info("Cloud %s ✓ · model=%s label=%s score=%.2f usd=$%.4f",
                 stage, self.text_model, decision.label, decision.score, usd)
        log.debug("Cloud %s latency=%.0fms raw_len=%d", stage,
                  (time.time() - t0) * 1000, len(text))
        return decision

    def _call_compare_model(self, *, model: str, instruction: str, content: str,
                            system: Optional[str]) -> dict[str, Any]:
        """One comparison model's call — reuses the same retry/backoff `_post`, but
        is purely observational: never touches spend_cap, `self.store`, or a flag.
        Runs on a worker thread (see `_classify_text_with_comparison`), so it must
        not read/write anything from `self` that another thread mutates (it only
        reads immutable-per-call config: `self.max_retries` etc via `_post`)."""
        payload = self._build_text_payload(instruction=instruction, content=content,
                                           system=system, model=model)
        t0 = time.time()
        try:
            body = self._post(payload)
            text = _content_or_none(body)
            if text is None:
                raise RuntimeError(f"malformed response: {str(body)[:160]}")
            usd = self._cost_from_usage(body, model)
            p = _extract_json(text)
            return {
                "model": model,
                "label": p.get("label"),
                "score": p.get("score"),
                "confidence": p.get("confidence"),
                "latency_ms": (time.time() - t0) * 1000,
                "usd": usd,
                "error": None,
            }
        except Exception as e:  # noqa: BLE001 — a comparison model's failure is data, not a fault
            return {
                "model": model, "label": None, "score": None, "confidence": None,
                "latency_ms": (time.time() - t0) * 1000, "usd": 0.0, "error": str(e),
            }

    def _classify_text_with_comparison(self, *, instruction: str, content: str,
                                       campaign_id: str, stage: str,
                                       session_id: Optional[str],
                                       system: Optional[str],
                                       threshold: Optional[float]) -> Decision:
        """Fires every comparison model concurrently (background threads) while the
        primary call runs synchronously on THIS (the calling) thread — the only
        thread allowed to touch `self.store`'s sqlite3 connection. Logs one
        `model_comparison_log` row per model (including the primary) and returns
        the primary `Decision` with `.comparisons` attached; the primary verdict
        itself is byte-for-byte what `_classify_text_primary` alone would produce."""
        executor = ThreadPoolExecutor(max_workers=len(self.compare_models))
        try:
            futures = {
                executor.submit(self._call_compare_model, model=m, instruction=instruction,
                                content=content, system=system): m
                for m in self.compare_models
            }
            t0 = time.time()
            primary = self._classify_text_primary(
                instruction=instruction, content=content, campaign_id=campaign_id,
                stage=stage, session_id=session_id, system=system)
            primary_latency_ms = (time.time() - t0) * 1000

            comparisons: list[dict[str, Any]] = []
            for fut in as_completed(futures):
                model = futures[fut]
                try:
                    result = fut.result()
                except Exception as e:  # noqa: BLE001 — defensive; _call_compare_model never raises
                    result = {"model": model, "label": None, "score": None,
                             "confidence": None, "latency_ms": None, "usd": 0.0,
                             "error": str(e)}
                comparisons.append(result)
        finally:
            executor.shutdown(wait=True)

        if self.store is not None:
            for result in comparisons:
                agreed = None
                if threshold is not None and result.get("score") is not None:
                    agreed = (float(result["score"]) >= threshold) == (primary.score >= threshold)
                self.store.log_model_comparison(
                    campaign_id=campaign_id, stage=stage, model=result["model"],
                    is_primary=False, session_id=session_id,
                    label=result.get("label"), score=result.get("score"),
                    confidence=result.get("confidence"), agreed=agreed,
                    latency_ms=result.get("latency_ms"), usd=result.get("usd"),
                    error=result.get("error"))
            self.store.log_model_comparison(
                campaign_id=campaign_id, stage=stage, model=self.text_model,
                is_primary=True, session_id=session_id,
                label=primary.label, score=primary.score, confidence=primary.confidence,
                agreed=(True if threshold is not None else None),
                latency_ms=primary_latency_ms, usd=primary.usd,
                error=primary.reason if primary.tier == "degraded" else None)

        return replace(primary, comparisons=comparisons)

    def classify_image(self, *, instruction: str, images_b64: list[str],
                       campaign_id: str, stage: str,
                       session_id: Optional[str] = None,
                       system: Optional[str] = None) -> Decision:
        if not self._spend_guard(campaign_id):
            return self._degrade(campaign_id, stage, "spend cap")
        if isinstance(images_b64, str):       # tolerate a single-frame caller
            images_b64 = [images_b64]
        if not images_b64:
            return self._degrade(campaign_id, stage, "no frames to read")
        frame_count = len(images_b64)
        parts: list[dict[str, Any]] = [{
            "type": "text",
            "text": f"CAMPAIGN RELEVANCE DEFINITION:\n{instruction}\n\n"
                    f"Read the on-screen text across these {frame_count} frame(s) of "
                    "the same reel and judge relevance. Output ONLY the JSON object.",
        }]
        for b64 in images_b64:
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        sys_prompt = system if (system and system.strip()) else VISION_GENERIC
        payload = {
            "model": self.vision_model,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": parts},
            ],
            "temperature": 0,
            "usage": {"include": True},
        }
        t0 = time.time()
        try:
            body = self._post(payload)
        except Exception as e:  # noqa: BLE001
            return self._degrade(campaign_id, stage, str(e))
        text = _content_or_none(body)
        if text is None:  # 200 but no usable choices (flaky/free models do this)
            return self._degrade(campaign_id, stage, f"malformed response: {str(body)[:160]}")
        usd = self._cost_from_usage(body, self.vision_model)
        self._record(campaign_id, stage, self.vision_model, usd, session_id)
        decision = _decision_from_payload(_extract_json(text), "cloud", usd, text,
                                          model=self.vision_model)
        log.info("Cloud %s ✓ (vision) · model=%s label=%s score=%.2f usd=$%.4f",
                 stage, self.vision_model, decision.label, decision.score, usd)
        log.debug("Cloud %s latency=%.0fms frames=%d", stage,
                  (time.time() - t0) * 1000, frame_count)
        return decision

    def generate_json(self, *, system: str, user: str,
                      images_b64: Optional[list[str]] = None,
                      model: Optional[str] = None, campaign_id: str = "_generate",
                      stage: str = "campaign_gen",
                      session_id: Optional[str] = None) -> dict[str, Any]:
        """General-purpose "return a JSON object" call (campaign generation, etc.).

        Reuses every OpenRouter internal the classify_* paths use — the retry +
        malformed-200 latch in `_post`, the `response_format` JSON-mode latch-off,
        cost recording, and the tolerant `_extract_json` parser. Returns the parsed
        dict, or `{}` on degrade/garbage — it NEVER raises, so the caller owns the
        retry/default policy (the parse-untrusted-text rule)."""
        if not self._spend_guard(campaign_id):
            self._degrade(campaign_id, stage, "spend cap")
            return {}
        use_model = model or self.text_model
        if images_b64:
            content: Any = [{"type": "text", "text": user}]
            for b64 in images_b64:
                content.append({"type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        else:
            content = user
        payload: dict[str, Any] = {
            "model": use_model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": content}],
            "temperature": 0,
            "usage": {"include": True},
        }
        if self._json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            body = self._post(payload)
        except Exception as e:  # noqa: BLE001 — mirrors classify_text's JSON-mode latch
            if self._json_mode and self._looks_like_param_rejection(e):
                self._json_mode = False
                payload.pop("response_format", None)
                try:
                    body = self._post(payload)
                except Exception as e2:  # noqa: BLE001
                    self._degrade(campaign_id, stage, str(e2))
                    return {}
            else:
                self._degrade(campaign_id, stage, str(e))
                return {}
        text = _content_or_none(body)
        if text is None:
            self._degrade(campaign_id, stage, f"malformed response: {str(body)[:160]}")
            return {}
        usd = self._cost_from_usage(body, use_model)
        self._record(campaign_id, stage, use_model, usd, session_id)
        log.info("Cloud %s ✓ · model=%s usd=$%.4f", stage, use_model, usd)
        return _extract_json(text)

    def transcribe(self, *, audio_path: str, campaign_id: str,
                   session_id: Optional[str] = None) -> Decision:
        # v2 (PRD §6, §11). Not wired in v1.
        raise NotImplementedError("Audio transcription is a v2 tier.")
