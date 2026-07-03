#!/usr/bin/env sh
set -eu

APP_USER="${LOCALFORGE_CONTAINER_USER:-localforge}"
DOCKER_SOCKET="${DOCKER_SOCKET:-/var/run/docker.sock}"

if [ "$(id -u)" = "0" ]; then
  if [ -S "$DOCKER_SOCKET" ]; then
    SOCKET_GID="$(stat -c '%g' "$DOCKER_SOCKET" 2>/dev/null || true)"
    if [ -n "$SOCKET_GID" ]; then
      if ! getent group "$SOCKET_GID" >/dev/null 2>&1; then
        groupadd --gid "$SOCKET_GID" dockerhost
      fi
      SOCKET_GROUP="$(getent group "$SOCKET_GID" | cut -d: -f1)"
      usermod -aG "$SOCKET_GROUP" "$APP_USER"
    fi
  fi

  exec setpriv \
    --reuid="$APP_USER" \
    --regid="$APP_USER" \
    --init-groups \
    "$@"
fi

exec "$@"
