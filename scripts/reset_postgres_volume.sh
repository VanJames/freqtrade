#!/usr/bin/env sh
set -eu

if [ "${1:-}" != "--force" ]; then
  echo "This removes the Docker volume trading_postgres_data."
  echo "Run: sh scripts/reset_postgres_volume.sh --force"
  exit 2
fi

python scripts/ensure_postgres_password.py
docker compose down
docker volume rm trading_postgres_data 2>/dev/null || true
docker compose up -d --build init-db app dashboard
