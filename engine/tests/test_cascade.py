from pathlib import Path

from reelradar.engines.instagram.cascade import Cascade
from reelradar.core.config import campaign_from_brief, load_campaign
from reelradar.core.feed import Comment, Reel
from reelradar.core.router import Decision

CONFIG = Path(__file__).resolve().parents[1] / "config"


class ScriptedRouter:
    """Returns queued decisions; records calls so we can assert escalation."""
    def __init__(self, text_queue, image_queue=None):
        self.text_queue = list(text_queue)
        self.image_queue = list(image_queue or [])
        self.text_calls = []
        self.image_calls = []
        self.systems = []        # system prompt passed on each text call
        self.instructions = []   # brief/instruction passed on each text call

    def classify_text(self, *, instruction, content, campaign_id, stage,
                      session_id=None, system=None):
        self.text_calls.append((stage, content))
        self.systems.append(system)
        self.instructions.append(instruction)
        return self.text_queue.pop(0)

    def classify_image(self, *, instruction, images_b64, campaign_id, stage,
                       session_id=None, system=None):
        self.image_calls.append(images_b64)
        return self.image_queue.pop(0)

    def transcribe(self, **kw):
        raise NotImplementedError


def camp():
    return load_campaign(CONFIG / "campaign.md")  # band (0.40, 0.75), threshold 0.70


def test_confident_relevance_no_escalation():
    r = ScriptedRouter([Decision("relevant", 0.9, 0.95)])
    res = Cascade(r, camp()).gate_reel(Reel("r1", caption="acme app demo"))
    assert res.relevant and not res.escalated and not res.used_vision
    assert len(r.text_calls) == 1


def test_unsure_relevance_uses_vision_then_escalates():
    # caption unsure (conf 0.5 in band) → vision also unsure (0.6) → escalate
    r = ScriptedRouter(
        text_queue=[Decision("relevant", 0.6, 0.50),   # caption, unsure
                    Decision("relevant", 0.85, 0.9)],  # cloud escalation, confident
        image_queue=[Decision("relevant", 0.6, 0.60)], # vision, still unsure
    )
    reel = Reel("r1", caption="ambiguous", on_screen_frames=["b64"])
    res = Cascade(r, camp()).gate_reel(reel)
    assert res.used_vision and res.escalated and res.relevant
    assert len(r.image_calls) == 1
    assert "[ESCALATED]" in r.text_calls[-1][1]


def test_thin_caption_uses_vision_as_primary():
    # Empty/emoji caption → don't trust text; judge from the video frames first.
    r = ScriptedRouter(text_queue=[], image_queue=[Decision("relevant", 0.85, 0.9)])
    reel = Reel("r1", caption="🔥🔥", on_screen_frames=["b64a", "b64b"])
    res = Cascade(r, camp()).gate_reel(reel)
    assert res.used_vision and res.relevant
    assert len(r.text_calls) == 0                 # caption classifier never called
    assert r.image_calls[0] == ["b64a", "b64b"]   # all frames sent to one vision call


def test_thin_caption_no_frames_falls_back_to_text():
    # Thin caption but nothing to screenshot → fall back to the text verdict.
    r = ScriptedRouter(text_queue=[Decision("irrelevant", 0.1, 0.9)])
    res = Cascade(r, camp()).gate_reel(Reel("r1", caption=""))
    assert not res.relevant and not res.used_vision
    assert len(r.text_calls) == 1


def test_lazy_capture_fn_only_runs_when_vision_needed():
    # A confident caption verdict must NOT trigger a screenshot capture.
    calls = {"n": 0}
    def cap():
        calls["n"] += 1
        return ["frame"]
    r = ScriptedRouter(text_queue=[Decision("relevant", 0.95, 0.97)])
    res = Cascade(r, camp()).gate_reel(Reel("r1", caption="New Acme app feature launch"),
                                       capture_fn=cap)
    assert res.relevant and calls["n"] == 0       # no capture for a confident caption


def test_comment_match_threshold():
    r = ScriptedRouter([Decision("match", 0.8, 0.9)])
    res = Cascade(r, camp()).score_comment(Comment("c1", "u", "how much is the Pro plan"))
    assert res.is_match and not res.escalated


