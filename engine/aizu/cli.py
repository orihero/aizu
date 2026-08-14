"""Command-line entrypoint.

  aizu run   --dry-run          run the full loop on the built-in fake feed
  aizu run                      live: OpenRouter + CDP attach (CDP not yet wired)
  aizu status                   print open health flags + last session

Config paths default to ./config/soul.md and ./config/campaign.md; DB defaults
to ./aizu.db. The panel reads the same DB.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

from . import dispatch
from . import warming_control
from .core import accounts as accounts_lib
from .core.config import (CDP_PLATFORMS, PER_ORG_CREDENTIAL_PLATFORMS, Campaign,
                          ChannelSpec, load_campaign, load_soul, resolve_campaign)
from .core.feed import Comment, FakeFeed, Reel
from .core.logsetup import configure_logging, get_logger
from .core.mock_router import MockRouter
from .core.pacing import PacingConfig, Pacer
from .core.pause import pause_file_from_env, wait_while_paused
from .core.router import OpenRouterRouter, build_router, env_flag, _parse_csv_env
from .core.store import Store
# 9333 is the canonical warmed-Chrome port repo-wide (ledger F10). The literal is
# duplicated from readiness.DEFAULT_CDP_URL on purpose, exactly as core/cdp.py and
# worker/config.py do it: `readiness` imports Playwright AT IMPORT TIME, and this module
# is pulled in by `worker/job_runner.py` (for the `_run_session_loop` seam), so a
# module-scope `from .readiness import ...` here dragged the whole Playwright package
# into every worker sidecar — including an API-only box that never opens a browser — and
# silently defeated the lazy `from .. import readiness` imports inside
# `worker/preflight.py`, which exist for precisely that reason. One string is not worth
# the dependency; `test_config_import_cost.py` pins the constants against each other.
DEFAULT_CDP_URL = "http://127.0.0.1:9333"

# Explicit name (not __name__): run as `python -m aizu.cli`, __name__ is
# "__main__", which would sit outside the configured `aizu` logger tree.
log = get_logger("aizu.cli")


def _load_env() -> None:
    """Load a local .env (engine/.env or ./.env) into os.environ if present.

    Uses python-dotenv when available; otherwise a tiny built-in parser so the
    key works with no extra install. Existing env vars win over the file.
    """
    candidates = [Path.cwd() / ".env",
                  Path(__file__).resolve().parent.parent / ".env"]
    try:
        from dotenv import load_dotenv  # type: ignore
        for p in candidates:
            if p.exists():
                load_dotenv(p, override=False)
        return
    except Exception:
        pass
    for p in candidates:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip("'\""))

DEFAULT_CONFIG = Path("config")
DEFAULT_DB = Path("aizu.db")


def _sample_feed() -> FakeFeed:
    """A small, brief-relevant fake feed for dry runs."""
    return FakeFeed([
        Reel(reel_id="r1", author="acme.io",
             caption="Acme — plan your sprints in one place. Start your free trial.",
             ocr_text="ACME · Pro from $12/seat · 14-day free trial",
             comments=[
                 Comment("c1", "dev_dana", "How much is the Pro plan? +1 415 555 0142", "en"),
                 Comment("c2", "spam_bot", "Nice video! 🔥🔥", "en"),
                 Comment("c3", "sam_t", "Does it integrate with Slack? Email me sam@example.com", "en"),
             ]),
        Reel(reel_id="r2", author="catlover", caption="Funny cat compilation", comments=[
            Comment("c4", "user1", "lol", "en"),
        ]),
        Reel(reel_id="r3", author="acme.io",
             caption="New Acme app dashboard, book a demo",
             ocr_text="ACME · Free 14-day trial · book a demo",
             comments=[
                 Comment("c5", "maria", "Want to buy this for our team, can I get a demo? +1 312 555 0199", "en"),
             ]),
    ])


def _build_run_io(campaign: Campaign, store: Store, dry_run: bool,
                  args: argparse.Namespace, engine_mode: str = "harvest"):
    """Build the (router, feed, pacer) triple for one session.

    Raises RuntimeError when a live run lacks OPENROUTER_API_KEY and lets
    build_feed's NotImplementedError (unsupported platform) propagate — the caller
    decides whether that aborts the run or just skips one campaign.
    """
    if dry_run:
        # No network, no real sleeping, no daytime gate.
        return (MockRouter(store=store), _sample_feed(),
                Pacer(cfg=PacingConfig(enforce_daytime=False), sleep=lambda _t: None))
    if engine_mode == "warming":
        return _build_warming_io(campaign, store, args)
    # A live run needs a working LLM backend: a local endpoint (AIZU_LLM_BASE_URL,
    # e.g. Ollama qwen3-vl) OR an OpenRouter cloud key. A fully-local run no longer
    # requires the cloud key (local-first, per soliq's stack).
    if not os.environ.get("AIZU_LLM_BASE_URL", "").strip() \
            and not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError(
            "No LLM backend configured: set AIZU_LLM_BASE_URL (local Ollama/vLLM) "
            "or OPENROUTER_API_KEY (cloud) for a live run")
    from .dispatch import build_feed
    # Model-comparison fan-out: the superadmin DB toggle governs an in-process run
    # (this box IS the cloud box, so `store` sees the live setting); a distributed
    # worker's local DB never gets that row written, so MODEL_COMPARISON_ENABLED
    # is the fleet's own opt-in — either source turns it on. Models are always
    # env-declared (never stored), same on both box types.
    comparison_enabled = ((store is not None and store.model_comparison_enabled())
                          or env_flag("MODEL_COMPARISON_ENABLED"))
    # build_router: local-first with optional cloud fallback (see core/router.py).
    router = build_router(store=store, spend_cap_usd=args.spend_cap,
                          text_model=args.text_model,
                          vision_model=args.vision_model,
                          enable_comparison=comparison_enabled,
                          compare_models=_parse_csv_env("MODEL_COMPARISON_MODELS"))
    credentials = _resolve_platform_credentials(
        campaign, store, baked=getattr(args, "platform_credentials", None))
    feed = build_feed(campaign.platform, cdp_url=args.cdp_url,
                      seed_hashtags=campaign.seed_hashtags,
                      seed_accounts=campaign.seed_accounts,
                      seed_channels=campaign.seed_channels,
                      include_home_feed=campaign.include_home_feed,
                      credentials=credentials)
    pacer = Pacer()
    if not pacer.cfg.enforce_daytime:
        log.warning("Daytime guard DISABLED (AIZU_IGNORE_DAYTIME) — live run will "
                    "proceed off-hours; intended for testing only")
    return router, feed, pacer


def _build_warming_io(campaign: Campaign, store: Store, args: argparse.Namespace):
    """Build the (router, feed, pacer) triple for a WARMING session (PRD §4.2).

    Warming forces a home-feed-only feed (empty seeds), needs NO LLM (zero ML
    inference — a placeholder MockRouter the engine ignores, so a warming run does
    not require OPENROUTER_API_KEY), and constructs a Pacer with the daytime guard
    FORCED ON, clocked in the account's own timezone. Forcing `enforce_daytime=True`
    is mandatory: an explicit PacingConfig value always beats the
    AIZU_IGNORE_DAYTIME env opt-out (which live runs set), so warming can
    never write at 3am (§9.3).
    """
    from .dispatch import build_feed
    account = store.resolve_account_for_campaign(campaign.campaign_id, campaign.platform)
    credentials = _resolve_platform_credentials(
        campaign, store, baked=getattr(args, "platform_credentials", None))
    feed = build_feed(campaign.platform, cdp_url=args.cdp_url,
                      seed_hashtags=(), seed_accounts=(), seed_channels=(),
                      include_home_feed=True, credentials=credentials)
    return MockRouter(store=store), feed, _warming_pacer(account)


def _warming_pacer(account: Optional[dict]) -> Pacer:
    """A Pacer with the daytime guard forced ON, clocked in the account's tz so
    warming runs at the account's LOCAL hours (PRD §4.2, §8.3). Falls back to the
    host clock when no fingerprint timezone is recorded."""
    from datetime import datetime
    clock = datetime.now
    fp = account.get("fingerprint") if account else None
    tz_id = fp.get("timezone_id") if isinstance(fp, dict) else None
    if tz_id:
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(tz_id)
            clock = lambda: datetime.now(zone)  # noqa: E731
        except Exception:  # noqa: BLE001 — unknown tz → host clock, guard still on
            log.warning("Unknown account timezone %r — using host clock for daytime guard",
                        tz_id)
    return Pacer(cfg=PacingConfig(enforce_daytime=True), clock=clock)


# Platforms whose creds are connected per-org in the panel (encrypted secret
# store). Instagram stays single-tenant (warmed-Chrome / CDP from .env), so it
# has no per-org secret and resolves to None → build_feed reads env. Now lives
# in core.config (PER_ORG_CREDENTIAL_PLATFORMS) so server._dispatch_run_to_fleet
# can share it without importing this module; aliased here to keep this file's
# existing references unchanged.
_PER_ORG_CREDENTIAL_PLATFORMS = PER_ORG_CREDENTIAL_PLATFORMS

# Platforms driven through the warmed-Chrome / CDP browser (multi-platform plan
# C4). A genuine account-level halt on one of these poisons the SHARED browser
# session, so the fan-out skips the remaining CDP channels; API platforms are
# unaffected. The set itself now lives in core.config beside SUPPORTED_PLATFORMS —
# the bridge's readiness gate needs the same split, and one copy per caller was
# already one copy too many.
_CDP_PLATFORMS = CDP_PLATFORMS

# HaltKinds that POISON the shared CDP browser/account: after one of these, the
# remaining CDP channels in a fan-out can't be trusted to run cleanly. A
# `daytime` window halt and an `unknown`/None halt are NOT poison — daytime just
# ends the run, and an unclassified halt is recorded without poisoning peers.
# (Phase 2 raises these kinds; Phase 1 only defines the set.)
_POISON_HALT_KINDS = frozenset({"action_block", "checkpoint", "login", "canary"})


def _resolve_platform_credentials(campaign: Campaign, store: Store,
                                  baked: Optional[dict] = None) -> Optional[dict]:
    """Per-org stored secret for this campaign's platform, or None to fall back
    to env.

    `baked` is the credential dict the worker sidecar fetched fresh for THIS job
    (Sidecar._fetch_job_credential → POST /api/worker/jobs/{id}/credential, lease-
    holder-gated) and threaded onto the JobSpec before spawning the child — SECURITY
    REVIEW CRITICAL/HIGH retired the prior mechanism (server._dispatch_run_to_fleet
    baking the decrypted secret straight into the job spec, a durable cloud-side
    plaintext copy) in favor of this decrypt-on-demand pull, but this function's own
    contract is UNCHANGED: a genuinely remote worker still has no `campaign_meta`/
    `integration_secrets` row for this org on its own local DB, so
    `store.org_for_campaign` would still resolve None there even though the org really
    did connect this platform. A non-None `baked` wins outright — no local lookup — so
    the remote-worker path never depends on a DB row that can't exist on that box. A
    None `baked` (no fetch was attempted — a direct CLI run or a same-box/dev job — OR
    the fetch ran and found nothing connected for this org/platform) falls back to the
    local per-org store lookup: on the same box that still finds the real secret
    (unchanged single-tenant behavior); on a truly remote box the local lookup also
    resolves nothing, so the two None cases converge to the same (correct) answer
    either way.

    Returns None for Instagram, an unregistered campaign, or no stored secret
    anywhere — keeping local single-tenant runs working. A decryption failure
    (SecretCipherError, a RuntimeError) propagates and is folded per-campaign."""
    if campaign.platform not in _PER_ORG_CREDENTIAL_PLATFORMS:
        return None
    if baked is not None:
        return baked
    org_id = store.org_for_campaign(campaign.campaign_id)
    if org_id is None:
        return None
    return store.get_integration_secret(org_id, campaign.platform)


def _is_auth_error(exc: BaseException) -> bool:
    """True for a credential/auth failure on a per-org platform run: a missing or
    invalid stored secret, an HTTP 401/403 (YouTube bad key / quota), or a revoked/
    expired Telethon session. Matched structurally (status code, class name) so we
    don't depend on importing httpx/telethon here."""
    msg = str(exc).lower()
    if "reconnect" in msg or "credentials missing" in msg:
        return True
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) in (401, 403):
        return True
    name = type(exc).__name__
    return any(k in name for k in
               ("Unauthorized", "AuthKey", "SessionRevoked", "SessionExpired"))


