"""Feed source — the boundary the scraper lives behind.

The session loop talks only to this interface, so the CDP/interception layer can
land later (or be swapped for a fixture) with zero changes upstream. Per PRD §3:
real Chrome over CDP, network interception (not DOM scraping), read-only.

Implementations:
  - FakeFeed:  in-memory reels/comments for tests and dry runs (this module).
  - CDPFeed:   real Playwright-over-CDP attach + interception, in `cdp.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Optional


@dataclass
class Comment:
    comment_id: str
    username: str
    text: str
    lang: Optional[str] = None
    is_reply: bool = False


@dataclass
class Reel:
    reel_id: str
    caption: str = ""
    author: str = ""
    ocr_text: str = ""                                          # read by vision, if any
    on_screen_frames: list[str] = field(default_factory=list)  # base64 JPEGs for OCR
    comments: list[Comment] = field(default_factory=list)       # used by FakeFeed only
    audio_path: Optional[str] = None    # test-injectable fixture (FakeFeed reads this)
    transcript: str = ""                # STT output, Uzbek-only, gated (cascade.py writes it)
    # Real (ffmpeg-extracted) video frames for the video-analysis/fusion tier —
    # distinct from `on_screen_frames` (cheap in-page screenshots for the fast
    # relevance gate). `video_frames` are base64 JPEGs; `frame_timestamps` are the
    # parallel ms offsets (for a "FRAME n @ Xs" roster). `video_analysis` caches the
    # tier's structured verdict extras. See core/media.py + cascade.py.
    video_path: Optional[str] = None    # test-injectable fixture (FakeFeed reads this)
    video_frames: list[str] = field(default_factory=list)
    frame_timestamps: list[int] = field(default_factory=list)
    video_analysis: Optional[dict] = None


class FeedSource:
    """Interface. See FakeFeed for the contract semantics."""

    def walk(self) -> Iterator[Reel]:
        raise NotImplementedError

    def fetch_comments(self, reel_id: str, since_cursor: Optional[str]
                       ) -> tuple[list[Comment], Optional[str]]:
        raise NotImplementedError

    def capture_frame(self, reel: Reel) -> Optional[str]:
        """Return a base64 JPEG of a representative frame for OCR, or None."""
        return reel.on_screen_frames[0] if reel.on_screen_frames else None

    def capture_frames(self, reel: Reel, n: int = 3) -> list[str]:
        """Return up to n base64 frames for the vision tier (more on-screen text
        than a single frame). Default reads any pre-baked frames (FakeFeed/tests)."""
        return list(reel.on_screen_frames[:n]) if reel.on_screen_frames else []

    def capture_audio(self, reel: Reel) -> Optional[str]:
        """Return a filesystem path to a short audio/video file for `reel`, or
        None. Base/FakeFeed default: read the pre-baked test fixture field —
        same contract as capture_frame/capture_frames above."""
        return reel.audio_path

    def capture_video_frames(self, reel: Reel) -> list[str]:
        """Return real (ffmpeg-extracted) base64 video frames for the
        video-analysis tier, setting ``reel.frame_timestamps`` to the parallel ms
        offsets. Base/FakeFeed default: read the pre-baked ``video_frames`` fixture
        field — same contract as capture_frames/capture_audio above. The Instagram
        engine overrides this to download the reel and sample real frames."""
        return list(reel.video_frames)

    def healthy(self) -> bool:
        """Empty-interception canary: True if traffic is being captured."""
        return True

    def open_reel(self, reel: Reel) -> bool:
        """Open the reel full-screen (its permalink) so comments + engagement
        controls become available. No-op for in-memory feeds. Returns success."""
        return True

    # ---- engagement (opt-in; read-only feeds leave these as no-ops) ----
    def like_reel(self, reel: Reel) -> bool:
        """Like the active reel. Returns True on success. No-op by default."""
        return False

    def follow_author(self, reel: Reel) -> bool:
        """Follow the active reel's author. Returns True on success. No-op by default."""
        return False

    def save_reel(self, reel: Reel) -> bool:
        """Save/bookmark the active reel. Returns True on success. No-op by default."""
        return False

    def share_reel(self, reel: Reel, target: Optional[str] = None) -> bool:
        """Share the active reel via DM to ``target`` (an IG handle) if given, else
        to the first contact in the share list. Returns True on success. No-op by
        default — read-only/harvest feeds never share."""
        return False

    def detect_action_block(self) -> bool:
        """True if Instagram is showing an action-block/try-again-later screen."""
        return False


class FakeFeed(FeedSource):
    def __init__(self, reels: list[Reel]):
        self._reels = reels
        self._by_id = {r.reel_id: r for r in reels}

    def walk(self) -> Iterator[Reel]:
        yield from self._reels

    def fetch_comments(self, reel_id: str, since_cursor: Optional[str]
                       ) -> tuple[list[Comment], Optional[str]]:
        reel = self._by_id.get(reel_id)
        if not reel:
            return [], since_cursor
        # Simple cursor = count of comments already returned.
        start = int(since_cursor) if since_cursor else 0
        new = reel.comments[start:]
        return new, str(len(reel.comments))
