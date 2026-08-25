"""v27 reveal-on-demand — POST /api/lead/reveal.

Leads are anonymized by default (username + comment text are stripped from every
org-facing payload). This endpoint is the ONE sanctioned way back to a lead's
identity, and every property that makes it safe is asserted here:

  * role gate — owner/admin/member may reveal, `viewer` may not (the read-only
    role reads the anonymized list and nothing else);
  * 404-not-403 for a lead in another org, so the endpoint cannot be used as a
    cross-tenant existence oracle;
  * an audit row on EVERY call, including the denials — those are the rows an
    operator actually goes looking for;
  * it is a READ: no status change, no history row, no `updated_at` bump;
  * exactly ONE lead comes back, and nothing else from the `matches` row;
  * `reelId` — the pointer to the public post — reaches an org HERE and nowhere
    else, which is what makes every route to a lead's identity an audited one;
  * the disclosure is CAPPED by the plan's period lead allowance, counted in
    DISTINCT leads rather than calls, so a scripted loop over the anonymized list
    cannot rebuild the bulk export the redaction exists to prevent.
"""
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


# --------------------------------------------------------------------------- #
# HTTP helpers (same shape as test_rbac_hardening.py)
# --------------------------------------------------------------------------- #
def _post(base, path, body, cookie=None):
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(base + path, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or "null"), resp.headers.get("Set-Cookie")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or "null"), e.headers.get("Set-Cookie")


def _cookie(set_cookie):
    return set_cookie.split(";", 1)[0]


def _signup(base, email, company="Co"):
    code, resp, set_cookie = _post(base, "/api/auth/signup",
                                   {"email": email, "password": PW, "companyName": company})
    assert code == 200, resp
    return resp["data"]["user"], _cookie(set_cookie)


def _add_user(base, owner_cookie, email, role):
    """Create a teammate with `role` and log them in; returns their session cookie."""
    code, resp, _ = _post(base, "/api/team",
                          {"op": "create", "email": email, "password": PW, "role": role},
                          owner_cookie)
    assert code == 200, resp
    code, resp, set_cookie = _post(base, "/api/auth/login", {"email": email, "password": PW})
    assert code == 200, resp
    return _cookie(set_cookie)


def _seed_lead(db, campaign_id, org_id, *, comment_id, username, text,
               reel_id, platform="instagram"):
    """Register `campaign_id` to `org_id` and give it one captured lead."""
    store = Store(db)
    try:
        store.upsert_campaign_meta(campaign_id, org_id=org_id, display_name=campaign_id)
        store.upsert_match(
            campaign_id=campaign_id, reel_id=reel_id, comment_id=comment_id,
            username=username, text=text, lang="en", score=0.9,
            reason="asked for the product", extracted={"size": "42"}, tier="local",
            platform=platform, intent="Interested in sneakers, size 42")
    finally:
        store.close()


def _reveal_rows(db, org_id):
    store = Store(db)
    try:
        return [r for r in store.audit_entries(org_id) if r["action"] == "reveal_lead"]
    finally:
        store.close()


def _match_row(db, campaign_id, comment_id):
    store = Store(db)
    try:
        return next(m for m in store.matches(campaign_id) if m["comment_id"] == comment_id)
    finally:
        store.close()


