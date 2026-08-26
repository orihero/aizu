"""Adversarial regression suite for the v27 lead redaction.

Dropping the `username` and `text` KEYS from the org payload is not the same as
keeping those two VALUES out of it. Every test here started as an attempt to
break the redaction from the customer side and succeeded before the scrub in
`core.matching.redact_identity` / `redact_extracted` landed:

  * `reason` is model-authored and no prompt in `core/prompts.py` has ever
    constrained it — `MATCH_INTENT_RULE` binds `"intent"` alone, and a
    campaign-authored match prompt written before v27 constrains nothing. An
    ordinary reason names the handle and quotes the comment.
  * `extracted` was published raw. `_fallback_intent` already refused a value
    that is just the comment, but only for the line it composed; the same dict
    shipped beside it untouched.
  * an identity-named extracted key (`instagram_handle`) is neither a contact
    field nor an `@`-prefixed token, so it slipped past BOTH `ground_extracted`
    and `_strip_identity` and composed into the intent line itself.

The superadmin assertions are as load-bearing as the org ones: a scrub that also
blinded the platform admin would have removed the only place the raw comment can
still be read.
"""
import os
import tempfile

from aizu.core.matching import (derive_intent, ground_extracted,
                                redact_extracted, redact_identity)
from aizu.core.store import Store
from aizu.panel import _build_matches, _build_reels
from aizu.panel_org import build_admin_org_leads, build_leads_org

# One realistic Uzbek buying comment that carries the commenter's own handle —
# the exact shape the whole change exists for.
COMMENT = ("Salom! Menga 42 razmer qizil Nike krossovka kerak, Toshkentda. "
           "Yozing @alibek_uz")
HANDLE = "alibek_uz"
# The post id the sweeps below look for BY VALUE. Deliberately longer than two or
# three characters: since v28 an org lead row carries a 16-char `secrets.token_urlsafe`
# token, and a two-character needle turns up inside one about once every 250 runs
# (measured), which would make the headline invariant a coin flip rather than a test.
REEL = "reel-9f3a1c"


def _bare_store():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    return Store(path)


def _leaky_lead(store, *, cid="c1", org_id=None, comment_id="cm1"):
    """Seed one lead whose model output leaks identity three different ways.

    Nothing here is contrived: this is what a classifier returns when the prompt
    only ever told it to constrain `intent`.
    """
    store.upsert_campaign_meta(cid, org_id=org_id, display_name="C")
    extracted = ground_extracted({
        "product": "Nike krossovka",
        "size": "42",
        "city": "Toshkent",
        "instagram_handle": HANDLE,   # identity-named key, no "@" to strip
        "question": COMMENT,          # the whole comment in a non-contact key
        "phone": "+998901234567",     # hallucinated — grounding drops it
    }, COMMENT)
    store.upsert_match(
        campaign_id=cid, reel_id=REEL, comment_id=comment_id, username=HANDLE,
        text=COMMENT, lang="uz", score=0.9,
        reason=f'Commenter @{HANDLE} writes "{COMMENT}" — clear buying intent.',
        extracted=extracted, tier="local", platform="instagram", session_id="s1",
        intent=derive_intent(None, extracted=extracted,
                             post_caption="Nike sneakers drop",
                             comment_text=COMMENT))
    return extracted


# --------------------------------------------------------------------------- #
# The boundary itself
# --------------------------------------------------------------------------- #

def test_org_lead_payload_carries_neither_the_handle_nor_the_comment():
    """The headline invariant, asserted over the WHOLE serialized row.

    Checked as a substring sweep rather than field by field on purpose: a field
    added later inherits the assertion instead of quietly escaping it."""
    store = _bare_store()
    try:
        _leaky_lead(store)
        row = _build_matches(store, "c1")[0]
    finally:
        store.close()

    blob = repr(row)
    assert HANDLE not in blob, f"handle survived into the org payload: {row}"
    assert COMMENT not in blob, f"comment survived into the org payload: {row}"
    assert "username" not in row and "text" not in row
    # Redaction must not become deletion — the lead is still worth opening.
    assert "Nike krossovka" in row["intent"]
    assert row["extracted"]["size"] == "42"
    assert row["extracted"]["city"] == "Toshkent"


