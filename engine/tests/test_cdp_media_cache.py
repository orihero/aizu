"""Gap 3: capture_audio (STT tier) and capture_video_frames (video-analysis
tier) must share ONE download of a reel's CDN mp4, not each fetch it
independently. Mirrors soliq's cdpDataSource.ts memoized-download design,
adapted to aizu's synchronous, per-reel-then-move-on call pattern (no
concurrency to guard, per cascade.py's gate_reel: STT then video-analysis fire
back-to-back for the SAME reel before the walk moves on).

No browser/network/ffmpeg touched — `download_media`, `extract_audio_wav`, and
`extract_frames` are monkeypatched at the cdp module import site.
"""
import os

import pytest

from aizu.core.feed import Reel
from aizu.engines.instagram import cdp as cdp_module
from aizu.engines.instagram.cdp import CDPFeed


class FakeCookies:
    def cookies(self):
        return [{"name": "sessionid", "value": "abc"}]


class FakePage:
    def __init__(self):
        self.context = FakeCookies()

    def evaluate(self, _expr):
        return "UA/1.0"


def _feed_with_reel(reel_id="r1", url="https://cdn.example.com/r1.mp4"):
    feed = CDPFeed()
    feed._page = FakePage()
    feed._code_to_media_url[reel_id] = url
    return feed


@pytest.fixture
def counting_download(monkeypatch):
    """Records every (url, dest_path) pair and writes nothing — the download
    itself is faked, only the call count/args matter here."""
    calls = []

    def fake_download_media(url, *, dest_path, **_kw):
        calls.append((url, dest_path))
        return True

    monkeypatch.setattr(cdp_module, "download_media", fake_download_media)
    return calls


@pytest.fixture(autouse=True)
def _fake_ffmpeg_steps(monkeypatch):
    """Neither ffmpeg call needs to actually run for this test — only that the
    mp4 path handed to them came from the shared cache."""
    monkeypatch.setattr(cdp_module, "extract_audio_wav", lambda mp4, wav: True)
    monkeypatch.setattr(cdp_module, "extract_frames",
                        lambda mp4, workdir, **kw: [])


def test_stt_then_video_tier_download_the_mp4_only_once(counting_download):
    feed = _feed_with_reel()
    reel = Reel("r1", caption="c")

    wav_path = feed.capture_audio(reel)
    frames = feed.capture_video_frames(reel)

    assert wav_path is not None
    assert frames == []                      # faked extract_frames returns none
    assert len(counting_download) == 1        # ONE network fetch for both tiers
    os.unlink(wav_path)


def test_video_tier_first_then_stt_still_downloads_once(counting_download):
    """Order-independent: whichever tier runs first primes the cache for the
    other (cascade.py always runs STT before video-analysis, but the cache
    itself must not assume that ordering)."""
    feed = _feed_with_reel()
    reel = Reel("r1", caption="c")

    frames = feed.capture_video_frames(reel)
    wav_path = feed.capture_audio(reel)

    assert frames == []
    assert wav_path is not None
    assert len(counting_download) == 1
    os.unlink(wav_path)


def test_different_reel_triggers_a_fresh_download(counting_download):
    """The cache must not leak a stale mp4 across reels — a different
    reel_id evicts the previous entry and re-downloads."""
    feed = _feed_with_reel("r1", "https://cdn.example.com/r1.mp4")
    feed._code_to_media_url["r2"] = "https://cdn.example.com/r2.mp4"

    feed.capture_video_frames(Reel("r1", caption="c"))
    feed.capture_video_frames(Reel("r2", caption="c"))

    assert len(counting_download) == 2
    assert counting_download[0][0] == "https://cdn.example.com/r1.mp4"
    assert counting_download[1][0] == "https://cdn.example.com/r2.mp4"
    # evicting r1's cache must have swept its workdir, not just forgotten it
    assert not os.path.exists(os.path.dirname(counting_download[0][1]))


def test_failed_download_is_cached_too_not_retried(counting_download, monkeypatch):
    """A doomed download (e.g. an expired signed URL) must not be retried by
    the second tier in the same gate_reel() call — it's cached as a miss."""
    monkeypatch.setattr(cdp_module, "download_media", lambda url, *, dest_path, **kw: False)
    feed = _feed_with_reel()
    reel = Reel("r1", caption="c")

    assert feed.capture_audio(reel) is None
    assert feed.capture_video_frames(reel) == []


def test_close_sweeps_the_cached_mp4_workdir(counting_download):
    feed = _feed_with_reel()
    reel = Reel("r1", caption="c")
    feed.capture_video_frames(reel)
    cached_dir = feed._media_cache_workdir.path
    assert os.path.isdir(cached_dir)

    feed.close()

    assert not os.path.exists(cached_dir)
    assert feed._media_cache_reel_id is None
    assert feed._media_cache_mp4 is None
