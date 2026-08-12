"""Config loaders for soul.md and campaign.md.

Both files are read ONCE at session start (PRD §3, §4). soul.md is domain-free
identity + safety; campaign.md carries every domain decision. The campaign file
embeds a small YAML front block (between ```yaml fences) for machine-readable
knobs, plus free-text prose the model router passes to the LLM verbatim.

No third-party YAML dependency — we parse the tiny known subset ourselves so the
engine has zero extra install surface for config.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Platforms the engine knows how to run against (multi-platform plan Part B).
# Instagram, YouTube, Telegram, Reddit, LinkedIn and X are live; the remaining
# siblings (Threads, Quora) have PRDs (docs/prd/) and are added one at a time.
# Each maps onto a FeedSource selected by `dispatch.build_feed`.
SUPPORTED_PLATFORMS = ("instagram", "youtube", "telegram", "reddit",
                       "linkedin", "x")

# The subset driven through the warmed-Chrome / CDP browser rather than a platform
# API. Two things hang off this split: a genuine account-level halt on one of these
# poisons the SHARED browser session (cli._POISON_HALT_KINDS), and only a run that
# touches one of them needs the agent-readiness gate — an API-only campaign runs
# fine with no browser anywhere. Mirrored by the panel's CDP_PLATFORMS
# (admin-panel/src/features/campaigns/CampaignForm.tsx); keep the two in lockstep.
CDP_PLATFORMS = frozenset({"instagram", "linkedin", "x"})

# Platforms whose creds are connected per-org in the panel (encrypted secret
# store, Store.get_integration_secret) rather than read from worker-box env.
# Instagram (and the other CDP_PLATFORMS) stay single-tenant (warmed-Chrome /
# CDP from .env), so they have no per-org secret and resolve to env instead.
# Lives here (not cli.py) so server._dispatch_run_to_fleet can bake the org's
# secret into the job spec at enqueue time (mirrors campaign_brief/soul_text,
# BUILD-PLAN C5/B4) without importing the CLI module.
PER_ORG_CREDENTIAL_PLATFORMS = frozenset({"youtube", "telegram", "reddit"})

# How a run drives the account (warming PRD §4.2). 'harvest' is the read-only
# lead-discovery loop (every existing engine); 'warming' ramps/sustains a managed
# account via the warming engine. Distinct from `mode` (dry/live) on purpose.
VALID_ENGINE_MODES = ("harvest", "warming")


def _coerce(value: str) -> Any:
    v = value.strip()
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [_coerce(x) for x in inner.split(",")]
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d*\.\d+", v):
        return float(v)
    if v.lower() in {"true", "false"}:
        return v.lower() == "true"
    return v.strip("'\"")


def _parse_yaml_block(text: str) -> dict[str, Any]:
    """Parse the first ```yaml ... ``` fenced block: flat key: value pairs only."""
    m = re.search(r"```ya?ml\s*\n(.*?)\n```", text, re.DOTALL)
    if not m:
        return {}
    out: dict[str, Any] = {}
    for line in m.group(1).splitlines():
        line = line.split("#", 1)[0].rstrip()
        if not line.strip() or ":" not in line:
            continue
        key, _, val = line.partition(":")
        out[key.strip()] = _coerce(val)
    return out


def _section(text: str, heading: str) -> str:
    """Return the prose body under a `## heading` until the next `## `."""
    pat = re.compile(rf"^##\s+{re.escape(heading)}.*?$\n(.*?)(?=^##\s|\Z)",
                     re.DOTALL | re.MULTILINE | re.IGNORECASE)
    m = pat.search(text)
    return m.group(1).strip() if m else ""


# The "Extract" section is prose, but the engine needs the machine KEYS it
# implies — those become the required keys of the model's `extracted` object
# (cascade injects them as the output contract; the panel shows them as chips).
# Cap the count so a runaway brief can't bloat every match prompt.
_MAX_EXTRACT_FIELDS = 24
# A field bullet is `key — description` / `key: description` / `key (note)`;
# everything from the first such separator on is the human gloss. NB: a plain
# hyphen is NOT a separator (it's a valid word-joiner, and `\s-\s` truncates
# names like "sign - up date"); use an em/en dash or a colon to add a gloss.
_FIELD_GLOSS = re.compile(r"\s*(?:—|–|:|\().*$", re.DOTALL)
# Markdown bullet markers: -, *, • and ordered `1.` / `1)` lists.
_BULLET_LINE = re.compile(r"^[ \t]*(?:[-*•]|\d+[.)])\s+(.*\S)\s*$", re.MULTILINE)


def _slug_field(raw: str) -> str:
    """One brief line → a field key, or '' if there's no key in it.

    Unicode-aware: diacritics are folded (``téléphone`` → ``telephone``) and
    non-Latin letters are kept (``телефон`` stays ``телефон``) rather than being
    treated as separators and silently dropped — the engine targets mixed
    uz/ru/en briefs, so a Cyrillic or accented field name must survive."""
    raw = raw.strip()
    backticked = re.match(r"`([^`]+)`", raw)            # prefer an explicit `key`
    token = backticked.group(1) if backticked else _FIELD_GLOSS.sub("", raw)
    # Fold combining accents (NFKD then drop the marks): café → cafe, naïve → naive.
    token = "".join(c for c in unicodedata.normalize("NFKD", token.lower())
                    if not unicodedata.combining(c))
    # Keep Unicode letters/digits; collapse everything else to a single underscore.
    return re.sub(r"[^\w]+", "_", token, flags=re.UNICODE).strip("_")


def _fallback_items(extract_def: str) -> list[str]:
    """Field items when there are no markdown bullets. Each non-blank line is one
    field — EXCEPT a line that's a comma list of single-word tokens (``phone,
    email``), which splits into one field per token. A prose line with spaced
    phrases (``full mailing address, including street, …``) stays one field, so a
    description never fragments into phantom keys."""
    items: list[str] = []
    for line in extract_def.splitlines():
        line = line.strip()
        if not line:
            continue
        pieces = [p.strip() for p in line.split(",")]
        if len(pieces) > 1 and all(p and " " not in p for p in pieces):
            items.extend(pieces)          # `phone, email, budget` shorthand
        else:
            items.append(line)            # a single (possibly prose) field
    return items


def parse_extract_fields(extract_def: str) -> list[str]:
    """Field keys declared by an `## Extract` section, de-duped, order-preserving.

    Primary form is one markdown bullet per field (the panel placeholder steers
    this): `- \\`phone\\` — …` → `phone`, `- first name and last name` →
    `first_name_and_last_name`, `1. budget` → `budget`. With no bullets, fall
    back to one field per line (`phone, email` shorthand splits on commas).
    Returns [] for an empty/blank section, which tells the cascade to leave
    extraction unconstrained (legacy behaviour) rather than force an empty schema.
    """
    if not extract_def or not extract_def.strip():
        return []
    bullets = _BULLET_LINE.findall(extract_def)
    items = bullets if bullets else _fallback_items(extract_def)
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        field = _slug_field(item)
        if field and field not in seen:
            seen.add(field)
            out.append(field)
    return out[:_MAX_EXTRACT_FIELDS]


# An Instagram handle: letters, digits, '.', '_', max 30 chars (a leading '@'
# is stripped before matching). Used to validate the optional share_target knob.
_IG_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def _norm_share_target(raw: Any) -> Optional[str]:
    """Normalize the optional IG `share` DM recipient (warming-writes PRD §6.2).

    None/'' => None (the share helper falls back to the FIRST contact in the live
    IG share list). Strips a leading '@'. Rejects anything that is not a plausible
    IG handle so a typo can't silently DM the wrong account.
    """
    if raw is None:
        return None
    s = str(raw).strip().lstrip("@")
    if not s:
        return None
    if not _IG_HANDLE_RE.match(s):
        raise ValueError(
            f"campaign.share_target {raw!r} is not a valid Instagram handle "
            f"(letters, digits, '.', '_'; max 30 chars)")
    return s


def _resolve_engine_mode(knobs: dict[str, Any]) -> str:
    """Validate + normalize the engine_mode knob (warming PRD §4.2)."""
    mode = str(knobs.get("engine_mode", "harvest")).strip().lower()
    if mode not in VALID_ENGINE_MODES:
        raise ValueError(
            f"campaign.engine_mode {mode!r} not supported; "
            f"expected one of {', '.join(VALID_ENGINE_MODES)}")
    return mode


def _resolve_home_feed(knobs: dict[str, Any], seed_hashtags: list[str],
                       seed_accounts: list[str],
                       seed_channels: list[str]) -> bool:
    """Should discovery walk the account's algorithmic HOME feed as a source?

    An explicit `include_home_feed` in config/brief always wins. Otherwise the
    default is seed-aware: a campaign that defines its own hashtag/account/channel
    seeds hunts THOSE (home feed OFF), so it doesn't drag in whatever the warmed
    account feed happens to surface; a seedless campaign keeps the home feed as its
    only discovery source (home feed ON).
    """
    if "include_home_feed" in knobs:
        return bool(knobs["include_home_feed"])
    return not (seed_hashtags or seed_accounts or seed_channels)


@dataclass(frozen=True)
class Soul:
    text: str
    path: Path


@dataclass(frozen=True)
class ChannelSpec:
    """One platform a campaign discovers on, with its OWN seeds (multi-platform
    campaign plan, C1). A campaign carries a tuple of these; an empty tuple means
    the legacy single-platform campaign (its flat scalar seeds are used instead).

    Frozen + tuple seed fields on purpose: a ChannelSpec is hashable and value-
    comparable, so `Campaign.channels` keeps a clean default and equality works.
    """
    platform: str
    seed_hashtags: tuple[str, ...] = ()
    seed_accounts: tuple[str, ...] = ()
    seed_channels: tuple[str, ...] = ()
    include_home_feed: bool = True


def _channel_spec_from_dict(d: Any) -> Optional[ChannelSpec]:
    """One brief `channels[]` entry → a ChannelSpec, or None to drop it.

    Returns None for a non-dict entry or an unsupported/blank platform (the
    parser silently drops it rather than failing the whole campaign — C2). Seed
    arrays are coerced to string tuples; `include_home_feed` resolves seed-aware
    when the key is absent, exactly like the flat-scalar path.
    """
    if not isinstance(d, dict):
        return None
    platform = str(d.get("platform", "")).strip().lower()
    if platform not in SUPPORTED_PLATFORMS:
        return None
    seed_hashtags = [str(x) for x in d.get("seed_hashtags", [])]
    seed_accounts = [str(x) for x in d.get("seed_accounts", [])]
    seed_channels = [str(x) for x in d.get("seed_channels", [])]
    return ChannelSpec(
        platform=platform,
        seed_hashtags=tuple(seed_hashtags),
        seed_accounts=tuple(seed_accounts),
        seed_channels=tuple(seed_channels),
        include_home_feed=_resolve_home_feed(
            d, seed_hashtags, seed_accounts, seed_channels),
    )


@dataclass(frozen=True)
class Campaign:
    campaign_id: str
    goal: str
    threshold: float
    escalate_band: tuple[float, float]
    language_mix: list[str]
    relevance_def: str
    match_def: str
    extract_def: str
    seed_direction: str
    raw: str
    path: Path
    knobs: dict[str, Any] = field(default_factory=dict)
    # System prompts for the model router (PRD §3: generic engine — the vertical
    # lives in config, not code). Empty when the campaign omits the section; the
    # router then falls back to its domain-free generic prompt.
    relevance_prompt: str = ""
    match_prompt: str = ""
    vision_prompt: str = ""
    # Which platform this brief runs against. Selects the FeedSource (PRD §3).
    platform: str = "instagram"
    # Harvest (read-only discovery) vs warming (account ramp/upkeep). Selects the
    # engine in dispatch.run_engine_session (warming PRD §4.2). CLI --engine-mode
    # overrides this. Default harvest preserves every existing campaign's behavior.
    engine_mode: str = "harvest"
    # Telegram discovery is deterministic: the operator seeds public channels /
    # groups (the algorithmic-feed seeds above don't apply). Empty for others.
    seed_channels: list[str] = field(default_factory=list)
    # Active discovery seeds — let the engine find relevant reels on ANY account
    # (not just a pre-steered home feed). Empty = home feed only.
    seed_hashtags: list[str] = field(default_factory=list)
    seed_accounts: list[str] = field(default_factory=list)
    # Walk the account's algorithmic HOME feed as a discovery source. Seed-aware
    # default (see `_resolve_home_feed`): OFF when the campaign has its own seeds,
    # ON when it has none. A brief/yaml `include_home_feed` overrides either way.
    include_home_feed: bool = True
    # Multi-platform fan-out (C2): the platforms this ONE campaign discovers on,
    # each with its own seeds. Empty tuple ≡ legacy single-platform — the flat
    # scalar fields above are used and the CLI runs exactly one engine session.
    # When non-empty the CLI fans out one session per channel sequentially.
    channels: tuple[ChannelSpec, ...] = field(default_factory=tuple)
    # Opt-in engagement (PRD departure: read-only is the default). Rate-limited.
    enable_actions: bool = False
    # Explicit per-campaign Uzbek-STT opt-in (Instagram-only, gated further by
    # `"uz" in language_mix` and the AIZU_STT_ENABLED global kill-switch — see
    # engines/instagram/cascade.py::gate_reel and core/transcribe.py).
    enable_stt: bool = False
    # Explicit per-campaign video-analysis opt-in (Instagram-only, gated further by
    # the AIZU_VIDEO_ANALYSIS_ENABLED global kill-switch — see
    # engines/instagram/cascade.py::gate_reel). Downloads + frame-samples a reel for
    # a multi-frame vision/fusion verdict when caption+vision+STT leave it unsure.
    enable_video_analysis: bool = False
    # Optional corroboration GATE (gap #4): when on, a match-stage disagreement
    # among the model-comparison fan-out (superadmin-switchable; see
    # core/router.py's `enable_comparison`/`compare_models`) demotes the match to
    # `needs_review` instead of a hard accept. Off by default — preserves every
    # existing campaign's accept path byte-for-byte, and is itself a no-op unless
    # the fan-out is also active (see matching.corroboration_needs_review).
    require_corroboration: bool = False
    max_likes_per_session: int = 8
    max_follows_per_session: int = 4
    # Optional DM recipient for the IG warming `share` action (warming-writes PRD
    # §6.2, O-share-target). None => the share helper falls back to the FIRST
    # contact in the live IG share list. Operators are nudged to set this for
    # safety. Validated as a plausible IG handle by `_norm_share_target`.
    share_target: Optional[str] = None

    def extract_fields(self) -> list[str]:
        """The field keys this campaign's `## Extract` section declares — the
        exact keys the model is told to emit and the panel renders as chips."""
        return parse_extract_fields(self.extract_def)


def load_soul(path: str | Path) -> Soul:
    p = Path(path)
    return Soul(text=p.read_text(encoding="utf-8"), path=p)


def load_campaign(path: str | Path) -> Campaign:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    knobs = _parse_yaml_block(text)

    band = knobs.get("escalate_band", [0.4, 0.75])
    if not isinstance(band, (list, tuple)) or len(band) != 2:
        raise ValueError("campaign.escalate_band must be a 2-element list [lo, hi]")

    cid = knobs.get("campaign_id")
    if not cid:
        raise ValueError("campaign.md missing required key: campaign_id")

    platform = str(knobs.get("platform", "instagram")).strip().lower()
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(
            f"campaign.platform {platform!r} not supported; "
            f"expected one of {', '.join(SUPPORTED_PLATFORMS)}")

    seed_hashtags = [str(x) for x in knobs.get("seed_hashtags", [])]
    seed_accounts = [str(x) for x in knobs.get("seed_accounts", [])]
    seed_channels = [str(x) for x in knobs.get("seed_channels", [])]

    return Campaign(
        campaign_id=str(cid),
        goal=str(knobs.get("goal", "signal")),
        threshold=float(knobs.get("threshold", 0.70)),
        escalate_band=(float(band[0]), float(band[1])),
        language_mix=[str(x) for x in knobs.get("language_mix", [])],
        relevance_def=_section(text, "Relevance"),
        match_def=_section(text, "Match"),
        extract_def=_section(text, "Extract"),
        seed_direction=_section(text, "Seed"),
        raw=text,
        path=p,
        knobs=knobs,
        relevance_prompt=_section(text, "Relevance Prompt"),
        match_prompt=_section(text, "Match Prompt"),
        vision_prompt=_section(text, "Vision Prompt"),
        platform=platform,
        seed_channels=seed_channels,
        seed_hashtags=seed_hashtags,
        seed_accounts=seed_accounts,
        include_home_feed=_resolve_home_feed(
            knobs, seed_hashtags, seed_accounts, seed_channels),
        enable_actions=bool(knobs.get("enable_actions", False)),
        enable_stt=bool(knobs.get("enable_stt", False)),
        enable_video_analysis=bool(knobs.get("enable_video_analysis", False)),
        require_corroboration=bool(knobs.get("require_corroboration", False)),
        max_likes_per_session=int(knobs.get("max_likes_per_session", 8)),
        max_follows_per_session=int(knobs.get("max_follows_per_session", 4)),
        engine_mode=_resolve_engine_mode(knobs),
        share_target=_norm_share_target(knobs.get("share_target")),
    )


# Fleet-dispatch (B4) bakes the resolved brief into the job spec, mirroring the
# soul_text pattern (BUILD-PLAN C5). A real-world brief (engine/config/campaign.md,
# through campaign_to_brief) measures ~12.5KB — this is a generous backstop against a
# pathological/tampered brief, not a working ceiling to design campaigns against.
MAX_CAMPAIGN_BRIEF_BYTES = 512 * 1024


def campaign_from_brief(campaign_id: str, brief: dict[str, Any]) -> Campaign:
    """Build a Campaign from a panel-authored brief dict (DB-stored, v4).

    Mirrors `load_campaign`'s validation/defaults so a DB campaign behaves exactly
    like a file campaign. Keys are snake_case (the bridge server translates the UI
    payload into this shape before persisting). `path` is synthetic — there is no
    file. Missing keys fall back to the same defaults as the file path.
    """
    if not campaign_id:
        raise ValueError("campaign_from_brief requires a campaign_id")
    platform = str(brief.get("platform", "instagram")).strip().lower()
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(
            f"campaign.platform {platform!r} not supported; "
            f"expected one of {', '.join(SUPPORTED_PLATFORMS)}")
    band = brief.get("escalate_band", [0.4, 0.75])
    if not isinstance(band, (list, tuple)) or len(band) != 2:
        raise ValueError("campaign.escalate_band must be a 2-element list [lo, hi]")
    seed_hashtags = [str(x) for x in brief.get("seed_hashtags", [])]
    seed_accounts = [str(x) for x in brief.get("seed_accounts", [])]
    seed_channels = [str(x) for x in brief.get("seed_channels", [])]
    # Multi-platform fan-out (C2): parse the optional channels[] list, silently
    # dropping non-dict / unsupported-platform entries. Absent/empty → () (legacy
    # single-platform, the flat scalars above drive a single engine session).
    raw_channels = brief.get("channels")
    channels = tuple(
        spec for spec in
        (_channel_spec_from_dict(d) for d in raw_channels)
        if spec is not None
    ) if isinstance(raw_channels, list) else ()
    return Campaign(
        campaign_id=str(campaign_id),
        goal=str(brief.get("goal", "lead")),
        threshold=float(brief.get("threshold", 0.70)),
        escalate_band=(float(band[0]), float(band[1])),
        language_mix=[str(x) for x in brief.get("language_mix", [])],
        relevance_def=str(brief.get("relevance_def", "")),
        match_def=str(brief.get("match_def", "")),
        extract_def=str(brief.get("extract_def", "")),
        seed_direction=str(brief.get("seed_direction", "")),
        raw=json.dumps(brief, ensure_ascii=False),
        path=Path(f"<db:{campaign_id}>"),
        knobs=dict(brief),
        relevance_prompt=str(brief.get("relevance_prompt", "")),
        match_prompt=str(brief.get("match_prompt", "")),
        vision_prompt=str(brief.get("vision_prompt", "")),
        platform=platform,
        seed_channels=seed_channels,
        seed_hashtags=seed_hashtags,
        seed_accounts=seed_accounts,
        include_home_feed=_resolve_home_feed(
            brief, seed_hashtags, seed_accounts, seed_channels),
        channels=channels,
        enable_actions=bool(brief.get("enable_actions", False)),
        enable_stt=bool(brief.get("enable_stt", False)),
        enable_video_analysis=bool(brief.get("enable_video_analysis", False)),
        require_corroboration=bool(brief.get("require_corroboration", False)),
        max_likes_per_session=int(brief.get("max_likes_per_session", 8)),
        max_follows_per_session=int(brief.get("max_follows_per_session", 4)),
        engine_mode=_resolve_engine_mode(brief),
        share_target=_norm_share_target(brief.get("share_target")),
    )


def campaign_to_brief(c: Campaign) -> dict[str, Any]:
    """A Campaign → a complete snake_case brief blob (the inverse of
    `campaign_from_brief`), JSON-serializable for the campaign_briefs store.

    Used to seed a merge base when persisting an edit to the FILE-backed primary
    campaign: its DB brief doesn't exist yet, so without this the saved brief
    would be only the panel-form fields and the YAML-only knobs (escalate_band,
    enable_actions, caps, seed_direction) would be silently dropped on the next
    resolve. Emits every key `campaign_from_brief` reads, so the round-trip is
    lossless.

    Multi-platform collapse rule (C2): a `channels` key is emitted ONLY at length
    ≥ 2; 0 or 1 channels collapse to the flat scalar brief so a single-platform
    campaign never carries redundant scalar/channel state that could drift. When
    exactly one channel exists (M4), the flat scalars are sourced FROM that channel
    so an explicit per-channel `include_home_feed` / seeds survive the collapse.
    """
    brief: dict[str, Any] = {
        "platform": c.platform,
        "goal": c.goal,
        "threshold": c.threshold,
        "escalate_band": [c.escalate_band[0], c.escalate_band[1]],
        "language_mix": list(c.language_mix),
        "relevance_def": c.relevance_def,
        "match_def": c.match_def,
        "extract_def": c.extract_def,
        "seed_direction": c.seed_direction,
        "relevance_prompt": c.relevance_prompt,
        "match_prompt": c.match_prompt,
        "vision_prompt": c.vision_prompt,
        "seed_channels": list(c.seed_channels),
        "seed_hashtags": list(c.seed_hashtags),
        "seed_accounts": list(c.seed_accounts),
        "include_home_feed": c.include_home_feed,
        "enable_actions": c.enable_actions,
        "enable_stt": c.enable_stt,
        "enable_video_analysis": c.enable_video_analysis,
        "require_corroboration": c.require_corroboration,
        "max_likes_per_session": c.max_likes_per_session,
        "max_follows_per_session": c.max_follows_per_session,
        "engine_mode": c.engine_mode,
        "share_target": c.share_target,
    }
    if len(c.channels) == 1:
        # M4: collapse to flat scalars sourced from the single channel.
        ch = c.channels[0]
        brief["platform"] = ch.platform
        brief["seed_hashtags"] = list(ch.seed_hashtags)
        brief["seed_accounts"] = list(ch.seed_accounts)
        brief["seed_channels"] = list(ch.seed_channels)
        brief["include_home_feed"] = ch.include_home_feed
    elif len(c.channels) >= 2:
        brief["channels"] = [
            {
                "platform": ch.platform,
                "seed_hashtags": list(ch.seed_hashtags),
                "seed_accounts": list(ch.seed_accounts),
                "seed_channels": list(ch.seed_channels),
                "include_home_feed": ch.include_home_feed,
            }
            for ch in c.channels
        ]
    return brief


def resolve_campaign(store: Any, cfg_dir: str | Path,
                     campaign_id: str) -> Optional[Campaign]:
    """Resolve a runnable Campaign for `campaign_id` from the same rule the CLI,
    the batch runner, and the bridge server all share.

    A panel-authored brief in the DB wins; otherwise the file campaign matches by
    its own id; otherwise None (the caller reports "not runnable"). A malformed
    brief raises ValueError — callers surface that as a user error, not a crash.
    `store` is any object exposing `get_campaign_brief(campaign_id)` (typed loosely
    so config.py keeps no dependency on the store module).
    """
    brief = store.get_campaign_brief(campaign_id)
    if brief is not None:
        return campaign_from_brief(campaign_id, brief)
    try:
        file_campaign = load_campaign(Path(cfg_dir) / "campaign.md")
    except (FileNotFoundError, ValueError):
        return None
    if file_campaign.campaign_id == campaign_id:
        return file_campaign
    return None
