# syntax=docker/dockerfile:1
#
# Backend image: FastAPI + the LangGraph agent pipeline. Builds the dependency set
# with uv against the project's own lockfile (the same uv.lock `uv sync` uses locally),
# then ships a slim runtime image with no build toolchain. Migrations run at container
# start (docker-entrypoint.sh) rather than requiring a separate manual step, since a
# container has no equivalent of the README's "run this once after cloning" step.

FROM python:3.12-slim AS builder
WORKDIR /app

RUN pip install --no-cache-dir uv

# Dependencies first, so an app-code-only change doesn't invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project

COPY app ./app
COPY templates ./templates
COPY static ./static
COPY alembic ./alembic
COPY alembic.ini ./
COPY seed ./seed


FROM python:3.12-slim AS runtime
WORKDIR /app

RUN useradd --create-home --uid 1000 agentcare
COPY --from=builder /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Created (and owned) here, before the volume mounts in docker-compose.yml shadow
# them — Docker seeds a fresh named volume from the image directory it replaces,
# ownership included, so this is what makes the container able to write to either
# once it's running as the non-root `agentcare` user below.
RUN mkdir -p /app/storage/documents /app/data && chown -R agentcare:agentcare /app

COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

USER agentcare
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; port = os.environ.get('PORT', '8000'); urllib.request.urlopen('http://localhost:' + port + '/health', timeout=3)" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
