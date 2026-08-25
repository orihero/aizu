"""Org-wide aggregate builders for the per-page panel endpoints.

Where `panel.build_raw` (`/api/state`) builds ONE campaign's view, these build the
data for ONE Pulse page across EVERY campaign the org owns:

    build_dashboard_org  → /api/dashboard
    build_campaigns_org  → /api/campaigns
    build_leads_org      → /api/leads   (filtered + sorted + paginated)
    build_reports_org    → /api/reports
    build_settings_org   → /api/settings

To avoid duplicating panel.py's period-aggregation logic, the heavy dashboard/report
builders are reused VERBATIM through `_OrgStore` — a read-only proxy that presents the
same per-campaign query interface as `Store` but transparently merges each query across
the org's campaign ids. The wrapped builders call e.g. `store.matches_by_day(cid, ...)`;
the proxy ignores the `cid` they pass and aggregates the same query over `self._cids`,
so single-campaign code yields an org-wide result with no second implementation to drift.

Shape contract: the dicts returned here mirror the matching keys of `panel.build_raw`
and are validated by admin-panel/src/shared/schemas/endpoints.ts (Zod) — keep in sync.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from .core.config import Campaign
from .core.store import WIN_STATUS, Store
from .panel import (TASHKENT, build_config, _build_alerts, _build_billing,
                    _build_campaigns, _build_dashboard, _build_health,
                    _build_integrations, _build_invites, _build_matches,
                    _build_reports, _build_sessions, _build_team, _empty_campaign)
from . import rbac

# Pagination bounds for /api/leads. Default keeps the first page light; the max caps
# a hostile/large pageSize so one request can't pull an unbounded org-wide lead set.
LEADS_PAGE_SIZE_DEFAULT = 50
LEADS_PAGE_SIZE_MAX = 200

# Archived leads are treated as "removed": excluded from every dashboard/report
# aggregate and from the default Leads list, and reachable only by explicitly
# selecting the Archived status filter on the Leads table.
ARCHIVED_STATUS = "archived"


def _active_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The non-archived subset — what every dashboard/aggregate counts."""
    return [m for m in matches if m["status"] != ARCHIVED_STATUS]

# Lead-list sort keys → how to read the sortable value off a built match record.
# v27: an org-facing row has no `username` to sort by — `intent` is the lead's
# only prose. `username` survives for the superadmin lead table (which asks
# `_build_matches` for identity-bearing rows), and reads through `.get` so a
# redacted row sorts as "" instead of turning a stale `?sort=username` from a
# cached client bundle into a KeyError 500.
_LEAD_SORT_KEYS = {
    "capturedAt": lambda m: m["capturedAt"]["ts"],
    "score": lambda m: m["score"],
    "intent": lambda m: (m.get("intent") or "").lower(),
    "username": lambda m: (m.get("username") or "").lower(),
    "platform": lambda m: m["platform"],
    "status": lambda m: m["status"],
}


