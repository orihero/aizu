"""Media extraction — download a post's video, sample real frames, extract audio.

Ported from soliq's ``pipeline/`` (``audio.ts``/``frames.ts``) and generalized
from the Instagram ``capture_audio`` prototype so any engine can turn a reel's
CDN video into (a) a 16 kHz mono WAV for STT and (b) evenly-sampled, timestamped
frames for the video-analysis / fusion tier.

Everything here follows the never-wedge contract every ``capture_*`` in
``core/cdp.py`` uses: no function raises into the run loop — a failure degrades to
``False`` / ``[]`` / ``None`` and logs. Audio extraction reuses
``transcribe.extract_audio_wav`` unchanged (aizu's 16 kHz mono WAV is already the
right STT input — soliq's mp3 was for a cloud size budget aizu doesn't have).
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import List, Optional

from .logsetup import get_logger

log = get_logger(__name__)

try:  # httpx is a runtime dep but keep import soft for offline/test use
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore

# Sanity floor on a downloaded media body before paying for ffmpeg — rejects an
# obvious error page / truncated / expired-signature response cheaply. Mirrors the
# Instagram prototype's MIN_MEDIA_BYTES; ffmpeg still gets the final say.
MIN_MEDIA_BYTES = 20_000


class MediaWorkdir:
    """A per-job temp directory, removed on exit no matter what (soliq's per-job
    ``workDir`` pattern). Use as a context manager:

        with MediaWorkdir() as wd:
            ...write into wd.path...
        # wd.path is gone here, even on exception

    ``__exit__`` never raises — cleanup is best-effort (``ignore_errors=True``)."""

    def __init__(self, prefix: str = "aizu-media-"):
        self._prefix = prefix
        self.path: str = ""

    def __enter__(self) -> "MediaWorkdir":
        self.path = tempfile.mkdtemp(prefix=self._prefix)
        return self

    def __exit__(self, *exc) -> None:
        if self.path:
            shutil.rmtree(self.path, ignore_errors=True)
        return None


def download_media(url: str, *, dest_path: str, cookies: str = "",
                   user_agent: str = "", referer: str = "",
                   timeout_s: float = 15.0,
                   min_bytes: int = MIN_MEDIA_BYTES) -> bool:
    """GET ``url`` (with optional cookie/UA/referer headers) into ``dest_path``.

    Generalizes the Instagram prototype's httpx block. Never raises — returns
    False on any failure (httpx missing, non-200, body below ``min_bytes``,
    network error, timeout). A too-small body is treated as failure (error page /
    expired signature), same as the prototype."""
    if httpx is None:
        log.debug("download_media: httpx unavailable")
        return False
    headers = {}
    if cookies:
        headers["Cookie"] = cookies
    if user_agent:
        headers["User-Agent"] = user_agent
    if referer:
        headers["Referer"] = referer
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout_s,
                         follow_redirects=True)
        if resp.status_code != 200 or len(resp.content) < min_bytes:
            log.debug("download_media rejected · status=%s bytes=%d",
                      resp.status_code, len(resp.content))
            return False
        with open(dest_path, "wb") as f:
            f.write(resp.content)
        return os.path.exists(dest_path) and os.path.getsize(dest_path) >= min_bytes
    except Exception as e:  # noqa: BLE001 — never raise into the run loop
        log.debug("download_media failed · url=%s err=%s", url[:80], e)
        return False


def extract_frames(video_path: str, workdir: str, fps: float = 1.0,
                   max_seconds: Optional[float] = 60.0,
                   timeout_s: float = 60.0) -> List[str]:
    """Shell ffmpeg to write ``frame-%04d.png`` at ``fps`` into ``workdir``.

    ``max_seconds`` caps how much of the video is decoded (``-t``) so a long clip
    can't blow the caller's per-reel wall-clock budget — a cap soliq lacks because
    its inputs are pre-vetted-length. Mirrors ``extract_audio_wav``'s subprocess
    idiom (capture_output, timeout, returncode check). Never raises — returns the
    sorted list of produced PNG paths, or ``[]`` on any failure."""
    pattern = os.path.join(workdir, "frame-%04d.png")
    cmd = ["ffmpeg", "-y"]
    if max_seconds and max_seconds > 0:
        cmd += ["-t", str(max_seconds)]
    cmd += ["-i", video_path, "-vf", f"fps={fps}", pattern]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s)
        if proc.returncode != 0:
            log.warning("ffmpeg frame extract nonzero exit · video=%s rc=%d",
                        video_path, proc.returncode)
            return []
    except Exception as e:  # noqa: BLE001
        log.warning("ffmpeg frame extract failed · video=%s err=%s", video_path, e)
        return []
    frames = sorted(
        os.path.join(workdir, n) for n in os.listdir(workdir)
        if n.startswith("frame-") and n.endswith(".png"))
    return frames


@dataclass
class SampledFrame:
    index: int      # position in the full extracted pool
    time_ms: int    # timestamp offset from the video start
    path: str


def sample_frames(paths: List[str], fps: float,
                  max_frames: int = 8) -> List[SampledFrame]:
    """Evenly pick up to ``max_frames`` frames from the extracted pool, tagging
    each with its ms offset (``index / fps * 1000``). Direct translation of soliq's
    ``sampleFrames``: an even step across the whole pool so the picks span the clip
    rather than clustering at the start."""
    if not paths:
        return []
    n = len(paths)
    m = max(1, min(max_frames, n))
    if m >= n:
        chosen = list(range(n))
    else:
        step = n / m
        chosen = sorted({min(n - 1, int(i * step)) for i in range(m)})
    out: List[SampledFrame] = []
    for idx in chosen:
        time_ms = int(round(idx / fps * 1000)) if fps > 0 else 0
        out.append(SampledFrame(index=idx, time_ms=time_ms, path=paths[idx]))
    return out


def frames_to_base64(frames: List[SampledFrame]) -> List[str]:
    """Read + base64-encode each sampled frame (order preserved). A frame that
    can't be read is skipped, never fatal. Direct translation of soliq's
    ``toImageInputs`` (minus the mime wrapper — aizu's vision call adds the
    ``data:image/jpeg;base64,`` prefix itself)."""
    out: List[str] = []
    for f in frames:
        try:
            with open(f.path, "rb") as fh:
                out.append(base64.b64encode(fh.read()).decode("ascii"))
        except Exception as e:  # noqa: BLE001 — a missing/unreadable frame is not fatal
            log.debug("frames_to_base64 skip · path=%s err=%s", f.path, e)
    return out
