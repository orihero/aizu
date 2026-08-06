"""
Assemble the scenes into one film and lay the narration over it.

    python tools/mix.py

Scene order and offsets are derived from the rendered files themselves, not hardcoded,
so re-timing a scene and re-rendering it is enough — this picks the change up.

Each line starts a beat after its scene's first frame (LEAD_IN) so narration never lands
exactly on a cut. Every line is loudness-normalised to the same target, because Zonos
output level drifts between takes.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
AUDIO = ROOT / "assets" / "audio"

SCENES = [f"scene{i:02d}" for i in range(1, 12)]
LEAD_IN = 0.30      # seconds after the cut before the voice starts
LUFS = -18.0        # integrated loudness target for the narration


def probe(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", str(path)],
        capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def main():
    vids = [OUT / f"{s}.mp4" for s in SCENES]
    missing = [v.name for v in vids if not v.exists()]
    if missing:
        raise SystemExit("missing renders: " + ", ".join(missing))

    durs = [probe(v) for v in vids]
    starts, t = [], 0.0
    for d in durs:
        starts.append(t)
        t += d
    total = t

    # 1 · picture
    concat = OUT / "_concat.txt"
    concat.write_text("".join(f"file '{v.name}'\n" for v in vids), encoding="utf-8")
    subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-f", "concat",
                    "-safe", "0", "-i", str(concat), "-c", "copy",
                    str(OUT / "_picture.mp4")], check=True)
    concat.unlink()

    # 2 · narration bed: every line delayed to its scene, normalised, summed
    vos, report = [], []
    for name, start, dur in zip(SCENES, starts, durs):
        wav = AUDIO / f"vo-{name}.wav"
        if not wav.exists():
            report.append((name, start, dur, None, "no VO"))
            continue
        vlen = probe(wav)
        end = LEAD_IN + vlen
        flag = "" if end <= dur + 0.02 else f"OVERRUNS by {end - dur:.2f}s"
        report.append((name, start, dur, vlen, flag))
        vos.append((wav, start + LEAD_IN))

    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(OUT / "_picture.mp4")]
    for wav, _ in vos:
        cmd += ["-i", str(wav)]

    parts = []
    for i, (_, at) in enumerate(vos, start=1):
        ms = int(round(at * 1000))
        parts.append(f"[{i}:a]loudnorm=I={LUFS}:TP=-1.5:LRA=11,"
                     f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                     f"adelay={ms}|{ms}[v{i}]")
    mixin = "".join(f"[v{i}]" for i in range(1, len(vos) + 1))
    parts.append(f"{mixin}amix=inputs={len(vos)}:normalize=0:dropout_transition=0,"
                 f"apad,atrim=0:{total:.3f},asetpts=N/SR/TB[vo]")

    cmd += ["-filter_complex", ";".join(parts),
            "-map", "0:v", "-map", "[vo]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
            str(OUT / "aizu-full.mp4")]
    subprocess.run(cmd, check=True)
    (OUT / "_picture.mp4").unlink()

    print(f"{'scene':<10}{'starts':>9}{'scene':>8}{'voice':>8}   note")
    print("-" * 60)
    for name, start, dur, vlen, flag in report:
        v = f"{vlen:.2f}s" if vlen else "--"
        print(f"{name:<10}{start:>8.2f}s{dur:>7.2f}s{v:>8}   {flag}")
    print(f"\ntotal {total:.2f}s -> out/aizu-full.mp4")


if __name__ == "__main__":
    main()
