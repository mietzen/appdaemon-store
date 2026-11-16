"""Manage AppDaemon apps from GitHub repositories.

Architecture overview (strict top-down layer dependencies)::

    Presentation / API   (cli.py, web.py)
            |
            v
    Service Layer        (StoreManager, MigrationService)
            |
            v
    Driver Layer         (path_resolver, GitHubClient, filesystem,
                          StateStore, SupervisorClient)
"""

from appdaemon_store.const import APP_VERSION

__version__ = APP_VERSION
__all__ = ["__version__"]
