"""Enums and Pydantic v2 models for AppDaemon Store."""

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from appdaemon_store.const import (
    DEFAULT_CHECK_HOURS,
    DEFAULT_HAOS_APPDAEMON_YAML,
    DEFAULT_HAOS_APPS_DIR,
    DEFAULT_HOST,
    DEFAULT_LOG_LEVEL,
    DEFAULT_PORT,
    DEFAULT_STATE_FILE,
    DEFAULT_SUPERVISOR_URL,
)


class AutoUpdateMode(StrEnum):
    """Control whether updates are applied automatically."""

    ENABLED = "enabled"
    DISABLED = "disabled"


class RepoLayout(StrEnum):
    """Classify the folder structure of an AppDaemon repository."""

    AUTO = "auto"
    HACS_APPS = "hacs_apps"
    ROOT = "root"


class VersionKind(StrEnum):
    """Identify how a repository version was resolved."""

    RELEASE = "release"
    TAG = "tag"
    BRANCH = "branch"
    PINNED = "pinned"


class RestartMode(StrEnum):
    """Control whether AppDaemon restarts after syncing packages."""

    RESTART = "restart"
    NO_RESTART = "no_restart"


class ConfigSource(StrEnum):
    """Record how the active apps directory path was determined."""

    EXPLICIT = "explicit"
    APPDAEMON_YAML = "appdaemon_yaml"
    DEFAULT = "default"


class SeedOrigin(StrEnum):
    """Identify whether a repository came from config or the UI."""

    CONFIG = "config"
    UI = "ui"


class RuntimeMode(StrEnum):
    """Represent the host environment mode."""

    HAOS = "haos"
    STANDALONE = "standalone"


class MigrationType(StrEnum):
    """Classify detected legacy or conflicting app setups."""

    LEGACY_PATH = "legacy_path"
    DUPLICATE_APP = "duplicate_app"
    UNMANAGED_DIR = "unmanaged_dir"


class NotifyMode(StrEnum):
    """Control whether update checks send Supervisor notifications."""

    NOTIFY = "notify"
    SILENT = "silent"


class ConflictMode(StrEnum):
    """Control collision handling during file copies."""

    OVERWRITE = "overwrite"
    PRESERVE = "preserve"


class SchedulerMode(StrEnum):
    """Control whether the background update scheduler runs."""

    ENABLED = "enabled"
    DISABLED = "disabled"


class AppConfig(BaseModel):
    """Resolved runtime configuration from options, env, and CLI."""

    apps_dir: Path = Path(DEFAULT_HAOS_APPS_DIR)
    config_source: ConfigSource = ConfigSource.DEFAULT
    appdaemon_yaml_path: Path = Path(DEFAULT_HAOS_APPDAEMON_YAML)
    state_file: Path = Path(DEFAULT_STATE_FILE)
    repositories: list[str] = Field(default_factory=list)
    github_token: str | None = None
    auto_update: AutoUpdateMode = AutoUpdateMode.DISABLED
    check_interval_hours: int = DEFAULT_CHECK_HOURS
    port: int = DEFAULT_PORT
    host: str = DEFAULT_HOST
    log_level: str = DEFAULT_LOG_LEVEL
    supervisor_url: str = DEFAULT_SUPERVISOR_URL
    supervisor_token: str | None = None
    explicit_apps_dir: str | None = None


class RepoSpec(BaseModel):
    """Parsed GitHub repository coordinates and optional pinned ref."""

    owner: str
    repo: str
    full_name: str
    pinned_ref: str | None = None
    url: str


class ReleaseInfo(BaseModel):
    """Resolved version metadata and tarball download URL."""

    version: str
    ref: str
    kind: VersionKind
    tarball_url: str
    etag: str | None = None
    html_url: str | None = None


class RepoRecord(BaseModel):
    """Persisted state record for a single managed repository."""

    id: str
    owner: str
    repo: str
    url: str
    pinned_ref: str | None = None
    auto_update: AutoUpdateMode = AutoUpdateMode.DISABLED
    installed_version: str | None = None
    latest_version: str | None = None
    version_kind: VersionKind | None = None
    update_available: bool = False
    installed_folders: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    python_modules: list[str] = Field(default_factory=list)
    origin: SeedOrigin = SeedOrigin.UI
    etag: str | None = None
    last_checked: str | None = None
    last_updated: str | None = None
    last_error: str | None = None


class StoreState(BaseModel):
    """Persisted store state saved to state.json."""

    repositories: dict[str, RepoRecord] = Field(default_factory=dict)
    dismissed_migrations: list[str] = Field(default_factory=list)
    last_global_check: str | None = None


class MigrationCandidate(BaseModel):
    """Detected legacy path or unmanaged directory requiring action."""

    id: str
    migration_type: MigrationType
    title: str
    description: str
    source_path: str
    target_path: str | None = None
    related_repo: str | None = None
    items: list[str] = Field(default_factory=list)


class RequirementSyncResult(BaseModel):
    """Preview or execution summary of AppDaemon package syncing."""

    available: bool
    current_packages: list[str] = Field(default_factory=list)
    required_packages: list[str] = Field(default_factory=list)
    missing_packages: list[str] = Field(default_factory=list)
    added_packages: list[str] = Field(default_factory=list)
    restarted: bool = False
    message: str = ""


class StoreStatus(BaseModel):
    """Full service status snapshot exposed to CLI and Web UI."""

    runtime_mode: RuntimeMode
    apps_dir: str
    config_source: ConfigSource
    apps_dir_exists: bool
    supervisor_available: bool
    auto_update_default: AutoUpdateMode
    check_interval_hours: int
    last_global_check: str | None = None
    all_requirements: list[str] = Field(default_factory=list)
    repositories: list[RepoRecord] = Field(default_factory=list)
    migrations: list[MigrationCandidate] = Field(default_factory=list)