class _OrgStore:
    """Read proxy over a real `Store` that aggregates each per-campaign query across a
    fixed set of campaign ids. Implements only the query methods that `_build_dashboard`
    and `_build_reports` call; the `campaign_id` they pass positionally is ignored in
    favour of `self._cids`. Read-only — never used for writes."""

    def __init__(self, store: Store, cids: list[str]) -> None:
        self._store = store
        self._cids = cids

    def matches_by_day(self, _cid: str, since_ts: Optional[float] = None,
                       until_ts: Optional[float] = None,
                       platform: Optional[str] = None) -> list[dict[str, Any]]:
        acc: dict[str, int] = {}
        for cid in self._cids:
            for r in self._store.matches_by_day(cid, since_ts, until_ts, platform):
                acc[r["day"]] = acc.get(r["day"], 0) + r["n"]
        return [{"day": d, "n": n} for d, n in sorted(acc.items())]

    def matches_by_hour(self, _cid: str, since_ts: Optional[float] = None,
                        until_ts: Optional[float] = None,
                        platform: Optional[str] = None) -> dict[int, int]:
        acc: dict[int, int] = {}
        for cid in self._cids:
            for hr, n in self._store.matches_by_hour(cid, since_ts, until_ts, platform).items():
                acc[hr] = acc.get(hr, 0) + n
        return acc

    def matches_by_platform(self, _cid: str, since_ts: Optional[float] = None,
                            until_ts: Optional[float] = None) -> dict[str, int]:
        acc: dict[str, int] = {}
        for cid in self._cids:
            for p, n in self._store.matches_by_platform(cid, since_ts, until_ts).items():
                acc[p] = acc.get(p, 0) + n
        return acc

    def won_count(self, _cid: str, since_ts: Optional[float] = None,
                  until_ts: Optional[float] = None,
                  platform: Optional[str] = None) -> int:
        return sum(self._store.won_count(cid, since_ts, until_ts, platform)
                   for cid in self._cids)

    def scored_count(self, _cid: str, since_ts: Optional[float] = None,
                     until_ts: Optional[float] = None) -> int:
        return sum(self._store.scored_count(cid, since_ts, until_ts) for cid in self._cids)

    def spend_by_day(self, _cid: str, since_ts: Optional[float] = None,
                     until_ts: Optional[float] = None) -> list[dict[str, Any]]:
        acc: dict[str, float] = {}
        for cid in self._cids:
            for r in self._store.spend_by_day(cid, since_ts, until_ts):
                acc[r["day"]] = acc.get(r["day"], 0.0) + r["usd"]
        return [{"day": d, "usd": v} for d, v in sorted(acc.items())]

    def spend_by_stage(self, _cid: str, since_ts: Optional[float] = None,
                       until_ts: Optional[float] = None) -> dict[str, float]:
        acc: dict[str, float] = {}
        for cid in self._cids:
            for stage, v in self._store.spend_by_stage(cid, since_ts, until_ts).items():
                acc[stage] = acc.get(stage, 0.0) + v
        return acc

    def funnel_totals(self, _cid: str, since_ts: Optional[float] = None,
                      until_ts: Optional[float] = None) -> dict[str, int]:
        acc = {"reels": 0, "relevant": 0, "scored": 0, "matches": 0}
        for cid in self._cids:
            for k, v in self._store.funnel_totals(cid, since_ts, until_ts).items():
                acc[k] = acc.get(k, 0) + v
        return acc

    def status_breakdown(self, _cid: str, since_ts: Optional[float] = None,
                         until_ts: Optional[float] = None,
                         platform: Optional[str] = None) -> dict[str, int]:
        acc: dict[str, int] = {}
        for cid in self._cids:
            for s, n in self._store.status_breakdown(cid, since_ts, until_ts, platform).items():
                # Archived leads are "removed" — never counted in any dashboard or
                # report aggregate (incl. pipeline_conversion, which reads this).
                if s == ARCHIVED_STATUS:
                    continue
                acc[s] = acc.get(s, 0) + n
        return acc

    def pipeline_conversion(self, _cid: str, since_ts: Optional[float] = None,
                            until_ts: Optional[float] = None,
                            platform: Optional[str] = None) -> dict[str, Any]:
        # Recompute from the merged breakdown — win/engaged RATES can't be summed, only
        # the underlying counts can. Mirrors Store.pipeline_conversion's formula.
        b = self.status_breakdown(_cid, since_ts, until_ts, platform)
        total = sum(b.values())
        won = sum(b.get(s, 0) for s in WIN_STATUS)
        engaged = total - b.get("new", 0)
        lost = b.get("closed", 0) + b.get("couldnt_connect", 0) + b.get("archived", 0)
        return {"total": total, "won": won,
                "winRate": round(won / total, 4) if total else 0.0,
                "engagedRate": round(engaged / total, 4) if total else 0.0,
                "lost": lost}

    def status_changes_by_user(self, _cid: str, since_ts: Optional[float] = None,
                               until_ts: Optional[float] = None) -> list[dict[str, Any]]:
        acc: dict[Any, dict[str, Any]] = {}
        for cid in self._cids:
            for r in self._store.status_changes_by_user(cid, since_ts, until_ts):
                cur = acc.get(r["userId"])
                if cur is not None:
                    cur["changes"] += r["changes"]
                else:
                    acc[r["userId"]] = dict(r)
        return sorted(acc.values(), key=lambda r: r["changes"], reverse=True)

    def needs_attention(self, _cid: str, *, stuck_days: float = 7.0,
                        idle_days: float = 14.0, now: Optional[float] = None,
                        platform: Optional[str] = None) -> dict[str, Any]:
        agg = {"stuckInProgress": 0, "couldntConnectTotal": 0, "noActivity": 0,
               "stuckDays": stuck_days, "idleDays": idle_days}
        for cid in self._cids:
            na = self._store.needs_attention(cid, stuck_days=stuck_days, idle_days=idle_days,
                                             now=now, platform=platform)
            agg["stuckInProgress"] += na["stuckInProgress"]
            agg["couldntConnectTotal"] += na["couldntConnectTotal"]
            agg["noActivity"] += na["noActivity"]
        return agg

    def per_campaign_rollup(self, org_id: Optional[int] = None) -> list[dict[str, Any]]:
        # Already org-wide on the real store; delegate untouched.
        return self._store.per_campaign_rollup(org_id)


