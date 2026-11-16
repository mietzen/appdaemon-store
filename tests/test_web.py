"""Tests for FastAPI web backend, Ingress UI rendering, and REST API."""

import asyncio
import io
import json
from pathlib import Path
import tarfile
from typing import Any

from fastapi.testclient import TestClient
import httpx
import pytest

from appdaemon_store.const import (
    HEADER_ETAG,
    HEADER_IF_NONE_MATCH,
    HEADER_INGRESS_PATH,
    HTTP_BAD_REQUEST,
    HTTP_FORBIDDEN,
    HTTP_INTERNAL_ERROR,
    HTTP_NOT_FOUND,
    HTTP_NOT_MODIFIED,
    HTTP_OK,
)
from appdaemon_store.exceptions import StoreError
from appdaemon_store.github_client import GitHubClient
from appdaemon_store.migration import MigrationService
from appdaemon_store.state_store import StateStore
from appdaemon_store.store_manager import StoreManager
from appdaemon_store.supervisor_client import SupervisorClient
from appdaemon_store.type_definitions import (
    AppConfig,
    AutoUpdateMode,
    ConfigSource,
    RuntimeMode,
    SchedulerMode,
)
from appdaemon_store.web import create_app


def _tar_bytes(entries: dict[str, bytes]) -> bytes:
    """Create an in-memory .tar.gz payload for mocked tarball downloads."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class _FakeGitHub:
    """Configurable mock GitHub transport for web API tests."""

    def __init__(self) -> None:
        """Initialize release state and mock tarballs."""
        self.version = "v1.0.0"
        self.etag = '"etag-v1"'
        self.fail_status: int | None = None
        self.tarballs: dict[str, bytes] = {
            "v1.0.0": _tar_bytes(
                {
                    "acme-demo-111/apps/demo_app/demo.py": b"ver = '1.0.0'\n",
                    "acme-demo-111/requirements.txt": b"httpx>=0.27\n",
                }
            ),
            "v2.0.0": _tar_bytes(
                {
                    "acme-demo-222/apps/demo_app/demo.py": b"ver = '2.0.0'\n",
                    "acme-demo-222/requirements.txt": b"httpx>=0.27\npyyaml>=6.0\n",
                }
            ),
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Respond to mocked GitHub release, commit, and tarball requests."""
        if self.fail_status is not None:
            return httpx.Response(self.fail_status, text="rate limited")

        path = request.url.path
        if path == "/repos/acme/demo/releases/latest":
            if request.headers.get(HEADER_IF_NONE_MATCH) == self.etag:
                return httpx.Response(HTTP_NOT_MODIFIED)
            return httpx.Response(
                HTTP_OK,
                headers={HEADER_ETAG: self.etag},
                json={
                    "tag_name": self.version,
                    "tarball_url": (
                        f"https://api.github.com/repos/acme/demo/tarball/{self.version}"
                    ),
                },
            )
        if path == "/repos/acme/demo/commits/v1.0.0":
            return httpx.Response(HTTP_OK, json={"sha": "sha-v1"})
        if path.startswith("/repos/acme/demo/tarball/"):
            ver = path.rsplit("/", maxsplit=1)[-1]
            if ver in self.tarballs:
                return httpx.Response(HTTP_OK, content=self.tarballs[ver])
        return httpx.Response(HTTP_NOT_FOUND)


def _make_mgr(
    tmp_path: Path,
    gh_mock: _FakeGitHub,
    *,
    sup_transport: httpx.AsyncBaseTransport | None = None,
    sup_token: str | None = None,
    check_hours: int = 6,
) -> StoreManager:
    """Create a StoreManager bound to tmp_path and mock HTTP transports."""
    apps_dir = tmp_path / "apps"
    yaml_path = tmp_path / "appdaemon.yaml"
    state_file = tmp_path / "state.json"
    config = AppConfig(
        apps_dir=apps_dir,
        config_source=ConfigSource.EXPLICIT,
        appdaemon_yaml_path=yaml_path,
        state_file=state_file,
        auto_update=AutoUpdateMode.DISABLED,
        check_interval_hours=check_hours,
        supervisor_token=sup_token,
    )
    return StoreManager(
        config=config,
        github_client=GitHubClient(transport=httpx.MockTransport(gh_mock.handler)),
        state_store=StateStore(state_file),
        supervisor_client=SupervisorClient(token=sup_token, transport=sup_transport),
        migration_service=MigrationService(
            ha_root=tmp_path / "ha",
            addon_root=tmp_path / "addon",
        ),
    )


def test_index_standalone(tmp_path: Path) -> None:
    """Render standalone HTML with empty data-base and root asset paths."""
    mgr = _make_mgr(tmp_path, _FakeGitHub())
    app = create_app(mgr, SchedulerMode.DISABLED)

    with TestClient(app) as client:
        resp = client.get("/")

    assert resp.status_code == HTTP_OK
    assert 'data-base=""' in resp.text
    assert 'href="/static/style.css"' in resp.text
    assert 'src="/static/app.js"' in resp.text


