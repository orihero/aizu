import json
import os
import tempfile
from pathlib import Path

from aizu.core.config import load_campaign, load_soul
from aizu.core.feed import Comment, FakeFeed, Reel
from aizu.core.mock_router import MockRouter
from aizu.core.pacing import PacingConfig, Pacer
from aizu.panel import build_raw
from aizu.engines.instagram.session import Session, SessionConfig
from aizu.core.store import Store

CONFIG = Path(__file__).resolve().parents[1] / "config"


def _seeded_store():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    store = Store(path)
    feed = FakeFeed([
        Reel("r1", author="acme.io", caption="Acme app — sprint planning, free trial",
             ocr_text="Pro from $12/seat", comments=[
                 Comment("c1", "dana", "How much is the Pro plan? +1 415 555 0142", "en"),
                 Comment("c2", "bot", "🔥", "en"),
             ]),
        Reel("r2", author="cats", caption="funny cats", comments=[Comment("c3", "x", "lol", "en")]),
    ])
    Session(store=store, router=MockRouter(store=store), feed=feed,
            soul=load_soul(CONFIG / "soul.md"),
            campaign=load_campaign(CONFIG / "campaign.md"),
            pacer=Pacer(cfg=PacingConfig(enforce_daytime=False), sleep=lambda _t: None),
            cfg=SessionConfig()).run()
    return store


def _raw():
    store = _seeded_store()
    return build_raw(store, load_soul(CONFIG / "soul.md"),
                     load_campaign(CONFIG / "campaign.md"))


def test_raw_has_all_keys():
    raw = _raw()
    for k in ["CONFIG", "CAMPAIGNS", "SESSIONS", "REELS", "MATCHES",
              "ESCALATION_LOG", "ALERTS", "HEALTH", "SOUL"]:
        assert k in raw


def test_raw_is_json_serializable():
    json.dumps(_raw(), ensure_ascii=False)  # must not raise


def test_sessions_and_matches_reconcile():
    raw = _raw()
    assert len(raw["SESSIONS"]) == 1
    s = raw["SESSIONS"][0]
    # runId is surfaced for deep-linking into the activity feed; None here since
    # this session ran without RunManager correlation.
    assert "runId" in s and s["runId"] is None
    assert s["reelsSeen"] == 2
    assert s["matches"] == len(raw["MATCHES"]) == 1   # only c1 is a lead
    m = raw["MATCHES"][0]
    # No `reelId` on an org-facing lead (v27): the post it names is public and shows
    # both the handle and the comment, so it is superadmin-only — the audited reveal
    # does not hand it over either, because a pointer to the comment is the comment.
    assert "reelId" not in m
    assert m["sessionId"] == s["id"]
    assert "555" in (m["extracted"].get("phone") or "")


def test_reels_only_relevant_with_content():
    raw = _raw()
    ids = {r["id"] for r in raw["REELS"]}
    assert "r1" in ids and "r2" not in ids       # r2 (cats) is irrelevant
    r1 = next(r for r in raw["REELS"] if r["id"] == "r1")
    assert r1["author"] == "acme.io" and "Acme" in r1["caption"]


def test_platform_surfaced_on_matches_sessions_and_summary():
    raw = _raw()
    # every match + session carries its platform (defaulting to instagram)
    assert all(m["platform"] == "instagram" for m in raw["MATCHES"])
    assert all(s["platform"] == "instagram" for s in raw["SESSIONS"])
    # the per-platform rollup is present and reconciles with the match count
    assert "PLATFORMS" in raw
    ig = next(p for p in raw["PLATFORMS"] if p["platform"] == "instagram")
    assert ig["matches"] == len(raw["MATCHES"])
    assert ig["sessions"] == len(raw["SESSIONS"])


def test_config_and_soul_from_brief():
    raw = _raw()
    assert raw["CONFIG"]["matchThreshold"] == 0.70
    assert raw["SOUL"]["rules"], "soul rules should be parsed"
    assert raw["CAMPAIGNS"][0]["goalType"] == "lead"
    assert "phone" in raw["CAMPAIGNS"][0]["extractFields"]


