"""Tests for StateStore atomic JSON persistence."""

from pathlib import Path

import pytest

from appdaemon_store.exceptions import StoreError
from appdaemon_store.state_store import StateStore
from appdaemon_store.type_definitions import (
    AutoUpdateMode,
    RepoRecord,
    SeedOrigin,
    StoreState,
)


def test_load_missing_file(tmp_path: Path) -> None:
    """Return empty StoreState when state.json does not exist."""
    store = StateStore(tmp_path / "state.json")

    state = store.load()

    assert state.repositories == {}
    assert state.dismissed_migrations == []
    assert state.last_global_check is None


def test_save_and_reload(tmp_path: Path) -> None:
    """Persist StoreState atomically and reload matching fields."""
    state_file = tmp_path / "nested" / "state.json"
    store = StateStore(state_file)
    record = RepoRecord(
        id="acme/app",
        owner="acme",
        repo="app",
        url="https://github.com/acme/app",
        auto_update=AutoUpdateMode.DISABLED,
        installed_version="v1.0.0",
        installed_folders=["app"],
        requirements=["requests>=2.31"],
        python_modules=["app.py"],
        origin=SeedOrigin.CONFIG,
    )
    state = StoreState(
        repositories={"acme/app": record},
        dismissed_migrations=["unmanaged_dir:foo"],
        last_global_check="2026-01-01T00:00:00+00:00",
    )

    store.save(state)
    loaded = store.load()

    assert loaded == state
    leftover_tmps = list(state_file.parent.glob("*.tmp"))
    assert leftover_tmps == []


def test_load_corrupted_raises(tmp_path: Path) -> None:
    """Raise StoreError when state.json holds malformed JSON."""
    state_file = tmp_path / "state.json"
    state_file.write_text("{broken json", encoding="utf-8")
    store = StateStore(state_file)

    with pytest.raises(StoreError):
        store.load()
