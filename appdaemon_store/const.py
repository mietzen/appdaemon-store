"""Constants and specification values for AppDaemon Store."""

APP_NAME = "appdaemon-store"
APP_VERSION = "0.1.2"
DEFAULT_PORT = 8099
DEFAULT_HOST = "0.0.0.0"
DEFAULT_LOG_LEVEL = "info"

# Home Assistant OS and AppDaemon path conventions.
APPDAEMON_ADDON_SLUG = "a0d7b954_appdaemon"
DEFAULT_HAOS_ADDON_CONFIG = "/addon_configs/a0d7b954_appdaemon"
DEFAULT_HAOS_APPDAEMON_YAML = "/addon_configs/a0d7b954_appdaemon/appdaemon.yaml"
DEFAULT_HAOS_APPS_DIR = "/addon_configs/a0d7b954_appdaemon/apps"
APPDAEMON_CONTAINER_APPS_DIR = "/config/apps"
CONTAINER_CONFIG_DIR = "/config"
CONTAINER_CONFIG_PREFIX = "/config/"
CONTAINER_HA_DIR = "/homeassistant"
CONTAINER_HA_PREFIX = "/homeassistant/"
HAOS_ADDON_CONFIG_PREFIX = "/addon_configs/a0d7b954_appdaemon/"
LEGACY_HACS_APPS_DIR = "/homeassistant/appdaemon/apps"
LEGACY_CONFIG_APPS_DIR = "/config/appdaemon/apps"
DEFAULT_STATE_FILE = "/data/state.json"
DEFAULT_OPTIONS_FILE = "/data/options.json"

# Background check and Supervisor defaults.
DEFAULT_SUPERVISOR_URL = "http://supervisor"
DEFAULT_CHECK_HOURS = 6
DEFAULT_SCHEDULER_SLEEP_SECONDS = 300
SECONDS_PER_HOUR = 3600

# Web UI, API, and logging defaults.
API_PREFIX = "/api"
STATIC_URL_PATH = "/static"
STATIC_DIR_NAME = "static"
TEMPLATES_DIR_NAME = "templates"
INDEX_TEMPLATE_NAME = "index.html"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_LEVEL_NAMES = ("debug", "info", "warning", "error", "critical")

# GitHub API and HTTP configuration.
GITHUB_API_URL = "https://api.github.com"
GITHUB_WEB_URL = "https://github.com"
GITHUB_ACCEPT_HEADER = "application/vnd.github+json"
GITHUB_API_VERSION = "2022-11-28"
USER_AGENT = f"{APP_NAME}/{APP_VERSION}"
HTTP_TIMEOUT_SECONDS = 30.0
TARBALL_TIMEOUT_SECONDS = 120.0
SHORT_SHA_LENGTH = 7
TAGS_PAGE_SIZE = 1

# HTTP status codes.
HTTP_OK = 200
HTTP_NOT_MODIFIED = 304
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_UNPROCESSABLE = 422
HTTP_INTERNAL_ERROR = 500

# HTTP header names.
HEADER_ACCEPT = "Accept"
HEADER_API_VERSION = "X-GitHub-Api-Version"
HEADER_AUTHORIZATION = "Authorization"
HEADER_ETAG = "ETag"
HEADER_IF_NONE_MATCH = "If-None-Match"
HEADER_INGRESS_PATH = "X-Ingress-Path"
HEADER_USER_AGENT = "User-Agent"

# Environment variable names.
ENV_APPS_DIR = "APPDAEMON_STORE_APPS_DIR"
ENV_STATE_FILE = "APPDAEMON_STORE_STATE_FILE"
ENV_OPTIONS_FILE = "APPDAEMON_STORE_OPTIONS_FILE"
ENV_REPOSITORIES = "APPDAEMON_STORE_REPOSITORIES"
ENV_GITHUB_TOKEN = "APPDAEMON_STORE_GITHUB_TOKEN"
ENV_AUTO_UPDATE = "APPDAEMON_STORE_AUTO_UPDATE"
ENV_CHECK_HOURS = "APPDAEMON_STORE_CHECK_HOURS"
ENV_PORT = "APPDAEMON_STORE_PORT"
ENV_HOST = "APPDAEMON_STORE_HOST"
ENV_LOG_LEVEL = "APPDAEMON_STORE_LOG_LEVEL"
ENV_SUPERVISOR_TOKEN = "SUPERVISOR_TOKEN"
ENV_SUPERVISOR_URL = "SUPERVISOR_URL"
ENV_APPDAEMON_YAML = "APPDAEMON_STORE_APPDAEMON_YAML"

# Repository layout and AppDaemon file names.
HACS_JSON_NAME = "hacs.json"
APPS_YAML_NAME = "apps.yaml"
REQUIREMENTS_NAME = "requirements.txt"
APPS_SUBDIR_NAME = "apps"
PYTHON_EXT = ".py"
INIT_PY_NAME = "__init__.py"
PYCACHE_DIR = "__pycache__"
PAX_GLOBAL_HEADER = "pax_global_header"
YAML_APPDAEMON_KEY = "appdaemon"
YAML_APP_DIR_KEY = "app_dir"
HACS_CONTENT_IN_ROOT_KEY = "content_in_root"

IGNORED_EXTRACT_NAMES = frozenset(
    {
        ".git",
        ".github",
        ".gitignore",
        ".DS_Store",
        "hacs.json",
        "info.md",
        "LICENSE",
        "README.md",
    }
)
IGNORED_COMMON_NAMES = frozenset({".git", ".DS_Store", "__pycache__"})

# Supervisor API endpoints and payload keys.
SUPERVISOR_NOTIFY_PATH = "/core/api/services/persistent_notification/create"
SUPERVISOR_ADDON_INFO_FMT = "/addons/%s/info"
SUPERVISOR_ADDON_OPTS_FMT = "/addons/%s/options"
SUPERVISOR_ADDON_RESTART_FMT = "/addons/%s/restart"
ADDON_DATA_KEY = "data"
ADDON_OPTIONS_KEY = "options"
ADDON_PYTHON_PKGS_KEY = "python_packages"
NOTIFY_ID_PREFIX = "appdaemon_store_"

# Migration candidate ID prefixes.
MIG_ID_LEGACY_PREFIX = "legacy_path:"
MIG_ID_DUP_PREFIX = "duplicate_app:"
MIG_ID_UNMANAGED_PREFIX = "unmanaged_dir:"
