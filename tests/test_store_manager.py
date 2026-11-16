"""Tests for StoreManager service orchestration."""

import io
import json
from pathlib import Path
import tarfile

import httpx
import pytest

from appdaemon_store.const import (
    ENV_APPS_DIR,
    ENV_AUTO_UPDATE,
    ENV_PORT,
    ENV_REPOSITORIES,
    HEADER_ETAG,
    HEADER_IF_NONE_MATCH,
    HTTP_NOT_FOUND,
    HTTP_NOT_MODIFIED,
    HTTP_OK,
)
from appdaemon_store.exceptions import RepoNotFoundError, ValidationError
from appdaemon_store.github_client import GitHubClient
from appdaemon_store.migration import MigrationService
from appdaemon_store.state_store import StateStore
from appdaemon_store.store_manager import StoreManager
from appdaemon_store.supervisor_client import SupervisorClient
from appdaemon_store.type_definitions import (
    AppConfig,
    AutoUpdateMode,
    ConfigSource,
    NotifyMode,
    RestartMode,
    RuntimeMode,
    SeedOrigin,
)


def _tar_bytes(entries: dict[str, bytes]) -> bytes:
    """Create a .tar.gz archive in memory for mock GitHub tarball downloads."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class _FakeGitHub:
    """Configurable GitHub mock transport state for StoreManager tests."""

    def __init__(self) -> None:
        """Initialize mock release versions and tarball payloads."""
        self.version = "v1.0.0"
        self.etag = '"etag-v1"'
        self.req_pkg = "fritzconnection>=1.13.0"
        self.notifications_checked = 0
        self.tarballs: dict[str, bytes] = {
            "v1.0.0": _tar_bytes(
                {
                    "acme-demo-111/apps/demo_app/demo.py": b"ver = '1.0.0'\n",
                    "acme-demo-111/requirements.txt": b"fritzconnection>=1.13.0\n",
                }
            ),
            "v2.0.0": _tar_bytes(
                {
                    "acme-demo-222/apps/demo_app/demo.py": b"ver = '2.0.0'\n",
                    "acme-demo-222/requirements.txt": b"fritzconnection>=1.14.0\nhttpx>=0.27\n",
                }
            ),
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Handle GitHub API and tarball requests."""
        path = request.url.path
        if path == "/repos/acme/demo/releases/latest":
            if request.headers.get(HEADER_IF_NONE_MATCH) == self.etag:
                return httpx.Response(HTTP_NOT_MODIFIED)
            return httpx.Response(
                HTTP_OK,
                headers={HEADER_ETAG: self.etag},
                json={
                    "tag_name": self.version,
                    "tarball_url": f"https://api.github.com/repos/acme/demo/tarball/{self.version}",
                },
            )
        if path == "/repos/acme/demo/commits/v1.0.0":
            return httpx.Response(HTTP_OK, json={"sha": "sha-v1"})
        if path.startswith("/repos/acme/demo/tarball/"):
            ver = path.rsplit("/", maxsplit=1)[-1]
            if ver in self.tarballs:
                return httpx.Response(HTTP_OK, content=self.tarballs[ver])
        return httpx.Response(HTTP_NOT_FOUND)


def _make_manager(
    tmp_path: Path,
    gh_mock: _FakeGitHub,
    *,
    repos: list[str] | None = None,
    auto_update: AutoUpdateMode = AutoUpdateMode.ENABLED,
    sup_transport: httpx.AsyncBaseTransport | None = None,
    sup_token: str | None = None,
) -> StoreManager:
    """Construct a StoreManager wired to tmp_path and mock HTTP transports."""
    apps_dir = tmp_path / "apps"
    yaml_path = tmp_path / "appdaemon.yaml"
    state_file = tmp_path / "state.json"
    config = AppConfig(
        apps_dir=apps_dir,
        config_source=ConfigSource.EXPLICIT,
        appdaemon_yaml_path=yaml_path,
        state_file=state_file,
        repositories=repos or [],
        auto_update=auto_update,
        supervisor_token=sup_token,
    )
    return StoreManager(
        config=config,
        github_client=GitHubClient(transport=httpx.MockTransport(gh_mock.handler)),
        state_store=StateStore(state_file),
        supervisor_client=SupervisorClient(token=sup_token, transport=sup_transport),
        migration_service=MigrationService(
            ha_root=tmp_path / "ha", addon_root=tmp_path / "addon"
        ),
    )


