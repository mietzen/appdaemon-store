"""Filesystem operations for repository layout detection and app installation."""

from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import shutil

from appdaemon_store.const import (
    APPS_SUBDIR_NAME,
    HACS_CONTENT_IN_ROOT_KEY,
    HACS_JSON_NAME,
    IGNORED_COMMON_NAMES,
    IGNORED_EXTRACT_NAMES,
    INIT_PY_NAME,
    PYCACHE_DIR,
    PYTHON_EXT,
    REQUIREMENTS_NAME,
)
from appdaemon_store.exceptions import ValidationError
from appdaemon_store.type_definitions import RepoLayout

logger = logging.getLogger(__name__)


def _read_hacs_root(staged_dir: Path) -> RepoLayout:
    """Check hacs.json for explicit content_in_root setting."""
    hacs_path = staged_dir / HACS_JSON_NAME
    if not hacs_path.is_file():
        return RepoLayout.AUTO

    try:
        data = json.loads(hacs_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Ignoring invalid %s in %s", HACS_JSON_NAME, staged_dir)
        return RepoLayout.AUTO

    if isinstance(data, dict) and data.get(HACS_CONTENT_IN_ROOT_KEY) is True:
        return RepoLayout.ROOT

    return RepoLayout.AUTO


def detect_layout(
    staged_dir: Path, repo_name: str
) -> tuple[RepoLayout, list[tuple[Path, str]]]:
    """Detect whether repository apps live under apps/ or at the repo root."""
    hacs_hint = _read_hacs_root(staged_dir)
    apps_dir = staged_dir / APPS_SUBDIR_NAME

    if hacs_hint != RepoLayout.ROOT and apps_dir.is_dir():
        entries = [
            entry
            for entry in apps_dir.iterdir()
            if entry.name not in IGNORED_COMMON_NAMES
        ]
        subdirs = sorted(
            (entry for entry in entries if entry.is_dir()), key=lambda p: p.name
        )
        if subdirs:
            return RepoLayout.HACS_APPS, [(subdir, subdir.name) for subdir in subdirs]

        if entries:
            return RepoLayout.HACS_APPS, [(apps_dir, repo_name)]

    return RepoLayout.ROOT, [(staged_dir, repo_name)]


def _validate_folder(apps_dir: Path, folder_name: str) -> Path:
    """Validate that folder_name stays strictly inside apps_dir."""
    cleaned = folder_name.strip()
    if not cleaned or cleaned in {".", ".."} or "/" in cleaned or "\\" in cleaned:
        raise ValidationError(f"Invalid target folder name: {folder_name!r}")

    root = apps_dir.resolve()
    target = (root / cleaned).resolve()
    if target == root or not target.is_relative_to(root):
        raise ValidationError(f"Target folder escapes apps_dir: {folder_name!r}")

    return target


def remove_folders(apps_dir: Path, folders: list[str]) -> None:
    """Safely remove tracked app folders strictly inside apps_dir."""
    if not apps_dir.exists():
        return

    for folder in folders:
        target = _validate_folder(apps_dir, folder)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()


def _copy_tree(src_dir: Path, dst_dir: Path, ignored_names: frozenset[str]) -> None:
    """Copy a directory tree into dst_dir skipping ignored filenames."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in src_dir.iterdir():
        if item.name in ignored_names or item.name in IGNORED_COMMON_NAMES:
            continue

        dest_item = dst_dir / item.name
        if item.is_dir():
            _copy_tree(item, dest_item, IGNORED_COMMON_NAMES)
        elif item.is_file():
            shutil.copy2(item, dest_item)


def _parse_reqs(req_path: Path) -> list[str]:
    """Parse non-empty, non-comment package lines from a requirements.txt file."""
    if not req_path.is_file():
        return []

    lines: list[str] = []
    for raw_line in req_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)

    return lines


def scan_dir_modules(folder: Path) -> list[str]:
    """Return sorted unique Python module filenames inside folder recursively."""
    if not folder.is_dir():
        return []

    modules: set[str] = set()
    for py_file in folder.rglob(f"*{PYTHON_EXT}"):
        if any(
            part in IGNORED_COMMON_NAMES or part == PYCACHE_DIR
            for part in py_file.parts
        ):
            continue
        if py_file.name == INIT_PY_NAME:
            continue
        modules.add(py_file.name)

    return sorted(modules)


def install_staged(
    staged_dir: Path,
    apps_dir: Path,
    repo_name: str,
    old_folders: list[str],
) -> tuple[list[str], list[str], list[str]]:
    """Install staged repository contents into apps_dir and inspect metadata."""
    apps_dir.mkdir(parents=True, exist_ok=True)
    layout, targets = detect_layout(staged_dir, repo_name)
    validated_targets = [
        (src, folder_name, _validate_folder(apps_dir, folder_name))
        for src, folder_name in targets
    ]

    remove_folders(apps_dir, old_folders)

    installed_folders: list[str] = []
    all_reqs: list[str] = []
    seen_reqs: set[str] = set()
    all_modules: set[str] = set()

    for src_path, folder_name, dst_path in validated_targets:
        if dst_path.is_dir():
            shutil.rmtree(dst_path)
        elif dst_path.exists():
            dst_path.unlink()

        skip_names = (
            IGNORED_EXTRACT_NAMES
            if layout == RepoLayout.ROOT and src_path == staged_dir
            else IGNORED_COMMON_NAMES
        )
        _copy_tree(src_path, dst_path, skip_names)
        installed_folders.append(folder_name)

        for req in _parse_reqs(dst_path / REQUIREMENTS_NAME):
            if req not in seen_reqs:
                seen_reqs.add(req)
                all_reqs.append(req)

        all_modules.update(scan_dir_modules(dst_path))

    # HACS repositories sometimes place requirements.txt at the repo root.
    for req in _parse_reqs(staged_dir / REQUIREMENTS_NAME):
        if req not in seen_reqs:
            seen_reqs.add(req)
            all_reqs.append(req)

    return installed_folders, all_reqs, sorted(all_modules)


def _backup_existing(dst_path: Path) -> None:
    """Rename an existing colliding destination path with a UTC timestamp suffix."""
    if not dst_path.exists():
        return

    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    backup = dst_path.with_name(f"{dst_path.name}.bak.{stamp}")
    counter = 1
    while backup.exists():
        backup = dst_path.with_name(f"{dst_path.name}.bak.{stamp}.{counter}")
        counter += 1

    dst_path.rename(backup)


def copy_legacy_apps(src_dir: Path, dst_dir: Path) -> list[str]:
    """Copy apps.yaml, subdirectories, and files safely from a legacy folder."""
    if not src_dir.is_dir():
        return []

    dst_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []

    for item in sorted(src_dir.iterdir(), key=lambda p: p.name):
        if item.name in IGNORED_COMMON_NAMES or item.name.startswith("."):
            continue

        target = dst_dir / item.name
        _backup_existing(target)

        if item.is_dir():
            _copy_tree(item, target, IGNORED_COMMON_NAMES)
            copied.append(item.name)
        elif item.is_file():
            shutil.copy2(item, target)
            copied.append(item.name)

    return copied
