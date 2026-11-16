"""GitHub API client, repo specifier parser, and safe tarball extractor."""

from enum import StrEnum
import io
from pathlib import Path
import re
import tarfile
from typing import Any
from urllib.parse import urlparse

import httpx

from appdaemon_store.const import (
    GITHUB_ACCEPT_HEADER,
    GITHUB_API_URL,
    GITHUB_API_VERSION,
    GITHUB_WEB_URL,
    HEADER_ACCEPT,
    HEADER_API_VERSION,
    HEADER_AUTHORIZATION,
    HEADER_ETAG,
    HEADER_IF_NONE_MATCH,
    HEADER_USER_AGENT,
    HTTP_BAD_REQUEST,
    HTTP_NOT_FOUND,
    HTTP_NOT_MODIFIED,
    HTTP_OK,
    HTTP_TIMEOUT_SECONDS,
    PAX_GLOBAL_HEADER,
    SHORT_SHA_LENGTH,
    TAGS_PAGE_SIZE,
    TARBALL_TIMEOUT_SECONDS,
    USER_AGENT,
)
from appdaemon_store.exceptions import (
    ExtractionError,
    GitHubApiError,
    RepoNotFoundError,
    ValidationError,
)
from appdaemon_store.type_definitions import ReleaseInfo, RepoSpec, VersionKind

_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_INVALID_REF_CHARS = frozenset({" ", "\t", "\n", "\r", "?", "#", "%", "\\"})
_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
_URL_PART_PAIR = 2
_URL_TREE_MIN_PARTS = 4


class _ProbeOutcome(StrEnum):
    """Internal result of probing a GitHub version endpoint."""

    HIT = "hit"
    NOT_MODIFIED = "not_modified"
    EMPTY = "empty"
    MISSING = "missing"


def _validate_segment(name: str, label: str) -> str:
    """Validate a GitHub owner or repository name segment."""
    if not name or name in {".", ".."} or not _SAFE_SEGMENT_RE.fullmatch(name):
        raise ValidationError(f"Invalid GitHub {label}: {name!r}")

    return name


def _validate_ref(ref: str | None) -> str | None:
    """Validate an optional git tag, branch, or commit ref."""
    if ref is None:
        return None

    cleaned = ref.strip()
    if (
        not cleaned
        or ".." in cleaned
        or cleaned.startswith("/")
        or cleaned.endswith("/")
    ):
        raise ValidationError(f"Invalid git ref: {ref!r}")

    if any(char in _INVALID_REF_CHARS for char in cleaned):
        raise ValidationError(f"Invalid characters in git ref: {ref!r}")

    return cleaned


def _split_at_ref(value: str) -> tuple[str, str | None]:
    """Split an optional '@ref' suffix from a repository string."""
    if "@" not in value:
        return value, None

    base, ref = value.split("@", maxsplit=1)
    if not ref.strip():
        raise ValidationError(f"Empty ref after '@' in {value!r}")

    return base, ref


def parse_repo_input(raw: str) -> RepoSpec:
    """Parse owner/repo[@ref] or a GitHub URL into a validated RepoSpec."""
    cleaned = raw.strip()
    if not cleaned:
        raise ValidationError("Repository input cannot be empty")

    pinned_ref: str | None = None
    if cleaned.startswith(("https://", "http://")):
        base_url, at_ref = _split_at_ref(cleaned)
        parsed = urlparse(base_url)
        if parsed.hostname not in _GITHUB_HOSTS:
            raise ValidationError(f"Only github.com URLs are supported: {raw!r}")

        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) == _URL_PART_PAIR:
            owner, repo = parts
            pinned_ref = at_ref
        elif (
            len(parts) >= _URL_TREE_MIN_PARTS and parts[2] == "tree" and at_ref is None
        ):
            owner, repo = parts[0], parts[1]
            pinned_ref = "/".join(parts[3:])
        else:
            raise ValidationError(f"Unsupported GitHub URL format: {raw!r}")
    else:
        path_part, pinned_ref = _split_at_ref(cleaned)
        parts = [part for part in path_part.strip("/").split("/") if part]
        if len(parts) != _URL_PART_PAIR:
            raise ValidationError(f"Expected 'owner/repo' format, got: {raw!r}")
        owner, repo = parts

    if repo.endswith(".git"):
        repo = repo[:-4]

    owner = _validate_segment(owner, "owner")
    repo = _validate_segment(repo, "repo")
    validated_ref = _validate_ref(pinned_ref)
    full_name = f"{owner}/{repo}"

    return RepoSpec(
        owner=owner,
        repo=repo,
        full_name=full_name,
        pinned_ref=validated_ref,
        url=f"{GITHUB_WEB_URL}/{full_name}",
    )


