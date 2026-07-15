"""Atomic JSON writes for the worker's on-disk rendezvous files (spec/result).

One implementation shared by the supervisor (job_runner) and the child (job_child) so the
tmp-then-os.replace + 0600 pattern lives in exactly one place. Kept dependency-free (no
cli/engine imports) so job_child can use it on its earliest failure path without paying the
heavy engine import cost.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# Spec files carry soul_text + local paths; result files carry a summary. Neither is a
# credential, but there is no reason to expose them beyond the owner, so mirror the 0600
# discipline FernetFileBackend uses for the token.
_FILE_MODE = 0o600


def atomic_write_json(path: Path, data: dict) -> None:
    """Write ``data`` as JSON to ``path`` atomically at mode 0600: a sibling tmp file is
    opened 0600, written, then os.replace'd onto the final path (atomic on POSIX). A
    reader therefore never sees a torn or world-readable file, even if the process is
    killed mid-write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    blob = json.dumps(data, ensure_ascii=False).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    try:
        os.write(fd, blob)
    finally:
        os.close(fd)
    os.replace(tmp, path)
