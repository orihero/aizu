"""Engagement-isolation guard (warming PRD §4.3, §9.2; warming-writes DECISIONS).

The P0 invariant EVOLVES, it does not break. Warming is now a LEGITIMATE write
path: from the light stage on it calls ``store.log_action`` + the feed write
helpers (``like_reel``/``follow_author``/``save_reel``/``share_reel``) via the
``WarmingActionExecutor``. What still must hold:

  1. Warming performs NO router scoring / match capture / ML spend — it never
     calls ``router.score`` / ``upsert_match`` / ``add_to_watchlist`` /
     ``log_spend`` (§4.3). The IG relevance gate is a cheap zero-ML token-overlap
     heuristic (O-relevance-model), NOT an LLM/router call.
  2. Harvest stays read-only by config-default: the shipped campaign.md does NOT
     enable engagement (§9.2). Harvest engines force ``enable_actions=False`` and
     keep likes=0/follows=0 — byte-for-byte unchanged.
"""
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_WARMING_DIR = _ROOT / "reelradar" / "engines" / "warming"
_WARMING_SESSION = _WARMING_DIR / "session.py"
_WARMING_EXECUTOR = _WARMING_DIR / "executor.py"
_WARMING_TELEGRAM = _WARMING_DIR / "telegram.py"
_WARMING_TG_GATE = _WARMING_DIR / "tg_relevance.py"
_CAMPAIGN_MD = _ROOT / "config" / "campaign.md"

# Call-form patterns the warming engine must never invoke — any one would mean ML
# spend or harvest-side match capture leaking in. Call-form (trailing `(`) so the
# docstring's prose mention of these names is not a false positive.
_FORBIDDEN_IN_WARMING = (
    "upsert_match(", "add_to_watchlist(", "log_spend(",
    "router.score(", "score_comment(", "gate_post(",
)

# The write helpers + log_action warming is NOW allowed to call (the evolved
# invariant). At least one must appear in the executor — that is the whole point.
_ALLOWED_WARMING_WRITES = (
    "log_action(", "like_reel(", "follow_author(", "save_reel(", "share_reel(",
)


def _warming_src() -> str:
    return (_WARMING_SESSION.read_text(encoding="utf-8")
            + "\n" + _WARMING_EXECUTOR.read_text(encoding="utf-8")
            + "\n" + _WARMING_TELEGRAM.read_text(encoding="utf-8")
            + "\n" + _WARMING_TG_GATE.read_text(encoding="utf-8"))


def test_warming_does_no_ml_inference_or_match_writes():
    src = _warming_src()
    leaked = [s for s in _FORBIDDEN_IN_WARMING if s in src]
    assert not leaked, f"warming calls harvest/ML symbols: {leaked}"


def test_telegram_warming_logs_join_and_react_via_log_action():
    # The EVOLVED invariant for Telegram: warming writes via the TG port + logs
    # join/react through store.log_action (never a real campaign — the sentinel).
    tg = _WARMING_TELEGRAM.read_text(encoding="utf-8")
    assert "log_action(" in tg, "TG warming must log its join/react actions"
    assert "'join'" in tg or '"join"' in tg, "TG warming must log a 'join' action"
    assert "'react'" in tg or '"react"' in tg, "TG warming must log a 'react' action"


def test_telegram_warming_relevance_gate_does_not_score_or_log_spend():
    # The thin TG gate is the single ML touch, but it must NOT pull in the harvest
    # router's score / match-capture / spend surface (isolation guard intact).
    gate = _WARMING_TG_GATE.read_text(encoding="utf-8")
    leaked = [s for s in _FORBIDDEN_IN_WARMING if s in gate]
    assert not leaked, f"TG relevance gate calls harvest/ML symbols: {leaked}"


def test_warming_may_call_log_action_and_feed_write_helpers():
    # The EVOLVED invariant: warming writes. The executor is the write path, so at
    # least log_action + a feed write helper must be present there.
    executor = _WARMING_EXECUTOR.read_text(encoding="utf-8")
    assert "log_action(" in executor, \
        "warming executor must log its engagement actions"
    assert any(h in executor for h in
               ("like_reel(", "follow_author(", "save_reel(", "share_reel(")), \
        "warming executor must call at least one feed write helper"


def test_warming_executor_uses_no_fixed_sleep_constant():
    # Delays must be randomized/right-skewed — no literal time.sleep(constant).
    executor = _WARMING_EXECUTOR.read_text(encoding="utf-8")
    assert "time.sleep(" not in executor, \
        "executor delays must go through the injectable Pacer, never time.sleep()"


def test_shipped_campaign_is_read_only_by_default():
    text = _CAMPAIGN_MD.read_text(encoding="utf-8")
    assert "enable_actions: false" in text, \
        "harvest must be read-only by config-default (warming PRD §9.2)"
    assert "enable_actions: true" not in text
