"""Panel data adapter — turns the engine's SQLite state into the record shape
the React panel consumes at `/api/state`. The shape is the contract validated
by admin-panel/src/shared/schemas/panelState.ts (Zod); keep the two in sync.

The panel derives every view from this small set of raw records via pure
selectors. We produce exactly those records from the live DB. Read-only: this
is the PRD v1 panel surface.

Anything the engine does not (yet) persist degrades gracefully to an empty list
or a neutral default rather than a fake value.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from . import billing, rbac
from .core.config import SUPPORTED_PLATFORMS, Campaign, Soul, parse_extract_fields
from .core.matching import redact_extracted, redact_identity
from .core.store import WIN_STATUS, Store

# Soft warning threshold for the usage meter: at/above this fraction of the lead
# cap the panel surfaces a "near limit" nudge before the 402 wall.
BILLING_NEAR_LIMIT_RATIO = 0.8

# Asia/Tashkent is a fixed UTC+5 (no DST) — safe to hardcode the offset and
# avoid a tz-database dependency.
TASHKENT = timezone(timedelta(hours=5))

# Dashboard/report period spans, in days. "today" is the calendar day; week/month
# are trailing windows. The previous window is the immediately preceding equal span.
PERIOD_DAYS = {"today": 1, "week": 7, "month": 30}
# Trailing days drawn in the hero sparkline per period (visual trend context).
SPARK_DAYS = {"today": 14, "week": 14, "month": 30}
CPL_BARS = 8          # bars in the dashboard CPL history tile
REPORT_DAYS = 14      # x-axis length for the report line/area charts

# v27 redaction: the dashboard live ticker shows a lead's INTENT line, not the
# commenter. A ticker row is a single glance-width line, so the cut happens here
# rather than in the client — one server-side place decides how much of any
# org-facing lead string travels, and the client can't widen it.
TICKER_INTENT_CHARS = 80

# ---- E.5/E.7: the two lead numbers, and the one word that reconciles them ----
#
# `leadsFound` is the deduped `run_events` estimate — what a run actually DISCOVERED.
# `leadsDelivered` is the org's real `matches` rows — what actually REACHED the account.
# On a healthy run they converge, because the rows land in the ack body.
#
# A dead-lettered run never acks. Leads travel ONLY in the ack body, so its harvest
# stays in the worker's local sqlite and the cloud's row count is 0 forever, while the
# estimate is the only record the customer will ever have of that run. Spend has the
# OPPOSITE asymmetry — the nack body ships it (`sidecar._nack` → `_attach_spend`), so
# the money is banked and the accounting is correct. That is how a card ends up reading
# "$4.10 spent, 0 leads" with nothing on it to explain the pairing.
#
# Either number alone is a lie: `leadsFound` implies leads the customer can open, and
# `leadsDelivered` denies work that really happened. So both travel, plus this word, and
# every surface that puts spend next to leads renders it.
DELIVERY_DELIVERED = "delivered"
DELIVERY_PENDING = "pending"           # still in flight — the gap is only ack lag
DELIVERY_NOT_DELIVERED = "not_delivered"


def delivery_state(leads_found: Any, leads_delivered: Any, *,
                   finished: bool) -> dict[str, Any]:
    """The lead pair plus its reconciliation word, for ONE run or ONE campaign.

    `finished` is what makes the gap mean anything: mid-flight EVERY fleet-routed run
    reads found > delivered because the rows only land at ack, so an unfinished gap is
    `pending`, not a fault. Only a finished run whose gap never closed is
    `not_delivered` — and the spend beside it, which WAS banked, is spend on an
    incomplete run and must be labelled that way rather than hidden or zeroed.

    The discriminator is `leadsFound > leadsDelivered` and nothing else. In particular
    it is NOT a dash in CPL: cost-per-lead is guarded on `won`
    (`WIN_STATUS = {interested, closed}`), so it reads `—` on every untriaged campaign,
    healthy or not. And no CPL is ever synthesised from `leadsFound` — a cost per lead
    the customer cannot open is a fiction.

    Pure and total: never raises, never goes negative, never renders the two numbers
    inconsistently (the estimate can only under-count, so it is floored at the rows).
    """
    try:
        found = max(0, int(leads_found or 0))
    except (TypeError, ValueError):
        found = 0
    try:
        delivered = max(0, int(leads_delivered or 0))
    except (TypeError, ValueError):
        delivered = 0
    # The event estimate under-counts by design (one person, two qualifying comments on
    # one post, dedupes to one) and the rows are authoritative once they exist, so the
    # pair is monotonic in the only direction that can be true.
    found = max(found, delivered)
    if found <= delivered:
        state = DELIVERY_DELIVERED
    elif finished:
        state = DELIVERY_NOT_DELIVERED
    else:
        state = DELIVERY_PENDING
    return {"leadsFound": found, "leadsDelivered": delivered, "delivery": state}


def _dt(ts: Optional[float]) -> Optional[datetime]:
    return datetime.fromtimestamp(ts, TASHKENT) if ts else None


def _iso_ts(ts: Optional[float]) -> Optional[str]:
    """Epoch → ISO-8601 UTC string — the wire shape for lifecycle timestamps."""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def _lifecycle_fields(meta: Optional[dict[str, Any]]) -> dict[str, Any]:
    """v12 lifecycle fields every campaign card carries: archive timestamp + pause
    cause (Phase 1) and the fixed-cadence schedule (Phase 3)."""
    if not meta:
        return {"archivedAt": None, "pausedReason": None, "scheduleEnabled": False,
                "scheduleKind": "", "scheduleDow": None, "scheduleHour": None,
                "scheduleMinute": None, "scheduleTz": "Asia/Tashkent",
                "nextRunAt": None}
    return {
        "archivedAt": _iso_ts(meta.get("archived_at")),
        "pausedReason": meta.get("paused_reason"),
        "scheduleEnabled": bool(meta.get("schedule_enabled")),
        "scheduleKind": meta.get("schedule_kind") or "",
        "scheduleDow": meta.get("schedule_dow"),
        "scheduleHour": meta.get("schedule_hour"),
        "scheduleMinute": meta.get("schedule_minute"),
        "scheduleTz": meta.get("schedule_tz") or "Asia/Tashkent",
        "nextRunAt": _iso_ts(meta.get("next_run_at")),
    }


def _date_label(ts: Optional[float]) -> str:
    d = _dt(ts)
    return f"{d.strftime('%b')} {d.day}" if d else "—"


def _time_label(ts: Optional[float]) -> str:
    d = _dt(ts)
    return d.strftime("%H:%M") if d else "—"


def _session_flag(status: str) -> str:
    return {"halted": "halted", "running": "live"}.get(status, "")


def _period_window(today: datetime, period: str) -> tuple[float, float, float, float]:
    """Return (cur_start, cur_end, prev_start, prev_end) as unix timestamps.

    `cur` is the current period; `prev` is the immediately preceding equal-length
    span (for deltas). Days are bucketed in Asia/Tashkent local time.
    """
    days = PERIOD_DAYS[period]
    midnight = today.replace(hour=0, minute=0, second=0, microsecond=0)
    cur_end = midnight + timedelta(days=1)
    cur_start = midnight if period == "today" else midnight - timedelta(days=days - 1)
    span = cur_end - cur_start
    return (cur_start.timestamp(), cur_end.timestamp(),
            (cur_start - span).timestamp(), cur_start.timestamp())


def _fill_values(by_day: dict[str, float], end_date: datetime, n: int) -> list[float]:
    """Dense trailing n-day series ending at end_date (missing days → 0)."""
    out = []
    for i in range(n - 1, -1, -1):
        key = (end_date - timedelta(days=i)).strftime("%Y-%m-%d")
        out.append(by_day.get(key, 0))
    return out


def _day_labels(end_date: datetime, n: int) -> list[str]:
    return [(end_date - timedelta(days=i)).strftime("%b %d") for i in range(n - 1, -1, -1)]


def _delta_str(cur: float, prev: float) -> str:
    """Signed percentage change of cur vs prev as a display string."""
    if prev == 0:
        return "+100%" if cur > 0 else "0%"
    pct = round((cur - prev) / prev * 100)
    return f"{'+' if pct >= 0 else ''}{pct}%"


def _build_sessions(store: Store, cid: str) -> list[dict[str, Any]]:
    out = []
    for s in store.all_sessions(cid):
        reels = s["reels_seen"] or 0
        skip = s["already_seen_skips"] or 0
        dur = 0
        if s["ended_at"] and s["started_at"]:
            dur = max(0, round((s["ended_at"] - s["started_at"]) / 60))
        out.append({
            "id": s["session_id"],
            # Campaign attribution so the org-wide /api/campaigns feed (which pools
            # every campaign's sessions) can be filtered back to one campaign in the UI.
            "campaignId": s.get("campaign_id") or cid,
            # The RunManager run this session belongs to — lets the panel deep-link
            # into the recorded activity feed. Nullable: pre-v10 sessions and CLI
            # runs without AIZU_RUN_ID correlation have no run_id.
            "runId": s.get("run_id"),
            "platform": s["platform"] if s.get("platform") else "instagram",
            "date": _date_label(s["started_at"]),
            "start": _time_label(s["started_at"]),
            "durationMin": dur,
            "reelsSeen": reels,
            "alreadySeen": skip,
            "relevant": s["relevance_passes"] or 0,
            "commentsScored": s["comments_scored"] or 0,
            "matches": s["matches"] or 0,
            "escalations": s["escalations"] or 0,
            "spendUsd": round(s["spend_usd"] or 0.0, 4),
            "flag": _session_flag(s["status"]),
            "skipRatio": (skip / reels) if reels else 0.0,
            "watermark": "QVFD" + str(s["session_id"])[:6] + "Kc",
        })
    return out


def lead_uid(campaign_id: str, platform: str, comment_id: str) -> str:
    """The panel-facing UNIQUE id of a lead.

    A lead's real identity is the `matches` composite primary key
    `(campaign_id, platform, comment_id)` — a `comment_id` is only unique inside
    one platform's id namespace, and the same commenter can legitimately appear
    under two campaigns. Flattening the row to a bare `comment_id` (as this
    payload used to) collapsed those into ONE panel row, so clicking a lead in
    campaign A could open — and write status to — campaign B's lead.

    The encoding is a `|`-joined triple with `%` and `|` percent-escaped inside
    each part, which makes it injective (distinct triples never collide) and
    safe to carry in a URL path segment. `admin-panel/src/shared/lib/leadId.ts`
    implements the SAME encoding character-for-character — keep them in lockstep.
    The panel treats the value as opaque: it never parses it back, and every
    write resolves the composite key from the record's own
    `campaignId`/`platform`/`commentId` fields.
    """
    return "|".join(str(part).replace("%", "%25").replace("|", "%7C")
                    for part in (campaign_id, platform, comment_id))


def _ticker_intent(intent: str) -> str:
    """One lead's intent line cut down to a ticker row: whitespace collapsed, then
    a word-boundary cut with an ellipsis so the tile never ends mid-word. `""`
    stays `""` — the client renders its neutral "intent not captured" placeholder,
    never a bare ellipsis and never a fallback to any identifier."""
    s = " ".join((intent or "").split())
    if len(s) <= TICKER_INTENT_CHARS:
        return s
    head = s[:TICKER_INTENT_CHARS].rsplit(" ", 1)[0] or s[:TICKER_INTENT_CHARS]
    return head + "…"


def _build_matches(store: Store, cid: str, *,
                   include_identity: bool = False) -> list[dict[str, Any]]:
    """Panel lead records for one campaign, newest-first.

    v27 redaction: the ORG-facing record carries NO `username` and NO comment
    `text`. A customer sees `intent` — the one-line summary of what the person
    wants, derived at capture time by `core.matching.derive_intent` — plus the
    classifier's reason, the grounded `extracted` fields, and the workflow state.
    The raw identity stays in `matches` and is served ONLY through the superadmin
    plane, which opts in with `include_identity=True`. So does `reelId`: a POINTER
    to the identity is the identity. The post it names is public and carries both
    the handle and the comment, so an org-facing `reelId` let anyone with devtools
    reconstruct the whole list without touching /api/lead/reveal — an audited
    disclosure with an unaudited side door is just an unaudited disclosure.

    The flag defaults to DENY on purpose: a future org-facing caller that forgets
    it leaks nothing, whereas a default-allow with an opt-out leaks on every
    caller someone forgets to update.

    Dropping the two KEYS is not the same as redacting the two VALUES, and the
    difference is the whole finding behind `matching.redact_identity`: `reason`
    and `extracted` are model-authored, no prompt has ever constrained them, and
    a perfectly ordinary classifier reason quotes the comment and names the
    handle. So an org-facing row has its prose scrubbed against the handle and
    the comment the row itself carries — the boundary is the one place both
    strings are known for certain. `include_identity=True` skips the scrub
    entirely: the superadmin plane exists to see exactly this.
    """
    out = []
    # Batch-fetch the per-lead audit log + notes once (avoids an N+1 across leads).
    hist_map = store.status_history_by_lead(cid)
    notes_map = store.notes_by_lead(cid)
    for m in sorted(store.matches(cid), key=lambda r: r["captured_at"] or 0,
                    reverse=True):  # newest-first by raw timestamp, not label
        ts = m["captured_at"]
        d = _dt(ts)
        escalated = (m.get("tier") in ("cloud", "degraded"))
        platform = m["platform"] if m.get("platform") else "instagram"
        history = hist_map.get((platform, m["comment_id"]), [])
        notes = notes_map.get((platform, m["comment_id"]), [])
        last = history[-1] if history else None
        extracted = m["extracted"] if isinstance(m["extracted"], dict) else {}
        intent = m.get("intent") or ""
        reason = m["reason"] or ""
        if not include_identity:
            # The scrub runs on the way OUT, not at capture time, so it also
            # covers rows written before v27 and rows synced back by a worker
            # older than this build — neither of which can be re-derived.
            handle, comment = m.get("username"), m.get("text")
            intent = redact_identity(intent, username=handle, comment_text=comment)
            reason = redact_identity(reason, username=handle, comment_text=comment)
            extracted = redact_extracted(extracted, username=handle,
                                         comment_text=comment)
        row = {
            # Unique per (campaign, platform, comment) — never a bare comment_id.
            "id": lead_uid(m["campaign_id"], platform, m["comment_id"]),
            "commentId": m["comment_id"],
            "campaignId": m["campaign_id"],
            "platform": platform,
            "sessionId": m.get("session_id"),
            "lang": m["lang"],
            # The ONLY lead prose an org sees (v27). `""` when nothing could be
            # derived honestly — a pre-v27 row captured before redaction existed,
            # or `derive_intent`'s documented last resort — and the panel shows a
            # neutral placeholder for it. Never guessed from the raw comment.
            "intent": intent,
            "score": round(m["score"] or 0.0, 2),
            "reason": reason,
            "extracted": extracted,
            "status": m["status"],
            "escalated": escalated,
            "escalationCost": 0,
            "capturedAt": {"date": _date_label(ts),
                           "time": d.strftime("%H:%M") if d else "—",
                           # Raw epoch so the panel can sort by capture time;
                           # the labels above are display-only and not sortable.
                           "ts": float(ts) if ts else 0.0},
            # NOTE: `reelId` is NOT here — see the `include_identity` block below.
            # Real last-changer from the audit log (None for never-changed leads).
            "statusBy": last["by"] if last else None,
            "statusAt": _date_label(last["at"]) if last else None,
            "statusHistory": [
                {"fromStatus": h["fromStatus"], "toStatus": h["toStatus"],
                 "by": h["by"], "at": _date_label(h["at"]), "atTs": float(h["at"]),
                 "note": h["reason"]} for h in history],
            "notes": [
                {"id": str(n["id"]), "body": n["body"], "authorEmail": n["authorEmail"],
                 "authorId": n["authorId"], "createdAt": _date_label(n["createdAt"]),
                 "createdAtTs": float(n["createdAt"])} for n in notes],
        }
        if include_identity:
            # Superadmin plane only (see the docstring). Added AFTER the shared
            # shape so the org-facing keys are literally the same dict either way
            # — the two payloads can't drift apart into two record shapes.
            row["username"] = m["username"]
            row["text"] = m["text"]
            # `reelId` rides WITH the identity, not with the product fields, and
            # that is the whole point of moving it here: the post it names is
            # public, and the lead's comment — handle and words — is plainly
            # readable on it. Shipping it org-facing meant any operator with
            # devtools could walk the anonymized list straight to every identity
            # without ever calling /api/lead/reveal, i.e. the reveal endpoint's
            # audit trail was optional. The reveal response returns `reelId`
            # itself, so the post is still one click away — one AUDITED click.
            row["reelId"] = m["reel_id"]
        out.append(row)
    return out


def _build_reels(store: Store, cid: str, ttl_days: float, *,
                 include_identity: bool = False) -> list[dict[str, Any]]:
    """The posts this campaign scanned — what was looked at, not who answered.

    The post ITSELF is the product here (A7): its id, author and caption are how
    an operator sees what the agent read, and a post's author is the content
    creator, not the lead. Those stay for every caller.

    What does NOT stay is the WATCHLIST join. `Store.add_to_watchlist` is called
    from exactly one place — after a comment batch, `if found` — so a watchlist
    row exists if and ONLY if that post produced at least one lead. Every field
    derived from that join is therefore a mark on the lead-bearing posts:

      * `newSinceLastPoll` (`w.match_count`) — 1 on the post that produced the
        lead, 0 on the ones that did not. On a campaign whose leads came from one
        post it is not an aggregate at all, it is a pointer.
      * `expiresInDays` (`w.expires_at`) — the TTL is only ever stamped by
        `add_to_watchlist`, so `10` vs `0` says the same thing just as exactly.
        Fixing only the count and leaving this would have moved the leak, not
        closed it.
      * `addedAt` — `w.added_at` when watchlisted, else `first_seen`. Weaker (the
        two coincide on a post scored the day it was first seen) but it is the
        same join, so it falls back to `first_seen` for an org caller rather than
        leaking a third time.

    That mark is the re-join the audited reveal exists to prevent: filter for the
    marked post, open its public URL, and read the handle and the comment for
    every lead the campaign found — no `/api/lead/reveal` call, no audit row, and
    it works for a `viewer`, the one role RBAC refuses `reveal_lead` outright.

    So the watchlist fields ship only under `include_identity=True`, next to the
    identity they point at. Keys are OMITTED rather than zeroed: a fake `0` is a
    claim about the data, and `_build_matches` sets the precedent — dropping the
    KEY is what makes a forgetful future caller leak nothing.
    """
    out = []
    now = datetime.now(TASHKENT).timestamp()
    for r in store.reels(cid, only_relevant=True):
        row = {
            "id": r["reel_id"],
            "author": r.get("author") or r["reel_id"],
            "authorFull": r.get("author") or r["reel_id"],
            "caption": r.get("caption") or "",
            "ocrText": r.get("ocr_text") or "",
            "thumbSeed": r["reel_id"],
            "addedAt": _date_label((r.get("added_at") if include_identity else None)
                                   or r.get("first_seen")),
            "lastPoll": _date_label(r.get("last_seen")),
            "pollHistory": [],
        }
        if include_identity:
            expires = r.get("expires_at")
            exp_days = round((expires - now) / 86400) if expires else 0
            row["expiresInDays"] = max(0, exp_days)
            row["newSinceLastPoll"] = r.get("match_count") or 0
        out.append(row)
    return out


def _build_escalation_log(store: Store, cid: str) -> list[dict[str, Any]]:
    out = []
    for e in reversed(store.spend_entries(cid)):  # most recent first
        out.append({
            "time": f"{_date_label(e['created_at'])} {_time_label(e['created_at'])}",
            "sessionId": e.get("session_id") or "—",
            "stage": e["stage"],
            "model": e.get("model") or "—",
            "tokens": 0,
            "cost": round(e["usd"], 4),
            "outcome": "",
        })
    return out[:40]


_TIER_MAP = {"halt": "halt", "soft": "soft"}


def _build_alerts(store: Store, cid: str) -> list[dict[str, Any]]:
    out = []
    for f in store.all_flags(cid):
        out.append({
            "time": f"{_date_label(f['created_at'])} {_time_label(f['created_at'])}",
            "tier": _TIER_MAP.get(f["severity"], "info"),
            "title": f["kind"].replace("_", " ").title(),
            "desc": f["detail"] or "",
        })
    return out


def _build_health(store: Store, cid: str, sessions: list[dict[str, Any]],
                  skip_threshold: float, canary_limit: int,
                  org_id: Optional[int]) -> dict[str, Any]:
    open_halt = [f for f in store.open_flags(org_id, "halt")]
    open_feed = [f for f in store.all_flags(cid)
                 if f["kind"] == "feed_health" and f["resolved_at"] is None]
    last = sessions[-1] if sessions else None
    skip_ratio = round(last["skipRatio"], 2) if last else 0.0
    return {
        "overall": "halted" if open_halt else "operational",
        "login": {"state": "valid", "detail": "Cookie session — status not independently tracked"},
        "checkpoint": {
            "state": "halted" if open_halt else "clear",
            "detail": open_halt[0]["detail"] if open_halt else "No open challenge",
        },
        "canary": {"emptyStreak": 0, "limit": canary_limit, "lastJson": "—",
                   "detail": "Interceptor reading reel + comment JSON"},
        "actionBlock": {"state": "none", "detail": "No write actions ever issued — read-only by design"},
        "feed": {
            "skipRatio": skip_ratio, "threshold": skip_threshold,
            "flagged": bool(open_feed),
            "lastFlag": _date_label(open_feed[0]["created_at"]) if open_feed else "—",
            "lastResteer": "—",
            "detail": "Tired-feed flag open — re-steer on mobile" if open_feed
                      else "Feed nominal",
        },
    }


def _build_platforms(matches: list[dict[str, Any]],
                     sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-platform rollup for the dashboard (multi-platform plan Part B3).

    One campaign brief can be fanned across platforms and pooled here; the panel
    uses this for a breakdown / filter. Derived from the already-built records so
    it needs no extra query, and stays empty-list-safe for a single-platform run.
    """
    counts: dict[str, dict[str, int]] = {}
    for m in matches:
        counts.setdefault(m["platform"], {"matches": 0, "sessions": 0})["matches"] += 1
    for s in sessions:
        counts.setdefault(s["platform"], {"matches": 0, "sessions": 0})["sessions"] += 1
    return [{"platform": p, "matches": c["matches"], "sessions": c["sessions"]}
            for p, c in sorted(counts.items())]


