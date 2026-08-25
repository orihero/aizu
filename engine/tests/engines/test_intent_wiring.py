"""Every engine hands the store a redacted `intent` for the lead it just stored.

Schema v27 drops the username and the raw comment from the org-facing payload, so
this one line is the whole of what a customer reads about a lead. An engine that
forgets to pass it does not fail — it quietly writes a lead nobody can act on —
so the wiring is asserted per engine on the source, the way the pre-filter's is,
and not only through the one platform a happy-path test happens to walk.
"""
import ast
import os
import pathlib
import tempfile

import pytest

from aizu.core.config import load_campaign, load_soul
from aizu.core.feed import Comment, FakeFeed, Reel
from aizu.core.mock_router import MockRouter
from aizu.core.pacing import Pacer, PacingConfig
from aizu.core.router import _decision_from_payload
from aizu.core.store import Store
from aizu.engines.instagram.session import Session, SessionConfig

ENGINES = ("instagram", "youtube", "telegram", "reddit", "x", "linkedin")
ROOT = pathlib.Path(__file__).parents[2]
CONFIG = ROOT / "config"


def _upsert_match_call(engine):
    """The single `self.store.upsert_match(...)` call in that engine's session."""
    tree = ast.parse((ROOT / f"aizu/engines/{engine}/session.py").read_text())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "upsert_match"]
    assert len(calls) == 1, f"{engine}: expected exactly one upsert_match call site"
    return calls[0]


@pytest.mark.parametrize("engine", ENGINES)
def test_every_session_passes_a_derived_intent(engine):
    call = _upsert_match_call(engine)
    kw = {k.arg: k.value for k in call.keywords if k.arg}
    assert "intent" in kw, (
        f"{engine}: the lead is stored with no intent — org-facing, that lead is "
        "blank, because username and text are no longer part of the payload")
    derive = kw["intent"]
    assert isinstance(derive, ast.Call) and getattr(derive.func, "id", "") == "derive_intent", (
        f"{engine}: intent must come from matching.derive_intent — it is the "
        "redaction boundary (echo test, identity stripping), not a formatter")
    # The model's own line is the first positional arg; the rest are keyword-only.
    assert derive.args, f"{engine}: derive_intent called without the model's intent"
    names = {k.arg for k in derive.keywords}
    assert names == {"extracted", "post_caption", "comment_text"}, (
        f"{engine}: derive_intent needs all three grounding inputs, got {names} — "
        "without comment_text the echo test cannot fire and the comment can be "
        "republished verbatim as the 'summary'")


@pytest.mark.parametrize("engine", ENGINES)
def test_every_session_imports_derive_intent(engine):
    src = (ROOT / f"aizu/engines/{engine}/session.py").read_text()
    assert "derive_intent" in src.split("class ", 1)[0], (
        f"{engine}: derive_intent is not imported at module scope")


def test_a_stored_lead_carries_an_intent_and_no_identity():
    """End-to-end on the one engine with a fake feed: the row the panel reads has
    an intent, and that intent leaks neither the commenter nor their phone number.

    The mock router emits no top-level `intent` (nor does any campaign-authored
    MATCH prompt), so this also covers the deterministic fallback — the path the
    existing fleet of campaigns will actually take.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = Store(path)
    feed = FakeFeed([
        Reel("r1", caption="Acme app — plan your sprints, free trial", comments=[
            Comment("c1", "dana", "How much is the Pro plan? +1 415 555 0142", "en"),
        ]),
    ])
    Session(store=store, router=MockRouter(store=store), feed=feed,
            soul=load_soul(CONFIG / "soul.md"),
            campaign=load_campaign(CONFIG / "campaign.md"),
            pacer=Pacer(cfg=PacingConfig(enforce_daytime=False), sleep=lambda _t: None),
            cfg=SessionConfig()).run()

    rows = store.matches(load_campaign(CONFIG / "campaign.md").campaign_id)
    assert rows, "the fixture comment should have matched"
    intent = (rows[0]["intent"] or "")
    assert intent, "a match with a grounded extract must produce an intent line"
    assert "dana" not in intent.lower()
    assert "415" not in intent          # the phone belongs in `extracted`, not in prose
    assert rows[0]["text"] not in intent  # never the comment handed back
    store.close()


class _IntentRouter(MockRouter):
    """A MockRouter whose MATCH verdict carries its own `intent`, the way a v27 prompt
    is supposed to. The verdict is rebuilt through the REAL `_decision_from_payload`
    rather than by assigning the attribute — Python would happily let a test stick
    `intent` onto a dataclass that has no such field, which is exactly the broken
    state this is here to catch."""

    def classify_text(self, **kw):
        d = super().classify_text(**kw)
        if kw.get("stage") != "match":
            return d
        return _decision_from_payload(
            {"label": d.label, "score": d.score, "confidence": d.confidence,
             "reason": d.reason, "extracted": d.extracted,
             "intent": "Wants pricing for the Pro plan"},
            d.tier, d.usd, d.raw or "")


def test_the_models_own_intent_is_preferred_over_the_fallback():
    """The end of the wire. `Decision` carried no `intent` field for a while, so the
    key was parsed off the reply and then dropped before any engine could read it:
    the model's line never reached `derive_intent`, every lead took the deterministic
    fallback, and nothing failed. Only a test that puts an intent on the DECISION and
    reads the STORED row can tell the two paths apart."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = Store(path)
    feed = FakeFeed([
        Reel("r1", caption="Acme app — plan your sprints, free trial", comments=[
            Comment("c1", "dana", "How much is the Pro plan? +1 415 555 0142", "en"),
        ]),
    ])
    Session(store=store, router=_IntentRouter(store=store), feed=feed,
            soul=load_soul(CONFIG / "soul.md"),
            campaign=load_campaign(CONFIG / "campaign.md"),
            pacer=Pacer(cfg=PacingConfig(enforce_daytime=False), sleep=lambda _t: None),
            cfg=SessionConfig()).run()

    rows = store.matches(load_campaign(CONFIG / "campaign.md").campaign_id)
    assert rows, "the fixture comment should have matched"
    assert rows[0]["intent"] == "Wants pricing for the Pro plan"
    store.close()
