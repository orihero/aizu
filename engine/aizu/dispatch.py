"""Engine dispatch — pick the per-platform engine for a run.

The control plane (``cli``) builds the ``(router, feed, pacer)`` triple and hands
it here; ``select_engine`` maps ``campaign.platform`` to that platform's
``run_session`` entrypoint (see ``engines.base.EngineProtocol``) and
``run_engine_session`` invokes it. This is the ONE place that knows the
platform→engine mapping; adding a platform means adding a branch here plus the
engine package, with no change to ``cli``/``runner``/``server``.

Each engine owns its own loop, content model, access adapter, pacing/engagement/
halt policy, and scoring — they share only ``aizu.core`` (transport, store,
brief parsing, the FeedSource content interface).
"""
from __future__ import annotations

from typing import Callable, Optional, Sequence

from .core.config import SUPPORTED_PLATFORMS
from .core.feed import FeedSource
from .core.logsetup import get_logger

log = get_logger(__name__)


def build_feed(platform: str, *, cdp_url: str,
               seed_hashtags: Sequence[str] = (),
               seed_accounts: Sequence[str] = (),
               seed_channels: Sequence[str] = (),
               include_home_feed: bool = True,
               credentials: Optional[dict] = None) -> FeedSource:
    """Construct and attach the live FeedSource for `platform`.

    Each platform's feed adapter lives in its engine package; this factory is the
    control-plane wiring that picks one by platform. Raises ValueError for an
    unknown platform and NotImplementedError for a supported-but-not-yet-built one,
    so a misconfigured run fails loudly. `include_home_feed` is Instagram-only.

    `credentials` is the per-org stored secret (YT/TG). When provided it is used;
    when None the client falls back to `from_env` — so local single-tenant runs
    and Instagram (which has no per-org secret) keep working unchanged.
    """
    log.info("Building %s feed · hashtags=%d accounts=%d channels=%d home=%s%s",
             platform, len(seed_hashtags), len(seed_accounts), len(seed_channels),
             include_home_feed, " · per-org creds" if credentials else "")
    if platform == "instagram":
        from .engines.instagram.cdp import CDPConfig, CDPFeed
        feed: FeedSource = CDPFeed(CDPConfig(
            cdp_url=cdp_url,
            seed_hashtags=tuple(seed_hashtags),
            seed_accounts=tuple(seed_accounts),
            include_home_feed=include_home_feed,
        ))
        feed.attach()  # connect_over_cdp to the warmed Chrome
        return feed

    if platform == "youtube":
        from .engines.youtube.feed import YouTubeDataApiClient, YouTubeFeed
        if not (seed_accounts or seed_hashtags or seed_channels):
            raise ValueError(
                "youtube needs seed_channels (channel ids) and/or seed_hashtags "
                "(search queries) in campaign.md to discover videos")
        # YouTube channels live in seed_channels (telegram-style) or seed_accounts;
        # seed_hashtags are reused as search queries.
        channels = tuple(seed_channels) or tuple(seed_accounts)
        client = (YouTubeDataApiClient.from_credentials(credentials) if credentials
                  else YouTubeDataApiClient.from_env())
        feed = YouTubeFeed(client=client,
                           channels=channels, queries=tuple(seed_hashtags))
        feed.attach()
        return feed

    if platform == "telegram":
        from .engines.telegram.feed import TelegramFeed, TelethonClient
        if not seed_channels:
            raise ValueError(
                "telegram needs seed_channels in campaign.md (deterministic "
                "discovery — there is no algorithmic feed)")
        # MTProto user session: reads any public channel by @username, no admin
        # step and with history. The Bot API can only see channels the bot is an
        # admin of, and only new messages — so it can't monitor third-party channels.
        client = (TelethonClient.from_credentials(credentials) if credentials
                  else TelethonClient.from_env())
        feed = TelegramFeed(client=client, channels=tuple(seed_channels))
        feed.attach()
        return feed

    if platform == "reddit":
        from .engines.reddit.feed import RedditDataApiClient, RedditFeed
        if not seed_channels:
            raise ValueError(
                "reddit needs seed_channels (subreddits) in campaign.md — "
                "deterministic discovery, there is no algorithmic feed")
        # subreddits live in seed_channels (telegram-style deterministic seed);
        # seed_hashtags are reused as per-subreddit search queries.
        client = (RedditDataApiClient.from_credentials(credentials) if credentials
                  else RedditDataApiClient.from_env())
        feed = RedditFeed(client=client, subreddits=tuple(seed_channels),
                          queries=tuple(seed_hashtags))
        feed.attach()  # pre-mint the OAuth token (early, clean 401 canary)
        return feed

    if platform == "linkedin":
        from .engines.linkedin.cdp import LinkedInCDPConfig, LinkedInFeed
        # Managed-CDP, like Instagram: warmed Chrome, no per-org secret. Walks the
        # home feed + seeded hashtags + people/companies (include_home_feed toggle).
        feed = LinkedInFeed(LinkedInCDPConfig(
            cdp_url=cdp_url,
            seed_hashtags=tuple(seed_hashtags),
            seed_accounts=tuple(seed_accounts),
            include_home_feed=include_home_feed,
        ))
        feed.attach()  # connect_over_cdp to the warmed Chrome
        return feed

    if platform == "x":
        from .engines.x.cdp import XCDPConfig, XFeed
        # Managed-CDP, like Instagram: warmed Chrome, no per-org secret. Walks
        # For You + Search + Lists (include_home_feed toggles the For You feed).
        feed = XFeed(XCDPConfig(
            cdp_url=cdp_url,
            seed_hashtags=tuple(seed_hashtags),
            seed_accounts=tuple(seed_accounts),
            include_home_feed=include_home_feed,
        ))
        feed.attach()  # connect_over_cdp to the warmed Chrome
        return feed

    if platform in SUPPORTED_PLATFORMS:
        raise NotImplementedError(
            f"{platform} feed is planned but not yet implemented "
            f"(see docs/prd/{platform}-lead-agent-PRD.md). "
            "Run --dry-run to exercise the loop on the fake feed.")

    raise ValueError(
        f"unknown platform {platform!r}; expected one of "
        f"{', '.join(SUPPORTED_PLATFORMS)}")