def _build_dashboard(store: Store, cid: str, today: datetime, *,
                     goal_target: Optional[int], matches: list[dict[str, Any]],
                     campaigns: list[dict[str, Any]]) -> dict[str, Any]:
    """Pulse bento dashboard, pre-aggregated per period (today/week/month)."""
    # Period-independent pieces (monthly goal, heatmap, campaign rollup, ticker).
    month_s, month_e, _, _ = _period_window(today, "month")
    goal_current = sum(r["n"] for r in store.matches_by_day(cid, since_ts=month_s, until_ts=month_e))
    target = goal_target if goal_target else max(goal_current, 1)
    heat_map = store.matches_by_hour(cid, since_ts=month_s, until_ts=month_e)
    heat = [heat_map.get(h, 0) for h in range(24)]
    active = sum(1 for c in campaigns if c["status"] == "live")
    top = sorted(campaigns, key=lambda c: c["leads"], reverse=True)[:5]
    # E.7: this row puts `leads` next to `cpl` — a spend-derived number — so it carries
    # the delivery pair straight off the card it mirrors rather than recomputing it.
    # Note the period tiles above/below deliberately do NOT: those are windowed, the
    # found estimate is lifetime, and mixing them would invent a ratio true of neither.
    top_mini = [{"id": c["id"], "name": c["name"], "platform": c.get("platform", "instagram"),
                 "status": c["status"], "leads": c["leads"], "cpl": c["cpl"],
                 "leadsFound": c.get("leadsFound", c["leads"]),
                 "leadsDelivered": c.get("leadsDelivered", c["leads"]),
                 "delivery": c.get("delivery", DELIVERY_DELIVERED)} for c in top]
    # v27: the ticker names what the lead WANTS, not who they are.
    ticker = [{"id": m["id"], "intent": _ticker_intent(m["intent"]),
               "platform": m["platform"], "score": m["score"],
               "capturedAt": m["capturedAt"]} for m in matches[:10]]
    # Needs-attention is current-state (not windowed) — compute once and reuse.
    attention = store.needs_attention(cid, now=today.timestamp())

    out: dict[str, Any] = {}
    for period in ("today", "week", "month"):
        cs, ce, ps, pe = _period_window(today, period)
        leads_cur = sum(r["n"] for r in store.matches_by_day(cid, since_ts=cs, until_ts=ce))
        leads_prev = sum(r["n"] for r in store.matches_by_day(cid, since_ts=ps, until_ts=pe))
        spark_days = SPARK_DAYS[period]
        spark_src = {r["day"]: r["n"] for r in store.matches_by_day(
            cid, since_ts=(today - timedelta(days=spark_days)).timestamp())}
        spark = _fill_values(spark_src, today, spark_days)

        spend_cur = sum(store.spend_by_stage(cid, since_ts=cs, until_ts=ce).values())
        won_cur = store.won_count(cid, since_ts=cs, until_ts=ce)
        cpl = round(spend_cur / won_cur, 2) if won_cur else None
        spend_bars = {r["day"]: r["usd"] for r in store.spend_by_day(
            cid, since_ts=(today - timedelta(days=CPL_BARS)).timestamp())}
        match_bars = {r["day"]: r["n"] for r in store.matches_by_day(
            cid, since_ts=(today - timedelta(days=CPL_BARS)).timestamp())}
        cpl_history = []
        for i in range(CPL_BARS - 1, -1, -1):
            key = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            n = match_bars.get(key, 0)
            cpl_history.append(round(spend_bars.get(key, 0.0) / n, 2) if n else 0.0)

        scored_cur = store.scored_count(cid, since_ts=cs, until_ts=ce)
        scored_prev = store.scored_count(cid, since_ts=ps, until_ts=pe)
        conv_cur = round(leads_cur / scored_cur, 4) if scored_cur else 0.0
        conv_prev = (leads_prev / scored_prev) if scored_prev else 0.0

        chan_cur = store.matches_by_platform(cid, since_ts=cs, until_ts=ce)
        chan_prev = store.matches_by_platform(cid, since_ts=ps, until_ts=pe)
        channels = [{"platform": p, "current": chan_cur.get(p, 0), "previous": chan_prev.get(p, 0)}
                    for p in sorted(set(chan_cur) | set(chan_prev))]

        # v6 lead-pipeline stat groups.
        breakdown = store.status_breakdown(cid, since_ts=cs, until_ts=ce)
        pipeline = store.pipeline_conversion(cid, since_ts=cs, until_ts=ce)
        team_activity = store.status_changes_by_user(cid, since_ts=cs, until_ts=ce)

        out[period] = {
            "leads": {"value": leads_cur, "delta": _delta_str(leads_cur, leads_prev), "spark": spark},
            "goal": {"target": target, "current": goal_current,
                     "pct": min(100, round(goal_current / target * 100)) if target else 0},
            "cpl": {"value": cpl, "history": cpl_history},
            "conversion": {"value": conv_cur, "delta": _delta_str(conv_cur, conv_prev)},
            "channels": channels,
            "funnel": store.funnel_totals(cid, since_ts=cs, until_ts=ce),
            "bestHour": heat,
            "activeCampaigns": active,
            "topCampaigns": top_mini,
            "ticker": ticker,
            # Lead Kanban pipeline stats (v6).
            "leadStatus": {
                "counts": breakdown,
                "distribution": [{"status": s, "count": breakdown[s]}
                                 for s in sorted(breakdown)],
            },
            "pipeline": pipeline,
            "teamActivity": team_activity,
            "needsAttention": attention,
        }
    return out


