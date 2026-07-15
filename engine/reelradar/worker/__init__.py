"""Worker plane: the off-cloud sidecar that leases engine jobs and runs them
against a LOCAL warmed Chrome (distributed-workers PRD §2, BUILD-PLAN Phase 1).

PULL model for the JOB channel: this package never accepts inbound connections for
work. It long-polls the cloud dispatch over outbound HTTPS, runs ONE leased job in a
supervised child process that drives ``cli._run_session_loop`` (the exact CLI ``reelradar
run`` seam, via ``job_runner._execute_job``), and posts the result back. The engine writes
its leads + run_events to the same Store the panel reads.

Phase 6 adds two things that do NOT change the PULL job channel: (a) the sidecar
supervises the engine run as a KILLABLE child process so an operator/heartbeat halt
force-stops it mid-run (``job_child`` + the ``job_runner`` supervisor); (b) an OPTIONAL,
opt-in, loopback-ONLY control surface the local desktop shell polls for status and
commands (``control_surface``) — never the LAN, never the job channel.
"""
from __future__ import annotations

DEFAULT_HEARTBEAT_INTERVAL_SEC = 20

# Port the local desktop shell polls for status/commands. Loopback-only, opt-in
# (WorkerConfig.control_surface_enabled). Shared here so config.py and control_surface
# import the SAME constant (mirrors DEFAULT_HEARTBEAT_INTERVAL_SEC's sharing).
DEFAULT_CONTROL_SURFACE_PORT = 8799

# Result "kind" strings the job-child writes on an unrunnable job and the supervisor
# maps back to an exception class. A closed enum shared by both sides (job_runner
# writes/reads, job_child produces) so the two files never drift on a bare string.
JOB_RESULT_KIND_CAMPAIGN_NOT_FOUND = "campaign_not_found"
JOB_RESULT_KIND_SOUL_MISSING = "soul_missing"
JOB_RESULT_KIND_CAMPAIGN_MALFORMED = "campaign_malformed"
JOB_RESULT_KIND_ERROR = "error"
JOB_RESULT_KIND_SPEC_UNREADABLE = "spec_unreadable"
