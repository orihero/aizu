"""Uzbek STT gating inside Cascade.gate_reel (core/transcribe.py's real
KotibTranscriber is never touched here — only the `transcribe_fn` seam).

Covers the STRICT per-campaign gate (`campaign.enable_stt AND "uz" in
language_mix` — see cascade.py's `stt_gate_ok`), the per-reel cost gate (only
invoked when caption+vision left the verdict unsure, same discipline as the
vision tier), the transcript fold into the relevance re-classify call and into
`_comment_content`, and the `transcriptions` usage counter.
"""
from aizu.core.config import campaign_from_brief
from aizu.core.feed import Comment, Reel
from aizu.core.router import Decision
from aizu.engines.instagram.cascade import Cascade


class ScriptedRouter:
    """Minimal stand-in for Router — returns queued decisions, records the text
    calls so tests can inspect what content reached the model. Mirrors the
    ScriptedRouter idiom in ../../test_cascade.py, trimmed to what these
    STT-gate tests need (no image queue: these captions never trigger vision
    because no frames/capture_fn are ever supplied)."""

    def __init__(self, text_queue):
        self.text_queue = list(text_queue)
        self.text_calls = []  # [(stage, content), ...]

    def classify_text(self, *, instruction, content, campaign_id, stage,
                      session_id=None, system=None):
        self.text_calls.append((stage, content))
        return self.text_queue.pop(0)

    def classify_image(self, *, instruction, images_b64, campaign_id, stage,
                       session_id=None, system=None):
        raise AssertionError("vision must not be invoked — no frames supplied")

    def transcribe(self, **kw):
        raise NotImplementedError


def _campaign(enable_stt=True, language_mix=("uz",)):
    # threshold 0.7 / escalate_band (0.4, 0.75) — same shape as the shipped
    # config/campaign.md fixture the sibling test_cascade.py uses.
    return campaign_from_brief("ig-stt-leadgen", {
        "platform": "instagram", "threshold": 0.7, "escalate_band": [0.4, 0.75],
        "relevance_def": "acme saas app posts", "match_def": "buyer intent",
        "extract_def": "- phone",
        "enable_stt": enable_stt, "language_mix": list(language_mix),
    })


def _counting_transcribe(text="salom, narxi qancha"):
    """A `transcribe_fn` that records how many times it was invoked."""
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return text
    fn.calls = calls
    return fn


# The caption/decision shapes below are hand-picked, not MockRouter-derived —
# ScriptedRouter just replays whatever Decision is queued, so "unsure" here
# means "queued with a confidence inside the campaign's escalate_band".
_CAPTION_UNSURE = Decision("relevant", 0.6, 0.50)          # caption verdict, unsure
_CLOUD_RESOLVES = Decision("relevant", 0.85, 0.9)           # escalation verdict, confident


def test_stt_not_invoked_when_enable_stt_false():
    """The global per-campaign toggle is off -> transcribe_fn must never fire,
    even though language_mix has 'uz' and the caption verdict is unsure."""
    transcribe_fn = _counting_transcribe()
    r = ScriptedRouter([_CAPTION_UNSURE, _CLOUD_RESOLVES])
    camp = _campaign(enable_stt=False, language_mix=["uz"])
    reel = Reel("r1", caption="ambiguous but real caption text")
    res = Cascade(r, camp).gate_reel(reel, transcribe_fn=transcribe_fn)
    assert transcribe_fn.calls["n"] == 0
    assert reel.transcript == ""
    assert res.used_stt is False
    assert res.escalated is True   # still escalates to cloud — STT was just skipped


def test_stt_not_invoked_when_language_mix_lacks_uz():
    """enable_stt is on, but the campaign's language_mix doesn't declare 'uz' ->
    the strict AND gate must keep transcribe_fn dark."""
    transcribe_fn = _counting_transcribe()
    r = ScriptedRouter([_CAPTION_UNSURE, _CLOUD_RESOLVES])
    camp = _campaign(enable_stt=True, language_mix=["en", "ru"])
    reel = Reel("r1", caption="ambiguous but real caption text")
    res = Cascade(r, camp).gate_reel(reel, transcribe_fn=transcribe_fn)
    assert transcribe_fn.calls["n"] == 0
    assert reel.transcript == ""
    assert res.used_stt is False


def test_stt_not_invoked_when_language_mix_empty():
    """No empty-language_mix passthrough — an unset language_mix must NOT be
    read as 'anything goes'."""
    transcribe_fn = _counting_transcribe()
    r = ScriptedRouter([_CAPTION_UNSURE, _CLOUD_RESOLVES])
    camp = _campaign(enable_stt=True, language_mix=[])
    reel = Reel("r1", caption="ambiguous but real caption text")
    res = Cascade(r, camp).gate_reel(reel, transcribe_fn=transcribe_fn)
    assert transcribe_fn.calls["n"] == 0
    assert reel.transcript == ""


def test_stt_not_invoked_when_no_transcribe_fn_supplied():
    """Structural platform scoping: even a fully-gated-on campaign does nothing
    when the caller (a non-Instagram session, or Instagram before wiring) never
    passes transcribe_fn at all."""
    r = ScriptedRouter([_CAPTION_UNSURE, _CLOUD_RESOLVES])
    camp = _campaign(enable_stt=True, language_mix=["uz"])
    reel = Reel("r1", caption="ambiguous but real caption text")
    res = Cascade(r, camp).gate_reel(reel)   # transcribe_fn defaults to None
    assert res.used_stt is False
    assert reel.transcript == ""


