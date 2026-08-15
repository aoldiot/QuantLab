#!/usr/bin/env python3
from __future__ import annotations

import ast
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate_strategy.py <strategy.py>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1]).resolve()
    if path.suffix != ".py" or "backend/app/strategies" not in path.as_posix():
        print("target must be a Python file under backend/app/strategies", file=sys.stderr)
        return 2
    source = path.read_text(encoding="utf-8")
    compile(source, str(path), "exec")
    tree = ast.parse(source, filename=str(path))
    assignments = {target.id for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign)) for target in ((node.targets if isinstance(node, ast.Assign) else [node.target])) if isinstance(target, ast.Name)}
    functions = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    errors = []
    if "STRATEGY_MANIFEST" not in assignments:
        errors.append("missing STRATEGY_MANIFEST")
    if "calculate_indicators" not in functions:
        errors.append("missing calculate_indicators")
    if errors:
        print("; ".join(errors), file=sys.stderr)
        return 1
    print(f"OK: {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
