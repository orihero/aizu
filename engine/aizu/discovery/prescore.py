"""Seed-account prescoring — is this account worth a warmed browser's time?

Campaign Lab, Remedy Sheet #2 / Remedy C. Expansion and mining
(`Store.seed_candidates`) produce candidate accounts; this decides which of them
are alive and commerce-active, at ONE request per candidate and — on YouTube,
Telegram and the anonymous Instagram path — zero warmed-account exposure.

The unit of identity here is the STABLE ID, never the handle: IG `pk`, YouTube
`UC…`, X `rest_id`, LinkedIn URN. A rename then reads as "same seed, new handle",
and a 404 on the ID is the true death signal rather than an ambiguous one.
Telegram is the documented exception — its public surface exposes no stable id, so
a rename genuinely is a new seed.

What is deliberately NOT here, and why:
  * X — profile reads are login-walled and attributable, and the GraphQL queryIds
    rotate every 2-4 weeks. The research's verdict is that the right amount of
    pre-validation is zero; X seeds are judged by per-source yield instead.
  * LinkedIn — validating a PERSONAL profile costs a direct-profile visit
    (~50/day before detection risk, against ~2,000 search-result rows). Company
    pages and search snippets only; there is no cheap probe worth writing.
  * Reddit — no free anonymous path since the `.json` API died (~May 2026);
    needs a grandfathered OAuth app, which is an operator credential decision.

Every probe returns an `AccountProfile` and never raises. An unreachable probe
yields `checked=False`, which the gate reports as `unknown` — not as a pass.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from ..core.logsetup import get_logger

log = get_logger(__name__)

DAY = 86400.0

# Gate verdicts.
ALIVE = "alive"        # posting recently and at a healthy cadence
STALE = "stale"        # exists, but not posting enough to seed a campaign
DEAD = "dead"          # gone (404 on the stable id)
UNKNOWN = "unknown"    # not checked — never presentable as a pass


@dataclass
class AccountProfile:
    """What one probe could learn about a candidate account.

    Every numeric field defaults to 0/None meaning "not reported", never "zero" —
    the gate distinguishes the two, because a platform that does not publish a
    follower count must not make an account look like it has no followers.
    """
    seed: str                      # what the operator typed / what we mined
    platform: str
    stable_id: str = ""            # the id everything downstream should key on
    handle: str = ""
    exists: bool = True
    checked: bool = False          # did the probe actually get an answer?
    private: bool = False
    followers: Optional[int] = None
    following: Optional[int] = None
    posts: Optional[int] = None
    recent_post_ats: list[float] = field(default_factory=list)   # epoch seconds
    recent_likes: list[int] = field(default_factory=list)
    recent_comments: list[int] = field(default_factory=list)
    has_discussion: Optional[bool] = None   # Telegram: a linked comments group
    category: str = ""
    detail: str = ""

    # ---- derived, never stored (core/warmth.py's pattern) ----
    @property
    def last_post_age_days(self) -> Optional[float]:
        if not self.recent_post_ats:
            return None
        return max(0.0, (time.time() - max(self.recent_post_ats)) / DAY)

    def posts_in(self, days: float) -> int:
        cutoff = time.time() - days * DAY
        return sum(1 for t in self.recent_post_ats if t >= cutoff)

    @property
    def follower_ratio(self) -> Optional[float]:
        if not self.followers or self.following is None:
            return None
        return self.followers / max(1, self.following)

    @property
    def engagement_rate(self) -> Optional[float]:
        """Mean (likes + comments) per post ÷ followers. None when either side is
        unreported — an unknown ER must not read as a zero one."""
        if not self.followers or not self.recent_likes:
            return None
        n = len(self.recent_likes)
        likes = sum(self.recent_likes)
        comments = sum(self.recent_comments[:n]) if self.recent_comments else 0
        return (likes + comments) / n / self.followers

    @property
    def comment_like_ratio(self) -> Optional[float]:
        """Comments per like. THE metric that matters for this engine: leads live
        in comment sections, so a post with 500 likes and 3 comments is worth less
        to us than one with 50 likes and 30 comments."""
        if not self.recent_likes or not self.recent_comments:
            return None
        likes = sum(self.recent_likes)
        if likes <= 0:
            return None
        return sum(self.recent_comments) / likes

    def as_dict(self) -> dict[str, Any]:
        return {"seed": self.seed, "platform": self.platform,
                "stableId": self.stable_id, "handle": self.handle,
                "exists": self.exists, "checked": self.checked,
                "private": self.private, "followers": self.followers,
                "following": self.following, "posts": self.posts,
                "lastPostAgeDays": self.last_post_age_days,
                "postsLast30d": self.posts_in(30),
                "engagementRate": self.engagement_rate,
                "commentLikeRatio": self.comment_like_ratio,
                "hasDiscussion": self.has_discussion,
                "category": self.category, "detail": self.detail}


# ---------------------------------------------------------------------------
# Liveness gate
# ---------------------------------------------------------------------------

# Synthesized aizu policy (Remedy Sheet #2). Each threshold is checked ONLY when
# the platform actually reported the input — an unreported signal is never a
# failure, or every Telegram channel would fail the follower-ratio test that
# Telegram does not publish.
MAX_LAST_POST_DAYS = 14.0
MIN_POSTS_PER_30D = 3
MIN_FOLLOWER_RATIO = 2.0
# Published account-level median ER is ~0.45%; >1% is above average. A floor here
# would reject most legitimate mid-size accounts, so this is a FLOOR ON NOISE, not
# a quality bar — the buyer-density score is what actually ranks accounts.
MIN_ENGAGEMENT_RATE = 0.001
# Leads live in comments. Below this the comment section is too thin to harvest.
MIN_COMMENT_LIKE_RATIO = 0.01


@dataclass
class GateVerdict:
    verdict: str
    reasons: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """`unknown` is usable: we have no evidence AGAINST the account, and
        dropping it on a failed probe would silently narrow a campaign."""
        return self.verdict in (ALIVE, UNKNOWN)


def liveness_gate(profile: AccountProfile) -> GateVerdict:
    """Is this account alive enough to seed a campaign?

    Fails only on signals the platform actually reported (see the constants
    above). Order matters: existence beats everything, and "we could not check"
    is reported honestly rather than as a pass."""
    if not profile.checked:
        return GateVerdict(UNKNOWN, [profile.detail or "not checked"])
    if not profile.exists:
        return GateVerdict(DEAD, ["no such account"])
    reasons: list[str] = []
    age = profile.last_post_age_days
    if age is not None and age > MAX_LAST_POST_DAYS:
        reasons.append(f"last post {age:.0f}d ago (>{MAX_LAST_POST_DAYS:.0f}d)")
    if profile.recent_post_ats:
        n30 = profile.posts_in(30)
        if n30 < MIN_POSTS_PER_30D:
            reasons.append(f"{n30} post(s) in 30d (<{MIN_POSTS_PER_30D})")
    ratio = profile.follower_ratio
    if ratio is not None and ratio < MIN_FOLLOWER_RATIO:
        reasons.append(f"follower/following {ratio:.1f} (<{MIN_FOLLOWER_RATIO:.0f})")
    er = profile.engagement_rate
    if er is not None and er < MIN_ENGAGEMENT_RATE:
        reasons.append(f"engagement {er:.2%} (<{MIN_ENGAGEMENT_RATE:.1%})")
    clr = profile.comment_like_ratio
    if clr is not None and clr < MIN_COMMENT_LIKE_RATIO:
        reasons.append(f"comments/likes {clr:.1%} (<{MIN_COMMENT_LIKE_RATIO:.0%}) "
                       "— thin comment section")
    # A private account can still be size-scored, but its comments are unreachable,
    # so it can never be a harvest seed.
    if profile.private:
        reasons.append("private — comments unreachable")
    return GateVerdict(STALE if reasons else ALIVE, reasons)


# ---------------------------------------------------------------------------
# Instagram — one XHR from the warmed session (or anonymously)
# ---------------------------------------------------------------------------

IG_PROFILE_URL = ("https://i.instagram.com/api/v1/users/web_profile_info/"
                  "?username=%s")
IG_APP_ID = "936619743392459"
# Single-digit reads per minute, jittered. Reads do not trip write action-blocks
# but they do count toward the same rate accounting (Instaloader encodes 199
# api/v1 requests per 1800s as the safe ceiling).
IG_MIN_INTERVAL_SECONDS = 8.0


class InstagramProfileProbe:
    """`web_profile_info` — the same XHR the web app fires when you open a
    profile, so it is the lowest-risk read available from a warmed session.

    401/403/429 are a statement about OUR SESSION, not about the account: they
    stop the sweep rather than marking candidates dead.
    """

    def __init__(self, feed: Any, *, min_interval: float = IG_MIN_INTERVAL_SECONDS,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self._feed = feed
        self._min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        self._last_at: Optional[float] = None
        self.session_unhealthy = False

    def _pace(self) -> None:
        if self._last_at is None:
            return
        wait = self._min_interval - (self._clock() - self._last_at)
        if wait > 0:
            self._sleep(wait)

    def _fetch(self, handle: str) -> Optional[dict]:
        page = getattr(self._feed, "_page", None)
        if page is None:
            return None
        url = IG_PROFILE_URL % urllib.parse.quote(handle)
        script = ("async (u) => { const r = await fetch(u, "
                  "{credentials: 'include', headers: {'x-ig-app-id': '" + IG_APP_ID
                  + "'}}); return r.ok ? await r.text() : 'HTTP:' + r.status; }")
        raw = page.evaluate(script, url)
        if not isinstance(raw, str):
            return None
        if raw.startswith("HTTP:"):
            code = raw[5:]
            if code == "404":
                return {}                       # genuinely gone
            if code in ("401", "403", "429"):
                self.session_unhealthy = True
            raise RuntimeError(f"web_profile_info HTTP {code}")
        return json.loads(raw)

    def probe(self, seed: str) -> AccountProfile:
        handle = str(seed or "").strip().lstrip("@").strip("/")
        prof = AccountProfile(seed=seed, platform="instagram", handle=handle)
        if not handle:
            prof.detail = "empty seed"
            return prof
        if self.session_unhealthy:
            prof.detail = "session flagged unhealthy; sweep stopped"
            return prof
        self._pace()
        try:
            body = self._fetch(handle)
        except Exception as e:  # noqa: BLE001 — a probe must never end a run
            log.debug("IG web_profile_info failed for %r", handle, exc_info=True)
            prof.detail = str(e)[:200]
            return prof
        finally:
            self._last_at = self._clock()
        if body is None:
            prof.detail = "no page attached"
            return prof
        return _ig_profile(seed, handle, body)

    def probe_many(self, seeds: Sequence[str]) -> list[AccountProfile]:
        return [self.probe(s) for s in seeds]


def _ig_profile(seed: str, handle: str, body: dict) -> AccountProfile:
    """Read a `web_profile_info` body. Defensive at every index — the shape drifts
    and a missing wrapper must degrade a field, not the probe."""
    user = ((body or {}).get("data") or {}).get("user")
    if not isinstance(user, dict):
        # An empty/200-with-no-user body is how a deleted account reads.
        return AccountProfile(seed=seed, platform="instagram", handle=handle,
                              exists=False, checked=True, detail="no such account")
    prof = AccountProfile(seed=seed, platform="instagram", handle=handle,
                          checked=True, exists=True)
    prof.stable_id = str(user.get("id") or "")
    prof.private = bool(user.get("is_private"))
    prof.followers = _count(user, "edge_followed_by")
    prof.following = _count(user, "edge_follow")
    prof.posts = _count(user, "edge_owner_to_timeline_media")
    prof.category = str(user.get("category_name") or "")
    media = ((user.get("edge_owner_to_timeline_media") or {}).get("edges") or [])
    for edge in media:
        node = edge.get("node") if isinstance(edge, dict) else None
        if not isinstance(node, dict):
            continue
        ts = node.get("taken_at_timestamp")
        if isinstance(ts, (int, float)):
            prof.recent_post_ats.append(float(ts))
        likes = _count(node, "edge_liked_by") or _count(node, "edge_media_preview_like")
        comments = _count(node, "edge_media_to_comment")
        if likes is not None:
            prof.recent_likes.append(likes)
        if comments is not None:
            prof.recent_comments.append(comments)
    return prof


def _count(node: dict, key: str) -> Optional[int]:
    """`node[key].count`, or None when absent — never 0-as-unknown."""
    sub = node.get(key)
    if isinstance(sub, dict) and isinstance(sub.get("count"), (int, float)):
        return int(sub["count"])
    return None


# ---------------------------------------------------------------------------
# YouTube — 50 channels per quota unit
# ---------------------------------------------------------------------------

# `channels.list` bills 1 unit for up to 50 ids, and `playlistItems.list` on the
# uploads playlist bills 1 more. NEVER `search.list` (100 units, and since June
# 2026 one of only 100 searches per day).
YT_CHANNELS_PER_CALL = 50
YT_UPLOADS_SAMPLE = 10


class YouTubeChannelProbe:
    """Score up to 50 channels per quota unit via `channels.list`, then sample the
    uploads playlist for cadence. ~13 units fully scores 10 channels — 0.13% of a
    day's 10k pool, and none of the separate search bucket."""

    def __init__(self, client: Any):
        self._client = client

    def probe_many(self, seeds: Sequence[str]) -> list[AccountProfile]:
        ids = [str(s).strip() for s in seeds if str(s).strip()]
        if not ids:
            return []
        out: list[AccountProfile] = []
        for start in range(0, len(ids), YT_CHANNELS_PER_CALL):
            batch = ids[start:start + YT_CHANNELS_PER_CALL]
            out.extend(self._probe_batch(batch))
        return out

    def probe(self, seed: str) -> AccountProfile:
        got = self.probe_many([seed])
        return got[0] if got else AccountProfile(seed=seed, platform="youtube")

    def _probe_batch(self, ids: Sequence[str]) -> list[AccountProfile]:
        try:
            body = self._client._get("channels", {
                "part": "id,statistics,contentDetails,brandingSettings",
                "id": ",".join(ids), "maxResults": YT_CHANNELS_PER_CALL})
        except Exception as e:  # noqa: BLE001 — quota/network degrade to unknown
            log.debug("YouTube channels.list failed", exc_info=True)
            return [AccountProfile(seed=s, platform="youtube", detail=str(e)[:200])
                    for s in ids]
        found: dict[str, dict] = {}
        for item in (body.get("items") or []):
            if isinstance(item, dict) and item.get("id"):
                found[str(item["id"])] = item
        out: list[AccountProfile] = []
        for seed in ids:
            item = found.get(seed)
            if item is None:
                # An id absent from `items[]` does not exist. This is the signal
                # that used to be indistinguishable from a quiet channel.
                out.append(AccountProfile(seed=seed, platform="youtube",
                                          exists=False, checked=True,
                                          detail="no such channel"))
                continue
            out.append(self._one(seed, item))
        return out

    def _one(self, seed: str, item: dict) -> AccountProfile:
        stats = item.get("statistics") or {}
        prof = AccountProfile(seed=seed, platform="youtube", checked=True,
                              stable_id=str(item.get("id") or ""))
        prof.handle = str(((item.get("brandingSettings") or {}).get("channel")
                           or {}).get("title") or "")
        prof.followers = _int(stats.get("subscriberCount"))
        prof.posts = _int(stats.get("videoCount"))
        # `hiddenSubscriberCount` means the number is withheld, not zero.
        if stats.get("hiddenSubscriberCount"):
            prof.followers = None
        uploads = (((item.get("contentDetails") or {}).get("relatedPlaylists") or {})
                   .get("uploads"))
        if uploads:
            prof.recent_post_ats = self._upload_dates(str(uploads))
        return prof

    def _upload_dates(self, playlist_id: str) -> list[float]:
        """Publication timestamps of the latest uploads (1 quota unit)."""
        try:
            body = self._client._get("playlistItems", {
                "part": "snippet", "playlistId": playlist_id,
                "maxResults": YT_UPLOADS_SAMPLE})
        except Exception:  # noqa: BLE001 — cadence is optional, existence is not
            log.debug("YouTube playlistItems failed for %s", playlist_id, exc_info=True)
            return []
        out: list[float] = []
        for item in (body.get("items") or []):
            sn = item.get("snippet") if isinstance(item, dict) else None
            ts = _iso_to_epoch((sn or {}).get("publishedAt"))
            if ts is not None:
                out.append(ts)
        return out


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})")


