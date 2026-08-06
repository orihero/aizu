"""v19 video-analysis persistence: seen_reels.video_analyzed/summary + the
sessions.video_analyses counter. Additive, so a fresh DB gets the columns from
SCHEMA and update_counters/mark_seen round-trip them."""
import os
import tempfile

from aizu.core.store import SessionCounters, Store


def _store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path)


def test_mark_seen_persists_video_columns():
    s = _store()
    s.mark_seen("c", "r1", relevant=True, video_analyzed=True,
                video_analysis_summary='{"brand":"acme"}')
    row = s._conn.execute(
        "SELECT video_analyzed, video_analysis_summary FROM seen_reels "
        "WHERE campaign_id='c' AND reel_id='r1'").fetchone()
    assert row[0] == 1
    assert row[1] == '{"brand":"acme"}'


def test_mark_seen_video_columns_default_null():
    s = _store()
    s.mark_seen("c", "r2", relevant=False)          # no video args
    row = s._conn.execute(
        "SELECT video_analyzed, video_analysis_summary FROM seen_reels "
        "WHERE reel_id='r2'").fetchone()
    assert (row[0], row[1]) == (None, None)


def test_update_counters_persists_video_analyses():
    s = _store()
    s.start_session("sess1", "c", platform="instagram")
    counters = SessionCounters(reels_seen=5, video_analyses=3, transcriptions=2)
    s.update_counters("sess1", counters)
    row = s._conn.execute(
        "SELECT video_analyses, transcriptions FROM sessions "
        "WHERE session_id='sess1'").fetchone()
    assert (row[0], row[1]) == (3, 2)
