from __future__ import annotations

import math
from pathlib import Path
from typing import Any
import json

import pandas as pd


def _column(frame: pd.DataFrame, *names: str) -> str | None:
    lookup = {str(name).lower(): str(name) for name in frame.columns}
    return next((lookup[name.lower()] for name in names if name.lower() in lookup), None)


def _time_ms(value: Any) -> int:
    if isinstance(value, (pd.Timestamp,)):
        return int(value.value // 1_000_000)
    if hasattr(value, "timestamp"):
        return int(value.timestamp() * 1_000)
    number = int(value)
    if number > 10**17:
        return number // 1_000_000
    if number > 10**14:
        return number // 1_000
    if number > 10**11:
        return number
    if number > 10**9:
        return number * 1_000
    return number


def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = float(str(value).split()[0].replace(",", ""))
    return result if math.isfinite(result) else 0.0


def _read(path: Path, columns: list[str] | None = None) -> pd.DataFrame:
    return pd.read_parquet(path, columns=columns)


def available_symbols(artifact_dir: Path) -> list[str]:
    path = artifact_dir / "bars.parquet"
    if not path.exists():
        return []
    frame = _read(path)
    symbol = _column(frame, "symbol", "instrument_id")
    return sorted(frame[symbol].astype(str).unique().tolist()) if symbol else []


def _bar_type_fragment(timeframe: str) -> str:
    unit = timeframe[-1:].lower()
    amount = timeframe[:-1]
    names = {"m": "MINUTE", "h": "HOUR", "d": "DAY", "w": "WEEK"}
    return f"-{amount}-{names.get(unit, unit.upper())}-"


def _parse_timeframe(bar_type: str) -> str | None:
    parts = bar_type.split("-")
    unit_map = {"MINUTE": "m", "MINUTES": "m", "HOUR": "h", "HOURS": "h", "DAY": "d", "DAYS": "d", "WEEK": "w", "WEEKS": "w", "MONTH": "M"}
    for i in range(len(parts) - 1):
        amount = parts[i]
        unit = parts[i + 1].upper()
        if amount.isdigit() and unit in unit_map:
            return f"{amount}{unit_map[unit]}"
    return None


def load_chart(artifact_dir: Path, symbol: str | None, start: int | None, end: int | None,
               limit: int = 5000, timeframe: str | None = None) -> dict:
    bars_path = artifact_dir / "bars.parquet"
    if not bars_path.exists():
        raise FileNotFoundError("回测结果未包含 bars.parquet，请重新运行回测以采集 K 线")
    import pyarrow.parquet as pq
    columns = pq.read_schema(bars_path).names
    time_col = next((name for name in ("timestamp", "ts_init", "ts_event") if name in columns), None)
    symbol_col = next((name for name in ("symbol", "instrument_id") if name in columns), None)
    if not time_col or not symbol_col:
        raise ValueError("bars.parquet 缺少 timestamp/symbol 字段")
    symbol_frame = _read(bars_path, [symbol_col])
    symbols = sorted(symbol_frame[symbol_col].astype(str).unique().tolist())
    selected = symbol if symbol in symbols else (symbols[0] if symbols else "")
    del symbol_frame
    bars = pd.read_parquet(bars_path, filters=[(symbol_col, "==", selected)])
    bar_type_col = _column(bars, "bar_type")
    chosen = None
    if bar_type_col:
        bar_types = bars[bar_type_col].astype(str)
        if timeframe:
            matches = bar_types[bar_types.str.contains(_bar_type_fragment(timeframe), regex=False)]
            if not matches.empty:
                chosen = matches.iloc[0]
        if chosen is None and (artifact_dir / "indicators.parquet").exists():
            try:
                ind_schema = pq.read_schema(artifact_dir / "indicators.parquet").names
                if "bar_type" in ind_schema:
                    ind_types = pd.read_parquet(artifact_dir / "indicators.parquet", columns=["bar_type"])["bar_type"].astype(str)
                    common = set(bar_types.unique()) & set(ind_types.unique())
                    if common:
                        chosen = next(iter(common))
            except Exception:
                pass
        if chosen is None and not bar_types.empty:
            chosen = bar_types.value_counts().index[0]
        if chosen is not None:
            bars = bars[bar_types == chosen].copy()
    resolved_timeframe = _parse_timeframe(chosen) if chosen else timeframe
    bars["_time"] = bars[time_col].map(_time_ms)
    if start is not None:
        bars = bars[bars["_time"] >= start]
    if end is not None:
        bars = bars[bars["_time"] <= end]
    bars = bars.sort_values("_time")
    bars = bars.drop_duplicates("_time", keep="last")
    truncated = len(bars) > limit
    if truncated:
        bars = bars.tail(limit)
    o, h, l, c, volume = (_column(bars, name) for name in ("open", "high", "low", "close", "volume"))
    if not all((o, h, l, c)):
        raise ValueError("bars.parquet 缺少 OHLC 字段")
    candle_rows = [
        {"time": int(row["_time"] // 1000), "open": _number(row[o]), "high": _number(row[h]),
         "low": _number(row[l]), "close": _number(row[c]), "volume": _number(row[volume]) if volume else 0}
        for _, row in bars.iterrows()
    ]

    fills: list[dict] = []
    fills_path = artifact_dir / "fills.parquet"
    if fills_path.exists():
        frame = _read(fills_path)
        ft, fs = _column(frame, "timestamp", "ts_event", "ts_init"), _column(frame, "symbol", "instrument_id")
        fp, fq, side = _column(frame, "price", "last_px"), _column(frame, "quantity", "size", "last_qty"), _column(frame, "side", "order_side")
        if ft and fs and fp and side:
            frame = frame[frame[fs].astype(str) == selected].copy()
            frame["_time"] = frame[ft].map(_time_ms)
            if start is not None: frame = frame[frame["_time"] >= start]
            if end is not None: frame = frame[frame["_time"] <= end]
            for _, row in frame.sort_values("_time").iterrows():
                fills.append({"time": int(row["_time"] // 1000), "price": _number(row[fp]),
                              "quantity": _number(row[fq]) if fq else 0, "side": str(row[side]).upper()})
    plot_config: dict[str, Any] = {}
    indicator_series: dict[str, list[dict[str, Any]]] = {}
    indicators_path, plot_path = artifact_dir / "indicators.parquet", artifact_dir / "plot_config.json"
    if indicators_path.exists() and plot_path.exists():
        plot_config = json.loads(plot_path.read_text(encoding="utf-8"))
        indicators = pd.read_parquet(indicators_path, filters=[("symbol", "==", selected)])
        ibt = _column(indicators, "bar_type")
        if ibt:
            if chosen and (indicators[ibt].astype(str) == chosen).any():
                indicators = indicators[indicators[ibt].astype(str) == chosen]
            elif timeframe:
                mask = indicators[ibt].astype(str).str.contains(_bar_type_fragment(timeframe), regex=False)
                if mask.any():
                    indicators = indicators[mask]
        it = _column(indicators, "ts_init", "timestamp", "ts_event")
        configured = list(plot_config.get("main_plot", {}))
        configured += [column for pane in plot_config.get("subplots", {}).values() for column in pane]
        if it:
            indicators["_time"] = indicators[it].map(_time_ms)
            indicators = indicators.sort_values("_time").drop_duplicates("_time", keep="last")
            if start is not None: indicators = indicators[indicators["_time"] >= start]
            if end is not None: indicators = indicators[indicators["_time"] <= end]
            if len(indicators) > limit: indicators = indicators.tail(limit)
            for column in configured:
                if column in indicators:
                    indicator_series[column] = [
                        {"time": int(row["_time"] // 1000), "value": _number(row[column])}
                        for _, row in indicators[indicators[column].notna()].iterrows()
                    ]
    return {"symbol": selected, "symbols": symbols, "bars": candle_rows, "fills": fills,
            "timeframe": resolved_timeframe, "truncated": truncated, "plot_config": plot_config,
            "indicator_series": indicator_series}
