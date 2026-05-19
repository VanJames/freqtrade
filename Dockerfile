FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./

RUN pip install --upgrade pip

RUN python - <<'PY' > /tmp/requirements.txt
import tomllib

with open("pyproject.toml", "rb") as handle:
    project = tomllib.load(handle)["project"]
print("\n".join(project["dependencies"]))
PY

RUN pip install -r /tmp/requirements.txt

COPY src ./src
COPY knowledge ./knowledge

RUN pip install -e . --no-deps

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["run", "--dry-run", "--with-store", "--with-redis"]