def test_no_org_facing_payload_carries_the_post_pointer():
    """A POINTER to the identity is the identity.

    `reelId` names the public post the comment sits on, where the handle and the
    words are both plainly readable. While the org payload carried it, the whole
    redaction was optional: any org user could walk the anonymized list, join each
    row's `reelId` against the reels list, open the post and read everything the
    redaction had just removed — with no audit row anywhere. So the field moved
    behind `include_identity` with the two it points at, and it is BLOCKED for an
    org rather than sold at the price of an audit row: `POST /api/lead/reveal`
    hands over the handle and nothing else, so there is no org-facing route to the
    post at all. An audited route to the post would be an audited route to the
    comment, and the comment is superadmin-only.

    Asserted over the WHOLE serialized payload of BOTH org producers, not just the
    key, so a future field that smuggles the same id under another name is caught.
    """
    store = _bare_store()
    try:
        _leaky_lead(store, org_id=1)
        row = _build_matches(store, "c1")[0]
        page = build_leads_org(store, None, org_id=1, role="owner")
    finally:
        store.close()
    assert "reelId" not in row
    # The id itself must be gone from the row, not merely renamed out of sight.
    assert REEL not in repr(row)
    for item in page["items"]:
        assert "reelId" not in item, "the /api/leads row still points at the post"


def test_the_superadmin_lead_keeps_the_post_pointer():
    """The other half: the plane that is allowed to read the comment is the only
    plane that keeps the way to it.

    For an org the post is blocked outright — the reveal returns the handle alone,
    so there is no audited route and no other route. That makes this the SOLE
    surviving surface for `reelId`, which is why it is asserted rather than assumed:
    a scrub that reached `include_identity=True` would leave the platform admin
    unable to open the post their own investigation depends on."""
    store = _bare_store()
    try:
        _leaky_lead(store, org_id=1)
        row = _build_matches(store, "c1", include_identity=True)[0]
    finally:
        store.close()
    assert row["reelId"] == REEL


def test_reason_keeps_its_verdict_after_the_quotation_is_cut():
    """A reason is scrubbed, not discarded: the classifier's judgement is the
    product, only the copied comment and the handle are not."""
    store = _bare_store()
    try:
        _leaky_lead(store)
        reason = _build_matches(store, "c1")[0]["reason"]
    finally:
        store.close()
    assert "clear buying intent" in reason
    assert HANDLE not in reason
    assert "Nike krossovka kerak" not in reason


def test_identity_named_extracted_key_never_reaches_the_org():
    """`instagram_handle` is not a contact field and carries no "@", so it beat
    both `ground_extracted` and `_strip_identity`. It is the handle regardless."""
    store = _bare_store()
    try:
        _leaky_lead(store)
        row = _build_matches(store, "c1")[0]
    finally:
        store.close()
    assert "instagram_handle" not in row["extracted"]
    # ...and it must not have composed into the derived line either.
    assert "instagram handle" not in row["intent"].lower()


def test_extracted_value_that_is_just_the_comment_is_dropped_not_trimmed():
    """`{"question": "<the whole comment>"}` has no fact left once the quotation
    goes, and an empty husk would imply the model extracted something."""
    store = _bare_store()
    try:
        _leaky_lead(store)
        extracted = _build_matches(store, "c1")[0]["extracted"]
    finally:
        store.close()
    assert "question" not in extracted


def test_contact_fields_survive_the_scrub():
    """The phone number is what the customer bought. `ground_extracted` already
    proves it was really in the comment; scrubbing it would delete the lead."""
    store = _bare_store()
    try:
        store.upsert_campaign_meta("c1", display_name="C")
        text = "Narxi qancha? Telefon +998 90 123 45 67"
        extracted = ground_extracted({"phone": "+998 90 123 45 67",
                                      "product": "krossovka"}, text)
        store.upsert_match(campaign_id="c1", reel_id="r1", comment_id="cm1",
                           username="dana", text=text, lang="uz", score=0.8,
                           reason="asked for a price", extracted=extracted,
                           tier="local", platform="instagram", intent="Wants a price")
        row = _build_matches(store, "c1")[0]
    finally:
        store.close()
    assert row["extracted"]["phone"] == "+998 90 123 45 67"


def test_superadmin_still_sees_the_handle_the_comment_and_the_raw_extracted():
    """The scrub is org-facing only. If it reached `include_identity=True` the
    platform admin would have lost the one remaining view of the raw lead."""
    store = _bare_store()
    try:
        _leaky_lead(store)
        row = _build_matches(store, "c1", include_identity=True)[0]
    finally:
        store.close()
    assert row["username"] == HANDLE
    assert row["text"] == COMMENT
    assert row["extracted"]["instagram_handle"] == HANDLE
    assert row["extracted"]["question"] == COMMENT
    assert HANDLE in row["reason"] and COMMENT in row["reason"]


# --------------------------------------------------------------------------- #
# The indirect leak: search as an existence oracle
# --------------------------------------------------------------------------- #

