"""The engine contract every per-platform engine implements.

Each engine exposes a module-level ``run_session(...)`` that drives ONE
discovery session for its platform and returns a summary dict carrying the keys
in ``SUMMARY_KEYS``. The control plane (``cli`` -> ``dispatch``) is the only
caller: it builds the ``(router, feed, pacer)`` triple, selects the engine by
``campaign.platform``, and aggregates the summary.

``HaltSession`` lives here (not in any one engine) so every engine and the
control plane raise/catch the SAME exception type (PRD §9 tier 3: stop the
session and alert a human — login expired, checkpoint, action-block, empty
interception, daytime window closed).
"""
from __future__ import annotations

import contextlib
from typing import Literal, Optional, Protocol

# Why a session stopped, classified so the control plane can decide whether the
# halt poisons the SHARED warmed-Chrome browser/account (multi-platform plan C4).
# ``action_block``/``checkpoint``/``login``/``canary`` are account-level — they
# poison the remaining CDP channels in a fan-out. ``daytime`` is a wall-clock gate
# (ends the run, poisons nothing); ``unknown`` is the unclassified default (an API
# rate-limit halt reports ``None``, treated the same as ``unknown`` — never poison).
# ``checkpoint``/``login`` are reserved forward-compat members not raised in scope.
# ``peer_flood`` is the Telegram-specific account-level flood/spam block (raised
# by the TG warming routine); it is account-level like ``action_block`` and the TG
# session handler maps it to a ``peer_flood`` health flag + FLAGGED transition.
HaltKind = Literal["daytime", "action_block", "checkpoint", "login", "canary",
                   "peer_flood", "unknown"]


class HaltSession(Exception):
    """Stop the session and alert a human. ``reason`` is surfaced to the panel;
    ``kind`` classifies it (see ``HaltKind``) so the fan-out knows whether it
    poisons the shared CDP browser. Defaults to ``"unknown"`` (never poison)."""

    def __init__(self, reason: str, kind: HaltKind = "unknown"):
        super().__init__(reason)
        self.reason = reason
        self.kind: HaltKind = kind


@contextlib.contextmanager
def session_crash_guard(store, session_id):
    """Guarantee a discovery session row reaches a terminal state. If the wrapped
    body exits via an UNEXPECTED exception (browser/network crash, etc.), close the
    session as 'halted' with a crash reason so it never lingers as an orphaned
    'running' row. HaltSession passes through untouched — the engine has already
    ended the session with the correct halt reason before raising it."""
    try:
        yield
    except HaltSession:
        raise
    except BaseException as e:
        try:
            store.end_session(session_id, "halted",
                              f"crashed: {type(e).__name__}: {e}"[:500])
        except Exception:  # never mask the real crash with a bookkeeping error
            pass
        raise


# Every engine's run_session() summary dict MUST carry these keys so the CLI
# run-loop aggregation and the panel's per-run summary stay uniform across
# platforms. Engines that have no notion of a key still report it (e.g. YouTube
# and Telegram report likes=0/follows=0/feed_health_flag=False).
SUMMARY_KEYS = (
    "session_id",
    "reels_seen",
    "relevance_passes",
    "matches",
    "escalations",
    "already_seen_skips",
    "spend_usd",
    "feed_health_flag",
    "likes",
    "follows",
    "halt_reason",
    "halt_kind",
)


class EngineProtocol(Protocol):
    """Structural type for an engine module's entrypoint.

    Implemented as a module-level function (not a class) in each engine, so
    ``dispatch.select_engine(platform)`` returns the callable directly.
    """

    def run_session(
        self,
        *,
        campaign,
        store,
        router,
        soul,
        pacer,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
        lead_target: Optional[int] = None,
    ) -> dict:
        ...
