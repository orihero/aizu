"""Shared pytest configuration.

Keep the test suite from writing real log files: any code path that calls
``configure_logging`` (e.g. ``server.serve``) would otherwise create
``engine/logs/aizu.log``. Forcing the file sink off keeps test runs from
polluting the repo. Tests that need a file sink (test_logsetup) pass an explicit
``log_file=`` which takes precedence over this env var.
"""
import os

os.environ["AIZU_LOG_FILE"] = "off"
os.environ.setdefault("AIZU_LOG_COLOR", "never")

# Human-sim (core/human.py) defaults ON in production, but the existing CDP tests
# assert the pre-human-sim behaviour (a single wheel per scroll, no jitter sleeps)
# and would otherwise incur real inter-tick/settle sleeps. Default it OFF for the
# suite — the ``off`` path is byte-identical to the old behaviour. test_human.py
# constructs an explicitly-enabled HumanSim with an injected sleep spy, so it is
# unaffected by this default.
os.environ.setdefault("HUMAN_SIM", "off")
