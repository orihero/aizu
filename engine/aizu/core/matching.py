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

# Extracted-field keys that name WHO the commenter is rather than what they
# want. Deliberately a SEPARATE set from `_CONTACT_FIELD_HINTS`: a contact field
# is a detail the person volunteered and the customer is entitled to (F.3 renders
# it in the drawer before any reveal), whereas the platform handle is the very
# thing the v27 redaction hides — it is what turns a public post into a name.
#
# Kept out of `_CONTACT_FIELD_HINTS` on purpose, because that set also drives
# `ground_extracted`, which DROPS an ungrounded value; changing what is stored is
# a bigger blast radius than changing what is published, and the superadmin plane
# reads the stored row. So these hints gate publication only.
#
# "name" is NOT a hint here: it is a substring of `product_name`/`brand_name`,
# which are the headline facts the intent line is built from.
_IDENTITY_FIELD_HINTS = ("username", "user_name", "handle", "nickname", "nick",
                        "instagram", "insta", "profile", "account", "author",
                        "commenter", "screen_name", "screenname")

_NON_DIGIT_RE = re.compile(r"\D")


def _is_contact_field(key: str) -> bool:
    k = key.lower()
    return any(hint in k for hint in _CONTACT_FIELD_HINTS)


def _is_identity_field(key: str) -> bool:
    """True for an extracted key that names the person rather than the need."""
    k = key.lower()
    return any(hint in k for hint in _IDENTITY_FIELD_HINTS)


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
# Customer-facing intent line (schema v27)
# --------------------------------------------------------------------------- #
# The org-facing lead payload carries NO username and NO comment text: an
# operator sees only `intent`, one sentence saying what the commenter wants. So
# this function is the redaction boundary, not a formatting nicety — anything
# identifying that survives here is published to every viewer of the org, and
# the raw comment stays available in the superadmin plane for the cases where
# somebody genuinely needs it.
#
# Two failure modes it exists to stop, both observed from real classifier
# output: the model "summarizing" by echoing the comment back verbatim (a
# redaction that reprints the comment redacts nothing), and the model helpfully
# folding the handle/phone/e-mail it just extracted into the summary sentence.
# Contact details belong in `extracted`, which the panel renders under its own
# affordances; they must never ride along inside prose.
#
# Pure and stdlib-only: every platform session imports this module.

INTENT_MAX_CHARS = 180

_ELLIPSIS = "…"

# Identity shapes stripped from anything we keep. E-mails are matched FIRST
# because they contain "@" and would otherwise be half-eaten by the handle rule.
_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@,;:!?]+")
_HANDLE_RE = re.compile(r"(?<![\w./])@[\w.]{2,}")
# Phone-shaped: a separator-tolerant digit run ("+998 90 123 45 67"), kept only
# if it actually carries `_MIN_PHONE_DIGITS` digits (defined with the pre-filter
# below — same floor, same reason). Counting DIGITS rather than characters is
# what keeps "budget 500 000" in the sentence while "+998 90 123 45 67" leaves
# it; a character-length rule ate both.
_PHONE_CANDIDATE_RE = re.compile(r"\+?\d[\d\s()./‐-―-]{3,}\d")
_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
# A bare profile link is a handle with a domain in front of it, and people write
# them without a scheme far more often than with one.
_CONTACT_LINK_RE = re.compile(
    r"(?i)\b(?:t\.me|telegram\.me|wa\.me|m\.me|instagram\.com|linkedin\.com)/\S+")
_HASHTAG_RE = re.compile(r"#\w+")

# Extracted keys that already NAME the thing wanted, so the key adds nothing to
# the sentence ("Interested in sneakers", not "Interested in product sneakers").
# Any other key is prefixed with its own name, because the bare value is
# meaningless on its own ("42" vs "size 42").
_HEADLINE_KEYS = ("product", "item", "service", "interest", "intent", "need",
                  "want", "wants", "looking for", "request", "topic", "subject",
                  "query", "goal")

# How much of the caption may become the topic phrase. Long enough to name a
# subject, short enough that it can't smuggle a whole post into the line.
_TOPIC_MAX_WORDS = 8
_TOPIC_MAX_CHARS = 60

# A candidate this close to the comment is the comment. Compared as a prefix in
# either direction so a trailing "." or a truncated echo doesn't sneak past.
_ECHO_PREFIX_RATIO = 0.9
# ...and any contiguous overlap at least this long is copied text however it is
# framed ("The user says: <comment>", or the comment minus its last clause). The
# floor keeps a genuinely short summary ("asking price") that happens to appear
# in the comment from being thrown away — a phrase is not the comment.
_ECHO_SUBSTRING_MIN_CHARS = 15


