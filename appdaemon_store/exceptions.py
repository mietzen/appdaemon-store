"""Domain exceptions for AppDaemon Store."""


class StoreError(Exception):
    """Raise as base exception for all AppDaemon Store failures."""


class GitHubApiError(StoreError):
    """Raise when a GitHub API request or download fails."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        """Initialize GitHub API error with optional HTTP status code."""
        super().__init__(message)
        self.status_code = status_code


class RepoNotFoundError(GitHubApiError):
    """Raise when a repository, ref, or managed repo ID is missing."""


class ExtractionError(StoreError):
    """Raise when extracting or validating a downloaded archive fails."""


class SupervisorError(StoreError):
    """Raise when a Home Assistant Supervisor API call fails."""


class MigrationError(StoreError):
    """Raise when detecting or applying a migration fails."""


class ValidationError(StoreError):
    """Raise when user input or configuration fails validation."""
