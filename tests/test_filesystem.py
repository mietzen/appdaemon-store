"""Tests for filesystem layout detection, installation, and legacy copies."""

from pathlib import Path

import pytest

from appdaemon_store.exceptions import ValidationError
from appdaemon_store.filesystem import (
    copy_legacy_apps,
    detect_layout,
    install_staged,
    remove_folders,
    scan_dir_modules,
)
from appdaemon_store.type_definitions import RepoLayout


def test_layout_hacs_subdirs(tmp_path: Path) -> None:
    """Detect HACS apps/ subdirectories as separate target folders."""
    staged = tmp_path / "staged"
    (staged / "apps" / "app_one").mkdir(parents=True)
    (staged / "apps" / "app_two").mkdir(parents=True)
    (staged / "apps" / "app_one" / "one.py").write_text("", encoding="utf-8")

    layout, targets = detect_layout(staged, "my-repo")

    assert layout == RepoLayout.HACS_APPS
    assert [name for _, name in targets] == ["app_one", "app_two"]


def test_layout_hacs_files_only(tmp_path: Path) -> None:
    """Map apps/ with only files to a folder named after the repo."""
    staged = tmp_path / "staged"
    (staged / "apps").mkdir(parents=True)
    (staged / "apps" / "my_app.py").write_text("", encoding="utf-8")

    layout, targets = detect_layout(staged, "my-repo")

    assert layout == RepoLayout.HACS_APPS
    assert targets == [(staged / "apps", "my-repo")]


def test_layout_root_content(tmp_path: Path) -> None:
    """Respect content_in_root in hacs.json even if apps/ exists."""
    staged = tmp_path / "staged"
    (staged / "apps" / "sub").mkdir(parents=True)
    (staged / "hacs.json").write_text('{"content_in_root": true}', encoding="utf-8")

    layout, targets = detect_layout(staged, "root-repo")

    assert layout == RepoLayout.ROOT
    assert targets == [(staged, "root-repo")]


def test_install_staged_and_clean(tmp_path: Path) -> None:
    """Install staged app, skip ignored root files, and clean old folders."""
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "README.md").write_text("docs", encoding="utf-8")
    (staged / "LICENSE").write_text("mit", encoding="utf-8")
    (staged / ".gitignore").write_text("*.pyc", encoding="utf-8")
    (staged / "hacs.json").write_text("{}", encoding="utf-8")
    (staged / "my_offset.py").write_text("class Offset: pass\n", encoding="utf-8")
    (staged / "requirements.txt").write_text(
        "# comment\nfritzconnection>=1.13.0\n\nhttpx>=0.27\n",
        encoding="utf-8",
    )

    apps_dir = tmp_path / "apps"
    stale_dir = apps_dir / "old_folder"
    stale_dir.mkdir(parents=True)
    (stale_dir / "stale.py").write_text("", encoding="utf-8")

    folders, reqs, modules = install_staged(
        staged,
        apps_dir,
        "fritz-thermostat",
        ["old_folder"],
    )

    assert folders == ["fritz-thermostat"]
    assert reqs == ["fritzconnection>=1.13.0", "httpx>=0.27"]
    assert modules == ["my_offset.py"]
    assert not stale_dir.exists()
    installed = apps_dir / "fritz-thermostat"
    assert (installed / "my_offset.py").is_file()
    assert not (installed / "README.md").exists()
    assert not (installed / "LICENSE").exists()
    assert not (installed / ".gitignore").exists()


def test_install_root_reqs_hacs(tmp_path: Path) -> None:
    """Collect requirements.txt placed at repo root for HACS_APPS repos."""
    staged = tmp_path / "staged"
    (staged / "apps" / "thermostat").mkdir(parents=True)
    (staged / "apps" / "thermostat" / "app.py").write_text("", encoding="utf-8")
    (staged / "requirements.txt").write_text("pyyaml>=6.0\n", encoding="utf-8")

    apps_dir = tmp_path / "apps"
    folders, reqs, modules = install_staged(staged, apps_dir, "repo-name", [])

    assert folders == ["thermostat"]
    assert reqs == ["pyyaml>=6.0"]
    assert modules == ["app.py"]


def test_remove_folders_guard(tmp_path: Path) -> None:
    """Reject traversal folder names while removing valid ones."""
    apps_dir = tmp_path / "apps"
    (apps_dir / "valid").mkdir(parents=True)

    with pytest.raises(ValidationError):
        remove_folders(apps_dir, ["../outside"])

    with pytest.raises(ValidationError):
        remove_folders(apps_dir, ["a/b"])

    remove_folders(apps_dir, ["valid"])
    assert not (apps_dir / "valid").exists()


def test_scan_dir_modules(tmp_path: Path) -> None:
    """Find recursive .py files while ignoring __init__.py and __pycache__."""
    folder = tmp_path / "app"
    (folder / "nested").mkdir(parents=True)
    (folder / "__pycache__").mkdir(parents=True)
    (folder / "__init__.py").write_text("", encoding="utf-8")
    (folder / "main.py").write_text("", encoding="utf-8")
    (folder / "nested" / "helper.py").write_text("", encoding="utf-8")
    (folder / "__pycache__" / "cached.py").write_text("", encoding="utf-8")

    assert scan_dir_modules(folder) == ["helper.py", "main.py"]


def test_copy_legacy_backups(tmp_path: Path) -> None:
    """Copy legacy apps and create backup if target already exists."""
    src = tmp_path / "legacy_apps"
    (src / "my_app").mkdir(parents=True)
    (src / "my_app" / "run.py").write_text("new", encoding="utf-8")
    (src / "apps.yaml").write_text("new_cfg: 1\n", encoding="utf-8")

    dst = tmp_path / "new_apps"
    dst.mkdir(parents=True)
    (dst / "apps.yaml").write_text("old_cfg: 1\n", encoding="utf-8")

    copied = copy_legacy_apps(src, dst)

    assert copied == ["apps.yaml", "my_app"]
    assert (dst / "apps.yaml").read_text(encoding="utf-8") == "new_cfg: 1\n"
    assert (dst / "my_app" / "run.py").read_text(encoding="utf-8") == "new"
    backups = list(dst.glob("apps.yaml.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "old_cfg: 1\n"