def test_index_ingress_hdr(tmp_path: Path) -> None:
    """Render Ingress HTML using X-Ingress-Path for data-base and assets."""
    mgr = _make_mgr(tmp_path, _FakeGitHub())
    app = create_app(mgr, SchedulerMode.DISABLED)

    with TestClient(app) as client:
        resp = client.get(
            "/",
            headers={HEADER_INGRESS_PATH: "/api/hassio_ingress/tok123/"},
        )

    assert resp.status_code == HTTP_OK
    assert 'data-base="/api/hassio_ingress/tok123"' in resp.text
    assert 'href="/api/hassio_ingress/tok123/static/style.css"' in resp.text
    assert 'src="/api/hassio_ingress/tok123/static/app.js"' in resp.text


def test_static_assets(tmp_path: Path) -> None:
    """Serve packaged static CSS and JavaScript assets."""
    mgr = _make_mgr(tmp_path, _FakeGitHub())
    app = create_app(mgr, SchedulerMode.DISABLED)

    with TestClient(app) as client:
        css_resp = client.get("/static/style.css")
        js_resp = client.get("/static/app.js")

    assert css_resp.status_code == HTTP_OK
    assert js_resp.status_code == HTTP_OK
    assert "AppDaemon" in css_resp.text or "--ha-primary" in css_resp.text
    assert "document.body.dataset.base" in js_resp.text


def test_health_and_status(tmp_path: Path) -> None:
    """Return health liveness check and initial StoreStatus snapshot."""
    mgr = _make_mgr(tmp_path, _FakeGitHub())
    app = create_app(mgr, SchedulerMode.DISABLED)

    with TestClient(app) as client:
        health = client.get("/api/health")
        status = client.get("/api/status")

    assert health.status_code == HTTP_OK
    assert health.json() == {"status": "ok"}
    assert status.status_code == HTTP_OK
    payload = status.json()
    assert payload["runtime_mode"] == RuntimeMode.STANDALONE
    assert payload["apps_dir"] == str(tmp_path / "apps")
    assert payload["repositories"] == []


def test_repo_crud_and_errors(tmp_path: Path) -> None:
    """Verify repository add, duplicate 400, unknown 404, and delete."""
    mgr = _make_mgr(tmp_path, _FakeGitHub())
    app = create_app(mgr, SchedulerMode.DISABLED)

    with TestClient(app) as client:
        created = client.post(
            "/api/repos",
            json={"repo": "acme/demo", "auto_update": True},
        )
        assert created.status_code == HTTP_OK
        assert created.json()["id"] == "acme/demo"
        assert created.json()["auto_update"] == AutoUpdateMode.ENABLED

        duplicate = client.post("/api/repos", json={"repo": "acme/demo"})
        assert duplicate.status_code == HTTP_BAD_REQUEST
        assert "already managed" in duplicate.json()["detail"]

        invalid = client.post("/api/repos", json={"repo": "not-valid"})
        assert invalid.status_code == HTTP_BAD_REQUEST

        missing = client.post("/api/repos/acme/unknown/update")
        assert missing.status_code == HTTP_NOT_FOUND

        removed = client.delete("/api/repos/acme/demo")
        assert removed.status_code == HTTP_OK
        assert removed.json() == {"removed": "acme/demo"}

        missing_del = client.delete("/api/repos/acme/demo")
        assert missing_del.status_code == HTTP_NOT_FOUND


def test_reinstall_pin_autoupd(tmp_path: Path) -> None:
    """Exercise update, reinstall, pin/unpin, and auto-update toggle."""
    gh = _FakeGitHub()
    gh.version = "v2.0.0"
    gh.etag = '"etag-v2"'
    mgr = _make_mgr(tmp_path, gh)
    app = create_app(mgr, SchedulerMode.DISABLED)

    with TestClient(app) as client:
        add_resp = client.post(
            "/api/repos",
            json={"repo": "acme/demo", "auto_update": "disabled"},
        )
        assert add_resp.status_code == HTTP_OK
        assert add_resp.json()["installed_version"] == "v2.0.0"

        pin_resp = client.post(
            "/api/repos/acme/demo/pin",
            json={"pinned_ref": "v1.0.0"},
        )
        assert pin_resp.status_code == HTTP_OK
        assert pin_resp.json()["pinned_ref"] == "v1.0.0"
        assert pin_resp.json()["installed_version"] == "v1.0.0"

        reinstall_resp = client.post("/api/repos/acme/demo/reinstall")
        assert reinstall_resp.status_code == HTTP_OK
        assert reinstall_resp.json()["installed_version"] == "v1.0.0"

        unpin_resp = client.post("/api/repos/acme/demo/pin", json={})
        assert unpin_resp.status_code == HTTP_OK
        assert unpin_resp.json()["pinned_ref"] is None
        assert unpin_resp.json()["installed_version"] == "v2.0.0"

        auto_resp = client.post(
            "/api/repos/acme/demo/auto-update",
            json={"auto_update": True},
        )
        assert auto_resp.status_code == HTTP_OK
        assert auto_resp.json()["auto_update"] == AutoUpdateMode.ENABLED

        upd_resp = client.post("/api/repos/acme/demo/update")
        assert upd_resp.status_code == HTTP_OK


