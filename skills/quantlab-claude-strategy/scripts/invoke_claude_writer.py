#!/usr/bin/env python3
"""
QuantLab Claude Strategy Writer - CLI Invoker for Hermes Agent.

This script allows Hermes Agent to invoke QuantLab's Claude Agent SDK
to generate, modify, and self-heal NautilusTrader strategies with 4-level pre-flight verification.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Invoke QuantLab Claude Agent SDK to write and verify NautilusTrader strategies."
    )
    parser.add_argument(
        "--strategy-name",
        required=True,
        help="Strategy slug (e.g. btc_ema_atr, macd_triple_filter_trend)",
    )
    parser.add_argument(
        "--instructions",
        required=True,
        help="Strategy requirements, trading logic, indicators, and parameters",
    )
    parser.add_argument(
        "--specification",
        default=None,
        help="Optional structured specification JSON string or path to JSON file",
    )
    parser.add_argument(
        "--project-id",
        default=None,
        help="QuantLab research project ID",
    )
    parser.add_argument(
        "--is-fix",
        action="store_true",
        default=False,
        help="Whether this is a repair for backtest/runtime error",
    )
    parser.add_argument(
        "--error-context",
        default=None,
        help="Error stacktrace or message when is_fix=True",
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("QUANTLAB_API_URL", "http://127.0.0.1:8000"),
        help="QuantLab backend base URL (default: http://127.0.0.1:8000)",
    )
    return parser.parse_args()


def parse_specification(spec_arg: str | None) -> dict | None:
    if not spec_arg:
        return None
    spec_arg = spec_arg.strip()
    if spec_arg.startswith("{") and spec_arg.endswith("}"):
        try:
            return json.loads(spec_arg)
        except Exception:
            pass
    spec_path = Path(spec_arg)
    if spec_path.exists() and spec_path.is_file():
        try:
            return json.loads(spec_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def call_via_api(
    api_url: str,
    payload: dict,
    timeout: int = 300,
) -> dict | None:
    """Attempt to call QuantLab backend REST API endpoint."""
    endpoint = f"{api_url.rstrip('/')}/api/research/tools/write-strategy"
    req_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=req_data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 200:
                res_body = response.read().decode("utf-8")
                return json.loads(res_body)
    except urllib.error.HTTPError as exc:
        err_msg = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "error": f"API HTTP {exc.code}: {err_msg}"}
    except Exception as exc:
        return None


async def call_via_local_module(
    strategy_name: str,
    instructions: str,
    is_fix: bool,
    error_context: str | None,
    specification: dict | None,
    project_id: str | None,
) -> dict:
    """Fallback: Import and run QuantLab backend write_strategy_with_claude directly."""
    current_file = Path(__file__).resolve()
    quantlab_root = None

    # Check env var
    if os.environ.get("QUANTLAB_ROOT"):
        p = Path(os.environ["QUANTLAB_ROOT"]).resolve()
        if (p / "backend/app").exists():
            quantlab_root = p

    # Check file parents
    if quantlab_root is None:
        for parent in current_file.parents:
            if (parent / "backend/app").exists():
                quantlab_root = parent
                break

    # Check cwd and parents
    if quantlab_root is None:
        cwd = Path.cwd().resolve()
        for p in [cwd] + list(cwd.parents):
            if (p / "backend/app").exists():
                quantlab_root = p
                break

    # Check default path
    if quantlab_root is None:
        default_p = Path("/Users/sky/Desktop/code/Quantlab")
        if (default_p / "backend/app").exists():
            quantlab_root = default_p

    if quantlab_root is None:
        return {
            "ok": False,
            "error": "Cannot locate QuantLab workspace root. Make sure you run this script from QuantLab project directory or set QUANTLAB_ROOT.",
        }


    backend_path = quantlab_root / "backend"
    if str(backend_path) not in sys.path:
        sys.path.insert(0, str(backend_path))

    try:
        from app.agent.tools import write_strategy_with_claude
        from app.db import SessionLocal

        async with SessionLocal() as db:
            result = await write_strategy_with_claude(
                strategy_name=strategy_name,
                instructions=instructions,
                is_fix=is_fix,
                error_context=error_context,
                specification=specification,
                project_id=project_id,
                db=db,
            )
            return result
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Failed to execute write_strategy_with_claude locally: {exc}",
        }


def main():
    args = parse_args()
    spec = parse_specification(args.specification)

    payload = {
        "strategy_name": args.strategy_name,
        "instructions": args.instructions,
        "is_fix": args.is_fix,
        "error_context": args.error_context,
        "specification": spec,
        "project_id": args.project_id,
    }

    # 1. Try calling QuantLab API endpoint first
    result = call_via_api(args.api_url, payload)

    # 2. If API unreachable, fallback to direct local execution
    if result is None:
        result = asyncio.run(
            call_via_local_module(
                strategy_name=args.strategy_name,
                instructions=args.instructions,
                is_fix=args.is_fix,
                error_context=args.error_context,
                specification=spec,
                project_id=args.project_id,
            )
        )

    # Print output formatted as readable JSON
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if result.get("ok"):
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
