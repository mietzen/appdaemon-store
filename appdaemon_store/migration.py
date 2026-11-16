"""Detect and apply migrations for legacy HACS or conflicting AppDaemon setups."""

from pathlib import Path

from appdaemon_store import filesystem, path_resolver
from appdaemon_store.const import (
    APPDAEMON_CONTAINER_APPS_DIR,
    APPS_SUBDIR_NAME,
    CONTAINER_CONFIG_DIR,
    CONTAINER_HA_DIR,
    DEFAULT_HAOS_ADDON_CONFIG,
    IGNORED_COMMON_NAMES,
    LEGACY_CONFIG_APPS_DIR,
    LEGACY_HACS_APPS_DIR,
    MIG_ID_DUP_PREFIX,
    MIG_ID_LEGACY_PREFIX,
    MIG_ID_UNMANAGED_PREFIX,
    PYCACHE_DIR,
)
from appdaemon_store.exceptions import MigrationError
from appdaemon_store.state_store import StateStore
from appdaemon_store.type_definitions import (
    MigrationCandidate,
    MigrationType,
    StoreState,
)

_LEGACY_APP_DIRS = frozenset({LEGACY_HACS_APPS_DIR, LEGACY_CONFIG_APPS_DIR})


class MigrationService:
    """Service for detecting and resolving legacy or conflicting app directories."""

    def __init__(
        self,
        state_store: StateStore | None = None,
        ha_root: Path = Path(CONTAINER_HA_DIR),
        cfg_root: Path = Path(CONTAINER_CONFIG_DIR),
        addon_root: Path = Path(DEFAULT_HAOS_ADDON_CONFIG),
    ) -> None:
        """Initialize migration service with state store and container path roots."""
        self._state_store = state_store
        self._ha_root = ha_root
        self._cfg_root = cfg_root
        self._addon_root = addon_root

    def detect_all(
        self,
        apps_dir: Path,
        yaml_path: Path,
        state: StoreState,
    ) -> list[MigrationCandidate]:
        """Scan appdaemon.yaml and apps_dir for actionable migration candidates."""
        candidates: list[MigrationCandidate] = []

        legacy = self._detect_legacy(apps_dir, yaml_path, state)
        if legacy is not None:
            candidates.append(legacy)

        candidates.extend(self._detect_dirs(apps_dir, state))
        return candidates

    def _detect_legacy(
        self,
        apps_dir: Path,
        yaml_path: Path,
        state: StoreState,
    ) -> MigrationCandidate | None:
        """Detect a legacy HACS app_dir configured in appdaemon.yaml."""
        raw_app_dir = path_resolver.get_app_dir(yaml_path)
        if not raw_app_dir:
            return None

        normalized = raw_app_dir.rstrip("/")
        if normalized not in _LEGACY_APP_DIRS:
            return None

        candidate_id = f"{MIG_ID_LEGACY_PREFIX}{normalized}"
        if candidate_id in state.dismissed_migrations:
            return None

        source_path = path_resolver.map_legacy_path(
            normalized,
            ha_root=self._ha_root,
            cfg_root=self._cfg_root,
            addon_root=self._addon_root,
        )
        if not source_path.is_dir():
            return None

        target_path = (
            apps_dir
            if apps_dir.resolve() != source_path.resolve()
            else self._addon_root / APPS_SUBDIR_NAME
        )
        if target_path.resolve() == source_path.resolve():
            return None

        items = sorted(
            item.name
            for item in source_path.iterdir()
            if item.name not in IGNORED_COMMON_NAMES and not item.name.startswith(".")
        )
        return MigrationCandidate(
            id=candidate_id,
            migration_type=MigrationType.LEGACY_PATH,
            title="Legacy HACS AppDaemon apps directory detected",
            description=(
                f"appdaemon.yaml points to '{normalized}'. "
                f"Migrate apps and apps.yaml to '{target_path}' and update app_dir."
            ),
            source_path=str(source_path),
            target_path=str(target_path),
            related_repo=None,
            items=items,
        )

    def _collect_managed_modules(
        self,
        apps_dir: Path,
        state: StoreState,
    ) -> dict[str, set[str]]:
        """Map each managed repo ID to its set of Python module filenames."""
        repo_modules: dict[str, set[str]] = {}

        for repo_id, record in state.repositories.items():
            mods = set(record.python_modules)
            if not mods:
                for folder in record.installed_folders:
                    mods.update(filesystem.scan_dir_modules(apps_dir / folder))
            if mods:
                repo_modules[repo_id] = mods

        return repo_modules

    def _find_dup_repo(
        self,
        dir_modules: list[str],
        repo_modules: dict[str, set[str]],
    ) -> tuple[str | None, list[str]]:
        """Find a managed repository sharing Python module filenames with a directory."""
        if not dir_modules:
            return None, []

        dir_set = set(dir_modules)
        for repo_id, managed_mods in repo_modules.items():
            overlap = sorted(dir_set & managed_mods)
            if overlap:
                return repo_id, overlap

        return None, []

    def _detect_dirs(
        self,
        apps_dir: Path,
        state: StoreState,
    ) -> list[MigrationCandidate]:
        """Detect duplicate or unmanaged directories inside apps_dir."""
        if not apps_dir.is_dir():
            return []

        managed_folders = {
            folder
            for record in state.repositories.values()
            for folder in record.installed_folders
        }
        repo_modules = self._collect_managed_modules(apps_dir, state)
        candidates: list[MigrationCandidate] = []

        for entry in sorted(apps_dir.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            if (
                entry.name.startswith(".")
                or entry.name in IGNORED_COMMON_NAMES
                or entry.name == PYCACHE_DIR
            ):
                continue
            if entry.name in managed_folders:
                continue

            dir_modules = filesystem.scan_dir_modules(entry)
            dup_repo, overlap = self._find_dup_repo(dir_modules, repo_modules)
            if dup_repo is not None:
                candidate_id = f"{MIG_ID_DUP_PREFIX}{entry.name}"
                if candidate_id in state.dismissed_migrations:
                    continue
                candidates.append(
                    MigrationCandidate(
                        id=candidate_id,
                        migration_type=MigrationType.DUPLICATE_APP,
                        title=f"Duplicate app directory '{entry.name}'",
                        description=(
                            f"Directory '{entry.name}' contains modules already managed by "
                            f"'{dup_repo}' and may cause AppDaemon conflicts."
                        ),
                        source_path=str(entry),
                        target_path=None,
                        related_repo=dup_repo,
                        items=overlap,
                    )
                )
                continue

            candidate_id = f"{MIG_ID_UNMANAGED_PREFIX}{entry.name}"
            if candidate_id in state.dismissed_migrations:
                continue

            items = (
                dir_modules
                if dir_modules
                else sorted(
                    p.name for p in entry.iterdir() if not p.name.startswith(".")
                )
            )
            candidates.append(
                MigrationCandidate(
                    id=candidate_id,
                    migration_type=MigrationType.UNMANAGED_DIR,
                    title=f"Unmanaged directory '{entry.name}'",
                    description=(
                        f"Directory '{entry.name}' in apps_dir is not tracked by AppDaemon Store."
                    ),
                    source_path=str(entry),
                    target_path=None,
                    related_repo=None,
                    items=items,
                )
            )

        return candidates

    def apply_migration(
        self,
        candidate_id: str,
        apps_dir: Path,
        yaml_path: Path,
        state: StoreState,
    ) -> str:
        """Execute a detected migration candidate safely and return a summary."""
        candidates = self.detect_all(apps_dir, yaml_path, state)
        match = next((item for item in candidates if item.id == candidate_id), None)
        if match is None:
            raise MigrationError(f"Migration candidate not found: {candidate_id}")

        if match.migration_type == MigrationType.LEGACY_PATH:
            return self._apply_legacy(match, apps_dir, yaml_path)

        return self._remove_dir(match, apps_dir)

    def _apply_legacy(
        self,
        candidate: MigrationCandidate,
        apps_dir: Path,
        yaml_path: Path,
    ) -> str:
        """Copy legacy apps to the modern apps_dir and update appdaemon.yaml."""
        src_dir = Path(candidate.source_path)
        dst_dir = Path(candidate.target_path) if candidate.target_path else apps_dir
        copied = filesystem.copy_legacy_apps(src_dir, dst_dir)

        new_yaml_app_dir = self._yaml_app_dir_for(dst_dir)
        path_resolver.set_app_dir_yaml(yaml_path, new_yaml_app_dir)

        return (
            f"Copied {len(copied)} item(s) from {src_dir} to {dst_dir} "
            f"and updated app_dir to {new_yaml_app_dir}"
        )

    def _yaml_app_dir_for(self, dst_dir: Path) -> str:
        """Compute the app_dir value to write into appdaemon.yaml."""
        resolved_dst = dst_dir.resolve()
        resolved_addon = self._addon_root.resolve()

        if resolved_dst.is_relative_to(resolved_addon):
            rel = resolved_dst.relative_to(resolved_addon).as_posix()
            if rel == APPS_SUBDIR_NAME:
                return APPDAEMON_CONTAINER_APPS_DIR
            if rel == ".":
                return CONTAINER_CONFIG_DIR
            return f"/config/{rel}"

        return str(dst_dir)

    def _remove_dir(self, candidate: MigrationCandidate, apps_dir: Path) -> str:
        """Remove a duplicate or unmanaged directory strictly inside apps_dir."""
        target = Path(candidate.source_path)
        if target.parent.resolve() != apps_dir.resolve():
            raise MigrationError(
                f"Refusing to remove directory outside {apps_dir}: {target}"
            )

        filesystem.remove_folders(apps_dir, [target.name])
        return f"Removed directory '{target.name}' from {apps_dir}"

    def dismiss(self, candidate_id: str, state: StoreState) -> None:
        """Mark a migration candidate as dismissed and persist if store is attached."""
        if candidate_id not in state.dismissed_migrations:
            state.dismissed_migrations.append(candidate_id)

        if self._state_store is not None:
            self._state_store.save(state)