def _org_campaign_ids(store: Store, campaign: Campaign, org_id: Optional[int],
                      include_primary: bool) -> list[str]:
    """The campaign ids an org owns: the home/primary campaign (when present) plus its
    registered campaign_meta rows, deduped and stable-ordered. Matches the set
    `_build_campaigns` produces, so cards and aggregates cover the same campaigns."""
    ids: list[str] = []
    if include_primary:
        ids.append(campaign.campaign_id)
    for m in store.list_campaign_meta(org_id):
        if m["campaign_id"] not in ids:
            ids.append(m["campaign_id"])
    return ids


def _org_matches(store: Store, cids: list[str], *,
                 include_identity: bool = False) -> list[dict[str, Any]]:
    """Every campaign's matches pooled and re-sorted newest-first by capture time, so
    the org-wide list keeps the same ordering contract as a single campaign's.

    `include_identity` is threaded straight to `_build_matches` and is the ONLY way
    a username/comment text enters one of these payloads — superadmin callers only
    (see `build_admin_org_leads`)."""
    pooled: list[dict[str, Any]] = []
    for cid in cids:
        pooled.extend(_build_matches(store, cid, include_identity=include_identity))
    pooled.sort(key=lambda m: m["capturedAt"]["ts"], reverse=True)
    return pooled


def _org_sessions(store: Store, cids: list[str]) -> list[dict[str, Any]]:
    pooled: list[dict[str, Any]] = []
    for cid in cids:
        pooled.extend(_build_sessions(store, cid))
    return pooled


def _org_alerts(store: Store, cids: list[str]) -> list[dict[str, Any]]:
    pooled: list[dict[str, Any]] = []
    for cid in cids:
        pooled.extend(_build_alerts(store, cid))
    return pooled


class _OrgContext:
    """The pieces every org builder needs: the (possibly synthetic-empty) home campaign,
    the org's campaign ids, the per-page CONFIG, and pooled org-wide sessions/matches
    (built lazily so a page that doesn't need them never pays for them)."""

    def __init__(self, store: Store, campaign: Optional[Campaign], *, org_id: Optional[int],
                 role: str, today: datetime, spend_cap_usd: float, skip_threshold: float,
                 canary_limit: int, watchlist_ttl_days: float) -> None:
        self._store = store
        self._skip_threshold = skip_threshold
        self._canary_limit = canary_limit
        self.org_id = org_id
        self.today = today
        self.spend_cap_usd = spend_cap_usd
        self.include_primary = campaign is not None
        self.campaign = campaign or _empty_campaign(org_id)
        self.cids = _org_campaign_ids(store, self.campaign, org_id, self.include_primary)
        self.config = build_config(
            store, self.campaign, org_id=org_id, role=role, today=today,
            spend_cap_usd=spend_cap_usd, skip_threshold=skip_threshold,
            canary_limit=canary_limit, watchlist_ttl_days=watchlist_ttl_days)

    def campaigns(self) -> tuple[list[dict[str, Any]], Optional[int]]:
        """Campaign cards (primary built from its own matches/sessions, drafts from the
        org rollup) plus the goal target that threads into the dashboard gauge."""
        cid = self.campaign.campaign_id
        return _build_campaigns(
            self._store, self.campaign, _build_sessions(self._store, cid),
            _build_matches(self._store, cid), self.today, self.spend_cap_usd,
            self.org_id, include_primary=self.include_primary)

    def health(self, sessions: list[dict[str, Any]]) -> dict[str, Any]:
        return _build_health(self._store, self.campaign.campaign_id, sessions,
                             self._skip_threshold, self._canary_limit, self.org_id)