def select_engine(platform: str) -> Callable[..., dict]:
    """Return the ``run_session`` callable for ``platform``.

    Raises ValueError for an unknown platform and NotImplementedError for a
    supported-but-not-yet-built engine, so a misconfigured run fails loudly.
    """
    if platform == "instagram":
        from .engines.instagram.session import run_session
        return run_session
    if platform == "youtube":
        from .engines.youtube.session import run_session
        return run_session
    if platform == "telegram":
        from .engines.telegram.session import run_session
        return run_session
    if platform == "reddit":
        from .engines.reddit.session import run_session
        return run_session
    if platform == "linkedin":
        from .engines.linkedin.session import run_session
        return run_session
    if platform == "x":
        from .engines.x.session import run_session
        return run_session
    if platform in SUPPORTED_PLATFORMS:
        raise NotImplementedError(
            f"{platform} engine is planned but not yet implemented")
    raise ValueError(
        f"unknown platform {platform!r}; expected one of "
        f"{', '.join(SUPPORTED_PLATFORMS)}")


def run_engine_session(*, campaign, store, router, feed, soul, pacer,
                       run_id: Optional[str] = None,
                       lead_target: Optional[int] = None,
                       engine_mode: str = "harvest") -> dict:
    """Select the engine for ``campaign.platform`` and run one session.

    Returns the engine's summary dict (uniform keys across engines; halts are
    folded into ``halt_reason`` by the engine). Feed construction and teardown
    stay with the caller (``cli._run_one``) so the multi-session loop can attach
    a fresh feed per session.

    ``engine_mode='warming'`` routes to the warming engine BEFORE the harvest
    fan-out, so the harvest call below stays byte-for-byte unchanged — the
    strongest guarantee read-only harvest survives (warming PRD §4.1). Only
    ``run_warming_session`` is new; the six harvest signatures are untouched.
    """
    if engine_mode == "warming":
        from .engines.warming.session import run_warming_session
        log.debug("Dispatching WARMING engine · platform=%s campaign=%s",
                  campaign.platform, campaign.campaign_id)
        return run_warming_session(campaign=campaign, store=store, router=router,
                                   feed=feed, soul=soul, pacer=pacer, run_id=run_id)
    run_session = select_engine(campaign.platform)
    log.debug("Dispatching engine · platform=%s campaign=%s",
              campaign.platform, campaign.campaign_id)
    return run_session(campaign=campaign, store=store, router=router, feed=feed,
                       soul=soul, pacer=pacer, run_id=run_id,
                       lead_target=lead_target)