@pytest.fixture
def srv():
    """Two orgs, each with one campaign and one lead. Org A is the caller throughout;
    org B exists solely so "someone else's lead" is a real row, not a missing one —
    the 404 has to hide an EXISTING lead to be worth anything."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    panel_dir = tempfile.mkdtemp(suffix="-spa")
    (Path(panel_dir) / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
    httpd = serve(db_path, panel_dir, str(CONFIG), port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    owner_a, cookie_a = _signup(base, "owner@a.io", company="Acme")
    owner_b, cookie_b = _signup(base, "owner@b.io", company="Beta")
    _seed_lead(db_path, "camp-a", owner_a["orgId"], comment_id="c-1",
               username="alice_a", text="do you ship to Tashkent?", reel_id="reel-a")
    # A SECOND lead in the caller's own campaign — the response must not carry it.
    _seed_lead(db_path, "camp-a", owner_a["orgId"], comment_id="c-2",
               username="bob_a", text="what is the price?", reel_id="reel-a2")
    _seed_lead(db_path, "camp-b", owner_b["orgId"], comment_id="c-9",
               username="carol_b", text="foreign org lead", reel_id="reel-b")

    yield {"base": base, "db": db_path,
           "owner": cookie_a, "orgA": owner_a["orgId"], "ownerId": owner_a["id"],
           "orgB": owner_b["orgId"], "ownerB": cookie_b}

    httpd.shutdown()
    os.unlink(db_path)
    shutil.rmtree(panel_dir, ignore_errors=True)


def _body(comment_id="c-1", campaign_id="camp-a", platform="instagram"):
    return {"campaignId": campaign_id, "commentId": comment_id, "platform": platform}


# --------------------------------------------------------------------------- #
# Role matrix
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", ["owner", "admin", "member"])
def test_permitted_roles_can_reveal(srv, role):
    # Arrange
    cookie = srv["owner"] if role == "owner" \
        else _add_user(srv["base"], srv["owner"], f"{role}@a.io", role)
    # Act
    code, resp, _ = _post(srv["base"], "/api/lead/reveal", _body(), cookie)
    # Assert — the identity comes back for that one lead.
    assert code == 200, resp
    assert resp["data"]["username"] == "alice_a"
    assert resp["data"]["text"] == "do you ship to Tashkent?"
    assert resp["data"]["reelId"] == "reel-a"


def test_viewer_is_refused(srv):
    # Arrange — the read-only role may see the anonymized list, never the identity.
    viewer = _add_user(srv["base"], srv["owner"], "viewer@a.io", "viewer")
    # Act
    code, resp, _ = _post(srv["base"], "/api/lead/reveal", _body(), viewer)
    # Assert
    assert code == 403, resp
    assert resp["ok"] is False
    assert "username" not in json.dumps(resp)
    assert "alice_a" not in json.dumps(resp)


def test_anonymous_caller_is_refused(srv):
    # Act — no session cookie at all.
    code, resp, _ = _post(srv["base"], "/api/lead/reveal", _body())
    # Assert — the auth gate, not the role gate.
    assert code == 401, resp
    assert "alice_a" not in json.dumps(resp)


# --------------------------------------------------------------------------- #
# BOLA: a foreign lead is a 404, never a 403
# --------------------------------------------------------------------------- #
def test_foreign_lead_is_404_not_403(srv):
    # Act — org A asks for org B's real, existing lead.
    code, resp, _ = _post(srv["base"], "/api/lead/reveal",
                          _body(comment_id="c-9", campaign_id="camp-b"), srv["owner"])
    # Assert — indistinguishable from a lead that never existed (no existence oracle).
    assert code == 404, resp
    assert resp["error"] == "unknown lead"
    assert "carol_b" not in json.dumps(resp)


def test_unknown_campaign_and_foreign_campaign_answer_identically(srv):
    # Act
    foreign = _post(srv["base"], "/api/lead/reveal",
                    _body(comment_id="c-9", campaign_id="camp-b"), srv["owner"])
    missing = _post(srv["base"], "/api/lead/reveal",
                    _body(comment_id="c-9", campaign_id="camp-nope"), srv["owner"])
    # Assert — same status AND same message; a difference in either is the oracle.
    assert foreign[0] == missing[0] == 404
    assert foreign[1]["error"] == missing[1]["error"]


def test_unknown_comment_in_own_campaign_is_404(srv):
    # Act
    code, resp, _ = _post(srv["base"], "/api/lead/reveal",
                          _body(comment_id="c-does-not-exist"), srv["owner"])
    # Assert
    assert code == 404, resp
    assert resp["error"] == "unknown lead"


def test_wrong_platform_for_a_real_comment_is_404(srv):
    # Arrange / Act — the composite key is (campaign, platform, comment); a right
    # comment id under the wrong platform is a different lead, i.e. no lead.
    code, resp, _ = _post(srv["base"], "/api/lead/reveal",
                          _body(platform="linkedin"), srv["owner"])
    # Assert
    assert code == 404, resp
    assert "alice_a" not in json.dumps(resp)


def test_org_is_resolved_from_the_session_not_the_body(srv):
    # Act — org A sends a body that names org B in every way it can.
    code, resp, _ = _post(srv["base"], "/api/lead/reveal",
                          {"campaignId": "camp-b", "commentId": "c-9",
                           "platform": "instagram", "orgId": srv["orgB"]},
                          srv["owner"])
    # Assert — the body's orgId is inert; the session still says org A.
    assert code == 404, resp
    assert "carol_b" not in json.dumps(resp)


# --------------------------------------------------------------------------- #
# Audit — on success AND on denial
# --------------------------------------------------------------------------- #
def test_success_writes_one_audit_row(srv):
    # Act
    code, _, _ = _post(srv["base"], "/api/lead/reveal", _body(), srv["owner"])
    assert code == 200
    # Assert — exactly one row, targeting the composite lead uid.
    rows = _reveal_rows(srv["db"], srv["orgA"])
    assert len(rows) == 1
    assert rows[0]["target"] == lead_uid("camp-a", "instagram", "c-1")
    assert rows[0]["actorUserId"] == srv["ownerId"]
    assert json.loads(rows[0]["detail"]) == {"campaignId": "camp-a",
                                             "platform": "instagram",
                                             "result": "revealed"}


def test_denied_attempt_is_audited(srv):
    # Arrange
    viewer = _add_user(srv["base"], srv["owner"], "viewer@a.io", "viewer")
    # Act
    code, _, _ = _post(srv["base"], "/api/lead/reveal", _body(), viewer)
    assert code == 403
    # Assert — the refusal is recorded, not silently dropped. This is the row an
    # operator investigating an attempted disclosure actually needs.
    rows = _reveal_rows(srv["db"], srv["orgA"])
    assert len(rows) == 1
    assert rows[0]["target"] == lead_uid("camp-a", "instagram", "c-1")
    assert json.loads(rows[0]["detail"])["result"] == "denied"


def test_foreign_lead_attempt_is_audited_against_the_caller_org(srv):
    # Act
    code, _, _ = _post(srv["base"], "/api/lead/reveal",
                       _body(comment_id="c-9", campaign_id="camp-b"), srv["owner"])
    assert code == 404
    # Assert — org A's trail records the attempt; org B's trail is untouched (the
    # audit row must not become the leak the 404 just prevented).
    mine = _reveal_rows(srv["db"], srv["orgA"])
    assert len(mine) == 1
    assert json.loads(mine[0]["detail"])["result"] == "not_found"
    assert _reveal_rows(srv["db"], srv["orgB"]) == []


def test_every_call_writes_its_own_row(srv):
    # Act — reopening the drawer re-reveals, and each re-reveal is its own event.
    for _ in range(3):
        assert _post(srv["base"], "/api/lead/reveal", _body(), srv["owner"])[0] == 200
    # Assert
    assert len(_reveal_rows(srv["db"], srv["orgA"])) == 3


# --------------------------------------------------------------------------- #
# Reveal is a READ
# --------------------------------------------------------------------------- #
def test_reveal_does_not_mutate_the_lead(srv):
    # Arrange
    before = _match_row(srv["db"], "camp-a", "c-1")
    # Act
    assert _post(srv["base"], "/api/lead/reveal", _body(), srv["owner"])[0] == 200
    # Assert — same row, byte for byte, and no status-history entry appeared.
    after = _match_row(srv["db"], "camp-a", "c-1")
    assert after == before
    store = Store(srv["db"])
    try:
        assert store.status_history_by_lead("camp-a") == {}
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# Exactly one lead — no bulk path, no widening parameter
# --------------------------------------------------------------------------- #
def test_response_carries_no_other_lead(srv):
    # Act
    code, resp, _ = _post(srv["base"], "/api/lead/reveal", _body(), srv["owner"])
    # Assert — the sibling lead in the SAME campaign is nowhere in the body, and the
    # payload is a flat single record (nothing list-shaped to hold a second one).
    assert code == 200
    blob = json.dumps(resp)
    assert "bob_a" not in blob and "what is the price?" not in blob
    assert set(resp["data"]) == {"id", "commentId", "platform", "username",
                                 "text", "reelId"}
    assert not any(isinstance(v, (list, dict)) for v in resp["data"].values())


def test_no_bulk_form_of_the_request(srv):
    # Act — the plural body a bulk client would send.
    code, resp, _ = _post(srv["base"], "/api/lead/reveal",
                          {"campaignId": "camp-a",
                           "commentIds": ["c-1", "c-2"], "platform": "instagram"},
                          srv["owner"])
    # Assert — rejected at validation; there is no list form to fall back to.
    assert code == 400, resp
    assert "commentId" in resp["error"]
    assert "alice_a" not in json.dumps(resp)


def test_extra_widening_parameters_are_inert(srv):
    # Act — every "give me all of them" knob someone might try to bolt on.
    code, resp, _ = _post(srv["base"], "/api/lead/reveal",
                          {"campaignId": "camp-a", "commentId": "c-1",
                           "platform": "instagram",
                           "all": True, "limit": 100, "status": "new"},
                          srv["owner"])
    # Assert — still exactly the one named lead.
    assert code == 200, resp
    assert resp["data"]["commentId"] == "c-1"
    assert "bob_a" not in json.dumps(resp)


def test_reveal_leaks_nothing_beyond_handle_comment_and_post(srv):
    # Arrange — the row carries scored/internal fields the customer must not get.
    code, resp, _ = _post(srv["base"], "/api/lead/reveal", _body(), srv["owner"])
    # Assert — the response is CONSTRUCTED from named keys, so a new `matches`
    # column can never ride along.
    assert code == 200
    for internal in ("reason", "score", "tier", "extracted", "sessionId",
                     "session_id", "found_by_models", "source", "intent"):
        assert internal not in resp["data"]


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("body,expected", [
    ("not-an-object", "body must be a JSON object"),
    ({"commentId": "c-1"}, "missing or empty field: campaignId"),
    ({"campaignId": "camp-a"}, "missing or empty field: commentId"),
    ({"campaignId": "camp-a", "commentId": "  "}, "missing or empty field: commentId"),
    ({"campaignId": "camp-a", "commentId": "c-1", "platform": ""},
     "platform, if present, must be a non-empty string"),
])
def test_malformed_bodies_are_400(srv, body, expected):
    # Act
    code, resp, _ = _post(srv["base"], "/api/lead/reveal", body, srv["owner"])
    # Assert
    assert code == 400, resp
    assert resp["error"] == expected


def test_platform_defaults_to_instagram(srv):
    # Act — an older client that omits platform still resolves the default-platform lead.
    code, resp, _ = _post(srv["base"], "/api/lead/reveal",
                          {"campaignId": "camp-a", "commentId": "c-1"}, srv["owner"])
    # Assert
    assert code == 200, resp
    assert resp["data"]["platform"] == "instagram"
    assert resp["data"]["username"] == "alice_a"


# --------------------------------------------------------------------------- #
# The list itself offers no way to the post
# --------------------------------------------------------------------------- #
def _get(base, path, cookie):
    req = urllib.request.Request(base + path, headers={"Cookie": cookie})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def test_no_org_endpoint_ships_the_post_pointer(srv):
    """The reveal is only audited if it is the ONLY route to the post.

    `reelId` names a public page carrying the handle and the comment in plain
    sight, so an org-facing one turned the anonymized list into a two-step export:
    read the ids, open the posts, never call this endpoint. Both org list surfaces
    are swept whole — key and value — so a rename cannot slip the id back in.
    """
    # Act
    leads = _get(srv["base"], "/api/leads", srv["owner"])
    state = _get(srv["base"], "/api/state", srv["owner"])
    # Assert — no `reelId` KEY anywhere on either org surface...
    for name, payload in (("leads", leads), ("state", state)):
        assert "reelId" not in json.dumps(payload), \
            f"/api/{name} still carries a post pointer"
    # ...and no post id by VALUE on any lead row. Scoped to the lead rows on purpose:
    # the watchlist (`REELS`) legitimately names the posts the campaign SCANNED —
    # that is what it is for — and a payload-wide value sweep would fail on it.
    rows = leads["data"]["items"] + state["MATCHES"]
    assert rows, "the fixture must actually produce lead rows for this to mean anything"
    for row in rows:
        assert "reel-a" not in json.dumps(row), "a lead row still names its post"
    # The audited endpoint hands it over, so the post stays ONE click away — audited.
    code, resp, _ = _post(srv["base"], "/api/lead/reveal", _body(), srv["owner"])
    assert code == 200 and resp["data"]["reelId"] == "reel-a"


def _campaign_with_three_scanned_posts(srv):
    """A REAL campaign (created through the API, so `/api/state` can resolve it),
    three relevant scanned posts, one lead, and the watchlist row the engine writes
    for the post that produced it.

    Three posts is the minimum shape in which the finding below is visible: with one
    scanned post there is nothing to distinguish, and with none the `REELS` array is
    empty — which is exactly why the payload sweep above missed it.
    """
    # The fixture org already holds `camp-a` and Free allows exactly one campaign,
    # so lift the plan first — the campaign cap is not what this test is about.
    _set_tier(srv["db"], srv["orgA"], "starter", ts=1.0)
    code, resp, _ = _post(srv["base"], "/api/campaign", {
        "campaignId": "scan", "displayName": "Scan", "status": "live",
        "brief": {"platform": "instagram", "threshold": 0.7,
                  "relevanceDef": "sneaker shopping", "matchDef": "buyer intent",
                  "extractDef": "- phone", "languageMix": ["en"]},
    }, srv["owner"])
    assert code == 200, resp
    cid = resp["data"]["campaign_id"]
    store = Store(srv["db"])
    try:
        for rid, caption in (("post-lead", "Nike drop"), ("post-decoy1", "Adidas sale"),
                             ("post-decoy2", "Puma restock")):
            store.mark_seen(cid, rid, relevant=True, author="shop_uz",
                            caption=caption, source="#krossovka")
        store.upsert_match(
            campaign_id=cid, reel_id="post-lead", comment_id="c-scan",
            username="alice_a", text="do you ship to Tashkent?", lang="en",
            score=0.9, reason="asked for the product", extracted={"size": "42"},
            tier="local", platform="instagram", intent="Wants sneakers, size 42")
        # `add_to_watchlist` is called from ONE place in the engine — after a comment
        # batch, `if found` — so this row means "this post produced a lead".
        store.add_to_watchlist(cid, "post-lead")
    finally:
        store.close()
    return cid


def test_the_watchlist_does_not_mark_which_post_produced_the_lead(srv):
    """The re-join the sweep above did not cover: `REELS` names every scanned post,
    and until v27 it also marked the ones that produced leads.

    `Store.add_to_watchlist` runs only `if found`, so every watchlist-derived field
    is a flag on the lead-bearing posts. Two of them said so exactly — `match_count`
    (1 vs 0) and the TTL `expires_at` (10 days vs none) — and the second is why this
    test compares whole rows rather than one key: a fix that dropped only the count
    would have moved the leak to its neighbour, not closed it.

    Reading it needs no reveal call and writes no audit row: filter `REELS` for the
    marked post, open its public URL, read every commenter's handle and comment.
    """
    # Arrange
    cid = _campaign_with_three_scanned_posts(srv)
    # Act
    state = _get(srv["base"], f"/api/state?campaign={cid}", srv["owner"])
    reels = {r["id"]: r for r in state["REELS"]}
    # Assert — the posts themselves are the product and still ship (contract A7)...
    assert set(reels) == {"post-lead", "post-decoy1", "post-decoy2"}
    assert reels["post-lead"]["caption"] == "Nike drop"

    # ...but nothing on the lead-bearing row distinguishes it from the two decoys.
    def _comparable(row):
        return {k: v for k, v in row.items() if k not in ("id", "thumbSeed", "caption")}

    assert _comparable(reels["post-lead"]) == _comparable(reels["post-decoy1"]), \
        "the watchlisted post is still distinguishable from an unwatchlisted one"
    for key in ("newSinceLastPoll", "expiresInDays"):
        assert key not in reels["post-lead"], f"{key} still marks the lead-bearing post"


def test_a_viewer_cannot_identify_the_lead_bearing_post(srv):
    """The role RBAC refuses `reveal_lead` outright must not have a second route.

    A `viewer` is the read-only role: it may read the anonymized list and may NOT
    reveal (`test_viewer_is_refused`). It is nonetheless served the full `/api/state`,
    watchlist included — so the marker above handed the one role the design most
    wants locked down a complete, unaudited way to every lead's identity.
    """
    # Arrange
    cid = _campaign_with_three_scanned_posts(srv)
    viewer = _add_user(srv["base"], srv["owner"], "watcher@a.io", "viewer")
    # Act
    state = _get(srv["base"], f"/api/state?campaign={cid}", viewer)
    # Assert — a viewer sees WHAT was scanned and nothing about which post paid off.
    assert [r["id"] for r in state["REELS"]], "the viewer must actually get REELS"
    assert not [r for r in state["REELS"]
                if r.get("newSinceLastPoll") or r.get("expiresInDays")]
    # And the sanctioned route stays shut for them.
    code, _, _ = _post(srv["base"], "/api/lead/reveal",
                       _body(comment_id="c-scan", campaign_id=cid), viewer)
    assert code == 403


# --------------------------------------------------------------------------- #
# Plan cap — the endpoint is per-lead, so without one a loop IS a bulk export
# --------------------------------------------------------------------------- #
def _set_tier(db, org_id, tier, *, ts):
    """Switch the org's plan. `upsert_subscription` drops any event whose stamp is not
    strictly newer than the stored one (the monotonic webhook guard), so each call
    needs its own ascending `ts`."""
    store = Store(db)
    try:
        store.upsert_subscription(org_id, last_event_ts=ts, tier=tier, status="active")
    finally:
        store.close()


def _seed_leads(db, org_id, n, *, first=0, campaign_id="camp-a"):
    """`n` more leads in the caller's own campaign, ids `c-<i>` from `first`."""
    for i in range(first, first + n):
        _seed_lead(db, campaign_id, org_id, comment_id=f"cap-{i}",
                   username=f"user{i}", text=f"comment {i}", reel_id=f"reel-{i}")


