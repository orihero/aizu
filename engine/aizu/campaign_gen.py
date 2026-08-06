"""AI-first campaign generation.

Turns any combination of a product URL, a product screenshot, and a free-text
description into a complete, launchable campaign DRAFT (the flat shape the panel's
create form consumes). Nothing here persists — the bridge endpoint returns the
draft for the user to review/edit/save through the normal `POST /api/campaign`.

Pipeline (`generate_campaign`):
  1. build_product_context — gather raw signal (each source best-effort):
       url       → fetch_rendered_text()  [Playwright-first, httpx fallback]
       image_b64 → caption_image()        [router.generate_json with a frame]
       text      → verbatim
  2. synthesize_brief — one router.generate_json call → a brief JSON object.
  3. assemble_draft   — coerce/default every field to the panel contract.
  4. buildability     — campaign_from_brief() must not raise; required defs present.
       on failure → retry synthesis once → else CampaignGenError.

Robustness is the whole point: the configured text model (`openrouter/owl-alpha`)
is flaky for structured output, so we tolerant-parse → coerce → validate → retry,
and NEVER let a bad model payload or a network hiccup crash the request. The URL
fetch takes a user-supplied URL, so it is SSRF-guarded on every path/redirect hop.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlsplit

from .core.config import SUPPORTED_PLATFORMS, campaign_from_brief
from .core.logsetup import get_logger
from .core.prompts import (CAMPAIGN_IMAGE_CAPTION, SYSTEM_CAMPAIGN_GEN,
                           SYSTEM_CAMPAIGN_INTERVIEW, SYSTEM_CAMPAIGN_PROMPTS)

log = get_logger(__name__)

try:  # httpx is a runtime dep; keep the import soft for offline/test use
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore

# Optional stronger model for THIS one-shot synthesis; defaults to the router's
# text model (owl-alpha) when unset. Mirrors the OPENROUTER_TEXT_MODEL precedent.
CAMPAIGN_GEN_MODEL = os.environ.get("CAMPAIGN_GEN_MODEL")

# Panel-form defaults the AI shouldn't decide (mirror admin-panel useCampaignForm).
_DEFAULT_BUDGET_CAP = 7500
_DEFAULT_GOAL_TARGET = 200
_DEFAULT_THRESHOLD = 0.7
_VALID_OBJECTIVES = ("lead", "traffic", "awareness")

# Conversational interview guards (mirror admin-panel MAX_INTERVIEW_ROUNDS).
MAX_INTERVIEW_ROUNDS = 3
_MAX_QUESTIONS_PER_ROUND = 4
_MAX_OPTIONS_PER_QUESTION = 8
_QUESTION_TYPES = ("single", "multi", "text", "platforms")
_MAX_CONTEXT_CHARS = 16_000

# Fetch guards.
_FETCH_TIMEOUT = 8.0
_NAV_TIMEOUT_MS = 12_000
_MAX_REDIRECTS = 3
_MAX_FETCH_BYTES = 2 * 1024 * 1024
_MAX_TEXT_CHARS = 6000
_ALLOWED_SCHEMES = ("http", "https")
_ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
_FETCH_HEADERS = {"User-Agent": "AIZU-CampaignGen/1.0", "Accept": "text/html,*/*"}


class CampaignGenError(RuntimeError):
    """A generation failure safe to show the user. `public` is the panel message;
    the full cause is logged, never returned over the wire."""

    def __init__(self, public: str, *, internal: str = "") -> None:
        super().__init__(internal or public)
        self.public = public


class UrlFetchError(CampaignGenError):
    """The URL could not be fetched (SSRF guard, network, bad content)."""


class _PlaywrightUnavailable(Exception):
    """Playwright/Chromium is not installed or failed to launch — fall back."""


# --------------------------------------------------------------------------- #
# SSRF guard
# --------------------------------------------------------------------------- #

Resolver = Callable[[str, Any], list]


def _ip_is_public(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _assert_url_safe(url: str, resolver: Resolver = socket.getaddrinfo) -> None:
    """Reject anything that isn't a public http(s) URL — blocks file:/data:/gopher:
    and any host resolving to a private/loopback/link-local/metadata address
    (re-run on every redirect hop, so an open redirect can't reach the intranet)."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UrlFetchError("only http(s) links are supported")
    host = parts.hostname
    if not host:
        raise UrlFetchError("the link has no host")
    try:
        infos = resolver(host, None)
    except OSError as e:
        raise UrlFetchError("could not resolve the link's host", internal=str(e))
    addrs = {info[4][0] for info in infos}
    if not addrs:
        raise UrlFetchError("could not resolve the link's host")
    for addr in addrs:
        if not _ip_is_public(addr):
            raise UrlFetchError("the link points to a non-public address")


# --------------------------------------------------------------------------- #
# HTML → text (stdlib, no extra dependency)
# --------------------------------------------------------------------------- #

class _HTMLTextExtractor(HTMLParser):
    """Collect visible text; drop script/style/noscript; capture <title> and a
    meta/og description as priority signal. Tolerant — never raises on bad markup."""

    _SKIP = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self.title = ""
        self.meta_description = ""
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta" and not self.meta_description:
            a = {k.lower(): (v or "") for k, v in attrs}
            name = (a.get("name") or a.get("property") or "").lower()
            if name in ("description", "og:description", "og:title") and a.get("content"):
                self.meta_description = a["content"].strip()

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        else:
            text = data.strip()
            if text:
                self._chunks.append(text)

    def to_text(self) -> tuple[str, str]:
        title = re.sub(r"\s+", " ", self.title).strip()
        body = re.sub(r"\s+", " ", " ".join(self._chunks)).strip()
        lead = self.meta_description.strip()
        combined = "\n".join(p for p in (lead, body) if p)[:_MAX_TEXT_CHARS]
        return title, combined


def _html_to_text(html: str) -> tuple[str, str]:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(html)
    except Exception as e:  # noqa: BLE001 — malformed markup, salvage what we have
        log.debug("HTML parse warning: %s", e)
    return parser.to_text()


# --------------------------------------------------------------------------- #
# URL fetch: Playwright-first, httpx fallback (both SSRF-guarded)
# --------------------------------------------------------------------------- #

def fetch_rendered_text(url: str, resolver: Resolver = socket.getaddrinfo) -> tuple[str, str]:
    """(title, text) for a product URL. Tries a headless browser (so JS-rendered
    pages yield content); falls back to a plain fetch when no chromium is installed
    or rendering fails. Raises UrlFetchError on an SSRF violation (never falls back
    to fetching a blocked target)."""
    _assert_url_safe(url, resolver)
    try:
        return _fetch_via_playwright(url, resolver)
    except UrlFetchError:
        raise  # SSRF on a redirect/sub-resource — do NOT fall back
    except _PlaywrightUnavailable as e:
        log.info("playwright unavailable (%s); using httpx fetch", e)
    except Exception as e:  # noqa: BLE001 — nav/render failure → simple fetch
        log.info("playwright fetch failed (%s); using httpx fetch", e)
    return _fetch_via_httpx(url, resolver)


def _fetch_via_playwright(url: str, resolver: Resolver) -> tuple[str, str]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001 — not installed
        raise _PlaywrightUnavailable(str(e))
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001 — browser binary missing
            raise _PlaywrightUnavailable(str(e))
        try:
            page = browser.new_context().new_page()

            def _guard(route: Any) -> None:
                try:
                    _assert_url_safe(route.request.url, resolver)
                    route.continue_()
                except Exception:  # noqa: BLE001 — block private/odd sub-resources
                    route.abort()

            page.route("**/*", _guard)
            page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
            page.wait_for_timeout(1200)  # let client-side rendering settle
            _assert_url_safe(page.url, resolver)  # re-check the post-redirect URL
            title = page.title() or ""
            body = page.inner_text("body") or ""
        finally:
            browser.close()
    body = re.sub(r"\s+", " ", body).strip()[:_MAX_TEXT_CHARS]
    return re.sub(r"\s+", " ", title).strip(), body


def _open_httpx_client():  # pragma: no cover - thin factory, overridden in tests
    return httpx.Client(follow_redirects=False, timeout=_FETCH_TIMEOUT,
                        headers=_FETCH_HEADERS)


def _fetch_via_httpx(url: str, resolver: Resolver, *, open_client=None) -> tuple[str, str]:
    if httpx is None:
        raise UrlFetchError("link fetching is unavailable")
    open_client = open_client or _open_httpx_client
    client = open_client()
    current = url
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            _assert_url_safe(current, resolver)  # re-validate every hop
            with client.stream("GET", current) as resp:
                if resp.is_redirect:
                    loc = resp.headers.get("location")
                    if not loc:
                        raise UrlFetchError("redirect without a location")
                    current = urljoin(current, loc)
                    continue
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype and ctype not in _ALLOWED_CONTENT_TYPES:
                    raise UrlFetchError("the link is not a web page")
                raw = _read_capped(resp.iter_bytes())
                return _html_to_text(raw.decode(resp.encoding or "utf-8", "replace"))
        raise UrlFetchError("too many redirects")
    finally:
        client.close()


def _read_capped(chunks) -> bytes:
    out = bytearray()
    for chunk in chunks:
        out += chunk
        if len(out) >= _MAX_FETCH_BYTES:
            return bytes(out[:_MAX_FETCH_BYTES])
    return bytes(out)


# --------------------------------------------------------------------------- #
# Product context + image captioning
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ProductContext:
    description: str = ""
    url: str = ""
    page_title: str = ""
    page_text: str = ""
    image_caption: str = ""
    notes: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.description or self.page_text or self.page_title
                    or self.image_caption)


