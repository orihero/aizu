"""The demand-side request-pattern matrix.

Campaign Lab, Remedy Sheet #1 / Remedy A.2. The generated `seedHashtags` today
are *marketing* vocabulary — `#videography`, `#weddingfilm` — which is what
sellers tag their own work with. Buyers do not write that. Buyers write
"videograf kerak", "кто снимает свадьбы?", "any videographer recs?".

Every platform this engine searches (X, Telegram, Reddit, LinkedIn, YouTube) takes
a free-text query, so the highest-yield search string is a REQUEST, not a tag. The
same list doubles as the vocabulary the match prompt should expect in comments —
Sheet #3's role gate needs exactly these buyer-side markers.

The patterns are fixed and multilingual by design: an LLM asked to invent them
produces textbook phrasings, while these are the forms that actually appear in
uz/ru/en comment sections. `{}` is the noun slot.
"""
from __future__ import annotations

from typing import Iterable, Sequence

# Ordered by how strongly each family signals purchase intent, strongest first.
# `family` is carried through so a caller can weight or filter (a `price_ask` hit
# is worth more than a `recommend` hit, and Sheet #3's stage ladder uses the same
# vocabulary).
REQUEST_PATTERNS: tuple[tuple[str, str, str], ...] = (
    # (language, family, template)
    ("uz", "price_ask",   "{} narxi"),
    ("uz", "price_ask",   "{} qancha turadi"),
    ("uz", "price_ask",   "{} narxi qancha"),
    ("uz", "need",        "{} kerak"),
    ("uz", "need",        "{} kerak edi"),
    ("uz", "need",        "{} izlayapman"),
    ("uz", "recommend",   "yaxshi {} bormi"),
    ("uz", "recommend",   "{} tavsiya qiling"),
    ("uz", "how_to_buy",  "{} qanday buyurtma qilaman"),
    ("uz", "where",       "toshkentda {} qayerda"),

    ("ru", "price_ask",   "{} сколько стоит"),
    ("ru", "price_ask",   "{} цена"),
    ("ru", "need",        "нужен {}"),
    ("ru", "need",        "ищу {}"),
    ("ru", "recommend",   "посоветуйте {}"),
    ("ru", "recommend",   "кто делает {}"),
    ("ru", "how_to_buy",  "как заказать {}"),
    ("ru", "where",       "где найти {} в ташкенте"),

    ("en", "price_ask",   "{} how much"),
    ("en", "price_ask",   "{} pricing"),
    ("en", "need",        "looking for a {}"),
    ("en", "need",        "need a {}"),
    ("en", "recommend",   "{} recommendations"),
    ("en", "recommend",   "any good {}"),
    ("en", "how_to_buy",  "how to hire a {}"),
    ("en", "worth_it",    "is {} worth it"),
)

# Question prefixes for autocomplete mining. Prepending one of these to a noun is
# what makes Google's suggest endpoint return QUESTIONS people ask rather than
# navigational completions — the trick AnswerThePublic productised.
QUESTION_PREFIXES: dict[str, tuple[str, ...]] = {
    "uz": ("qanday", "qancha", "qayerda", "qaysi", "nima uchun"),
    "ru": ("как", "сколько", "где", "какой", "почему"),
    "en": ("how", "how much", "where", "which", "why", "best"),
}

SUPPORTED_LANGS = tuple(QUESTION_PREFIXES)


def request_patterns(langs: Sequence[str] = SUPPORTED_LANGS
                     ) -> list[tuple[str, str, str]]:
    """The pattern rows for `langs`, in priority order.

    An unknown language code is simply absent from the result — a brief written
    for a language we have no patterns for gets noun-only expansion rather than
    English patterns wrongly labelled as its own."""
    wanted = {str(l).strip().lower()[:2] for l in langs if str(l).strip()}
    return [row for row in REQUEST_PATTERNS if row[0] in wanted]


def demand_queries(nouns: Iterable[str], *,
                   langs: Sequence[str] = SUPPORTED_LANGS,
                   families: Sequence[str] = (),
                   limit: int = 60) -> list[str]:
    """Cross `nouns` with the request-pattern matrix into concrete search strings.

    Deduped, order-stable (noun-major, then pattern priority), capped. The cap is
    real: 6 nouns x 26 patterns is 156 queries, and every one of them costs a
    search on some platform."""
    rows = request_patterns(langs)
    if families:
        keep = {str(f).strip().lower() for f in families}
        rows = [r for r in rows if r[1] in keep]
    out: list[str] = []
    seen: set[str] = set()
    for noun in nouns:
        noun = str(noun).strip().lstrip("#")
        if not noun:
            continue
        for _lang, _family, template in rows:
            q = template.format(noun).strip()
            key = q.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(q)
            if len(out) >= limit:
                return out
    return out