def _flag_needs_reconnect(store: Store, campaign: Campaign) -> None:
    """Flip the per-org integration to needs-reconnect after a run-time auth
    failure, so the panel surfaces a reconnect badge (Phase 4). Best-effort:
    bookkeeping must never mask the original error."""
    if campaign.platform not in _PER_ORG_CREDENTIAL_PLATFORMS:
        return
    org_id = store.org_for_campaign(campaign.campaign_id)
    if org_id is None:
        return
    try:
        store.set_integration(org_id, campaign.platform,
                              connected=False, detail="needs reconnect")
    except Exception:  # noqa: BLE001 - never let this hide the real failure
        pass


def _effective_engine_mode(campaign: Campaign, args: argparse.Namespace) -> str:
    """The engine mode for this run: an explicit CLI --engine-mode wins, else the
    campaign brief's engine_mode (default 'harvest'). (warming PRD §4.2)"""
    return getattr(args, "engine_mode", None) or getattr(campaign, "engine_mode", "harvest")


def _effective_stt_enabled(campaign: Campaign, args: argparse.Namespace) -> bool:
    """Uzbek STT for this run: an explicit CLI --stt/--no-stt wins, else the
    campaign brief's enable_stt (default False). Still gated at run_session()
    construction by AIZU_STT_ENABLED/AIZU_STT_MODEL_PATH and per-reel by
    "uz" in language_mix — see core/transcribe.py and engines/instagram/cascade.py."""
    override = getattr(args, "stt", None)
    if override is not None:
        return override
    return getattr(campaign, "enable_stt", False)


