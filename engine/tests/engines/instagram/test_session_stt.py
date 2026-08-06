"""Instagram Session <-> Uzbek-STT wiring, fully offline.

Proves the plumbing around core/transcribe.py (never the real KotibTranscriber —
that's another agent's test_transcribe.py): a Session with a real (non-Null)
Transcriber injected calls FeedSource.capture_audio -> Transcriber.transcribe ->
writes reel.transcript, persists it to seen_reels via mark_seen, cleans up the
audio file, and rolls the transcriptions counter up through Cascade -> _flush ->
SessionCounters -> the sessions table. Also proves the belt-and-suspenders
defence: a session holding a real transcriber still does nothing when the
CAMPAIGN gate (enable_stt / language_mix) is off — Cascade's own gate is
authoritative, not just "was a transcriber injected."
"""
import os
import sqlite3
import tempfile

from aizu.core.config import campaign_from_brief
from aizu.core.feed import Comment, FakeFeed, Reel
from aizu.core.mock_router import MockRouter
from aizu.core.pacing import Pacer, PacingConfig
from aizu.core.transcribe import NullTranscriber, Transcriber, TranscriptResult
from aizu.engines.instagram.session import Session, SessionConfig


def _store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from aizu.core.store import Store
    return Store(path), path


def _campaign(enable_stt=True, language_mix=("uz",)):
    return campaign_from_brief("ig-stt-leadgen", {
        "platform": "instagram", "threshold": 0.7, "escalate_band": [0.4, 0.75],
        "relevance_def": "acme saas app posts",
        "match_def": "a commenter asking to buy / price / contact",
        "extract_def": "- phone",
        "enable_stt": enable_stt, "language_mix": list(language_mix),
    })


def _daytime_pacer():
    return Pacer(cfg=PacingConfig(enforce_daytime=False), sleep=lambda _t: None)


class FakeTranscriber(Transcriber):
    """Records every audio path it's asked to transcribe; always returns the
    same canned Uzbek transcript. Stands in for a warm-loaded KotibTranscriber
    without touching faster_whisper/ffmpeg at all."""

    def __init__(self, text="bizning yangi demo mahsulot"):
        self.text = text
        self.calls: list[str] = []

    def transcribe(self, audio_path: str):
        self.calls.append(audio_path)
        return TranscriptResult(text=self.text, language="uz",
                                language_probability=1.0, duration_ms=1200)


def _seen_row(path, campaign_id, reel_id):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT transcript, transcript_lang FROM seen_reels "
        "WHERE campaign_id=? AND reel_id=?", (campaign_id, reel_id)).fetchone()
    con.close()
    return dict(row) if row else None


def test_default_transcriber_is_null_and_unwired():
    """Session() with no transcriber kwarg (every non-STT test/caller) must
    default to NullTranscriber and mark itself un-wired, so the lambda in
    _run_after_start never even builds a capture_audio call."""
    store, _ = _store()
    session = Session(store=store, router=MockRouter(store=store), feed=FakeFeed([]),
                      soul=None, campaign=_campaign(), pacer=_daytime_pacer(),
                      cfg=SessionConfig())
    assert isinstance(session.transcriber, NullTranscriber)
    assert session._stt_wired is False


def test_stt_wired_transcribes_persists_and_counts():
    """End-to-end (offline): a thin-on-relevance-cues caption leaves the
    relevance verdict unsure -> STT fires -> the transcript's extra relevance
    cue tips the verdict confidently relevant -> transcript is persisted on the
    seen_reels row and the session's transcriptions counter is 1."""
    store, path = _store()
    # MockRouter (core/mock_router.py) scores relevance by counting keyword
    # hits: 1 hit lands confidence in the escalate band (0.4-0.75) = "unsure";
    # the transcript below adds a SECOND hit ("demo"), which the STT-informed
    # re-classify sees, pushing confidence to 0.9 (2+ hits) and the verdict to
    # a confident relevant — the same mechanism as the sibling cascade test,
    # just driven by the real MockRouter instead of a scripted one.
    fd, audio_path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    transcriber = FakeTranscriber(text="bizning yangi demo bugun")
    reel = Reel("r1", caption="trial only", audio_path=audio_path,
                comments=[Comment("c1", "buyer1", "how much is the trial?")])
    session = Session(store=store, router=MockRouter(store=store),
                      feed=FakeFeed([reel]), soul=None,
                      campaign=_campaign(enable_stt=True, language_mix=["uz"]),
                      pacer=_daytime_pacer(), cfg=SessionConfig(),
                      transcriber=transcriber)
    assert session._stt_wired is True
    out = session.run()

    assert transcriber.calls == [audio_path]        # capture_audio -> transcribe, once
    assert not os.path.exists(audio_path)            # _transcribe_reel cleaned up the wav

    row = _seen_row(path, "ig-stt-leadgen", "r1")
    assert row == {"transcript": "bizning yangi demo bugun", "transcript_lang": "uz"}

    sess_row = store.get_session(session.session_id)
    assert sess_row["transcriptions"] == 1
    assert out["reels_seen"] == 1


def test_campaign_gate_off_ignores_a_wired_transcriber():
    """Belt-and-suspenders: injecting a real Transcriber is NOT sufficient on
    its own — Cascade's own per-campaign gate (enable_stt AND 'uz' in
    language_mix) must still block it when the campaign itself has STT off."""
    store, path = _store()
    fd, audio_path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    transcriber = FakeTranscriber()
    reel = Reel("r1", caption="trial only", audio_path=audio_path)
    session = Session(store=store, router=MockRouter(store=store),
                      feed=FakeFeed([reel]), soul=None,
                      campaign=_campaign(enable_stt=False, language_mix=["uz"]),
                      pacer=_daytime_pacer(), cfg=SessionConfig(),
                      transcriber=transcriber)
    # _stt_wired only reflects "was a real Transcriber injected" — Cascade's own
    # gate is the thing that actually decides whether to call it per-reel.
    assert session._stt_wired is True
    session.run()

    assert transcriber.calls == []
    row = _seen_row(path, "ig-stt-leadgen", "r1")
    assert row == {"transcript": None, "transcript_lang": None}
    os.unlink(audio_path)   # never consumed by the session — clean up ourselves
