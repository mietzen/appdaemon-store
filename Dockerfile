FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APPDAEMON_STORE_APPS_DIR=/apps \
    APPDAEMON_STORE_STATE_FILE=/data/state.json \
    APPDAEMON_STORE_OPTIONS_FILE=/data/options.json \
    APPDAEMON_STORE_HOST=0.0.0.0 \
    APPDAEMON_STORE_PORT=8099

COPY pyproject.toml README.md CHANGELOG.md LICENSE ./
COPY appdaemon_store ./appdaemon_store

RUN pip install --no-cache-dir . && mkdir -p /data /apps

EXPOSE 8099

VOLUME ["/data", "/apps"]

CMD ["appdaemon-store"]
