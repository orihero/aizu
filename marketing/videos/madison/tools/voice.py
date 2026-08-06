"""
Generate the narration locally and verify it, with no cloud dependency.

    <venv>\\python.exe tools/voice.py speak            # TTS every line in vo/lines.json
    <venv>\\python.exe tools/voice.py speak scene04    # …or just one
    <venv>\\python.exe tools/voice.py check            # ASR back, compare, print durations

TTS is Zonos-v0.1-transformer (Zyphra, Apache-2.0) running on the local GPU; ASR is
faster-whisper large-v3. Both models are already cached under D:\\AI\\hf_cache.

Why the ASR pass: nobody here can hear the output, so "check" transcribes what was
actually synthesised and diffs it against the intended words. That catches dropped
words, spelled-out acronyms, and mangled brand names — and hands back the real
per-line durations the scenes have to be retimed against.

Run it with the venv at D:\\AI\\uzbek-tts\\.venv, which already has zonos, torch+cu130,
librosa, soundfile, phonemizer and faster-whisper installed.
"""

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINES = ROOT / "vo" / "lines.json"
OUT = ROOT / "assets" / "audio"
UZ = Path(r"D:\AI\uzbek-tts")

os.environ.setdefault("HF_HOME", r"D:\AI\hf_cache")
# phonemizer needs to be pointed at the eSpeak NG install before zonos is imported
os.environ.setdefault("PHONEMIZER_ESPEAK_LIBRARY", r"C:\Program Files\eSpeak NG\libespeak-ng.dll")
os.environ.setdefault("PHONEMIZER_ESPEAK_PATH", r"C:\Program Files\eSpeak NG\espeak-ng.exe")

SEED = 421  # fixed so a re-run reproduces the same read
REF = UZ / "Zonos" / "assets" / "exampleaudio.mp3"


def load_lines(only=None):
    data = json.loads(LINES.read_text(encoding="utf-8"))
    rows = data["lines"]
    if only:
        rows = [r for r in rows if r["id"] == only]
        if not rows:
            raise SystemExit(f"no line with id {only!r}")
    return rows


def split_sentences(text):
    return [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]


def speak(only=None):
    import torch
    import librosa
    import soundfile as sf
    from zonos.model import Zonos
    from zonos.conditioning import make_cond_dict
    from zonos.utils import DEFAULT_DEVICE as device

    OUT.mkdir(parents=True, exist_ok=True)
    rows = load_lines(only)

    print(f"[tts] loading Zonos on {device}", flush=True)
    model = Zonos.from_pretrained("Zyphra/Zonos-v0.1-transformer", device=device)

    y, sr = librosa.load(str(REF), sr=None, mono=True)
    speaker = model.make_speaker_embedding(torch.from_numpy(y).unsqueeze(0), sr)
    out_sr = model.autoencoder.sampling_rate

    for row in rows:
        torch.manual_seed(SEED)
        pieces = []
        for sent in split_sentences(row["speak"]):
            cond = make_cond_dict(text=sent, speaker=speaker, language="en-us")
            codes = model.generate(model.prepare_conditioning(cond), disable_torch_compile=True)
            pieces.append(model.autoencoder.decode(codes).cpu()[0])
        full = torch.cat(pieces, dim=-1)
        path = OUT / f"vo-{row['id']}.wav"
        sf.write(str(path), full.detach().cpu().numpy().T, out_sr)
        print(f"[tts] {row['id']}: {full.shape[-1] / out_sr:.2f}s -> {path.name}", flush=True)


def norm(s):
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split()


def check(only=None):
    from faster_whisper import WhisperModel

    rows = load_lines(only)
    # CPU on purpose: ctranslate2 4.8 links against CUDA 12 (cublas64_12.dll) while this
    # venv's torch is cu130, so the GPU path raises on first use — and because
    # transcribe() returns a lazy generator, it raises when consumed, not when created.
    # These are ~5s clips, so int8 on CPU is fast enough to not be worth fixing.
    print("[asr] loading faster-whisper large-v3 (cpu/int8)", flush=True)
    model = WhisperModel("large-v3", device="cpu", compute_type="int8")

    print()
    print(f"{'line':<10}{'audio':>8}  transcript / notes")
    print("-" * 100)
    for row in rows:
        path = OUT / f"vo-{row['id']}.wav"
        if not path.exists():
            print(f"{row['id']:<10}{'--':>8}  (not generated)")
            continue
        segs, info = model.transcribe(str(path), language="en", word_timestamps=True)
        segs = list(segs)
        heard = " ".join(s.text.strip() for s in segs).strip()
        words = [w for s in segs for w in (s.words or [])]
        dur = words[-1].end if words else 0.0

        want, got = norm(row["speak"]), norm(heard)
        missing = [w for w in want if w not in got]

        print(f"{row['id']:<10}{dur:>7.2f}s  {heard}")
        if missing:
            print(f"{'':<10}{'':>8}  ⚠ not heard: {' '.join(missing)}")
        print()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "speak"
    only = sys.argv[2] if len(sys.argv) > 2 else None
    if cmd == "speak":
        speak(only)
    elif cmd == "check":
        check(only)
    else:
        raise SystemExit("usage: voice.py [speak|check] [line-id]")
