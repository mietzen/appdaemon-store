"""Tests for path_resolver driver module."""

from pathlib import Path

import pytest
import yaml

from appdaemon_store.const import DEFAULT_HAOS_APPS_DIR
from appdaemon_store.exceptions import MigrationError
from appdaemon_store.path_resolver import (
    get_app_dir,
    map_legacy_path,
    read_appdaemon_yaml,
    resolve_apps_dir,
    set_app_dir_yaml,
)
from appdaemon_store.type_definitions import ConfigSource


def test_resolve_explicit(tmp_path: Path) -> None:
    """Return explicit path when provided regardless of appdaemon.yaml."""
    yaml_path = tmp_path / "appdaemon.yaml"
    yaml_path.write_text("appdaemon:\n  app_dir: /config/apps\n", encoding="utf-8")
    custom = str(tmp_path / "custom_apps")

    resolved, source = resolve_apps_dir(custom, yaml_path)

    assert resolved == Path(custom)
    assert source == ConfigSource.EXPLICIT


def test_resolve_yaml_config_pfx(tmp_path: Path) -> None:
    """Translate /config/ prefix in appdaemon.yaml to addon_root."""
    yaml_path = tmp_path / "appdaemon.yaml"
    yaml_path.write_text("appdaemon:\n  app_dir: /config/apps\n", encoding="utf-8")
    addon_root = tmp_path / "addon_configs" / "a0d7b954_appdaemon"

    resolved, source = resolve_apps_dir(None, yaml_path, addon_root=addon_root)

    assert resolved == addon_root / "apps"
    assert source == ConfigSource.APPDAEMON_YAML


def test_resolve_yaml_ha_root(tmp_path: Path) -> None:
    """Keep /homeassistant/ path when ha_root exists on disk."""
    ha_root = tmp_path / "homeassistant"
    ha_root.mkdir()
    yaml_path = tmp_path / "appdaemon.yaml"
    yaml_path.write_text(
        "appdaemon:\n  app_dir: /homeassistant/appdaemon/apps\n",
        encoding="utf-8",
    )

    resolved, source = resolve_apps_dir(
        None,
        yaml_path,
        ha_root=ha_root,
        cfg_root=tmp_path / "missing_cfg",
    )

    assert resolved == ha_root / "appdaemon" / "apps"
    assert source == ConfigSource.APPDAEMON_YAML


def test_resolve_yaml_ha_fallback(tmp_path: Path) -> None:
    """Map /homeassistant/ to cfg_root when ha_root is absent and cfg_root exists."""
    cfg_root = tmp_path / "config"
    cfg_root.mkdir()
    yaml_path = tmp_path / "appdaemon.yaml"
    yaml_path.write_text(
        "appdaemon:\n  app_dir: /homeassistant/appdaemon/apps\n",
        encoding="utf-8",
    )

    resolved, source = resolve_apps_dir(
        None,
        yaml_path,
        ha_root=tmp_path / "missing_ha",
        cfg_root=cfg_root,
    )

    assert resolved == cfg_root / "appdaemon" / "apps"
    assert source == ConfigSource.APPDAEMON_YAML


def test_resolve_yaml_custom(tmp_path: Path) -> None:
    """Return custom non-prefixed app_dir directly from appdaemon.yaml."""
    yaml_path = tmp_path / "appdaemon.yaml"
    yaml_path.write_text(
        "appdaemon:\n  app_dir: /srv/appdaemon/apps\n", encoding="utf-8"
    )

    resolved, source = resolve_apps_dir(None, yaml_path)

    assert resolved == Path("/srv/appdaemon/apps")
    assert source == ConfigSource.APPDAEMON_YAML


def test_resolve_default_missing(tmp_path: Path) -> None:
    """Fallback to DEFAULT_HAOS_APPS_DIR when yaml file does not exist."""
    resolved, source = resolve_apps_dir(None, tmp_path / "missing.yaml")

    assert resolved == Path(DEFAULT_HAOS_APPS_DIR)
    assert source == ConfigSource.DEFAULT


def test_read_invalid_yaml(tmp_path: Path) -> None:
    """Return None when appdaemon.yaml contains invalid YAML."""
    yaml_path = tmp_path / "appdaemon.yaml"
    yaml_path.write_text("appdaemon: [\n", encoding="utf-8")

    assert read_appdaemon_yaml(yaml_path) is None
    assert get_app_dir(yaml_path) is None