# --- Multi-platform card + brief form (Phase 4) ------------------------------

from datetime import datetime

from aizu.panel import (_all_campaign_platforms, _brief_form_from_campaign,
                             _brief_form_from_stored, _draft_campaign)
from aizu.core.config import campaign_from_brief


def _bare_store():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    return Store(path)


def test_all_campaign_platforms_from_channels():
    assert _all_campaign_platforms(
        {"channels": [{"platform": "instagram"}, {"platform": "youtube"}]}
    ) == ["instagram", "youtube"]
    # No channels → fall back to the single flat platform, else the default.
    assert _all_campaign_platforms({"platform": "x"}) == ["x"]
    assert _all_campaign_platforms({}) == ["instagram"]


def test_brief_form_from_stored_emits_channels():
    form = _brief_form_from_stored({"platform": "instagram", "channels": [
        {"platform": "youtube", "seed_channels": ["UC1"], "include_home_feed": False}]})
    assert form["channels"] == [{"platform": "youtube", "seedHashtags": [],
                                 "seedAccounts": [], "seedChannels": ["UC1"],
                                 "includeHomeFeed": False}]


def test_brief_form_from_stored_empty_channels_emits_empty_list():
    assert _brief_form_from_stored({"platform": "instagram"})["channels"] == []


def test_brief_form_from_campaign_emits_channels():
    c = campaign_from_brief("c", {"channels": [
        {"platform": "instagram"}, {"platform": "youtube"}]})
    form = _brief_form_from_campaign(c)
    assert [ch["platform"] for ch in form["channels"]] == ["instagram", "youtube"]


def test_draft_campaign_card_has_platforms_list():
    store = _bare_store()
    try:
        store.upsert_campaign_meta("multi", status="draft")
        store.upsert_campaign_brief("multi", {"channels": [
            {"platform": "instagram"}, {"platform": "youtube"}]})
        m = next(x for x in store.list_campaign_meta() if x["campaign_id"] == "multi")
        card = _draft_campaign(store, m, {}, datetime.now(), 20.0)
    finally:
        store.close()
    assert card["platforms"] == ["instagram", "youtube"]


def test_single_platform_card_platforms_list_fallback():
    store = _bare_store()
    try:
        store.upsert_campaign_meta("solo", status="draft")
        store.upsert_campaign_brief("solo", {"platform": "youtube",
                                             "seed_channels": ["UC1"]})
        m = next(x for x in store.list_campaign_meta() if x["campaign_id"] == "solo")
        card = _draft_campaign(store, m, {}, datetime.now(), 20.0)
    finally:
        store.close()
    assert card["platforms"] == ["youtube"]      # falls back to [platform]


# --- Lead identity: the composite (campaign, platform, comment) key -----------

from aizu.panel import _build_matches, lead_uid


def test_lead_uid_is_injective_across_the_composite_key():
    """Distinct (campaign, platform, comment) triples must never collide — including
    when a part itself contains the delimiter or an escape character."""
    assert lead_uid("c", "instagram", "x1") != lead_uid("c", "x", "x1")
    assert lead_uid("a", "instagram", "x1") != lead_uid("b", "instagram", "x1")
    # "a|b" + "c" must not collide with "a" + "b|c".
    assert lead_uid("a|b", "instagram", "c") != lead_uid("a", "instagram", "b|c")
    # A literal "%7C" in a part must not read back as an escaped delimiter.
    assert lead_uid("a%7Cb", "instagram", "c") != lead_uid("a|b", "instagram", "c")