def test_load_config_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify CLI overrides > env vars > options.json precedence."""
    options_file = tmp_path / "options.json"
    options_file.write_text(
        json.dumps(
            {
                "apps_dir": str(tmp_path / "from_options"),
                "repositories": ["opt/repo"],
                "auto_update": False,
                "check_interval_hours": 12,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(ENV_APPS_DIR, str(tmp_path / "from_env"))
    monkeypatch.setenv(ENV_REPOSITORIES, "env/a, env/b")
    monkeypatch.setenv(ENV_AUTO_UPDATE, "enabled")
    monkeypatch.setenv(ENV_PORT, "9001")

    cfg = StoreManager.load_config(
        options_file=options_file,
        overrides={"apps_dir": str(tmp_path / "from_cli")},
    )

    assert cfg.apps_dir == tmp_path / "from_cli"
    assert cfg.config_source == ConfigSource.EXPLICIT
    assert cfg.repositories == ["env/a", "env/b"]
    assert cfg.auto_update == AutoUpdateMode.ENABLED
    assert cfg.check_interval_hours == 12
    assert cfg.port == 9001


async def test_initialize_seeds_repos(tmp_path: Path) -> None:
    """Seed valid configured repos and record error for broken repos cleanly."""
    gh = _FakeGitHub()
    mgr = _make_manager(tmp_path, gh, repos=["acme/demo", "acme/missing"])

    await mgr.initialize()
    status = await mgr.get_status()
    await mgr.aclose()

    assert status.apps_dir_exists
    assert status.runtime_mode == RuntimeMode.STANDALONE
    by_id = {r.id: r for r in status.repositories}
    assert by_id["acme/demo"].origin == SeedOrigin.CONFIG
    assert by_id["acme/demo"].installed_version == "v1.0.0"
    assert (tmp_path / "apps" / "demo_app" / "demo.py").is_file()
    assert by_id["acme/missing"].last_error is not None


async def test_add_update_remove_repo(tmp_path: Path) -> None:
    """Exercise add, check update notification, manual update, and remove."""
    gh = _FakeGitHub()
    sup_calls: list[str] = []

    def sup_handler(request: httpx.Request) -> httpx.Response:
        sup_calls.append(request.url.path)
        return httpx.Response(
            HTTP_OK, json={"data": {"options": {"python_packages": []}}}
        )

    mgr = _make_manager(
        tmp_path,
        gh,
        sup_transport=httpx.MockTransport(sup_handler),
        sup_token="sup-tok",
    )
    await mgr.initialize()

    record = await mgr.add_repo("acme/demo", AutoUpdateMode.DISABLED)
    assert record.installed_version == "v1.0.0"
    assert record.requirements == ["fritzconnection>=1.13.0"]

    with pytest.raises(ValidationError):
        await mgr.add_repo("acme/demo", AutoUpdateMode.DISABLED)

    # Publish v2.0.0 and check updates with NotifyMode.NOTIFY.
    gh.version = "v2.0.0"
    gh.etag = '"etag-v2"'
    status = await mgr.check_updates(NotifyMode.NOTIFY)
    assert status.repositories[0].update_available is True
    assert "/core/api/services/persistent_notification/create" in sup_calls

    # ETag 304 on repeat check preserves update_available.
    status_304 = await mgr.check_updates(NotifyMode.SILENT)
    assert status_304.repositories[0].latest_version == "v2.0.0"

    updated = await mgr.update_repo("acme/demo")
    assert updated.installed_version == "v2.0.0"
    assert updated.update_available is False
    assert "2.0.0" in (tmp_path / "apps" / "demo_app" / "demo.py").read_text(
        encoding="utf-8"
    )

    await mgr.remove_repo("acme/demo")
    assert not (tmp_path / "apps" / "demo_app").exists()
    assert (await mgr.get_status()).repositories == []

    with pytest.raises(RepoNotFoundError):
        await mgr.remove_repo("acme/demo")
    await mgr.aclose()


async def test_check_auto_updates(tmp_path: Path) -> None:
    """Automatically install new release when AutoUpdateMode.ENABLED."""
    gh = _FakeGitHub()
    mgr = _make_manager(tmp_path, gh)
    await mgr.initialize()
    await mgr.add_repo("acme/demo", AutoUpdateMode.ENABLED)

    gh.version = "v2.0.0"
    gh.etag = '"etag-v2"'
    status = await mgr.check_updates(NotifyMode.SILENT)

    assert status.repositories[0].installed_version == "v2.0.0"
    assert status.repositories[0].update_available is False
    assert status.all_requirements == ["fritzconnection>=1.14.0", "httpx>=0.27"]
    await mgr.aclose()


async def test_pin_and_auto_upd_toggle(tmp_path: Path) -> None:
    """Switch installed code on set_pin and toggle auto_update."""
    gh = _FakeGitHub()
    gh.version = "v2.0.0"
    gh.etag = '"etag-v2"'
    mgr = _make_manager(tmp_path, gh)
    await mgr.initialize()
    await mgr.add_repo("acme/demo", AutoUpdateMode.ENABLED)

    pinned = await mgr.set_pin("acme/demo", "v1.0.0")
    assert pinned.pinned_ref == "v1.0.0"
    assert pinned.installed_version == "v1.0.0"

    toggled = await mgr.set_auto_upd("acme/demo", AutoUpdateMode.DISABLED)
    assert toggled.auto_update == AutoUpdateMode.DISABLED

    unpinned = await mgr.set_pin("acme/demo", None)
    assert unpinned.pinned_ref is None
    assert unpinned.installed_version == "v2.0.0"
    await mgr.aclose()


async def test_preview_and_sync_pkgs(tmp_path: Path) -> None:
    """Aggregate repo requirements and sync to Supervisor add-on options."""
    gh = _FakeGitHub()

    def sup_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                HTTP_OK, json={"data": {"options": {"python_packages": []}}}
            )
        return httpx.Response(HTTP_OK, json={"result": "ok"})

    mgr = _make_manager(
        tmp_path,
        gh,
        sup_transport=httpx.MockTransport(sup_handler),
        sup_token="sup-tok",
    )
    await mgr.initialize()
    await mgr.add_repo("acme/demo", AutoUpdateMode.ENABLED)

    preview = await mgr.preview_pkgs()
    assert preview.missing_packages == ["fritzconnection>=1.13.0"]

    synced = await mgr.sync_pkgs(RestartMode.RESTART)
    assert synced.added_packages == ["fritzconnection>=1.13.0"]
    assert synced.restarted is True
    await mgr.aclose()


async def test_migration_via_manager(tmp_path: Path) -> None:
    """Detect, apply, and dismiss migrations through StoreManager."""
    gh = _FakeGitHub()
    mgr = _make_manager(tmp_path, gh)
    await mgr.initialize()
    (tmp_path / "apps" / "unmanaged_one").mkdir()
    (tmp_path / "apps" / "unmanaged_one" / "u1.py").write_text("", encoding="utf-8")
    (tmp_path / "apps" / "unmanaged_two").mkdir()
    (tmp_path / "apps" / "unmanaged_two" / "u2.py").write_text("", encoding="utf-8")

    status = await mgr.get_status()
    assert len(status.migrations) == 2

    after_dismiss = await mgr.dismiss_mig(status.migrations[0].id)
    assert len(after_dismiss.migrations) == 1

    after_apply = await mgr.apply_migration(after_dismiss.migrations[0].id)
    assert after_apply.migrations == []
    assert not (tmp_path / "apps" / "unmanaged_two").exists()
    await mgr.aclose()