def _build_reports(store: Store, cid: str, today: datetime, *,
                   campaigns: list[dict[str, Any]],
                   org_id: Optional[int]) -> dict[str, Any]:
    """Pulse reports, pre-aggregated per period. Time series share one label axis."""
    rollup = {r["campaignId"]: r for r in store.per_campaign_rollup(org_id)}
    out: dict[str, Any] = {}
    for period in ("today", "week", "month"):
        cs, ce, _, _ = _period_window(today, period)
        labels = _day_labels(today, REPORT_DAYS)
        series_floor = (today - timedelta(days=REPORT_DAYS)).timestamp()

        plat_totals = store.matches_by_platform(cid, since_ts=cs, until_ts=ce)
        by_platform = []
        for p in sorted(plat_totals):
            by = {r["day"]: r["n"] for r in store.matches_by_day(cid, since_ts=series_floor, platform=p)}
            by_platform.append({"platform": p, "values": _fill_values(by, today, REPORT_DAYS)})

        spend_days = {r["day"]: r["usd"] for r in store.spend_by_day(cid, since_ts=series_floor)}
        match_days = {r["day"]: r["n"] for r in store.matches_by_day(cid, since_ts=series_floor)}
        cpl_trend = []
        for i in range(REPORT_DAYS - 1, -1, -1):
            key = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            n = match_days.get(key, 0)
            cpl_trend.append(round(spend_days.get(key, 0.0) / n, 2) if n else 0.0)

        stage = store.spend_by_stage(cid, since_ts=cs, until_ts=ce)
        spend_by_stage = [{"name": k, "value": round(v, 4)} for k, v in sorted(stage.items())]
        ranking = [{"platform": p, "leads": n}
                   for p, n in sorted(plat_totals.items(), key=lambda kv: kv[1], reverse=True)]
        # E.7: leads and spend on the SAME row — the report must not read "$X spent,
        # 0 leads" for a dead-lettered run with no way to tell that from a barren one.
        per_campaign = [{"id": c["id"], "name": c["name"], "status": c["status"],
                         "leads": c["leads"], "cpl": c["cpl"], "spend": c["spent"],
                         "leadsFound": c.get("leadsFound", c["leads"]),
                         "leadsDelivered": c.get("leadsDelivered", c["leads"]),
                         "delivery": c.get("delivery", DELIVERY_DELIVERED)}
                        for c in campaigns]

        out[period] = {
            "labels": labels,
            "matchesByPlatform": by_platform,
            "cplTrend": cpl_trend,
            "spendByStage": spend_by_stage,
            "platformRanking": ranking,
            "perCampaign": per_campaign,
        }
    return out