def _reveal(srv, comment_id):
    return _post(srv["base"], "/api/lead/reveal", _body(comment_id=comment_id),
                 srv["owner"])


def test_reveals_are_capped_by_the_plan_allowance(srv):
    """Free = 10 leads a period, so the ELEVENTH distinct lead is refused. The cap is
    the plan's lead allowance and nothing else: an org may not un-anonymize more leads
    than its plan let it capture in the first place."""
    # Arrange — eleven distinct leads on a Free org (the signup default).
    _seed_leads(srv["db"], srv["orgA"], 11)
    # Act — ten reveals are inside the allowance...
    for i in range(10):
        code, resp, _ = _reveal(srv, f"cap-{i}")
        assert code == 200, (i, resp)
    # Assert — ...and the boundary itself refuses, with the number and the reset in it.
    code, resp, _ = _reveal(srv, "cap-10")
    assert code == 402, resp
    assert "10 lead reveals" in resp["error"] and "Resets" in resp["error"]
    assert "user10" not in json.dumps(resp)


def test_a_refusal_at_the_cap_is_audited(srv):
    """The refused attempt is exactly the row an operator wants: it is what a scripted
    enumeration looks like from the trail's side."""
    # Arrange
    _seed_leads(srv["db"], srv["orgA"], 11)
    for i in range(10):
        assert _reveal(srv, f"cap-{i}")[0] == 200
    # Act
    assert _reveal(srv, "cap-10")[0] == 402
    # Assert — the 402 wrote its own row, distinguishable from a success.
    rows = _reveal_rows(srv["db"], srv["orgA"])
    capped = [r for r in rows if json.loads(r["detail"])["result"] == "capped"]
    assert len(capped) == 1
    assert capped[0]["target"] == lead_uid("camp-a", "instagram", "cap-10")


