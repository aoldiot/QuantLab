from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

from app.backtests.builder import instrument_id, timeframe_to_bar_spec
from app.config import settings


def _resolve_catalog_path(catalog_path: str | Path | None = None) -> Path:
    if catalog_path is not None:
        p = Path(catalog_path).expanduser().resolve()
        if p.exists():
            return p
    default_p = settings.catalog_path.resolve()
    if default_p.exists():
        return default_p
    # fallback to local backend/catalog
    local_p = Path("catalog").resolve()
    return local_p if local_p.exists() else default_p


def get_catalog_instruments(catalog_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Query all available instruments and their timeframes in the Parquet catalog."""
    resolved = _resolve_catalog_path(catalog_path)
    bar_dir = resolved / "data" / "bar"
    if not bar_dir.exists():
        return []

    instruments_map: dict[str, dict[str, Any]] = {}

    for d in bar_dir.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue

        # Format: <symbol>-<timeframe>-<spec>-EXTERNAL e.g. BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL
        parts = d.name.split("-")
        if len(parts) < 4:
            continue

        inst_id = parts[0]
        # Parse timeframe
        timeframe_label = f"{parts[1]}-{parts[2]}".lower()
        if "1-hour" in timeframe_label:
            tf = "1h"
        elif "4-hour" in timeframe_label:
            tf = "4h"
        elif "1-day" in timeframe_label:
            tf = "1d"
        elif "15-minute" in timeframe_label:
            tf = "15m"
        elif "5-minute" in timeframe_label:
            tf = "5m"
        elif "1-minute" in timeframe_label:
            tf = "1m"
        else:
            tf = f"{parts[1]}{parts[2][0].lower()}"

        symbol = inst_id.split("-")[0].split(".")[0]

        if inst_id not in instruments_map:
            instruments_map[inst_id] = {
                "symbol": symbol,
                "instrument_id": inst_id,
                "timeframes": [],
                "catalog_path": str(resolved),
            }

        if tf not in instruments_map[inst_id]["timeframes"]:
            instruments_map[inst_id]["timeframes"].append(tf)

    return list(instruments_map.values())


def load_market_bars(
    symbol: str,
    timeframe: str = "1h",
    start_date: str | None = None,
    end_date: str | None = None,
    venue: str = "BINANCE",
    catalog_path: str | Path | None = None,
) -> pd.DataFrame:
    """Load historical OHLCV bars into a clean pandas DataFrame."""
    resolved = _resolve_catalog_path(catalog_path)
    if not resolved.exists():
        return pd.DataFrame()

    inst = instrument_id(symbol, venue)
    spec = timeframe_to_bar_spec(timeframe)
    bar_type_str = f"{inst}-{spec}-EXTERNAL"

    catalog = ParquetDataCatalog(str(resolved))
    start_ts = f"{start_date}T00:00:00Z" if start_date else None
    end_ts = f"{end_date}T23:59:59Z" if end_date else None

    try:
        bars = catalog.bars(
            instrument_ids=[inst],
            bar_types=[bar_type_str],
            start=start_ts,
            end=end_ts,
        )
    except Exception:
        # Fallback query by instrument_ids only
        try:
            bars = catalog.bars(
                instrument_ids=[inst],
                start=start_ts,
                end=end_ts,
            )
        except Exception:
            bars = []

    if not bars:
        return pd.DataFrame()

    records = []
    for b in bars:
        ts = pd.to_datetime(b.ts_init, unit="ns", utc=True)
        records.append({
            "timestamp": ts,
            "open": float(b.open.as_double()),
            "high": float(b.high.as_double()),
            "low": float(b.low.as_double()),
            "close": float(b.close.as_double()),
            "volume": float(b.volume.as_double()),
        })

    df = pd.DataFrame.from_records(records)
    df.sort_values("timestamp", inplace=True)
    df.drop_duplicates(subset=["timestamp"], keep="last", inplace=True)
    df.set_index("timestamp", inplace=True)

    if start_date:
        df = df[df.index >= pd.to_datetime(start_date, utc=True)]
    if end_date:
        df = df[df.index <= pd.to_datetime(f"{end_date} 23:59:59", utc=True)]

    return df


def compute_market_stats(df: pd.DataFrame) -> dict[str, Any]:
    """Calculate descriptive statistics for historical market bars."""
    if df.empty or len(df) < 5:
        return {
            "total_bars": len(df),
            "error": "数据量不足，无法计算统计特征",
        }

    close = df["close"]
    returns = close.pct_change().dropna()

    ann_factor = math.sqrt(365 * 24)  # default assuming hourly
    std = returns.std()
    volatility = float(std * ann_factor) if not math.isnan(std) else 0.0

    high_low_range = ((df["high"] - df["low"]) / df["open"]).mean()
    skew = float(returns.skew()) if not math.isnan(returns.skew()) else 0.0
    kurt = float(returns.kurt()) if not math.isnan(returns.kurt()) else 0.0

    total_return = float((close.iloc[-1] / close.iloc[0]) - 1.0)
    avg_volume = float(df["volume"].mean())

    return {
        "start_time": str(df.index[0]),
        "end_time": str(df.index[-1]),
        "total_bars": len(df),
        "start_close": round(float(close.iloc[0]), 4),
        "end_close": round(float(close.iloc[-1]), 4),
        "total_return_pct": round(total_return * 100, 2),
        "annualized_volatility_pct": round(volatility * 100, 2),
        "average_bar_range_pct": round(float(high_low_range) * 100, 2),
        "average_volume": round(avg_volume, 2),
        "skewness": round(skew, 3),
        "kurtosis": round(kurt, 3),
    }
