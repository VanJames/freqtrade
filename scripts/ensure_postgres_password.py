from __future__ import annotations

import re
import secrets
from pathlib import Path


ENV_PATH = Path(".env")
PLACEHOLDER_VALUES = {
    "",
    "postgres",
    "change-this-to-a-long-random-password",
}


def main() -> None:
    if not ENV_PATH.exists():
        ENV_PATH.write_text("", encoding="utf-8")
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    current = find_value(lines, "POSTGRES_PASSWORD")
    password = current if current and current not in PLACEHOLDER_VALUES else secrets.token_urlsafe(40)
    lines = upsert(lines, "POSTGRES_PASSWORD", password)
    dsn = find_value(lines, "POSTGRES_DSN")
    if dsn:
        dsn = re.sub(r"postgres:[^@]*@", f"postgres:{password}@", dsn, count=1)
    else:
        dsn = f"postgresql+asyncpg://postgres:{password}@localhost:5432/trading"
    lines = upsert(lines, "POSTGRES_DSN", dsn)
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("POSTGRES_PASSWORD is set in .env")
    print("POSTGRES_DSN is updated in .env")


def find_value(lines: list[str], key: str) -> str | None:
    prefix = f"{key}="
    for line in lines:
        if line.startswith(prefix):
            return line.split("=", 1)[1]
    return None


def upsert(lines: list[str], key: str, value: str) -> list[str]:
    prefix = f"{key}="
    updated: list[str] = []
    found = False
    for line in lines:
        if line.startswith(prefix):
            updated.append(f"{key}={value}")
            found = True
        else:
            updated.append(line)
    if not found:
        updated.append(f"{key}={value}")
    return updated


if __name__ == "__main__":
    main()