def _effective_channels(campaign: Campaign) -> list[ChannelSpec]:
    """The channels this campaign discovers on, one engine session each (C2).

    A multi-platform campaign returns its stored channels; a legacy single-platform
    campaign (`channels == ()`) synthesizes ONE ChannelSpec from its flat scalar
    seeds, so the fan-out loop treats both shapes uniformly. Returns a NEW list each
    call so a caller can never mutate the campaign's channel tuple by aliasing.
    """
    if campaign.channels:
        return list(campaign.channels)
    return [ChannelSpec(
        platform=campaign.platform,
        seed_hashtags=tuple(campaign.seed_hashtags),
        seed_accounts=tuple(campaign.seed_accounts),
        seed_channels=tuple(campaign.seed_channels),
        include_home_feed=campaign.include_home_feed,
    )]


def _campaign_with_channel(c: Campaign, channel: ChannelSpec) -> Campaign:
    """A synthetic single-platform copy of `c` targeting one channel (C2).

    The fan-out loop (Phase 2) runs each channel through `dispatch.run_engine_session`
    via one of these copies, so every engine still sees a normal single-platform
    campaign and dispatch/the engines stay UNCHANGED. Identity isolation is critical:
    the copy gets FRESH mutable `list()` seeds and a fresh `dict(knobs)` (never the
    base campaign's objects), and `channels=()` so the copy itself never re-fans-out.
    The base campaign is left untouched (dataclasses.replace returns a new instance).
    """
    return dataclasses.replace(
        c,
        platform=channel.platform,
        seed_hashtags=list(channel.seed_hashtags),
        seed_accounts=list(channel.seed_accounts),
        seed_channels=list(channel.seed_channels),
        include_home_feed=channel.include_home_feed,
        knobs=dict(c.knobs),
        channels=(),
    )