def caption_image(image_b64: str, router: Any) -> str:
    """Describe a product screenshot via the vision model. Best-effort: '' on failure."""
    raw = router.generate_json(
        system=CAMPAIGN_IMAGE_CAPTION,
        user="Describe this product/landing screenshot. Output ONLY the JSON object.",
        images_b64=[image_b64], model=router.vision_model, stage="campaign_gen_vision")
    cap = raw.get("caption") if isinstance(raw, dict) else ""
    return str(cap).strip() if cap else ""


def build_product_context(*, url: Optional[str], image_b64: Optional[str],
                          text: Optional[str], router: Any,
                          resolver: Resolver = socket.getaddrinfo) -> ProductContext:
    notes: list[str] = []
    page_title = page_text = caption = ""
    if url:
        try:
            page_title, page_text = fetch_rendered_text(url, resolver)
        except UrlFetchError as e:
            notes.append(f"could not read the link: {e.public}")
        except Exception as e:  # noqa: BLE001
            notes.append("could not read the link")
            log.info("url fetch error: %s", e)
    if image_b64:
        try:
            caption = caption_image(image_b64, router)
            if not caption:
                notes.append("could not read the screenshot")
        except Exception as e:  # noqa: BLE001
            notes.append("could not read the screenshot")
            log.info("image caption error: %s", e)
    return ProductContext(description=(text or "").strip(), url=url or "",
                          page_title=page_title, page_text=page_text,
                          image_caption=caption, notes=notes)


