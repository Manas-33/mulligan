# Build from the repo root (the gateway reads contracts/ at runtime):
#   docker build -f gateway/Dockerfile .
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH

WORKDIR /app/gateway
COPY gateway/pyproject.toml gateway/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY gateway/ ./
COPY contracts/ /app/contracts/
COPY demo/seed/ /app/demo/seed/
RUN uv sync --frozen --no-dev

EXPOSE 8000 8001
CMD ["python", "-m", "mulligan"]
