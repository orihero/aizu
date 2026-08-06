"""Fixtures for the worker pull-loop tests (BUILD-PLAN Phase 1)."""
from __future__ import annotations

from pathlib import Path

import pytest

from aizu.secrets import SecretCipher
from aizu.worker.config import WorkerConfig


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "worker-state"
    d.mkdir()
    return d


@pytest.fixture
def cipher() -> SecretCipher:
    """A real Fernet cipher from a freshly generated key — no env mutation."""
    return SecretCipher(SecretCipher.generate_key())


@pytest.fixture
def cfg(tmp_path: Path, state_dir: Path) -> WorkerConfig:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    return WorkerConfig(
        dispatch_base_url="http://stub.local",
        cfg_dir=cfg_dir,
        db_path=":memory:",
        state_dir=state_dir,
        heartbeat_interval_sec=1,
        lease_poll_timeout_sec=1,
    )
