"""Buyer density — how many people in this account's comments are actually buying.

Campaign Lab, Remedy Sheet #2 / Remedy B. The research's finding: no commercial
tool scores this. HypeAuditor and Modash score audience AUTHENTICITY; nobody
scores buyer-question density in comment sections. For an engine whose leads ARE
commenters, that is the metric that decides whether a seed is worth walking, and
it is ours to build.

The number: **price-intent questions per 100 comments**, plus two corroborating
signals — the comment/like ratio (a thin comment section has nothing to harvest,
however many likes it has) and the owner reply-rate (an account that answers price
questions is commerce-active; one that ignores them is a content account).

Deliberately regex-first, not LLM-first. These are 3-to-6-word fragments in
uz/ru/en where a fixed pattern list is both cheaper and more reproducible than a
model call, and the research on short-text classification (Sheet #3) says the same.
An LLM pass belongs on the AMBIGUOUS residue, not on "narxi?".

The pattern tables here are the same vocabulary Sheet #3's role gate needs, so
they live in one place and are imported, not re-typed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from ..core.logsetup import get_logger

log = get_logger(__name__)

# --------------------------------------------------------------------------- #
# Demand side — a commenter asking to buy.
# --------------------------------------------------------------------------- #
# Grouped by family so a caller can weight them; `price_ask` is the strongest
# single signal a comment section can carry. Written against real uz/ru/en comment
# text, including the Cyrillic-Uzbek spellings, and matched case-insensitively on
# word boundaries so "narxi" does not fire inside an unrelated word.
_BUYER_PATTERNS: tuple[tuple[str, str, str], ...] = (
    # (family, language, regex)
    ("price_ask", "uz", r"\bnarx(i|lari|i\s*qancha)?\b"),
    ("price_ask", "uz", r"\bqancha(dan|ga)?\b"),
    ("price_ask", "uz", r"\bnech(cha|ta|pul)\b"),
    ("price_ask", "uz-cyr", r"\bнарх(и|лари)?\b"),
    ("price_ask", "uz-cyr", r"\bқанча\b"),
    ("price_ask", "ru", r"\bсколько\s+стоит\b"),
    ("price_ask", "ru", r"\bцена\b|\bпочём\b|\bпочем\b"),
    ("price_ask", "en", r"\bhow\s+much\b|\bprice\?|\bpricing\b|\bcost\?"),

    ("how_to_buy", "uz", r"\bqanday\s+(buyurtma|olsa|sotib)\b|\bbuyurtma\b"),
    ("how_to_buy", "ru", r"\bкак\s+(заказать|купить|оформить)\b|\bзаказ\b"),
    ("how_to_buy", "en", r"\bhow\s+(do\s+i|to)\s+(order|buy|get)\b"),

    ("availability", "uz", r"\bbormi\b|\bqoldimi\b|\bmavjudmi\b"),
    ("availability", "ru", r"\bесть\s+в\s+наличии\b|\bесть\?|\bостал(ось|ись)\b"),
    ("availability", "en", r"\bin\s+stock\b|\bavailable\?|\bstill\s+available\b"),

    ("contact_request", "uz", r"\baloqa\b|\btelefon\s*(raqam)?\b|\byozing\s*menga\b"),
    ("contact_request", "ru", r"\bнапишите\s+мне\b|\bваш\s+номер\b|\bконтакт\b"),
    ("contact_request", "en", r"\bdm\s+me\b|\bsend\s+me\s+(the\s+)?details\b"),
)

# --------------------------------------------------------------------------- #
# Supply side — a SELLER in the comments, not a buyer.
# --------------------------------------------------------------------------- #
# Scored separately and subtracted, because a comment section full of competing
# vendors looks superficially identical to one full of buyers: both are dense with
# prices and phone numbers. The discriminator is DIRECTION — an imperative CTA
# aimed at the reader, a volunteered price, a stock claim (Sheet #3 / Remedy B).
_SELLER_PATTERNS: tuple[tuple[str, str], ...] = (
    ("uz", r"\bbizda\s+bor\b|\bbizga\s+murojaat\b|\byozing\b(?!\s*menga)"),
    ("uz", r"\bdan\s+boshlab\b|\bchegirma\b|\barzon\s+narx"),
    ("ru", r"\bзаказывайте\b|\bобращайтесь\b|\bпишите\s+в\s+директ\b"),
    ("ru", r"\bот\s+\d[\d\s]*\s*(сум|сўм|у\.?е\.?|руб)\b|\bскидк"),
    ("en", r"\bdm\s+(us|for\s+price)\b|\bstarting\s+at\s+\$?\d|\border\s+now\b"),
)

# A volunteered contact detail is seller-shaped; a REQUESTED one is buyer-shaped.
# The buyer table above already captures the request form.
_CONTACT_OFFER_RE = re.compile(
    r"(\+?\d[\d\s\-()]{7,}\d)"                 # a phone number
    r"|(\bt\.me/\w+)|(\bwa\.me/\d+)", re.I)

_COMPILED_BUYER = tuple(
    (family, lang, re.compile(rx, re.I | re.UNICODE))
    for family, lang, rx in _BUYER_PATTERNS)
_COMPILED_SELLER = tuple(
    (lang, re.compile(rx, re.I | re.UNICODE)) for lang, rx in _SELLER_PATTERNS)


@dataclass
class CommentSignal:
    """What the pattern pass saw in one comment."""
    text: str
    buyer_families: list[str] = field(default_factory=list)
    seller: bool = False

    @property
    def is_buyer(self) -> bool:
        """Buyer wins only when nothing seller-shaped fired. A vendor quoting a
        price says "narx" too — the word alone proves nothing."""
        return bool(self.buyer_families) and not self.seller

    @property
    def asks_price(self) -> bool:
        return self.is_buyer and "price_ask" in self.buyer_families


def classify_comment(text: str) -> CommentSignal:
    """Pattern-match ONE comment. Pure, no I/O, no model call."""
    body = str(text or "")
    sig = CommentSignal(text=body)
    if not body.strip():
        return sig
    for family, _lang, rx in _COMPILED_BUYER:
        if rx.search(body) and family not in sig.buyer_families:
            sig.buyer_families.append(family)
    sig.seller = any(rx.search(body) for _lang, rx in _COMPILED_SELLER)
    # A volunteered phone/handle is seller-shaped ONLY when the comment is not
    # itself a question — "narxi? +998..." is a buyer leaving a callback number.
    if not sig.seller and _CONTACT_OFFER_RE.search(body) and "?" not in body:
        sig.seller = True
    return sig


@dataclass
class BuyerDensity:
    """The score for one candidate account's comment sections."""
    comments: int = 0
    buyers: int = 0
    price_asks: int = 0
    sellers: int = 0
    owner_replies: int = 0

    @property
    def price_asks_per_100(self) -> float:
        """THE headline number. Per 100 comments so accounts of different sizes
        are directly comparable."""
        return (self.price_asks / self.comments * 100) if self.comments else 0.0

    @property
    def buyer_share(self) -> float:
        return (self.buyers / self.comments) if self.comments else 0.0

    @property
    def seller_share(self) -> float:
        """A comment section that is mostly competing vendors is a bad seed even
        when it is dense with prices."""
        return (self.sellers / self.comments) if self.comments else 0.0

    @property
    def owner_reply_rate(self) -> float:
        """An owner who answers price questions is commerce-active; one who never
        does is running a content account."""
        return (self.owner_replies / self.comments) if self.comments else 0.0

    @property
    def score(self) -> float:
        """A single 0..1 ranking number.

        Computed in code from the counted criteria, never asked of a model — the
        same discipline Sheet #3 imposes on the match gate, for the same reason:
        a model-verbalized score collapses to a handful of values and is not
        reproducible. Weights are a starting point to be tuned against real
        outcomes once `source_stats` has enough per-seed lead history to tune on;
        they are NOT claimed to be optimal.
        """
        if not self.comments:
            return 0.0
        # Price asks saturate at 20 per 100 comments — an exceptional section.
        price = min(self.price_asks_per_100 / 20.0, 1.0)
        return max(0.0, min(1.0,
                            0.6 * price
                            + 0.25 * self.buyer_share
                            + 0.15 * self.owner_reply_rate
                            - 0.3 * self.seller_share))

    def as_dict(self) -> dict[str, Any]:
        return {"comments": self.comments, "buyers": self.buyers,
                "priceAsks": self.price_asks, "sellers": self.sellers,
                "priceAsksPer100": round(self.price_asks_per_100, 2),
                "buyerShare": round(self.buyer_share, 3),
                "sellerShare": round(self.seller_share, 3),
                "ownerReplyRate": round(self.owner_reply_rate, 3),
                "score": round(self.score, 3)}


