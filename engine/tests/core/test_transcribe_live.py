"""Live smoke test for the real KotibAI Uzbek CT2 model (RUNTIME SPIKE, 2026-07-21).

@pytest.mark.slow — never runs in default CI. This is the ONE place that would
actually prove/disprove that `language="uz", task="transcribe"` beats the
model's stale HF `forced_decoder_ids=<|en|>` on THIS machine's copy of the
model (the spike already confirmed it once: forced 'uz' -> probability 1.00
vs. auto-detect misfiring to 'en' at p=0.51 — see core/transcribe.py's
KotibTranscriber.transcribe docstring). Re-run manually with `pytest -m slow`
whenever the model file or faster-whisper version changes; don't trust the
feature on say-so alone.

Requires, all three:
  1. the `stt` extra installed (faster-whisper + the CUDA DLL packages on
     Windows — see pyproject.toml's `stt` extra / core/transcribe.py's
     _ensure_cuda_dll_path);
  2. the real ~1.5GB CT2 model on disk (AIZU_STT_MODEL_PATH, defaulting to
     this repo's known dev-machine path per CLAUDE.md);
  3. a short real Uzbek speech .wav fixture — deliberately NOT checked into
     the repo (the plan's fixtures are synthetic only, see STT_PLAN.md
     "STILL UNVERIFIED"); drop one at the path below to run this locally.
Missing any of the three -> clean skip, never an error/failure.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

# This repo's known local model location (CLAUDE.md), overridable so this test
# can also run on a different machine that has the model elsewhere.
DEFAULT_MODEL_PATH = r"D:\ai-models\ct2\kotib-uzbek_stt_v1"
MODEL_PATH = os.environ.get("AIZU_STT_MODEL_PATH", DEFAULT_MODEL_PATH)

# Not shipped in the repo — see module docstring point 3.
FIXTURE_WAV = Path(__file__).parent.parent / "fixtures" / "stt_uzbek_sample.wav"


def _faster_whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def _skip_reason() -> str:
    if not _faster_whisper_available():
        return 'faster-whisper not installed — pip install -e ".[stt]"'
    if not os.path.isdir(MODEL_PATH):
        return f"no local KotibAI model at {MODEL_PATH} (set AIZU_STT_MODEL_PATH)"
    if not FIXTURE_WAV.exists():
        return (f"no Uzbek audio fixture at {FIXTURE_WAV} — drop a short real "
                "Uzbek recording there to enable this test")
    return ""


@pytest.mark.skipif(bool(_skip_reason()), reason=_skip_reason() or "n/a")
def test_kotib_transcriber_transcribes_uzbek_audio_with_high_language_confidence():
    from aizu.core.transcribe import KotibTranscriber

    tr = KotibTranscriber(MODEL_PATH, device="cuda", compute_type="float16")
    tr.warm()  # cold load is ~1.1s per the spike — fine inline in a slow test
    result = tr.transcribe(str(FIXTURE_WAV))

    assert result is not None, "expected a transcript, got a soft-failed None"
    assert result.language == "uz"           # ALWAYS forced — see transcribe.py
    assert result.text.strip() != ""
    # The spike's headline finding: forced uz landed at probability 1.00 on its
    # sample clip. Don't over-assert an exact number here (a different clip's
    # confidence will vary) — just confirm it's not the ~0.51 auto-detect
    # misfire band that motivated the explicit language="uz" override.
    assert result.language_probability > 0.51