def _context(store: Store, campaign: Optional[Campaign], *, org_id: Optional[int],
             role: str, today: Optional[datetime], spend_cap_usd: float,
             skip_threshold: float, canary_limit: int,
             watchlist_ttl_days: float) -> _OrgContext:
    return _OrgContext(
        store, campaign, org_id=org_id, role=role,
        today=today or datetime.now(TASHKENT), spend_cap_usd=spend_cap_usd,
        skip_threshold=skip_threshold, canary_limit=canary_limit,
        watchlist_ttl_days=watchlist_ttl_days)


def build_dashboard_org(store: Store, campaign: Optional[Campaign], *,
                        org_id: Optional[int], role: str,
                        today: Optional[datetime] = None, spend_cap_usd: float = 20.0,
                        skip_threshold: float = 0.6, canary_limit: int = 5,
                        watchlist_ttl_days: float = 10.0) -> dict[str, Any]:
    """`/api/dashboard`: org-wide bento dashboard + ticker matches + health + alerts.
    The RUN block is attached by the server (in-memory control plane)."""
    ctx = _context(store, campaign, org_id=org_id, role=role, today=today,
                   spend_cap_usd=spend_cap_usd, skip_threshold=skip_threshold,
                   canary_limit=canary_limit, watchlist_ttl_days=watchlist_ttl_days)
    campaigns, goal_target = ctx.campaigns()
    # Archived leads are removed — keep them out of the dashboard's ticker and every
    # MATCHES-derived client stat (total/pipeline/win-rate/status distribution).
    org_matches = _active_matches(_org_matches(store, ctx.cids))
    dashboard = _build_dashboard(_OrgStore(store, ctx.cids), "", ctx.today,
                                 goal_target=goal_target, matches=org_matches,
                                 campaigns=campaigns)
    return {
        "DASHBOARD": dashboard,
        "MATCHES": org_matches,
        "HEALTH": ctx.health(_org_sessions(store, ctx.cids)),
        "ALERTS": _org_alerts(store, ctx.cids),
        "CONFIG": ctx.config,
    }


def build_campaigns_org(store: Store, campaign: Optional[Campaign], *,
                        org_id: Optional[int], role: str,
                        today: Optional[datetime] = None, spend_cap_usd: float = 20.0,
                        skip_threshold: float = 0.6, canary_limit: int = 5,
                        watchlist_ttl_days: float = 10.0) -> dict[str, Any]:
    """`/api/campaigns`: every campaign card + pooled org-wide sessions (the edit page
    filters these to one campaign client-side). RUN is attached by the server."""
    ctx = _context(store, campaign, org_id=org_id, role=role, today=today,
                   spend_cap_usd=spend_cap_usd, skip_threshold=skip_threshold,
                   canary_limit=canary_limit, watchlist_ttl_days=watchlist_ttl_days)
    campaigns, _ = ctx.campaigns()
    return {"CAMPAIGNS": campaigns, "SESSIONS": _org_sessions(store, ctx.cids)}


