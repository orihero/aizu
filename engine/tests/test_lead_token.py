"""v28 opaque org-facing lead key — `matches.lead_token`.

WHAT WENT WRONG IN v27, because every test here is a direct answer to it.

v27 anonymized the customer's lead: no `username`, no comment `text`, no `reelId`,
with the handle available only through the audited, metered `POST /api/lead/reveal`.
That redaction was COSMETIC on four of six platforms, because the one lead field the
org plane still shipped — `commentId` — is the platform's own comment id, and four
feeds compose that id as a permalink:

    reddit    engines/reddit/feed.py:209    f"{reel_id}/{c.comment_id}"
    youtube   engines/youtube/feed.py:339   f"{reel_id}/{c.comment_id}"
    telegram  engines/telegram/feed.py:160  f"{reel_id}/{reply.id}", and there
                                            `reel_id` is itself "{channel}/{msg}"
    x         engines/x/parsers.py:157      the reply's own tweet rest_id

So the post id v27 removed under the name `reelId` was still shipping as the PREFIX
of `commentId` on every lead row, and the comment the whole policy exists to withhold
was one hand-built URL away — with no reveal call and no audit row. Instagram and
LinkedIn were unaffected, which is exactly why the v27 suites (opaque fixture ids like
"c-1") stayed green while the hole was open. This file uses the REAL composition
shapes for that reason: a fixture that cannot express the bug cannot pin the fix.

v28 replaces the org-facing key with `matches.lead_token`, a random per-lead value
under a UNIQUE index, minted on first insert and resolved back server-side by
`Store.resolve_lead_token` / `server._resolve_org_lead` — which accept the token and
NOTHING else. That strictness is the load-bearing part: an endpoint that still took a
raw comment id would leave every client which already has one writing with it, and
any handler that accepts one is a standing reason to keep shipping one.

The tests below are grouped as the four things that can undo this:
  1. the token leaking the id it replaced (the value sweep, per platform shape);
  2. the raw id still working anywhere (all four org write paths);
  3. a token crossing a boundary — another org, another campaign, another platform;
  4. the redaction being applied at the SOURCE rather than to the org plane, which
     would blind the superadmin plane that is the last reader of the raw lead.
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

from aizu.panel import _build_matches, lead_uid
from aizu.panel_org import build_admin_org_leads
from aizu.server import serve
from aizu.core.store import Store

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
_INDEX_HTML = '<!doctype html><html><body><div id="root"></div></body></html>'
PW = "longenough1"

# The four permalink-composing platforms, spelled the way their feeds actually spell
# them. Each row is (platform, reel_id, comment_id, post_fragment, comment_fragment):
# the last two are the substrings that MUST NOT appear anywhere in an org payload —
# `post_fragment` is what identifies the public page, `comment_fragment` the reply on
# it. Instagram is carried alongside as the control: its ids were never permalinks,
# so a change that only "fixed" the composed platforms would still have to redact it.
#
# Every fragment is at least five characters ON PURPOSE. The sweeps below look for
# them inside a 16-character `secrets.token_urlsafe(12)` value, and a two-character
# needle lands inside one about once every 250 runs (measured) — which would turn the
# headline invariant of this file into a coin flip.
_PERMALINK_SHAPES = [
    # reddit: reel_id is the submission fullname, comment the t1_ fullname under it.
    pytest.param("reddit", "t3_post", "t3_post/t1_xyz", "t3_post", "t1_xyz",
                 id="reddit"),
    # youtube: reel_id is the video id, comment the UgC-prefixed comment id.
    pytest.param("youtube", "vid123", "vid123/UgxABC", "vid123", "UgxABC",
                 id="youtube"),
    # telegram: reel_id is ALREADY "{channel}/{message}", so the comment id is a
    # three-part path and the post pointer is two thirds of it.
    pytest.param("telegram", "channel/55", "channel/55/610932", "channel/55",
                 "610932", id="telegram"),
    # x: the reply's own tweet rest_id — a whole permalink on its own, no separator
    # to notice, which is why a "does it contain a slash" heuristic would miss it.
    pytest.param("x", "1700000000000000001", "1900000000000000009",
                 "1700000000000000001", "1900000000000000009", id="x"),
    # instagram: the control. Never a permalink, still redacted.
    pytest.param("instagram", "reel-a", "c-ig-1", "reel-a", "c-ig-1", id="instagram"),
]


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def _post(base, path, body, cookie=None):
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(base + path, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or "null")


def _get(base, path, cookie):
    req = urllib.request.Request(base + path, headers={"Cookie": cookie})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or "null")


def _signup(base, email, company):
    code, resp = _post(base, "/api/auth/signup",
                       {"email": email, "password": PW, "companyName": company})
    assert code == 200, resp
    req = urllib.request.Request(
        base + "/api/auth/login",
        data=json.dumps({"email": email, "password": PW}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as r:
        cookie = r.headers["Set-Cookie"].split(";", 1)[0]
    return resp["data"]["user"], cookie


def _seed(db, campaign_id, org_id, *, platform, reel_id, comment_id,
          username="alibek_uz", text="do you ship to Tashkent?"):
    store = Store(db)
    try:
        store.upsert_campaign_meta(campaign_id, org_id=org_id,
                                   display_name=campaign_id)
        # A brief too, so `?campaign=` on /api/state resolves — the scoped read is
        # what the panel actually issues, and its MATCHES is a second org surface.
        store.upsert_campaign_brief(
            campaign_id, {"platform": platform, "threshold": 0.7}, org_id=org_id)
        store.upsert_match(
            campaign_id=campaign_id, reel_id=reel_id, comment_id=comment_id,
            username=username, text=text, lang="en", score=0.9,
            reason="asked for the product", extracted={"size": "42"}, tier="local",
            platform=platform, intent="Wants sneakers, size 42")
    finally:
        store.close()


def _token(db, campaign_id, platform, comment_id):
    store = Store(db)
    try:
        return store.lead_token_for(campaign_id, platform, comment_id)
    finally:
        store.close()


@pytest.fixture
def srv():
    """Org A holds one lead per platform shape in `camp-a`; org B holds one lead in
    `camp-b`, so "someone else's lead" is a real row rather than a missing one — a
    404 that hides nothing proves nothing."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    panel_dir = tempfile.mkdtemp(suffix="-spa")
    (Path(panel_dir) / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
    httpd = serve(db_path, panel_dir, str(CONFIG), port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    owner_a, cookie_a = _signup(base, "owner@a.io", "Acme")
    owner_b, cookie_b = _signup(base, "owner@b.io", "Beta")
    for param in _PERMALINK_SHAPES:
        platform, reel_id, comment_id, _post_frag, _comment_frag = param.values
        _seed(db_path, "camp-a", owner_a["orgId"], platform=platform,
              reel_id=reel_id, comment_id=comment_id)
    _seed(db_path, "camp-b", owner_b["orgId"], platform="reddit",
          reel_id="t3_theirs", comment_id="t3_theirs/t1_theirs",
          username="carol_b", text="foreign org lead")

    yield {"base": base, "db": db_path, "owner": cookie_a, "ownerB": cookie_b,
           "orgA": owner_a["orgId"], "orgB": owner_b["orgId"]}

    httpd.shutdown()
    os.unlink(db_path)
    shutil.rmtree(panel_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 1. The key does not carry what it replaced
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("platform,reel_id,comment_id,post_frag,comment_frag",
                         _PERMALINK_SHAPES)
def test_no_org_lead_row_carries_its_post_or_comment_id(
        srv, platform, reel_id, comment_id, post_frag, comment_frag):
    """THE test. Over the real feed compositions, by VALUE, on both org list surfaces.

    Swept over the serialized row rather than checked key by key, and by value rather
    than by key name, because both of the ways this has actually broken are invisible
    to a key check: v27 shipped the post id under a DIFFERENT key (`commentId`,
    where it was a prefix), and a future field could ship it under a third name.
    Whatever the key is called, the fragments below must not be in the bytes.

    Scoped to LEAD ROWS, not to the whole `/api/state` payload: `REELS` legitimately
    lists every SCANNED post's id (contract A7) and is deliberately not joined to any
    lead, so a payload-wide sweep would fail on a surface that is working as designed.
    See `test_lead_reveal.py::test_the_watchlist_does_not_mark_which_post_produced_
    the_lead` for the property that guards that one.
    """
    # Act — the two org-facing lead surfaces.
    code, leads = _get(srv["base"], "/api/leads?pageSize=200", srv["owner"])
    assert code == 200, leads
    code, state = _get(srv["base"], "/api/state?campaign=camp-a", srv["owner"])
    assert code == 200, state

    token = _token(srv["db"], "camp-a", platform, comment_id)
    assert token, "the lead must have a token at all"
    rows = [r for r in leads["data"]["items"] + state["MATCHES"]
            if r["commentId"] == token]
    assert len(rows) == 2, \
        f"{platform} lead must appear on BOTH org surfaces, found {len(rows)}"

    # Assert — neither the post nor the comment survives, anywhere in the row.
    for row in rows:
        blob = json.dumps(row)
        assert post_frag not in blob, \
            f"{platform} lead row still names its post ({post_frag!r}): {row}"
        assert comment_frag not in blob, \
            f"{platform} lead row still names its comment ({comment_frag!r}): {row}"
        assert comment_id not in blob
        # ...and the composite `id` the panel routes on is composed over the token,
        # so a bookmarked lead URL is not a permalink either.
        assert row["id"] == lead_uid("camp-a", platform, token)
        assert post_frag not in row["id"] and comment_frag not in row["id"]


def test_the_token_is_not_derived_from_anything_on_the_row(srv):
    """A key derived from the lead is a key that leaks the lead.

    The failure mode this guards is the tempting "cheap" fix: hash the comment id, or
    base64 it, or slice it. Every one of those is reversible or, worse, STABLE across
    tenants — the same comment id in two orgs would produce the same key, which is a
    cross-tenant join. `store.new_lead_token` is `secrets.token_urlsafe(12)`, so the
    only thing two leads with identical content share is nothing.
    """
    # Arrange — the same comment id, same post, seeded under two different orgs.
    _seed(srv["db"], "camp-a", srv["orgA"], platform="reddit",
          reel_id="t3_same", comment_id="t3_same/t1_same")
    _seed(srv["db"], "camp-b", srv["orgB"], platform="reddit",
          reel_id="t3_same", comment_id="t3_same/t1_same")
    # Act
    a = _token(srv["db"], "camp-a", "reddit", "t3_same/t1_same")
    b = _token(srv["db"], "camp-b", "reddit", "t3_same/t1_same")
    # Assert — identical leads, different keys; neither key names the lead.
    assert a and b and a != b
    for tok in (a, b):
        assert "t3_same" not in tok and "t1_same" not in tok


def test_a_repoll_does_not_rotate_the_token(srv):
    """An open drawer must not 404 because the engine re-polled the post.

    `upsert_match`'s ON CONFLICT clause deliberately omits `lead_token`, so a re-poll
    (or a worker syncing the same lead back) refreshes score/reason/intent and leaves
    the panel's key alone. If it re-minted, every key the customer is currently
    holding — an open drawer, a bookmarked /leads/<uid>, a selection about to be bulk
    -archived — would go dead the moment the campaign ran again, and the write would
    answer 404 with no way for the panel to tell that from a deleted lead.
    """
    # Arrange
    before = _token(srv["db"], "camp-a", "reddit", "t3_post/t1_xyz")
    # Act — the same lead captured again, with everything else changed.
    store = Store(srv["db"])
    try:
        store.upsert_match(
            campaign_id="camp-a", reel_id="t3_post", comment_id="t3_post/t1_xyz",
            username="alibek_uz", text="still interested?", lang="en", score=0.4,
            reason="second verdict", extracted={"size": "43"}, tier="cloud",
            platform="reddit", intent="Wants sneakers, size 43")
    finally:
        store.close()
    # Assert — the row moved, the key did not.
    after = _token(srv["db"], "camp-a", "reddit", "t3_post/t1_xyz")
    assert after == before
    code, leads = _get(srv["base"], "/api/leads?pageSize=200", srv["owner"])
    row = next(r for r in leads["data"]["items"] if r["commentId"] == before)
    assert row["intent"] == "Wants sneakers, size 43", "the re-poll must have landed"
    # ...and the key the customer was already holding still writes.
    code, _ = _post(srv["base"], "/api/status",
                    {"campaignId": "camp-a", "platform": "reddit",
                     "commentId": before, "status": "interested"}, srv["owner"])
    assert code == 200


# --------------------------------------------------------------------------- #
# 2. The raw comment id is dead on every org write path
# --------------------------------------------------------------------------- #
def _write_calls(campaign_id, platform, key):
    """The four org-scoped lead writes, each named by `key`.

    Kept as one list so a fifth write path added later has an obvious place to join,
    and so no single endpoint can be quietly exempted: the whole point is that the
    key rule holds on ALL of them, since one accepting endpoint restores the id to
    the wire for every client that has it.
    """
    return [
        ("/api/status",
         {"campaignId": campaign_id, "platform": platform, "commentId": key,
          "status": "interested"}),
        ("/api/status/bulk",
         {"campaignId": campaign_id, "status": "in_progress",
          "items": [{"commentId": key, "platform": platform}]}),
        ("/api/lead/note",
         {"op": "create", "campaignId": campaign_id, "platform": platform,
          "commentId": key, "body": "called them"}),
        ("/api/lead/reveal",
         {"campaignId": campaign_id, "platform": platform, "commentId": key}),
    ]


@pytest.mark.parametrize("platform,reel_id,comment_id,post_frag,comment_frag",
                         _PERMALINK_SHAPES)
def test_a_write_keyed_by_the_raw_comment_id_is_refused(
        srv, platform, reel_id, comment_id, post_frag, comment_frag):
    """Knowing the real comment id must buy nothing on any write path.

    A caller can hold one from a pre-v28 bookmark, a CSV exported last month, or by
    reading the permalink off reddit. If any handler still resolved it, v28 would be
    decorative — and the tempting "be lenient, accept both" fix is exactly what this
    refuses. Everything else about these requests is legitimate: the caller's own
    org, own campaign, own lead, a role that may write. Only the key is wrong.

    Bulk is the exception in SHAPE, not in rule: it answers 200 with the unresolved
    item in `missing`, because a partial batch is not an error — but the item is not
    written, and `missing` echoes the key the CALLER sent rather than any real id.
    """
    for path, body in _write_calls("camp-a", platform, comment_id):
        code, resp = _post(srv["base"], path, body, srv["owner"])
        if path == "/api/status/bulk":
            assert code == 200, (path, resp)
            assert resp["data"]["updated"] == 0, f"{path} wrote using the raw id"
            assert resp["data"]["missing"] == [comment_id], (path, resp)
        else:
            assert code == 404, f"{path} accepted the raw comment id: {resp}"
            assert resp["ok"] is False
    # ...and the lead is untouched: nothing above landed by another door.
    store = Store(srv["db"])
    try:
        row = next(m for m in store.matches("camp-a")
                   if m["comment_id"] == comment_id)
        assert row["status"] == "new"
        assert store.notes_by_lead("camp-a").get(comment_id, []) == []
    finally:
        store.close()
    # ...while the TOKEN for that same lead writes fine, so this is the key being
    # refused rather than the lead being unreachable.
    token = _token(srv["db"], "camp-a", platform, comment_id)
    code, _ = _post(srv["base"], "/api/status",
                    {"campaignId": "camp-a", "platform": platform,
                     "commentId": token, "status": "interested"}, srv["owner"])
    assert code == 200


def test_no_write_response_echoes_the_real_comment_id(srv):
    """The other half of the write path: what comes BACK.

    Refusing a raw id on the way in is worthless if a successful write hands one out
    on the way back, and that is not hypothetical — `Store.add_note` returns a dict
    built around the RESOLVED comment id (correctly: the store is the plane below
    redaction), and shipping it verbatim would have answered a note-create with the
    post id AND the comment id, i.e. the exact permalink v28 exists to withhold, plus
    the `reelId` that `_build_matches` deliberately drops. One throwaway note per
    lead and the whole list is de-anonymized, with no audit row and no reveal meter.

    So every org write response is swept whole, the same way the list rows are.
    """
    platform, reel_id, comment_id = "reddit", "t3_post", "t3_post/t1_xyz"
    token = _token(srv["db"], "camp-a", platform, comment_id)
    for path, body in _write_calls("camp-a", platform, token):
        code, resp = _post(srv["base"], path, body, srv["owner"])
        assert code == 200, (path, resp)
        blob = json.dumps(resp)
        assert "t3_post" not in blob, f"{path} answered with the post id: {resp}"
        assert "t1_xyz" not in blob, f"{path} answered with the comment id: {resp}"
        assert comment_id not in blob
        # Whatever key the response uses for the lead, it must be the one the CALLER
        # sent — echoing the resolved id back is how the id returns to the wire.
        if "commentId" in json.dumps(resp.get("data") or {}):
            assert token in blob, f"{path} did not echo the caller's key: {resp}"


# --------------------------------------------------------------------------- #
# 3. A token does not cross a boundary
# --------------------------------------------------------------------------- #
def test_a_token_from_another_org_is_refused_indistinguishably(srv):
    """Org B's genuine token, presented by org A, on every write path.

    `Store.resolve_lead_token` looks the token up table-wide and THEN checks the
    row's own `org_id`, which is why the campaign is not passed to it: pairing a
    caller-supplied campaign with someone else's token is precisely the probe to
    refuse. And the refusal must be a 404 with the same message an unknown key gets —
    a 403, or a distinct message, would confirm the row exists and rebuild the
    cross-tenant existence oracle the reveal endpoint is careful not to be.
    """
    theirs = _token(srv["db"], "camp-b", "reddit", "t3_theirs/t1_theirs")
    assert theirs, "org B's lead must actually have a token"
    invented = "not-a-real-token-at-all"
    for path, _ in _write_calls("camp-b", "reddit", theirs):
        foreign = _post(srv["base"], path,
                        dict(_write_calls("camp-b", "reddit", theirs))[path],
                        srv["owner"])
        missing = _post(srv["base"], path,
                        dict(_write_calls("camp-b", "reddit", invented))[path],
                        srv["owner"])
        # Same status AND same body shape for "not yours" and "never existed".
        assert foreign[0] == missing[0], (path, foreign, missing)
        assert json.dumps(foreign[1]) == json.dumps(missing[1]).replace(
            invented, theirs), (path, foreign, missing)
        assert "carol_b" not in json.dumps(foreign[1])
    # ...and org B's lead is genuinely untouched by all of that.
    store = Store(srv["db"])
    try:
        row = next(m for m in store.matches("camp-b"))
        assert row["status"] == "new"
    finally:
        store.close()


def test_a_token_presented_under_the_wrong_campaign_or_platform_is_refused(srv):
    """The row's OWN campaign and platform decide, not the request's.

    A token is unique table-wide, so it resolves without a campaign — which would
    leave the campaign and platform in the request purely decorative, and one of an
    org's own leads writable under any campaign id the caller felt like naming. The
    third check in `server._resolve_org_lead` closes that: resolve, then require the
    row's own campaign and platform to agree with what the request claimed.

    Note this is NOT a cross-tenant case — every lead here belongs to the caller —
    which is why `resolve_lead_token`'s org check cannot catch it on its own.
    """
    # Arrange — a second campaign in the SAME org.
    _seed(srv["db"], "camp-a2", srv["orgA"], platform="youtube",
          reel_id="vid999", comment_id="vid999/UgxZZZ")
    token = _token(srv["db"], "camp-a2", "youtube", "vid999/UgxZZZ")
    for campaign_id, platform, why in (
            ("camp-a", "youtube", "wrong campaign"),
            ("camp-a2", "reddit", "wrong platform"),
            ("camp-a", "reddit", "both wrong")):
        for path, body in _write_calls(campaign_id, platform, token):
            code, resp = _post(srv["base"], path, body, srv["owner"])
            if path == "/api/status/bulk":
                assert code == 200 and resp["data"]["updated"] == 0, (why, path, resp)
                assert resp["data"]["missing"] == [token], (why, path, resp)
            else:
                assert code == 404, (why, path, resp)
    # ...and the truthful pairing still works, so the token itself is fine.
    code, _ = _post(srv["base"], "/api/status",
                    {"campaignId": "camp-a2", "platform": "youtube",
                     "commentId": token, "status": "interested"}, srv["owner"])
    assert code == 200


def test_resolve_lead_token_answers_none_for_both_kinds_of_miss(srv):
    """The store-level statement of the same rule, so the refusal cannot be a
    property of one handler that a second handler forgets to reproduce."""
    store = Store(srv["db"])
    try:
        mine = store.lead_token_for("camp-a", "reddit", "t3_post/t1_xyz")
        theirs = store.lead_token_for("camp-b", "reddit", "t3_theirs/t1_theirs")
        # Mine resolves, to my own row's real composite key.
        assert store.resolve_lead_token(srv["orgA"], mine) == {
            "campaignId": "camp-a", "platform": "reddit",
            "commentId": "t3_post/t1_xyz"}
        # Theirs does not — and neither does a string nobody minted. Both None, so
        # no caller can tell "exists but not yours" from "does not exist".
        assert store.resolve_lead_token(srv["orgA"], theirs) is None
        assert store.resolve_lead_token(srv["orgA"], "no-such-token") is None
        # Empty/whitespace is a miss too, never a wildcard that matches the NULL
        # rows a pre-v28 worker binary can still write.
        assert store.resolve_lead_token(srv["orgA"], "") is None
        assert store.resolve_lead_token(srv["orgA"], "   ") is None
        # A raw comment id is not a token, at the store level either.
        assert store.resolve_lead_token(srv["orgA"], "t3_post/t1_xyz") is None
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 4. The pairing: the superadmin plane keeps the real id
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("platform,reel_id,comment_id,post_frag,comment_frag",
                         _PERMALINK_SHAPES)
def test_the_superadmin_plane_still_names_the_lead_by_its_real_comment_id(
        srv, platform, reel_id, comment_id, post_frag, comment_frag):
    """The half a one-sided test misses, and the reason it is worth its own case.

    `panel._build_matches` is shared by both planes, `include_identity` is the entire
    difference, and the plausible wrong fix is to scrub at the source — swap the
    comment id for a token before the flag is read. Every org assertion in this file
    would still pass, while `GET /api/admin/orgs/{id}/leads` lost the only remaining
    handle on the raw lead: a platform admin investigating an abuse report could no
    longer find the comment they were sent, on any platform, ever.

    So the same lead is asserted from both sides: token on the org plane, the
    platform's own id on the superadmin one, and the two are different values.
    """
    store = Store(srv["db"])
    try:
        org_row = next(r for r in _build_matches(store, "camp-a")
                       if r["campaignId"] == "camp-a" and r["platform"] == platform)
        admin_row = next(
            r for r in _build_matches(store, "camp-a", include_identity=True)
            if r["platform"] == platform)
        page = build_admin_org_leads(store, org_id=srv["orgA"], page_size=200)
        admin_api = next(r for r in page["leads"] if r["platform"] == platform)
    finally:
        store.close()

    # The superadmin keeps everything the org lost — the key included.
    assert admin_row["commentId"] == comment_id
    assert admin_row["reelId"] == reel_id
    assert admin_row["username"] == "alibek_uz"
    assert admin_api["commentId"] == comment_id
    # ...and the org row names the same lead by a value that is none of the above.
    assert org_row["commentId"] != comment_id
    assert post_frag not in org_row["commentId"]
    assert comment_frag not in org_row["commentId"]
    # The two keys must actually resolve to ONE lead, or the planes are describing
    # different rows and the pairing above is a coincidence.
    store = Store(srv["db"])
    try:
        assert store.resolve_lead_token(srv["orgA"], org_row["commentId"]) == {
            "campaignId": "camp-a", "platform": platform, "commentId": comment_id}
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 5. The row that has no token yet — the fail-OPEN trap
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("platform,reel_id,comment_id,post_frag,comment_frag",
                         _PERMALINK_SHAPES)
def test_a_row_with_no_token_is_healed_never_fallen_back_on(
        srv, platform, reel_id, comment_id, post_frag, comment_frag):
    """A lead whose `lead_token` is NULL must NOT be projected under its real id.

    This is not a hypothetical row. The v28 backfill covers everything present when a
    database is OPENED, but a worker still running a PRE-v28 binary inserts without
    the column and SQLite writes NULL. The bridge holds a long-lived `Store`, so such
    a row can be read by a projection before any reopen re-runs the backfill — that
    window is this test's subject, and it is the only state in which the projection's
    untokened branch is reachable at all.

    The trap is that every fallback which can be DERIVED from such a row is the
    comment id, or contains it. `m["lead_token"] or lead_uid(campaign, platform,
    comment_id)` reads like ordinary defensive coding and reinstates the whole v27
    hole for exactly the rows a staged fleet rollout produces — silently, on the
    platforms where the id is a permalink. `_build_matches` therefore calls
    `Store.ensure_lead_token`, which MINTS and persists rather than deriving.

    Driven through `_build_matches` on ONE live `Store` rather than over HTTP, and
    that is the whole design of the test: every `Store(db_path)` open runs the
    backfill, so an HTTP request opens a fresh connection, heals the row on the way
    in, and can never observe the untokened branch. A version of this test that went
    through the API passed against the fail-open mutation — it proved the backfill
    works, not the fallback, which is a different claim.

    Written with an explicit column list omitting `lead_token`, because
    `upsert_match` always mints one: the only way to make this row is the way the old
    binary makes it.
    """
    store = Store(srv["db"])
    try:
        orphan = comment_id + "-old"
        with store._tx() as c:
            c.execute(
                """INSERT INTO matches
                     (campaign_id, org_id, platform, reel_id, comment_id,
                      username, text, lang, score, reason, status, tier,
                      intent, captured_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("camp-a", srv["orgA"], platform, reel_id, orphan,
                 "olduser", "written by a pre-v28 worker", "en", 0.9, "r", "new",
                 "local", "Wants sneakers", 1.0, 1.0))
        # Precondition: the row really is untokened on THIS connection, or the branch
        # under test is not the branch being exercised.
        assert store._conn.execute(
            "SELECT lead_token FROM matches WHERE comment_id=?",
            (orphan,)).fetchone()["lead_token"] is None, \
            "fixture failed to create an untokened row"

        # Act — the org projection, on the same live Store that holds the NULL row.
        rows = _build_matches(store, "camp-a")

        # Assert — the untokened row's real ids are nowhere in the projection.
        blob = json.dumps(rows)
        assert comment_frag not in blob, \
            "an untokened row fell back to its comment id"
        assert post_frag not in blob, "an untokened row fell back to its post id"

        # ...and it was HEALED, not merely hidden: the lead stays writable rather
        # than becoming a row the customer can see and can never action.
        healed = store._conn.execute(
            "SELECT lead_token FROM matches WHERE comment_id=?",
            (orphan,)).fetchone()["lead_token"]
        assert healed, "the untokened row was never healed"
        assert store.resolve_lead_token(srv["orgA"], healed) == {
            "campaignId": "camp-a", "platform": platform, "commentId": orphan}
    finally:
        store.close()
