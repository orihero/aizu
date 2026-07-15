"""Shared pytest configuration.

Keep the test suite from writing real log files: any code path that calls
``configure_logging`` (e.g. ``server.serve``) would otherwise create
``engine/logs/reelradar.log``. Forcing the file sink off keeps test runs from
polluting the repo. Tests that need a file sink (test_logsetup) pass an explicit
``log_file=`` which takes precedence over this env var.
"""
import os

os.environ["REELRADAR_LOG_FILE"] = "off"
os.environ.setdefault("REELRADAR_LOG_COLOR", "never")