def test_check_and_reqs_sync(tmp_path: Path) -> None:
    """Check updates endpoint plus requirement preview and Supervisor sync."""
    gh = _FakeGitHub()
    sup_calls: list[tuple[str, str, Any]] = []

    def sup_handler(request: httpx.Request) -> httpx.Response:
        """Respond to mocked Supervisor add-on option and restart requests."""
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        sup_calls.append((request.method, request.url.path, body))
        if request.method == "GET":
            return httpx.Response(
                HTTP_OK,
                json={"data": {"options": {"python_packages": []}}},
            )
        return httpx.Response(HTTP_OK, json={"result": "ok"})

    mgr = _make_mgr(
        tmp_path,
        gh,
        sup_transport=httpx.MockTransport(sup_handler),
        sup_token="sup-token",
    )
    app = create_app(mgr, SchedulerMode.DISABLED)

    with TestClient(app) as client:
        client.post("/api/repos", json={"repo": "acme/demo", "auto_update": False})

        gh.version = "v2.0.0"
        gh.etag = '"etag-v2"'
        check_resp = client.post("/api/check-updates")
        assert check_resp.status_code == HTTP_OK
        assert check_resp.json()["repositories"][0]["update_available"] is True

        preview_resp = client.get("/api/requirements/preview")
        assert preview_resp.status_code == HTTP_OK
        assert preview_resp.json()["missing_packages"] == ["httpx>=0.27"]

        sync_resp = client.post(
            "/api/requirements/sync",
            json={"restart": False},
        )
        assert sync_resp.status_code == HTTP_OK
        assert sync_resp.json()["added_packages"] == ["httpx>=0.27"]
        assert sync_resp.json()["restarted"] is False


def test_migrations_endpoints(tmp_path: Path) -> None:
    """Apply and dismiss migration candidates via REST API."""
    mgr = _make_mgr(tmp_path, _FakeGitHub())
    app = create_app(mgr, SchedulerMode.DISABLED)

    with TestClient(app) as client:
        apps_dir = tmp_path / "apps"
        (apps_dir / "old_one").mkdir()
        (apps_dir / "old_one" / "one.py").write_text("", encoding="utf-8")
        (apps_dir / "old_two").mkdir()
        (apps_dir / "old_two" / "two.py").write_text("", encoding="utf-8")

        status = client.get("/api/status").json()
        assert len(status["migrations"]) == 2
        first_id = status["migrations"][0]["id"]
        second_id = status["migrations"][1]["id"]

        dismissed = client.post(
            "/api/migrations/dismiss",
            json={"candidate_id": first_id},
        )
        assert dismissed.status_code == HTTP_OK
        assert len(dismissed.json()["migrations"]) == 1

        applied = client.post(
            "/api/migrations/apply",
            json={"candidate_id": second_id},
        )
        assert applied.status_code == HTTP_OK
        assert applied.json()["migrations"] == []
        assert not (apps_dir / "old_two").exists()

        bad_apply = client.post(
            "/api/migrations/apply",
            json={"candidate_id": "missing:id"},
        )
        assert bad_apply.status_code == HTTP_BAD_REQUEST


async def test_scheduler_and_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify background scheduler runs due checks and StoreError maps cleanly."""
    gh = _FakeGitHub()
    mgr = _make_mgr(tmp_path, gh, check_hours=1)
    await mgr.initialize()
    await mgr.add_repo("acme/demo", AutoUpdateMode.DISABLED)
    await mgr.aclose()

    # Force last_global_check to look stale and use a tiny poll interval.
    state_store = StateStore(tmp_path / "state.json")
    state = state_store.load()
    state.last_global_check = "2020-01-01T00:00:00+00:00"
    state_store.save(state)

    gh.version = "v2.0.0"
    gh.etag = '"etag-v2"'
    monkeypatch.setattr("appdaemon_store.web.DEFAULT_SCHEDULER_SLEEP_SECONDS", 0.02)

    app = create_app(mgr, SchedulerMode.ENABLED)

    @app.get("/api/test-store-err")
    async def _raise_store_err() -> None:
        """Raise StoreError for error-handler verification."""
        raise StoreError("boom")

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.08)
        status = await mgr.get_status()
        assert status.repositories[0].latest_version == "v2.0.0"
        assert status.repositories[0].update_available is True

    # Verify GitHubApiError 403 and generic StoreError 500 mappings.
    gh.fail_status = HTTP_FORBIDDEN
    err_app = create_app(_make_mgr(tmp_path / "sub", gh), SchedulerMode.DISABLED)

    @err_app.get("/api/test-store-err")
    async def _raise_generic() -> None:
        """Raise generic StoreError for HTTP 500 verification."""
        raise StoreError("generic failure")

    with TestClient(err_app) as client:
        gh_err = client.post("/api/repos", json={"repo": "acme/demo"})
        assert gh_err.status_code == HTTP_FORBIDDEN
        assert "detail" in gh_err.json()

        store_err = client.get("/api/test-store-err")
        assert store_err.status_code == HTTP_INTERNAL_ERROR
        assert store_err.json() == {"detail": "generic failure"}
