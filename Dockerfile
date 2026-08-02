# syntax=docker/dockerfile:1
#
# Container image for rss2email's local web UI (`r2e web`).
#
# The container runs as an unprivileged user (UID/GID 1000) with a
# read-only root filesystem. Only the bind-mounted /data directory and
# a tmpfs /tmp are writable, so a compromised rss2email process cannot
# modify the image or the host outside the mount.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install uv (used for dependency resolution and installation).
COPY --from=ghcr.io/astral-sh/uv:0.8.4 /uv /usr/local/bin/uv

# Install the application and its declared runtime dependencies. We
# install from the local source tree (rather than `uv sync` after
# copying everything) so a change to the lockfile invalidates only the
# expensive dependency-resolution layer, not the source copy.
COPY pyproject.toml uv.lock ./
COPY rss2email ./rss2email
COPY r2e ./r2e
COPY r2e.1 ./
COPY completion ./completion
COPY README.rst AUTHORS CHANGELOG COPYING ./
RUN uv sync --frozen --no-dev --no-install-project \
 && uv sync --frozen --no-dev

# Create an unprivileged, non-login user with a fixed UID/GID so the
# bind-mounted data directory can be owned consistently between the
# host and the container regardless of the image's build environment.
RUN groupadd --gid 1000 rss2email \
 && useradd  --uid 1000 --gid rss2email \
             --home-dir /data --shell /usr/sbin/nologin --no-create-home rss2email

# rss2email follows the XDG Base Directory Spec: it reads its config
# from $XDG_CONFIG_HOME/rss2email.cfg and its feed database from
# $XDG_DATA_HOME/rss2email.json. Pointing both at subdirectories of
# /data lets a single bind mount cover everything the application
# persists across restarts. HOME is set to /data so any library that
# inspects $HOME (rather than the XDG vars) also lands on the mount.
ENV XDG_CONFIG_HOME=/data/config \
    XDG_DATA_HOME=/data/data \
    HOME=/data \
    PATH=/app/.venv/bin:$PATH

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8080

USER rss2email

# The root filesystem is expected to be mounted read-only (see
# docker-compose.yml). The entrypoint only writes inside $XDG_*_HOME
# (the bind-mounted /data) so it can run under a read-only rootfs.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["r2e", "web", "-H", "0.0.0.0", "-p", "8080"]