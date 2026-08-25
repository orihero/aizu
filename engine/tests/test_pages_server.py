"""HTTP-level tests for the per-page, org-wide read endpoints that supersede the
monolithic /api/state: /api/dashboard, /api/campaigns, /api/leads, /api/reports,
/api/settings. Covers each endpoint's shape, the auth gate, the per-role RBAC matrix,
cross-org isolation, multi-campaign aggregation, and the /api/leads pagination contract.

Org A is seeded with TWO campaigns so aggregation (sum across campaigns) is observable;
org B has one campaign so isolation is observable. /api/state is intentionally NOT
touched here — its own suite (test_server / test_multitenancy_server) still guards it."""
import json
import os
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from aizu.panel import lead_uid
from aizu.server import serve
from aizu.core.store import Store

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
_INDEX_HTML = '<!doctype html><html><body><div id="root"></div></body></html>'
PW = "longenough1"

# Org A lead seed: (campaign, comment_id, username). 3 + 3 = 6 leads across 2 campaigns.
# NOTE the LAST row deliberately reuses camp-a1's `a1-c1` comment id under camp-a2:
# a lead's identity is the composite (campaign_id, platform, comment_id), and the same
# commenter really can surface under two campaigns. Flattening the payload to a bare
# comment id collapsed the two into one row, so the pagination/uniqueness assertions
# below double as the guard for that.
_A_LEADS = [
    ("camp-a1", "a1-c1", "a1u1"), ("camp-a1", "a1-c2", "a1u2"),
    ("camp-a2", "a2-c1", "a2u1"), ("camp-a2", "a2-c2", "a2u2"), ("camp-a2", "a2-c3", "a2u3"),
    ("camp-a2", "a1-c1", "a2dup"),
]
_A_TOTAL = len(_A_LEADS)

# A campaign that burned budget without producing a single lead — the shape that made
# the card report $0 while /api/reports summed the same money under spendByStage.
_DRY_CAMPAIGN = "camp-a3-dry"
_DRY_SPEND = 999.0


def _req(method, base, path, body=None, cookie=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or "null")


def _get(base, path, cookie=None):
    return _req("GET", base, path, None, cookie)


def _post(base, path, body, cookie=None):
    code, resp = _req("POST", base, path, body, cookie)
    return code, resp


def _cookie(set_cookie):
    return set_cookie.split(";", 1)[0]


