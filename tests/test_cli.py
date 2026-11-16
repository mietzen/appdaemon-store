"""Tests for AppDaemon Store CLI parsing and uvicorn startup."""

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from appdaemon_store import cli
from appdaemon_store.const import APP_VERSION
from appdaemon_store.type_definitions import ConfigSource


def test_cli_parse_all_flags(tmp_path: Path) -> None:
    """Parse all supported CLI flags into StoreManager config overrides."""
    parser = cli._build_parser()
    args = parser.parse_args(
        [
            "--apps-dir",
            str(tmp_path / "apps"),
            "--appdaemon-yaml",
            str(tmp_path / "appdaemon.yaml"),
            "--state-file",
            str(tmp_path / "state.json"),
            "--options-file",
            str(tmp_path / "options.json"),
            "--host",
            "127.0.0.1",
            "--port",
            "9123",
            "--log-level",
            "DEBUG",
            "--github-token",
            "ghp_test123",
            "--check-hours",
            "12",
        ]
    )

    overrides = cli._build_overrides(args)
    assert overrides == {
        "apps_dir": str(tmp_path / "apps"),
        "appdaemon_yaml": str(tmp_path / "appdaemon.yaml"),
        "state_file": str(tmp_path / "state.json"),
        "options_file": str(tmp_path / "options.json"),
        "host": "127.0.0.1",
        "port": 9123,
        "log_level": "debug",
        "github_token": "ghp_test123",
        "check_interval_hours": 12,
    }


def test_cli_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """Exit with code 0 and print APP_VERSION on --version."""
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--version"])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert APP_VERSION in captured.out


def test_cli_main_runs_uvicorn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Load config overrides, build FastAPI app, and invoke uvicorn.run."""
    options_file = tmp_path / "options.json"
    options_file.write_text(
        json.dumps({"apps_dir": str(tmp_path / "from_opts")}),
        encoding="utf-8",
    )

    recorded: dict[str, Any] = {}

    def fake_run(app: Any, **kwargs: Any) -> None:
        """Capture FastAPI app and uvicorn keyword arguments."""
        recorded["app"] = app
        recorded["kwargs"] = kwargs

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)

    cli.main(
        [
            "--options-file",
            str(options_file),
            "--apps-dir",
            str(tmp_path / "cli_apps"),
            "--state-file",
            str(tmp_path / "state.json"),
            "--host",
            "127.0.0.1",
            "--port",
            "8123",
            "--log-level",
            "warning",
            "--check-hours",
            "0",
        ]
    )

    assert recorded["app"] is not None
    assert recorded["kwargs"] == {
        "host": "127.0.0.1",
        "port": 8123,
        "log_level": "warning",
        "proxy_headers": True,
        "forwarded_allow_ips": "*",
    }


def test_cli_warn_log_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normalize log level aliases cleanly and verify ConfigSource."""
    assert cli._setup_logging("WARN") == "warning"
    assert logging.getLogger().level == logging.WARNING
    assert cli._setup_logging("invalid-level") == "info"

    options_file = tmp_path / "options.json"
    options_file.write_text("{}", encoding="utf-8")
    monkeypatch.delenv("APPDAEMON_STORE_APPS_DIR", raising=False)

    cfg = cli.StoreManager.load_config(
        overrides={
            "options_file": str(options_file),
            "apps_dir": str(tmp_path / "apps"),
        }
    )
    assert cfg.config_source == ConfigSource.EXPLICIT