def build_reports_org(store: Store, campaign: Optional[Campaign], *,
                      org_id: Optional[int], role: str,
                      today: Optional[datetime] = None, spend_cap_usd: float = 20.0,
                      skip_threshold: float = 0.6, canary_limit: int = 5,
                      watchlist_ttl_days: float = 10.0) -> dict[str, Any]:
    """`/api/reports`: org-wide time series + per-campaign rollup + health."""
    ctx = _context(store, campaign, org_id=org_id, role=role, today=today,
                   spend_cap_usd=spend_cap_usd, skip_threshold=skip_threshold,
                   canary_limit=canary_limit, watchlist_ttl_days=watchlist_ttl_days)
    campaigns, _ = ctx.campaigns()
    reports = _build_reports(_OrgStore(store, ctx.cids), "", ctx.today,
                             campaigns=campaigns, org_id=org_id)
    return {"REPORTS": reports, "HEALTH": ctx.health(_org_sessions(store, ctx.cids))}


def build_settings_org(store: Store, campaign: Optional[Campaign], *,
                       org_id: Optional[int], role: str,
                       today: Optional[datetime] = None, spend_cap_usd: float = 20.0,
                       skip_threshold: float = 0.6, canary_limit: int = 5,
                       watchlist_ttl_days: float = 10.0) -> dict[str, Any]:
    """`/api/settings`: CONFIG + team + invites + per-platform integration state."""
    ctx = _context(store, campaign, org_id=org_id, role=role, today=today,
                   spend_cap_usd=spend_cap_usd, skip_threshold=skip_threshold,
                   canary_limit=canary_limit, watchlist_ttl_days=watchlist_ttl_days)
    org_sessions = _org_sessions(store, ctx.cids)
    org_matches = _org_matches(store, ctx.cids)
    health = ctx.health(org_sessions)
    out = {
        "CONFIG": ctx.config,
        "TEAM": _build_team(store, org_id),
        "INVITES": _build_invites(store, org_id),
        "INTEGRATIONS": _build_integrations(
            store, ctx.campaign, org_sessions, org_matches, health, org_id),
    }
    # BILLING follows the INTEGRATIONS model: gated inside the builder by
    # view_billing (the /api/settings page itself is already action-gated upstream).
    if rbac.can(role, "view_billing"):
        out["BILLING"] = _build_billing(store, org_id)
    return out


def _lead_haystack(m: dict[str, Any]) -> str:
    """Everything the free-text lead search may look at, lowercased.

    v27: `username` and the comment `text` are no longer on an org-facing row, so
    searching them would silently match nothing. The search now runs over what a
    customer can actually SEE — the derived `intent`, the classifier's `reason`,
    and the `extracted` field VALUES (phone, city, budget…), which is what an
    operator hunting "Tashkent" or a phone number is really after. Identity is
    folded in only when the row carries it, i.e. the superadmin path, which asks
    for it explicitly; `.get` keeps this one function correct for both shapes."""
    parts = [str(m.get("intent") or ""), str(m.get("reason") or ""),
             str(m.get("username") or ""), str(m.get("text") or "")]
    extracted = m.get("extracted")
    if isinstance(extracted, dict):
        parts.extend(str(v) for v in extracted.values() if v is not None)
    return " ".join(parts).lower()


def _matches_filter_sort(matches: list[dict[str, Any]], *, q: Optional[str],
                         status: Optional[str], platform: Optional[str],
                         sort: str, descending: bool) -> list[dict[str, Any]]:
    """Apply the leads-page query: substring search (see `_lead_haystack`), status &
    platform facets, then sort. Pure — returns a new list, never mutates the input."""
    out = matches
    if status:
        out = [m for m in out if m["status"] == status]
    else:
        # "All" hides archived leads (they're removed) — they surface only when the
        # operator explicitly selects the Archived status filter above.
        out = [m for m in out if m["status"] != ARCHIVED_STATUS]
    if platform:
        out = [m for m in out if m["platform"] == platform]
    if q:
        needle = q.lower()
        out = [m for m in out if needle in _lead_haystack(m)]
    key = _LEAD_SORT_KEYS.get(sort, _LEAD_SORT_KEYS["capturedAt"])
    return sorted(out, key=key, reverse=descending)


