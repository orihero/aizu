"""
Generate the narration with ElevenLabs, replacing the local Zonos takes.

    python tools/eleven.py voices              # list the voices on this account
    python tools/eleven.py models              # list models the key can use
    python tools/eleven.py speak               # TTS every line in vo/lines.json
    python tools/eleven.py speak scene04       # ...or just one
    python tools/eleven.py cost                # characters/credits, generating nothing

Then verify and retime as before:

    <venv>\\python.exe tools/voice.py check     # ASR the result back, diff the words
    python tools/mix.py                        # reassemble; prints any OVERRUNS

Writes the same `assets/audio/vo-<id>.wav` files the Zonos path wrote, so `mix.py` and
the scenes need no changes. `voice.py` stays as the offline fallback and still owns the
`check` pass — that is provider-agnostic, it just transcribes whatever wav is on disk.

Stdlib only (urllib), so this runs under any Python on the box — it does not need the
torch venv that `voice.py` requires.

Config, from `.env` beside this project or the real environment (env wins):

    ELEVENLABS_API_KEY   required
    ELEVEN_VOICE_ID      required for `speak` — see `voices`
    ELEVEN_MODEL         default eleven_multilingual_v2
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINES = ROOT / "vo" / "lines.json"
OUT = ROOT / "assets" / "audio"
API = "https://api.elevenlabs.io"

# mp3 on purpose: the pcm_* output formats are gated to higher plans, while mp3 works on
# every tier. ffmpeg (already required by mix.py) converts it to the wav the pipeline wants.
FORMAT = "mp3_44100_128"

SEED = 421  # same fixed seed as the Zonos path, so a re-run reproduces the same read

VOICE_SETTINGS = {
    "stability": 0.45,        # lower = more expressive, higher = flatter and more repeatable
    "similarity_boost": 0.80,
    "style": 0.0,             # >0 adds delivery flourish and costs latency; keep off for VO
    "use_speaker_boost": True,
}


def load_env():
    """Read .env beside the project into os.environ without overriding the real env."""
    for path in (ROOT / ".env", ROOT.parent.parent.parent / ".env"):
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def key():
    load_env()
    k = os.environ.get("ELEVENLABS_API_KEY")
    if not k:
        raise SystemExit(
            "ELEVENLABS_API_KEY is not set.\n"
            f"Put it in {ROOT / '.env'} as ELEVENLABS_API_KEY=... (that path is gitignored)."
        )
    return k


def call(path, body=None, raw=False):
    req = urllib.request.Request(f"{API}{path}", method="POST" if body else "GET")
    req.add_header("xi-api-key", key())
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode("utf-8")
    try:
        with urllib.request.urlopen(req) as r:
            return r.read() if raw else json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise SystemExit(f"ElevenLabs {e.code} on {path}:\n{detail}")


def load_lines(only=None):
    rows = json.loads(LINES.read_text(encoding="utf-8"))["lines"]
    if only:
        rows = [r for r in rows if r["id"] == only]
        if not rows:
            raise SystemExit(f"no line with id {only!r}")
    return rows


def voices():
    data = call("/v1/voices")
    print(f"{'voice_id':<24}{'name':<24}labels")
    print("-" * 90)
    for v in data.get("voices", []):
        labels = ", ".join(f"{k}={x}" for k, x in (v.get("labels") or {}).items())
        print(f"{v['voice_id']:<24}{v.get('name', ''):<24}{labels}")


def models():
    data = call("/v1/models")
    print(f"{'model_id':<34}{'tts':<5}name")
    print("-" * 90)
    for m in data:
        tts = "yes" if m.get("can_do_text_to_speech") else "-"
        print(f"{m['model_id']:<34}{tts:<5}{m.get('name', '')}")


def cost(only=None):
    rows = load_lines(only)
    total = 0
    print(f"{'line':<10}{'chars':>7}  text")
    print("-" * 90)
    for r in rows:
        n = len(r["speak"])
        total += n
        print(f"{r['id']:<10}{n:>7}  {r['speak'][:66]}")
    print(f"\n{len(rows)} lines, {total} characters (1 credit per character on most plans)")


def speak(only=None):
    load_env()
    voice_id = os.environ.get("ELEVEN_VOICE_ID")
    if not voice_id:
        raise SystemExit("ELEVEN_VOICE_ID is not set — run `python tools/eleven.py voices` to pick one.")
    model = os.environ.get("ELEVEN_MODEL", "eleven_multilingual_v2")

    OUT.mkdir(parents=True, exist_ok=True)
    rows = load_lines(only)
    print(f"[tts] ElevenLabs voice={voice_id} model={model}", flush=True)

    for row in rows:
        audio = call(
            f"/v1/text-to-speech/{voice_id}?output_format={FORMAT}",
            {
                "text": row["speak"],
                "model_id": model,
                "voice_settings": VOICE_SETTINGS,
                "seed": SEED,
            },
            raw=True,
        )
        mp3 = OUT / f"vo-{row['id']}.mp3"
        wav = OUT / f"vo-{row['id']}.wav"
        mp3.write_bytes(audio)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(mp3),
             "-ac", "1", "-ar", "44100", str(wav)],
            check=True,
        )
        mp3.unlink()
        dur = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", str(wav)],
            capture_output=True, text=True, check=True).stdout.strip()
        print(f"[tts] {row['id']}: {float(dur):.2f}s -> {wav.name}", flush=True)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "speak"
    only = sys.argv[2] if len(sys.argv) > 2 else None
    if cmd == "voices":
        voices()
    elif cmd == "models":
        models()
    elif cmd == "cost":
        cost(only)
    elif cmd == "speak":
        speak(only)
    else:
        raise SystemExit("usage: eleven.py [voices|models|cost|speak] [line-id]")
