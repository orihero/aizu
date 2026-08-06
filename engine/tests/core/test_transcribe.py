"""core/transcribe.py — the local Uzbek STT ("KotibAI") wrapper.

No GPU, no faster_whisper, no 1.5GB model here. The missing-dependency paths are
SIMULATED via `_no_faster_whisper` rather than relying on the `stt` extra being
absent from the env — these must pass identically whether or not an operator has
run `pip install -e ".[stt]"` on the machine. `extract_audio_wav` is exercised
with `subprocess.run` monkeypatched on the transcribe module itself, so these
tests never touch a real ffmpeg binary either. See test_transcribe_live.py for
the one place that actually loads the real model (@pytest.mark.slow, skips here).
"""
from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.fixture
def _no_faster_whisper(monkeypatch):
    """Force `import faster_whisper` to raise ImportError inside _load().

    A None entry in sys.modules makes the import statement itself fail, which is
    exactly what an uninstalled `stt` extra looks like — and unlike deleting the
    entry, it stays deterministic on a machine where the extra IS installed.
    """
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

from aizu.core import transcribe as t


# ---------------------------------------------------------------------------
# NullTranscriber — the default when the feature gate is off.
# ---------------------------------------------------------------------------

def test_null_transcriber_always_returns_none():
    tr = t.NullTranscriber()
    assert tr.transcribe("whatever.wav") is None
    assert tr.transcribe("") is None


# ---------------------------------------------------------------------------
# build_transcriber() — the global AIZU_STT_ENABLED / AIZU_STT_MODEL_PATH gate.
# ---------------------------------------------------------------------------

def _clear_stt_env(monkeypatch):
    for var in ("AIZU_STT_ENABLED", "AIZU_STT_MODEL_PATH", "AIZU_STT_DEVICE",
                "AIZU_STT_COMPUTE_TYPE"):
        monkeypatch.delenv(var, raising=False)


def test_build_transcriber_defaults_to_null_when_gate_unset(monkeypatch):
    _clear_stt_env(monkeypatch)
    assert isinstance(t.build_transcriber(), t.NullTranscriber)


def test_build_transcriber_stays_null_when_enabled_but_no_model_path(monkeypatch):
    # The ops kill-switch alone isn't enough to arm real STT — an empty
    # AIZU_STT_MODEL_PATH must keep it off too (and log a warning, not raise).
    _clear_stt_env(monkeypatch)
    monkeypatch.setenv("AIZU_STT_ENABLED", "1")
    assert isinstance(t.build_transcriber(), t.NullTranscriber)


@pytest.mark.parametrize("falsy", ["", "0", "false", "no", "off"])
def test_build_transcriber_stays_null_for_falsy_enabled_values(monkeypatch, falsy):
    _clear_stt_env(monkeypatch)
    monkeypatch.setenv("AIZU_STT_ENABLED", falsy)
    monkeypatch.setenv("AIZU_STT_MODEL_PATH", "/models/kotib")
    assert isinstance(t.build_transcriber(), t.NullTranscriber)


def test_build_transcriber_returns_kotib_when_fully_gated_on(monkeypatch):
    _clear_stt_env(monkeypatch)
    monkeypatch.setenv("AIZU_STT_ENABLED", "true")
    monkeypatch.setenv("AIZU_STT_MODEL_PATH", "/models/kotib-uzbek")
    tr = t.build_transcriber()
    assert isinstance(tr, t.KotibTranscriber)
    assert tr.model_path == "/models/kotib-uzbek"
    # Device/compute_type default to the values baked for the RTX 5090 spike.
    assert tr.device == "cuda"
    assert tr.compute_type == "float16"


def test_build_transcriber_honors_device_and_compute_type_overrides(monkeypatch):
    _clear_stt_env(monkeypatch)
    monkeypatch.setenv("AIZU_STT_ENABLED", "1")
    monkeypatch.setenv("AIZU_STT_MODEL_PATH", "/models/kotib-uzbek")
    monkeypatch.setenv("AIZU_STT_DEVICE", "cpu")
    monkeypatch.setenv("AIZU_STT_COMPUTE_TYPE", "int8")
    tr = t.build_transcriber()
    assert tr.device == "cpu"
    assert tr.compute_type == "int8"


# ---------------------------------------------------------------------------
# KotibTranscriber — soft failure when faster_whisper isn't installed.
# ---------------------------------------------------------------------------

def test_kotib_load_raises_runtime_error_with_pip_hint_when_faster_whisper_missing(
        _no_faster_whisper):
    tr = t.KotibTranscriber("/models/kotib-uzbek")
    with pytest.raises(RuntimeError, match=r"pip install -e \".\[stt\]\""):
        tr.warm()


def test_kotib_transcribe_soft_fails_when_faster_whisper_missing(_no_faster_whisper):
    # transcribe() must never let the load failure escape — same never-wedge
    # contract as every capture_* method in core/cdp.py.
    tr = t.KotibTranscriber("/models/kotib-uzbek")
    assert tr.transcribe("some/audio.wav") is None


class _FakeSegment:
    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text


