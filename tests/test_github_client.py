"""Tests for github_client parser, resolver, and safe tarball extractor."""

import io
from pathlib import Path
import tarfile

import httpx
import pytest

from appdaemon_store.const import (
    HEADER_ETAG,
    HEADER_IF_NONE_MATCH,
    HTTP_NOT_FOUND,
    HTTP_NOT_MODIFIED,
    HTTP_OK,
)
from appdaemon_store.exceptions import (
    ExtractionError,
    RepoNotFoundError,
    ValidationError,
)
from appdaemon_store.github_client import GitHubClient, parse_repo_input
from appdaemon_store.type_definitions import VersionKind


def _make_tar_bytes(
    entries: dict[str, bytes], symlink: tuple[str, str] | None = None
) -> bytes:
    """Build an in-memory .tar.gz archive from filename-to-bytes entries."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if symlink is not None:
            link_name, target = symlink
            link_info = tarfile.TarInfo(name=link_name)
            link_info.type = tarfile.SYMTYPE
            link_info.linkname = target
            tar.addfile(link_info)
    return buf.getvalue()


def test_parse_owner_repo() -> None:
    """Parse owner/repo and optional @ref syntax."""
    spec = parse_repo_input("mietzen/fritz-advanced-thermostat-hacs")
    assert spec.owner == "mietzen"
    assert spec.repo == "fritz-advanced-thermostat-hacs"
    assert spec.full_name == "mietzen/fritz-advanced-thermostat-hacs"
    assert spec.pinned_ref is None

    pinned = parse_repo_input("mietzen/my-app@v1.2.3")
    assert pinned.pinned_ref == "v1.2.3"


def test_parse_urls() -> None:
    """Parse GitHub HTTPS URLs with .git, @ref, and /tree/ref forms."""
    plain = parse_repo_input("https://github.com/owner/repo.git")
    assert plain.full_name == "owner/repo"
    assert plain.pinned_ref is None

    at_url = parse_repo_input("https://github.com/owner/repo@v2.0.0")
    assert at_url.full_name == "owner/repo"
    assert at_url.pinned_ref == "v2.0.0"

    tree_url = parse_repo_input("https://github.com/owner/repo/tree/feature/x")
    assert tree_url.full_name == "owner/repo"
    assert tree_url.pinned_ref == "feature/x"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "only-one-part",
        "https://gitlab.com/owner/repo",
        "owner/repo/extra",
        "../repo",
        "owner/..",
        "owner/repo@",
        "owner/repo@bad ref",
        "owner/repo@../escape",
    ],
)
def test_parse_invalid(raw: str) -> None:
    """Reject malformed repository specifiers and unsafe refs."""
    with pytest.raises(ValidationError):
        parse_repo_input(raw)


async def test_resolve_latest_release() -> None:
    """Resolve latest published release with ETag."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/app/releases/latest":
            return httpx.Response(
                HTTP_OK,
                headers={HEADER_ETAG: '"rel-1"'},
                json={
                    "tag_name": "v1.4.0",
                    "tarball_url": "https://api.github.com/repos/acme/app/tarball/v1.4.0",
                    "html_url": "https://github.com/acme/app/releases/tag/v1.4.0",
                },
            )
        return httpx.Response(HTTP_NOT_FOUND)

    client = GitHubClient(transport=httpx.MockTransport(handler))
    async with client:
        info = await client.resolve_version(parse_repo_input("acme/app"))

    assert info is not None
    assert info.version == "v1.4.0"
    assert info.kind == VersionKind.RELEASE
    assert info.etag == '"rel-1"'


async def test_resolve_etag_304() -> None:
    """Return None when GitHub responds with 304 Not Modified."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get(HEADER_IF_NONE_MATCH) == '"rel-1"'
        return httpx.Response(HTTP_NOT_MODIFIED)

    client = GitHubClient(transport=httpx.MockTransport(handler))
    async with client:
        info = await client.resolve_version(
            parse_repo_input("acme/app"), etag='"rel-1"'
        )

    assert info is None


async def test_resolve_tag_fallback() -> None:
    """Fallback to newest tag when no GitHub release exists."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/app/releases/latest":
            return httpx.Response(HTTP_NOT_FOUND)
        if request.url.path == "/repos/acme/app/tags":
            return httpx.Response(
                HTTP_OK,
                json=[
                    {"name": "0.9.1", "tarball_url": "https://api.github.com/t/0.9.1"}
                ],
            )
        return httpx.Response(HTTP_NOT_FOUND)

    client = GitHubClient(transport=httpx.MockTransport(handler))
    async with client:
        info = await client.resolve_version(parse_repo_input("acme/app"))

    assert info is not None
    assert info.version == "0.9.1"
    assert info.kind == VersionKind.TAG