def test_re_revealing_the_same_lead_is_free(srv):
    """Revealed data is never cached client-side, so reopening a drawer RE-reveals.
    Metering calls would burn a Free org's whole allowance on one lead opened eleven
    times — the cap counts DISTINCT leads for exactly this reason."""
    # Arrange
    _seed_leads(srv["db"], srv["orgA"], 2)
    # Act — one lead, opened far more times than the plan's allowance.
    for _ in range(15):
        code, resp, _ = _reveal(srv, "cap-0")
        assert code == 200, resp
    # Assert — allowance untouched: a SECOND distinct lead still reveals.
    assert _reveal(srv, "cap-1")[0] == 200
    # ...and every one of those calls is still its own audit row (16 = 15 + 1).
    assert len(_reveal_rows(srv["db"], srv["orgA"])) == 16


def test_a_lead_revealed_before_the_cap_filled_stays_revealable(srv):
    """The distinct-lead rule has to hold in the other direction too: once an org is
    AT its cap, a lead it already revealed this period must keep opening. Refusing it
    would punish an operator for closing a drawer they were entitled to have open."""
    # Arrange — reveal one lead, then spend the rest of the allowance.
    _seed_leads(srv["db"], srv["orgA"], 12)
    assert _reveal(srv, "cap-0")[0] == 200
    for i in range(1, 10):
        assert _reveal(srv, f"cap-{i}")[0] == 200
    assert _reveal(srv, "cap-10")[0] == 402      # cap is full
    # Act / Assert — the already-revealed lead is still free.
    code, resp, _ = _reveal(srv, "cap-0")
    assert code == 200, resp
    assert resp["data"]["username"] == "user0"