def _run_one(*, campaign: Campaign, store: Store, soul, dry_run: bool,
             args: argparse.Namespace, lead_target: Optional[int] = None) -> dict:
    """Run a single discovery session and return its summary dict.

    A halt is folded into the summary as `halt_reason` rather than raised, so a
    batch can record it and decide whether to continue. Setup errors (missing key,
    unsupported platform) propagate to the caller. A live per-org auth failure
    flips the integration to needs-reconnect and is re-raised as a RuntimeError so
    the batch folds it per-campaign instead of crashing.
    """
    mode = "dry" if dry_run else "live"
    engine_mode = _effective_engine_mode(campaign, args)
    # Resolve the --stt/--no-stt override onto the campaign BEFORE it's handed to
    # dispatch/run_session, so engines/instagram/session.py's `campaign.enable_stt`
    # read (both the run_session warm-load gate and the per-reel cascade gate) sees
    # the effective value — same override-then-replace shape as engine_mode above,
    # except engine_mode is threaded as a separate kwarg while enable_stt lives on
    # the Campaign itself, so it must be replaced onto the object. Guarded on
    # is_dataclass so test doubles (plain classes standing in for Campaign in
    # cli tests) pass through unchanged instead of raising.
    if dataclasses.is_dataclass(campaign):
        campaign = dataclasses.replace(
            campaign, enable_stt=_effective_stt_enabled(campaign, args))
    log.info("Starting %s %s run · campaign=%s platform=%s",
             mode, engine_mode, campaign.campaign_id, campaign.platform)
    # Kill-switch (warming PRD §9.1): checked BEFORE _build_run_io so a disabled
    # warming campaign never attaches a live Chrome. Dry runs use the fake feed
    # (no Chrome), so they bypass the gate to keep the loop testable.
    if engine_mode == "warming" and not dry_run:
        org_id = store.org_for_campaign(campaign.campaign_id)
        reason = warming_control.warming_kill_reason(
            store, org_id=org_id, platform=campaign.platform)
        if reason:
            log.warning("Warming skipped · campaign=%s: %s", campaign.campaign_id, reason)
            return {"session_id": None, "halt_reason": reason}
    # Gap #1 self-healing cooldown: a prior SOFT halt (action_block/canary) on this
    # (campaign, platform) escalated an exponential-backoff cooldown instead of
    # waiting on a human (Store.record_soft_halt; engines/base.py's SOFT_HALT_KINDS).
    # Checked BEFORE _build_run_io so a still-cooling campaign never touches the
    # browser/account again — whichever caller retries next (a human clicking Run, a
    # run-all batch, or the schedule daemon's next fire) short-circuits cleanly here
    # until cooldown_until elapses, at which point this is a silent no-op and the run
    # proceeds exactly as before. No resolve_flag, no human step. Dry runs use the
    # fake feed (no real anti-bot signal ever fires) so they bypass the gate,
    # matching the warming kill-switch above; `store` is None only in unit tests that
    # stub this function out entirely (never in a real run), so guard it too.
    if store is not None and not dry_run:
        remaining = store.cooldown_remaining(campaign.campaign_id, campaign.platform)
        if remaining > 0:
            log.info("Cooldown active · campaign=%s platform=%s · %.0fs remaining",
                     campaign.campaign_id, campaign.platform, remaining)
            return {"session_id": None,
                    "halt_reason": f"cooling down ({remaining:.0f}s remaining)"}
    try:
        router, feed, pacer = _build_run_io(campaign, store, dry_run, args, engine_mode)
    except Exception as e:
        log.error("Run setup failed · campaign=%s: %s", campaign.campaign_id, e)
        raise _on_run_error(store, campaign, dry_run, e)
    # v10: the bridge passes the RunManager run_id by env so the session can stream
    # its activity to the panel; absent (direct CLI run) it stays a silent no-op.
    # The engine for campaign.platform is selected by dispatch; it folds a halt into
    # the summary's halt_reason, so only genuine setup/auth errors surface here.
    try:
        return dispatch.run_engine_session(
            campaign=campaign, store=store, router=router, feed=feed, soul=soul,
            pacer=pacer, run_id=os.environ.get("AIZU_RUN_ID"),
            lead_target=lead_target, engine_mode=engine_mode)
    except Exception as e:
        log.error("Run failed · campaign=%s: %s", campaign.campaign_id, e)
        raise _on_run_error(store, campaign, dry_run, e)
    finally:
        # Always tear the feed down so the NEXT session in a multi-session run can
        # attach a fresh Playwright driver (a 2nd sync_playwright().start() while the
        # first is alive raises "Sync API inside the asyncio loop").
        _close_feed(feed)


