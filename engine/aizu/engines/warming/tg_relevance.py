"""Thin LLM relevance gate for Telegram warming search hits (PRD §7.4).

This is the SINGLE machine-learning touch on any warming path (the deliberate X2
departure from P0's zero-ML rule). It vets ONLY untrusted keyword-search hits —
operator-seeded channels always skip it. It is intentionally a thin direct helper
(NOT the full harvest ``OpenRouterRouter``) so the warming isolation guard's
``router.score`` / ``log_spend`` prohibition stays intact: this helper never
scores, never captures a match, never logs spend.

Input is METADATA ONLY (title + username + participants + campaign goal) — no
message-content fetch before joining (cheaper; avoids reading private content
pre-join). The reply is parsed behind a tolerant never-throw boundary and is
FAIL-CLOSED: any parse failure / missing key ⇒ ``relevant=False`` (do not join).

``build_relevance_gate`` returns ``None`` when no ``OPENROUTER_API_KEY`` is
available, which tells the routine to DEGRADE to seeded-channels-only — warming
still runs, it just skips keyword search + its gate.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Optional

from ...core.config import Campaign
from ...core.logsetup import get_logger
from ...core.router import chat_completions_url, _content_or_none, _extract_json
from ..telegram.warming_writes import TgChannel

log = get_logger(__name__)

def _default_gate_model() -> str:
    """The text model for the metadata-only yes/no gate (pennies per session).

    Resolved through the SAME chain the router uses
    (`AIZU_TEXT_MODEL` -> `OPENROUTER_TEXT_MODEL` -> the router's own default)
    rather than carried as a fourth hardcoded copy of a model id. This module
    held `"openrouter/owl-alpha"`, which was absent from OpenRouter's live model
    listing when last checked — see memory/known-issues.md C0. A model id is a
    third-party fact with a shelf life, and duplicating one across four files is
    how three of those copies go stale unnoticed.

    Read at CALL time, not at import time, so a `.env` loaded after this module
    is imported is still honoured.
    """
    from ...core.router import OpenRouterRouter
    return (os.environ.get("AIZU_TEXT_MODEL")
            or os.environ.get("OPENROUTER_TEXT_MODEL")
            or OpenRouterRouter._DEFAULT_TEXT_MODEL)

_SYSTEM = (
    "You judge whether a Telegram channel is relevant to a marketing campaign's "
    "goal. Reply with a single JSON object: {\"relevant\": true|false}. "
    "Be conservative — only true when the channel clearly fits the goal."
)

_USER_TEMPLATE = (
    "Campaign goal: {goal}\n"
    "Channel: {username} — {title} ({participants} members)\n"
    "Is this channel relevant to the goal? Reply JSON {{\"relevant\": bool}}."
)


def _coerce_relevant(payload: dict[str, Any]) -> bool:
    """Fail-closed coercion of the gate reply's ``relevant`` field."""
    val = payload.get("relevant")
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "yes", "1")
    return False


def build_relevance_gate(
    *, api_key: Optional[str] = None, model: Optional[str] = None,
    post: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
) -> Optional[Callable[[TgChannel, Campaign], bool]]:
    """A ``(TgChannel, Campaign) -> bool`` gate, or ``None`` to degrade.

    Returns ``None`` (degrade to seeded-only) when no API key is available and no
    explicit ``post`` transport is injected. ``post`` is injectable so tests drive
    the parse + fail-closed boundary without a network call.
    """
    model = model or _default_gate_model()
    key = api_key if api_key is not None else os.environ.get("OPENROUTER_API_KEY", "")
    if not key and post is None:
        log.info("Telegram warming: no OPENROUTER_API_KEY — degrading to "
                 "seeded-channels-only (keyword search + gate skipped)")
        return None

    transport = post or _make_httpx_post(key)

    def gate(channel: TgChannel, campaign: Campaign) -> bool:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _USER_TEMPLATE.format(
                    goal=campaign.goal or "(unspecified)",
                    username=channel.username, title=channel.title or "(no title)",
                    participants=channel.participants)},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        try:
            body = transport(payload)
        except Exception as e:  # noqa: BLE001 - never-throw boundary; fail-closed
            log.debug("Telegram gate transport failed (fail-closed) · %s · %s",
                      channel.username, e)
            return False
        text = _content_or_none(body)
        if text is None:
            return False                      # fail-closed on a malformed reply
        return _coerce_relevant(_extract_json(text))

    return gate


def _make_httpx_post(api_key: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def _post(payload: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover - live only
        import httpx
        r = httpx.post(
            chat_completions_url(),
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json", "X-Title": "AIZU"},
            json=payload, timeout=30.0)
        r.raise_for_status()
        return r.json()
    return _post