def _tidy(text: str) -> str:
    """Collapse whitespace and repair the punctuation a removal left behind."""
    out = " ".join(text.split())
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)      # " ," from a stripped token
    out = re.sub(r"([,;:])(\s*[,;:])+", r"\1", out)  # ", ," from two of them
    return out.strip(" \t,;:.-–—")


def _strip_phones(text: str) -> str:
    def _drop(m: "re.Match[str]") -> str:
        return " " if len(_digits_only(m.group(0))) >= _MIN_PHONE_DIGITS else m.group(0)
    return _PHONE_CANDIDATE_RE.sub(_drop, text)


def _strip_identity(text: str) -> str:
    """Remove @handles, e-mails, profile links and phone-shaped digit runs."""
    out = _EMAIL_RE.sub(" ", text)
    out = _URL_RE.sub(" ", out)
    out = _CONTACT_LINK_RE.sub(" ", out)
    out = _HANDLE_RE.sub(" ", out)
    return _tidy(_strip_phones(out))


def _truncate(text: str) -> str:
    """At most `INTENT_MAX_CHARS`, cut on a word boundary, ellipsis if cut."""
    if len(text) <= INTENT_MAX_CHARS:
        return text
    cut = text[:INTENT_MAX_CHARS - 1]
    space = cut.rfind(" ")
    if space >= INTENT_MAX_CHARS // 2:   # only if a boundary exists late enough
        cut = cut[:space]
    return cut.rstrip(" ,;:.-–—") + _ELLIPSIS


def _echo_key(text: Optional[str]) -> str:
    """Comparison form for the echo test: whitespace/case/edge-punctuation blind."""
    return " ".join((text or "").split()).casefold().strip(" \"'`.,;:!?…")


def _echoes_comment(candidate: str, comment_text: Optional[str]) -> bool:
    """True when `candidate` is just the comment handed back.

    Equality, either string being a >=90% prefix of the other, or either being
    contained in the other once it is long enough to be the comment rather than
    a phrase. A model that "summarizes" by copying the comment and trimming the
    last clause, or by wrapping it in "the user asks: ...", is the common shape;
    all of those are still the comment, and all of them are checked in both
    directions.
    """
    a, b = _echo_key(candidate), _echo_key(comment_text)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if long.startswith(short) and len(short) >= _ECHO_PREFIX_RATIO * len(long):
        return True
    return len(short) >= _ECHO_SUBSTRING_MIN_CHARS and short in long


def _topic_phrase(post_caption: Optional[str]) -> str:
    """A short "what the post was about" phrase, identity- and hashtag-free."""
    if not isinstance(post_caption, str) or not post_caption.strip():
        return ""
    text = _HASHTAG_RE.sub(" ", post_caption)   # links go with `_strip_identity`
    text = re.split(r"[.!?\n\r]", text, maxsplit=1)[0]  # first sentence only
    words = _strip_identity(text).split()[:_TOPIC_MAX_WORDS]
    return _tidy(" ".join(words))[:_TOPIC_MAX_CHARS].strip()


def _fallback_intent(extracted: Optional[dict[str, Any]],
                     post_caption: Optional[str],
                     comment_text: Optional[str]) -> str:
    """Compose a line from the grounded facts, never from the comment itself.

    Only NON-contact, NON-identity `extracted` keys contribute — the contact
    fields are the identity this whole change hides, and an identity-named key
    (`instagram_handle`, `username`, `profile`…) is the handle itself. The
    identity exclusion is load-bearing rather than tidy: `_strip_identity` only
    recognises an `@`-prefixed handle, so `{"instagram_handle": "alibek_uz"}`
    would otherwise compose straight into "Interested in … instagram handle
    alibek_uz" — the redacted line publishing the exact handle it exists to
    hide. Values are
    identity-stripped too: a model that stuffed a handle into `note` doesn't get
    to publish it via the back door, and a field that simply holds the comment
    (`{"question": "<the whole comment>"}`) is dropped by the same echo test the
    model's own line faces — checked PER VALUE, because a copied value wrapped
    in "Interested in ..." no longer looks like an echo of the assembled line.
    """
    parts: list[str] = []
    if isinstance(extracted, dict):
        for key, value in extracted.items():
            if not isinstance(key, str) or _is_contact_field(key) \
                    or _is_identity_field(key):
                continue
            # Booleans carry no phrasing a viewer could act on; None is "the
            # model didn't know", which is not a fact about what they want.
            if value is None or isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                text = str(value)
            elif isinstance(value, str):
                text = value
            else:
                continue
            text = _strip_identity(text)
            if not text or _echoes_comment(text, comment_text):
                continue
            label = key.replace("_", " ").replace("-", " ").strip().lower()
            part = (text if label in _HEADLINE_KEYS or label in text.lower()
                    else f"{label} {text}")
            if part not in parts:
                parts.append(part)
    topic = _topic_phrase(post_caption)
    if topic and _echoes_comment(topic, comment_text):
        topic = ""   # a caption that IS the comment (a self-reply) is no topic
    if parts:
        line = "Interested in " + ", ".join(parts)
        if topic:
            line += f" — asking on a post about {topic}"
    elif topic:
        line = f"Asking on a post about {topic}"
    else:
        return ""
    return _tidy(line)