def test_lead_payload_keeps_two_campaigns_leads_distinct():
    """The same commenter under two campaigns (and the same comment id on two
    platforms) stays THREE distinct panel rows, each carrying its own composite key.

    The payload used to emit `"id": comment_id`, collapsing all three into one row —
    so clicking a lead in campaign A opened, and wrote status to, campaign B's lead.

    Since v28 the third part of that composite is `matches.lead_token`, minted per
    ROW under a UNIQUE index, so the three rows no longer share the value the
    composite was invented to disambiguate. The composite still earns its keep — it
    is what a client without the DB can key on, and the superadmin plane still
    composes it over real comment ids that DO collide — but on this plane the
    collision is now impossible rather than merely handled, and the assertions below
    say so by looking the rows up through their own emitted keys.
    """
    store = _bare_store()
    try:
        for cid, platform, user, wants in (("camp-a", "instagram", "alice", "roofing"),
                                           ("camp-b", "instagram", "bob", "gutters"),
                                           ("camp-a", "x", "carol", "cladding")):
            store.upsert_match(campaign_id=cid, reel_id="r", comment_id="dup-1",
                               username=user, text="t", lang="en", score=0.9,
                               reason="r", extracted=None, tier="local",
                               platform=platform,
                               # v27: the intent is now the only per-row prose an org
                               # sees, so it is what a collapsed row would visibly
                               # duplicate — seed a distinct one per record. The seed
                               # is the WANT, not the handle: `_build_matches` scrubs
                               # the row's own username out of its org-facing prose,
                               # so a handle-derived intent would be redacted back to
                               # a shared prefix and this test would stop testing
                               # distinctness (see test_lead_redaction_audit.py).
                               intent=f"Wants a quote for {wants}")
        rows = _build_matches(store, "camp-a") + _build_matches(store, "camp-b")
    finally:
        store.close()

    assert len(rows) == 3
    assert len({r["id"] for r in rows}) == 3, "each lead needs its own panel id"
    by_id = {r["id"]: r for r in rows}
    # Every id resolves back to exactly the record it came from — the write path
    # reads campaignId/platform/commentId off the resolved row.
    for r in rows:
        assert by_id[lead_uid(r["campaignId"], r["platform"], r["commentId"])] is r
    # v27: identity is superadmin-only, so the distinctness has to be visible on a
    # field the org actually receives — the derived intent.
    assert all("username" not in r and "text" not in r for r in rows)
    by_record = {(r["campaignId"], r["platform"]): r for r in rows}
    assert by_record[("camp-b", "instagram")]["intent"] == "Wants a quote for gutters"
    assert by_record[("camp-a", "x")]["intent"] == "Wants a quote for cladding"
    # v28: the org-facing key is the per-row token, so the three rows that DO share
    # a comment id ship three different values for it — and none of them is "dup-1".
    assert len({r["commentId"] for r in rows}) == 3
    assert all(r["commentId"] != "dup-1" and "dup-1" not in r["commentId"]
               for r in rows)
    # ...while the superadmin projection of the same three rows still carries the
    # real comment id they share. The redaction is scoped to the org plane, not
    # applied at the source — losing this half would blind the only plane that can
    # still read the raw lead.
    store = _bare_store()
    try:
        for cid, platform in (("camp-a", "instagram"), ("camp-b", "instagram"),
                              ("camp-a", "x")):
            store.upsert_match(campaign_id=cid, reel_id="r", comment_id="dup-1",
                               username="u", text="t", lang="en", score=0.9,
                               reason="r", extracted=None, tier="local",
                               platform=platform, intent="Wants a quote")
        admin = (_build_matches(store, "camp-a", include_identity=True)
                 + _build_matches(store, "camp-b", include_identity=True))
    finally:
        store.close()
    assert {r["commentId"] for r in admin} == {"dup-1"}


# ----- v27 lead redaction: intent in, identity out ------------------------------

