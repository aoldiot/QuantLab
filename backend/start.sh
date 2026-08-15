#!/bin/bash
set -e
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000