def test_set_app_dir_preserves(tmp_path: Path) -> None:
    """Update appdaemon.app_dir while preserving unrelated YAML keys."""
    yaml_path = tmp_path / "appdaemon.yaml"
    yaml_path.write_text(
        "secrets: /config/secrets.yaml\n"
        "appdaemon:\n"
        "  latitude: 52.5\n"
        "  app_dir: /homeassistant/appdaemon/apps\n"
        "http:\n"
        "  url: http://127.0.0.1:5050\n",
        encoding="utf-8",
    )

    set_app_dir_yaml(yaml_path, "/config/apps")
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))

    assert data["secrets"] == "/config/secrets.yaml"
    assert data["appdaemon"]["latitude"] == 52.5
    assert data["appdaemon"]["app_dir"] == "/config/apps"
    assert data["http"]["url"] == "http://127.0.0.1:5050"


def test_set_app_dir_bad_type(tmp_path: Path) -> None:
    """Raise MigrationError when appdaemon.yaml root is not a mapping."""
    yaml_path = tmp_path / "appdaemon.yaml"
    yaml_path.write_text("- item\n", encoding="utf-8")

    with pytest.raises(MigrationError):
        set_app_dir_yaml(yaml_path, "/config/apps")


def test_map_legacy_paths(tmp_path: Path) -> None:
    """Translate legacy /homeassistant and /config paths to accessible roots."""
    ha_root = tmp_path / "homeassistant"
    cfg_root = tmp_path / "config"
    addon_root = tmp_path / "addon"
    (cfg_root / "appdaemon" / "apps").mkdir(parents=True)

    mapped_ha = map_legacy_path(
        "/homeassistant/appdaemon/apps",
        ha_root=tmp_path / "missing_ha",
        cfg_root=cfg_root,
        addon_root=addon_root,
    )
    mapped_cfg = map_legacy_path(
        "/config/appdaemon/apps",
        ha_root=ha_root,
        cfg_root=cfg_root,
        addon_root=addon_root,
    )

    assert mapped_ha == cfg_root / "appdaemon" / "apps"
    assert mapped_cfg == cfg_root / "appdaemon" / "apps"


def test_yaml_custom_tags(tmp_path: Path) -> None:
    """Preserve custom YAML tags such as !env_var and !secret on read and write."""
    yaml_path = tmp_path / "appdaemon.yaml"
    yaml_path.write_text(
        "---\n"
        "# Main AppDaemon config\n"
        "secrets: /homeassistant/secrets.yaml\n"
        "appdaemon:\n"
        "  latitude: 0\n"
        "  app_dir: /homeassistant/appdaemon/apps  # legacy HACS dir\n"
        "  plugins:\n"
        "    HASS:\n"
        "      type: hass\n"
        "      token: !env_var SUPERVISOR_TOKEN\n"
        "    MQTT:\n"
        "      password: !secret mqtt_pass\n",
        encoding="utf-8",
    )

    assert get_app_dir(yaml_path) == "/homeassistant/appdaemon/apps"

    set_app_dir_yaml(yaml_path, "/config/apps")
    updated_text = yaml_path.read_text(encoding="utf-8")

    assert get_app_dir(yaml_path) == "/config/apps"
    assert "token: !env_var SUPERVISOR_TOKEN" in updated_text
    assert "password: !secret mqtt_pass" in updated_text
    assert "# Main AppDaemon config" in updated_text


def test_yaml_insert_and_append(tmp_path: Path) -> None:
    """Insert app_dir under appdaemon or create section cleanly when missing."""
    yaml_no_key = tmp_path / "no_key.yaml"
    yaml_no_key.write_text(
        "appdaemon:\n"
        "  latitude: 0\n"
        "  plugins:\n"
        "    HASS:\n"
        "      token: !env_var SUPERVISOR_TOKEN\n",
        encoding="utf-8",
    )
    set_app_dir_yaml(yaml_no_key, "/config/apps")
    text_no_key = yaml_no_key.read_text(encoding="utf-8")
    assert get_app_dir(yaml_no_key) == "/config/apps"
    assert "token: !env_var SUPERVISOR_TOKEN" in text_no_key

    yaml_no_section = tmp_path / "no_section.yaml"
    yaml_no_section.write_text(
        "secrets: /homeassistant/secrets.yaml\n", encoding="utf-8"
    )
    set_app_dir_yaml(yaml_no_section, "/config/apps")
    assert get_app_dir(yaml_no_section) == "/config/apps"
