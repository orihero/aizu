"""Model router — call sites and cloud tier (PRD §5, §6).

Three call sites the rest of the engine talks to:
  - classify_text(...)   relevance gate (caption) + comment match scoring
  - classify_image(...)  on-screen-text / OCR relevance (vision)
  - transcribe(...)      voiceover (v2; stub here)

Per the operator's choice this build is **OpenRouter cloud-only**: there is no
resident local model yet. The interface keeps a `tier` field and an
`escalate`-shaped return so a local tier can be slotted in front later with no
caller changes. Every cloud call:
  - retries with backoff, inside ONE wall-clock budget per logical classification
    (`_CLASSIFY_BUDGET_SEC`) so nested retries can never outlast the session
    watchdog,
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


def _resolve_chat_url(base: str) -> str:
    """Normalize a base URL to a full chat-completions endpoint. Empty → OpenRouter.
    Accepts either a ``/v1`` root (suffix appended) or a full endpoint."""
    base = (base or "").strip().rstrip("/")
    if not base:
        return OPENROUTER_URL
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


def chat_completions_url() -> str:
    """Chat-completions endpoint — OpenRouter unless pointed elsewhere.

    ``AIZU_LLM_BASE_URL`` aims the engine at any OpenAI-compatible server
    (Ollama, LM Studio, vLLM). Give it the ``/v1`` root; the
    ``/chat/completions`` suffix is appended if absent. Resolved per call, not
    at import, so a ``.env`` loaded after this module still takes effect.
    """
    return _resolve_chat_url(os.environ.get("AIZU_LLM_BASE_URL", ""))

# Rough fallback price table ($/1M tokens) used only when the API response
# carries no usage cost. Real cost is read from the response when present.
_PRICE = {
    "default":        (0.15, 0.60),   # (prompt, completion) per 1M tokens
}

# Provider statuses that will NOT fix themselves between one reel and the next.
# Retrying these is pure wall-clock: max_retries x exponential backoff on EVERY
# classification call, for a request that cannot succeed until a human edits a
# key, tops up an account, or corrects a model id. Measured on a live run with a
# dead key: ~7s burnt per call, ~40s per reel, forever.
#
# Deliberately EXCLUDED:
#   429 — rate limiting is exactly the transient case backoff exists for.
#   400 — usually the `response_format` rejection that classify_text recovers
#         from by latching JSON mode off (see _looks_like_param_rejection);
#         treating it as terminal would disable the cloud leg for a model that
#         works fine without JSON mode.
_FATAL_STATUS_HINT = {
    401: "the API key was rejected — check OPENROUTER_API_KEY",
    402: "out of credit — top up the OpenRouter account or lower the run's scope",
    403: "the account is not allowed to use this model",
    404: ("no such model — OpenRouter retires ids regularly (ledger D2); check "
          "OPENROUTER_TEXT_MODEL / OPENROUTER_VISION_MODEL against /api/v1/models"),
}
_FATAL_STATUS = frozenset(_FATAL_STATUS_HINT)

_DEGRADED_MARKER = "cloud leg disabled"

# ---- wall clock: one classification, one budget (fleet run 2026-08-20) -------
# job-2099fb29e88b dead-lettered after 5 attempts, every one halted by the bridge
# session watchdog ("stalled: no activity for over 180s"). ONE classification could
# legitimately run for minutes: `_post_verdict` loops `_PARSE_RETRY_LIMIT` (2) and
# each pass runs `_post`'s full `max_retries` (3) ladder, and the request timeout
# was a bare `timeout=60.0` — which httpx applies to EVERY phase separately
# (connect/read/write/pool), so a single request could burn far more than 60s. It
# did: 142s separated "OpenRouter retry 1/3" (10:33:24) from "retry 2/3" (10:35:46)
# on the failing run. Replayed against the pre-change code in tests/test_router.py:
# 127s for an always-malformed 200, 187s for a hanging provider (one classification
# outlasting the watchdog on its own), 254s and 6 requests when the parse-retry or
# the JSON-mode stripped retry buys a SECOND full ladder on top.
#
# Two bounds, both needed:
#   1. per-phase request caps (below) instead of one 60s scalar, and
#   2. `_CLASSIFY_BUDGET_SEC` — a single wall clock for ONE logical classification,
#      shared by the parse-retries, the transport retries AND the JSON-mode
#      stripped retry. Spent means stop: take the degrade path (tier="degraded",
#      which escalates the cascade and raises `cloud_degraded`) instead of buying
#      one more ladder we cannot afford.
#
# Sizing, against `session_watchdog.STALL_TIMEOUT_SEC` = 180:
#   read=60s    deliberately the SAME number the scalar had, so no request that
#               succeeds today can be cut short by this change — text was measured
#               p90 ~42s live and a non-streaming completion sends no bytes until
#               the whole answer exists, so the read phase must cover generation.
#               What changes is that connect/write/pool no longer get 60s EACH.
#   budget=80s  one full read timeout plus a clamped second attempt. Only the READ
#               phase is clamped to the budget's remaining time (see
#               `_request_timeout`), so the provable ceiling for one classification
#               is budget + connect + write + pool = 80 + 20 = 100s — 56% of the
#               watchdog, 80s of margin, against the 187-254s the same scenarios
#               cost before.
_CONNECT_TIMEOUT_SEC = 8.0
_READ_TIMEOUT_SEC = 60.0
_WRITE_TIMEOUT_SEC = 8.0
_POOL_TIMEOUT_SEC = 4.0
_CLASSIFY_BUDGET_SEC = 80.0
# Below this much budget left, opening another socket is pointless: the reply could
# not arrive before the deadline anyway, so spend the time degrading instead.
_MIN_ATTEMPT_SLICE_SEC = 5.0
# Marker on every "out of wall clock" error, read by `_looks_like_param_rejection`
# the same way `_DEGRADED_MARKER` is: running out of time is not a `response_format`
# problem, and a stripped retry would cost another ladder we by definition have no
# budget for.
_BUDGET_MARKER = "classification budget spent"


class _TerminalProviderError(RuntimeError):
    """A provider error that must never be retried and never be mistaken for a
    recoverable param rejection. Raised by _post, caught by the normal degrade
    paths in the classify_* callers."""


@dataclass
class _CallBudget:
    """The wall clock allowed to ONE logical classification, start to degrade.

    Passed down `_post_verdict` -> `_post` so the parse-retries and the transport
    retries draw on the SAME allowance instead of multiplying (2 x 3 requests was
    the direct cause of the 2026-08-20 worker_stall dead-letter).

    `clock` is injectable for the same reason the router's `sleep` is: a test must
    be able to drive a whole retry ladder — timeouts, backoffs and all — through
    simulated time, with nothing actually sleeping.
    """

    limit: float
    clock: Callable[[], float]
    started: float = 0.0

    def __post_init__(self) -> None:
        self.started = self.clock()

    def spent(self) -> float:
        return max(0.0, self.clock() - self.started)

    def remaining(self) -> float:
        return self.limit - self.spent()

    def exhausted(self) -> bool:
        return self.remaining() < _MIN_ATTEMPT_SLICE_SEC

    def error(self, detail: str = "") -> RuntimeError:
        """The exception that ends a classification that ran out of clock. Carries
        `_BUDGET_MARKER` so no downstream heuristic mistakes it for something
        retryable, and the last transport error so the `cloud_degraded` flag still
        tells the operator WHY the time went."""
        msg = f"{_BUDGET_MARKER}: {self.spent():.1f}s of {self.limit:.0f}s"
        return RuntimeError(f"{msg} · last error: {detail}" if detail else msg)


@dataclass
class Decision:
    """Uniform verdict from any call site."""
    label: str                       # e.g. "relevant"/"irrelevant", "match"/"no"
    score: float                     # 0..1 relevance/match strength
    confidence: float                # 0..1 model self-confidence
    reason: str = ""
    # v27 lead redaction: the model's own one-line "what this commenter wants".
    # Deliberately NOT inside `extracted` — that is the grounded field bag the
    # panel renders as data, while this is the prose a customer reads INSTEAD of
    # the username and comment. `matching.derive_intent` is what sanitizes it.
    intent: str = ""
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


def _raw_prefix(text: str, limit: int = 120) -> str:
    """First few characters of a model reply, whitespace-collapsed, for a log line
    or a health-flag detail — enough to tell an empty body from prose from a
    truncated JSON object without dumping a whole caption into the flag."""
    flat = " ".join((text or "").split())
    return (flat[:limit] + "\u2026") if len(flat) > limit else flat


def _has_usable_verdict(p: dict[str, Any]) -> bool:
    """True when a parsed reply actually decides something.

    `_decision_from_payload` defaults a missing label to "unknown" and a missing
    score to 0.00 — downstream that is indistinguishable from a CONFIDENT "no",
    so the absence of both has to be caught before it ever becomes a Decision
    (live IG shakedown 2026-08-19: 3 of 12 reels came back this way and were
    silently rejected). A bare label="unknown" is no verdict for the same reason;
    a numeric score alone is one, even unlabelled.

    A STATED label is authoritative: an explicit ``{"label":"unknown","score":0.0}``
    decides nothing no matter what score rides along with it. Reading the two keys
    as an OR instead let exactly that payload through — which is byte-for-byte the
    shape the shakedown logged — so the score branch is reachable only when the
    reply omitted ``label`` entirely.
    """
    if not isinstance(p, dict) or not p:
        return False
    label = p.get("label")
    if isinstance(label, str):
        stripped = label.strip().lower()
        return bool(stripped) and stripped != "unknown"
    score = p.get("score")
    if isinstance(score, bool):      # True/False is a coercible non-answer
        return False
    try:
        float(score)                 # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return True


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
        # v27: carried verbatim off the wire; `matching.derive_intent` is the
        # only thing allowed to decide whether it is usable and safe to show.
        intent=str(p.get("intent") or "")[:500],
        extracted=p.get("extracted") if isinstance(p.get("extracted"), dict) else {},
        tier=tier,
        usd=usd,
        raw=raw,
        model=model,
    )


def _parse_csv_env(name: str) -> list[str]:
    """Comma-separated env list, tolerant of blanks/whitespace (same idiom as
    `worker/config.py`'s `AIZU_WORKER_PLATFORMS` parsing). Empty/unset → []."""
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


def env_int(name: str, default: int = 0) -> int:
    """A non-negative int env var; malformed/absent → default."""
    try:
        v = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return v if v >= 0 else default


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
                 clock: Callable[[], float] = time.monotonic,
                 compare_models: Optional[list[str]] = None,
                 enable_comparison: bool = False,
                 base_url: Optional[str] = None,
                 num_ctx: Optional[int] = None):
        self.store = store
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        # Per-instance endpoint override. None → resolve AIZU_LLM_BASE_URL per call
        # (back-compat). A FallbackRouter passes each leg its own explicit endpoint
        # so a local primary and a cloud secondary can coexist in one process.
        self._base_url = base_url
        # Local context-window (Ollama num_ctx). Multi-frame vision truncates on
        # local models unless this is raised — soliq's docs/LOCAL_AI.md, ≥16384.
        # Applied to payloads ONLY when the endpoint is local (never sent to
        # OpenRouter, which would 400 on an unknown field). 0 → not sent.
        self.num_ctx = num_ctx if num_ctx is not None else env_int("AIZU_NUM_CTX", 0)
        # Resolve the model: explicit arg (CLI flag) > AIZU_* (local-first knob) >
        # OPENROUTER_* > hardcoded fallback. AIZU_TEXT_MODEL/AIZU_VISION_MODEL let an
        # operator point the run at a local Ollama VL model (e.g. qwen3-vl for both)
        # without disturbing the cloud OPENROUTER_* defaults.
        self.text_model = (text_model or os.environ.get("AIZU_TEXT_MODEL")
                           or os.environ.get("OPENROUTER_TEXT_MODEL") or self._DEFAULT_TEXT_MODEL)
        self.vision_model = (vision_model or os.environ.get("AIZU_VISION_MODEL")
                             or os.environ.get("OPENROUTER_VISION_MODEL")
                             or self._DEFAULT_VISION_MODEL)
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.spend_cap_usd = spend_cap_usd
        # Whether to ask for JSON output mode on text calls. Latches OFF the first
        # time the model rejects `response_format` (see classify_text) so a model
        # that can't do JSON mode pays the double-retry once, not on every call.
        self._json_mode = json_mode
        self._sleep = sleep
        # Monotonic by design (wall-clock jumps must not hand a call more or less
        # budget), and injectable so tests drive `_CallBudget` deterministically.
        self._clock = clock
        # Latched ON by the first TERMINAL provider error (see _FATAL_STATUS). Once
        # set, _post fails immediately with no socket and no backoff, so the rest of
        # the run degrades to the local stand-in at once instead of re-paying
        # max_retries x exponential backoff on every single classification call.
        self._degraded = False
        self._degraded_reason = ""
        # Model-comparison fan-out (superadmin-switchable — see store.py
        # model_comparison_enabled). `enable_comparison` is resolved by the CALLER
        # (never read from the DB here); explicit `compare_models` > env
        # MODEL_COMPARISON_MODELS. Off (either flag false or no models) means
        # classify_text's match-stage path never enters the fan-out branch at all.
        self.compare_models = (compare_models if compare_models is not None
                               else _parse_csv_env("MODEL_COMPARISON_MODELS"))
        self.enable_comparison = enable_comparison

    # ---- internals ----
    def _endpoint(self) -> str:
        """This router's chat-completions URL — the per-instance override if set,
        else the env-resolved default (per-call, so a late .env still applies)."""
        if self._base_url is not None:
            return _resolve_chat_url(self._base_url)
        return chat_completions_url()

    def _is_local(self) -> bool:
        return self._endpoint() != OPENROUTER_URL

    def _apply_local_opts(self, payload: dict[str, Any]) -> dict[str, Any]:
        """When talking to a local (non-OpenRouter) endpoint and num_ctx is set,
        raise the Ollama context window so multi-frame vision isn't truncated.
        Sets both `num_ctx` and `options.num_ctx` (soliq's belt-and-braces) — Ollama
        reads one or the other by version; unknown fields are ignored locally. Never
        applied to OpenRouter, which rejects unknown top-level fields."""
        if self.num_ctx and self.num_ctx > 0 and self._is_local():
            payload["num_ctx"] = self.num_ctx
            opts = payload.get("options")
            payload["options"] = {**opts, "num_ctx": self.num_ctx} if isinstance(opts, dict) \
                else {"num_ctx": self.num_ctx}
        return payload

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-Title": "AIZU",
        }
        # Only send Authorization when a key is present. A local Ollama/vLLM
        # endpoint needs no auth, and an empty "Bearer " value makes httpx raise
        # "Illegal header value" before the request is even sent (surfaced running
        # a key-less local-first build).
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _cost_from_usage(self, body: dict[str, Any], model: str) -> float:
        # Self-hosted inference has no per-token billing. Without this the
        # fallback price table would accrue phantom spend and trip the spend cap
        # mid-run on a local model.
        if self._is_local():
            return 0.0
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

    def _new_budget(self, limit: float = _CLASSIFY_BUDGET_SEC) -> _CallBudget:
        """A fresh allowance for one logical classification (see _CallBudget)."""
        return _CallBudget(limit=limit, clock=self._clock)

    def _request_timeout(self, budget: Optional[_CallBudget]) -> Any:
        """Timeout for ONE request — split per phase, not the old `timeout=60.0`.

        httpx applies a scalar timeout to each phase separately, so 60.0 never meant
        "this request takes at most a minute": the 142s gap between two retry lines
        on the 2026-08-20 run is what that actually bought. Connect/write/pool get
        small fixed caps because none of them has any business running long for a
        few-KB POST; only READ can legitimately take ~a minute (a non-streaming
        completion sends nothing until the answer is generated), so READ is the one
        phase clamped to the budget's remaining time. That asymmetry is what makes
        the ceiling for a whole classification `budget + connect + write + pool`
        (100s) rather than `budget x 4`.
        """
        read = _READ_TIMEOUT_SEC
        if budget is not None:
            read = max(_MIN_ATTEMPT_SLICE_SEC, min(read, budget.remaining()))
        return httpx.Timeout(read, connect=_CONNECT_TIMEOUT_SEC,
                             write=_WRITE_TIMEOUT_SEC, pool=_POOL_TIMEOUT_SEC)

    def _post(self, payload: dict[str, Any], *,
              budget: Optional[_CallBudget] = None) -> dict[str, Any]:
        """One request with the retry ladder. `budget`, when given, is the wall
        clock this call shares with everything else in the same logical
        classification; `None` (campaign generation) keeps the pre-budget behaviour,
        since that path is operator-initiated, is not under the session watchdog,
        and legitimately produces much longer replies than a verdict."""
        if httpx is None:
            raise RuntimeError("httpx not available")
        # Already latched off by a terminal provider error: fail instantly. Callers
        # catch this and degrade, so the run keeps making progress on the local
        # stand-in rather than sleeping through a doomed retry ladder per call.
        if self._degraded:
            raise _TerminalProviderError(
                f"{_DEGRADED_MARKER}: {self._degraded_reason}")
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            # Checked BEFORE the socket, so the budget is a ceiling on requests
            # issued, not a thing noticed after the fact.
            if budget is not None and budget.exhausted():
                raise budget.error(str(last_err) if last_err else "")
            try:
                r = httpx.post(self._endpoint(), headers=self._headers(),
                               json=payload, timeout=self._request_timeout(budget))
                if r.status_code == 429 or r.status_code >= 500:
                    raise RuntimeError(f"retryable status {r.status_code}")
                if r.status_code in _FATAL_STATUS:
                    self._latch_degraded(r.status_code)
                    raise _TerminalProviderError(
                        f"{_DEGRADED_MARKER}: {self._degraded_reason}")
                r.raise_for_status()
                body = r.json()
                # A 200 with no usable choices is the flaky/free-model failure
                # mode — retry it like a 5xx instead of degrading on the first
                # bad roll. This is what keeps a relevant reel from being dropped
                # because one response came back malformed.
                if _content_or_none(body) is None:
                    raise RuntimeError("malformed 200: no usable choices")
                return body
            except _TerminalProviderError:
                # Never retried and never swallowed: the provider has told us this
                # request can't succeed until an operator changes something.
                raise
            except Exception as e:  # noqa: BLE001
                last_err = e
                backoff = self.base_delay * (2 ** attempt)
                if budget is not None:
                    # Never sleep past the deadline: backoff that outlives the
                    # budget is pure watchdog risk for a request we will not make.
                    backoff = max(0.0, min(backoff, budget.remaining()))
                log.debug("OpenRouter retry %d/%d · backoff=%.1fs · %s",
                          attempt + 1, self.max_retries, backoff, e)
                self._sleep(backoff)
        raise RuntimeError(f"OpenRouter failed after {self.max_retries} retries: {last_err}")

    # A 200 whose content is not a verdict is re-asked at most ONCE, never on the
    # full ladder: text calls measured p90 ~42s on the live run and temperature is
    # 0, so re-asking the identical prompt three times mostly buys ~90s of wall
    # clock per reel for the identical garbage. The one extra attempt still catches
    # the only version of this a retry CAN fix (a truncated / half-streamed reply).
    _PARSE_RETRY_LIMIT = 2          # the first attempt plus exactly one more

    def _post_verdict(self, payload: dict[str, Any], *,
                      budget: Optional[_CallBudget] = None) -> dict[str, Any]:
        """`_post` for the classify_* call sites, whose reply is worthless unless it
        parses into a label/score.

        Live IG shakedown 2026-08-19: 3 of 12 reels came back HTTP 200 with choices
        present but content that was prose, not JSON. `_extract_json` returns {} for
        that and `_decision_from_payload` then manufactures label=unknown score=0.00
        confidence=0.00 with tier="cloud" — a CONFIDENT reject that
        `cascade._unsure` cannot detect (not degraded, confidence below the escalate
        band, score nowhere near the threshold), logged as `Cloud relevance ✓`, with
        no re-ask and no health flag, made permanent by the TTL-free seen_reels
        watermark. Raising instead puts the call on the degrade path, where
        tier=="degraded" makes the cascade escalate and `cloud_degraded` fires.

        The raised message MUST keep the words "malformed 200":
        `_looks_like_param_rejection` keys off them, so a reply whose raw text
        happens to contain "400"/"unsupported" is never mistaken for a
        `response_format` rejection that latches JSON mode off.

        The re-ask draws on the caller's `budget` rather than a fresh one. Adding
        this wrapper is what turned `max_retries` requests per classification into
        `_PARSE_RETRY_LIMIT x max_retries` (2 x 3), which is how one gate call could
        outlast the 180s session watchdog; the shared budget keeps the fix (a
        verdictless 200 is never read as a confident reject) without the wall clock.

        A wrapper rather than a `_post` flag on purpose: `generate_json` returns
        arbitrary objects (campaign JSON has no label/score) and must keep `_post`'s
        behaviour exactly. Every network/5xx/terminal-status rule still lives in
        `_post` and is unchanged — this only re-asks a *successful* request whose
        answer decided nothing.
        """
        raw = ""
        for attempt in range(self._PARSE_RETRY_LIMIT):
            # The re-ask shares the caller's wall clock: two parse attempts x three
            # transport attempts is exactly the multiplication that stalled the
            # session, so once the clock is spent we stop and let the caller degrade.
            if budget is not None and budget.exhausted():
                log.warning("Cloud verdict attempt skipped · %.1fs of %.0fs budget spent",
                            budget.spent(), budget.limit)
                raise budget.error("no usable verdict · raw=" + (raw or "<empty>"))
            body = self._post(payload, budget=budget)
            content = _content_or_none(body) or ""
            if _has_usable_verdict(_extract_json(content)):
                return body
            raw = _raw_prefix(content)
            log.warning("Cloud 200 carried no usable verdict · attempt=%d/%d · raw=%s",
                        attempt + 1, self._PARSE_RETRY_LIMIT, raw or "<empty>")
        raise RuntimeError("malformed 200: unparseable content · raw=" + (raw or "<empty>"))

    def _latch_degraded(self, status: int) -> None:
        """Turn the cloud leg off for the rest of this router's life and say why in
        operator language. Logged at ERROR exactly once — the per-call degrade
        warning that follows is already noisy enough."""
        self._degraded = True
        self._degraded_reason = f"HTTP {status} — {_FATAL_STATUS_HINT[status]}"
        log.error("Cloud LLM disabled for this run · %s · every remaining call "
                  "uses the local stand-in until this is fixed", self._degraded_reason)

    @staticmethod
    def _looks_like_param_rejection(err: Exception) -> bool:
        """True when an error reads like the provider rejecting the REQUEST (a 4xx
        client error / a complaint about response_format) — as opposed to a flaky
        malformed-200 or a transient network/5xx. Only the former means JSON mode
        is the culprit and worth retrying without (and latching off)."""
        msg = str(err).lower()
        if "malformed 200" in msg:        # the flaky/free-model failure — not our param
            return False
        if _BUDGET_MARKER in msg:
            # Out of wall clock says nothing about response_format, and the stripped
            # retry would share the same (spent) budget anyway — so it would cost a
            # no-op request and could latch JSON mode off for a model that is merely
            # slow. The latch still happens on any later call whose rejection comes
            # back inside its budget.
            return False
        if isinstance(err, _TerminalProviderError) or _DEGRADED_MARKER in msg:
            # A dead key/credit/model is not a response_format problem. Without
            # this, every latched call would still burn a second _post to "retry
            # without JSON mode" — cheap now that it no-ops, but it would also
            # permanently disable JSON mode for a router whose only sin was a
            # typo'd model id.
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
        return self._apply_local_opts(payload)

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
        # ONE budget for the whole classification — parse-retries, transport
        # retries and the stripped retry below all draw on it.
        budget = self._new_budget()
        t0 = time.time()
        try:
            body = self._post_verdict(payload, budget=budget)
        except Exception as e:  # noqa: BLE001
            # A model that rejects `response_format` must not degrade every call.
            # If the failure looks like a request rejection (not a flaky 200/5xx),
            # latch JSON mode OFF and retry once without it — so the wasted retry
            # happens at most once per router, not on every subsequent call.
            if self._json_mode and self._looks_like_param_rejection(e):
                self._json_mode = False
                payload.pop("response_format", None)
                try:
                    # Same budget on purpose: the stripped retry is part of the same
                    # logical classification, so it must not double the wall clock.
                    # If the rejection ladder already ate the budget this raises
                    # without a socket and we degrade — the latch is still set, so
                    # the next call pays nothing for JSON mode.
                    body = self._post_verdict(payload, budget=budget)
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
            # Its own budget, not the primary's: these run on worker threads (a
            # shared _CallBudget would be cross-thread state) but the reel still
            # WAITS on them in `_classify_text_with_comparison`, so an unbounded
            # comparison model would stall the session just as the primary did.
            body = self._post(payload, budget=self._new_budget())
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
        payload = self._apply_local_opts({
            "model": self.vision_model,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": parts},
            ],
            "temperature": 0,
            "usage": {"include": True},
        })
        t0 = time.time()
        try:
            # Vision is on the same per-reel path as the text gate, under the same
            # watchdog, so it gets the same bounded wall clock.
            body = self._post(payload, budget=self._new_budget())
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
        self._apply_local_opts(payload)
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


