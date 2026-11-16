# AppDaemon Store

Manage [AppDaemon](https://appdaemon.readthedocs.io/) apps directly from GitHub repositories on Home Assistant OS and Docker Compose.

Minimum required Python version: 3.12.

## Features

- Install, update, reinstall, pin (`@tag`, `@branch`, or commit SHA), and remove AppDaemon app repositories from GitHub.
- Supports both standard `apps/<app_name>/` HACS-style layouts and `content_in_root` / root-level layouts (`hacs.json` aware).
- Automatic version resolution: latest GitHub Release, falling back to newest Git tag, falling back to default branch HEAD SHA (with conditional `ETag` polling).
- Home Assistant Ingress Web UI (`X-Ingress-Path`) on port `8099` with dark and light themes.
- Home Assistant OS integration via Supervisor API:
  - Auto-detects `appdaemon.yaml` and maps `/config/apps` to `/addon_configs/a0d7b954_appdaemon/apps`.
  - Previews and syncs discovered `requirements.txt` packages into the AppDaemon add-on `python_packages` option (with optional add-on restart).
  - Sends persistent notifications in Home Assistant when updates are available.
- One-Time HACS Migration Assistant to move legacy `/homeassistant/appdaemon/apps` or `/config/appdaemon/apps` setups and clean up duplicate or unmanaged app directories.

## Architecture

```text
+-----------------------------------------------------------------------+
|                       Presentation & API Layer                        |
|            cli.py (argparse + uvicorn)   |   web.py (FastAPI + UI)    |
+-----------------------------------------------------------------------+
                                   |
                                   v
+-----------------------------------------------------------------------+
|                             Service Layer                             |
|                 StoreManager   |   MigrationService                   |
+-----------------------------------------------------------------------+
                                   |
          +----------------+-------+--------+----------------+
          v                v                v                v
+---------------+  +---------------+  +-----------+  +------------------+
| path_resolver |  | GitHubClient  |  | StateStore|  | SupervisorClient |
| & filesystem  |  | (tarballs/API)|  | (JSON)    |  | (pkgs / notify)  |
+---------------+  +---------------+  +-----------+  +------------------+
```

## Installation

### Home Assistant App (Add-on)

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fmietzen%2Fhassio-addons)

AppDaemon Store runs as a Home Assistant App with Ingress enabled on port `8099`.

1. Ensure the official AppDaemon add-on (`a0d7b954_appdaemon`) is installed.
2. Configure optional startup repositories, `github_token`, `auto_update`, `check_interval_hours`, and `log_level` in the App configuration (`/data/options.json`).
3. Open **AppDaemon Store** from the Home Assistant sidebar via Ingress.

### Docker Compose

Use the included `Dockerfile` and `docker-compose.yml` to run AppDaemon Store alongside a standalone AppDaemon container:

```yaml
services:
  appdaemon-store:
    build: .
    container_name: appdaemon-store
    restart: unless-stopped
    ports:
      - "8099:8099"
    volumes:
      - ./appdaemon/apps:/apps
      - ./store-data:/data
    environment:
      APPDAEMON_STORE_APPS_DIR: /apps
      APPDAEMON_STORE_STATE_FILE: /data/state.json
      APPDAEMON_STORE_CHECK_HOURS: "6"
      APPDAEMON_STORE_AUTO_UPDATE: "false"
      APPDAEMON_STORE_LOG_LEVEL: info
```

Start the service and open `http://localhost:8099`:

```shell
docker compose up -d
```

### Python (`pip`)

```shell
pip install git+https://github.com/mietzen/appdaemon-store.git
```

Run directly from the command line:

```shell
appdaemon-store --apps-dir ./apps --state-file ./state.json --port 8099
```

## Configuration

Configuration precedence (highest to lowest):

1. CLI flags
2. Environment variables
3. Home Assistant `/data/options.json`
4. `appdaemon.yaml` auto-detection (`/addon_configs/a0d7b954_appdaemon/appdaemon.yaml`) and defaults

