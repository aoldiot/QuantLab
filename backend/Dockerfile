FROM node:22-slim AS dsh-runtime-deps

WORKDIR /app/dsh_runtime
COPY backend/dsh_runtime/package.json backend/dsh_runtime/package-lock.json ./
RUN npm ci --omit=dev


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
COPY backend/pyproject.toml backend/uv.lock ./

# Install python dependencies in isolated venv
RUN uv sync --frozen --no-dev

# Copy application source code and migrations
COPY backend/alembic.ini ./
COPY backend/app/ ./app/
COPY backend/migrations/ ./migrations/
COPY backend/scripts/ ./scripts/
COPY docs/ ./docs/
COPY .agents/skills/ ./.agents/skills/

# The DSH SDK loads these Cordis files and the QuantLab bridge plugin at runtime.
# Install the plugin's Node dependencies in the image rather than relying on a
# developer's local node_modules directory being present in the build context.
COPY backend/dsh_runtime/ ./dsh_runtime/
COPY --from=dsh-runtime-deps /app/dsh_runtime/node_modules ./dsh_runtime/node_modules

# Create runtime directories
RUN mkdir -p data catalog data/backtests data/strategy-repository data/agent

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 \
    CMD curl -f http://localhost:8000/api/health || exit 1

# Execute database migrations then launch FastAPI server
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
