"""Every engine captures rejected comments for later labelling (Sheet #3 / Remedy E).

The seam is `self.router.store`, NOT a constructor argument: three of the six
engines wrap the router in a `_HeartbeatRouter` facade that forwards attribute
access, so one seam covers all six without touching a session file.
"""
import os
import tempfile

import pytest

from aizu.core.config import campaign_from_brief
from aizu.core.feed import Comment
from aizu.core.router import Decision
from aizu.core.store import Store

ENGINES = ("instagram", "youtube", "telegram", "reddit", "x", "linkedin")


class _Router:
    """A router that carries a store, like the real one does."""

    def __init__(self, store, score):
        self.store = store
        self._score = score

    def classify_text(self, **kw):
        return Decision(label="no" if self._score < 0.7 else "yes",
                        score=self._score, confidence=0.85,
                        reason="because", tier="cloud", raw='{"score": %s}' % self._score)

    def classify_image(self, **kw):  # pragma: no cover
        raise AssertionError("vision must not fire on a comment")


class _Facade:
    """Mirrors _HeartbeatRouter: forwards everything to the wrapped router."""

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _cascade_class(mod):
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, type) and name.endswith("Cascade") \
                and obj.__module__ == mod.__name__:
            return obj
    raise AssertionError(f"no Cascade class in {mod.__name__}")


@pytest.fixture
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = Store(path)
    yield s
    s.close()


def _cascade(engine, store, score, wrap=False):
    mod = __import__(f"aizu.engines.{engine}.cascade", fromlist=["cascade"])
    campaign = campaign_from_brief("c1", {
        "platform": engine, "threshold": 0.7, "relevance_def": "x",
        "match_def": "y", "extract_def": "- phone"})
    router = _Router(store, score)
    return _cascade_class(mod)(router=_Facade(router) if wrap else router,
                               campaign=campaign, session_id="s1")


@pytest.mark.parametrize("engine", ENGINES)
def test_a_near_miss_is_kept_for_labelling(engine, store):
    """0.62 against a 0.7 gate: rejected, and exactly the kind of boundary case a
    threshold sweep turns on. It used to be discarded."""
    cas = _cascade(engine, store, 0.62)
    res = cas.score_comment(Comment(comment_id="k1", username="u", text="narxi qancha?"))
    assert res.is_match is False
    (row,) = store.eval_candidates("c1", platform=engine)
    assert row["band"] == "near"
    assert row["text"] == "narxi qancha?"
    assert row["score"] == 0.62 and row["threshold"] == 0.7
    assert row["confidence"] == 0.85 and row["raw"]
    assert row["session_id"] == "s1"


@pytest.mark.parametrize("engine", ENGINES)
def test_capture_works_through_the_heartbeat_facade(engine, store):
    """instagram/linkedin/x wrap the router; the seam must survive that."""
    cas = _cascade(engine, store, 0.62, wrap=True)
    cas.score_comment(Comment(comment_id="k1", username="u", text="narxi?"))
    assert len(store.eval_candidates("c1", platform=engine)) == 1


@pytest.mark.parametrize("engine", ENGINES)
def test_an_accepted_match_is_captured_too(engine, store):
    cas = _cascade(engine, store, 0.91)
    res = cas.score_comment(Comment(comment_id="k1", username="u", text="narxi?"))
    assert res.is_match is True
    assert store.eval_candidates("c1", platform=engine)[0]["band"] == "accepted"


@pytest.mark.parametrize("engine", ENGINES)
def test_a_pre_filtered_comment_is_not_captured(engine, store):
    """It never reached the model, so there is no verdict to label."""
    cas = _cascade(engine, store, 0.62)
    cas.score_comment(Comment(comment_id="k1", username="u", text="🔥🔥"))
    assert store.eval_candidates("c1", platform=engine) == []


@pytest.mark.parametrize("engine", ENGINES)
def test_a_storeless_router_captures_nothing_and_does_not_crash(engine, store):
    """Dry runs, tests and the replay harness all run store-less."""
    class _Bare:
        def classify_text(self, **kw):
            return Decision(label="no", score=0.1, confidence=0.9)

    mod = __import__(f"aizu.engines.{engine}.cascade", fromlist=["cascade"])
    campaign = campaign_from_brief("c1", {
        "platform": engine, "threshold": 0.7, "relevance_def": "x",
        "match_def": "y", "extract_def": "- phone"})
    cas = _cascade_class(mod)(router=_Bare(), campaign=campaign)
    assert cas.score_comment(
        Comment(comment_id="k1", username="u", text="narxi?")).is_match is False


@pytest.mark.parametrize("engine", ENGINES)
def test_the_session_cap_bounds_a_long_run(engine, store, monkeypatch):
    monkeypatch.setattr(Store, "EVAL_SESSION_CAP", 3)
    cas = _cascade(engine, store, 0.62)
    for i in range(10):
        cas.score_comment(Comment(comment_id=f"k{i}", username=f"u{i}", text=f"narxi {i}?"))
    assert len(store.eval_candidates("c1", platform=engine)) == 3


@pytest.mark.parametrize("engine", ENGINES)
def test_a_broken_store_never_fails_the_run(engine, store):
    """Collection for a future gold set must not break a run that is finding leads."""
    class _Exploding:
        def __getattr__(self, name):
            raise RuntimeError("db is gone")

    mod = __import__(f"aizu.engines.{engine}.cascade", fromlist=["cascade"])
    campaign = campaign_from_brief("c1", {
        "platform": engine, "threshold": 0.7, "relevance_def": "x",
        "match_def": "y", "extract_def": "- phone"})
    router = _Router(_Exploding(), 0.91)
    cas = _cascade_class(mod)(router=router, campaign=campaign)
    assert cas.score_comment(
        Comment(comment_id="k1", username="u", text="narxi?")).is_match is True
