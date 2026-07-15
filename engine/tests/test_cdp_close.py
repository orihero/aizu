"""CDPFeed.close() must release the Playwright driver (so a back-to-back session can
re-attach in the same process) WITHOUT closing the externally-warmed Chrome."""
from reelradar.engines.instagram.cdp import CDPFeed


def test_close_stops_driver_but_keeps_warmed_chrome():
    feed = CDPFeed()
    calls = []

    class PW:
        def stop(self):
            calls.append("pw.stop")

    class Browser:
        def close(self):
            calls.append("browser.close")

    feed._pw = PW()
    feed._browser = Browser()
    feed.close()

    assert calls == ["pw.stop"]            # driver released; warmed Chrome untouched
    assert feed._pw is None and feed._browser is None


def test_close_is_safe_when_never_attached():
    # _pw is None (attach never ran) → no error, idempotent.
    feed = CDPFeed()
    feed.close()
    feed.close()