def test_org_lead_payload_carries_intent_and_no_identity():
    """The central promise of v27: an org-facing lead row shows what the person
    WANTS and nothing that says who they are. The username and the comment body stay
    in the DB — they simply never reach this payload."""
    CID = "cm-9f3a1c"
    store = _bare_store()
    try:
        store.upsert_match(campaign_id="camp-a", reel_id="r", comment_id=CID,
                           username="alice", text="how much for the red ones?",
                           lang="en", score=0.9, reason="asked price",
                           extracted=None, tier="local", platform="instagram",
                           intent="Wants a price for the red sneakers")
        row = _build_matches(store, "camp-a")[0]
    finally:
        store.close()
    assert "username" not in row and "text" not in row
    assert row["intent"] == "Wants a price for the red sneakers"
    # Neither identity may survive anywhere else in the row either (a nested copy in
    # `extracted` or a reason string would be the same leak by another door).
    body = json.dumps(row)
    assert "alice" not in body and "red ones" not in body
    # The rest of the row is the product and must be untouched.
    assert row["reason"] == "asked price" and row["score"] == 0.9
    # ...except the KEY, which v28 replaced. `commentId` on an org row is the opaque
    # `matches.lead_token`, not the platform's own id — on reddit/youtube/telegram/x
    # that id is composed as "{reel_id}/{comment_id}" (or the reply's own tweet id),
    # so shipping it left the redaction above one hand-built URL from being undone.
    # The assertion is inverted rather than dropped: "is not the comment id, and does
    # not contain it" is exactly the property the change buys. The seeded id is long
    # enough for the substring half to mean something: a token is 16 characters of
    # urlsafe base64, and a two-character needle turns up inside one about once every
    # 250 runs, which is a coin flip rather than an assertion.
    assert row["commentId"] != CID and CID not in row["commentId"]
    # `reelId` is a POINTER to the identity and goes with it: the post it names is
    # public and carries the handle and the comment in plain sight, so an org-facing
    # reelId would have let anyone with devtools rebuild the list without ever
    # calling the reveal. It is blocked outright for an org rather than sold behind
    # an audit row — the reveal hands over the HANDLE and nothing else — because a
    # pointer to the comment is the comment, and the comment is superadmin-only.
    assert "reelId" not in row and "reel" not in body


def test_include_identity_is_opt_in_and_off_by_default():
    """`_build_matches` is shared by the org and superadmin paths, so the flag is the
    whole gate. It defaults to DENY: a future org-facing caller that forgets it leaks
    nothing, where a default-allow would leak on every caller someone forgets."""
    store = _bare_store()
    try:
        store.upsert_match(campaign_id="camp-a", reel_id="r", comment_id="c1",
                           username="alice", text="how much?", lang="en", score=0.9,
                           reason="asked price", extracted=None, tier="local",
                           platform="instagram", intent="Wants a price")
        org = _build_matches(store, "camp-a")[0]
        admin = _build_matches(store, "camp-a", include_identity=True)[0]
    finally:
        store.close()
    assert "username" not in org and "text" not in org and "reelId" not in org
    assert admin["username"] == "alice" and admin["text"] == "how much?"
    # …and the superadmin keeps the post pointer, which is now the ONLY surface
    # carrying it: for an org the post is blocked outright, not audited, because the
    # reveal returns the handle alone. Losing it here would leave nobody able to open
    # the page the raw comment is read from.
    assert admin["reelId"] == "r"
    # The superadmin sees BOTH — the derived line beside the raw evidence it came
    # from, which is the only way to tell a good intent from a bad one.
    assert admin["intent"] == org["intent"] == "Wants a price"


def test_a_lead_captured_before_v27_renders_an_empty_intent_not_a_guess():
    """A row migrated in from v26 has `intent IS NULL`. The payload must carry the
    empty string — the panel's cue for its neutral placeholder — and must never
    substitute a fallback derived from the comment or the handle."""
    store = _bare_store()
    try:
        store.upsert_match(campaign_id="camp-a", reel_id="r", comment_id="legacy-1",
                           username="alice", text="how much?", lang="en", score=0.9,
                           reason="asked price", extracted=None, tier="local",
                           platform="instagram")          # no intent: pre-v27 row
        row = _build_matches(store, "camp-a")[0]
    finally:
        store.close()
    assert row["intent"] == ""
