"""Managed Chrome lifecycle (chrome_manager.py) — all with fakes, no real Chrome."""
from __future__ import annotations

from pathlib import Path

import pytest

from reelradar.worker.chrome_manager import (ChromeBinaryNotFoundError,
                                             ChromeLaunchError, ChromeManager,
                                             ChromeManagerConfig, ChromeUnhealthyError,
                                             _build_launch_args, resolve_chrome_binary)


class FakeHandle:
    def __init__(self):
        self.terminated = False
        self.killed = False
        self._alive = True

    @property
    def pid(self):
        return 999

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False


def _cfg(tmp_path, **kw) -> ChromeManagerConfig:
    base = dict(cdp_url="http://127.0.0.1:9333", user_data_dir=tmp_path / "profile")
    base.update(kw)
    return ChromeManagerConfig(**base)


def _mgr(cfg, *, http, cdp_seq, **kw) -> tuple:
    """cdp_seq is a list of successive cdp_prober return values."""
    launched = {"argv": None, "handle": None}
    seq = list(cdp_seq)

    def prober(url, timeout):
        return seq.pop(0) if seq else (cdp_seq[-1] if cdp_seq else False)

    def launcher(argv):
        launched["argv"] = argv
        launched["handle"] = FakeHandle()
        return launched["handle"]

    mgr = ChromeManager(cfg, launcher=launcher, cdp_prober=prober,
                        http_probe=lambda u, t: http, sleep=lambda s: None,
                        path_exists=lambda p: True, **kw)
    return mgr, launched


def test_reconnects_when_cdp_already_attaches(tmp_path):
    mgr, launched = _mgr(_cfg(tmp_path), http=True, cdp_seq=[True])
    status = mgr.ensure_running()
    assert status.state == "attached_existing"
    assert status.launched_by_us is False
    assert launched["argv"] is None            # never launched a second Chrome


def test_launches_when_nothing_listening(tmp_path):
    # http probe fails → launch → cdp attaches after one poll.
    mgr, launched = _mgr(_cfg(tmp_path, chrome_binary="/fake/chrome"),
                         http=False, cdp_seq=[False, True])
    status = mgr.ensure_running()
    assert status.state == "launched" and status.launched_by_us is True
    assert "--remote-debugging-port=9333" in launched["argv"]
    assert any("--user-data-dir=" in a for a in launched["argv"])


def test_stale_unhealthy_chrome_raises_and_never_launches(tmp_path):
    mgr, launched = _mgr(_cfg(tmp_path), http=True, cdp_seq=[False])
    with pytest.raises(ChromeUnhealthyError):
        mgr.ensure_running()
    assert launched["argv"] is None            # must not kill/relaunch someone else's


def test_launch_timeout_raises(tmp_path):
    mgr, _ = _mgr(_cfg(tmp_path, chrome_binary="/fake/chrome", launch_timeout_sec=1.0,
                       launch_poll_interval_sec=0.5),
                  http=False, cdp_seq=[False])
    with pytest.raises(ChromeLaunchError):
        mgr.ensure_running()


def test_stop_is_noop_when_only_attached(tmp_path):
    mgr, _ = _mgr(_cfg(tmp_path), http=True, cdp_seq=[True])
    mgr.ensure_running()
    mgr.stop()  # must not raise; nothing we launched


def test_stop_terminates_process_we_launched(tmp_path):
    mgr, launched = _mgr(_cfg(tmp_path, chrome_binary="/fake/chrome"),
                         http=False, cdp_seq=[True])
    mgr.ensure_running()
    mgr.stop()
    assert launched["handle"].terminated is True


def test_config_rejects_empty_profile(tmp_path):
    with pytest.raises(ValueError):
        ChromeManagerConfig(cdp_url="http://127.0.0.1:9333", user_data_dir=Path(""))


def test_config_rejects_unparseable_port():
    with pytest.raises(ValueError):
        ChromeManagerConfig(cdp_url="not-a-url", user_data_dir=Path("/tmp/p"))


def test_build_launch_args_includes_flags_and_extras(tmp_path):
    cfg = _cfg(tmp_path, extra_flags=("--proxy-server=http://p:8080",))
    args = _build_launch_args(cfg, "/bin/chrome")
    assert args[0] == "/bin/chrome"
    assert "--no-first-run" in args and "--no-default-browser-check" in args
    assert args[-1] == "--proxy-server=http://p:8080"


def test_resolve_binary_override_wins(tmp_path):
    cfg = _cfg(tmp_path, chrome_binary="/opt/chrome")
    assert resolve_chrome_binary(cfg, os_name="Linux",
                                 path_exists=lambda p: True) == "/opt/chrome"


def test_resolve_binary_prefers_playwright_cft(tmp_path):
    cfg = _cfg(tmp_path)
    got = resolve_chrome_binary(cfg, os_name="Darwin", path_exists=lambda p: True,
                                playwright_executable_path=lambda: "/pw/cft")
    assert got == "/pw/cft"


def test_resolve_binary_falls_back_to_system(tmp_path):
    cfg = _cfg(tmp_path)
    got = resolve_chrome_binary(cfg, os_name="Darwin", path_exists=lambda p: True,
                                playwright_executable_path=lambda: None)
    assert "Google Chrome" in got


def test_resolve_binary_raises_when_nothing_exists(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ChromeBinaryNotFoundError):
        resolve_chrome_binary(cfg, os_name="Linux", path_exists=lambda p: False)


def test_focus_dispatches_by_os(tmp_path):
    calls = {"mac": 0}

    def mac():
        calls["mac"] += 1
        return True
    mgr = ChromeManager(_cfg(tmp_path), os_name=lambda: "Darwin", focus_macos=mac)
    assert mgr.focus_window() is True and calls["mac"] == 1


def test_focus_unsupported_os_returns_false(tmp_path):
    mgr = ChromeManager(_cfg(tmp_path), os_name=lambda: "Plan9")
    assert mgr.focus_window() is False   # logged no-op, never raises
