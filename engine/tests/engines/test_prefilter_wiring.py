"""The pre-filter is wired into every engine's comment path, and it is observable.

A pre-filtered comment is never scored and never stored, so an over-eager filter
is an invisible lost lead. The counters exist so it stops being invisible.
"""
import ast
import pathlib

import pytest

from aizu.core.config import campaign_from_brief
from aizu.core.feed import Comment

ENGINES = ("instagram", "youtube", "telegram", "reddit", "x", "linkedin")
ROOT = pathlib.Path(__file__).parents[2]


class _CountingRouter:
    """Fails loudly if the cascade sends a comment it should have filtered."""

    def __init__(self):
        self.calls = 0

    def classify_text(self, **kw):
        from aizu.core.router import Decision
        self.calls += 1
        return Decision(label="no", score=0.1, confidence=0.9)

    def classify_image(self, **kw):  # pragma: no cover
        raise AssertionError("vision must not fire on a comment")


def _cascade_class(mod):
    """Each engine names its own class (Cascade, XCascade, RedditCascade, …)."""
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, type) and name.endswith("Cascade") \
                and obj.__module__ == mod.__name__:
            return obj
    raise AssertionError(f"no Cascade class in {mod.__name__}")


def _cascade(engine):
    mod = __import__(f"aizu.engines.{engine}.cascade", fromlist=["cascade"])
    campaign = campaign_from_brief("c1", {
        "platform": engine, "threshold": 0.7,
        "relevance_def": "x", "match_def": "y", "extract_def": "- phone"})
    router = _CountingRouter()
    return _cascade_class(mod)(router=router, campaign=campaign), router


@pytest.mark.parametrize("engine", ENGINES)
def test_a_reaction_only_comment_never_reaches_the_model(engine):
    cas, router = _cascade(engine)
    res = cas.score_comment(Comment(comment_id="c1", username="u", text="🔥🔥"))
    assert router.calls == 0
    assert res.is_match is False
    assert res.decision.tier == "prefilter"
    assert cas.prefiltered == {"no_words": 1}


@pytest.mark.parametrize("engine", ENGINES)
def test_a_real_question_does_reach_the_model(engine):
    cas, router = _cascade(engine)
    cas.score_comment(Comment(comment_id="c1", username="u", text="narxi qancha?"))
    assert router.calls == 1
    assert cas.prefiltered == {}


@pytest.mark.parametrize("engine", ENGINES)
def test_one_author_repeating_themselves_is_scored_once(engine):
    cas, router = _cascade(engine)
    for i in range(3):
        cas.score_comment(Comment(comment_id=f"c{i}", username="spam", text="buy now"))
    assert router.calls == 1
    assert cas.prefiltered == {"duplicate": 2}


@pytest.mark.parametrize("engine", ENGINES)
def test_two_authors_with_the_same_question_are_both_scored(engine):
    cas, router = _cascade(engine)
    cas.score_comment(Comment(comment_id="c1", username="a", text="narxi?"))
    cas.score_comment(Comment(comment_id="c2", username="b", text="narxi?"))
    assert router.calls == 2
    assert cas.prefiltered == {}


@pytest.mark.parametrize("engine", ENGINES)
def test_the_docstring_promise_is_now_true(engine):
    """Three of these files have advertised a local pre-filter since they were
    written; none had one."""
    src = (ROOT / f"aizu/engines/{engine}/cascade.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "score_comment")
    first = fn.body[0] if not isinstance(fn.body[0], ast.Expr) else fn.body[1]
    assert isinstance(first, ast.Assign), f"{engine}: score_comment does not open with the filter"
    call = first.value
    assert isinstance(call, ast.Call) and call.func.id == "comment_prefilter_reason", (
        f"{engine}: the pre-filter must be the FIRST thing score_comment does — "
        "anything before it is work spent on a comment we are about to drop")