def score_comments(comments: Iterable[Any], *,
                   owner: Optional[str] = None) -> BuyerDensity:
    """Score a sample of comments for one account.

    `comments` may be `core.feed.Comment` objects, dicts with `text`/`username`,
    or plain strings. `owner` is the account handle, so replies BY the owner are
    counted as owner engagement instead of as audience comments.
    """
    out = BuyerDensity()
    owner_norm = str(owner or "").strip().lstrip("@").lower()
    for item in comments:
        text, username = _text_and_user(item)
        if not str(text or "").strip():
            continue
        if owner_norm and str(username or "").strip().lstrip("@").lower() == owner_norm:
            out.owner_replies += 1
            continue          # the owner is not their own audience
        out.comments += 1
        sig = classify_comment(text)
        if sig.seller:
            out.sellers += 1
        if sig.is_buyer:
            out.buyers += 1
        if sig.asks_price:
            out.price_asks += 1
    return out


def _text_and_user(item: Any) -> tuple[str, str]:
    if isinstance(item, str):
        return item, ""
    if isinstance(item, dict):
        return str(item.get("text") or ""), str(item.get("username") or "")
    return (str(getattr(item, "text", "") or ""),
            str(getattr(item, "username", "") or ""))


def rank_candidates(scored: Sequence[tuple[str, BuyerDensity]],
                    min_comments: int = 20) -> list[tuple[str, BuyerDensity]]:
    """Rank scored candidates best-first, dropping under-sampled ones.

    `min_comments` is a real floor, not a formality: a 3-comment sample where one
    asks a price reads as 33 price-asks per 100, which is nonsense."""
    eligible = [(seed, d) for seed, d in scored if d.comments >= min_comments]
    return sorted(eligible, key=lambda sd: (-sd[1].score, sd[0]))