# --------------------------------------------------------------------------- #
# Synthesis + coercion
# --------------------------------------------------------------------------- #

_SKELETON = (
    '{"name":"","platform":"instagram","objective":"lead","relevanceDef":"",'
    '"matchDef":"","extractDef":"- field — gloss","seedHashtags":[],'
    '"seedAccounts":[],"languageMix":[],"threshold":0.7}')


def _context_body(ctx: ProductContext) -> str:
    """The PRODUCT CONTEXT blocks shared by the interview and synthesis prompts.
    Also the round-trip serialization the panel carries between interview rounds
    (so we fetch the URL / caption the screenshot exactly once)."""
    blocks: list[str] = []
    if ctx.description:
        blocks.append(f"PRODUCT DESCRIPTION:\n{ctx.description}")
    if ctx.page_title or ctx.page_text:
        blocks.append("PRODUCT PAGE:\n"
                      + "\n".join(p for p in (ctx.page_title, ctx.page_text) if p))
    if ctx.image_caption:
        blocks.append(f"PRODUCT SCREENSHOT:\n{ctx.image_caption}")
    return "\n\n".join(blocks)


def serialize_context(ctx: ProductContext) -> str:
    """Flatten a built ProductContext to the text the panel echoes back on later
    interview rounds (and the final synthesis), avoiding a repeat URL/image fetch."""
    return _context_body(ctx)[:_MAX_CONTEXT_CHARS]


def _interview_block(interview: Optional[list[dict[str, Any]]]) -> str:
    """Render the gathered Q&A transcript for the prompt (weighted heavily)."""
    pairs = []
    for qa in interview or []:
        q = _str(qa.get("question") if isinstance(qa, dict) else "")
        a = _str(qa.get("answer") if isinstance(qa, dict) else "")
        if q and a:
            pairs.append(f"Q: {q}\nA: {a}")
    if not pairs:
        return ""
    return ("CLIENT INTERVIEW (their own answers — weight these heavily):\n"
            + "\n\n".join(pairs))


def _synthesis_user(context_text: str, *, strict: bool = False,
                    interview: Optional[list[dict[str, Any]]] = None) -> str:
    blocks = [b for b in (context_text, _interview_block(interview)) if b]
    head = ("Your previous answer was unusable. Return STRICTLY one JSON object "
            "with exactly these keys and no prose.\n\n") if strict else ""
    return (f"{head}PRODUCT CONTEXT\n===============\n" + "\n\n".join(blocks)
            + "\n\nReturn ONE JSON object with EXACTLY this shape (fill every key):\n"
            + _SKELETON)


