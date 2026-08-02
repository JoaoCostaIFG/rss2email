#!/bin/sh
# Container entrypoint for the rss2email web UI.
#
# Ensures the XDG directories that back the bind-mounted /data exist
# (the bind mount may start empty the first time it is attached) and
# then hands off to the image's CMD (`r2e web ...`). Nothing here
# needs to write outside /data, so it is safe under a read-only rootfs.

set -eu

mkdir -p "${XDG_CONFIG_HOME:?}" "${XDG_DATA_HOME:?}"

exec "$@"