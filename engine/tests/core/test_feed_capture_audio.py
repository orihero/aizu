"""core/feed.py — Reel.audio_path/transcript and FeedSource.capture_audio().

Parity test with the existing capture_frame/capture_frames contract: the base
FeedSource (and FakeFeed, which takes no override) must read the pre-baked
Reel.audio_path fixture field straight through, exactly like capture_frame
reads on_screen_frames. No CDP/CDPFeed involved — that override is Instagram's
own module and is covered separately (engines/instagram, out of this file's
scope).
"""
from __future__ import annotations

from aizu.core.feed import Comment, FakeFeed, FeedSource, Reel


def test_reel_defaults_no_audio_path_and_empty_transcript():
    reel = Reel(reel_id="r1")
    assert reel.audio_path is None
    assert reel.transcript == ""


def test_feedsource_default_capture_audio_reads_audio_path():
    reel = Reel(reel_id="r1", audio_path="/tmp/r1.wav")
    assert FeedSource().capture_audio(reel) == "/tmp/r1.wav"


def test_feedsource_default_capture_audio_is_none_when_unset():
    reel = Reel(reel_id="r1")
    assert FeedSource().capture_audio(reel) is None


def test_fakefeed_inherits_capture_audio_default_unchanged():
    # FakeFeed adds no override — this locks that fact so a future FakeFeed
    # change can't silently start ignoring audio_path.
    assert "capture_audio" not in FakeFeed.__dict__
    with_audio = Reel(reel_id="r1", audio_path="/fixtures/r1.wav")
    without_audio = Reel(reel_id="r2")
    feed = FakeFeed([with_audio, without_audio])
    assert feed.capture_audio(with_audio) == "/fixtures/r1.wav"
    assert feed.capture_audio(without_audio) is None


def test_reel_transcript_is_a_plain_mutable_field_not_shared_across_instances():
    # transcript defaults to "" (a str, not a mutable default pitfall like the
    # list fields below), but assert instances don't alias each other's state
    # regardless — cascade.py writes reel.transcript in place per-reel.
    a = Reel(reel_id="a")
    b = Reel(reel_id="b")
    a.transcript = "salom dunyo"
    assert b.transcript == ""


def test_reel_comments_field_unaffected_by_new_audio_transcript_fields():
    # Sanity: the two new fields were inserted without disturbing the existing
    # comments/on_screen_frames default_factory fields (a dataclass field-order
    # regression could otherwise silently break positional construction).
    c = Comment(comment_id="c1", username="u", text="hi")
    reel = Reel(reel_id="r1", comments=[c])
    assert reel.comments == [c]
    assert reel.on_screen_frames == []
