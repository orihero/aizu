"""v16 platform execution-backend switch (store layer).

The superadmin switch that routes every campaign run to the in-process RunManager or
the distributed worker fleet. Defaults to in_process; an unknown/corrupt stored value
falls back to the safe default; the setter rejects unknown backends.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from aizu.core.store import (EXECUTION_DISTRIBUTED, EXECUTION_IN_PROCESS,
                                   Store)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(str(tmp_path / "s.db"))
    yield s
    s.close()


def test_defaults_to_in_process(store: Store):
    assert store.execution_backend() == EXECUTION_IN_PROCESS


def test_set_and_read_back_distributed(store: Store):
    store.set_execution_backend(EXECUTION_DISTRIBUTED, by="ops@x.io")
    assert store.execution_backend() == EXECUTION_DISTRIBUTED
    # Provenance recorded.
    assert store.get_platform_setting("execution_backend") == EXECUTION_DISTRIBUTED


def test_switch_is_idempotent_and_reversible(store: Store):
    store.set_execution_backend(EXECUTION_DISTRIBUTED)
    store.set_execution_backend(EXECUTION_IN_PROCESS)
    assert store.execution_backend() == EXECUTION_IN_PROCESS


def test_unknown_backend_is_rejected(store: Store):
    with pytest.raises(ValueError):
        store.set_execution_backend("magic")


def test_corrupt_stored_value_falls_back_to_default(store: Store):
    # A hand-corrupted row must never break run routing — degrade to in_process.
    store.set_platform_setting("execution_backend", "garbage")
    assert store.execution_backend() == EXECUTION_IN_PROCESS


def test_generic_setting_get_default(store: Store):
    assert store.get_platform_setting("nope", "fallback") == "fallback"
