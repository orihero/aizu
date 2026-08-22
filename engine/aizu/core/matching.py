"""Shared match-decision helpers used by every platform's session loop."""
from __future__ import annotations

import re
from typing import Any, Optional

# Extracted-field keys treated as verifiable contact info — the only fields the
# deterministic grounding check touches (gap #4). Substring match, not name
# match: any key whose lowercased name CONTAINS one of these hints qualifies
# (e.g. "phone_number", "whatsapp_contact").
_CONTACT_FIELD_HINTS = ("phone", "mobile", "whatsapp", "telegram", "contact",
                       "email", "e-mail", "e_mail")

_NON_DIGIT_RE = re.compile(r"\D")


def _is_contact_field(key: str) -> bool:
    k = key.lower()
    return any(hint in k for hint in _CONTACT_FIELD_HINTS)


def _digits_only(s: str) -> str:
    return _NON_DIGIT_RE.sub("", s)


def ground_extracted(extracted: Optional[dict[str, Any]],
                     *sources: Optional[str]) -> dict[str, Any]:
    """Drop a hallucinated contact value the classifier invented wholesale.

    Cheap deterministic defense (gap #4's "grounding check") against trusting a
    single LLM call's ``extracted`` verbatim: for any key that LOOKS like a
    contact field (see ``_CONTACT_FIELD_HINTS``), the value must actually appear
    in ``sources`` — the same comment text (and reel/submission context) that
    was fed to the classifier — or it is dropped to ``None`` rather than kept.
    Phone-shaped values are compared digits-only so formatting differences
    (spaces/dashes/parens) between the model's normalized output and the raw
    source text don't cause a false drop; anything else is compared as a
    case-insensitive substring. Every non-contact field passes through
    untouched — this is NOT a general hallucination filter, just contact info.

    Never mutates; returns a new dict (or the input unchanged if it isn't a
    non-empty dict — mirrors ``_coerce_extracted``'s no-op-on-empty shape).
    """
    if not isinstance(extracted, dict) or not extracted:
        return extracted if isinstance(extracted, dict) else {}
    haystack = "\n".join(s for s in sources if s)
    haystack_lower = haystack.lower()
    haystack_digits = _digits_only(haystack)
    grounded: dict[str, Any] = {}
    for key, value in extracted.items():
        if _is_contact_field(key) and isinstance(value, str) and value.strip():
            v = value.strip()
            digits = _digits_only(v)
            if len(digits) >= 5:  # phone-shaped — compare on digits alone
                grounded[key] = value if digits in haystack_digits else None
            else:                 # email or free-text contact — substring
                grounded[key] = value if v.lower() in haystack_lower else None
        else:
            grounded[key] = value
    return grounded


# --------------------------------------------------------------------------- #
# Cheap comment pre-filter (Campaign Lab, Remedy Sheet #3 / Remedy C)
# --------------------------------------------------------------------------- #
# Three cascade docstrings have advertised "local pre-filter → local scoring →
# escalate-if-unsure → cloud" since they were written, and no pre-filter existed:
# every comment, including every bare "🔥🔥", bought a model call.
#
# The rule that shapes what may live here: a pre-filtered comment is NEVER
# SCORED AND NEVER STORED, so a wrong skip is an invisible lost lead — the same
# failure mode as the relevance gate's false negatives. Therefore only comments
# that CANNOT be a lead under any reading are filtered, and anything arguable
# goes to the model.
#
# In particular sellers are deliberately NOT filtered: Sheet #3 / Remedy B is
# explicit that supply-side commenters are routed for competitor intel, never
# dropped. Detecting them is `discovery/buyer_density.classify_comment`; acting
# on them belongs to the output-contract redesign, not to a silent skip.

SKIP_EMPTY = "empty"
SKIP_NO_WORDS = "no_words"
SKIP_DUPLICATE = "duplicate"

