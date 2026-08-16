FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv package manager
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Configure PyPI mirror to speed up package downloads
ENV UV_DEFAULT_INDEX="https://mirrors.aliyun.com/pypi/simple" \
    UV_INDEX_URL="https://mirrors.aliyun.com/pypi/simple"

# Copy dependency specifications
COPY pyproject.toml uv.lock ./

# Install python dependencies in isolated venv
RUN uv sync --frozen --no-dev

# Copy application source code and migrations
COPY alembic.ini ./
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY scripts/ ./scripts/

# Create runtime directories
RUN mkdir -p data catalog data/backtests data/strategy-repository data/agent

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 \
    CMD curl -f http://localhost:8000/api/health || exit 1

# Execute database migrations then launch FastAPI server
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
