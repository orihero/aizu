"""Local Uzbek STT ("KotibAI") — Instagram-only, campaign-gated (see cascade.py).

Deliberately NOT `Router.transcribe(...) -> Decision` (core/router.py:87-88, 571-574):
that protocol member models a distinct, still-unbuilt "cloud audio-judge" v2 tier.
This module produces plain transcript TEXT that feeds the *existing* classify_text
call sites exactly the way `reel.ocr_text` already does.
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional

from .logsetup import get_logger

log = get_logger(__name__)


@dataclass
class TranscriptSegment:
    """One STT segment with millisecond timing — lets a downstream fusion prompt
    cite spoken text at a timestamp ("at 4.2s the audio says …"), mirroring the
    frame roster ("FRAME n @ Xs"). Ported from soliq's ``TranscriptSegment``."""
    start_ms: int
    end_ms: int
    text: str


@dataclass
class TranscriptResult:
    text: str
    language: str
    language_probability: float
    duration_ms: int
    # Per-segment timing. Empty when a backend returns flat text only; the joined
    # `text` above is always the authoritative transcript regardless.
    segments: list[TranscriptSegment] = field(default_factory=list)


class Transcriber:
    """Protocol-by-duck-typing. transcribe() must never raise; return None on
    any failure (missing model, corrupt audio, decode error) — same never-wedge
    convention as every capture_* method in core/cdp.py."""
    def transcribe(self, audio_path: str) -> Optional[TranscriptResult]:
        raise NotImplementedError


class NullTranscriber(Transcriber):
    """Zero imports, always None. The default when the feature gate is off."""
    def transcribe(self, audio_path: str) -> Optional[TranscriptResult]:
        return None


def _ensure_cuda_dll_path() -> None:
    """Make ctranslate2's native CUDA loader able to find cublas64_12.dll etc. on
    Windows, BEFORE `from faster_whisper import WhisperModel` runs.

    Empirically verified on this machine (RUNTIME SPIKE, 2026-07-21):
    `os.add_dll_directory(...)` does NOT work — ctranslate2's native extension
    loads its CUDA dependencies in a way that ignores the add_dll_directory
    search list entirely. What DOES work is prepending the DLL directories to
    `os.environ["PATH"]` before the import happens (Windows' loader still
    consults PATH for dependent-DLL resolution). The DLLs ship as the pip
    packages `nvidia-cublas-cu12` / `nvidia-cudnn-cu12`, installed under
    `<site-packages>/nvidia/*/bin/`.

    No-op on non-Windows (Linux/mac wheels resolve CUDA differently and are not
    the platform this was reproduced on) and a no-op when those packages are
    simply not installed (glob finds nothing) — never raises.
    """
    if os.name != "nt":
        return
    try:
        for site_dir in sys.path:
            pattern = os.path.join(site_dir, "nvidia", "*", "bin")
            for bin_dir in glob.glob(pattern):
                if os.path.isdir(bin_dir) and bin_dir not in os.environ["PATH"]:
                    os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]
    except Exception as e:  # noqa: BLE001 - PATH massaging must never break startup
        log.debug("cuda dll path setup failed err=%s", e)


