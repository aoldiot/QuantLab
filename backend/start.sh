#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DSH_RUNTIME_DIR="$SCRIPT_DIR/dsh_runtime"
DSH_LOCK_FILE="$DSH_RUNTIME_DIR/package-lock.json"
DSH_INSTALL_STAMP="$DSH_RUNTIME_DIR/node_modules/.quantlab-package-lock.sha256"

if ! command -v npm >/dev/null 2>&1; then
  echo "错误：本地运行 DSH 需要 Node.js/npm（建议 Node.js 22）。" >&2
  exit 1
fi

if command -v sha256sum >/dev/null 2>&1; then
  DSH_LOCK_HASH="$(sha256sum "$DSH_LOCK_FILE" | awk '{print $1}')"
else
  DSH_LOCK_HASH="$(shasum -a 256 "$DSH_LOCK_FILE" | awk '{print $1}')"
fi

if [ ! -f "$DSH_INSTALL_STAMP" ] || [ "$(cat "$DSH_INSTALL_STAMP")" != "$DSH_LOCK_HASH" ]; then
  echo "正在安装 DeepSeek Harness Node.js 运行时依赖..."
  npm --prefix "$DSH_RUNTIME_DIR" ci --omit=dev
  printf '%s\n' "$DSH_LOCK_HASH" > "$DSH_INSTALL_STAMP"
fi

uv run alembic upgrade head
uv run uvicorn app.main:app --reload --reload-dir app --reload-exclude "app/strategies/*" --reload-exclude "*/strategies/*" --reload-exclude "artifacts/*" --reload-exclude "data/*" --host 0.0.0.0 --port 8000