class GitHubClient:
    """Async GitHub API driver for resolving versions and downloading tarballs."""

    def __init__(
        self,
        token: str | None = None,
        base_url: str = GITHUB_API_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = HTTP_TIMEOUT_SECONDS,
    ) -> None:
        """Initialize the GitHub client with optional auth token and transport."""
        self._token = token.strip() if token and token.strip() else None
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "GitHubClient":
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

    def _get_client(self) -> httpx.AsyncClient:
        """Return or lazily create the shared httpx.AsyncClient."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=True,
                headers=self._headers(),
            )
        return self._client

    def _headers(self, etag: str | None = None) -> dict[str, str]:
        """Build standard GitHub REST API request headers."""
        headers = {
            HEADER_ACCEPT: GITHUB_ACCEPT_HEADER,
            HEADER_API_VERSION: GITHUB_API_VERSION,
            HEADER_USER_AGENT: USER_AGENT,
        }
        if self._token:
            headers[HEADER_AUTHORIZATION] = f"Bearer {self._token}"
        if etag:
            headers[HEADER_IF_NONE_MATCH] = etag
        return headers

    async def _get_json(
        self,
        path: str,
        etag: str | None = None,
        params: dict[str, str | int] | None = None,
    ) -> tuple[int, Any, str | None]:
        """Execute a GET request and return status, JSON payload, and ETag."""
        url = f"{self._base_url}{path}"
        headers = self._headers(etag=etag)
        try:
            resp = await self._get_client().get(url, headers=headers, params=params)
        except httpx.HTTPError as err:
            raise GitHubApiError(f"GitHub request failed for {path}: {err}") from err

        if resp.status_code == HTTP_NOT_MODIFIED:
            return HTTP_NOT_MODIFIED, None, resp.headers.get(HEADER_ETAG)

        if resp.status_code == HTTP_NOT_FOUND:
            return HTTP_NOT_FOUND, None, None

        if resp.status_code >= HTTP_BAD_REQUEST:
            raise GitHubApiError(
                f"GitHub API error {resp.status_code} for {path}: {resp.text}",
                status_code=resp.status_code,
            )

        try:
            payload = resp.json()
        except ValueError as err:
            raise GitHubApiError(f"Invalid JSON from GitHub for {path}: {err}") from err

        return resp.status_code, payload, resp.headers.get(HEADER_ETAG)

    async def resolve_version(
        self, spec: RepoSpec, etag: str | None = None
    ) -> ReleaseInfo | None:
        """Resolve latest release, tag, branch SHA, or pinned ref; None on 304."""
        if spec.pinned_ref:
            return await self._resolve_pinned(spec, spec.pinned_ref, etag)

        rel_outcome, rel_info = await self._probe_release(spec, etag)
        if rel_outcome == _ProbeOutcome.NOT_MODIFIED:
            return None
        if rel_outcome == _ProbeOutcome.HIT:
            return rel_info

        tag_outcome, tag_info = await self._probe_tag(spec, etag)
        if tag_outcome == _ProbeOutcome.NOT_MODIFIED:
            return None
        if tag_outcome == _ProbeOutcome.HIT:
            return tag_info
        if tag_outcome == _ProbeOutcome.MISSING:
            raise RepoNotFoundError(
                f"Repository not found: {spec.full_name}",
                status_code=HTTP_NOT_FOUND,
            )

        return await self._resolve_branch(spec, etag)

    async def _probe_release(
        self, spec: RepoSpec, etag: str | None
    ) -> tuple[_ProbeOutcome, ReleaseInfo | None]:
        """Check latest published GitHub release."""
        status, data, new_etag = await self._get_json(
            f"/repos/{spec.full_name}/releases/latest",
            etag=etag,
        )
        if status == HTTP_NOT_MODIFIED:
            return _ProbeOutcome.NOT_MODIFIED, None
        if status == HTTP_NOT_FOUND or not isinstance(data, dict):
            return _ProbeOutcome.MISSING, None

        tag_name = str(data.get("tag_name") or "").strip()
        if not tag_name:
            return _ProbeOutcome.MISSING, None

        tarball = str(
            data.get("tarball_url")
            or f"{self._base_url}/repos/{spec.full_name}/tarball/{tag_name}"
        )
        html_url = data.get("html_url")
        info = ReleaseInfo(
            version=tag_name,
            ref=tag_name,
            kind=VersionKind.RELEASE,
            tarball_url=tarball,
            etag=new_etag,
            html_url=str(html_url)
            if html_url
            else f"{spec.url}/releases/tag/{tag_name}",
        )
        return _ProbeOutcome.HIT, info

    async def _probe_tag(
        self, spec: RepoSpec, etag: str | None
    ) -> tuple[_ProbeOutcome, ReleaseInfo | None]:
        """Check the newest git tag when no GitHub release is published."""
        status, data, new_etag = await self._get_json(
            f"/repos/{spec.full_name}/tags",
            etag=etag,
            params={"per_page": TAGS_PAGE_SIZE},
        )
        if status == HTTP_NOT_MODIFIED:
            return _ProbeOutcome.NOT_MODIFIED, None
        if status == HTTP_NOT_FOUND:
            return _ProbeOutcome.MISSING, None
        if not isinstance(data, list) or not data:
            return _ProbeOutcome.EMPTY, None

        first = data[0]
        if not isinstance(first, dict) or not first.get("name"):
            return _ProbeOutcome.EMPTY, None

        tag_name = str(first["name"])
        tarball = str(
            first.get("tarball_url")
            or f"{self._base_url}/repos/{spec.full_name}/tarball/{tag_name}"
        )
        info = ReleaseInfo(
            version=tag_name,
            ref=tag_name,
            kind=VersionKind.TAG,
            tarball_url=tarball,
            etag=new_etag,
            html_url=f"{spec.url}/releases/tag/{tag_name}",
        )
        return _ProbeOutcome.HIT, info

    async def _resolve_branch(
        self, spec: RepoSpec, etag: str | None
    ) -> ReleaseInfo | None:
        """Fallback to default branch latest commit SHA."""
        repo_status, repo_data, _ = await self._get_json(f"/repos/{spec.full_name}")
        if repo_status == HTTP_NOT_FOUND or not isinstance(repo_data, dict):
            raise RepoNotFoundError(
                f"Repository not found: {spec.full_name}",
                status_code=HTTP_NOT_FOUND,
            )

        branch = str(repo_data.get("default_branch") or "").strip()
        if not branch:
            raise GitHubApiError(f"Repository {spec.full_name} has no default branch")

        status, commit_data, new_etag = await self._get_json(
            f"/repos/{spec.full_name}/commits/{branch}",
            etag=etag,
        )
        if status == HTTP_NOT_MODIFIED:
            return None
        if status == HTTP_NOT_FOUND or not isinstance(commit_data, dict):
            raise RepoNotFoundError(
                f"Branch '{branch}' not found in {spec.full_name}",
                status_code=HTTP_NOT_FOUND,
            )

        sha = str(commit_data.get("sha") or "").strip()
        if not sha:
            raise GitHubApiError(f"Missing commit SHA for {spec.full_name}@{branch}")

        short_sha = sha[:SHORT_SHA_LENGTH]
        html_url = commit_data.get("html_url")
        return ReleaseInfo(
            version=f"{branch}@{short_sha}",
            ref=sha,
            kind=VersionKind.BRANCH,
            tarball_url=f"{self._base_url}/repos/{spec.full_name}/tarball/{sha}",
            etag=new_etag,
            html_url=str(html_url) if html_url else f"{spec.url}/tree/{branch}",
        )

    async def _resolve_pinned(
        self, spec: RepoSpec, pinned_ref: str, etag: str | None
    ) -> ReleaseInfo | None:
        """Resolve a pinned branch, tag, or commit ref."""
        branch_status, branch_data, branch_etag = await self._get_json(
            f"/repos/{spec.full_name}/branches/{pinned_ref}",
            etag=etag,
        )
        if branch_status == HTTP_NOT_MODIFIED:
            return None

        if branch_status == HTTP_OK and isinstance(branch_data, dict):
            commit = branch_data.get("commit")
            sha = (
                str(commit.get("sha") or "").strip() if isinstance(commit, dict) else ""
            )
            if sha:
                short_sha = sha[:SHORT_SHA_LENGTH]
                return ReleaseInfo(
                    version=f"{pinned_ref}@{short_sha}",
                    ref=sha,
                    kind=VersionKind.PINNED,
                    tarball_url=f"{self._base_url}/repos/{spec.full_name}/tarball/{sha}",
                    etag=branch_etag,
                    html_url=f"{spec.url}/tree/{pinned_ref}",
                )

        commit_status, commit_data, commit_etag = await self._get_json(
            f"/repos/{spec.full_name}/commits/{pinned_ref}",
            etag=etag,
        )
        if commit_status == HTTP_NOT_MODIFIED:
            return None
        if commit_status == HTTP_NOT_FOUND or not isinstance(commit_data, dict):
            raise RepoNotFoundError(
                f"Pinned ref '{pinned_ref}' not found in {spec.full_name}",
                status_code=HTTP_NOT_FOUND,
            )

        return ReleaseInfo(
            version=pinned_ref,
            ref=pinned_ref,
            kind=VersionKind.PINNED,
            tarball_url=f"{self._base_url}/repos/{spec.full_name}/tarball/{pinned_ref}",
            etag=commit_etag,
            html_url=f"{spec.url}/tree/{pinned_ref}",
        )

    async def download_tarball(self, tarball_url: str, dest_dir: Path) -> Path:
        """Download and safely extract a repository tarball into dest_dir."""
        try:
            resp = await self._get_client().get(
                tarball_url,
                headers=self._headers(),
                timeout=TARBALL_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as err:
            raise GitHubApiError(
                f"Failed downloading tarball {tarball_url}: {err}"
            ) from err

        if resp.status_code == HTTP_NOT_FOUND:
            raise RepoNotFoundError(
                f"Tarball not found: {tarball_url}",
                status_code=HTTP_NOT_FOUND,
            )
        if resp.status_code >= HTTP_BAD_REQUEST:
            raise GitHubApiError(
                f"Tarball download failed ({resp.status_code}): {tarball_url}",
                status_code=resp.status_code,
            )

        _safe_extract(resp.content, dest_dir)
        return dest_dir


def _validate_member(member: tarfile.TarInfo) -> tuple[str, ...]:
    """Validate tar member type and path components against traversal attacks."""
    if member.issym() or member.islnk() or member.isdev() or member.isfifo():
        raise ExtractionError(f"Refusing unsafe tar member type: {member.name}")

    if not member.isdir() and not member.isreg():
        raise ExtractionError(f"Unsupported tar member type: {member.name}")

    raw_path = Path(member.name)
    if member.name.startswith("/") or raw_path.is_absolute():
        raise ExtractionError(f"Refusing absolute tar path: {member.name}")

    parts = tuple(part for part in raw_path.parts if part not in {"", "."})
    if ".." in parts:
        raise ExtractionError(f"Refusing path traversal in tar entry: {member.name}")

    return parts


def _has_common_wrapper(entries: list[tuple[tarfile.TarInfo, tuple[str, ...]]]) -> bool:
    """Return whether all tar entries sit inside one common top-level wrapper dir."""
    if not entries:
        return False

    first_parts = {parts[0] for _, parts in entries if parts}
    if len(first_parts) != 1:
        return False

    return any(len(parts) > 1 or info.isdir() for info, parts in entries)


def _safe_extract(archive_bytes: bytes, dest_dir: Path) -> None:
    """Validate all archive members and extract into dest_dir stripping wrapper."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_root = dest_dir.resolve()

    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as archive:
            entries: list[tuple[tarfile.TarInfo, tuple[str, ...]]] = []
            for member in archive.getmembers():
                if member.name == PAX_GLOBAL_HEADER or member.type in (
                    tarfile.XGLTYPE,
                    tarfile.XHDTYPE,
                ):
                    continue
                parts = _validate_member(member)
                if parts:
                    entries.append((member, parts))

            strip_top = _has_common_wrapper(entries)
            for member, parts in entries:
                rel_parts = parts[1:] if strip_top else parts
                if not rel_parts:
                    continue

                target = (dest_root / Path(*rel_parts)).resolve()
                if target != dest_root and not target.is_relative_to(dest_root):
                    raise ExtractionError(f"Escaping target path: {member.name}")

                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                with extracted:
                    target.write_bytes(extracted.read())
    except (tarfile.TarError, EOFError, OSError) as err:
        raise ExtractionError(f"Failed extracting tarball: {err}") from err