def test_a_bigger_plan_reveals_more(srv):
    """The cap is the PLAN's allowance, not a constant: Lite (50) walks past Free's
    tenth lead without a 402."""
    # Arrange
    _seed_leads(srv["db"], srv["orgA"], 12)
    _set_tier(srv["db"], srv["orgA"], "lite", ts=1000.0)
    # Act — twelve distinct leads, two past what Free would have allowed.
    codes = [_reveal(srv, f"cap-{i}")[0] for i in range(12)]
    # Assert
    assert codes == [200] * 12


def test_the_cap_does_not_leak_which_leads_exist(srv):
    """Order of the gates: ownership and existence are settled BEFORE the cap, so an
    org sitting at its limit still answers 404 for a foreign lead. A 402 there would
    say "that row is real, come back next month" to a tenant who may not know it."""
    # Arrange — fill the allowance.
    _seed_leads(srv["db"], srv["orgA"], 10)
    for i in range(10):
        assert _reveal(srv, f"cap-{i}")[0] == 200
    # Act — org B's real lead, and a lead that never existed.
    foreign = _post(srv["base"], "/api/lead/reveal",
                    _body(comment_id="c-9", campaign_id="camp-b"), srv["owner"])
    missing = _post(srv["base"], "/api/lead/reveal",
                    _body(comment_id="nope", campaign_id="camp-b"), srv["owner"])
    # Assert — both 404, same message, neither 402.
    assert foreign[0] == missing[0] == 404
    assert foreign[1]["error"] == missing[1]["error"] == "unknown lead"


