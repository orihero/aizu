"""Video-analysis tier gating inside Cascade.gate_reel (core/media.py is never
touched here — only the `video_analyze_fn` seam).

Covers the STRICT gate (`campaign.enable_video_analysis` AND the
AIZU_VIDEO_ANALYSIS_ENABLED global kill-switch — see cascade.py's
`video_gate_ok`), the per-reel cost gate (only when caption+vision+STT left the
verdict unsure), the FRAME roster prompt, the stronger-confidence fold, the
`reel.video_analysis` cache, and the `video_analyses` usage counter.
"""
import pytest

from aizu.core.config import campaign_from_brief
from aizu.core.feed import Reel
from aizu.core.router import Decision
from aizu.engines.instagram.cascade import Cascade


class ScriptedRouter:
    def __init__(self, text_queue, image_queue=None):
        self.text_queue = list(text_queue)
        self.image_queue = list(image_queue or [])
        self.text_calls = []
        self.image_calls = []   # [(stage, instruction, n_frames), ...]

    def classify_text(self, *, instruction, content, campaign_id, stage,
                      session_id=None, system=None):
        self.text_calls.append((stage, content))
        return self.text_queue.pop(0)

    def classify_image(self, *, instruction, images_b64, campaign_id, stage,
                       session_id=None, system=None):
        self.image_calls.append((stage, instruction, len(images_b64)))
        return self.image_queue.pop(0)

    def transcribe(self, **kw):
        raise NotImplementedError


def _campaign(enable_video_analysis=True):
    return campaign_from_brief("ig-va-leadgen", {
        "platform": "instagram", "threshold": 0.7, "escalate_band": [0.4, 0.75],
        "relevance_def": "acme saas app posts", "match_def": "buyer intent",
        "extract_def": "- phone",
        "enable_video_analysis": enable_video_analysis,
    })


def _counting_video(frames=("f1", "f2"), timestamps=(0, 1000)):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return list(frames)
    fn.calls = calls
    fn.timestamps = list(timestamps)
    return fn


_CAPTION_UNSURE = Decision("relevant", 0.6, 0.50)          # inside escalate band
_CAPTION_CONFIDENT = Decision("relevant", 0.95, 0.95)      # not unsure → no tiers
_VIDEO_RESOLVES = Decision("relevant", 0.9, 0.92, extracted={"brand": "acme"})


@pytest.fixture(autouse=True)
def _clear_va_env(monkeypatch):
    monkeypatch.delenv("AIZU_VIDEO_ANALYSIS_ENABLED", raising=False)


def _reel(ts=(0, 1000)):
    r = Reel(reel_id="r1", caption="thin caption text here")
    r.frame_timestamps = list(ts)
    return r


# ---- gate: campaign flag ----

def test_video_not_invoked_when_campaign_flag_off(monkeypatch):
    monkeypatch.setenv("AIZU_VIDEO_ANALYSIS_ENABLED", "1")   # global on
    vfn = _counting_video()
    r = ScriptedRouter([_CAPTION_UNSURE, Decision("relevant", 0.85, 0.9)])
    camp = _campaign(enable_video_analysis=False)            # campaign off
    Cascade(r, camp).gate_reel(_reel(), video_analyze_fn=vfn)
    assert vfn.calls["n"] == 0
    assert r.image_calls == []


def test_video_not_invoked_when_global_switch_off(monkeypatch):
    # campaign opted in, but the AIZU_VIDEO_ANALYSIS_ENABLED kill-switch is off
    vfn = _counting_video()
    r = ScriptedRouter([_CAPTION_UNSURE, Decision("relevant", 0.85, 0.9)])
    Cascade(r, _campaign()).gate_reel(_reel(), video_analyze_fn=vfn)
    assert vfn.calls["n"] == 0


def test_video_not_invoked_without_fn(monkeypatch):
    monkeypatch.setenv("AIZU_VIDEO_ANALYSIS_ENABLED", "1")
    r = ScriptedRouter([_CAPTION_UNSURE, Decision("relevant", 0.85, 0.9)])
    res = Cascade(r, _campaign()).gate_reel(_reel())        # no video_analyze_fn
    assert res.used_video is False


def test_video_not_invoked_when_caption_confident(monkeypatch):
    monkeypatch.setenv("AIZU_VIDEO_ANALYSIS_ENABLED", "1")
    vfn = _counting_video()
    r = ScriptedRouter([_CAPTION_CONFIDENT])               # verdict already sure
    res = Cascade(r, _campaign()).gate_reel(_reel(), video_analyze_fn=vfn)
    assert vfn.calls["n"] == 0 and res.used_video is False


# ---- positive path ----

def test_video_runs_when_gated_and_unsure(monkeypatch):
    monkeypatch.setenv("AIZU_VIDEO_ANALYSIS_ENABLED", "1")
    vfn = _counting_video()
    r = ScriptedRouter([_CAPTION_UNSURE], image_queue=[_VIDEO_RESOLVES])
    reel = _reel(ts=(0, 1500))
    casc = Cascade(r, _campaign())
    res = casc.gate_reel(reel, video_analyze_fn=vfn)
    assert vfn.calls["n"] == 1
    assert res.used_video is True and casc.video_analyses == 1
    # took the stronger-confidence video verdict → relevant, no cloud escalation
    assert res.relevant is True and res.decision.score == 0.9
    assert len(r.text_calls) == 1                          # only the caption text call
    # the video call carried a FRAME roster built from reel.frame_timestamps
    stage, instruction, n = r.image_calls[0]
    assert stage == "video_analysis" and n == 2
    assert "FRAME 1 @ 0.0s" in instruction and "FRAME 2 @ 1.5s" in instruction
    # structured extras cached on the reel
    assert reel.video_analysis == {"brand": "acme"}


def test_video_empty_frames_does_not_count_and_escalates(monkeypatch):
    monkeypatch.setenv("AIZU_VIDEO_ANALYSIS_ENABLED", "1")
    empty = _counting_video(frames=())
    # caption unsure → video returns [] → still unsure → cloud escalation text call
    r = ScriptedRouter([_CAPTION_UNSURE, Decision("relevant", 0.85, 0.9)])
    casc = Cascade(r, _campaign())
    res = casc.gate_reel(_reel(), video_analyze_fn=empty)
    assert empty.calls["n"] == 1
    assert res.used_video is False and casc.video_analyses == 0
    assert res.escalated is True                            # fell through to cloud