class FallbackRouter:
    """Local-primary → cloud-secondary router (soliq's ``FallbackProvider``).

    Every call runs on ``primary`` (a local Ollama-backed ``OpenRouterRouter``);
    if the local leg *degrades* (endpoint down, malformed reply, spend-guard) the
    SAME call is retried on ``secondary`` (the OpenRouter cloud leg). Degrade is
    the signal because ``OpenRouterRouter``'s call sites never raise — they return
    a ``tier == "degraded"`` ``Decision`` (or ``{}`` from ``generate_json``).

    Only the cloud leg bills, so spend logging stays honest: the local leg records
    $0 and the fallback logs the real cloud cost via the secondary's own machinery.
    Unknown attributes (``text_model``, ``compare_models``, …) proxy to the primary
    so existing callers that read them keep working.
    """

    def __init__(self, primary: "OpenRouterRouter", secondary: "OpenRouterRouter"):
        self.primary = primary
        self.secondary = secondary

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes not set on the FallbackRouter itself.
        return getattr(self.primary, name)

    def classify_text(self, **kwargs) -> Decision:
        d = self.primary.classify_text(**kwargs)
        if d.tier == "degraded":
            log.warning("local classify_text degraded → cloud fallback · stage=%s",
                        kwargs.get("stage"))
            return self.secondary.classify_text(**kwargs)
        return d

    def classify_image(self, **kwargs) -> Decision:
        d = self.primary.classify_image(**kwargs)
        if d.tier == "degraded":
            log.warning("local classify_image degraded → cloud fallback · stage=%s",
                        kwargs.get("stage"))
            return self.secondary.classify_image(**kwargs)
        return d

    def generate_json(self, **kwargs) -> dict[str, Any]:
        out = self.primary.generate_json(**kwargs)
        if not out:   # {} means the local leg degraded / returned garbage
            log.warning("local generate_json empty → cloud fallback · stage=%s",
                        kwargs.get("stage"))
            return self.secondary.generate_json(**kwargs)
        return out

    def transcribe(self, **kwargs) -> Decision:
        return self.primary.transcribe(**kwargs)


