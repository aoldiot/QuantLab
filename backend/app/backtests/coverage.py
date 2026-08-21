from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from nautilus_trader.model.data import Bar
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog


@dataclass(frozen=True)
class Coverage:
    complete: bool
    actual_count: int
    expected_count: int | None
    missing_count: int | None
    first_ns: int | None
    last_ns: int | None
    message: str


def date_bounds(start: date, end: date) -> tuple[int, int]:
    lower = int(
        datetime.combine(start, datetime.min.time(), UTC).timestamp() * 1_000_000_000
    )
    upper = int(
        datetime.combine(end + timedelta(days=1), datetime.min.time(), UTC).timestamp()
        * 1_000_000_000
    )
    return lower, upper


def timeframe_ns(timeframe: str) -> int:
    units = {"m": 60, "h": 3600, "d": 86400}
    try:
        return int(timeframe[:-1]) * units[timeframe[-1]] * 1_000_000_000
    except (KeyError, ValueError):
        raise ValueError(f"不支持的数据周期: {timeframe}") from None


def bar_spec_to_timeframe(spec: str) -> str:
    amount, unit, *_ = spec.split("-")
    suffix = {"MINUTE": "m", "HOUR": "h", "DAY": "d"}[unit]
    return f"{amount}{suffix}"


def query_coverage(
    catalog: ParquetDataCatalog,
    data_cls: type,
    identifier: str,
    start_ns: int,
    end_exclusive_ns: int,
    timeframe: str | None = None,
) -> Coverage:
    """Check coverage through NT's public catalog query API, including gaps inside files."""
    rows = catalog.query(
        data_cls=data_cls,
        identifiers=[identifier],
        start=start_ns,
        end=end_exclusive_ns - 1,
    )
    timestamps = sorted(
        {
            int(item.ts_event)
            for item in rows
            if start_ns <= int(item.ts_event) < end_exclusive_ns
        }
    )
    first_ns = timestamps[0] if timestamps else None
    last_ns = timestamps[-1] if timestamps else None
    if data_cls is not Bar or timeframe is None:
        complete = bool(timestamps)
        return Coverage(
            complete,
            len(timestamps),
            None,
            None,
            first_ns,
            last_ns,
            "存在数据" if complete else "没有数据",
        )

    interval_ns = timeframe_ns(timeframe)
    expected_count = (end_exclusive_ns - start_ns) // interval_ns
    expected_first = (
        start_ns + interval_ns - 1_000_000
    )  # Binance K line close timestamps are millisecond based.
    expected_last = start_ns + expected_count * interval_ns - 1_000_000
    in_grid = {
        ts
        for ts in timestamps
        if ts >= expected_first and (ts - expected_first) % interval_ns == 0
    }
    missing_count = max(0, expected_count - len(in_grid))
    complete = (
        expected_count > 0
        and missing_count == 0
        and first_ns == expected_first
        and last_ns == expected_last
    )
    if complete:
        message = f"数据完整：{len(timestamps):,}/{expected_count:,} 根"
    elif not timestamps:
        message = f"请求范围内没有数据，应有 {expected_count:,} 根"
    else:
        message = (
            f"数据不完整：实有 {len(timestamps):,} 根，应有 {expected_count:,} 根，"
            f"至少缺少 {missing_count:,} 根"
        )
    return Coverage(
        complete,
        len(timestamps),
        expected_count,
        missing_count,
        first_ns,
        last_ns,
        message,
    )
