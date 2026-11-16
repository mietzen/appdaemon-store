"""Core orchestration service layer for AppDaemon Store."""

import asyncio
from datetime import UTC, datetime, timedelta
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any

from appdaemon_store import filesystem, path_resolver
from appdaemon_store.const import (
    DEFAULT_CHECK_HOURS,
    DEFAULT_HAOS_APPDAEMON_YAML,
    DEFAULT_HOST,
    DEFAULT_LOG_LEVEL,
    DEFAULT_OPTIONS_FILE,
    DEFAULT_PORT,
    DEFAULT_STATE_FILE,
    DEFAULT_SUPERVISOR_URL,
    ENV_APPDAEMON_YAML,
    ENV_APPS_DIR,
    ENV_AUTO_UPDATE,
    ENV_CHECK_HOURS,
    ENV_GITHUB_TOKEN,
    ENV_HOST,
    ENV_LOG_LEVEL,
    ENV_OPTIONS_FILE,
    ENV_PORT,
    ENV_REPOSITORIES,
    ENV_STATE_FILE,
    ENV_SUPERVISOR_TOKEN,
    ENV_SUPERVISOR_URL,
    HTTP_NOT_FOUND,
    MIG_ID_LEGACY_PREFIX,
    NOTIFY_ID_PREFIX,
)
from appdaemon_store.exceptions import (
    GitHubApiError,
    RepoNotFoundError,
    StoreError,
    SupervisorError,
    ValidationError,
)
from appdaemon_store.github_client import GitHubClient, parse_repo_input
from appdaemon_store.migration import MigrationService
from appdaemon_store.state_store import StateStore
from appdaemon_store.supervisor_client import SupervisorClient
from appdaemon_store.type_definitions import (
    AppConfig,
    AutoUpdateMode,
    ConfigSource,
    NotifyMode,
    ReleaseInfo,
    RepoRecord,
    RepoSpec,
    RequirementSyncResult,
    RestartMode,
    RuntimeMode,
    SeedOrigin,
    StoreState,
    StoreStatus,
)

logger = logging.getLogger(__name__)

_TRUTHY_STRINGS = frozenset({"1", "true", "yes", "on", "enabled"})
_FALSY_STRINGS = frozenset({"0", "false", "no", "off", "disabled"})


def _utc_now_iso() -> str:
    """Return current UTC timestamp formatted as ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def _parse_iso_dt(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp string into a UTC-aware datetime."""
    if not raw:
        return None

    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)

    return parsed.astimezone(UTC)


def _parse_auto_update(raw: Any, default: AutoUpdateMode) -> AutoUpdateMode:
    """Parse an AutoUpdateMode value from CLI, env var, or JSON option."""
    if raw is None:
        return default
    if isinstance(raw, AutoUpdateMode):
        return raw
    if isinstance(raw, bool):
        return AutoUpdateMode.ENABLED if raw else AutoUpdateMode.DISABLED

    cleaned = str(raw).strip().lower()
    if not cleaned:
        return default
    if cleaned in _TRUTHY_STRINGS:
        return AutoUpdateMode.ENABLED
    if cleaned in _FALSY_STRINGS:
        return AutoUpdateMode.DISABLED

    raise ValidationError(f"Invalid auto_update value: {raw!r}")


def _parse_repo_list(raw: Any) -> list[str]:
    """Parse repositories list from JSON array or comma/newline-delimited string."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]

    text = str(raw).strip()
    if not text:
        return []

    if text.startswith("["):
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError as err:
            raise ValidationError(f"Invalid JSON repositories list: {err}") from err
        if not isinstance(loaded, list):
            raise ValidationError("ENV repositories JSON must be a list")
        return [str(item).strip() for item in loaded if str(item).strip()]

    normalized = text.replace("\n", ",")
    return [part.strip() for part in normalized.split(",") if part.strip()]


def _parse_int_val(raw: Any, default: int, label: str) -> int:
    """Parse a positive integer configuration value."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default

    try:
        value = int(raw)
    except (TypeError, ValueError) as err:
        raise ValidationError(f"Invalid integer for {label}: {raw!r}") from err

    return value