class _FakeInfo:
    language_probability = 0.99
    duration = 12.5


class _FakeModel:
    """Stands in for faster_whisper.WhisperModel: returns a lazy generator of
    segments + an info object, exactly like the real .transcribe()."""

    def __init__(self, segments):
        self._segments = segments

    def transcribe(self, audio_path, language=None, task=None, vad_filter=None):
        return (iter(self._segments), _FakeInfo())


def test_kotib_transcribe_builds_segments_with_ms_timestamps(monkeypatch):
    tr = t.KotibTranscriber("/models/kotib-uzbek")
    tr._model = _FakeModel([
        _FakeSegment(0.0, 2.5, " salom "),
        _FakeSegment(2.5, 5.0, " narxi qancha "),
    ])
    res = tr.transcribe("audio.wav")
    assert res is not None
    assert res.text == "salom narxi qancha"        # joined + stripped
    assert res.language == "uz"
    assert [(s.start_ms, s.end_ms, s.text) for s in res.segments] == [
        (0, 2500, "salom"),
        (2500, 5000, "narxi qancha"),
    ]
    assert res.duration_ms == 12500                 # info.duration * 1000


def test_kotib_transcribe_empty_text_returns_none(monkeypatch):
    tr = t.KotibTranscriber("/models/kotib-uzbek")
    tr._model = _FakeModel([_FakeSegment(0.0, 1.0, "   ")])
    assert tr.transcribe("audio.wav") is None       # no usable text → None


def test_kotib_load_is_cached_after_first_success(monkeypatch):
    # _load() must not re-import/re-construct once a model is resident. Stub
    # _load itself (loading the real model is test_transcribe_live.py's job).
    tr = t.KotibTranscriber("/models/kotib-uzbek")
    calls = []
    tr._model = object()  # pretend a model is already loaded
    monkeypatch.setattr(t, "_ensure_cuda_dll_path", lambda: calls.append(1))
    assert tr._load() is tr._model
    assert calls == []  # short-circuited before touching CUDA/DLL setup at all


# ---------------------------------------------------------------------------
# extract_audio_wav() — subprocess.run monkeypatched on the transcribe module.
# ---------------------------------------------------------------------------

def _fake_run_ok(out_path: str):
    def run(cmd, capture_output=True, timeout=None):
        with open(out_path, "wb") as f:
            f.write(b"\x00" * 16)  # non-empty — extract_audio_wav checks getsize > 0
        return subprocess.CompletedProcess(cmd, returncode=0)
    return run


def test_extract_audio_wav_success(monkeypatch, tmp_path):
    out = tmp_path / "out.wav"
    monkeypatch.setattr(t.subprocess, "run", _fake_run_ok(str(out)))
    assert t.extract_audio_wav("in.mp4", str(out)) is True


def test_extract_audio_wav_nonzero_exit_is_false(monkeypatch, tmp_path):
    out = tmp_path / "out.wav"

    def run(cmd, capture_output=True, timeout=None):
        return subprocess.CompletedProcess(cmd, returncode=1)
    monkeypatch.setattr(t.subprocess, "run", run)
    assert t.extract_audio_wav("in.mp4", str(out)) is False
    assert not out.exists()


def test_extract_audio_wav_missing_binary_is_false(monkeypatch, tmp_path):
    # ffmpeg not on PATH raises FileNotFoundError from subprocess.run.
    def run(cmd, capture_output=True, timeout=None):
        raise FileNotFoundError("ffmpeg")
    monkeypatch.setattr(t.subprocess, "run", run)
    assert t.extract_audio_wav("in.mp4", str(tmp_path / "out.wav")) is False


def test_extract_audio_wav_timeout_is_false(monkeypatch, tmp_path):
    def run(cmd, capture_output=True, timeout=None):
        raise subprocess.TimeoutExpired(cmd, timeout)
    monkeypatch.setattr(t.subprocess, "run", run)
    assert t.extract_audio_wav("in.mp4", str(tmp_path / "out.wav"), timeout_s=1.0) is False


def test_extract_audio_wav_zero_size_output_is_false(monkeypatch, tmp_path):
    # returncode 0 but ffmpeg wrote an empty file (e.g. a genuinely audio-less
    # input) must still be treated as failure, not a usable transcript source.
    out = tmp_path / "out.wav"

    def run(cmd, capture_output=True, timeout=None):
        out.write_bytes(b"")
        return subprocess.CompletedProcess(cmd, returncode=0)
    monkeypatch.setattr(t.subprocess, "run", run)
    assert t.extract_audio_wav("in.mp4", str(out)) is False


def test_extract_audio_wav_bad_path_never_raises():
    # No monkeypatching at all: a nonexistent input handed to the real
    # subprocess call must degrade to False, never propagate an exception —
    # this is the "soft-fail on a bad path" contract regardless of whether
    # ffmpeg itself is on PATH in the test environment.
    ok = t.extract_audio_wav("/definitely/does/not/exist.mp4",
                             "/definitely/does/not/exist.wav", timeout_s=5.0)
    assert ok is False
