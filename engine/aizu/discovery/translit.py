"""Uzbek script fan-out.

Uzbek is written in three overlapping systems in the wild: pre-1993 Cyrillic
(still dominant on Telegram and among older users), the official post-1995 Latin,
and the 2021-reformed Latin. A hashtag typed in one is a *different string* from
the same word in another, and the platforms do not fold them — so a brief seeded
with `#tamirlash` silently misses every `#таъмирлаш` post.

`UzTransliterator` (arXiv:2205.09578) does this properly, but it is a heavy pip
dependency for what is, at the tag level, a character mapping plus a handful of
digraphs. This module keeps the engine dependency-free; if the corpus ever needs
true morphological transliteration, swap the internals — the surface is one
function.

The apostrophe collapse matters as much as the script flip: `oʻzbek`, `o'zbek`,
`o‘zbek` and `ozbek` are four distinct strings, and hashtags in practice drop the
mark entirely.
"""
from __future__ import annotations

import unicodedata

# Every apostrophe-shaped character Uzbek Latin is written with in the wild. The
# ʻ (modifier letter turned comma, U+02BB) is the correct one; nobody types it.
_APOSTROPHES = "'‘’ʻʼ`´"

# Latin → Cyrillic. Digraphs MUST be tried before single letters ("sh" before
# "s"), so this is an ordered list, not a dict.
_LAT_TO_CYR: tuple[tuple[str, str], ...] = (
    ("shch", "щ"), ("yo", "ё"), ("yu", "ю"), ("ya", "я"), ("ye", "е"),
    ("ch", "ч"), ("sh", "ш"), ("ng", "нг"), ("ts", "ц"),
    ("o‘", "ў"), ("o'", "ў"), ("oʻ", "ў"), ("g‘", "ғ"), ("g'", "ғ"), ("gʻ", "ғ"),
    ("a", "а"), ("b", "б"), ("d", "д"), ("e", "е"), ("f", "ф"), ("g", "г"),
    ("h", "ҳ"), ("i", "и"), ("j", "ж"), ("k", "к"), ("l", "л"), ("m", "м"),
    ("n", "н"), ("o", "о"), ("p", "п"), ("q", "қ"), ("r", "р"), ("s", "с"),
    ("t", "т"), ("u", "у"), ("v", "в"), ("x", "х"), ("y", "й"), ("z", "з"),
)

# Cyrillic → Latin (official post-1995 orthography, with the modifier-letter
# apostrophes the reform mandates).
_CYR_TO_LAT: tuple[tuple[str, str], ...] = (
    ("щ", "shch"), ("ё", "yo"), ("ю", "yu"), ("я", "ya"), ("ч", "ch"),
    ("ш", "sh"), ("ц", "ts"), ("ў", "oʻ"), ("ғ", "gʻ"), ("ъ", "ʼ"), ("ь", ""),
    ("а", "a"), ("б", "b"), ("в", "v"), ("г", "g"), ("д", "d"), ("е", "e"),
    ("ж", "j"), ("з", "z"), ("и", "i"), ("й", "y"), ("к", "k"), ("қ", "q"),
    ("л", "l"), ("м", "m"), ("н", "n"), ("о", "o"), ("п", "p"), ("р", "r"),
    ("с", "s"), ("т", "t"), ("у", "u"), ("ф", "f"), ("х", "x"), ("ҳ", "h"),
    ("ы", "i"), ("э", "e"),
)

_CYRILLIC_RANGE = range(0x0400, 0x0500)


def is_cyrillic(text: str) -> bool:
    """True when the text is written predominantly in Cyrillic.

    Predominantly, not exclusively: real seeds are mixed ("ремонт Toshkent"),
    and the question this answers is only "which direction do we transliterate"."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    cyr = sum(1 for c in letters if ord(c) in _CYRILLIC_RANGE)
    return cyr * 2 > len(letters)


def strip_apostrophes(text: str) -> str:
    """Drop every apostrophe variant — how these words are actually hashtagged."""
    return "".join(c for c in text if c not in _APOSTROPHES)


def _apply(text: str, table: tuple[tuple[str, str], ...]) -> str:
    """Greedy longest-match mapping, preserving the case of the first letter.

    Case is handled by lowercasing for the lookup and re-capitalising after,
    rather than by doubling every table entry: seeds are lowercase by convention
    and hashtags are case-insensitive on every platform we walk."""
    lowered = text.lower()
    out: list[str] = []
    i = 0
    while i < len(lowered):
        for src, dst in table:
            if lowered.startswith(src, i):
                out.append(dst)
                i += len(src)
                break
        else:
            out.append(lowered[i])
            i += 1
    return "".join(out)


def to_cyrillic(text: str) -> str:
    """Uzbek Latin → Cyrillic. Already-Cyrillic text is returned unchanged."""
    return text if is_cyrillic(text) else _apply(text, _LAT_TO_CYR)


def to_latin(text: str) -> str:
    """Uzbek Cyrillic → official Latin. Already-Latin text is returned unchanged."""
    return _apply(text, _CYR_TO_LAT) if is_cyrillic(text) else text


def script_variants(term: str) -> list[str]:
    """Every spelling of `term` worth searching, `term` itself first.

    Deduped and order-stable so a caller can take the head as the canonical form.
    Round-tripping is deliberately NOT asserted: `ы→i` and `ь→` are lossy, and a
    variant that does not round-trip is still a real string real users type."""
    term = unicodedata.normalize("NFC", (term or "").strip())
    if not term:
        return []
    out: list[str] = []
    for candidate in (term, to_cyrillic(term), to_latin(term),
                      strip_apostrophes(term),
                      strip_apostrophes(to_latin(term))):
        c = candidate.strip()
        if c and c not in out:
            out.append(c)
    return out
