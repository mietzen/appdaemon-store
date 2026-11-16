"""Tests for MigrationService legacy and directory detection/execution."""

from pathlib import Path

from appdaemon_store.migration import MigrationService
from appdaemon_store.state_store import StateStore
from appdaemon_store.type_definitions import MigrationType, RepoRecord, StoreState


def test_detect_legacy_path(tmp_path: Path) -> None:
    """Detect legacy /homeassistant/appdaemon/apps configured in appdaemon.yaml."""
    ha_root = tmp_path / "homeassistant"
    legacy_dir = ha_root / "appdaemon" / "apps"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "apps.yaml").write_text("hello: {}\n", encoding="utf-8")
    (legacy_dir / "hello_app").mkdir()

    addon_root = tmp_path / "addon_configs" / "a0d7b954_appdaemon"
    apps_dir = addon_root / "apps"
    apps_dir.mkdir(parents=True)
    yaml_path = addon_root / "appdaemon.yaml"
    yaml_path.write_text(
        "appdaemon:\n  app_dir: /homeassistant/appdaemon/apps\n",
        encoding="utf-8",
    )

    svc = MigrationService(ha_root=ha_root, addon_root=addon_root)
    candidates = svc.detect_all(apps_dir, yaml_path, StoreState())

    assert len(candidates) == 1
    assert candidates[0].migration_type == MigrationType.LEGACY_PATH
    assert candidates[0].source_path == str(legacy_dir)
    assert candidates[0].target_path == str(apps_dir)
    assert candidates[0].items == ["apps.yaml", "hello_app"]


def test_apply_legacy_migration(tmp_path: Path) -> None:
    """Copy legacy apps and update appdaemon.yaml app_dir to /config/apps."""
    ha_root = tmp_path / "homeassistant"
    legacy_dir = ha_root / "appdaemon" / "apps"
    (legacy_dir / "my_legacy").mkdir(parents=True)
    (legacy_dir / "my_legacy" / "legacy.py").write_text("x = 1\n", encoding="utf-8")
    (legacy_dir / "apps.yaml").write_text("app: {}\n", encoding="utf-8")

    addon_root = tmp_path / "addon_configs" / "a0d7b954_appdaemon"
    apps_dir = addon_root / "apps"
    apps_dir.mkdir(parents=True)
    yaml_path = addon_root / "appdaemon.yaml"
    yaml_path.write_text(
        "appdaemon:\n  app_dir: /homeassistant/appdaemon/apps\n  threads: 4\n",
        encoding="utf-8",
    )

    svc = MigrationService(ha_root=ha_root, addon_root=addon_root)
    state = StoreState()
    candidates = svc.detect_all(apps_dir, yaml_path, state)
    summary = svc.apply_migration(candidates[0].id, apps_dir, yaml_path, state)

    assert "Copied 2 item(s)" in summary
    assert (apps_dir / "apps.yaml").read_text(encoding="utf-8") == "app: {}\n"
    assert (apps_dir / "my_legacy" / "legacy.py").read_text(
        encoding="utf-8"
    ) == "x = 1\n"
    assert "app_dir: /config/apps" in yaml_path.read_text(encoding="utf-8")
    remaining = svc.detect_all(apps_dir, yaml_path, state)
    assert all(c.migration_type != MigrationType.LEGACY_PATH for c in remaining)


def test_detect_dup_and_unmanaged(tmp_path: Path) -> None:
    """Classify unmanaged dirs as DUPLICATE_APP when modules overlap, else UNMANAGED_DIR."""
    apps_dir = tmp_path / "apps"
    (apps_dir / "fritz_thermostat_offset").mkdir(parents=True)
    (apps_dir / "fritz_thermostat_offset" / "fritz_thermostat_offset.py").write_text(
        "",
        encoding="utf-8",
    )
    (apps_dir / "fritz-advanced-thermostat-hacs").mkdir()
    (
        apps_dir / "fritz-advanced-thermostat-hacs" / "fritz_thermostat_offset.py"
    ).write_text(
        "",
        encoding="utf-8",
    )
    (apps_dir / "custom_local_app").mkdir()
    (apps_dir / "custom_local_app" / "local_helper.py").write_text("", encoding="utf-8")

    state = StoreState(
        repositories={
            "mietzen/fritz-advanced-thermostat-hacs": RepoRecord(
                id="mietzen/fritz-advanced-thermostat-hacs",
                owner="mietzen",
                repo="fritz-advanced-thermostat-hacs",
                url="https://github.com/mietzen/fritz-advanced-thermostat-hacs",
                installed_folders=["fritz_thermostat_offset"],
                python_modules=["fritz_thermostat_offset.py"],
            )
        }
    )

    svc = MigrationService()
    candidates = svc.detect_all(apps_dir, tmp_path / "missing.yaml", state)

    assert len(candidates) == 2
    by_type = {c.migration_type: c for c in candidates}
    dup = by_type[MigrationType.DUPLICATE_APP]
    assert dup.related_repo == "mietzen/fritz-advanced-thermostat-hacs"
    assert dup.items == ["fritz_thermostat_offset.py"]
    unmanaged = by_type[MigrationType.UNMANAGED_DIR]
    assert unmanaged.items == ["local_helper.py"]

    svc.apply_migration(dup.id, apps_dir, tmp_path / "missing.yaml", state)
    assert not (apps_dir / "fritz-advanced-thermostat-hacs").exists()


def test_dismiss_persists(tmp_path: Path) -> None:
    """Persist dismissed candidate IDs so they do not reappear."""
    apps_dir = tmp_path / "apps"
    (apps_dir / "manual_app").mkdir(parents=True)
    (apps_dir / "manual_app" / "manual.py").write_text("", encoding="utf-8")

    state_store = StateStore(tmp_path / "state.json")
    svc = MigrationService(state_store=state_store)
    state = state_store.load()
    candidates = svc.detect_all(apps_dir, tmp_path / "missing.yaml", state)
    assert len(candidates) == 1

    svc.dismiss(candidates[0].id, state)
    reloaded = state_store.load()
    assert svc.detect_all(apps_dir, tmp_path / "missing.yaml", reloaded) == []
