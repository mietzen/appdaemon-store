"""FastAPI web backend and Home Assistant Ingress UI for AppDaemon Store."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from appdaemon_store.const import (
    API_PREFIX,
    APP_NAME,
    APP_VERSION,
    DEFAULT_SCHEDULER_SLEEP_SECONDS,
    HEADER_INGRESS_PATH,
    HTTP_BAD_REQUEST,
    HTTP_INTERNAL_ERROR,
    HTTP_NOT_FOUND,
    INDEX_TEMPLATE_NAME,
    STATIC_DIR_NAME,
    STATIC_URL_PATH,
    TEMPLATES_DIR_NAME,
)
from appdaemon_store.exceptions import (
    MigrationError,
    RepoNotFoundError,
    StoreError,
    ValidationError,
)
from appdaemon_store.store_manager import StoreManager
from appdaemon_store.type_definitions import (
    AutoUpdateMode,
    NotifyMode,
    RepoRecord,
    RequirementSyncResult,
    RestartMode,
    SchedulerMode,
    StoreStatus,
)

logger = logging.getLogger(__name__)

_PACKAGE_DIR = Path(__file__).resolve().parent


class _AddRepoBody(BaseModel):
    """Request payload for installing a GitHub repository."""

    repo: str
    auto_update: AutoUpdateMode | bool = AutoUpdateMode.DISABLED

    def to_mode(self) -> AutoUpdateMode:
        """Convert boolean or enum field value into AutoUpdateMode."""
        if isinstance(self.auto_update, AutoUpdateMode):
            return self.auto_update

        return AutoUpdateMode.ENABLED if self.auto_update else AutoUpdateMode.DISABLED


class _PinRepoBody(BaseModel):
    """Request payload for pinning or unpinning a repository ref."""

    pinned_ref: str | None = None


class _AutoUpdateBody(BaseModel):
    """Request payload for toggling repository auto-update mode."""

    auto_update: AutoUpdateMode | bool

    def to_mode(self) -> AutoUpdateMode:
        """Convert boolean or enum field value into AutoUpdateMode."""
        if isinstance(self.auto_update, AutoUpdateMode):
            return self.auto_update

        return AutoUpdateMode.ENABLED if self.auto_update else AutoUpdateMode.DISABLED


class _SyncBody(BaseModel):
    """Request payload for syncing AppDaemon Python packages."""

    restart: RestartMode | bool = RestartMode.RESTART

    def to_mode(self) -> RestartMode:
        """Convert boolean or enum field value into RestartMode."""
        if isinstance(self.restart, RestartMode):
            return self.restart

        return RestartMode.RESTART if self.restart else RestartMode.NO_RESTART


class _MigrationBody(BaseModel):
    """Request payload identifying a migration candidate."""

    candidate_id: str


def _extract_base_path(request: Request) -> str:
    """Read and sanitize the Home Assistant Ingress prefix header."""
    raw = request.headers.get(HEADER_INGRESS_PATH, "").strip().rstrip("/")
    if not raw:
        return ""

    # Reject protocol-relative or non-root paths to keep template URLs safe.
    if not raw.startswith("/") or raw.startswith("//"):
        return ""

    return raw


def _is_update_due(status: StoreStatus) -> bool:
    """Return whether a periodic background update check should run now."""
    if not status.repositories or status.check_interval_hours <= 0:
        return False

    if not status.last_global_check:
        return True

    try:
        last_dt = datetime.fromisoformat(status.last_global_check)
    except ValueError:
        return True

    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=UTC)

    interval = timedelta(hours=status.check_interval_hours)
    return datetime.now(UTC) - last_dt >= interval


async def _scheduler_loop(manager: StoreManager, poll_seconds: float) -> None:
    """Periodically poll store state and trigger due update checks."""
    while True:
        await asyncio.sleep(poll_seconds)
        try:
            status = await manager.get_status()
            if _is_update_due(status):
                logger.info("Running scheduled repository update check")
                await manager.check_updates(NotifyMode.NOTIFY)
        except asyncio.CancelledError:
            raise
        except StoreError as err:
            logger.warning("Scheduled update check failed: %s", err)
        except Exception:
            logger.exception("Unexpected error in background update scheduler")


def _resolve_err_status(exc: StoreError) -> int:
    """Extract a valid HTTP error status from a StoreError when present."""
    raw_code = getattr(exc, "status_code", None)
    if isinstance(raw_code, int) and raw_code >= HTTP_BAD_REQUEST:
        return raw_code

    return HTTP_INTERNAL_ERROR


def _register_err_handlers(app: FastAPI) -> None:
    """Register domain exception handlers returning JSON detail payloads."""

    @app.exception_handler(RepoNotFoundError)
    async def _on_not_found(_req: Request, exc: RepoNotFoundError) -> JSONResponse:
        """Return HTTP 404 when a repository or ref is not found."""
        return JSONResponse(status_code=HTTP_NOT_FOUND, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def _on_validation(_req: Request, exc: ValidationError) -> JSONResponse:
        """Return HTTP 400 when input or state validation fails."""
        return JSONResponse(status_code=HTTP_BAD_REQUEST, content={"detail": str(exc)})

    @app.exception_handler(MigrationError)
    async def _on_migration(_req: Request, exc: MigrationError) -> JSONResponse:
        """Return HTTP 400 when applying a migration fails."""
        return JSONResponse(status_code=HTTP_BAD_REQUEST, content={"detail": str(exc)})

    @app.exception_handler(StoreError)
    async def _on_store_err(_req: Request, exc: StoreError) -> JSONResponse:
        """Return domain status code or HTTP 500 for unexpected store errors."""
        return JSONResponse(
            status_code=_resolve_err_status(exc),
            content={"detail": str(exc)},
        )


def create_app(
    manager: StoreManager,
    scheduler_mode: SchedulerMode | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application for AppDaemon Store."""
    resolved_mode = scheduler_mode
    if resolved_mode is None:
        resolved_mode = (
            SchedulerMode.ENABLED
            if manager.config.check_interval_hours > 0
            else SchedulerMode.DISABLED
        )

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Initialize store state, manage background scheduler, and close clients."""
        await manager.initialize()
        bg_tasks: set[asyncio.Task[None]] = set()

        if resolved_mode == SchedulerMode.ENABLED:
            task = asyncio.create_task(
                _scheduler_loop(manager, DEFAULT_SCHEDULER_SLEEP_SECONDS)
            )
            bg_tasks.add(task)
            task.add_done_callback(bg_tasks.discard)

        try:
            yield
        finally:
            for task in list(bg_tasks):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await manager.aclose()

    app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=_lifespan)
    _register_err_handlers(app)

    templates = Jinja2Templates(directory=str(_PACKAGE_DIR / TEMPLATES_DIR_NAME))
    app.mount(
        STATIC_URL_PATH,
        StaticFiles(directory=str(_PACKAGE_DIR / STATIC_DIR_NAME)),
        name=STATIC_DIR_NAME,
    )

    @app.get("/", response_class=HTMLResponse)
    async def render_index(request: Request) -> HTMLResponse:
        """Render the Ingress-aware Web UI entrypoint."""
        base_path = _extract_base_path(request)
        return templates.TemplateResponse(
            request=request,
            name=INDEX_TEMPLATE_NAME,
            context={"base_path": base_path},
        )

    @app.get(f"{API_PREFIX}/health")
    async def health_check() -> dict[str, str]:
        """Return lightweight liveness probe payload."""
        return {"status": "ok"}

    @app.get(f"{API_PREFIX}/status", response_model=StoreStatus)
    async def get_status() -> StoreStatus:
        """Return full store status, repositories, requirements, and migrations."""
        return await manager.get_status()

    @app.post(f"{API_PREFIX}/repos", response_model=RepoRecord)
    async def add_repo(body: _AddRepoBody) -> RepoRecord:
        """Add and install a GitHub repository into apps_dir."""
        return await manager.add_repo(body.repo, body.to_mode())

    @app.post(
        f"{API_PREFIX}/repos/{{owner}}/{{repo}}/update",
        response_model=RepoRecord,
    )
    async def update_repo(owner: str, repo: str) -> RepoRecord:
        """Update a managed repository to its latest resolved version."""
        return await manager.update_repo(f"{owner}/{repo}")

    @app.post(
        f"{API_PREFIX}/repos/{{owner}}/{{repo}}/reinstall",
        response_model=RepoRecord,
    )
    async def reinstall_repo(owner: str, repo: str) -> RepoRecord:
        """Force reinstall a managed repository cleanly replacing its folders."""
        return await manager.reinstall_repo(f"{owner}/{repo}")

    @app.post(
        f"{API_PREFIX}/repos/{{owner}}/{{repo}}/pin",
        response_model=RepoRecord,
    )
    async def pin_repo(
        owner: str,
        repo: str,
        body: _PinRepoBody | None = None,
    ) -> RepoRecord:
        """Pin or unpin a managed repository to a specific ref."""
        pinned_ref = body.pinned_ref if body is not None else None
        return await manager.set_pin(f"{owner}/{repo}", pinned_ref)

    @app.post(
        f"{API_PREFIX}/repos/{{owner}}/{{repo}}/auto-update",
        response_model=RepoRecord,
    )
    async def set_auto_update(
        owner: str,
        repo: str,
        body: _AutoUpdateBody,
    ) -> RepoRecord:
        """Toggle auto-update behavior for a managed repository."""
        return await manager.set_auto_upd(f"{owner}/{repo}", body.to_mode())

    @app.delete(f"{API_PREFIX}/repos/{{owner}}/{{repo}}")
    async def remove_repo(owner: str, repo: str) -> dict[str, str]:
        """Remove a managed repository and its installed app folders."""
        repo_id = f"{owner}/{repo}"
        await manager.remove_repo(repo_id)
        return {"removed": repo_id}

    @app.post(f"{API_PREFIX}/check-updates", response_model=StoreStatus)
    async def check_updates() -> StoreStatus:
        """Trigger an update check across all managed repositories."""
        return await manager.check_updates(NotifyMode.NOTIFY)

    @app.get(
        f"{API_PREFIX}/requirements/preview",
        response_model=RequirementSyncResult,
    )
    async def preview_reqs() -> RequirementSyncResult:
        """Preview missing Python packages against AppDaemon add-on options."""
        return await manager.preview_pkgs()

    @app.post(
        f"{API_PREFIX}/requirements/sync",
        response_model=RequirementSyncResult,
    )
    async def sync_reqs(
        body: _SyncBody | None = None,
    ) -> RequirementSyncResult:
        """Sync missing Python packages to AppDaemon add-on options."""
        restart_mode = body.to_mode() if body is not None else RestartMode.RESTART
        return await manager.sync_pkgs(restart_mode)

    @app.post(f"{API_PREFIX}/migrations/apply", response_model=StoreStatus)
    async def apply_migration(body: _MigrationBody) -> StoreStatus:
        """Apply a migration candidate and return refreshed store status."""
        return await manager.apply_migration(body.candidate_id)

    @app.post(f"{API_PREFIX}/migrations/dismiss", response_model=StoreStatus)
    async def dismiss_migration(body: _MigrationBody) -> StoreStatus:
        """Dismiss a migration candidate and return refreshed store status."""
        return await manager.dismiss_mig(body.candidate_id)

    return app
