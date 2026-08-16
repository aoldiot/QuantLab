#!/bin/bash
set -e
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --reload-dir app --reload-exclude "app/strategies/*" --reload-exclude "*/strategies/*" --reload-exclude "artifacts/*" --reload-exclude "data/*" --host 0.0.0.0 --port 8000
