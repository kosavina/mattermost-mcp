# syntax=docker/dockerfile:1

# Stage "uv": only a source for the uv binary. It is its own FROM line so that Dependabot,
# which reads FROM lines, can update the version and digest.
FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

# Stage "build": installs the dependencies and the app into /app/.venv. uv, the lock file
# and the build caches stay in this stage and never reach the final image.
FROM python:3.14.10-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first: this layer is cached and rebuilt only when uv.lock or pyproject.toml change.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
# Then the app itself, as a regular (non-editable) install, so the venv needs no src/.
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# Final stage: the same Python base (the venv points at its interpreter), the venv, and a
# non-root user. Nothing else.
FROM python:3.14.10-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
ARG APP_COMMIT=dev
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 MCP_HOST=0.0.0.0 APP_COMMIT=$APP_COMMIT
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]
CMD ["mattermost-mcp"]
