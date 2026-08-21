from __future__ import annotations

import json
import math
import sys
import traceback
from pathlib import Path

from nautilus_trader.backtest.node import BacktestNode

from .analytics import collect
from .builder import build_run_config
from .coverage import bar_spec_to_timeframe, query_coverage


def ensure_catalog_coverage(run_config, ignore_missing: bool = True) -> list[str]:
    """Scan catalog coverage for requested identifiers. In lenient mode, log warnings and continue without crashing."""
    missing_items = []
    for data_config in run_config.data:
        catalog = BacktestNode.load_catalog(data_config)
        query = data_config.query
        for identifier in query["identifiers"]:
            coverage = query_coverage(
                catalog,
                data_config.data_type,
                str(identifier),
                data_config.start_time_nanos,
                data_config.end_time_nanos + 1,
                bar_spec_to_timeframe(data_config.bar_spec)
                if data_config.bar_spec
                else None,
            )
            if not coverage.complete:
                missing_items.append(f"{identifier}: {coverage.message}")

    if missing_items:
        print(
            "[WARN] 检测到以下 Catalog 数据未覆盖请求范围（宽松模式自动跳过并继续回测）：\n  "
            + "\n  ".join(missing_items),
            flush=True,
        )
        if not ignore_missing:
            raise ValueError(
                "Catalog 数据不覆盖请求范围：\n  "
                + "\n  ".join(missing_items)
                + "\n未找到匹配的 Parquet 文件"
            )
    return missing_items


def json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


def main(payload_path: Path, output_path: Path) -> None:
    node = None
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        run_config, _ = build_run_config(payload)
        ignore_missing = bool(
            payload.get("config", {}).get("ignore_missing_data", True)
        )
        ensure_catalog_coverage(run_config, ignore_missing=ignore_missing)
        node = BacktestNode(configs=[run_config])

        results = node.run()
        if not results:
            raise RuntimeError("BacktestNode 未返回结果")
        engine = node.get_engine(run_config.id)
        if engine is None:
            raise RuntimeError("无法取得回测引擎")
        metrics, result = collect(
            engine,
            results[0],
            payload["config"]["venue"],
            output_path.parent,
            payload["strategy"]["module"],
            payload["config"]["strategy_parameters"],
            payload["strategy"]["data_requirements"]["primary_timeframe"],
            payload["config"],
        )
        output_path.write_text(
            json.dumps(
                json_safe({"ok": True, "metrics": metrics, "result": result}),
                ensure_ascii=False,
                allow_nan=False,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        output_path.write_text(
            json.dumps(
                {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        raise
    finally:
        if node is not None:
            node.dispose()


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