def _iso_to_epoch(value: Any) -> Optional[float]:
    """Parse an RFC-3339 UTC timestamp without pulling in a date library."""
    if not isinstance(value, str):
        return None
    m = _ISO_RE.match(value.strip())
    if not m:
        return None
    import calendar
    return float(calendar.timegm(tuple(int(g) for g in m.groups()) + (0, 0, 0)))


# ---------------------------------------------------------------------------
# Telegram — the unauthenticated t.me/s/ preview
# ---------------------------------------------------------------------------

TG_PREVIEW_URL = "https://t.me/s/%s"
_TG_SUBSCRIBERS_RE = re.compile(
    r'tgme_(?:channel|page)_extra"[^>]*>\s*([\d\s ,\.]+)\s*(?:subscribers|members)',
    re.I)
_TG_VIEWS_RE = re.compile(r'tgme_widget_message_views"[^>]*>([^<]+)<', re.I)
_TG_TIME_RE = re.compile(r'<time[^>]+datetime="([^"]+)"', re.I)
_TG_COMMENTS_RE = re.compile(r'tgme_widget_message_(?:footer_)?comments?', re.I)


def _tg_handle(seed: str) -> str:
    """Normalise every form an operator pastes down to a bare @username.

    `@growthlab`, `growthlab`, `t.me/growthlab`, `https://t.me/s/growthlab` and
    the trailing-slash variants all name the same channel; feeding an un-stripped
    URL back into the preview URL builds `t.me/s/https%3A//t.me/s/...`, which 404s
    and would have read as a dead seed."""
    text = str(seed or "").strip()
    for prefix in ("https://", "http://"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.split("?", 1)[0].split("#", 1)[0].strip("/")
    parts = [p for p in text.split("/") if p]
    if parts and parts[0].lower() in ("t.me", "telegram.me", "telegram.dog"):
        parts = parts[1:]
    if parts and parts[0].lower() == "s":       # the /s/ preview path
        parts = parts[1:]
    return parts[0].lstrip("@") if parts else ""


class TelegramPreviewProbe:
    """`t.me/s/<channel>` — free, unauthenticated, zero account exposure.

    Telegram is the documented stable-id exception: the public surface exposes no
    numeric id, so the @username IS the key and a rename genuinely is a new seed.

    The signal unique to this platform: a "Comments" control under posts means the
    channel has a LINKED DISCUSSION GROUP — i.e. it has reachable commenters at
    all, which for a lead engine is categorically better than raw subscriber
    count."""

    def __init__(self, *, opener: Optional[Callable[[str], bytes]] = None,
                 min_interval: float = 2.0,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self._opener = opener
        self._min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        self._last_at: Optional[float] = None

    def _fetch(self, url: str) -> bytes:
        if self._opener is not None:
            return self._opener(url)
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            return resp.read()

    def probe(self, seed: str) -> AccountProfile:
        handle = _tg_handle(seed)
        prof = AccountProfile(seed=seed, platform="telegram", handle=handle,
                              stable_id=handle)
        if not handle:
            prof.detail = "empty seed"
            return prof
        if self._last_at is not None:
            wait = self._min_interval - (self._clock() - self._last_at)
            if wait > 0:
                self._sleep(wait)
        try:
            raw = self._fetch(TG_PREVIEW_URL % urllib.parse.quote(handle))
        except Exception as e:  # noqa: BLE001 — unreachable ≠ dead
            log.debug("t.me preview failed for %r", handle, exc_info=True)
            prof.detail = str(e)[:200]
            return prof
        finally:
            self._last_at = self._clock()
        return _tg_profile(prof, raw.decode("utf-8", errors="replace"))

    def probe_many(self, seeds: Sequence[str]) -> list[AccountProfile]:
        return [self.probe(s) for s in seeds]


def _tg_profile(prof: AccountProfile, html: str) -> AccountProfile:
    prof.checked = True
    # A preview page for a nonexistent/private channel carries no message widgets
    # and no subscriber line.
    m = _TG_SUBSCRIBERS_RE.search(html)
    if m:
        digits = re.sub(r"[^\d]", "", m.group(1))
        prof.followers = int(digits) if digits else None
    prof.recent_post_ats = [t for t in
                            (_iso_to_epoch(v) for v in _TG_TIME_RE.findall(html))
                            if t is not None]
    prof.has_discussion = bool(_TG_COMMENTS_RE.search(html))
    if prof.followers is None and not prof.recent_post_ats:
        prof.exists = False
        prof.detail = "no public preview (private, empty or nonexistent)"
    return prof


def probe_for(platform: str, **kw) -> Optional[Any]:
    """The prescore probe for `platform`, or None where the research says the
    correct amount of probing is zero (X, LinkedIn) or there is no free path
    (Reddit)."""
    if platform == "instagram" and kw.get("feed") is not None:
        return InstagramProfileProbe(kw["feed"])
    if platform == "youtube" and kw.get("client") is not None:
        return YouTubeChannelProbe(kw["client"])
    if platform == "telegram":
        return TelegramPreviewProbe(opener=kw.get("opener"))
    return None
