"""core/media.py — download / frame-extract / sample, all never-raise.

`sample_frames`/`frames_to_base64`/`MediaWorkdir` are pure and always run.
`download_media` uses a fake httpx transport (no network). `extract_frames` runs
a real ffmpeg round-trip against a tiny generated clip, skipped when ffmpeg is
absent (mirrors the STT tests' posture toward external binaries).
"""
import base64
import os
import shutil
import subprocess

import pytest

from aizu.core import media as m


# ---- MediaWorkdir ----

def test_workdir_creates_and_removes_even_on_exception():
    captured = {}
    try:
        with m.MediaWorkdir() as wd:
            captured["path"] = wd.path
            assert os.path.isdir(wd.path)
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert not os.path.exists(captured["path"])   # swept despite the exception


# ---- download_media (fake httpx) ----

class _FakeResp:
    def __init__(self, status_code, content):
        self.status_code = status_code
        self.content = content


class _FakeHttpx:
    def __init__(self, resp=None, raises=None):
        self._resp = resp
        self._raises = raises
        self.calls = []

    def get(self, url, headers=None, timeout=None, follow_redirects=None):
        self.calls.append((url, headers))
        if self._raises:
            raise self._raises
        return self._resp


def test_download_media_writes_on_success(monkeypatch, tmp_path):
    body = b"\x00" * 50_000
    monkeypatch.setattr(m, "httpx", _FakeHttpx(_FakeResp(200, body)))
    dest = tmp_path / "v.mp4"
    assert m.download_media("http://x/v.mp4", dest_path=str(dest),
                            cookies="a=b", user_agent="UA", referer="R") is True
    assert dest.read_bytes() == body


def test_download_media_rejects_small_body(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "httpx", _FakeHttpx(_FakeResp(200, b"tiny")))
    dest = tmp_path / "v.mp4"
    assert m.download_media("http://x/v.mp4", dest_path=str(dest)) is False
    assert not dest.exists()


def test_download_media_rejects_non_200(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "httpx", _FakeHttpx(_FakeResp(403, b"\x00" * 50_000)))
    assert m.download_media("http://x", dest_path=str(tmp_path / "v.mp4")) is False


def test_download_media_never_raises_on_network_error(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "httpx", _FakeHttpx(raises=OSError("conn reset")))
    assert m.download_media("http://x", dest_path=str(tmp_path / "v.mp4")) is False


def test_download_media_false_when_httpx_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "httpx", None)
    assert m.download_media("http://x", dest_path=str(tmp_path / "v.mp4")) is False


# ---- sample_frames (pure) ----

def test_sample_frames_even_spacing_and_timestamps():
    paths = [f"/f/frame-{i:04d}.png" for i in range(10)]
    got = m.sample_frames(paths, fps=1.0, max_frames=4)
    assert len(got) == 4
    idxs = [f.index for f in got]
    assert idxs == sorted(idxs) and idxs[0] == 0            # spans from the start
    assert all(f.time_ms == f.index * 1000 for f in got)    # 1fps → idx seconds
    # picks span the pool rather than clustering at the front
    assert idxs[-1] >= 6


def test_sample_frames_fewer_than_max_returns_all():
    paths = [f"/f/{i}.png" for i in range(3)]
    got = m.sample_frames(paths, fps=2.0, max_frames=8)
    assert [f.index for f in got] == [0, 1, 2]
    assert [f.time_ms for f in got] == [0, 500, 1000]       # 2fps → 500ms steps


def test_sample_frames_empty_pool():
    assert m.sample_frames([], fps=1.0) == []


# ---- frames_to_base64 (pure) ----

def test_frames_to_base64_encodes_and_skips_unreadable(tmp_path):
    good = tmp_path / "frame-0001.png"
    good.write_bytes(b"PNGDATA")
    frames = [
        m.SampledFrame(index=0, time_ms=0, path=str(good)),
        m.SampledFrame(index=1, time_ms=1000, path=str(tmp_path / "missing.png")),
    ]
    out = m.frames_to_base64(frames)
    assert out == [base64.b64encode(b"PNGDATA").decode("ascii")]   # missing skipped


# ---- extract_frames (real ffmpeg round-trip) ----

def _have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


@pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg not on PATH")
def test_extract_frames_real_roundtrip(tmp_path):
    video = tmp_path / "src.mp4"
    # A 3-second synthetic clip; at 1fps extract_frames should yield ~3 PNGs.
    gen = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=3:size=128x128:rate=10",
         "-pix_fmt", "yuv420p", str(video)],
        capture_output=True, timeout=60)
    assert gen.returncode == 0 and video.exists()
    workdir = tmp_path / "work"
    workdir.mkdir()
    frames = m.extract_frames(str(video), str(workdir), fps=1.0, max_seconds=3.0)
    assert 2 <= len(frames) <= 4
    assert all(p.endswith(".png") and os.path.getsize(p) > 0 for p in frames)
    # end-to-end: sample + encode the real frames
    sampled = m.sample_frames(frames, fps=1.0, max_frames=2)
    b64 = m.frames_to_base64(sampled)
    assert len(b64) == 2 and all(len(x) > 0 for x in b64)


def test_extract_frames_bad_input_returns_empty(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    # Nonexistent input → ffmpeg fails (or is absent) → [], never raises.
    assert m.extract_frames(str(tmp_path / "nope.mp4"), str(workdir)) == []