def test_free_text_search_is_not_a_handle_oracle_for_an_org():
    """Rendering no username is not enough: a search box that MATCHES one still
    confirms which handles an org's leads belong to, one guess at a time.

    The org path must answer the same way for a handle that is really there and
    a handle that is not — while the superadmin path, which asks for identity
    explicitly, must still find it."""
    store = _bare_store()
    try:
        _leaky_lead(store, org_id=1)
        org_hit = build_leads_org(store, None, org_id=1, role="owner", q=HANDLE)
        org_miss = build_leads_org(store, None, org_id=1, role="owner",
                                   q="someone_who_never_commented")
        admin_hit = build_admin_org_leads(store, org_id=1, q=HANDLE)
    finally:
        store.close()

    assert org_hit["total"] == 0, "searching a real handle must not confirm it"
    assert org_hit["total"] == org_miss["total"], "hit and miss must be indistinguishable"
    assert admin_hit["total"] == 1, "the superadmin plane keeps the identity search"


def test_free_text_search_is_not_a_comment_oracle_for_an_org():
    """Same oracle, phrased as the words the person wrote rather than their name."""
    store = _bare_store()
    try:
        _leaky_lead(store, org_id=1)
        hit = build_leads_org(store, None, org_id=1, role="owner",
                              q="qizil Nike krossovka kerak")
        # ...but the customer-facing prose IS searchable — redaction must not
        # cost the operator their leads page.
        usable = build_leads_org(store, None, org_id=1, role="owner", q="Toshkent")
    finally:
        store.close()
    assert hit["total"] == 0
    assert usable["total"] == 1


def test_sorting_by_username_is_inert_rather_than_a_500_or_an_oracle():
    """`?sort=username` survives in `_LEAD_SORT_KEYS` so a stale client bundle
    keeps working. It must therefore read every redacted row as equal — a sort
    that actually ordered by the hidden handle would leak it a bit at a time."""
    store = _bare_store()
    try:
        _leaky_lead(store, org_id=1, comment_id="cm1")
        # A second lead whose handle sorts BEFORE the first one's.
        store.upsert_match(campaign_id="c1", reel_id="r1", comment_id="cm2",
                           username="aaa_first", text="hi", lang="en", score=0.5,
                           reason="asked", extracted=None, tier="local",
                           platform="instagram", intent="Wants B")
        asc = build_leads_org(store, None, org_id=1, role="owner",
                              sort="username", descending=False)
        desc = build_leads_org(store, None, org_id=1, role="owner",
                               sort="username", descending=True)
    finally:
        store.close()
    assert [m["commentId"] for m in asc["items"]] == [m["commentId"] for m in desc["items"]], \
        "reversing a username sort reordered redacted rows — the handle is observable"


# --------------------------------------------------------------------------- #
# The helpers, directly
# --------------------------------------------------------------------------- #

def test_redact_identity_removes_a_handle_that_carries_no_at_sign():
    """`_strip_identity` only recognises "@handle". The boundary knows the real
    one, so a bare mention is not a guess — it is the same string."""
    assert HANDLE not in redact_identity(
        f"Buyer {HANDLE} wants sneakers", username=HANDLE, comment_text="x")
    # A longer token that merely CONTAINS the handle is left alone.
    assert "alibek_uz_official" in redact_identity(
        "see alibek_uz_official", username=HANDLE, comment_text="x")


def test_redact_identity_cuts_a_quoted_comment_out_of_its_framing():
    out = redact_identity(f'The user says "{COMMENT}" and wants a price',
                          username=HANDLE, comment_text=COMMENT)
    assert "krossovka kerak" not in out
    assert "wants a price" in out


def test_redact_identity_returns_empty_when_the_text_was_only_a_quotation():
    """Nothing left to say beats a fragment: the panel renders "not captured"."""
    assert redact_identity(COMMENT, username=HANDLE, comment_text=COMMENT) == ""


def test_redact_identity_leaves_ordinary_prose_untouched():
    """The scrub must be a no-op on the overwhelmingly common case, or every
    reason in the product silently degrades."""
    reason = "Clear buying intent: asks for a price and a size."
    assert redact_identity(reason, username=HANDLE, comment_text=COMMENT) == reason


def test_redact_identity_short_username_cannot_eat_the_sentence():
    """A one- or two-character handle would match inside ordinary words; the
    floor keeps a pathological account from blanking every reason in the org."""
    assert redact_identity("a big order", username="a", comment_text="x") == "a big order"


def test_redact_extracted_passes_structural_values_through():
    """Numbers, booleans and nested objects are not prose and carry no identity
    shape; rewriting them to strings would corrupt the panel's rendering."""
    out = redact_extracted({"qty": 3, "urgent": True, "meta": {"k": "v"}},
                           username=HANDLE, comment_text=COMMENT)
    assert out == {"qty": 3, "urgent": True, "meta": {"k": "v"}}


def test_redact_extracted_scrubs_a_handle_hidden_in_a_non_contact_value():
    out = redact_extracted({"note": f"ping {HANDLE} about sizing"},
                           username=HANDLE, comment_text=COMMENT)
    assert HANDLE not in out["note"]
    assert "about sizing" in out["note"]


