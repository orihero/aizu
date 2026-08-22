"""The author_id chain, end to end (schema v25, Campaign Lab Sheet #2/A).

`Reel.author_id` is only useful if it survives the whole path:
parser → Reel → session → `mark_seen` → `seen_reels.author_id` →
`Store.seed_candidates`. Six engines each call `mark_seen` separately, so a
silently-dropped kwarg in ONE of them means that platform mines seeds by display
name and every rename splits one candidate into two dead ones — with no error
anywhere.

Two layers of cover here: a real end-to-end run through the Reddit engine (the
simplest session to drive without a browser), and a cheap structural guard across
all six so a future edit to any single engine cannot drop the kwarg unnoticed.
"""
import ast
import os
import pathlib
import tempfile

import pytest

from aizu.core.config import campaign_from_brief
from aizu.core.feed import Comment, Reel
from aizu.core.router import Decision
from aizu.core.store import Store

ENGINES = ("instagram", "youtube", "telegram", "reddit", "x", "linkedin")


# ---------------- structural guard, all six engines ----------------

def _mark_seen_calls(engine: str) -> list[ast.Call]:
    src = pathlib.Path(
        f"{pathlib.Path(__file__).parents[2]}/aizu/engines/{engine}/session.py"
    ).read_text()
    return [n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "mark_seen"]


@pytest.mark.parametrize("engine", ENGINES)
def test_every_engine_passes_provenance_to_mark_seen(engine):
    """Both the gate-result path AND the parse-skip path must carry it: a reel
    that failed to classify is still evidence about which seed served it."""
    calls = _mark_seen_calls(engine)
    assert calls, f"{engine}/session.py calls mark_seen nowhere"
    for call in calls:
        kwargs = {kw.arg for kw in call.keywords}
        assert "source" in kwargs, f"{engine}: a mark_seen call drops `source`"
        assert "author_id" in kwargs, f"{engine}: a mark_seen call drops `author_id`"


# ---------------- real end-to-end run ----------------

class _Router:
    def classify_text(self, *, instruction, content, campaign_id, stage,
                      session_id=None, system=None):
        if stage == "relevance":
            return Decision(label="relevant", score=0.9, confidence=0.96)
        return Decision(label="yes", score=0.92, confidence=0.96,
                        extracted={"phone": "+998901234567"})

    def classify_image(self, **_kw):  # pragma: no cover - must never be called
        raise AssertionError("this engine must not call vision")


class _Feed:
    """Yields one relevant post carrying BOTH a display name and a stable id."""

    def walk(self):
        yield Reel(reel_id="s1", caption="Acme renovation, Tashkent",
                   author="Acme Renovations", author_id="t2_stable99",
                   source="uzbekistan")

    def fetch_comments(self, reel_id, cursor):
        return [Comment(comment_id="s1/c1", username="buyer", text="narxi qancha?")], None

    def capture_frames(self, reel, n=3):
        return []

    def healthy(self):
        return True


def _campaign():
    return campaign_from_brief("wiring-test", {
        "platform": "reddit", "threshold": 0.7,
        "relevance_def": "renovation", "match_def": "buyer intent",
        "extract_def": "- phone"})


@pytest.fixture
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = Store(path)
    yield s
    s.close()


def test_the_stable_id_survives_the_whole_path(store):
    from aizu.engines.reddit.session import run_session
    run_session(campaign=_campaign(), store=store, router=_Router(),
                feed=_Feed(), soul=None, pacer=None, run_id="run-1")

    row = store._conn.execute(
        "SELECT author, author_id, source FROM seen_reels WHERE reel_id='s1'"
    ).fetchone()
    assert row["author"] == "Acme Renovations"     # display name, for the operator
    assert row["author_id"] == "t2_stable99"       # stable id, for the seed
    assert row["source"] == "uzbekistan"           # which seed served it

    # …and the mined candidate is keyed on the id, not the name, so a later
    # rename stays one candidate rather than splitting into two dead ones.
    (candidate,) = store.seed_candidates("wiring-test")
    assert candidate["seed"] == "t2_stable99"
    assert candidate["author"] == "Acme Renovations"
    assert candidate["leads"] == 1                 # proof, not just a signal


def test_a_reel_with_no_stable_id_still_records_the_display_name(store):
    """Platforms that expose no stable id (and pre-v25 rows) must degrade to the
    display name, not to nothing."""
    from aizu.engines.reddit.session import run_session

    class _Anon(_Feed):
        def walk(self):
            yield Reel(reel_id="s2", caption="Acme renovation",
                       author="NoIdHere", source="uzbekistan")

    run_session(campaign=_campaign(), store=store, router=_Router(),
                feed=_Anon(), soul=None, pacer=None, run_id="run-2")
    row = store._conn.execute(
        "SELECT author, author_id FROM seen_reels WHERE reel_id='s2'").fetchone()
    assert row["author"] == "NoIdHere" and row["author_id"] is None
    (candidate,) = store.seed_candidates("wiring-test")
    assert candidate["seed"] == "NoIdHere"        # falls back to the name