def _spec_from_record(record: RepoRecord) -> RepoSpec:
    """Construct a RepoSpec from a persisted RepoRecord."""
    return RepoSpec(
        owner=record.owner,
        repo=record.repo,
        full_name=record.id,
        pinned_ref=record.pinned_ref,
        url=record.url,
    )


def _collect_requirements(state: StoreState) -> list[str]:
    """Collect deduplicated Python package requirements across all managed repos."""
    reqs: list[str] = []
    seen: set[str] = set()

    for record in state.repositories.values():
        for req in record.requirements:
            if req not in seen:
                seen.add(req)
                reqs.append(req)

    return reqs


class StoreManager:
    """Service facade coordinating repositories, updates, packages, and migrations."""

    def __init__(
        self,
        config: AppConfig,
        github_client: GitHubClient | None = None,
        state_store: StateStore | None = None,
        supervisor_client: SupervisorClient | None = None,
        migration_service: MigrationService | None = None,
    ) -> None:
        """Initialize the store manager with configuration and optional drivers."""
        self._config = config
        self._github = github_client or GitHubClient(token=config.github_token)
        self._state_store = state_store or StateStore(config.state_file)
        self._supervisor = supervisor_client or SupervisorClient(
            base_url=config.supervisor_url,
            token=config.supervisor_token,
        )
        self._migrations = migration_service or MigrationService(
            state_store=self._state_store,
        )
        self._lock = asyncio.Lock()

    @property
    def config(self) -> AppConfig:
        """Return the resolved runtime configuration."""
        return self._config

    @classmethod
    def load_config(
        cls,
        options_file: Path | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> AppConfig:
        """Build AppConfig from CLI overrides, environment variables, and options.json."""
        ovr = overrides or {}
        opt_path = (
            options_file
            or (Path(str(ovr["options_file"])) if ovr.get("options_file") else None)
            or (
                Path(os.environ[ENV_OPTIONS_FILE])
                if os.environ.get(ENV_OPTIONS_FILE)
                else Path(DEFAULT_OPTIONS_FILE)
            )
        )
        opts = path_resolver.read_options_json(opt_path)

        explicit_apps = _pick_str(
            ovr.get("apps_dir"), os.environ.get(ENV_APPS_DIR), opts.get("apps_dir")
        )
        yaml_raw = _pick_str(
            ovr.get("appdaemon_yaml"),
            os.environ.get(ENV_APPDAEMON_YAML),
            None,
        )
        yaml_path = Path(yaml_raw) if yaml_raw else Path(DEFAULT_HAOS_APPDAEMON_YAML)
        apps_dir, config_source = path_resolver.resolve_apps_dir(
            explicit_apps, yaml_path
        )

        state_raw = _pick_str(
            ovr.get("state_file"), os.environ.get(ENV_STATE_FILE), None
        )
        state_file = Path(state_raw) if state_raw else Path(DEFAULT_STATE_FILE)

        repos_raw = ovr.get("repositories")
        if repos_raw is None:
            env_repos = os.environ.get(ENV_REPOSITORIES)
            repos_raw = (
                env_repos
                if env_repos and env_repos.strip()
                else opts.get("repositories")
            )
        repositories = _parse_repo_list(repos_raw)

        github_token = _pick_str(
            ovr.get("github_token"),
            os.environ.get(ENV_GITHUB_TOKEN),
            opts.get("github_token"),
        )
        env_auto = os.environ.get(ENV_AUTO_UPDATE)
        auto_raw = (
            ovr.get("auto_update")
            if ovr.get("auto_update") is not None
            else (
                env_auto if env_auto and env_auto.strip() else opts.get("auto_update")
            )
        )
        auto_update = _parse_auto_update(auto_raw, AutoUpdateMode.DISABLED)

        env_hours = os.environ.get(ENV_CHECK_HOURS)
        hours_raw = (
            ovr.get("check_interval_hours")
            if ovr.get("check_interval_hours") is not None
            else (
                env_hours
                if env_hours and env_hours.strip()
                else opts.get("check_interval_hours")
            )
        )
        check_hours = _parse_int_val(
            hours_raw,
            DEFAULT_CHECK_HOURS,
            "check_interval_hours",
        )
        port = _parse_int_val(
            ovr.get("port")
            if ovr.get("port") is not None
            else os.environ.get(ENV_PORT),
            DEFAULT_PORT,
            "port",
        )
        host = (
            _pick_str(ovr.get("host"), os.environ.get(ENV_HOST), None) or DEFAULT_HOST
        )
        log_level = (
            _pick_str(
                ovr.get("log_level"),
                os.environ.get(ENV_LOG_LEVEL),
                opts.get("log_level"),
            )
            or DEFAULT_LOG_LEVEL
        )
        supervisor_url = (
            _pick_str(
                ovr.get("supervisor_url"), os.environ.get(ENV_SUPERVISOR_URL), None
            )
            or DEFAULT_SUPERVISOR_URL
        )
        supervisor_token = _pick_str(
            ovr.get("supervisor_token"),
            os.environ.get(ENV_SUPERVISOR_TOKEN),
            None,
        )

        return AppConfig(
            apps_dir=apps_dir,
            config_source=config_source,
            appdaemon_yaml_path=yaml_path,
            state_file=state_file,
            repositories=repositories,
            github_token=github_token,
            auto_update=auto_update,
            check_interval_hours=check_hours,
            port=port,
            host=host,
            log_level=log_level,
            supervisor_url=supervisor_url,
            supervisor_token=supervisor_token,
            explicit_apps_dir=explicit_apps,
        )

    async def aclose(self) -> None:
        """Close underlying async HTTP clients."""
        await self._github.aclose()
        await self._supervisor.aclose()

    async def initialize(self) -> None:
        """Ensure directories, seed configured repositories, and run due checks."""
        self._config.apps_dir.mkdir(parents=True, exist_ok=True)
        needs_check = False

        async with self._lock:
            state = self._state_store.load()
            had_existing = bool(state.repositories)

            await self._seed_repos(state)
            if state.repositories and not had_existing:
                state.last_global_check = _utc_now_iso()

            self._state_store.save(state)
            needs_check = had_existing and self._is_check_due(state)

        if needs_check:
            await self.check_updates(NotifyMode.NOTIFY)

    async def _seed_repos(self, state: StoreState) -> None:
        """Seed configured repositories that are missing or absent on disk."""
        for raw_repo in self._config.repositories:
            try:
                spec = parse_repo_input(raw_repo)
            except ValidationError:
                logger.exception("Skipping invalid configured repository: %s", raw_repo)
                continue

            existing = state.repositories.get(spec.full_name)
            if existing is not None:
                if self._needs_disk_repair(existing):
                    await self._repair_repo(state, existing)
                continue

            try:
                record = await self._install_new(
                    spec,
                    self._config.auto_update,
                    SeedOrigin.CONFIG,
                )
            except StoreError as err:
                logger.exception(
                    "Failed seeding configured repository %s", spec.full_name
                )
                record = RepoRecord(
                    id=spec.full_name,
                    owner=spec.owner,
                    repo=spec.repo,
                    url=spec.url,
                    pinned_ref=spec.pinned_ref,
                    auto_update=self._config.auto_update,
                    origin=SeedOrigin.CONFIG,
                    last_checked=_utc_now_iso(),
                    last_error=str(err),
                )

            state.repositories[spec.full_name] = record

    def _needs_disk_repair(self, record: RepoRecord) -> bool:
        """Return whether a tracked repository is missing all folders on disk."""
        if not record.installed_folders:
            return record.installed_version is None

        return not any(
            (self._config.apps_dir / folder).exists()
            for folder in record.installed_folders
        )

    async def _repair_repo(self, state: StoreState, record: RepoRecord) -> None:
        """Reinstall a seeded repository whose installed folders are missing."""
        spec = _spec_from_record(record)
        try:
            release = await self._github.resolve_version(spec, etag=None)
            if release is None:
                return
            folders, reqs, mods = await self._install_release(
                spec,
                release,
                record.installed_folders,
            )
        except StoreError as err:
            logger.exception("Failed repairing repository %s", record.id)
            record.last_error = str(err)
            record.last_checked = _utc_now_iso()
            return

        now = _utc_now_iso()
        record.installed_version = release.version
        record.latest_version = release.version
        record.version_kind = release.kind
        record.update_available = False
        record.installed_folders = folders
        record.requirements = reqs
        record.python_modules = mods
        record.etag = release.etag
        record.last_checked = now
        record.last_updated = now
        record.last_error = None
        state.repositories[record.id] = record

    def _is_check_due(self, state: StoreState) -> bool:
        """Return whether an automatic update check should run now."""
        if not state.repositories or self._config.check_interval_hours <= 0:
            return False

        last_check = _parse_iso_dt(state.last_global_check)
        if last_check is None:
            return True

        interval = timedelta(hours=self._config.check_interval_hours)
        return datetime.now(UTC) - last_check >= interval

    async def _install_release(
        self,
        spec: RepoSpec,
        release: ReleaseInfo,
        old_folders: list[str],
    ) -> tuple[list[str], list[str], list[str]]:
        """Download tarball to temporary staging dir and install into apps_dir."""
        with tempfile.TemporaryDirectory(prefix="adstore_") as tmp_dir:
            staged = Path(tmp_dir) / "staged"
            await self._github.download_tarball(release.tarball_url, staged)
            return filesystem.install_staged(
                staged,
                self._config.apps_dir,
                spec.repo,
                old_folders,
            )

    async def _install_new(
        self,
        spec: RepoSpec,
        auto_update: AutoUpdateMode,
        origin: SeedOrigin,
    ) -> RepoRecord:
        """Resolve, download, and install a new repository."""
        release = await self._github.resolve_version(spec, etag=None)
        if release is None:
            raise GitHubApiError(f"Could not resolve version for {spec.full_name}")

        folders, reqs, mods = await self._install_release(spec, release, [])
        now = _utc_now_iso()

        return RepoRecord(
            id=spec.full_name,
            owner=spec.owner,
            repo=spec.repo,
            url=spec.url,
            pinned_ref=spec.pinned_ref,
            auto_update=auto_update,
            installed_version=release.version,
            latest_version=release.version,
            version_kind=release.kind,
            update_available=False,
            installed_folders=folders,
            requirements=reqs,
            python_modules=mods,
            origin=origin,
            etag=release.etag,
            last_checked=now,
            last_updated=now,
            last_error=None,
        )

    def _build_status(self, state: StoreState) -> StoreStatus:
        """Build StoreStatus snapshot from current StoreState and filesystem."""
        runtime_mode = (
            RuntimeMode.HAOS
            if self._supervisor.is_available()
            else RuntimeMode.STANDALONE
        )
        migrations = self._migrations.detect_all(
            self._config.apps_dir,
            self._config.appdaemon_yaml_path,
            state,
        )
        repos = sorted(state.repositories.values(), key=lambda item: item.id.lower())

        return StoreStatus(
            runtime_mode=runtime_mode,
            apps_dir=str(self._config.apps_dir),
            config_source=self._config.config_source,
            apps_dir_exists=self._config.apps_dir.is_dir(),
            supervisor_available=self._supervisor.is_available(),
            auto_update_default=self._config.auto_update,
            check_interval_hours=self._config.check_interval_hours,
            last_global_check=state.last_global_check,
            all_requirements=_collect_requirements(state),
            repositories=repos,
            migrations=migrations,
        )

    async def get_status(self) -> StoreStatus:
        """Return full status including repositories, requirements, and migrations."""
        async with self._lock:
            state = self._state_store.load()
            return self._build_status(state)

    async def add_repo(self, raw_repo: str, auto_update: AutoUpdateMode) -> RepoRecord:
        """Add and install a GitHub repository into apps_dir."""
        spec = parse_repo_input(raw_repo)

        async with self._lock:
            state = self._state_store.load()
            if spec.full_name in state.repositories:
                raise ValidationError(f"Repository already managed: {spec.full_name}")

            record = await self._install_new(spec, auto_update, SeedOrigin.UI)
            state.repositories[spec.full_name] = record
            self._state_store.save(state)
            return record

    async def _install_existing(self, repo_id: str) -> RepoRecord:
        """Resolve latest matching version and reinstall an existing managed repo."""
        async with self._lock:
            state = self._state_store.load()
            record = state.repositories.get(repo_id)
            if record is None:
                raise RepoNotFoundError(
                    f"Repository not managed: {repo_id}",
                    status_code=HTTP_NOT_FOUND,
                )

            spec = _spec_from_record(record)
            release = await self._github.resolve_version(spec, etag=None)
            if release is None:
                raise GitHubApiError(f"Could not resolve version for {repo_id}")

            folders, reqs, mods = await self._install_release(
                spec,
                release,
                record.installed_folders,
            )
            now = _utc_now_iso()
            record.installed_version = release.version
            record.latest_version = release.version
            record.version_kind = release.kind
            record.update_available = False
            record.installed_folders = folders
            record.requirements = reqs
            record.python_modules = mods
            record.etag = release.etag
            record.last_checked = now
            record.last_updated = now
            record.last_error = None

            self._state_store.save(state)
            return record

    async def update_repo(self, repo_id: str) -> RepoRecord:
        """Update a managed repository to its latest resolved version."""
        return await self._install_existing(repo_id)

    async def reinstall_repo(self, repo_id: str) -> RepoRecord:
        """Force reinstall a managed repository cleanly replacing its folders."""
        return await self._install_existing(repo_id)

    async def set_pin(self, repo_id: str, pinned_ref: str | None) -> RepoRecord:
        """Pin or unpin a repository to a specific tag, branch, or SHA."""
        cleaned_ref = pinned_ref.strip() if pinned_ref and pinned_ref.strip() else None
        if cleaned_ref is not None:
            parse_repo_input(f"{repo_id}@{cleaned_ref}")

        async with self._lock:
            state = self._state_store.load()
            record = state.repositories.get(repo_id)
            if record is None:
                raise RepoNotFoundError(
                    f"Repository not managed: {repo_id}",
                    status_code=HTTP_NOT_FOUND,
                )

            spec = RepoSpec(
                owner=record.owner,
                repo=record.repo,
                full_name=record.id,
                pinned_ref=cleaned_ref,
                url=record.url,
            )
            release = await self._github.resolve_version(spec, etag=None)
            if release is None:
                raise GitHubApiError(f"Could not resolve ref for {repo_id}")

            now = _utc_now_iso()
            if release.version != record.installed_version:
                folders, reqs, mods = await self._install_release(
                    spec,
                    release,
                    record.installed_folders,
                )
                record.installed_version = release.version
                record.installed_folders = folders
                record.requirements = reqs
                record.python_modules = mods
                record.last_updated = now

            record.pinned_ref = cleaned_ref
            record.latest_version = release.version
            record.version_kind = release.kind
            record.update_available = False
            record.etag = release.etag
            record.last_checked = now
            record.last_error = None

            self._state_store.save(state)
            return record

    async def set_auto_upd(
        self,
        repo_id: str,
        auto_update: AutoUpdateMode,
    ) -> RepoRecord:
        """Update auto-update mode for a managed repository."""
        async with self._lock:
            state = self._state_store.load()
            record = state.repositories.get(repo_id)
            if record is None:
                raise RepoNotFoundError(
                    f"Repository not managed: {repo_id}",
                    status_code=HTTP_NOT_FOUND,
                )

            record.auto_update = auto_update
            self._state_store.save(state)
            return record

    async def remove_repo(self, repo_id: str) -> None:
        """Remove a repository's installed folders and delete it from state."""
        async with self._lock:
            state = self._state_store.load()
            record = state.repositories.get(repo_id)
            if record is None:
                raise RepoNotFoundError(
                    f"Repository not managed: {repo_id}",
                    status_code=HTTP_NOT_FOUND,
                )

            filesystem.remove_folders(self._config.apps_dir, record.installed_folders)
            del state.repositories[repo_id]
            self._state_store.save(state)

    async def check_updates(self, notify_mode: NotifyMode) -> StoreStatus:
        """Check all repositories for updates and auto-update when enabled."""
        available_updates: list[str] = []

        async with self._lock:
            state = self._state_store.load()
            now = _utc_now_iso()

            for repo_id, record in state.repositories.items():
                spec = _spec_from_record(record)
                try:
                    release = await self._github.resolve_version(spec, etag=record.etag)
                except StoreError as err:
                    logger.exception("Update check failed for %s", repo_id)
                    record.last_error = str(err)
                    record.last_checked = now
                    continue

                record.last_checked = now
                record.last_error = None
                if release is None:
                    continue

                record.latest_version = release.version
                record.version_kind = release.kind
                record.etag = release.etag

                if release.version == record.installed_version:
                    record.update_available = False
                    continue

                if record.auto_update == AutoUpdateMode.ENABLED:
                    try:
                        folders, reqs, mods = await self._install_release(
                            spec,
                            release,
                            record.installed_folders,
                        )
                    except StoreError as err:
                        logger.exception("Auto-update failed for %s", repo_id)
                        record.last_error = str(err)
                        record.update_available = True
                        continue

                    record.installed_version = release.version
                    record.installed_folders = folders
                    record.requirements = reqs
                    record.python_modules = mods
                    record.update_available = False
                    record.last_updated = now
                else:
                    record.update_available = True
                    available_updates.append(
                        f"{repo_id} ({record.installed_version} -> {release.version})"
                    )

            state.last_global_check = now
            self._state_store.save(state)
            status = self._build_status(state)

        if (
            notify_mode == NotifyMode.NOTIFY
            and available_updates
            and self._supervisor.is_available()
        ):
            try:
                await self._supervisor.send_notify(
                    "AppDaemon Store Updates Available",
                    "Updates ready to install:\n- " + "\n- ".join(available_updates),
                    f"{NOTIFY_ID_PREFIX}updates",
                )
            except SupervisorError as err:
                logger.warning("Failed sending update notification: %s", err)

        return status

    async def preview_pkgs(self) -> RequirementSyncResult:
        """Preview missing Python packages against AppDaemon Supervisor options."""
        async with self._lock:
            state = self._state_store.load()
            reqs = _collect_requirements(state)

        return await self._supervisor.preview_packages(reqs)

    async def sync_pkgs(self, restart_mode: RestartMode) -> RequirementSyncResult:
        """Sync missing Python packages to AppDaemon options and optionally restart."""
        async with self._lock:
            state = self._state_store.load()
            reqs = _collect_requirements(state)

        return await self._supervisor.sync_packages(reqs, restart_mode)

    async def apply_migration(self, candidate_id: str) -> StoreStatus:
        """Apply a migration candidate and refresh StoreStatus."""
        async with self._lock:
            state = self._state_store.load()
            candidates = self._migrations.detect_all(
                self._config.apps_dir,
                self._config.appdaemon_yaml_path,
                state,
            )
            target_cand = next((c for c in candidates if c.id == candidate_id), None)

            self._migrations.apply_migration(
                candidate_id,
                self._config.apps_dir,
                self._config.appdaemon_yaml_path,
                state,
            )

            # Switch active apps_dir when migrating away from the current legacy path.
            if (
                candidate_id.startswith(MIG_ID_LEGACY_PREFIX)
                and target_cand is not None
                and target_cand.target_path
                and Path(target_cand.source_path).resolve()
                == self._config.apps_dir.resolve()
            ):
                self._config.apps_dir = Path(target_cand.target_path)
                self._config.config_source = ConfigSource.APPDAEMON_YAML
                self._config.apps_dir.mkdir(parents=True, exist_ok=True)

            self._state_store.save(state)
            return self._build_status(state)

    async def dismiss_mig(self, candidate_id: str) -> StoreStatus:
        """Dismiss a migration candidate and return updated StoreStatus."""
        async with self._lock:
            state = self._state_store.load()
            self._migrations.dismiss(candidate_id, state)
            self._state_store.save(state)
            return self._build_status(state)


def _pick_str(*values: Any) -> str | None:
    """Return the first non-empty stripped string from candidate values."""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()

    return None
