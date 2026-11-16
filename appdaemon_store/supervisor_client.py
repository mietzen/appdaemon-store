"""Home Assistant Supervisor API driver for package syncing and alerts."""

import re
from typing import Any

import httpx

from appdaemon_store.const import (
    ADDON_DATA_KEY,
    ADDON_OPTIONS_KEY,
    ADDON_PYTHON_PKGS_KEY,
    APPDAEMON_ADDON_SLUG,
    DEFAULT_SUPERVISOR_URL,
    HEADER_AUTHORIZATION,
    HTTP_BAD_REQUEST,
    HTTP_TIMEOUT_SECONDS,
    SUPERVISOR_ADDON_INFO_FMT,
    SUPERVISOR_ADDON_OPTS_FMT,
    SUPERVISOR_ADDON_RESTART_FMT,
    SUPERVISOR_NOTIFY_PATH,
)
from appdaemon_store.exceptions import SupervisorError
from appdaemon_store.type_definitions import RequirementSyncResult, RestartMode

_PKG_SPLIT_RE = re.compile(r"[<>=!~;\[\s@]")


def _pkg_key(spec: str) -> str:
    """Normalize a PEP 508 requirement string to its lowercase PEP 503 name."""
    base = _PKG_SPLIT_RE.split(spec.strip(), maxsplit=1)[0]
    return base.lower().replace("_", "-")


def _dedupe_reqs(packages: list[str]) -> list[str]:
    """Deduplicate package specifications by normalized base name preserving order."""
    seen: set[str] = set()
    unique: list[str] = []

    for pkg in packages:
        cleaned = pkg.strip()
        if not cleaned:
            continue
        key = _pkg_key(cleaned)
        if key and key not in seen:
            seen.add(key)
            unique.append(cleaned)

    return unique


class SupervisorClient:
    """Async client for Home Assistant Supervisor add-on and notification APIs."""

    def __init__(
        self,
        base_url: str = DEFAULT_SUPERVISOR_URL,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = HTTP_TIMEOUT_SECONDS,
    ) -> None:
        """Initialize the Supervisor client with URL, token, and optional transport."""
        self._base_url = base_url.rstrip("/")
        self._token = token.strip() if token and token.strip() else None
        self._transport = transport
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "SupervisorClient":
        """Enter async context manager."""
        self._get_client()
        return self

    async def __aexit__(self, *_args: object) -> None:
        """Close underlying HTTP client on context exit."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close the internal httpx.AsyncClient if open."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def is_available(self) -> bool:
        """Return whether a Supervisor API token is configured."""
        return self._token is not None

    def _get_client(self) -> httpx.AsyncClient:
        """Return or lazily create the shared httpx.AsyncClient."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout,
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        """Build authenticated Supervisor request headers."""
        if not self._token:
            raise SupervisorError("Supervisor token is not configured")

        return {HEADER_AUTHORIZATION: f"Bearer {self._token}"}

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a Supervisor HTTP request and return its JSON mapping."""
        url = f"{self._base_url}{path}"
        headers = self._headers()

        try:
            resp = await self._get_client().request(
                method,
                url,
                headers=headers,
                json=json_body,
            )
        except httpx.HTTPError as err:
            raise SupervisorError(
                f"Supervisor request failed ({method} {path}): {err}"
            ) from err

        if resp.status_code >= HTTP_BAD_REQUEST:
            raise SupervisorError(
                f"Supervisor API error {resp.status_code} on {path}: {resp.text}"
            )

        if not resp.content:
            return {}

        try:
            payload = resp.json()
        except ValueError as err:
            raise SupervisorError(
                f"Invalid Supervisor JSON from {path}: {err}"
            ) from err

        return payload if isinstance(payload, dict) else {}

    async def get_addon_info(self, slug: str = APPDAEMON_ADDON_SLUG) -> dict[str, Any]:
        """Fetch add-on metadata and options from Supervisor."""
        payload = await self._request("GET", SUPERVISOR_ADDON_INFO_FMT % slug)
        data = payload.get(ADDON_DATA_KEY)
        if isinstance(data, dict):
            return data

        return payload

    def _extract_packages(
        self, addon_info: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        """Extract options mapping and current python_packages list."""
        raw_options = addon_info.get(ADDON_OPTIONS_KEY)
        options = dict(raw_options) if isinstance(raw_options, dict) else {}
        raw_pkgs = options.get(ADDON_PYTHON_PKGS_KEY)
        if not isinstance(raw_pkgs, list):
            return options, []

        return options, [str(pkg) for pkg in raw_pkgs if str(pkg).strip()]

    async def preview_packages(
        self,
        packages: list[str],
        slug: str = APPDAEMON_ADDON_SLUG,
    ) -> RequirementSyncResult:
        """Compare required packages against AppDaemon add-on options without modifying."""
        required = _dedupe_reqs(packages)
        if not self.is_available():
            return RequirementSyncResult(
                available=False,
                required_packages=required,
                missing_packages=list(required),
                message="Supervisor API unavailable",
            )

        addon_info = await self.get_addon_info(slug)
        _, current = self._extract_packages(addon_info)
        current_keys = {_pkg_key(pkg) for pkg in current}
        missing = [req for req in required if _pkg_key(req) not in current_keys]

        if missing:
            msg = (
                f"{len(missing)} package(s) missing from AppDaemon add-on configuration"
            )
        else:
            msg = "All required packages are already configured"

        return RequirementSyncResult(
            available=True,
            current_packages=current,
            required_packages=required,
            missing_packages=missing,
            message=msg,
        )

    async def sync_packages(
        self,
        packages: list[str],
        restart_mode: RestartMode,
        slug: str = APPDAEMON_ADDON_SLUG,
    ) -> RequirementSyncResult:
        """Add missing python_packages to AppDaemon add-on options and optionally restart."""
        required = _dedupe_reqs(packages)
        if not self.is_available():
            return RequirementSyncResult(
                available=False,
                required_packages=required,
                missing_packages=list(required),
                message="Supervisor API unavailable",
            )

        addon_info = await self.get_addon_info(slug)
        options, current = self._extract_packages(addon_info)
        current_keys = {_pkg_key(pkg) for pkg in current}
        missing = [req for req in required if _pkg_key(req) not in current_keys]

        if not missing:
            return RequirementSyncResult(
                available=True,
                current_packages=current,
                required_packages=required,
                missing_packages=[],
                added_packages=[],
                restarted=False,
                message="All required packages are already configured",
            )

        updated = [*current, *missing]
        options[ADDON_PYTHON_PKGS_KEY] = updated
        await self._request(
            "POST",
            SUPERVISOR_ADDON_OPTS_FMT % slug,
            json_body={ADDON_OPTIONS_KEY: options},
        )

        restarted = False
        if restart_mode == RestartMode.RESTART:
            await self._request("POST", SUPERVISOR_ADDON_RESTART_FMT % slug)
            restarted = True

        msg = (
            f"Added {len(missing)} package(s) and restarted AppDaemon"
            if restarted
            else f"Added {len(missing)} package(s) to AppDaemon options"
        )
        return RequirementSyncResult(
            available=True,
            current_packages=updated,
            required_packages=required,
            missing_packages=[],
            added_packages=missing,
            restarted=restarted,
            message=msg,
        )

    async def send_notify(self, title: str, message: str, notify_id: str) -> None:
        """Create a persistent notification in Home Assistant Core."""
        if not self.is_available():
            return

        await self._request(
            "POST",
            SUPERVISOR_NOTIFY_PATH,
            json_body={
                "title": title,
                "message": message,
                "notification_id": notify_id,
            },
        )