def build_router(*, store=None, spend_cap_usd: Optional[float] = None,
                 text_model: Optional[str] = None,
                 vision_model: Optional[str] = None,
                 enable_comparison: bool = False,
                 compare_models: Optional[list[str]] = None):
    """Construct the run-path router, local-first with an optional cloud fallback.

    - Local endpoint set (``AIZU_LLM_BASE_URL``) **and** an OpenRouter key present
      **and** ``AIZU_LLM_FALLBACK_ENABLED`` truthy → ``FallbackRouter`` (local
      primary → cloud secondary), mirroring soliq's ``FallbackProvider``.
    - Otherwise a single ``OpenRouterRouter`` pointed at whichever backend is
      configured (local via ``AIZU_LLM_BASE_URL``, else OpenRouter cloud).

    Model resolution keeps the two legs independent: the local leg honours
    ``AIZU_TEXT_MODEL``/``AIZU_VISION_MODEL`` (e.g. ``qwen3-vl`` for both), the
    cloud leg honours ``AIZU_LLM_FALLBACK_TEXT_MODEL``/``_VISION_MODEL`` (else the
    ``OPENROUTER_*`` defaults). ``compare_models``/``enable_comparison`` apply to
    the primary only.
    """
    local_base = os.environ.get("AIZU_LLM_BASE_URL", "").strip()
    has_cloud = bool(os.environ.get("OPENROUTER_API_KEY", "").strip())
    want_fallback = env_flag("AIZU_LLM_FALLBACK_ENABLED")

    primary = OpenRouterRouter(
        store=store, spend_cap_usd=spend_cap_usd,
        text_model=text_model, vision_model=vision_model,
        enable_comparison=enable_comparison, compare_models=compare_models)

    if local_base and has_cloud and want_fallback:
        # Cloud secondary: explicit OpenRouter endpoint + cloud model ids, so it is
        # unaffected by the AIZU_* local model envs the primary picks up.
        cloud = OpenRouterRouter(
            store=store, spend_cap_usd=spend_cap_usd,
            base_url="https://openrouter.ai/api/v1",
            text_model=(os.environ.get("AIZU_LLM_FALLBACK_TEXT_MODEL")
                        or os.environ.get("OPENROUTER_TEXT_MODEL")),
            vision_model=(os.environ.get("AIZU_LLM_FALLBACK_VISION_MODEL")
                          or os.environ.get("OPENROUTER_VISION_MODEL")),
            enable_comparison=False, compare_models=[])
        log.info("Router: local-first with cloud fallback · local=%s", local_base)
        return FallbackRouter(primary, cloud)

    log.info("Router: single backend · endpoint=%s", primary._endpoint())
    return primary
