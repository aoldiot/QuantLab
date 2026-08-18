#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

# Add backend directory to sys.path if not present
backend_dir = (Path(__file__).resolve().parents[4] / "backend").resolve()
if not (backend_dir / "app").exists():
    backend_dir = (Path.cwd() / "backend").resolve()
if (backend_dir / "app").exists() and str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.agent.strategy_verifier import verify_strategy_file


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate_strategy.py <strategy.py>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1]).resolve()
    if path.suffix != ".py" or "backend/app/strategies" not in path.as_posix():
        print("target must be a Python file under backend/app/strategies", file=sys.stderr)
        return 2

    result = verify_strategy_file(path)
    for step in result.steps:
        mark = "✓" if step.ok else "✗"
        print(f"[{mark} {step.level}] {step.name}: {step.message}")

    if not result.ok:
        print(f"\nERROR: [{result.failed_level}] {result.error_message}", file=sys.stderr)
        if result.suggestion:
            print(f"SUGGESTION: {result.suggestion}", file=sys.stderr)
        return 1

    print(f"\nALL 4 PRE-FLIGHT LEVELS PASSED: {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