def _initials(email: str) -> str:
    local = (email or "").split("@")[0]
    parts = re.split(r"[._-]+", local)
    letters = "".join(p[0] for p in parts[:2] if p)
    return (letters or local[:2]).upper()


def _build_team(store: Store, org_id: Optional[int]) -> list[dict[str, Any]]:
    """Real org members (v7) — the panel Team page operates on actual login accounts."""
    if org_id is None:
        return []
    out = []
    for u in store.list_org_users(org_id):
        email = u["email"]
        out.append({"id": str(u["id"]), "userId": u["id"],
                    "name": email.split("@")[0], "email": email, "role": u["role"],
                    "initials": _initials(email), "status": "active",
                    "createdAt": u.get("createdAt")})
    return out


def _build_invites(store: Store, org_id: Optional[int]) -> list[dict[str, Any]]:
    """Pending team invites (the copy-link path) for the Team page."""
    if org_id is None:
        return []
    return [{"id": inv["id"], "email": inv["email"] or "", "role": inv["role"],
             "status": inv["status"], "createdAt": inv["createdAt"],
             "expiresAt": inv["expiresAt"]}
            for inv in store.list_invites(org_id)]


def _build_integrations(store: Store, campaign: Campaign, sessions: list[dict[str, Any]],
                        matches: list[dict[str, Any]], health: dict[str, Any],
                        org_id: Optional[int]) -> list[dict[str, Any]]:
    """Per-platform connection state: DB override wins, else derived from activity."""
    overrides = ({i["platform"]: i for i in store.list_integrations(org_id)}
                 if org_id is not None else {})
    out = []
    for p in SUPPORTED_PLATFORMS:
        ov = overrides.get(p)
        if ov is not None:
            connected = bool(ov["connected"])
            detail = ov.get("detail") or ("Connected" if connected else "Not connected")
            source = "override"
        else:
            has_activity = (any(s["platform"] == p for s in sessions)
                            or any(m["platform"] == p for m in matches))
            connected = bool(p == campaign.platform and has_activity
                             and health["overall"] != "halted")
            detail = "Connected" if connected else "Not connected"
            source = "derived"
        out.append({"id": p, "platform": p, "name": p.title(),
                    "connected": connected, "detail": detail, "source": source})
    return out