def _lead_stats(matches: list[dict[str, Any]]) -> dict[str, Any]:
    """Org-wide lead stat tiles over the UNfiltered set, so the totals stay stable as
    the operator filters/pages. Zero-filled status counts give a stable shape."""
    counts = {s: 0 for s in ("new", "in_progress", "interested",
                             "closed", "couldnt_connect", "archived")}
    escalated = 0
    for m in matches:
        if m["status"] in counts:
            counts[m["status"]] += 1
        if m["escalated"]:
            escalated += 1
    won = sum(counts[s] for s in WIN_STATUS)
    return {"total": len(matches), "counts": counts, "won": won,
            "escalated": escalated,
            "labeled": sum(1 for m in matches if m["status"] != "new")}


def _campaign_options(store: Store, cids: list[str],
                      org_id: Optional[int]) -> list[dict[str, str]]:
    """Lightweight `[{id, name}]` for the Leads-page campaign filter, in the org's
    campaign order. Shipped IN the leads payload (not via /api/campaigns) so a
    leads-only member — who can't read the campaigns endpoint — still gets names."""
    meta = {m["campaign_id"]: m for m in store.list_campaign_meta(org_id)}
    options: list[dict[str, str]] = []
    for cid in cids:
        row = meta.get(cid)
        name = (row["display_name"] if row and row.get("display_name")
                else cid.replace("-", " ").title())
        options.append({"id": cid, "name": name})
    return options


def build_leads_org(store: Store, campaign: Optional[Campaign], *,
                    org_id: Optional[int], role: str, page: int = 1,
                    page_size: int = LEADS_PAGE_SIZE_DEFAULT, q: Optional[str] = None,
                    status: Optional[str] = None, platform: Optional[str] = None,
                    campaign_filter: Optional[str] = None,
                    sort: str = "capturedAt", descending: bool = True,
                    include_identity: bool = False,
                    today: Optional[datetime] = None, spend_cap_usd: float = 20.0,
                    skip_threshold: float = 0.6, canary_limit: int = 5,
                    watchlist_ttl_days: float = 10.0) -> dict[str, Any]:
    """`/api/leads`: org-wide leads, server-side filtered/sorted/paginated, plus
    stat tiles + platform/campaign facets + CONFIG. Returns the inner payload the
    server wraps in the `{ok,data,error}` envelope (pagination metadata has no home
    among the top-level record keys the other pages return).

    `campaign_filter` SCOPES the whole page (list + tiles) to one campaign; the
    status/platform/search facets narrow within that scope. Archived leads are
    excluded from the tiles and the default list (see `_matches_filter_sort`).

    `include_identity` (v27) is FALSE for every org caller — `/api/leads` carries
    the derived `intent`, never a username or comment text. Only the superadmin
    adapter below sets it."""
    ctx = _context(store, campaign, org_id=org_id, role=role, today=today,
                   spend_cap_usd=spend_cap_usd, skip_threshold=skip_threshold,
                   canary_limit=canary_limit, watchlist_ttl_days=watchlist_ttl_days)
    org_matches = _org_matches(store, ctx.cids, include_identity=include_identity)
    # Scope to one campaign before faceting/paginating, so the tiles reflect it too.
    scoped = ([m for m in org_matches if m["campaignId"] == campaign_filter]
              if campaign_filter else org_matches)
    page_size = max(1, min(page_size, LEADS_PAGE_SIZE_MAX))
    page = max(1, page)
    filtered = _matches_filter_sort(scoped, q=q, status=status, platform=platform,
                                    sort=sort, descending=descending)
    start = (page - 1) * page_size
    return {
        "items": filtered[start:start + page_size],
        "total": len(filtered),
        "page": page,
        "pageSize": page_size,
        "stats": _lead_stats(_active_matches(scoped)),
        # Facet options span the whole org set so they never shrink unexpectedly.
        "platforms": sorted({m["platform"] for m in org_matches}),
        "campaigns": _campaign_options(store, ctx.cids, org_id),
        "CONFIG": ctx.config,
    }


