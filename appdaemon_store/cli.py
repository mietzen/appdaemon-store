"""Command-line entrypoint for running the AppDaemon Store service."""

import argparse
from collections.abc import Sequence
import logging
from typing import Any

import uvicorn

from appdaemon_store.const import (
    APP_NAME,
    APP_VERSION,
    DEFAULT_LOG_LEVEL,
    LOG_FORMAT,
    LOG_LEVEL_NAMES,
)
from appdaemon_store.store_manager import StoreManager
from appdaemon_store.web import create_app

logger = logging.getLogger(__name__)

_LOG_LEVEL_MAP: dict[str, int] = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
    "trace": logging.DEBUG,
}


def _build_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser for AppDaemon Store."""
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Manage AppDaemon apps from GitHub repositories.",
    )
    parser.add_argument(
        "--apps-dir",
        dest="apps_dir",
        help="Explicit AppDaemon apps directory path.",
    )
    parser.add_argument(
        "--appdaemon-yaml",
        dest="appdaemon_yaml",
        help="Path to appdaemon.yaml for automatic path resolution.",
    )
    parser.add_argument(
        "--state-file",
        dest="state_file",
        help="Path to persisted store state JSON file.",
    )
    parser.add_argument(
        "--options-file",
        dest="options_file",
        help="Path to Home Assistant add-on options.json file.",
    )
    parser.add_argument(
        "--host",
        dest="host",
        help="HTTP listen host address.",
    )
    parser.add_argument(
        "--port",
        dest="port",
        type=int,
        help="HTTP listen port.",
    )
    parser.add_argument(
        "--log-level",
        dest="log_level",
        type=str.lower,
        choices=LOG_LEVEL_NAMES,
        help="Logging verbosity level.",
    )
    parser.add_argument(
        "--github-token",
        dest="github_token",
        help="Optional GitHub token to raise API rate limits.",
    )
    parser.add_argument(
        "--check-hours",
        dest="check_interval_hours",
        type=int,
        help="Hours between scheduled background update checks.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {APP_VERSION}",
    )
    return parser


def _build_overrides(args: argparse.Namespace) -> dict[str, Any]:
    """Extract non-None CLI arguments as StoreManager config overrides."""
    raw_map: dict[str, Any] = {
        "apps_dir": args.apps_dir,
        "appdaemon_yaml": args.appdaemon_yaml,
        "state_file": args.state_file,
        "options_file": args.options_file,
        "host": args.host,
        "port": args.port,
        "log_level": args.log_level,
        "github_token": args.github_token,
        "check_interval_hours": args.check_interval_hours,
    }
    return {key: value for key, value in raw_map.items() if value is not None}


def _setup_logging(raw_level: str) -> str:
    """Configure Python root logging and return normalized uvicorn log level."""
    normalized = raw_level.strip().lower()
    if normalized == "warn":
        normalized = "warning"

    numeric_level = _LOG_LEVEL_MAP.get(normalized, logging.INFO)
    logging.basicConfig(level=numeric_level, format=LOG_FORMAT, force=True)

    if normalized not in LOG_LEVEL_NAMES:
        logger.warning(
            "Unknown log level %r; defaulting uvicorn to %s",
            raw_level,
            DEFAULT_LOG_LEVEL,
        )
        return DEFAULT_LOG_LEVEL

    return normalized


def main(argv: Sequence[str] | None = None) -> None:
    """Parse CLI options, build configuration, and run the ASGI web server."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    overrides = _build_overrides(args)

    config = StoreManager.load_config(overrides=overrides)
    uvicorn_level = _setup_logging(config.log_level)

    logger.info(
        "Starting %s v%s on %s:%s (apps_dir=%s, source=%s)",
        APP_NAME,
        APP_VERSION,
        config.host,
        config.port,
        config.apps_dir,
        config.config_source,
    )

    manager = StoreManager(config)
    app = create_app(manager)

    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level=uvicorn_level,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