def org_campaign_count(store: Store, org_id: Optional[int]) -> int:
    """Non-archived campaigns the org currently holds — the number the plan's
    campaign cap (`billing.tier_campaign_cap`) is enforced against.

    Counts `campaign_meta` rows, which is every campaign the panel can create.
    Archived rows are excluded because the cap bounds the WORKING set: an org
    sitting at its cap can archive its way forward instead of being wedged.
    Single producer on purpose — the settings meter and the create gate in
    `server.py` must never disagree about what "used" means, the same way both
    read `store.get_subscription` for the lead cap."""
    if org_id is None:
        return 0
    return sum(1 for m in store.list_campaign_meta(org_id)
               if m.get("archived_at") is None)


def _build_billing(store: Store, org_id: Optional[int]) -> dict[str, Any]:
    """Org billing summary for `/api/settings` (mirrors `_build_integrations`).

    Reads the single choke point `store.get_subscription` and the SAME period
    anchor (`period_since`) the run gate uses, so the UI meter can never disagree
    with enforcement. `tiers` carries the full comparison grid (prices per
    interval, lead + campaign allowance) for the upgrade UI."""
    if org_id is None:
        sub = {"tier": "free", "interval": None, "status": "active",
               "lead_cap": billing.tier_lead_cap("free"), "current_period_end": None,
               "cancel_at_period_end": False}
        used = 0
        reveals_used = 0
    else:
        sub = store.get_subscription(org_id)
        since = store.period_since(org_id)
        used = store.count_leads_this_period(org_id, since)
        reveals_used = store.count_reveals_this_period(org_id, since)
    cap = sub["lead_cap"]
    usage_ratio = (used / cap) if cap else 0.0
    tiers = [
        {"tier": t, "displayName": meta["display_name"], "leadCap": meta["lead_cap"],
         # null = unlimited, NOT "unset" — the comparison grid must print
         # "Unlimited campaigns", never "0 campaigns".
         "campaignCap": meta["campaign_cap"],
         "selfServe": meta["self_serve"], "prices": meta["prices"]}
        for t, meta in billing.TIERS.items()
    ]
    return {
        "tier": sub["tier"],
        "interval": sub["interval"],
        "status": sub["status"],
        "periodEnd": sub["current_period_end"],
        "cancelAtPeriodEnd": bool(sub["cancel_at_period_end"]),
        "leadCap": cap,
        "leadsUsed": used,
        # Reveal allowance (v27). Leads are anonymized by default and un-anonymized
        # one at a time through the audited `POST /api/lead/reveal`, which is capped
        # by the SAME period allowance — otherwise a script walking the list turns a
        # per-lead endpoint back into the bulk export the redaction exists to stop.
        # `revealsUsed` counts DISTINCT leads revealed this period, never calls: the
        # drawer never caches a revealed lead, so it re-reveals on every open and a
        # call-counting meter would spend a Free org's ten on one lead opened ten
        # times. Same number as `leadCap` by construction — surfaced separately so
        # the UI can show the two meters filling independently, which they do.
        "revealCap": cap,
        "revealsUsed": reveals_used,
        # Campaign allowance (v27 plan limits). `campaignCap` is null on the
        # unlimited tiers, so the client gates on `!== null` — a falsy check would
        # read unlimited as zero and disable New Campaign for a paying org.
        "campaignCap": billing.tier_campaign_cap(sub["tier"]),
        "campaignsUsed": org_campaign_count(store, org_id),
        # Largest lead target ONE run may request. There is no separate per-run
        # allowance, so it is the period cap — read RESOLVED (`sub["lead_cap"]`,
        # which `get_subscription` has already overlaid with any per-org
        # `lead_cap_override`) rather than off the catalogue, because Scale's
        # catalogue cap is a deliberate fail-closed 0 and a provisioned Scale org
        # must not be offered a run target of zero. Identical to
        # `billing.tier_max_run_leads(tier)` for every un-overridden tier.
        "maxRunLeads": int(cap or 0),
        "usageRatio": round(usage_ratio, 4),
        "nearLimit": usage_ratio >= BILLING_NEAR_LIMIT_RATIO,
        "tiers": tiers,
    }


