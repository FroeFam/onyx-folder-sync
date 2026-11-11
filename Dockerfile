ARG PYTHON_TAG=3.12
FROM python:${PYTHON_TAG}-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv

WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/
# Install exactly the dependency versions in uv.lock (what CI tested), then the
# project itself without re-resolving its dependencies.
RUN uv export --locked --no-dev --no-emit-project --format requirements-txt -o requirements.txt \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt \
    && pip install --no-cache-dir --prefix=/install --no-deps .
FROM python:${PYTHON_TAG}-slim

COPY --from=builder /install /usr/local

# /data is the mount point for the folders to index. The tool only reads it
# (mount it read-only), so it stays root-owned. /state holds the SQLite
# state database and must be writable by the app user.
RUN useradd --create-home --uid 1000 app \
    && mkdir /state /data \
    && chown app:app /state
USER app
WORKDIR /home/app

# Mount the YAML config at /config/config.yml (or override the variable).
# Keep /state on a local disk, not a network share.
ENV ONYX_FOLDER_SYNC_CONFIG=/config/config.yml
VOLUME /state

ENTRYPOINT ["onyx-folder-sync"]