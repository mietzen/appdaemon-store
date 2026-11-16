"""Resolve AppDaemon directory paths and read/update appdaemon.yaml."""

import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any

import yaml

from appdaemon_store.const import (
    CONTAINER_CONFIG_DIR,
    CONTAINER_CONFIG_PREFIX,
    CONTAINER_HA_DIR,
    CONTAINER_HA_PREFIX,
    DEFAULT_HAOS_ADDON_CONFIG,
    DEFAULT_HAOS_APPS_DIR,
    YAML_APP_DIR_KEY,
    YAML_APPDAEMON_KEY,
)
from appdaemon_store.exceptions import MigrationError
from appdaemon_store.type_definitions import ConfigSource

logger = logging.getLogger(__name__)


def read_appdaemon_yaml(yaml_path: Path) -> dict[str, Any] | None:
    """Read and parse appdaemon.yaml, returning None if missing or invalid."""
    if not yaml_path.is_file():
        return None

    try:
        raw = yaml_path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError):
        logger.exception("Failed reading appdaemon YAML at %s", yaml_path)
        return None

    if not isinstance(parsed, dict):
        return None

    return parsed


def get_app_dir(yaml_path: Path) -> str | None:
    """Return appdaemon.app_dir from appdaemon.yaml if configured."""
    data = read_appdaemon_yaml(yaml_path)
    if data is None:
        return None

    section = data.get(YAML_APPDAEMON_KEY)
    if not isinstance(section, dict):
        return None

    app_dir = section.get(YAML_APP_DIR_KEY)
    if not isinstance(app_dir, str) or not app_dir.strip():
        return None

    return app_dir.strip()


def _strip_prefix(raw_path: str, exact_dir: str, prefix: str) -> str:
    """Extract the relative tail after a container root prefix."""
    if raw_path == exact_dir:
        return ""

    return raw_path.removeprefix(prefix).lstrip("/")


def _join_rel(base: Path, rel: str) -> Path:
    """Join a relative subpath when non-empty, otherwise return base."""
    return base / rel if rel else base


def resolve_apps_dir(
    explicit: str | None,
    yaml_path: Path,
    ha_root: Path = Path(CONTAINER_HA_DIR),
    cfg_root: Path = Path(CONTAINER_CONFIG_DIR),
    addon_root: Path = Path(DEFAULT_HAOS_ADDON_CONFIG),
) -> tuple[Path, ConfigSource]:
    """Resolve the active AppDaemon apps directory and its config source."""
    if explicit and explicit.strip():
        return Path(explicit.strip()), ConfigSource.EXPLICIT

    app_dir = get_app_dir(yaml_path)
    if not app_dir:
        return Path(DEFAULT_HAOS_APPS_DIR), ConfigSource.DEFAULT

    # Inside a0d7b954_appdaemon, /config maps to /addon_configs/a0d7b954_appdaemon.
    if app_dir == CONTAINER_CONFIG_DIR or app_dir.startswith(CONTAINER_CONFIG_PREFIX):
        rel = _strip_prefix(app_dir, CONTAINER_CONFIG_DIR, CONTAINER_CONFIG_PREFIX)
        return _join_rel(addon_root, rel), ConfigSource.APPDAEMON_YAML

    if app_dir == CONTAINER_HA_DIR or app_dir.startswith(CONTAINER_HA_PREFIX):
        rel = _strip_prefix(app_dir, CONTAINER_HA_DIR, CONTAINER_HA_PREFIX)
        if ha_root.exists():
            return _join_rel(ha_root, rel), ConfigSource.APPDAEMON_YAML
        if cfg_root.exists():
            return _join_rel(cfg_root, rel), ConfigSource.APPDAEMON_YAML

    return Path(app_dir), ConfigSource.APPDAEMON_YAML


def map_legacy_path(
    app_dir: str,
    ha_root: Path = Path(CONTAINER_HA_DIR),
    cfg_root: Path = Path(CONTAINER_CONFIG_DIR),
    addon_root: Path = Path(DEFAULT_HAOS_ADDON_CONFIG),
) -> Path:
    """Translate a container app_dir path to an accessible filesystem path."""
    cleaned = app_dir.strip()
    if not cleaned:
        return Path(DEFAULT_HAOS_APPS_DIR)

    if cleaned == CONTAINER_HA_DIR or cleaned.startswith(CONTAINER_HA_PREFIX):
        rel = _strip_prefix(cleaned, CONTAINER_HA_DIR, CONTAINER_HA_PREFIX)
        if ha_root.exists():
            return _join_rel(ha_root, rel)
        if cfg_root.exists():
            return _join_rel(cfg_root, rel)
        return Path(cleaned)

    if cleaned == CONTAINER_CONFIG_DIR or cleaned.startswith(CONTAINER_CONFIG_PREFIX):
        rel = _strip_prefix(cleaned, CONTAINER_CONFIG_DIR, CONTAINER_CONFIG_PREFIX)
        for root in (addon_root, cfg_root, ha_root):
            candidate = _join_rel(root, rel)
            if candidate.exists():
                return candidate
        return _join_rel(addon_root, rel)

    return Path(cleaned)


def set_app_dir_yaml(yaml_path: Path, new_app_dir: str) -> None:
    """Update appdaemon.app_dir in appdaemon.yaml while preserving other keys."""
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}

    if yaml_path.exists():
        try:
            loaded = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as err:
            raise MigrationError(f"Failed reading {yaml_path}: {err}") from err

        if loaded is not None and not isinstance(loaded, dict):
            raise MigrationError(f"Expected YAML mapping in {yaml_path}")
        if isinstance(loaded, dict):
            data = loaded

    section = data.get(YAML_APPDAEMON_KEY)
    if section is None:
        section = {}
        data[YAML_APPDAEMON_KEY] = section
    elif not isinstance(section, dict):
        raise MigrationError(
            f"Expected mapping for '{YAML_APPDAEMON_KEY}' in {yaml_path}"
        )

    section[YAML_APP_DIR_KEY] = new_app_dir
    dumped = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    _atomic_write(yaml_path, dumped)


def read_options_json(options_path: Path) -> dict[str, Any]:
    """Load Home Assistant add-on options.json if present."""
    if not options_path.is_file():
        return {}

    try:
        parsed = json.loads(options_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("Failed reading options file at %s", options_path)
        return {}

    if not isinstance(parsed, dict):
        return {}

    return parsed


def _atomic_write(target_path: Path, content: str) -> None:
    """Write text atomically to target_path using a same-directory temp file."""
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target_path.parent,
            prefix=f".{target_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target_path)
    except OSError as err:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise MigrationError(f"Failed writing {target_path}: {err}") from err
