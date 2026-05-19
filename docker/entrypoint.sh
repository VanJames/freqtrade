#!/usr/bin/env sh
set -eu

CONTAINER_POSTGRES_DSN="${POSTGRES_DSN:-}"
CONTAINER_REDIS_URL="${REDIS_URL:-}"

if [ -f /run/secrets/app_env ]; then
  set -a
  # shellcheck disable=SC1091
  . /run/secrets/app_env
  set +a
fi

if [ -n "$CONTAINER_POSTGRES_DSN" ]; then
  export POSTGRES_DSN="$CONTAINER_POSTGRES_DSN"
fi

if [ -n "$CONTAINER_REDIS_URL" ]; then
  export REDIS_URL="$CONTAINER_REDIS_URL"
fi

case "${1:-}" in
  python|uvicorn)
    exec "$@"
    ;;
  *)
    exec okx-quant "$@"
    ;;
esac