# Unicode word characters. A comment with none of these carries no proposition:
# emoji, punctuation, or a bare "+1" of arrow characters. Digits alone do NOT
# qualify as words here — but see the guard below, because a bare phone number is
# a real (seller-shaped) signal and must survive.
_WORD_RE = re.compile(r"[^\W\d_]", re.UNICODE)
# Total digits, NOT a consecutive run: a real number is written "+998 90 123 45 67"
# far more often than "998901234567", and a run-based test filtered exactly the
# contact details this exemption exists to protect. Seven is the shortest
# plausible subscriber number.
_MIN_PHONE_DIGITS = 7


def _digit_count(text: str) -> int:
    return sum(1 for c in text if c.isdigit())


def comment_prefilter_reason(text: Optional[str], *,
                             username: Optional[str] = None,
                             seen: Optional[set[str]] = None) -> Optional[str]:
    """Why this comment cannot be a lead, or None to send it to the model.

    `seen` is a per-session set of already-scored `(author, text)` pairs, mutated
    here.

    KEYED ON THE AUTHOR, not on the text alone. Text-only dedupe looks like the
    cheapest spam signal there is and is actively wrong for this engine: the
    highest-value comments are SHORT, COMMON buyer questions — "narxi qancha?",
    "how much?", "цена?" — and two different people asking the same question
    under two different posts are two leads, not a broadcast. Text-only dedupe
    drops the second one silently, and it drops it precisely because it was a
    textbook buyer phrase. One ACCOUNT repeating itself is the real spam
    pattern, and it is also already-captured: that person is a lead we have.

    Deliberately conservative. Three rules, each of which is a certainty:
      * nothing at all;
      * no letters anywhere (emoji/punctuation only) AND too few digits to be a
        phone number — the digit exemption keeps a bare "+998 90 123 45 67"
        alive, which is a real contact signal despite containing no letters;
      * the same AUTHOR repeating text they already had scored this session.
    """
    body = (text or "").strip()
    if not body:
        return SKIP_EMPTY
    if not _WORD_RE.search(body) and _digit_count(body) < _MIN_PHONE_DIGITS:
        return SKIP_NO_WORDS
    if seen is not None and username:
        key = f"{str(username).strip().lower()}\x00{' '.join(body.lower().split())}"
        if key in seen:
            return SKIP_DUPLICATE
        seen.add(key)
    return None


def corroboration_needs_review(primary_score: float,
                               comparisons: list[dict[str, Any]],
                               threshold: float) -> bool:
    """The optional corroboration GATE (gap #4): True when a comparison model
    disagrees with, or was inconclusive about, the primary's threshold verdict.

    Callers only invoke this when the campaign has explicitly opted in (see
    ``Campaign.require_corroboration``) — off by default, so today's accept
    path is unaffected. Agreement across every comparison => False (keep the
    verdict); any disagreement or inconclusive comparison (errored, or scoreless)
    => True (the caller demotes the match to ``needs_review`` instead of a hard
    accept). No comparisons at all (the model-comparison fan-out isn't active,
    e.g. the superadmin switch is off) => False — the campaign flag alone has
    nothing to gate on, so it is a no-op rather than a footgun that blocks every
    match until an unrelated admin setting is also flipped.
    """
    if not comparisons:
        return False
    primary_match = primary_score >= threshold
    for c in comparisons:
        if c.get("error"):
            return True
        score = c.get("score")
        if score is None:
            return True
        if (float(score) >= threshold) != primary_match:
            return True
    return False


def compute_found_by(primary_model: str, comparisons: list[dict[str, Any]],
                     threshold: float) -> list[str]:
    """Which models independently would have called this comment a match.

    Always includes `primary_model` (it's what created the lead). Every
    comparison model whose own score cleared the campaign's threshold is added
    too, in order, de-duplicated; a comparison that errored is excluded (an
    unknown verdict is not a "found it"). Empty/falsy inputs degrade gracefully
    (no primary model name, or the feature off with no comparisons) to `[]`.
    """
    found: list[str] = []
    if primary_model:
        found.append(primary_model)
    for c in comparisons:
        model = c.get("model")
        score = c.get("score")
        if not model or model in found or c.get("error"):
            continue
        if score is not None and score >= threshold:
            found.append(model)
    return found