def test_the_four_identity_shapes_the_scrub_actually_guarantees():
    """The guarantee, pinned as positive assertions so its EDGE is legible.

    These four shapes are removed: an `@`handle, an e-mail, a profile link, and
    a phone-shaped run of at least `_MIN_PHONE_DIGITS` digits — plus the row's
    own known handle and any quoted run of its own comment (covered above).

    What is NOT covered, verified by probing and reported rather than papered
    over, because each needs a signal the engine does not have:
      * a real display NAME ("Alibek Karimov"). `feed.Comment` carries only
        `username`, so there is nothing to match it against.
      * an obfuscated address ("alibek at gmail dot com").
      * a phone below the digit floor, or split by a separator outside
        `_PHONE_CANDIDATE_RE`'s class ("998*90*123*45*67"). The floor is what
        keeps "budget 500 000" in the sentence; lowering it trades one leak for
        a different kind of damage.
    A prompt is the control for those; `MATCH_INTENT_RULE` already asks for it.
    """
    kept = redact_identity(
        "Wants sneakers, dm @alibek_uz or ali@example.com or t.me/alibek "
        "or call +998 90 123 45 67",
        username="somebody_else", comment_text="menga kerak")
    assert "Wants sneakers" in kept          # the need survives...
    for shape in ("@alibek_uz", "ali@example.com", "t.me/alibek", "123 45 67"):
        assert shape not in kept, f"{shape!r} survived the scrub: {kept!r}"


def test_fallback_intent_will_not_compose_an_identity_named_key():
    """The regression that produced "…, instagram handle alibek_uz" — the one
    line the redaction exists to write, publishing the handle."""
    line = derive_intent(None,
                         extracted={"product": "krossovka", "profile": "alibek_uz"},
                         post_caption="sneakers", comment_text="menga kerak")
    assert "krossovka" in line
    assert "alibek_uz" not in line


# --------------------------------------------------------------------------- #
# The watchlist re-join (v27, found by an end-to-end payload sweep)
# --------------------------------------------------------------------------- #
def _scanned_campaign(store, *, cid="c1", org_id=None):
    """Three relevant posts, one lead, and the watchlist row the engine writes for
    the post that produced it. `add_to_watchlist` is called from exactly one place —
    after a comment batch, `if found` — which is what makes every watchlist-derived
    field a flag on the lead-bearing posts."""
    _leaky_lead(store, cid=cid, org_id=org_id)
    for rid, caption in (("r1", "Nike drop"), ("r2", "Adidas sale"),
                         ("r3", "Puma restock")):
        store.mark_seen(cid, rid, relevant=True, author="shop_uz", caption=caption)
    store.add_to_watchlist(cid, "r1")   # r1 is the post the lead came from
    return cid


def test_org_reels_do_not_mark_the_post_the_lead_came_from():
    """The whole point of withholding `reelId`, undone one payload over.

    An org-facing reel row used to carry `newSinceLastPoll` (the watchlist
    `match_count`) and `expiresInDays` (its TTL). Both exist only for a post that
    produced a lead, so each one alone answered "which of these posts do I open to
    read the handle and the comment" — restoring, from the reels payload, the exact
    route the lead payload closed, and doing it for a `viewer` too.
    """
    store = _bare_store()
    cid = _scanned_campaign(store)
    rows = {r["id"]: r for r in _build_reels(store, cid, 10.0)}
    # The posts still ship — a scanned post is the product (contract A7)...
    assert set(rows) == {"r1", "r2", "r3"}
    assert rows["r1"]["caption"] == "Nike drop"
    # ...and the lead-bearing one is indistinguishable from the two that produced none.
    assert "newSinceLastPoll" not in rows["r1"]
    assert "expiresInDays" not in rows["r1"]
    assert set(rows["r1"]) == set(rows["r2"]) == set(rows["r3"])
    store.close()


def test_superadmin_reels_keep_the_watchlist_signal():
    """The counterpart assertion, and as load-bearing as the one above: the platform
    admin exists to see exactly which post paid off. A scrub that also blinded them
    would have removed the only place this can still be read."""
    store = _bare_store()
    cid = _scanned_campaign(store)
    rows = {r["id"]: r for r in _build_reels(store, cid, 10.0, include_identity=True)}
    assert rows["r1"]["newSinceLastPoll"] == 1
    assert rows["r1"]["expiresInDays"] > 0
    assert rows["r2"]["newSinceLastPoll"] == 0
    store.close()


def test_org_reels_default_to_hiding_the_watchlist():
    """Default-DENY, same rule as `_build_matches`: a future caller that forgets the
    flag leaks nothing. Asserted on the keyword itself so flipping the default is a
    test failure rather than a quiet regression."""
    import inspect
    sig = inspect.signature(_build_reels)
    assert sig.parameters["include_identity"].default is False
    assert sig.parameters["include_identity"].kind is inspect.Parameter.KEYWORD_ONLY