class KotibTranscriber(Transcriber):
    """faster-whisper/ctranslate2 wrapper around the KotibAI Uzbek CT2 model.

    Model load is deferred to the FIRST transcribe() call by default, but callers
    that already know the feature gate is on (engines/instagram/session.py
    run_session) should call `.warm()` explicitly at session construction time —
    NOT inside any per-reel deadline — to avoid the cold-load latency landing on
    whichever reel happens to be the first Uzbek-gated one in the walk. (Measured
    cold load: ~1.1s on cuda/float16 — cheap, but still not worth risking against
    the 90s per_reel_seconds budget.)
    """

    def __init__(self, model_path: str, device: str = "cuda",
                 compute_type: str = "float16", language: str = "uz"):
        self.model_path = model_path
        self.device = device
        self.compute_type = compute_type
        # Forced decode language. Default "uz" (the KotibAI fine-tune — a runtime
        # spike showed auto-detect misfires to 'en'). A general multilingual model
        # (soliq's faster-whisper-large-v3) can override via AIZU_STT_LANGUAGE.
        self.language = language or "uz"
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        _ensure_cuda_dll_path()  # must run BEFORE the faster_whisper import below
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise RuntimeError(
                "faster-whisper is required for Uzbek STT: "
                "pip install -e \".[stt]\"") from e
        self._model = WhisperModel(
            self.model_path, device=self.device, compute_type=self.compute_type)
        return self._model

    def warm(self) -> None:
        """Force the model load now (call at session construction, not per-reel)."""
        self._load()

    def transcribe(self, audio_path: str) -> Optional[TranscriptResult]:
        try:
            model = self._load()
            # ALWAYS explicit language="uz", task="transcribe" — CONFIRMED by runtime
            # spike: forced language="uz" gives info.language='uz' at probability 1.00,
            # while language=None (auto-detect) misfires to 'en' at p=0.51 on the same
            # audio. NEVER call with language=None.
            segments, info = model.transcribe(
                audio_path, language=self.language, task="transcribe", vad_filter=True)
            # faster-whisper yields a lazy generator; materialize once so we can
            # build both the joined text and the per-segment timing from it.
            seg_list = list(segments)
            text = " ".join(s.text.strip() for s in seg_list).strip()
            if not text:
                return None
            segs = [
                TranscriptSegment(
                    start_ms=int(getattr(s, "start", 0.0) * 1000),
                    end_ms=int(getattr(s, "end", 0.0) * 1000),
                    text=s.text.strip())
                for s in seg_list]
            return TranscriptResult(
                text=text, language=self.language,
                language_probability=getattr(info, "language_probability", 0.0),
                duration_ms=int(getattr(info, "duration", 0.0) * 1000),
                segments=segs)
        except Exception as e:  # noqa: BLE001 - never wedge the run
            log.warning("stt transcribe failed path=%s err=%s", audio_path, e)
            return None


def extract_audio_wav(media_path: str, out_wav_path: str,
                       sample_rate: int = 16000, timeout_s: float = 30.0) -> bool:
    """Shell out to ffmpeg (confirmed on PATH in dev) to produce a 16kHz mono WAV.
    Never raises — returns False on any failure (missing binary, bad input,
    timeout, non-zero exit)."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", media_path, "-ar", str(sample_rate),
             "-ac", "1", "-vn", out_wav_path],
            capture_output=True, timeout=timeout_s)
        return proc.returncode == 0 and os.path.exists(out_wav_path) \
            and os.path.getsize(out_wav_path) > 0
    except Exception as e:  # noqa: BLE001
        log.warning("ffmpeg extract failed media=%s err=%s", media_path, e)
        return False


def build_transcriber() -> Transcriber:
    """Factory read once at run_session() construction time. Returns
    NullTranscriber (zero faster_whisper import) unless AIZU_STT_ENABLED is truthy
    AND AIZU_STT_MODEL_PATH is set — the global ops kill-switch."""
    from .router import env_flag  # already-existing helper, no new dependency
    if not env_flag("AIZU_STT_ENABLED"):
        return NullTranscriber()
    model_path = os.environ.get("AIZU_STT_MODEL_PATH", "")
    if not model_path:
        log.warning("AIZU_STT_ENABLED is set but AIZU_STT_MODEL_PATH is empty — STT stays off")
        return NullTranscriber()
    device = os.environ.get("AIZU_STT_DEVICE", "cuda")
    compute_type = os.environ.get("AIZU_STT_COMPUTE_TYPE", "float16")
    language = os.environ.get("AIZU_STT_LANGUAGE", "").strip() or "uz"
    return KotibTranscriber(model_path, device=device, compute_type=compute_type,
                            language=language)