def synthesize_brief(context_text: str, router: Any, *, model: Optional[str],
                     strict: bool = False,
                     interview: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    return router.generate_json(
        system=SYSTEM_CAMPAIGN_GEN,
        user=_synthesis_user(context_text, strict=strict, interview=interview),
        model=model)


def _str(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _to_comma(value: Any) -> str:
    """A model list OR comma string → a clean comma string (the form's text shape)."""
    if isinstance(value, (list, tuple)):
        return ", ".join(_str(x) for x in value if _str(x))
    return _str(value)


def _split(value: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,\n]", value) if p.strip()]


def _coerce_extract_def(value: Any) -> str:
    """Normalize the extract field list to markdown bullets so parse_extract_fields
    yields clean keys (a bare line or list item becomes `- item`)."""
    if isinstance(value, (list, tuple)):
        items = [_str(x) for x in value if _str(x)]
    else:
        items = [ln.strip() for ln in _str(value).splitlines() if ln.strip()]
    bulleted = [ln if re.match(r"^\s*(?:[-*•]|\d+[.)])\s+", ln) else f"- {ln}"
                for ln in items]
    return "\n".join(bulleted)


# The engine finds BUYERS; a commenter advertising their own product/service is
# supply-side noise, not a lead. The generator is instructed to bake this into
# matchDef (SYSTEM_CAMPAIGN_GEN), but the model is flaky — so we guarantee it at
# the boundary: a lead campaign's matchDef ALWAYS carries the exclusion. Markers
# are exclusion-specific (never the demand-side word "seller"/"selling", which
# legitimately appears in "asks the seller for the price") so we don't skip a
# matchDef that merely mentions the seller without excluding them.
_SUPPLY_SIDE_CLAUSE = (
    "Exclude the SUPPLY side: a commenter who is OFFERING or SELLING (their own "
    "product or service, or a reseller/affiliate advertising what they sell) is "
    "NOT a lead, even if they leave a phone number or post a full pitch."
)
_SUPPLY_SIDE_MARKERS = ("supply", "not a lead", "offering or selling", "exclude")


def _ensure_supply_side_exclusion(match_def: str) -> str:
    """Guarantee a lead campaign's matchDef excludes supply-side sellers.

    No-op when matchDef is empty (the draft is then flagged unusable and the
    model is retried) or already states the exclusion; otherwise appends the
    canonical clause. Redundant appends are harmless; the risk we avoid is a
    matchDef that silently qualifies sellers, so detection stays conservative."""
    md = match_def.strip()
    if not md:
        return md
    low = md.lower()
    if any(marker in low for marker in _SUPPLY_SIDE_MARKERS):
        return md
    sep = " " if md.endswith((".", "!", "?")) else ". "
    return f"{md}{sep}{_SUPPLY_SIDE_CLAUSE}"


def _name_from_context(ctx: ProductContext) -> str:
    if ctx.page_title:
        return ctx.page_title[:80]
    if ctx.url:
        host = urlsplit(ctx.url).hostname or ""
        if host:
            return host[4:] if host.startswith("www.") else host
    if ctx.description:
        return " ".join(ctx.description.split()[:6])[:80]
    return "New campaign"


def assemble_draft(raw: dict[str, Any], ctx: ProductContext,
                   platforms: Optional[list[str]] = None) -> dict[str, Any]:
    """Coerce a (possibly ragged) model dict into the flat panel draft contract.
    Every field is defended: wrong type → default, never a raise.

    `platforms` (the platforms the client chose in the interview) overrides the
    model's single guess: the first becomes the primary `platform`, and when 2+ are
    chosen the draft also carries a `channels[]` array (matching CampaignFormState),
    with the model's seeds attached to the primary channel."""
    raw = raw if isinstance(raw, dict) else {}
    objective = _str(raw.get("objective")).lower()
    if objective not in _VALID_OBJECTIVES:
        objective = "lead"
    chosen = _valid_platforms(platforms) if platforms else []
    platform = chosen[0] if chosen else _str(raw.get("platform")).lower()
    if platform not in SUPPORTED_PLATFORMS:
        platform = "instagram"
    try:
        threshold = float(raw.get("threshold", _DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        threshold = _DEFAULT_THRESHOLD
    threshold = max(0.0, min(1.0, threshold))
    name = _str(raw.get("name") or raw.get("displayName")) or _name_from_context(ctx)
    match_def = _str(raw.get("matchDef"))
    if objective == "lead":
        match_def = _ensure_supply_side_exclusion(match_def)
    seed_hashtags = _to_comma(raw.get("seedHashtags"))
    seed_accounts = _to_comma(raw.get("seedAccounts"))
    seed_channels = _to_comma(raw.get("seedChannels")) if platform == "telegram" else ""
    draft = {
        "name": name,
        "objective": objective,
        "platform": platform,
        "threshold": threshold,
        "budgetCap": _DEFAULT_BUDGET_CAP,
        "goalTarget": _DEFAULT_GOAL_TARGET,
        "languages": _to_comma(raw.get("languageMix") or raw.get("languages")),
        "relevanceDef": _str(raw.get("relevanceDef")),
        "matchDef": match_def,
        "extractDef": _coerce_extract_def(raw.get("extractDef")),
        "relevancePrompt": "",
        "matchPrompt": "",
        "visionPrompt": "",
        "seedHashtags": seed_hashtags,
        "seedAccounts": seed_accounts,
        "seedChannels": seed_channels,
    }
    if len(chosen) >= 2:
        draft["channels"] = [
            {"platform": p,
             "seedHashtags": seed_hashtags if i == 0 else "",
             "seedAccounts": seed_accounts if i == 0 else "",
             "seedChannels": seed_channels if i == 0 else ""}
            for i, p in enumerate(chosen)]
    return draft


def _valid_platforms(value: Any) -> list[str]:
    """Filter a raw value to a de-duplicated list of supported platform slugs."""
    items = value if isinstance(value, (list, tuple)) else _split(_str(value))
    out: list[str] = []
    for item in items:
        slug = _str(item).lower()
        if slug in SUPPORTED_PLATFORMS and slug not in out:
            out.append(slug)
    return out


# --------------------------------------------------------------------------- #
# Conversational interview (multi-round clarifying questions before synthesis)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class InterviewResult:
    """One round's outcome. `product_context` is the serialized signal the panel
    echoes back on the next round so the URL/screenshot is read only once."""
    done: bool
    questions: list[dict[str, Any]]
    product_context: str


def _default_platforms_question() -> dict[str, Any]:
    return {"id": "platforms", "type": "platforms",
            "prompt": "Which platforms should we search for leads?",
            "help": "We suggest the ones that fit your product; add any others you want.",
            "suggested": ["instagram"]}


def _coerce_option(raw: Any) -> Optional[dict[str, str]]:
    if isinstance(raw, dict):
        value = _str(raw.get("value") or raw.get("label"))
        label = _str(raw.get("label") or raw.get("value"))
        hint = _str(raw.get("hint"))
    else:
        value = label = _str(raw)
        hint = ""
    if not value:
        return None
    opt = {"value": value, "label": label or value}
    if hint:
        opt["hint"] = hint
    return opt


def _coerce_question(raw: Any, idx: int) -> Optional[dict[str, Any]]:
    """One model question → the panel contract, or None if unusable. A choice
    question with no valid options is dropped (the UI can't render it)."""
    if not isinstance(raw, dict):
        return None
    prompt = _str(raw.get("prompt") or raw.get("question") or raw.get("text"))
    if not prompt:
        return None
    qtype = _str(raw.get("type")).lower()
    if qtype not in _QUESTION_TYPES:
        qtype = "text"
    out: dict[str, Any] = {"id": _str(raw.get("id")) or f"q{idx}",
                           "type": qtype, "prompt": prompt}
    help_text = _str(raw.get("help"))
    if help_text:
        out["help"] = help_text
    if qtype in ("single", "multi"):
        opts = [o for o in (_coerce_option(x) for x in (raw.get("options") or []))
                if o][:_MAX_OPTIONS_PER_QUESTION]
        if not opts:
            return None
        out["options"] = opts
        if raw.get("allowCustom"):
            out["allowCustom"] = True
    elif qtype == "platforms":
        out["suggested"] = _valid_platforms(raw.get("suggested")) or ["instagram"]
    else:  # text
        placeholder = _str(raw.get("placeholder"))
        if placeholder:
            out["placeholder"] = placeholder
    return out


def _dedupe_platforms(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep at most one platforms question (the first); drop later duplicates."""
    seen = False
    kept: list[dict[str, Any]] = []
    for q in questions:
        if q["type"] == "platforms":
            if seen:
                continue
            seen = True
        kept.append(q)
    return kept


def _normalize_prompt(text: str) -> str:
    """Whitespace/case-fold a prompt so near-identical repeats compare equal."""
    return re.sub(r"\s+", " ", _str(text)).strip().lower()


def assemble_interview(raw: Any, *, round: int,
                       asked: tuple[str, ...] = ()) -> tuple[bool, list[dict[str, Any]]]:
    """Coerce a ragged interview reply into (done, questions). Malformed questions
    are dropped; questions already asked in a prior round are dropped (the flaky
    model loves to repeat itself); the count is clamped; round 1 always carries a
    platforms question (injected if the model omitted it). done=True returns no
    questions. When dedup leaves nothing, the caller treats it as converged."""
    raw = raw if isinstance(raw, dict) else {}
    if bool(raw.get("done")):
        return True, []
    asked_set = {_normalize_prompt(a) for a in asked}
    questions: list[dict[str, Any]] = []
    seen_this_round: set[str] = set()
    for i, q in enumerate(raw.get("questions") or []):
        cq = _coerce_question(q, i)
        if not cq:
            continue
        norm = _normalize_prompt(cq["prompt"])
        if norm in asked_set or norm in seen_this_round:
            continue  # already asked before, or duplicated within this round
        seen_this_round.add(norm)
        questions.append(cq)
    questions = _dedupe_platforms(questions)
    if round <= 1 and not any(q["type"] == "platforms" for q in questions):
        questions = [_default_platforms_question()] + questions
    return False, questions[:_MAX_QUESTIONS_PER_ROUND]


def _asked_block(interview: Optional[list[dict[str, Any]]]) -> str:
    """An explicit do-not-repeat list, reinforcing the prompt against a model that
    re-asks the same questions every round."""
    asked = [_str(qa.get("question")) for qa in (interview or [])
             if isinstance(qa, dict) and _str(qa.get("question"))]
    if not asked:
        return ""
    return ("ALREADY ASKED — do NOT ask any of these again; ask something NEW or "
            'set "done":true if you have enough:\n' + "\n".join(f"- {q}" for q in asked))


def _interview_call(context_text: str, interview: Optional[list[dict[str, Any]]],
                    router: Any, *, model: Optional[str], strict: bool) -> Any:
    head = ("Your previous answer was unusable. Return STRICTLY one JSON object "
            '{"done":bool,"questions":[...]} and no prose.\n\n') if strict else ""
    blocks = [b for b in (context_text, _interview_block(interview),
                          _asked_block(interview)) if b]
    user = (f"{head}PRODUCT CONTEXT\n===============\n" + "\n\n".join(blocks)
            + '\n\nReturn ONE JSON object: {"done":bool,"questions":[...]}.')
    return router.generate_json(system=SYSTEM_CAMPAIGN_INTERVIEW, user=user, model=model)


def _interview_reply_is_empty(raw: Any) -> bool:
    """True when the model contributed NOTHING usable: `generate_json` returns `{}`
    on a hard degrade (dead/removed model → 404, transport failure, malformed 200,
    spend cap) or when the reply carried no JSON object. A genuine interview reply
    always carries a "done" flag or a "questions" list, so this cleanly separates
    "the AI is down" from "the AI answered but had nothing new to ask"."""
    return not (isinstance(raw, dict) and ("done" in raw or raw.get("questions")))


def run_interview(*, url: Optional[str] = None, image_b64: Optional[str] = None,
                  text: Optional[str] = None, product_context: Optional[str] = None,
                  interview: Optional[list[dict[str, Any]]] = None, router: Any,
                  round: int = 1,
                  resolver: Resolver = socket.getaddrinfo) -> InterviewResult:
    """Produce one round of clarifying questions (or done=True). Builds the product
    context on the first round and echoes it back so later rounds skip the fetch.
    Caps at MAX_INTERVIEW_ROUNDS and never dead-ends: an unusable reply (after one
    strict retry) returns done=True so the wizard can proceed to synthesis."""
    if round > MAX_INTERVIEW_ROUNDS:
        return InterviewResult(done=True, questions=[],
                               product_context=(product_context or "").strip())
    if product_context is not None:
        context_text = product_context.strip()
        is_empty = not context_text
    else:
        ctx = build_product_context(url=url, image_b64=image_b64, text=text,
                                    router=router, resolver=resolver)
        context_text = serialize_context(ctx)
        is_empty = ctx.is_empty()
    if is_empty and not interview:
        raise CampaignGenError(
            "We couldn't read any of your inputs. Add a description, a working "
            "link, or a clearer screenshot.", internal="empty interview context")
    model = CAMPAIGN_GEN_MODEL or None
    asked = tuple(_str(qa.get("question")) for qa in (interview or [])
                  if isinstance(qa, dict) and _str(qa.get("question")))
    raw = _interview_call(context_text, interview, router, model=model, strict=False)
    done, questions = assemble_interview(raw, round=round, asked=asked)
    # One strict retry when the model gave us nothing usable — either an empty/
    # degraded reply, or a reply whose questions were all dropped as junk/dupes.
    if _interview_reply_is_empty(raw) or (not done and not questions):
        raw = _interview_call(context_text, interview, router, model=model, strict=True)
        done, questions = assemble_interview(raw, round=round, asked=asked)
    # A still-empty reply on the FIRST round means the MODEL itself is unavailable
    # (removed model, 404, transport failure, spend cap). Don't fake a platform-only
    # round — surface it so the operator fixes the model. On later rounds the user has
    # already invested answers, so we converge to synthesis instead of dead-ending.
    if round <= 1 and _interview_reply_is_empty(raw) and not interview:
        raise CampaignGenError(
            "The AI model is temporarily unavailable, so we couldn't draft the "
            "interview questions. Please check the campaign-generation model and "
            "try again in a moment.",
            internal="interview: empty model reply (router degraded or unparseable)")
    if not done and not questions:
        done = True  # model responded but nothing new to ask → converge to synthesis
    return InterviewResult(done=done, questions=questions, product_context=context_text)


def _draft_is_usable(draft: dict[str, Any]) -> bool:
    """A draft is usable when it has a name + the two prose definitions the engine
    needs, AND builds into a valid Campaign (platform/band/threshold)."""
    if not (draft["name"] and draft["relevanceDef"] and draft["matchDef"]):
        return False
    snake = {
        "platform": draft["platform"], "goal": draft["objective"],
        "threshold": draft["threshold"],
        "language_mix": _split(draft["languages"]),
        "relevance_def": draft["relevanceDef"], "match_def": draft["matchDef"],
        "extract_def": draft["extractDef"],
        "seed_hashtags": _split(draft["seedHashtags"]),
        "seed_accounts": _split(draft["seedAccounts"]),
        "seed_channels": _split(draft["seedChannels"]),
    }
    try:
        campaign_from_brief("draft-preview", snake)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# Advanced classifier prompts (tuned per-campaign system prompts)
# --------------------------------------------------------------------------- #

_PROMPT_KEYS = ("relevancePrompt", "matchPrompt", "visionPrompt")
_PROMPT_MIN_LEN = 80


def _prompt_carries_contract(text: str) -> bool:
    """A generated classifier prompt is usable only if it keeps the strict JSON
    output contract the engine relies on — otherwise we drop it and the engine
    uses its tuned generic prompt instead (never break classification)."""
    low = text.lower()
    return (len(text.strip()) >= _PROMPT_MIN_LEN and "json" in low
            and '"score"' in text and '"label"' in text)


def _advanced_prompts_user(draft: dict[str, Any]) -> str:
    return ("CAMPAIGN\n========\n"
            f"Platform: {draft.get('platform', 'instagram')}\n"
            f"Objective: {draft.get('objective', 'lead')}\n\n"
            f"RELEVANCE DEFINITION:\n{draft.get('relevanceDef', '')}\n\n"
            f"MATCH DEFINITION:\n{draft.get('matchDef', '')}\n\n"
            f"EXTRACT FIELDS:\n{draft.get('extractDef', '')}\n\n"
            'Return ONE JSON object: '
            '{"relevancePrompt":"","matchPrompt":"","visionPrompt":""}')


def generate_advanced_prompts(draft: dict[str, Any], router: Any, *,
                              model: Optional[str]) -> dict[str, str]:
    """Best-effort: write the campaign's three tuned classifier system prompts from
    its brief. Any prompt missing the required JSON output contract is dropped (left
    unset → the engine's generic prompt is used), so a flaky model can enrich the
    draft but never degrade or break the engine. Never raises."""
    try:
        raw = router.generate_json(system=SYSTEM_CAMPAIGN_PROMPTS,
                                   user=_advanced_prompts_user(draft), model=model,
                                   stage="campaign_gen_prompts")
    except Exception as e:  # noqa: BLE001 — enrichment is optional, never fatal
        log.info("advanced prompt generation failed: %s", e)
        return {}
    raw = raw if isinstance(raw, dict) else {}
    return {key: _str(raw.get(key)) for key in _PROMPT_KEYS
            if _prompt_carries_contract(_str(raw.get(key)))}


_CONTEXT_LABEL_RE = re.compile(
    r"(?im)^(PRODUCT DESCRIPTION|PRODUCT PAGE|PRODUCT SCREENSHOT):\s*")


def _fallback_topic(context_text: str, interview: Optional[list[dict[str, Any]]]) -> str:
    """A short topic phrase from the product context (labels stripped) or, failing
    that, the interview answers — used to fill missing defs so an invested wizard
    never dead-ends on a flaky model."""
    text = re.sub(r"\s+", " ", _CONTEXT_LABEL_RE.sub("", context_text or "")).strip()
    if not text and interview:
        text = " ".join(_str(qa.get("answer")) for qa in interview
                        if isinstance(qa, dict) and _str(qa.get("answer")))
    return text[:160].strip()


def _ensure_minimum_defs(draft: dict[str, Any], context_text: str,
                         interview: Optional[list[dict[str, Any]]]) -> dict[str, Any]:
    """Fill any def the model left blank from the gathered signal, so a draft built
    from a real interview is always usable (the user reviews/edits it next). The
    prose is deliberately generic — a starting point the user tightens in the form."""
    topic = _fallback_topic(context_text, interview) or "this product or service"
    out = dict(draft)
    if not out.get("name"):
        out["name"] = topic[:60] or "New campaign"
    if not out.get("relevanceDef"):
        out["relevanceDef"] = f"Public posts, videos, and discussions about {topic}."
    if not out.get("matchDef"):
        match_def = (f"A commenter who shows clear intent to buy, hire, subscribe to, "
                     f"or ask about pricing/availability for {topic}.")
        if out.get("objective") == "lead":
            match_def = _ensure_supply_side_exclusion(match_def)
        out["matchDef"] = match_def
    if not out.get("extractDef"):
        out["extractDef"] = "- contact — how to reach them\n- intent — what they want"
    return out


def generate_campaign(*, url: Optional[str] = None, image_b64: Optional[str] = None,
                      text: Optional[str] = None, router: Any,
                      config_dir: str | Path = "config",
                      campaign_id_hint: Optional[str] = None,
                      product_context: Optional[str] = None,
                      interview: Optional[list[dict[str, Any]]] = None,
                      platforms: Optional[list[str]] = None) -> dict[str, Any]:
    """Produce a flat campaign draft (Partial<CampaignFormState>) from any mix of
    url/image/text — optionally refined by a conversational `interview` transcript
    and the `platforms` the client chose. When `product_context` (a pre-built,
    serialized context) is supplied, the URL/screenshot are NOT re-fetched. Raises
    CampaignGenError (panel-safe) when no usable campaign can be drafted; never lets
    a model/network failure escape as a bare exception."""
    if product_context is not None:
        context_text = product_context.strip()
        ctx = ProductContext(description=context_text)
        is_empty = not context_text
        notes: list[str] = []
    else:
        ctx = build_product_context(url=url, image_b64=image_b64, text=text,
                                    router=router)
        context_text = serialize_context(ctx)
        is_empty = ctx.is_empty()
        notes = ctx.notes
    if is_empty and not interview:
        raise CampaignGenError(
            "We couldn't read any of your inputs. Add a description, a working "
            "link, or a clearer screenshot.",
            internal=f"empty context; notes={notes}")
    model = CAMPAIGN_GEN_MODEL or None
    draft = assemble_draft(
        synthesize_brief(context_text, router, model=model, interview=interview),
        ctx, platforms)
    if not _draft_is_usable(draft):  # one retry with a stricter nudge
        draft = assemble_draft(
            synthesize_brief(context_text, router, model=model, strict=True,
                             interview=interview), ctx, platforms)
    if not _draft_is_usable(draft) and (interview or platforms):
        # The user invested in the interview — don't dead-end on a flaky model.
        # Fill the missing defs from their answers; they review/edit next.
        draft = _ensure_minimum_defs(draft, context_text, interview)
    if not _draft_is_usable(draft):
        raise CampaignGenError(
            "We couldn't turn that into a campaign draft. Try adding a short "
            "description or a clearer screenshot.",
            internal="model output not usable after retry")
    if campaign_id_hint:
        draft = {**draft, "name": draft["name"] or campaign_id_hint}
    # Enrich with tuned classifier prompts (best-effort; blanks stay blank → the
    # engine's generic prompt is used). Never fails the draft.
    prompts = generate_advanced_prompts(draft, router, model=model)
    if prompts:
        draft = {**draft, **prompts}
    log.info("Campaign draft generated · platform=%s objective=%s prompts=%d notes=%d",
             draft["platform"], draft["objective"], len(prompts), len(notes))
    return draft