def _signup(base, email, company="Co"):
    body = {"email": email, "password": PW, "companyName": company}
    req = urllib.request.Request(base + "/api/auth/signup",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        set_cookie = resp.headers.get("Set-Cookie")
        return json.loads(resp.read()), _cookie(set_cookie)


def _login(base, email):
    body = {"email": email, "password": PW}
    req = urllib.request.Request(base + "/api/auth/login",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        return _cookie(resp.headers.get("Set-Cookie"))


class _StubRunManager:
    def status(self, org_id=None):
        return {"active": None, "recent": []}

    def sweep_orphan_pause_files(self):
        return None


@pytest.fixture(scope="module")
def srv():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    panel_dir = tempfile.mkdtemp(suffix="-spa")
    (Path(panel_dir) / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
    httpd = serve(db_path, panel_dir, str(CONFIG), port=0, run_manager=_StubRunManager())
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    # Org A: owner + admin/member/viewer.
    a_resp, a_owner = _signup(base, "a-owner@x.io", company="Acme")
    org_a = a_resp["data"]["user"]["orgId"]
    cookies = {"owner": a_owner}
    for role in ("admin", "member", "viewer"):
        code, _ = _post(base, "/api/team",
                        {"op": "create", "email": f"a-{role}@x.io", "password": PW,
                         "role": role}, a_owner)
        assert code == 200
        cookies[role] = _login(base, f"a-{role}@x.io")

    # Org B: separate company.
    b_resp, b_owner = _signup(base, "b-owner@x.io", company="Beta")
    org_b = b_resp["data"]["user"]["orgId"]

    store = Store(db_path)
    for cid in ("camp-a1", "camp-a2"):
        store.upsert_campaign_meta(cid, org_id=org_a, status="live")
        store.upsert_campaign_brief(cid, {"platform": "instagram", "threshold": 0.7}, org_id=org_a)
    store.upsert_campaign_meta(_DRY_CAMPAIGN, org_id=org_a, status="live")
    store.upsert_campaign_brief(_DRY_CAMPAIGN, {"platform": "instagram", "threshold": 0.7},
                                org_id=org_a)
    store.log_spend(_DRY_CAMPAIGN, "vision", _DRY_SPEND, model="m")
    store.upsert_campaign_meta("camp-b", org_id=org_b, status="live")
    store.upsert_campaign_brief("camp-b", {"platform": "instagram", "threshold": 0.7}, org_id=org_b)
    for cid, comment_id, username in _A_LEADS:
        store.upsert_match(campaign_id=cid, reel_id="r", comment_id=comment_id, username=username,
                           text=f"hello from {username}", lang="uz", score=0.9, reason="x",
                           extracted=None, tier="local",
                           # v27: this — not the handle or the text above — is what an
                           # org-facing lead row actually shows, searches, and sorts on.
                           intent=f"Wants a quote for order {comment_id}")
    store.upsert_match(campaign_id="camp-b", reel_id="r", comment_id="b-c1", username="bu1",
                       text="hi", lang="uz", score=0.9, reason="x", extracted=None, tier="local")
    # One org-A lead set to 'interested' so the status facet has something to find.
    store.set_status("camp-a1", "a1-c1", "interested")
    # One ARCHIVED org-A lead (separate from _A_LEADS, so _A_TOTAL stays the ACTIVE
    # count): archived leads are "removed" — excluded from dashboards/aggregates and
    # the default leads list, reachable only via the Archived status filter.
    store.upsert_match(campaign_id="camp-a2", reel_id="r", comment_id="a2-arch",
                       username="a2arch", text="archived lead", lang="uz", score=0.9,
                       reason="x", extracted=None, tier="local")
    store.set_status("camp-a2", "a2-arch", "archived", reason="cleanup")
    store.close()

    yield {"base": base, "db": db_path, "cookies": cookies, "b_owner": b_owner,
           "org_a": org_a, "org_b": org_b}
    httpd.shutdown()
    os.unlink(db_path)
    shutil.rmtree(panel_dir, ignore_errors=True)


# ----- shape -----

def test_dashboard_shape(srv):
    code, resp = _get(srv["base"], "/api/dashboard", srv["cookies"]["owner"])
    assert code == 200
    for key in ("DASHBOARD", "MATCHES", "HEALTH", "ALERTS", "RUN", "CONFIG"):
        assert key in resp, f"dashboard missing {key}"
    assert {"today", "week", "month"} <= set(resp["DASHBOARD"])


def test_campaigns_shape(srv):
    code, resp = _get(srv["base"], "/api/campaigns", srv["cookies"]["owner"])
    assert code == 200
    for key in ("CAMPAIGNS", "SESSIONS", "RUN"):
        assert key in resp, f"campaigns missing {key}"
    # Every pooled session carries campaign attribution for client-side filtering.
    assert all("campaignId" in s for s in resp["SESSIONS"])


def test_reports_shape(srv):
    code, resp = _get(srv["base"], "/api/reports", srv["cookies"]["owner"])
    assert code == 200
    assert "REPORTS" in resp and "HEALTH" in resp
    assert {"today", "week", "month"} <= set(resp["REPORTS"])


def test_settings_shape(srv):
    code, resp = _get(srv["base"], "/api/settings", srv["cookies"]["owner"])
    assert code == 200
    for key in ("CONFIG", "TEAM", "INVITES", "INTEGRATIONS"):
        assert key in resp, f"settings missing {key}"


def test_leads_shape_is_paginated_envelope(srv):
    code, resp = _get(srv["base"], "/api/leads", srv["cookies"]["owner"])
    assert code == 200
    assert resp["ok"] is True
    data = resp["data"]
    for key in ("items", "total", "page", "pageSize", "stats", "platforms", "CONFIG"):
        assert key in data, f"leads payload missing {key}"


# ----- auth gate -----

@pytest.mark.parametrize("path", ["/api/dashboard", "/api/campaigns", "/api/leads",
                                  "/api/reports", "/api/settings"])
def test_endpoint_requires_auth(srv, path):
    code, _ = _get(srv["base"], path)
    assert code == 401


# ----- RBAC matrix: which roles may GET each page (others → 403) -----

_PAGE_RBAC = [
    ("/api/dashboard", {"owner", "admin", "viewer"}),
    ("/api/campaigns", {"owner", "admin", "viewer"}),
    ("/api/reports", {"owner", "admin", "viewer"}),
    ("/api/settings", {"owner", "admin"}),
    ("/api/leads", {"owner", "admin", "member", "viewer"}),
]


@pytest.mark.parametrize("path,allowed", _PAGE_RBAC)
def test_page_rbac_matrix(srv, path, allowed):
    for role, cookie in srv["cookies"].items():
        code, _ = _get(srv["base"], path, cookie)
        if role in allowed:
            assert code == 200, f"{role} should read {path} (got {code})"
        else:
            assert code == 403, f"{role} must be forbidden on {path} (got {code})"


# ----- aggregation across an org's campaigns -----

def test_dashboard_aggregates_all_campaigns(srv):
    resp = _get(srv["base"], "/api/dashboard", srv["cookies"]["owner"])[1]
    # MATCHES is the pooled, newest-first org-wide set: 2 + 3 = 5.
    assert len(resp["MATCHES"]) == _A_TOTAL
    ts = [m["capturedAt"]["ts"] for m in resp["MATCHES"]]
    assert ts == sorted(ts, reverse=True), "pooled matches must stay newest-first"


def test_campaigns_lists_every_org_campaign(srv):
    resp = _get(srv["base"], "/api/campaigns", srv["cookies"]["owner"])[1]
    ids = {c["id"] for c in resp["CAMPAIGNS"]}
    assert {"camp-a1", "camp-a2"} <= ids
    assert "camp-b" not in ids  # org B's campaign never leaks


def test_reports_per_campaign_covers_both(srv):
    resp = _get(srv["base"], "/api/reports", srv["cookies"]["owner"])[1]
    per = {c["id"] for c in resp["REPORTS"]["month"]["perCampaign"]}
    assert {"camp-a1", "camp-a2"} <= per


def test_leads_total_is_org_wide(srv):
    data = _get(srv["base"], "/api/leads", srv["cookies"]["owner"])[1]["data"]
    assert data["total"] == _A_TOTAL
    assert data["stats"]["total"] == _A_TOTAL


# ----- cross-org isolation -----

def test_org_b_sees_only_its_own_leads(srv):
    data = _get(srv["base"], "/api/leads", srv["b_owner"])[1]["data"]
    assert data["total"] == 1
    # v27: no username on an org-facing row, so assert what "only its own" actually
    # means (the campaign scope) — and that identity really is gone with it.
    assert all(m["campaignId"] == "camp-b" for m in data["items"])
    assert all("username" not in m and "text" not in m for m in data["items"])


def test_org_b_dashboard_excludes_org_a(srv):
    resp = _get(srv["base"], "/api/dashboard", srv["b_owner"])[1]
    assert len(resp["MATCHES"]) == 1


# ----- leads pagination + filter/sort -----

def test_leads_pagesize_clamped_to_max(srv):
    data = _get(srv["base"], "/api/leads?pageSize=9999", srv["cookies"]["owner"])[1]["data"]
    assert data["pageSize"] == 200  # LEADS_PAGE_SIZE_MAX


def test_leads_pages_do_not_overlap_and_cover_all(srv):
    seen = []
    for page in (1, 2, 3):
        data = _get(srv["base"], f"/api/leads?page={page}&pageSize=2",
                    srv["cookies"]["owner"])[1]["data"]
        assert data["total"] == _A_TOTAL
        seen.extend(m["id"] for m in data["items"])
    assert len(seen) == _A_TOTAL
    assert len(set(seen)) == _A_TOTAL  # no id appears on two pages


def test_leads_out_of_range_page_is_empty_with_real_total(srv):
    data = _get(srv["base"], "/api/leads?page=99&pageSize=2", srv["cookies"]["owner"])[1]["data"]
    assert data["items"] == []
    assert data["total"] == _A_TOTAL


def test_leads_non_int_params_fall_back(srv):
    data = _get(srv["base"], "/api/leads?page=abc&pageSize=xyz", srv["cookies"]["owner"])[1]["data"]
    assert data["page"] == 1
    assert data["pageSize"] == 50  # LEADS_PAGE_SIZE_DEFAULT


def test_leads_status_filter(srv):
    data = _get(srv["base"], "/api/leads?status=interested", srv["cookies"]["owner"])[1]["data"]
    assert data["total"] == 1
    assert all(m["status"] == "interested" for m in data["items"])


# ----- archived leads are "removed": hidden from dashboards + default list -----

def test_leads_default_list_excludes_archived(srv):
    """The unfiltered ("All") leads list omits the archived lead, and the tiles too."""
    data = _get(srv["base"], "/api/leads", srv["cookies"]["owner"])[1]["data"]
    ids = {m["id"] for m in data["items"]}
    assert "a2-arch" not in ids
    assert data["total"] == _A_TOTAL                # 5 active, archived not counted
    assert data["stats"]["total"] == _A_TOTAL
    assert data["stats"]["counts"].get("archived", 0) == 0


def test_leads_archived_visible_when_filter_selected(srv):
    """Explicitly selecting the Archived filter surfaces the archived lead."""
    data = _get(srv["base"], "/api/leads?status=archived", srv["cookies"]["owner"])[1]["data"]
    assert data["total"] == 1
    # `id` is the composite lead identity, not the bare comment id.
    assert [m["id"] for m in data["items"]] == [lead_uid("camp-a2", "instagram", "a2-arch")]


def test_dashboard_matches_exclude_archived(srv):
    """Dashboard MATCHES (the basis for every client-side lead stat) drop archived."""
    resp = _get(srv["base"], "/api/dashboard", srv["cookies"]["owner"])[1]
    assert len(resp["MATCHES"]) == _A_TOTAL
    assert all(m["status"] != "archived" for m in resp["MATCHES"])


# ----- campaign filter + campaign options -----

def test_leads_campaign_filter_scopes_list_and_tiles(srv):
    """?campaign= narrows both the list and the stat tiles to that one campaign."""
    a1 = _get(srv["base"], "/api/leads?campaign=camp-a1", srv["cookies"]["owner"])[1]["data"]
    assert a1["total"] == 2 and a1["stats"]["total"] == 2
    assert all(m["campaignId"] == "camp-a1" for m in a1["items"])
    a2 = _get(srv["base"], "/api/leads?campaign=camp-a2", srv["cookies"]["owner"])[1]["data"]
    # 4 active (incl. the `a1-c1` duplicate camp-a2 owns); the archived one stays hidden.
    assert a2["total"] == 4


def test_leads_payload_lists_org_campaigns(srv):
    """The leads payload carries the org's campaigns so the filter dropdown can render
    them even for a leads-only member (who can't read /api/campaigns)."""
    data = _get(srv["base"], "/api/leads", srv["cookies"]["member"])[1]["data"]
    ids = {c["id"] for c in data["campaigns"]}
    assert {"camp-a1", "camp-a2"} <= ids
    assert "camp-b" not in ids  # org B never leaks
    assert all(c.get("name") for c in data["campaigns"])


def test_leads_query_search_matches_intent_not_identity(srv):
    """v27: free-text search runs over what a customer can SEE — intent, reason and
    the extracted values — because username/text are no longer in the payload."""
    data = _get(srv["base"], "/api/leads?q=a2-c2", srv["cookies"]["owner"])[1]["data"]
    assert data["total"] == 1
    assert data["items"][0]["intent"] == "Wants a quote for order a2-c2"
    assert "username" not in data["items"][0] and "text" not in data["items"][0]


def test_leads_query_no_longer_searches_the_hidden_username(srv):
    """The handle is still in the DB. Searching it must find NOTHING — otherwise the
    search box is an oracle that confirms which handles an org's leads belong to,
    which is the same disclosure the payload redaction just removed."""
    data = _get(srv["base"], "/api/leads?q=a2u1", srv["cookies"]["owner"])[1]["data"]
    assert data["total"] == 0


def test_leads_sort_by_intent_ascending(srv):
    """`sort=username` went away with the field; intent is the sortable lead prose."""
    data = _get(srv["base"], "/api/leads?sort=intent&dir=asc&pageSize=200",
                srv["cookies"]["owner"])[1]["data"]
    intents = [m["intent"] for m in data["items"]]
    assert intents == sorted(intents)


def test_a_stale_username_sort_degrades_instead_of_500ing(srv):
    """A panel bundle cached from before v27 still sends `?sort=username`. The key is
    gone from the row, so a strict lookup would 500 every leads page for exactly the
    clients that have not reloaded yet."""
    code, resp = _get(srv["base"], "/api/leads?sort=username&dir=asc&pageSize=200",
                      srv["cookies"]["owner"])
    assert code == 200 and resp["data"]["total"] == _A_TOTAL


# ----- lead identity: the composite (campaign, platform, comment) key -----

def test_lead_ids_are_unique_across_campaigns_sharing_a_comment_id(srv):
    """Two campaigns holding the same comment id must stay two distinct panel rows.

    The payload used to flatten a lead to `"id": comment_id`, so the shared
    `a1-c1` collapsed to ONE row: clicking it in camp-a2 resolved camp-a1's lead
    and a status write landed on the wrong campaign's record."""
    data = _get(srv["base"], "/api/leads?pageSize=200", srv["cookies"]["owner"])[1]["data"]
    ids = [m["id"] for m in data["items"]]
    assert len(set(ids)) == len(ids), "every lead row needs its own id"
    dupes = [m for m in data["items"] if m["commentId"] == "a1-c1"]
    assert {m["campaignId"] for m in dupes} == {"camp-a1", "camp-a2"}
    assert len({m["id"] for m in dupes}) == 2
    # The id must resolve back to the row's own composite key, never a sibling's.
    for m in data["items"]:
        assert m["id"] == lead_uid(m["campaignId"], m["platform"], m["commentId"])


def test_status_write_on_a_shared_comment_id_hits_only_its_own_campaign(srv):
    """The write path resolves the full composite key, so marking camp-a2's copy of
    `a1-c1` leaves camp-a1's copy untouched."""
    data = _get(srv["base"], "/api/leads?pageSize=200", srv["cookies"]["owner"])[1]["data"]
    by_id = {m["id"]: m for m in data["items"]}
    target = by_id[lead_uid("camp-a2", "instagram", "a1-c1")]
    code, resp = _post(srv["base"], "/api/status",
                       {"campaignId": target["campaignId"], "platform": target["platform"],
                        "commentId": target["commentId"], "status": "in_progress"},
                       srv["cookies"]["owner"])
    assert code == 200 and resp["ok"] is True
    after = {m["id"]: m for m in
             _get(srv["base"], "/api/leads?pageSize=200", srv["cookies"]["owner"])[1]["data"]["items"]}
    assert after[lead_uid("camp-a2", "instagram", "a1-c1")]["status"] == "in_progress"
    assert after[lead_uid("camp-a1", "instagram", "a1-c1")]["status"] == "interested"


# ----- spend is reported from spend_log, not inferred from leads -----

def test_campaign_with_spend_but_no_leads_reports_its_spend(srv):
    """A campaign that burned budget without capturing a lead still shows the money.

    The rollup used to be built from `matches`, so a lead-less campaign had no row
    at all and the card defaulted to `spent: 0` — while /api/reports summed the very
    same dollars under spendByStage."""
    resp = _get(srv["base"], "/api/campaigns", srv["cookies"]["owner"])[1]
    card = next(c for c in resp["CAMPAIGNS"] if c["id"] == _DRY_CAMPAIGN)
    assert card["leads"] == 0
    assert card["spent"] == _DRY_SPEND
    # No lead means no cost-per-lead — a null, never a division by zero.
    assert card["cpl"] is None


def test_campaigns_and_reports_agree_on_org_spend(srv):
    """The two payloads must not contradict each other about the same money."""
    cards = _get(srv["base"], "/api/campaigns", srv["cookies"]["owner"])[1]["CAMPAIGNS"]
    month = _get(srv["base"], "/api/reports", srv["cookies"]["owner"])[1]["REPORTS"]["month"]
    card_total = round(sum(c["spent"] for c in cards), 4)
    stage_total = round(sum(s["value"] for s in month["spendByStage"]), 4)
    assert card_total == stage_total == _DRY_SPEND
    per_campaign = {c["id"]: c["spend"] for c in month["perCampaign"]}
    assert per_campaign[_DRY_CAMPAIGN] == _DRY_SPEND
