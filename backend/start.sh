#!/bin/bash
set -e
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --reload-dir app --port 8000