def _all_campaign_platforms(brief: dict[str, Any]) -> list[str]:
    """Every platform a stored brief discovers on (multi-platform plan C6): its
    channels' platforms when multi-platform, else the single flat platform. Drives
    the card's platform chips so a fanned-out campaign shows all of its channels."""
    channels = brief.get("channels")
    if isinstance(channels, list) and channels:
        plats = [str(c["platform"]) for c in channels
                 if isinstance(c, dict) and c.get("platform")]
        if plats:
            return plats
    return [brief.get("platform", "instagram")]


def _channel_to_camel(ch: dict[str, Any]) -> dict[str, Any]:
    """A stored snake_case channel dict → the camelCase wire shape (seed arrays) the
    edit form consumes. `includeHomeFeed` is omitted when the stored channel doesn't
    pin it, so the schema's seed-aware default is reconstructed on the client."""
    out: dict[str, Any] = {
        "platform": ch.get("platform", "instagram"),
        "seedHashtags": ch.get("seed_hashtags", []),
        "seedAccounts": ch.get("seed_accounts", []),
        "seedChannels": ch.get("seed_channels", []),
    }
    if "include_home_feed" in ch:
        out["includeHomeFeed"] = bool(ch["include_home_feed"])
    return out


def _brief_form_from_campaign(c: Campaign) -> dict[str, Any]:
    """The editable brief (camelCase) for the file-backed primary campaign."""
    return {
        "platform": c.platform, "goal": c.goal, "threshold": c.threshold,
        "languageMix": list(c.language_mix),
        "relevanceDef": c.relevance_def, "matchDef": c.match_def,
        "extractDef": c.extract_def,
        # Tuned classifier prompts so the edit form can show/round-trip them
        # instead of silently blanking a campaign's tuned system prompts.
        "relevancePrompt": c.relevance_prompt, "matchPrompt": c.match_prompt,
        "visionPrompt": c.vision_prompt,
        "seedHashtags": list(c.seed_hashtags), "seedAccounts": list(c.seed_accounts),
        "seedChannels": list(c.seed_channels),
        "includeHomeFeed": c.include_home_feed,
        # Multi-platform fan-out (camel, seed arrays). [] when single-platform.
        "channels": [
            {"platform": ch.platform,
             "seedHashtags": list(ch.seed_hashtags),
             "seedAccounts": list(ch.seed_accounts),
             "seedChannels": list(ch.seed_channels),
             "includeHomeFeed": ch.include_home_feed}
            for ch in c.channels
        ],
    }


def _brief_form_from_stored(brief: dict[str, Any]) -> dict[str, Any]:
    """The editable brief (camelCase) for a DB-authored campaign (stored snake_case)."""
    seed_hashtags = brief.get("seed_hashtags", [])
    seed_accounts = brief.get("seed_accounts", [])
    return {
        "platform": brief.get("platform", "instagram"),
        "goal": brief.get("goal", "lead"),
        "threshold": brief.get("threshold", 0.7),
        "languageMix": brief.get("language_mix", []),
        "relevanceDef": brief.get("relevance_def", ""),
        "matchDef": brief.get("match_def", ""),
        "extractDef": brief.get("extract_def", ""),
        "relevancePrompt": brief.get("relevance_prompt", ""),
        "matchPrompt": brief.get("match_prompt", ""),
        "visionPrompt": brief.get("vision_prompt", ""),
        "seedHashtags": seed_hashtags,
        "seedAccounts": seed_accounts,
        "seedChannels": brief.get("seed_channels", []),
        # Reflect the engine's seed-aware default (see config._resolve_home_feed)
        # when the brief doesn't pin it explicitly.
        "includeHomeFeed": bool(brief["include_home_feed"])
        if "include_home_feed" in brief else not (seed_hashtags or seed_accounts),
        # Multi-platform fan-out (camel, seed arrays). [] when single-platform.
        "channels": [_channel_to_camel(c) for c in brief.get("channels", [])
                     if isinstance(c, dict)],
    }