| CLI Flag | Environment Variable | `options.json` Key | Description | Default |
| --- | --- | --- | --- | --- |
| `--apps-dir` | `APPDAEMON_STORE_APPS_DIR` | `apps_dir` | Target AppDaemon `apps` directory | Auto (`appdaemon.yaml` or `/addon_configs/a0d7b954_appdaemon/apps`) |
| `--appdaemon-yaml` | `APPDAEMON_STORE_APPDAEMON_YAML` | - | Path to `appdaemon.yaml` | `/addon_configs/a0d7b954_appdaemon/appdaemon.yaml` |
| `--state-file` | `APPDAEMON_STORE_STATE_FILE` | - | Path to persistent state JSON | `/data/state.json` |
| `--options-file` | `APPDAEMON_STORE_OPTIONS_FILE` | - | Path to Home Assistant `options.json` | `/data/options.json` |
| `--github-token` | `APPDAEMON_STORE_GITHUB_TOKEN` | `github_token` | GitHub PAT to raise API rate limits | - |
| `--check-hours` | `APPDAEMON_STORE_CHECK_HOURS` | `check_interval_hours` | Interval in hours for scheduled checks | `6` |
| `--host` | `APPDAEMON_STORE_HOST` | - | HTTP server bind address | `0.0.0.0` |
| `--port` | `APPDAEMON_STORE_PORT` | - | HTTP server port | `8099` |
| `--log-level` | `APPDAEMON_STORE_LOG_LEVEL` | `log_level` | Logging level (`debug`, `info`, `warning`, `error`, `critical`) | `info` |
| - | `APPDAEMON_STORE_REPOSITORIES` | `repositories` | Startup repositories to seed (JSON array or comma-separated) | `[]` |
| - | `APPDAEMON_STORE_AUTO_UPDATE` | `auto_update` | Default auto-update mode for seeded repos | `false` |
| - | `SUPERVISOR_URL` | - | Home Assistant Supervisor base URL | `http://supervisor` |
| - | `SUPERVISOR_TOKEN` | - | Supervisor API bearer token | - |

## Repository Formats

Repositories can be added in the Web UI or seeded via configuration using any of these formats:

- `owner/repo`
- `owner/repo@v1.2.0` (or `@branch` / `@commit_sha`)
- `https://github.com/owner/repo`
- `https://github.com/owner/repo/tree/branch`

## HACS Migration Assistant

When AppDaemon Store detects remnants from legacy HACS installations, cards appear at the top of the Web UI with explicit confirmation before modifying files:

- **Legacy Path (`legacy_path`)**: Detects `appdaemon.app_dir` pointing to `/homeassistant/appdaemon/apps` or `/config/appdaemon/apps`. Migrates apps and `apps.yaml` into `/addon_configs/a0d7b954_appdaemon/apps` (preserving existing files via timestamped `.bak` backups on conflicts) and updates `appdaemon.yaml`.
- **Duplicate App (`duplicate_app`)**: Detects unmanaged folders in `apps_dir` whose Python modules overlap with an already-managed repository.
- **Unmanaged Directory (`unmanaged_dir`)**: Flags directories inside `apps_dir` not tracked by AppDaemon Store so you can inspect, remove, or dismiss them.

## REST API

- `GET /api/health` - Liveness check (`{"status": "ok"}`)
- `GET /api/status` - Full status, repositories, requirements, and migrations
- `POST /api/repos` - Install a repository (`{"repo": "owner/repo", "auto_update": false}`)
- `POST /api/repos/{owner}/{repo}/update` - Update a repository
- `POST /api/repos/{owner}/{repo}/reinstall` - Reinstall a repository
- `POST /api/repos/{owner}/{repo}/pin` - Pin or unpin a repository (`{"pinned_ref": "v1.0.0"}`)
- `POST /api/repos/{owner}/{repo}/auto-update` - Toggle auto-update (`{"auto_update": true}`)
- `DELETE /api/repos/{owner}/{repo}` - Remove a repository
- `POST /api/check-updates` - Check all managed repositories for updates
- `GET /api/requirements/preview` - Compare requirements against AppDaemon add-on options
- `POST /api/requirements/sync` - Sync missing requirements to AppDaemon add-on options
- `POST /api/migrations/apply` - Apply a migration candidate (`{"candidate_id": "..."}`)
- `POST /api/migrations/dismiss` - Dismiss a migration notice (`{"candidate_id": "..."}`)
