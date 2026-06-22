#!/usr/bin/env sh
set -eu

CONTAINER_POSTGRES_DSN="${POSTGRES_DSN:-}"
CONTAINER_REDIS_URL="${REDIS_URL:-}"

if [ -f /run/secrets/app_env ]; then
  eval "$(
    python - <<'PY'
from dotenv import dotenv_values
from shlex import quote

for key, value in dotenv_values("/run/secrets/app_env").items():
    if key and value is not None:
        print(f"export {key}={quote(value)}")
PY
  )"
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