def _build_campaigns(store: Store, campaign: Campaign, sessions: list[dict[str, Any]],
                     matches: list[dict[str, Any]], today: datetime,
                     spend_cap_usd: float, org_id: Optional[int],
                     include_primary: bool = True,
                     leads_found: Optional[dict[str, int]] = None,
                     ) -> tuple[list[dict[str, Any]], Optional[int]]:
    """Markdown brief overlaid with editable campaign_meta, plus any UI-created drafts.

    Returns (campaigns, goal_target) — goal_target threads into the dashboard gauge.
    `include_primary=False` (empty-org state) omits the scoped/primary campaign and
    returns only the org's registered campaigns (here, none).

    E.7: `leads_found` maps campaign_id -> the deduped `run_events` estimate summed over
    that campaign's FINISHED runs — what those runs discovered, as opposed to the
    `matches` rows they delivered. Only a dead-lettered run can make the two differ (its
    leads never leave the worker), and the card puts the difference next to the spend it
    really incurred instead of rendering "$X spent, 0 leads" unexplained. Omitted/absent
    means "no evidence of an undelivered run", which is the healthy shape — the estimate
    can only ever be floored at the delivered rows, so a missing entry degrades to
    `delivery: "delivered"` rather than to a fabricated gap.
    """
    found = leads_found or {}
    cid = campaign.campaign_id
    if not include_primary:
        campaigns = []
        goal_target = None
        rollup = {r["campaignId"]: r for r in store.per_campaign_rollup(org_id)}
        for m in store.list_campaign_meta(org_id):
            campaigns.append(_draft_campaign(store, m, rollup, today, spend_cap_usd,
                                             leads_found=found))
        return campaigns, goal_target
    meta = store.get_campaign_meta(cid)
    spent = round(store.total_spend(cid), 4)
    leads = sum(1 for m in matches)
    won = sum(1 for m in matches if m["status"] in WIN_STATUS)
    # CPL stays guarded on `won`, NOT on delivered leads: a freshly harvested lead is
    # "new", so this reads `—` on every untriaged campaign, healthy or not. That is
    # exactly why a dash here is not the not-delivered signal — `delivery` below is.
    # It is also never synthesised from the found estimate: a cost per lead the customer
    # cannot open is a fiction.
    cpl = round(spent / won, 2) if won else None
    spark = _fill_values(
        {r["day"]: r["n"] for r in store.matches_by_day(
            cid, since_ts=(today - timedelta(days=14)).timestamp())}, today, 14)
    goal_target = meta["goal_target"] if meta and meta.get("goal_target") is not None else None
    primary = {
        "id": cid,
        "name": (meta["display_name"] if meta and meta.get("display_name")
                 else cid.replace("-", " ").title()),
        "goalType": campaign.goal,
        "status": meta["status"] if meta else "live",
        "platform": campaign.platform,
        # C6: every card carries both the primary `platform` and the full
        # `platforms` list (each channel when multi-platform, else just the one).
        "platforms": [ch.platform for ch in campaign.channels] if campaign.channels
        else [campaign.platform],
        "threshold": campaign.threshold,
        "languages": campaign.language_mix,
        "extractFields": _extract_fields(campaign),
        "startedAt": sessions[0]["date"] if sessions else "—",
        "brief": " ".join(campaign.relevance_def.split())[:240],
        "budgetCap": (meta["budget_cap"] if meta and meta.get("budget_cap") is not None
                      else spend_cap_usd),
        "goalTarget": goal_target,
        "briefForm": _brief_form_from_campaign(campaign),
        "spent": spent, "leads": leads, "cpl": cpl, "spark": spark,
        # E.7: `spent` and `leads` sit side by side on this card, and they have OPPOSITE
        # failure asymmetries — a nack banks the spend, an unacked run strands the leads.
        # `delivery` is what keeps the pair honest; the spend is never hidden or zeroed,
        # it is labelled as spend on an incomplete run.
        **delivery_state(found.get(cid, leads), leads, finished=True),
        "warmth": _warmth_payload(store, cid, campaign.platform, today),
        **_lifecycle_fields(meta),
    }
    campaigns = [primary]
    # UI-created drafts (campaign_meta rows without a markdown brief) — org-scoped.
    rollup = {r["campaignId"]: r for r in store.per_campaign_rollup(org_id)}
    for m in store.list_campaign_meta(org_id):
        if m["campaign_id"] == cid:
            continue
        campaigns.append(_draft_campaign(store, m, rollup, today, spend_cap_usd,
                                         leads_found=found))
    return campaigns, goal_target


def _warmth_payload(store: Store, campaign_id: str, platform: str,
                    today: datetime) -> dict[str, Any]:
    """The server-authoritative warmth verdict for a campaign card (warming PRD
    §5/§7.2). Single producer — `panel_org` delegates here, so every endpoint
    returns identical scores. Travels WITH the campaign so the client never needs
    CONFIG to render the gate."""
    now = today.timestamp()
    score = store.warmth_for_campaign(campaign_id, now=now, platform=platform)
    return score.as_payload(today.isoformat())


def _draft_campaign(store: Store, m: dict[str, Any], rollup: dict[str, Any],
                    today: datetime, spend_cap_usd: float, *,
                    leads_found: Optional[dict[str, int]] = None) -> dict[str, Any]:
    """A campaign card for a campaign_meta row (UI-created / non-primary).

    `leads_found` is the same campaign_id -> discovered-estimate map `_build_campaigns`
    documents; absent means "no undelivered run on record" (see E.7 there)."""
    mcid = m["campaign_id"]
    r = rollup.get(mcid, {})
    m_leads = int(r.get("leads", 0))
    m_won = int(r.get("won", 0))
    m_spent = round(float(r.get("spend", 0.0)), 4)
    stored_brief = store.get_campaign_brief(mcid)
    brief_form = _brief_form_from_stored(stored_brief) if stored_brief else None
    return {
        "id": mcid,
        "name": m.get("display_name") or mcid.replace("-", " ").title(),
        "goalType": (stored_brief or {}).get("goal", "lead"),
        "status": m["status"],
        "platform": (stored_brief or {}).get("platform", "instagram"),
        "platforms": _all_campaign_platforms(stored_brief or {}),   # C6
        # Default matches _brief_form_from_stored so the card and its edit form
        # never show two different thresholds for a brief that omits the key.
        "threshold": (stored_brief or {}).get("threshold", 0.7),
        "languages": (stored_brief or {}).get("language_mix", []),
        # Derive the chips from the brief's Extract section — the same keys the
        # cascade enforces — so a UI campaign's fields aren't shown as empty.
        "extractFields": parse_extract_fields((stored_brief or {}).get("extract_def", "")),
        "startedAt": "—", "brief": "",
        "budgetCap": m["budget_cap"] if m.get("budget_cap") is not None else spend_cap_usd,
        "goalTarget": m.get("goal_target"),
        "briefForm": brief_form,
        "spent": m_spent, "leads": m_leads,
        # Guarded on `won`, like every other CPL here — never on delivered leads, and
        # never synthesised from the found estimate (E.7).
        "cpl": round(m_spent / m_won, 2) if m_won else None,
        # E.7: the same spend/leads pairing the primary card carries.
        **delivery_state((leads_found or {}).get(mcid, m_leads), m_leads, finished=True),
        "spark": _fill_values(
            {r2["day"]: r2["n"] for r2 in store.matches_by_day(
                mcid, since_ts=(today - timedelta(days=14)).timestamp())}, today, 14),
        "warmth": _warmth_payload(
            store, mcid, (stored_brief or {}).get("platform", "instagram"), today),
        **_lifecycle_fields(m),
    }


