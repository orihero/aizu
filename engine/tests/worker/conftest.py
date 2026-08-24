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


@pytest.fixture(autouse=True)
def worker_secret_key(monkeypatch) -> str:
    """This box's environment, as a healthy worker PC actually has it. Returns the key.

    Autouse because `Sidecar.run()` now runs the real launch preflight before register,
    and two of its FATAL checks probe real MECHANISMS rather than reading a config field:

      - ``token_persistence`` does a genuine TokenStore save/load/clear, which the default
        Fernet file backend cannot do without ``AIZU_SECRET_KEY`` (that IS F9.1).
      - ``llm_backend`` uses the exact predicate ``cli._build_run_io`` raises on.

    Without both, every Sidecar in the suite would park instead of leasing — the checks
    would be right and the tests would be testing a broken box. ``AIZU_TOKEN_BACKEND`` is
    cleared as well so a developer's ``keyring`` setting can never make the suite touch a
    real OS keychain (whose health probe can block on an unlock prompt)."""
    key = SecretCipher.generate_key()
    monkeypatch.setenv("AIZU_SECRET_KEY", key)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.delenv("AIZU_TOKEN_BACKEND", raising=False)
    return key


@pytest.fixture
def cipher(worker_secret_key: str) -> SecretCipher:
    """A real Fernet cipher — THE box's cipher, i.e. keyed on the same AIZU_SECRET_KEY an
    env-constructed ``TokenStore`` resolves.

    It used to be an unrelated freshly-generated key. That mattered once the preflight
    landed: `token_persistence` builds its own env-keyed TokenStore, so a token a test had
    seeded under a DIFFERENT cipher read back as an undecryptable blob, and the probe's
    save/load/clear round-trip then overwrote and deleted it — the test's `tokens.load()`
    came back None for reasons that had nothing to do with the behaviour under test. One
    key per box is also simply what a real worker has."""
    return SecretCipher(worker_secret_key)


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
        # A capability is now REQUIRED for a healthy box: `check_capabilities` is fatal
        # (F9.2 — a box advertising nothing can never be leased to, and used to flip the
        # tenant's readiness banner to a false ready:true). youtube specifically, because
        # it is API-only: every CDP check then SKIPS, so the whole suite stays network-free
        # and the preflight costs ~1ms per Sidecar instead of ~9.5s.
        capabilities=((None, "youtube", None),),
    )