def test_stt_not_invoked_when_caption_already_confident():
    """Per-reel cost gate: a confident caption verdict must not trigger STT,
    even when the campaign is fully gated on — same discipline as the existing
    vision-tier `test_lazy_capture_fn_only_runs_when_vision_needed` cost gate."""
    transcribe_fn = _counting_transcribe()
    r = ScriptedRouter([Decision("relevant", 0.9, 0.95)])   # confident, no tier needed
    camp = _campaign(enable_stt=True, language_mix=["uz"])
    reel = Reel("r1", caption="New Acme app feature launch")
    res = Cascade(r, camp).gate_reel(reel, transcribe_fn=transcribe_fn)
    assert transcribe_fn.calls["n"] == 0
    assert res.used_stt is False
    assert res.escalated is False


def test_stt_fires_folds_transcript_and_counts_when_fully_gated_on():
    """The positive case: enable_stt + 'uz' in language_mix + still-unsure verdict
    -> transcribe_fn runs exactly once, the transcript lands on the reel, the
    Cascade's transcriptions counter increments, and the re-classify call's
    content carries the 'SPOKEN TEXT (from audio):' tag + the transcript text."""
    transcribe_fn = _counting_transcribe("salom, narxi qancha ekan")
    # STT-informed re-classify comes back confident -> no cloud escalation needed.
    r = ScriptedRouter([_CAPTION_UNSURE, Decision("relevant", 0.9, 0.95)])
    camp = _campaign(enable_stt=True, language_mix=["uz"])
    reel = Reel("r1", caption="ambiguous but real caption text")
    cascade = Cascade(r, camp)
    res = cascade.gate_reel(reel, transcribe_fn=transcribe_fn)

    assert transcribe_fn.calls["n"] == 1
    assert reel.transcript == "salom, narxi qancha ekan"
    assert cascade.transcriptions == 1
    assert res.used_stt is True
    assert res.escalated is False   # STT verdict was confident -> cloud tier skipped
    assert len(r.text_calls) == 2   # caption, then STT-informed re-classify
    stage, content = r.text_calls[1]
    assert stage == "relevance"
    assert "SPOKEN TEXT (from audio):" in content
    assert "salom, narxi qancha ekan" in content


def test_stt_verdict_kept_only_when_more_confident():
    """When the STT-informed re-classify comes back LESS confident than the
    caption verdict, the caption verdict must win (the documented 'keep
    whichever verdict is more confident' rule) — but the transcript and
    counter still land, since the reel really was transcribed."""
    transcribe_fn = _counting_transcribe("noise")
    # STT re-classify is unsure too (0.50 < caption's 0.50? equal is fine, use a
    # strictly lower confidence to make the "kept caption" assertion unambiguous).
    r = ScriptedRouter([Decision("relevant", 0.6, 0.60),      # caption, unsure but conf 0.60
                        Decision("irrelevant", 0.1, 0.45),    # STT verdict, LESS confident
                        Decision("relevant", 0.85, 0.9)])     # cloud escalation resolves
    camp = _campaign(enable_stt=True, language_mix=["uz"])
    reel = Reel("r1", caption="ambiguous but real caption text")
    cascade = Cascade(r, camp)
    res = cascade.gate_reel(reel, transcribe_fn=transcribe_fn)

    assert reel.transcript == "noise"          # transcription still happened…
    assert cascade.transcriptions == 1
    assert res.used_stt is True
    # …but the weaker STT verdict was discarded, so the caption's "unsure"
    # verdict (0.60 confidence, inside the escalate band) still escalates.
    assert res.escalated is True
    assert len(r.text_calls) == 3


def test_transcript_returns_none_still_escalates_via_cloud():
    """transcribe_fn firing but coming back with no usable text (e.g. capture or
    ffmpeg failed) must leave the verdict untouched and fall through to the
    normal cloud-escalation tier — never crash, never fake a verdict."""
    def empty_transcribe():
        return None
    r = ScriptedRouter([_CAPTION_UNSURE, _CLOUD_RESOLVES])
    camp = _campaign(enable_stt=True, language_mix=["uz"])
    reel = Reel("r1", caption="ambiguous but real caption text")
    cascade = Cascade(r, camp)
    res = cascade.gate_reel(reel, transcribe_fn=empty_transcribe)

    assert reel.transcript == ""
    assert cascade.transcriptions == 0
    assert res.used_stt is False
    assert res.escalated is True


def test_comment_scoring_folds_transcript_into_reel_context():
    """`_comment_content` must include the transcript alongside caption/OCR text
    (score_comment reuses whatever gate_reel already wrote to reel.transcript —
    no re-transcription)."""
    r = ScriptedRouter([Decision("match", 0.8, 0.9)])
    reel = Reel("r3", author="acme.io", caption="",
                transcript="mahsulotning narxi qancha ekanini so'radi")
    Cascade(r, _campaign()).score_comment(
        Comment("c5", "marina", "narxi qancha?"), reel)
    _stage, content = r.text_calls[0]
    assert "spoken text: mahsulotning narxi qancha ekanini so'radi" in content


def test_comment_scoring_transcript_only_reel_not_dropped():
    """A reel with NO caption/OCR but a transcript must still enter the
    REEL BEING COMMENTED ON block (the transcript alone is enough context)."""
    r = ScriptedRouter([Decision("match", 0.9, 0.8)])   # clear of both the escalate
    # band and the threshold-straddle check (see _unsure()) so this resolves locally
    reel = Reel("r4", transcript="bizning demo mahsulotimiz haqida")
    Cascade(r, _campaign()).score_comment(Comment("c6", "u", "how much?"), reel)
    _stage, content = r.text_calls[0]
    assert "REEL BEING COMMENTED ON" in content
    assert "spoken text: bizning demo mahsulotimiz haqida" in content