def derive_intent(model_intent: Any, *, extracted: Optional[dict[str, Any]],
                  post_caption: Optional[str],
                  comment_text: Optional[str] = None) -> str:
    """The one line a customer sees instead of the username and the comment.

    Prefers the classifier's own `intent` (cleaned, identity-stripped, clipped
    to `INTENT_MAX_CHARS`), falls back to a deterministic line built from the
    grounded `extracted` fields plus a topic phrase from the post, and returns
    `""` when there is honestly nothing to say — the UI renders a neutral
    placeholder for that, because a guessed intent is worse than a blank one.

    The model's line is DISCARDED, not repaired, when it is the comment echoed
    back: repairing it would still be the comment.
    """
    candidate = model_intent.strip() if isinstance(model_intent, str) else ""
    if candidate and not _echoes_comment(candidate, comment_text):
        kept = _truncate(_strip_identity(candidate))
        if kept:
            return kept
    fallback = _fallback_intent(extracted, post_caption, comment_text)
    # The fallback is built from other fields, but a model can put the comment
    # in one of them — so it faces the same echo test before it is published.
    if fallback and not _echoes_comment(fallback, comment_text):
        return _truncate(fallback)
    return ""


# --------------------------------------------------------------------------- #
# Org-boundary scrub (v27 redaction, defence in depth)
# --------------------------------------------------------------------------- #
# `derive_intent` guards the ONE line it composes. It does not — and cannot —
# guard the other two pieces of model-authored prose that travel with a lead:
#
#   * `reason`, the classifier's justification. No prompt in `core/prompts.py`
#     has ever constrained it (`MATCH_INTENT_RULE` binds "intent" alone), and a
#     campaign-authored match prompt predating v27 constrains nothing at all. A
#     reason of the shape 'Commenter @alibek_uz writes "<the whole comment>" —
#     clear buying intent' is ordinary model output, and it shipped verbatim.
#   * non-contact `extracted` values. `_fallback_intent` already refuses a value
#     that is just the comment — but it refused it only for the composed line;
#     the same dict was published raw beside it.
#
# So the scrub belongs at the boundary, where the two strings that must not
# appear are actually KNOWN: the row carries `username` and `text`. Guessing at
# identity shapes is a fallback; matching the literal handle is not a guess.
#
# Contact fields are deliberately NOT scrubbed — a detail the person volunteered
# is the product, and F.3 renders it before any reveal.

# A handle shorter than this would match ordinary words even on a word boundary.
_MIN_KNOWN_HANDLE_CHARS = 3
# A copied run of the comment this long IS the comment, however it is framed.
# Counted in WORDS (not characters) so the floor means the same thing in Uzbek,
# Russian and English; four words is past the point where "clear buying intent"
# style boilerplate can collide with it by accident.
_QUOTE_MIN_WORDS = 4
# Bounds on the excision scan. A comment longer than this is scanned only up to
# the cap (the leading words are what gets quoted), and a reason splicing more
# than a handful of separate runs is not a shape real classifiers produce — both
# limits exist so a pathological row cannot make a leads page quadratic.
_QUOTE_SCAN_MAX_WORDS = 120
_QUOTE_MAX_REMOVALS = 6


_NON_WORD_RE = re.compile(r"\W+", re.UNICODE)


def _word_key(text: str) -> str:
    """Word characters only, case-blind — "did this lose any CONTENT?"."""
    return _NON_WORD_RE.sub("", text).casefold()