async def test_resolve_branch_fallback() -> None:
    """Fallback to default branch commit SHA when neither release nor tags exist."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/app/releases/latest":
            return httpx.Response(HTTP_NOT_FOUND)
        if request.url.path == "/repos/acme/app/tags":
            return httpx.Response(HTTP_OK, json=[])
        if request.url.path == "/repos/acme/app":
            return httpx.Response(HTTP_OK, json={"default_branch": "main"})
        if request.url.path == "/repos/acme/app/commits/main":
            return httpx.Response(HTTP_OK, json={"sha": "abcdef1234567890"})
        return httpx.Response(HTTP_NOT_FOUND)

    client = GitHubClient(transport=httpx.MockTransport(handler))
    async with client:
        info = await client.resolve_version(parse_repo_input("acme/app"))

    assert info is not None
    assert info.version == "main@abcdef1"
    assert info.ref == "abcdef1234567890"
    assert info.kind == VersionKind.BRANCH


async def test_resolve_pinned_refs() -> None:
    """Resolve pinned branch SHA and pinned tag/commit refs."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/app/branches/dev":
            return httpx.Response(HTTP_OK, json={"commit": {"sha": "1234567890abcdef"}})
        if request.url.path == "/repos/acme/app/commits/v1.0.0":
            return httpx.Response(HTTP_OK, json={"sha": "deadbeef1234"})
        return httpx.Response(HTTP_NOT_FOUND)

    client = GitHubClient(transport=httpx.MockTransport(handler))
    async with client:
        branch_info = await client.resolve_version(parse_repo_input("acme/app@dev"))
        tag_info = await client.resolve_version(parse_repo_input("acme/app@v1.0.0"))

    assert branch_info is not None
    assert branch_info.version == "dev@1234567"
    assert branch_info.kind == VersionKind.PINNED
    assert tag_info is not None
    assert tag_info.version == "v1.0.0"
    assert tag_info.kind == VersionKind.PINNED


async def test_resolve_missing_repo() -> None:
    """Raise RepoNotFoundError when repository returns 404."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(HTTP_NOT_FOUND)

    client = GitHubClient(transport=httpx.MockTransport(handler))
    async with client:
        with pytest.raises(RepoNotFoundError):
            await client.resolve_version(parse_repo_input("acme/missing"))


async def test_download_tarball_strips(tmp_path: Path) -> None:
    """Strip GitHub wrapper directory and pax_global_header on extraction."""
    archive = _make_tar_bytes(
        {
            "pax_global_header": b"52 comment=abc\n",
            "acme-app-1234/apps/my_app/my_app.py": b"print('hi')\n",
            "acme-app-1234/requirements.txt": b"httpx>=0.27\n",
        }
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(HTTP_OK, content=archive)

    dest = tmp_path / "staged"
    client = GitHubClient(transport=httpx.MockTransport(handler))
    async with client:
        await client.download_tarball("https://api.github.com/tarball/v1", dest)

    assert (dest / "apps" / "my_app" / "my_app.py").read_text(
        encoding="utf-8"
    ) == "print('hi')\n"
    assert (dest / "requirements.txt").exists()
    assert not (dest / "pax_global_header").exists()


async def test_tarball_blocks_dotdot(tmp_path: Path) -> None:
    """Reject archive entries containing path traversal segments."""
    archive = _make_tar_bytes({"acme-app-123/../../evil.py": b"x"})
    client = GitHubClient(
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(HTTP_OK, content=archive)
        )
    )
    async with client:
        with pytest.raises(ExtractionError):
            await client.download_tarball("https://api.github.com/t", tmp_path / "out")


async def test_tarball_blocks_abs_path(tmp_path: Path) -> None:
    """Reject archive entries with absolute paths."""
    archive = _make_tar_bytes({"/etc/passwd": b"x"})
    client = GitHubClient(
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(HTTP_OK, content=archive)
        )
    )
    async with client:
        with pytest.raises(ExtractionError):
            await client.download_tarball("https://api.github.com/t", tmp_path / "out")


async def test_tarball_blocks_symlink(tmp_path: Path) -> None:
    """Reject archive entries containing symlinks."""
    archive = _make_tar_bytes(
        {"acme-app-123/file.py": b"x"},
        symlink=("acme-app-123/escape", "/etc/passwd"),
    )
    client = GitHubClient(
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(HTTP_OK, content=archive)
        )
    )
    async with client:
        with pytest.raises(ExtractionError):
            await client.download_tarball("https://api.github.com/t", tmp_path / "out")