def test_comment_scoring_includes_reel_context():
    """The reel's caption + on-screen text must reach the match stage so the
    model can fill plan/team-size/use-case from the post being asked
    about (the comment alone rarely states them)."""
    r = ScriptedRouter([Decision("match", 0.8, 0.9)])
    reel = Reel("r3", author="acme.io",
                caption="Acme app demo and free trial",
                ocr_text="PRO PLAN · from $25/seat · monthly billing")
    Cascade(r, camp()).score_comment(
        Comment("c5", "marina", "How much is the Pro plan? +1 415 555 0142", "en"), reel)
    _stage, content = r.text_calls[0]
    assert "Acme app demo and free trial" in content
    assert "from $25/seat" in content
    assert "How much is the Pro plan? +1 415 555 0142" in content
    # the comment must be clearly delimited from the seller's reel text
    assert content.index("COMMENT") > content.index("REEL")


def test_comment_scoring_without_reel_is_comment_only():
    r = ScriptedRouter([Decision("match", 0.8, 0.9)])
    Cascade(r, camp()).score_comment(Comment("c1", "u", "how much is the Pro plan"))
    _stage, content = r.text_calls[0]
    assert content == "how much is the Pro plan"


def test_comment_below_threshold_not_match():
    r = ScriptedRouter([Decision("no", 0.2, 0.9)])
    res = Cascade(r, camp()).score_comment(Comment("c2", "u", "nice"))
    assert not res.is_match


def test_comment_unsure_escalates():
    r = ScriptedRouter([Decision("match", 0.6, 0.55),   # unsure (band)
                        Decision("match", 0.9, 0.95)])  # escalation resolves
    res = Cascade(r, camp()).score_comment(Comment("c3", "u", "maybe"))
    assert res.escalated and res.is_match


def test_degraded_relevance_escalates_not_dropped():
    # A degraded caption verdict (cloud failed / malformed) must trigger a retry,
    # not be read as a confident reject that silently drops a relevant reel.
    r = ScriptedRouter(
        text_queue=[Decision("unknown", 0.0, 0.0, tier="degraded"),  # 1st call degraded
                    Decision("relevant", 0.85, 0.9)],                # retry resolves
    )
    res = Cascade(r, camp()).gate_reel(Reel("r1", caption="Acme app demo, free trial"))
    assert res.escalated and res.relevant
    assert len(r.text_calls) == 2  # degraded result was retried, not dropped


def test_degraded_comment_escalates():
    r = ScriptedRouter([Decision("unknown", 0.0, 0.0, tier="degraded"),
                        Decision("match", 0.9, 0.95)])
    res = Cascade(r, camp()).score_comment(Comment("c4", "u", "how much is the Pro plan?"))
    assert res.escalated and res.is_match


def _custom_extract_campaign():
    # A UI-style brief with a custom extract schema and NO tuned match prompt.
    return campaign_from_brief("custom-leadgen", {
        "platform": "instagram", "threshold": 0.7,
        "match_def": "buyer intent", "extract_def": "- phone\n- company name",
    })


def test_match_instruction_injects_declared_field_schema():
    """The declared fields must reach the model as an explicit output contract —
    a JSON skeleton of exactly those keys — not just as loose Extract prose."""
    r = ScriptedRouter([Decision("match", 0.9, 0.95, extracted={})])
    Cascade(r, _custom_extract_campaign()).score_comment(
        Comment("c1", "u", "we need an app, call me"))
    instr = r.instructions[0]
    assert '"phone": null' in instr and '"company_name": null' in instr
    assert "EXACTLY these keys" in instr


def test_extracted_coerced_to_declared_fields():
    """Stray keys the model invents are dropped and missing declared keys are
    back-filled with null, so the stored lead matches the Extract contract."""
    messy = Decision("match", 0.9, 0.95,
                     extracted={"phone": "+14155550142", "junk": "x"})
    r = ScriptedRouter([messy])
    res = Cascade(r, _custom_extract_campaign()).score_comment(
        Comment("c1", "u", "call me +14155550142"))
    assert res.decision.extracted == {"phone": "+14155550142", "company_name": None}


def test_no_declared_fields_leaves_extraction_unconstrained():
    """An empty Extract section must NOT force an empty schema — the model's
    extracted object passes through untouched (legacy behaviour)."""
    camp = campaign_from_brief("loose", {"match_def": "intent", "extract_def": ""})
    passthrough = Decision("match", 0.9, 0.95, extracted={"anything": "kept"})
    r = ScriptedRouter([passthrough])
    res = Cascade(r, camp).score_comment(Comment("c1", "u", "interested"))
    assert res.decision.extracted == {"anything": "kept"}
    assert "OUTPUT CONTRACT" not in r.instructions[0]