def _close_feed(feed) -> None:
    """Release a session's feed (disconnect Playwright/CDP from the warmed Chrome).
    Guarded: the dry FakeFeed has no close(), and a teardown error must never mask
    the run's real result or error."""
    close = getattr(feed, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception as e:  # noqa: BLE001 — cleanup must not mask the run outcome
        log.warning("Feed close failed (continuing): %s", e)


def _on_run_error(store: Store, campaign: Campaign, dry_run: bool,
                  exc: Exception) -> Exception:
    """Map a run error: on a live auth failure, flag needs-reconnect and convert to
    a clear RuntimeError (foldable by the batch); otherwise return it unchanged."""
    if not dry_run and _is_auth_error(exc):
        _flag_needs_reconnect(store, campaign)
        return RuntimeError(f"{campaign.platform} needs reconnect: {exc}")
    return exc


# Counter keys summed across the sessions of a timed run, for one aggregate summary.
_AGG_COUNTERS = ("reels_seen", "relevance_passes", "comments_scored", "matches")

# Platforms whose discovery is DETERMINISTIC (seeded / API, not an algorithmic feed):
# re-running a session re-fetches the SAME items (now already-seen) at full API quota
# cost for zero new leads, so a single pass per run is complete. Only Instagram's
# algorithmic feed surfaces fresh items on a re-scroll, so only it loops to a target.
_SINGLE_PASS_PLATFORMS = {"youtube", "telegram", "reddit"}


def _run_one_channel(*, campaign: Campaign, store: Store, soul,
                     args: argparse.Namespace) -> dict:
    """Run ONE channel (a single-platform campaign) to completion and return its
    sub-summary. Always runs ≥1 pass (D1); when --target-leads and/or
    --duration-minutes is set it runs back-to-back LIVE sessions until the run is
    done, then stops.

    A run finishes on whichever comes first: the lead target is met (cumulative
    `matches >= --target-leads`), the wall-clock safety cap elapses
    (--duration-minutes), or a session halts (daytime window closed, action-block).
    Each session still respects its randomized reel budget for human-like pacing; the
    loop just spawns more sessions to reach a larger target. With neither flag the
    default is a single session. Dry runs always do exactly one session: the fake
    feed is instant, so looping would spin. A halt is surfaced in the summary
    (`halt_reason` + classified `halt_kind`) so the caller can both report it and
    decide whether it poisons sibling CDP channels.

    DETERMINISTIC platforms (YouTube, Telegram, Reddit) also do a SINGLE pass: their
    sources are seeded/API, so a second session re-fetches the SAME items (now
    already-seen) at full API quota cost for zero new leads. Only Instagram's
    algorithmic feed surfaces fresh items on a re-scroll, so only it loops to target.
    """
    target = getattr(args, "target_leads", None)
    duration = getattr(args, "duration_minutes", None)

    summary = _run_one(campaign=campaign, store=store, soul=soul,
                       dry_run=args.dry_run, args=args, lead_target=target)
    # Warming is always single-pass (one ramp session per run), checked BEFORE the
    # deterministic-platform list so X/LinkedIn warming doesn't loop to a lead
    # target the way their harvest does (PRD §4.2).
    single_pass = (_effective_engine_mode(campaign, args) == "warming"
                   or campaign.platform in _SINGLE_PASS_PLATFORMS)
    if single_pass and (target or duration):
        log.info("Single discovery pass for %s — deterministic source / warming; "
                 "skipping the back-to-back lead-target loop (re-running would "
                 "re-fetch the same items at full API cost).", campaign.platform)
    if (args.dry_run or summary.get("halt_reason")
            or (not target and not duration) or single_pass):
        return summary

    agg = {"sessions": 1, "spend_usd": float(summary.get("spend_usd") or 0.0),
           "halt_reason": None, "halt_kind": None}
    for key in _AGG_COUNTERS:
        agg[key] = int(summary.get(key) or 0)
    deadline = time.monotonic() + duration * 60 if duration else None
    pause_file = pause_file_from_env()
    while True:
        # v12: between-sessions cooperative pause checkpoint (the per-reel checkpoint
        # lives in the engine session). The duration deadline is checked AFTER, so a
        # run resumed past its cap stops at once — the wall-clock is never frozen.
        wait_while_paused(pause_file)
        if target is not None and agg["matches"] >= target:
            break
        if deadline is not None and time.monotonic() >= deadline:
            break
        # Top up only the leads still missing, so a session can short-circuit once the
        # run as a whole has hit the target (None → bounded only by the reel budget).
        remaining = (target - agg["matches"]) if target is not None else None
        nxt = _run_one(campaign=campaign, store=store, soul=soul,
                       dry_run=args.dry_run, args=args, lead_target=remaining)
        agg["sessions"] = agg["sessions"] + 1
        agg["spend_usd"] = agg["spend_usd"] + float(nxt.get("spend_usd") or 0.0)
        for key in _AGG_COUNTERS:
            agg[key] = agg[key] + int(nxt.get(key) or 0)
        if nxt.get("halt_reason"):
            agg["halt_reason"] = nxt["halt_reason"]
            agg["halt_kind"] = nxt.get("halt_kind")
            break
    agg["spend_usd"] = round(agg["spend_usd"], 6)
    if target is not None:
        agg["target_leads"] = target
    return agg


def _run_session_loop(*, campaign: Campaign, store: Store, soul,
                      args: argparse.Namespace) -> dict:
    """Run a campaign across ALL its channels and return one aggregate summary.

    A legacy single-platform campaign (`channels == ()`) runs exactly as before —
    one channel, byte-for-byte the old behavior. A multi-platform campaign fans out
    one engine session per channel SEQUENTIALLY in this one process / run_id / warmed
    Chrome (C2), via a `_campaign_with_channel` synthetic copy so dispatch and every
    engine stay unchanged.

    Halt policy across channels (C4 / D3): a per-channel auth/setup failure
    (RuntimeError/NotImplementedError) is recorded under `per_platform` and the run
    continues — leads already found on prior channels are never lost. An account-level
    halt (`halt_kind` in `_POISON_HALT_KINDS`) poisons the SHARED CDP browser, so the
    remaining CDP channels are skipped (`{skipped: "cdp_poisoned"}`); API channels
    still run. A `daytime` halt is a wall-clock gate — it ends the run cleanly without
    poisoning anything. An API/`unknown` halt is recorded and the fan-out continues.
    """
    if not campaign.channels:
        return _run_one_channel(campaign=campaign, store=store, soul=soul, args=args)

    target = getattr(args, "target_leads", None)
    agg: dict = {"sessions": 0, "spend_usd": 0.0, "halt_reason": None,
                 "halt_kind": None}
    for key in _AGG_COUNTERS:
        agg[key] = 0
    per_platform: dict[str, dict] = {}
    cdp_poisoned = False

    for channel in _effective_channels(campaign):
        plat = channel.platform
        if cdp_poisoned and plat in _CDP_PLATFORMS:
            per_platform[plat] = {"skipped": "cdp_poisoned"}
            continue
        sub_campaign = _campaign_with_channel(campaign, channel)
        try:
            sub = _run_one_channel(campaign=sub_campaign, store=store, soul=soul,
                                   args=args)
        except (RuntimeError, NotImplementedError) as e:
            # A mid-fan-out auth/setup failure must never lose prior channels' leads.
            log.warning("Channel %s failed (continuing): %s", plat, e)
            per_platform[plat] = {"error": str(e)}
            continue

        per_platform[plat] = sub
        for key in _AGG_COUNTERS:
            agg[key] = agg[key] + int(sub.get(key) or 0)
        agg["sessions"] = agg["sessions"] + int(sub.get("sessions") or 1)
        agg["spend_usd"] = agg["spend_usd"] + float(sub.get("spend_usd") or 0.0)

        halt_reason = sub.get("halt_reason")
        if halt_reason:
            halt_kind = sub.get("halt_kind")
            agg["halt_reason"] = halt_reason
            agg["halt_kind"] = halt_kind
            if halt_kind in _POISON_HALT_KINDS:
                cdp_poisoned = True            # skip the remaining CDP channels
            elif halt_kind == "daytime":
                break                          # wall-clock gate ends the whole run
            # else (API / unknown halt): recorded, fan-out continues to next channel

    # All-channels-failed guard: if NO channel even started a session and every attempted
    # channel recorded a setup/auth error (a missing OPENROUTER_API_KEY, expired login, …),
    # the whole run FAILED — surface it as a halt so the job result and the run activity feed
    # show the reason instead of a silent "done, 0 leads". A genuine empty run has sessions>0
    # and no per-channel error; a daytime/poison halt already set halt_reason above (so the
    # `halt_reason is None` guard leaves those untouched). This closed the "fleet can't run
    # campaigns" silent failure: a worker missing OPENROUTER_API_KEY looked identical to a
    # campaign that simply found nothing.
    if (agg["halt_reason"] is None and agg["sessions"] == 0 and per_platform
            and all("error" in sub for sub in per_platform.values())):
        agg["halt_reason"] = next(iter(per_platform.values()))["error"]
        agg["halt_kind"] = "setup"
    agg["spend_usd"] = round(agg["spend_usd"], 6)
    agg["per_platform"] = per_platform
    if target is not None:
        agg["target_leads"] = target
    return agg


def cmd_run(args: argparse.Namespace) -> int:
    cfg_dir = Path(args.config)
    soul = load_soul(cfg_dir / "soul.md")
    store = Store(args.db)
    try:
        # --campaign <id> resolves a panel-authored brief (or the file campaign by
        # id); without it the engine runs the file-based config/campaign.md.
        if args.campaign:
            try:
                campaign = resolve_campaign(store, cfg_dir, args.campaign)
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
            if campaign is None:
                print(f"error: no stored brief for campaign {args.campaign!r} — author "
                      "one in the panel, or omit --campaign to run config/campaign.md",
                      file=sys.stderr)
                return 2
        else:
            campaign = load_campaign(cfg_dir / "campaign.md")
        try:
            summary = _run_session_loop(campaign=campaign, store=store, soul=soul,
                                        args=args)
        except (RuntimeError, NotImplementedError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    finally:
        store.close()
    if summary.get("halt_reason"):
        print(f"HALTED: {summary['halt_reason']}", file=sys.stderr)
        return 3
    log.success("Run complete · matches=%s spend=$%.4f",
                summary.get("matches", 0), float(summary.get("spend_usd", 0.0)))
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def _live_campaigns(store: Store, cfg_dir: Path,
                    org_id: Optional[int] = None) -> list[Campaign]:
    """Ordered, de-duplicated runnable campaigns: status 'live' AND not archived.

    Every campaign_meta row with status 'live' that resolves — a DB brief, OR the
    file campaign (config/campaign.md) matched by id when its own id is registered
    live. Brief-less live drafts simply don't resolve and are skipped.

    The file campaign is NOT auto-included when it has no meta row: a batch only
    runs campaigns the operator explicitly registered live, so an orphaned legacy
    brief (e.g. a stale config/campaign.md) never runs by surprise. To run it in a
    batch, give it a 'live' meta row (resolve_campaign still matches it by id).
    """
    out: list[Campaign] = []
    seen: set[str] = set()
    for meta in store.list_campaign_meta(org_id):
        # v12: the runnable predicate — live AND not archived. Mirrors
        # store.RUNNABLE_SQL_PREDICATE and the panel's selectors.isRunnable so an
        # archived campaign can never be run by the batch path.
        if meta.get("status") != "live" or meta.get("archived_at") is not None:
            continue
        cid = meta["campaign_id"]
        try:
            campaign = resolve_campaign(store, cfg_dir, cid)
        except ValueError:
            campaign = None
        if campaign is not None and cid not in seen:
            seen.add(cid)
            out.append(campaign)
    # Run CDP campaigns (warmed-Chrome platforms) BEFORE API ones, stably: a poison
    # halt on a CDP campaign skips only the remaining CDP campaigns, so ordering them
    # first means an API campaign is never skipped for a browser problem it can't hit.
    return sorted(out, key=lambda c: 0 if c.platform in _CDP_PLATFORMS else 1)


def cmd_run_all(args: argparse.Namespace) -> int:
    cfg_dir = Path(args.config)
    soul = load_soul(cfg_dir / "soul.md")
    store = Store(args.db)
    # Fail fast on a global setup problem before touching any campaign.
    if not args.dry_run and not os.environ.get("OPENROUTER_API_KEY"):
        print("error: OPENROUTER_API_KEY not set (required for live run)", file=sys.stderr)
        store.close()
        return 2

    results: list[dict] = []
    # Only an ACCOUNT-level CDP halt poisons the shared warmed-Chrome browser (C4).
    # A daytime gate or an API rate-limit halt is recorded and the batch continues;
    # only `_POISON_HALT_KINDS` flips this and skips the remaining CDP campaigns.
    cdp_poisoned = False
    org_id = getattr(args, "org", None)
    try:
        live = _live_campaigns(store, cfg_dir, org_id)
        log.info("Batch run · %d live campaign(s)%s", len(live),
                 f" · org={org_id}" if org_id is not None else "")
        for campaign in live:
            cid = campaign.campaign_id
            if cdp_poisoned and campaign.platform in _CDP_PLATFORMS:
                # A prior CDP account-halt poisoned the shared browser — skip the
                # remaining CDP campaigns (API campaigns are unaffected, still run).
                results.append({"campaignId": cid, "ok": False, "skipped": True,
                                "reason": "cdp batch halted"})
                continue
            try:
                summary = _run_one(campaign=campaign, store=store, soul=soul,
                                   dry_run=args.dry_run, args=args)
            except (RuntimeError, NotImplementedError) as e:
                results.append({"campaignId": cid, "ok": False, "reason": str(e)})
                continue
            reason = summary.get("halt_reason")
            if reason:
                kind = summary.get("halt_kind")
                results.append({"campaignId": cid, "ok": False, "halted": True,
                                "reason": reason, "haltKind": kind})
                if kind in _POISON_HALT_KINDS:
                    cdp_poisoned = True
            else:
                results.append({"campaignId": cid, "ok": True,
                                "matches": summary.get("matches", 0),
                                "spendUsd": summary.get("spend_usd", 0.0)})
    finally:
        store.close()

    ran = sum(1 for r in results if not r.get("skipped"))
    ok = sum(1 for r in results if r.get("ok"))
    n_halted = sum(1 for r in results if r.get("halted"))
    (log.warning if n_halted else log.success)(
        "Batch complete · ran=%d ok=%d halted=%d", ran, ok, n_halted)
    print(json.dumps({"ran": ran, "ok": ok, "halted": n_halted, "results": results},
                     indent=2, ensure_ascii=False))
    return 3 if n_halted else 0


def cmd_panel(args: argparse.Namespace) -> int:
    from .server import PortInUseError, serve
    panel_dir = Path(args.panel_dir)
    if not (panel_dir / "index.html").exists():
        print(f"error: no index.html under {panel_dir} — build the panel first "
              "(cd admin-panel && npm run build), or pass --panel-dir",
              file=sys.stderr)
        return 2
    # The landing (index.html) and the SPA shell (app/index.html) are two separate
    # Vite entries as of the aizu.uz split; a multi-entry misconfiguration can emit
    # one without the other, which would otherwise only surface as a silent 404/500
    # the first time someone hits /app/ in production.
    if not (panel_dir / "app" / "index.html").exists():
        print(f"error: no app/index.html under {panel_dir} — build the panel first "
              "(cd admin-panel && npm run build), or pass --panel-dir",
              file=sys.stderr)
        return 2
    try:
        httpd = serve(args.db, str(panel_dir), args.config,
                      host=args.host, port=args.port)
    except PortInUseError as e:
        # The most common failure of the documented start command (a stale bridge from
        # an unclean exit). serve() already refuses to touch the DB or start a daemon
        # before it binds, so there is nothing to clean up — print it in the same
        # one-line `error: …` style the missing-panel-build checks above use, rather
        # than letting a traceback full of absolute paths out.
        print(f"error: {e}", file=sys.stderr)
        return 2
    url = f"http://{args.host}:{args.port}/"
    log.success("Panel serving %s at %s (db=%s)", panel_dir, url, args.db)
    print(f"AIZU panel serving {panel_dir} at {url}")
    print("Live data generated from", args.db, "— Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
        httpd.shutdown()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = Store(args.db)
    flags = store.open_flags()
    print("Open health flags:")
    if not flags:
        print("  (none)")
    for f in flags:
        print(f"  [{f['severity']}] {f['kind']}: {f['detail']}")
    store.close()
    return 0


def _verify_warm_identity(args: argparse.Namespace) -> Optional[str]:
    """Best-effort liveness check before registering a warming account (PRD §6.5).

    Attaches over CDP to the operator-warmed Chrome and confirms the feed is
    capturing traffic. Returns None on success, else a reason string. NOTE: a full
    logged-in-username assertion is per-platform navigation work (P1); this v1
    check proves the CDP target is reachable + healthy, with the operator having
    logged in manually. Use --skip-verify to register without attaching.
    """
    from .dispatch import build_feed
    feed = None
    try:
        feed = build_feed(args.platform, cdp_url=args.cdp_url,
                          seed_hashtags=(), seed_accounts=(), seed_channels=(),
                          include_home_feed=True)
        if not feed.healthy():
            return ("CDP feed attached but no traffic captured — is the account "
                    "logged in and the page loaded?")
        return None
    except Exception as e:  # noqa: BLE001 — surface a clean operator error
        return f"could not attach over CDP at {args.cdp_url}: {e}"
    finally:
        _close_feed(feed) if feed is not None else None


def cmd_warm_register(args: argparse.Namespace) -> int:
    """Register a managed account into the warming pool (creation is out of scope —
    the operator logs in once, this records the logged-in identity; PRD §6.5)."""
    if not accounts_lib.is_warmable(args.platform):
        print(f"error: platform {args.platform!r} is not warmable; expected one of "
              f"{', '.join(sorted(accounts_lib.WARMABLE_PLATFORMS))}", file=sys.stderr)
        return 2
    if not args.skip_verify:
        reason = _verify_warm_identity(args)
        if reason:
            print(f"error: identity verification failed: {reason}\n"
                  "  (pass --skip-verify to register without attaching)", file=sys.stderr)
            return 2
    store = Store(args.db)
    try:
        fingerprint = {"timezone_id": args.timezone} if args.timezone else None
        aid = store.add_account(args.org, args.platform, args.username,
                                profile_dir=args.chrome_profile, cdp_port=args.cdp_port,
                                fingerprint=fingerprint)
        if args.proxy:
            store.put_account_secret(args.org, args.platform, aid, {"proxy": args.proxy})
    finally:
        store.close()
    log.success("Registered warming account · id=%s @%s (%s)", aid, args.username,
                args.platform)
    print(json.dumps({"accountId": aid, "platform": args.platform,
                      "username": args.username, "state": accounts_lib.PROVISIONED},
                     indent=2, ensure_ascii=False))
    return 0


def _add_log_args(sp: argparse.ArgumentParser) -> None:
    """Verbosity toggles available on every subcommand (flag beats env)."""
    g = sp.add_mutually_exclusive_group()
    g.add_argument("-v", "--verbose", action="store_true",
                   help="DEBUG firehose: per-comment scoring, model latency, every DB write")
    g.add_argument("-q", "--quiet", action="store_true",
                   help="only warnings and errors on the console")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aizu", description="Reel-Comment Discovery Agent")
    p.add_argument("--db", default=str(DEFAULT_DB))
    sub = p.add_subparsers(dest="cmd", required=True)

    def _add_run_args(sp: argparse.ArgumentParser) -> None:
        _add_log_args(sp)
        sp.add_argument("--config", default=str(DEFAULT_CONFIG))
        sp.add_argument("--dry-run", action="store_true",
                        help="use the built-in fake feed + mock router")
        sp.add_argument("--target-leads", type=int, default=None,
                        help="keep running back-to-back live sessions until N leads "
                             "(matched comments) are found, then stop (campaign scope; "
                             "ignored for --dry-run, which does one session)")
        sp.add_argument("--duration-minutes", type=int, default=None,
                        help="optional wall-clock safety cap (minutes): stop once it "
                             "elapses even if --target-leads hasn't been reached; on its "
                             "own it is the legacy timed run (ignored for --dry-run)")
        sp.add_argument("--engine-mode", choices=("harvest", "warming"), default=None,
                        help="harvest (read-only lead discovery; default) or warming "
                             "(ramp/sustain a managed account). Overrides the campaign "
                             "brief's engine_mode when set (warming PRD §4.2)")
        sp.add_argument("--stt", action=argparse.BooleanOptionalAction, default=None,
                        help="force Uzbek STT transcription on/off for this run "
                             "(overrides the campaign brief's enable_stt; still "
                             "requires AIZU_STT_ENABLED + AIZU_STT_MODEL_PATH and "
                             "\"uz\" in language_mix to actually fire — Instagram only)")
        sp.add_argument("--cdp-url",
                        default=os.environ.get("AIZU_CDP_URL", DEFAULT_CDP_URL))
        sp.add_argument("--spend-cap", type=float, default=20.0, help="USD cloud spend cap")
        sp.add_argument("--text-model",
                        default=os.environ.get("OPENROUTER_TEXT_MODEL", "openrouter/owl-alpha"))
        sp.add_argument("--vision-model",
                        default=os.environ.get("OPENROUTER_VISION_MODEL", "nex-agi/nex-n2-pro:free"))

    r = sub.add_parser("run", help="run a discovery session")
    r.add_argument("--campaign", default=None,
                   help="run a panel-authored campaign brief by id (from the DB) "
                        "instead of config/campaign.md")
    _add_run_args(r)
    r.set_defaults(func=cmd_run)

    ra = sub.add_parser("run-all", help="run every live campaign sequentially")
    ra.add_argument("--org", type=int, default=None,
                    help="scope the batch to one org's live campaigns (v7 multi-tenancy)")
    _add_run_args(ra)
    ra.set_defaults(func=cmd_run_all)

    s = sub.add_parser("status", help="print open health flags")
    _add_log_args(s)
    s.set_defaults(func=cmd_status)

    wr = sub.add_parser("warm-register",
                        help="register a logged-in account into the warming pool")
    _add_log_args(wr)
    wr.add_argument("--org", type=int, required=True, help="owning org id")
    wr.add_argument("--platform", required=True,
                    help="warmable platform (x | linkedin | instagram)")
    wr.add_argument("--username", required=True, help="the logged-in account handle")
    wr.add_argument("--chrome-profile", default=None,
                    help="Chrome --user-data-dir bound to this account (profile_dir)")
    wr.add_argument("--cdp-port", type=int, default=None,
                    help="per-account CDP debug port (BASE_PORT + ordinal)")
    wr.add_argument("--proxy", default=None,
                    help="residential sticky proxy URL (stored encrypted)")
    wr.add_argument("--timezone", default=None,
                    help="account timezone id (e.g. America/New_York or UTC) for the daytime guard")
    wr.add_argument("--cdp-url",
                    default=os.environ.get("AIZU_CDP_URL", DEFAULT_CDP_URL))
    wr.add_argument("--skip-verify", action="store_true",
                    help="register without attaching over CDP to confirm the login")
    wr.set_defaults(func=cmd_warm_register)

    pn = sub.add_parser("panel",
                        help="serve the admin panel against the DB (reads + status-mark writes)")
    _add_log_args(pn)
    pn.add_argument("--config", default=str(DEFAULT_CONFIG))
    pn.add_argument("--panel-dir", default="../admin-panel/dist",
                    help="path to the built React panel (admin-panel/dist)")
    pn.add_argument("--host", default="127.0.0.1")
    pn.add_argument("--port", type=int, default=8765)
    pn.set_defaults(func=cmd_panel)
    return p


def main(argv: list[str] | None = None) -> int:
    _load_env()
    args = build_parser().parse_args(argv)
    level = "DEBUG" if getattr(args, "verbose", False) else (
        "WARNING" if getattr(args, "quiet", False) else None)
    configure_logging(level=level)
    log.debug("Dispatch · cmd=%s db=%s", getattr(args, "cmd", "?"), args.db)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
