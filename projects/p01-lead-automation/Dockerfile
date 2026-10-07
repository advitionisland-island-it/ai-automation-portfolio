# syntax=docker/dockerfile:1
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first so this layer is cached until pyproject.toml or uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN uv sync --locked --no-dev

COPY alembic.ini ./
COPY migrations ./migrations

RUN useradd --create-home --uid 10001 app
USER app

# Inside the container the server must listen on all interfaces; outside, compose publishes
# the port on 127.0.0.1 only. The code default is 127.0.0.1.
ENV PATH="/app/.venv/bin:$PATH" \
    API_HOST=0.0.0.0
EXPOSE 8000
CMD ["python", "-m", "sales_ops"]
