"""Server-side credential capture + validation for per-org platform connections.

The bridge server calls these to validate a customer-supplied credential before
persisting it (encrypted) via Store.set_integration_secret. Kept separate from
the run-time feed clients so the panel connect flow and the engine run path share
no state — only the stored secret.

Validation is a live network call, so callers/tests inject or monkeypatch these
functions. Failures raise ConnectionValidationError (never a bare crash), which
the server maps to a 400 with a user-facing message.
"""
from __future__ import annotations

from typing import Any

_YT_API_BASE = "https://www.googleapis.com/youtube/v3"
_REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_VALIDATE_TIMEOUT_SECONDS = 15.0


class ConnectionValidationError(RuntimeError):
    """A customer credential failed live validation (bad key, unreachable API)."""


def validate_youtube_api_key(api_key: str) -> None:
    """Confirm a YouTube Data API key works with one cheap search.list call.

    Raises ConnectionValidationError for an empty/invalid key or an unreachable
    API. Returns None on success.
    """
    key = (api_key or "").strip()
    if not key:
        raise ConnectionValidationError("API key is required")
    try:
        import httpx
    except Exception as e:  # pragma: no cover - dependency always present
        raise ConnectionValidationError("httpx is required to validate the key") from e
    try:
        resp = httpx.get(
            f"{_YT_API_BASE}/search",
            params={"part": "id", "type": "video", "maxResults": 1, "q": "test", "key": key},
            timeout=_VALIDATE_TIMEOUT_SECONDS,
        )
    except Exception as e:
        raise ConnectionValidationError(f"could not reach the YouTube API: {e}") from e
    if resp.status_code == 200:
        return
    reason = _youtube_error_reason(resp)
    raise ConnectionValidationError(
        reason or f"YouTube rejected the API key (HTTP {resp.status_code})")


def validate_reddit_credentials(client_id: str, client_secret: str,
                                user_agent: str) -> None:
    """Confirm a Reddit app's ``client_credentials`` grant works by minting one
    app-only OAuth token. Read-only public reads need only this app token (no user
    account), mirroring the YouTube key check.

    Raises ConnectionValidationError for missing fields, bad credentials, or an
    unreachable token endpoint. Returns None on success.
    """
    cid = (client_id or "").strip()
    secret = (client_secret or "").strip()
    ua = (user_agent or "").strip()
    if not cid or not secret:
        raise ConnectionValidationError("client_id and client_secret are required")
    if not ua:
        raise ConnectionValidationError(
            "user_agent is required (Reddit rejects requests without a descriptive "
            "User-Agent)")
    try:
        import httpx
    except Exception as e:  # pragma: no cover - dependency always present
        raise ConnectionValidationError(
            "httpx is required to validate the credentials") from e
    try:
        resp = httpx.post(
            _REDDIT_TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(cid, secret),
            headers={"User-Agent": ua},
            timeout=_VALIDATE_TIMEOUT_SECONDS,
        )
    except Exception as e:
        raise ConnectionValidationError(f"could not reach the Reddit API: {e}") from e
    if resp.status_code == 200:
        token = ""
        try:
            body = resp.json()
            token = str(body.get("access_token", "")) if isinstance(body, dict) else ""
        except Exception:  # noqa: BLE001 — tolerant: never trust external JSON
            token = ""
        if token.strip():
            return
        raise ConnectionValidationError(
            "Reddit accepted the credentials but returned no access token")
    if resp.status_code in (401, 403):
        raise ConnectionValidationError(
            "Reddit rejected the app credentials (check client_id / client_secret "
            "and that the app type is 'script' or 'web app')")
    raise ConnectionValidationError(
        f"Reddit rejected the credentials (HTTP {resp.status_code})")


def _youtube_error_reason(resp: Any) -> str:
    """Best-effort human reason from a Data API error body. Tolerant: any parse
    failure yields '' (the caller falls back to the status code), never a crash
    (project rule: never trust external JSON with a bare parse)."""
    try:
        body = resp.json()
    except Exception:
        return ""
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if not isinstance(error, dict):
        return ""
    errors = error.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        reason = errors[0].get("reason")
        if isinstance(reason, str) and reason:
            return f"YouTube API error: {reason}"
    message = error.get("message")
    return f"YouTube API error: {message}" if isinstance(message, str) and message else ""