def _soul_rules(soul: Soul) -> list[str]:
    rules = []
    for line in soul.text.splitlines():
        m = re.match(r"\s*-\s+(.*)", line)
        if m:
            rules.append(re.sub(r"\*\*(.*?)\*\*", r"\1", m.group(1)).strip())
    return rules[:8]


def _extract_fields(campaign: Campaign) -> list[str]:
    """The declared extract-field keys — the same set the cascade enforces as the
    model's output contract, so the panel chips can't drift from what's extracted."""
    return campaign.extract_fields()


def build_config(store: Store, campaign: Campaign, *, org_id: Optional[int],
                 role: str, today: datetime, spend_cap_usd: float,
                 skip_threshold: float, canary_limit: int,
                 watchlist_ttl_days: float) -> dict[str, Any]:
    """The CONFIG block: product/pacing defaults, org identity, caller role, and the
    per-org settings overlay. Shared by the single-campaign /api/state (build_raw) and
    the org-wide per-page endpoints (panel_org) so the CONFIG contract can't drift."""
    config = {
        "productName": "AIZU",
        "todayLabel": f"{today.strftime('%b')} {today.day}, {today.year}",
        "timezone": "Asia/Tashkent (UTC+5)",
        "matchThreshold": campaign.threshold,
        "skipRatioThreshold": skip_threshold,
        "budgetCapUsd": spend_cap_usd,
        "canaryLimitReels": canary_limit,
        "watchlistTtlDays": watchlist_ttl_days,
        "pacing": {
            "sessionsPerDay": "1–2", "sessionLength": "15–30 min",
            "reelsPerSession": "20–40", "dwell": "3–30 s",
            "betweenReels": "2–8 s", "window": "Daytime only",
        },
    }
    # v7: company identity + the caller's role (panel chrome + UI-gating mirror).
    org = store.get_organization(org_id) if org_id is not None else None
    config["organization"] = {
        "id": org_id,
        "name": org["name"] if org else config["productName"],
        "logo": org["logo"] if org else None,
        "description": org["description"] if org else None,
    }
    config["role"] = role
    # Editable per-org workspace settings override the hardcoded defaults above.
    if org_id is not None:
        for key, value in store.get_settings(org_id).items():
            if key == "pacing" and isinstance(value, dict):
                config["pacing"].update(value)
            else:
                config[key] = value
    return config


def build_raw(store: Store, soul: Soul, campaign: Campaign, *,
              org_id: Optional[int] = None, role: str = "owner",
              include_primary: bool = True,
              spend_cap_usd: float = 20.0, skip_threshold: float = 0.6,
              watchlist_ttl_days: float = 10.0, canary_limit: int = 5,
              today: Optional[datetime] = None,
              leads_found: Optional[dict[str, int]] = None) -> dict[str, Any]:
    """Build the panel state for ONE campaign, scoped to `org_id` and PRUNED to what
    `role` may see (the server is the real gate; this is server-side enforcement, not
    just UI). `org_id=None` (CLI/tests) is the unscoped full-owner view.

    `leads_found` (E.7) maps campaign_id -> the deduped `run_events` estimate over that
    campaign's FINISHED runs, so every card/report row that shows spend beside leads can
    say whether those leads were actually delivered. It is supplied by the caller rather
    than read here because the evidence lives in `run_events`, which this module has no
    reason to know about; omitted, every card renders the healthy `delivered` shape."""
    cid = campaign.campaign_id
    today = today or datetime.now(TASHKENT)
    sessions = _build_sessions(store, cid)
    config = build_config(
        store, campaign, org_id=org_id, role=role, today=today,
        spend_cap_usd=spend_cap_usd, skip_threshold=skip_threshold,
        canary_limit=canary_limit, watchlist_ttl_days=watchlist_ttl_days)

    matches = _build_matches(store, cid)
    campaigns, goal_target = _build_campaigns(
        store, campaign, sessions, matches, today, spend_cap_usd, org_id,
        include_primary=include_primary, leads_found=leads_found)

    # A member is strictly leads-only: return just config + lead context (the campaign
    # stubs feed the leads switcher). No dashboard/reports/team/integrations.
    if rbac.can(role, "view_leads") and not rbac.can(role, "view_dashboard"):
        camp_stubs = [{"id": c["id"], "name": c["name"],
                       "platform": c["platform"], "status": c["status"]}
                      for c in campaigns]
        return {"CONFIG": config, "CAMPAIGNS": camp_stubs, "MATCHES": matches}

    health = _build_health(store, cid, sessions, skip_threshold, canary_limit, org_id)
    raw: dict[str, Any] = {
        "CONFIG": config,
        "CAMPAIGNS": campaigns,
        "SESSIONS": sessions,
        "REELS": _build_reels(store, cid, watchlist_ttl_days),
        "MATCHES": matches,
        "PLATFORMS": _build_platforms(matches, sessions),
        "ESCALATION_LOG": _build_escalation_log(store, cid),
        "ALERTS": _build_alerts(store, cid),
        "HEALTH": health,
        "SOUL": {"file": "soul.md", "rules": _soul_rules(soul)},
        "DASHBOARD": _build_dashboard(store, cid, today, goal_target=goal_target,
                                      matches=matches, campaigns=campaigns),
        "REPORTS": _build_reports(store, cid, today, campaigns=campaigns, org_id=org_id),
    }
    # Team + integrations live under Settings → owner/admin only (omitted for viewer).
    if rbac.can(role, "view_team"):
        raw["TEAM"] = _build_team(store, org_id)
        raw["INVITES"] = _build_invites(store, org_id)
    if rbac.can(role, "view_settings"):
        raw["INTEGRATIONS"] = _build_integrations(
            store, campaign, sessions, matches, health, org_id)
    return raw


def _empty_campaign(org_id: Optional[int]) -> Campaign:
    """A synthetic campaign whose id matches no DB rows — yields an all-empty state
    for an org that has not created any campaigns yet (no cross-campaign bleed)."""
    return Campaign(
        campaign_id=f"__empty_org_{org_id}", goal="lead", threshold=0.7,
        escalate_band=(0.4, 0.75), language_mix=[], relevance_def="",
        match_def="", extract_def="", seed_direction="", raw="", path=Path("<empty>"))


def build_empty_raw(store: Store, soul: Soul, *, org_id: Optional[int], role: str,
                    spend_cap_usd: float = 20.0, skip_threshold: float = 0.6,
                    watchlist_ttl_days: float = 10.0, canary_limit: int = 5,
                    today: Optional[datetime] = None,
                    leads_found: Optional[dict[str, int]] = None) -> dict[str, Any]:
    """The panel state for an org with no campaigns yet — full key shape, all empty,
    plus the org's real team/invites. Reuses build_raw so the contract can't drift."""
    return build_raw(store, soul, _empty_campaign(org_id), org_id=org_id, role=role,
                     include_primary=False, spend_cap_usd=spend_cap_usd,
                     skip_threshold=skip_threshold, watchlist_ttl_days=watchlist_ttl_days,
                     canary_limit=canary_limit, today=today, leads_found=leads_found)
