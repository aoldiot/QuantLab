from __future__ import annotations

import json
import math
import sys
import traceback
from pathlib import Path

from nautilus_trader.backtest.node import BacktestNode

from .analytics import collect
from .builder import build_run_config


def ensure_catalog_coverage(run_config, ignore_missing: bool = True) -> list[str]:
    """Scan catalog coverage for requested identifiers. In lenient mode, log warnings and continue without crashing."""
    missing_items = []
    for data_config in run_config.data:
        catalog = BacktestNode.load_catalog(data_config)
        query = data_config.query
        for identifier in query["identifiers"]:
            # The public first/last timestamp helpers in NautilusTrader 1.227.0
            # use prefix matching and can mix BTC 1m/1h/1d directories. The
            # internal file selector is the same precise selector used by the
            # query backend and honors the complete BarType and time range.
            files = catalog._query_files(
                data_config.data_type,
                [identifier],
                query["start"],
                query["end"],
            )
            if not files:
                missing_items.append(f"{identifier} (范围: {query['start']} ~ {query['end']})")

    if missing_items:
        print(
            f"[WARN] 检测到以下 Catalog 数据未覆盖请求范围（宽松模式自动跳过并继续回测）：\n  "
            + "\n  ".join(missing_items),
            flush=True,
        )
        if not ignore_missing:
            raise ValueError(
                f"Catalog 数据不覆盖请求范围：\n  " + "\n  ".join(missing_items) + "\n未找到匹配的 Parquet 文件"
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
        ignore_missing = bool(payload.get("config", {}).get("ignore_missing_data", True))
        ensure_catalog_coverage(run_config, ignore_missing=ignore_missing)
        node = BacktestNode(configs=[run_config])

        results = node.run()
        if not results:
            raise RuntimeError("BacktestNode 未返回结果")
        engine = node.get_engine(run_config.id)
        if engine is None:
            raise RuntimeError("无法取得回测引擎")
        metrics, result = collect(
            engine, results[0], payload["config"]["venue"], output_path.parent,
            payload["strategy"]["module"], payload["config"]["strategy_parameters"],
            payload["strategy"]["data_requirements"]["primary_timeframe"],
        )
        output_path.write_text(json.dumps(json_safe({"ok": True, "metrics": metrics, "result": result}), ensure_ascii=False, allow_nan=False), encoding="utf-8")
    except Exception as exc:
        output_path.write_text(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"}, ensure_ascii=False), encoding="utf-8")
        raise
    finally:
        if node is not None:
            node.dispose()


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