def _strip_known_username(text: str, username: Optional[str]) -> str:
    """Remove the lead's actual handle, with or without its `@`, case-blind.

    Word-bounded (`.` and `_` count as part of a handle) so `alibek` does not
    eat the `alibek` inside a longer token. A coincidental collision with a real
    word is possible and is the safe direction: over-redacting prose costs a
    little clarity, under-redacting it names the person."""
    handle = (username or "").strip().lstrip("@")
    if len(handle) < _MIN_KNOWN_HANDLE_CHARS or not text:
        return text
    return re.sub(r"(?i)(?<![\w.])@?" + re.escape(handle) + r"(?![\w.])",
                  " ", text)


def _excise_comment_quotes(text: str, comment_text: Optional[str]) -> str:
    """Cut every run of `comment_text` that was copied into `text`.

    Longest run first, so one pass removes a whole quoted comment instead of
    nibbling at it. Unlike `_echoes_comment` — which asks "is this candidate
    JUST the comment?" and discards the whole string — this keeps the framing
    prose and removes only the copied words, because a reason is still useful
    once the quotation is gone."""
    words = " ".join((comment_text or "").split()).split()[:_QUOTE_SCAN_MAX_WORDS]
    if len(words) < _QUOTE_MIN_WORDS or not text:
        return text
    out = " ".join(text.split())
    for _ in range(_QUOTE_MAX_REMOVALS):
        found = ""
        for size in range(len(words), _QUOTE_MIN_WORDS - 1, -1):
            for start in range(len(words) - size + 1):
                phrase = " ".join(words[start:start + size])
                if phrase.casefold() in out.casefold():
                    found = phrase
                    break
            if found:
                break
        if not found:
            break
        idx = out.casefold().find(found.casefold())
        out = out[:idx] + " " + out[idx + len(found):]
    return out


def redact_identity(text: Any, *, username: Optional[str] = None,
                    comment_text: Optional[str] = None) -> str:
    """Model-authored prose, made safe to publish to an org.

    Three passes, in order: the identity SHAPES `derive_intent` already strips
    (@handles, e-mails, profile links, phone-shaped digit runs), then the handle
    we actually know, then any verbatim run of the comment. Returns `""` when
    nothing survives — a reason that was only a quotation has nothing left to
    say, and a blank field renders as "not captured" rather than as a fragment.
    """
    if not isinstance(text, str) or not text.strip():
        return ""
    collapsed = " ".join(text.split())
    out = _strip_identity(collapsed)
    out = _strip_known_username(out, username)
    out = _excise_comment_quotes(out, comment_text)
    # `_strip_identity` tidies unconditionally, which also eats the closing full
    # stop of a sentence nothing was removed from. Compare on WORD characters so
    # punctuation-only churn reads as untouched: every removal above deletes word
    # characters (a handle, an address, a digit run, quoted words), so this can
    # only ever call an actual redaction "unchanged" if it removed nothing.
    if _word_key(out) == _word_key(collapsed):
        return collapsed
    return _tidy(out)


def redact_extracted(extracted: Any, *, username: Optional[str] = None,
                     comment_text: Optional[str] = None) -> dict[str, Any]:
    """The org-facing `extracted` dict.

    CONTACT values pass through untouched: they are what the customer bought,
    they are already grounded against the comment by `ground_extracted`, and
    scrubbing them would delete the phone number the lead is for.

    Everything else is model-authored text and gets `redact_identity`, plus one
    extra rule — a non-contact value that IS the comment is DROPPED rather than
    trimmed. `{"question": "<the whole comment>"}` is a shape `_fallback_intent`
    already refuses for the intent line; the value carries no fact the customer
    can act on once its quotation is removed, so keeping an empty husk of it
    would only imply the model extracted something.

    Identity-named keys are dropped outright — see `_IDENTITY_FIELD_HINTS`.
    Non-string values (numbers, booleans, nested objects) are structural, not
    prose, and are passed through as they are.
    """
    if not isinstance(extracted, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in extracted.items():
        if isinstance(key, str) and _is_identity_field(key):
            continue
        if isinstance(key, str) and _is_contact_field(key):
            out[key] = value
            continue
        if not isinstance(value, str):
            out[key] = value
            continue
        if _echoes_comment(value, comment_text):
            continue
        out[key] = redact_identity(value, username=username,
                                   comment_text=comment_text)
    return out


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
