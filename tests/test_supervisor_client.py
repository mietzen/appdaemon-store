"""Tests for SupervisorClient package syncing and notifications."""

import json

import httpx
import pytest

from appdaemon_store.const import HTTP_INTERNAL_ERROR, HTTP_OK
from appdaemon_store.exceptions import SupervisorError
from appdaemon_store.supervisor_client import SupervisorClient
from appdaemon_store.type_definitions import RestartMode


async def test_unavailable_no_token() -> None:
    """Return available=False without HTTP requests when token is missing."""
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(HTTP_OK)

    client = SupervisorClient(token=None, transport=httpx.MockTransport(handler))
    async with client:
        assert not client.is_available()
        preview = await client.preview_packages(["httpx"])
        synced = await client.sync_packages(["httpx"], RestartMode.RESTART)
        await client.send_notify("Title", "Msg", "id1")

    assert not called
    assert not preview.available
    assert preview.missing_packages == ["httpx"]
    assert not synced.available


async def test_preview_packages_diff() -> None:
    """Normalize package names and detect missing requirements."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sup-tok"
        return httpx.Response(
            HTTP_OK,
            json={
                "result": "ok",
                "data": {
                    "options": {
                        "python_packages": ["PyYAML", "my_lib>=1.0"],
                        "system_packages": ["gcc"],
                    }
                },
            },
        )

    client = SupervisorClient(token="sup-tok", transport=httpx.MockTransport(handler))
    async with client:
        result = await client.preview_packages(
            [
                "pyyaml>=6.0",
                "my-lib",
                "fritzconnection>=1.13.0",
                "fritzconnection>=1.13.0",
            ]
        )

    assert result.available
    assert result.current_packages == ["PyYAML", "my_lib>=1.0"]
    assert result.missing_packages == ["fritzconnection>=1.13.0"]


async def test_sync_pkgs_no_restart() -> None:
    """Update python_packages options without restarting when NO_RESTART."""
    calls: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.method == "GET":
            return httpx.Response(
                HTTP_OK,
                json={
                    "data": {
                        "options": {"python_packages": ["pyyaml"], "init_commands": []}
                    }
                },
            )
        return httpx.Response(HTTP_OK, json={"result": "ok"})

    client = SupervisorClient(token="tok", transport=httpx.MockTransport(handler))
    async with client:
        result = await client.sync_packages(
            ["pyyaml", "httpx>=0.27"], RestartMode.NO_RESTART
        )

    assert result.added_packages == ["httpx>=0.27"]
    assert not result.restarted
    assert [c[:2] for c in calls] == [
        ("GET", "/addons/a0d7b954_appdaemon/info"),
        ("POST", "/addons/a0d7b954_appdaemon/options"),
    ]
    assert calls[1][2] == {
        "options": {"python_packages": ["pyyaml", "httpx>=0.27"], "init_commands": []}
    }


async def test_sync_pkgs_with_restart() -> None:
    """Post options and restart add-on when RestartMode.RESTART is selected."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(f"{request.method} {request.url.path}")
        if request.method == "GET":
            return httpx.Response(
                HTTP_OK, json={"data": {"options": {"python_packages": []}}}
            )
        return httpx.Response(HTTP_OK, json={"result": "ok"})

    client = SupervisorClient(token="tok", transport=httpx.MockTransport(handler))
    async with client:
        result = await client.sync_packages(["requests"], RestartMode.RESTART)

    assert result.restarted
    assert result.added_packages == ["requests"]
    assert paths == [
        "GET /addons/a0d7b954_appdaemon/info",
        "POST /addons/a0d7b954_appdaemon/options",
        "POST /addons/a0d7b954_appdaemon/restart",
    ]


async def test_sync_pkgs_none_missing() -> None:
    """Skip POST calls when all required packages are already present."""
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            HTTP_OK,
            json={"data": {"options": {"python_packages": ["requests>=2.30"]}}},
        )

    client = SupervisorClient(token="tok", transport=httpx.MockTransport(handler))
    async with client:
        result = await client.sync_packages(["requests"], RestartMode.RESTART)

    assert methods == ["GET"]
    assert result.added_packages == []
    assert not result.restarted


async def test_send_notify_and_error() -> None:
    """Send Home Assistant notification and raise SupervisorError on 500."""
    sent_bodies: list[dict[str, str]] = []

    def ok_handler(request: httpx.Request) -> httpx.Response:
        sent_bodies.append(json.loads(request.content))
        return httpx.Response(HTTP_OK, json=[])

    client = SupervisorClient(token="tok", transport=httpx.MockTransport(ok_handler))
    async with client:
        await client.send_notify("Updates", "New version", "ad_upd")

    assert sent_bodies == [
        {"title": "Updates", "message": "New version", "notification_id": "ad_upd"}
    ]

    err_client = SupervisorClient(
        token="tok",
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(HTTP_INTERNAL_ERROR, text="boom")
        ),
    )
    async with err_client:
        with pytest.raises(SupervisorError):
            await err_client.get_addon_info()
