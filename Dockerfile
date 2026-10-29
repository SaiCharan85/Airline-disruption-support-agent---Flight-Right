# Ops API image (docker compose --profile api). Needs judgekit from its git tag, not a local path.
FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY config ./config
RUN uv sync --frozen --no-dev
ENV FLIGHTRIGHT_HOST=0.0.0.0
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "python", "-m", "flightright.api.server"]