def test_a_denied_or_missing_attempt_never_consumes_allowance(srv):
    """Only a `revealed` row is allowance. A viewer's 403 and a 404 handed out no
    identity, so counting them would let anyone burn an org's month with noise."""
    # Arrange
    _seed_leads(srv["db"], srv["orgA"], 10)
    viewer = _add_user(srv["base"], srv["owner"], "viewer@a.io", "viewer")
    for i in range(20):
        _post(srv["base"], "/api/lead/reveal", _body(comment_id=f"cap-{i % 10}"), viewer)
        _post(srv["base"], "/api/lead/reveal", _body(comment_id=f"ghost-{i}"),
              srv["owner"])
    # Act / Assert — the whole allowance is still there.
    assert [_reveal(srv, f"cap-{i}")[0] for i in range(10)] == [200] * 10


def test_settings_surfaces_the_reveal_meter(srv):
    """`revealsUsed`/`revealCap` sit beside `leadsUsed`/`leadCap` and are read from the
    same audit rows the gate enforces on, so the meter can never disagree with it."""
    # Arrange
    _seed_leads(srv["db"], srv["orgA"], 3)
    for _ in range(2):                      # re-reveals must not move the meter
        assert _reveal(srv, "cap-0")[0] == 200
    assert _reveal(srv, "cap-1")[0] == 200
    # Act
    req = urllib.request.Request(srv["base"] + "/api/settings",
                                 headers={"Cookie": srv["owner"]})
    with urllib.request.urlopen(req) as resp:
        billing = json.loads(resp.read())["BILLING"]
    # Assert — two DISTINCT leads revealed out of Free's ten, despite three calls.
    assert billing["revealsUsed"] == 2
    assert billing["revealCap"] == billing["leadCap"] == 10