# ---------------------------------------------------------------------------
# Superadmin cross-org READ adapters (Phase 5d).
#
# The /api/admin/orgs/{id}/{campaigns,leads} endpoints serve a LEAN, read-only
# projection distinct from the org-plane pages: the panel only renders a name +
# platform + status per campaign and a flat lead row. These adapters reuse the
# org builders above (so platform resolution, campaign ordering, and lead
# filter/sort/pagination stay single-sourced) and reshape their rich output into
# the exact contract admin-panel/src/shared/schemas/admin.ts validates. Keep the
# keys below in sync with `adminOrgCampaignSchema` / `adminOrgLeadSchema`.
# ---------------------------------------------------------------------------

def build_admin_org_campaigns(store: Store, *, org_id: int) -> dict[str, Any]:
    """`{campaigns:[{id,displayName,platform,status,createdAt,updatedAt,archived}]}`.

    Reuses `build_campaigns_org` for the card list, then joins `campaign_meta` for
    the created/updated timestamps the card doesn't carry."""
    cards = build_campaigns_org(store, None, org_id=org_id, role="owner")["CAMPAIGNS"]
    meta_by_id = {m["campaign_id"]: m for m in store.list_campaign_meta(org_id)}
    campaigns = [
        {
            "id": c["id"],
            "displayName": c.get("name"),
            "platform": c.get("platform") or "",
            "status": c.get("status") or "",
            "createdAt": meta_by_id.get(c["id"], {}).get("created_at"),
            "updatedAt": meta_by_id.get(c["id"], {}).get("updated_at"),
            "archived": c.get("archivedAt") is not None,
        }
        for c in cards
    ]
    return {"campaigns": campaigns}


def _admin_lead_row(m: dict[str, Any]) -> dict[str, Any]:
    """Flatten one org match row into the admin lead contract (numeric capturedAt,
    boolean extracted). Pure — returns a new dict, never mutates the input.

    This is the ONE surface that still carries a lead's real identity: the
    superadmin plane (platform_admins, IP-allowlisted) sees the handle, the raw
    comment, AND the derived `intent` side by side — that pairing is how an
    operator checks that redaction is summarising honestly. Indexing `username`
    and `text` (rather than `.get`) is deliberate: if this ever gets fed redacted
    rows it must fail loudly, not silently serve blanks that read as "no data".
    """
    captured = m.get("capturedAt")
    return {
        "commentId": m["commentId"],
        "campaignId": m["campaignId"],
        "platform": m["platform"],
        "username": m["username"],
        "text": m["text"],
        # The customer-facing line, shown next to the raw text it was derived
        # from. "" for a pre-v27 row (captured before redaction existed).
        "intent": m.get("intent") or "",
        "capturedAt": captured.get("ts") if isinstance(captured, dict) else None,
        "status": m["status"],
        "score": m.get("score"),
        "reason": m.get("reason") or None,
        "extracted": bool(m.get("extracted")),
        "tier": m.get("tier"),
    }


def build_admin_org_leads(store: Store, *, org_id: int, page: int = 1,
                          page_size: int = LEADS_PAGE_SIZE_DEFAULT,
                          q: Optional[str] = None, status: Optional[str] = None,
                          platform: Optional[str] = None,
                          campaign_filter: Optional[str] = None,
                          sort: str = "capturedAt",
                          descending: bool = True) -> dict[str, Any]:
    """`{leads:[...], page, pageSize, total}`.

    Reuses `build_leads_org` for filter/sort/pagination, then flattens each row.
    `include_identity=True` is what makes this the superadmin path: it is the only
    call in the codebase that asks `_build_matches` for the username + comment
    text, so the search here also spans the handle and the raw comment."""
    data = build_leads_org(
        store, None, org_id=org_id, role="owner", page=page, page_size=page_size,
        q=q, status=status, platform=platform, campaign_filter=campaign_filter,
        sort=sort, descending=descending, include_identity=True)
    return {
        "leads": [_admin_lead_row(m) for m in data["items"]],
        "page": data["page"],
        "pageSize": data["pageSize"],
        "total": data["total"],
    }
