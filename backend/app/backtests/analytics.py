from __future__ import annotations

import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from nautilus_trader.model.identifiers import Venue
from app.strategy_contract import calculate_plot_indicators


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(str(value).split()[0].replace(",", ""))


def _safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return str(value)
    if isinstance(value, np.generic):
        return _safe(value.item())
    return value


def _parquet_safe(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    for column in frame.select_dtypes(include=["object"]).columns:
        if frame[column].map(lambda value: isinstance(value, (dict, list))).any():
            frame[column] = frame[column].map(
                lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True)
                if isinstance(value, (dict, list)) else value,
            )
    return frame


def _series_payload(series: pd.Series) -> list[dict[str, Any]]:
    return [
        {"timestamp": str(timestamp), "value": round(float(value), 10)}
        for timestamp, value in series.dropna().sort_index().items()
        if math.isfinite(float(value))
    ]


def _chart_data(equity: pd.Series, portfolio_returns: pd.Series, position_returns: pd.Series) -> dict[str, Any]:
    daily_equity = equity.resample("D").last().ffill() if isinstance(equity.index, pd.DatetimeIndex) else equity
    daily_returns = daily_equity.pct_change().dropna()
    returns = portfolio_returns.dropna() if not portfolio_returns.empty else daily_returns
    drawdown = (equity / equity.cummax() - 1.0) * 100
    monthly = (1.0 + returns).resample("ME").prod() - 1.0 if not returns.empty else pd.Series(dtype=float)
    yearly = (1.0 + returns).resample("YE").prod() - 1.0 if not returns.empty else pd.Series(dtype=float)
    rolling = (
        returns.rolling(30, min_periods=5).mean()
        / returns.rolling(30, min_periods=5).std()
        * math.sqrt(365)
        if not returns.empty else pd.Series(dtype=float)
    )
    distribution_source = position_returns.dropna() if not position_returns.empty else returns
    distribution: list[dict[str, float | int]] = []
    if not distribution_source.empty:
        counts, edges = np.histogram(distribution_source.astype(float), bins=min(20, max(5, int(math.sqrt(len(distribution_source))))))
        distribution = [
            {"from": round(float(edges[index]) * 100, 6), "to": round(float(edges[index + 1]) * 100, 6), "count": int(count)}
            for index, count in enumerate(counts)
        ]
    return {
        "equity": _series_payload(equity),
        "drawdown": _series_payload(drawdown),
        "monthly_returns": [
            {"year": int(ts.year), "month": int(ts.month), "value": round(float(value) * 100, 6)}
            for ts, value in monthly.items()
        ],
        "yearly_returns": [{"year": int(ts.year), "value": round(float(value) * 100, 6)} for ts, value in yearly.items()],
        "returns_distribution": distribution,
        "rolling_sharpe": _series_payload(rolling),
    }


def collect(engine, backtest_result, venue: str, artifact_dir: Path, strategy_module: str,
            strategy_parameters: dict[str, Any], primary_timeframe: str) -> tuple[dict, dict]:
    trader = engine.trader
    reports = {
        "orders": trader.generate_orders_report(),
        "order_fills": trader.generate_order_fills_report(),
        "fills": trader.generate_fills_report(),
        "positions": trader.generate_positions_report(),
        "account": trader.generate_account_report(venue=Venue(venue)),
    }
    report_manifest = {}
    for name, frame in reports.items():
        safe_frame = _parquet_safe(frame)
        safe_frame.to_parquet(artifact_dir / f"{name}.parquet")
        report_manifest[name] = {"rows": len(frame), "columns": list(frame.columns), "file": f"{name}.parquet"}

    # Persist the bars retained by Nautilus' cache so the result page can render
    # the exact data seen by the strategy without querying a live market source.
    bar_frames = []
    for bar_type in engine.cache.bar_types():
        cached = engine.cache.bars(bar_type)
        if not cached:
            continue
        frame = pd.DataFrame(type(bar).to_dict(bar) for bar in cached)
        frame["symbol"] = str(bar_type.instrument_id)
        frame["bar_type"] = str(bar_type)
        bar_frames.append(frame)
    plot_config: dict[str, Any] = {}
    if bar_frames:
        all_bars = pd.concat(bar_frames, ignore_index=True)
        all_bars.to_parquet(artifact_dir / "bars.parquet")
        unit_names = {"m": "MINUTE", "h": "HOUR", "d": "DAY", "w": "WEEK"}
        fragment = f"-{primary_timeframe[:-1]}-{unit_names[primary_timeframe[-1].lower()]}-"
        primary = all_bars[all_bars["bar_type"].astype(str).str.contains(fragment, regex=False)].copy()
        indicator_frames = []
        for (_, bar_type), frame in primary.groupby(["symbol", "bar_type"], sort=False):
            frame = frame.sort_values("ts_init").reset_index(drop=True)
            calculated, plot_config = calculate_plot_indicators(strategy_module, frame, strategy_parameters)
            columns = ["ts_init", "symbol", "bar_type"]
            configured = list(plot_config.get("main_plot", {}))
            configured += [column for pane in plot_config.get("subplots", {}).values() for column in pane]
            indicator_frames.append(calculated[columns + configured])
        if indicator_frames:
            pd.concat(indicator_frames, ignore_index=True).to_parquet(artifact_dir / "indicators.parquet")
            (artifact_dir / "plot_config.json").write_text(json.dumps(plot_config, ensure_ascii=False, indent=2), encoding="utf-8")

    account = reports["account"]
    if account.empty or "total" not in account:
        raise RuntimeError("账户报告为空，无法生成真实权益曲线")
    selected = account
    if "currency" in account:
        usdt = account[account["currency"].astype(str) == "USDT"]
        if not usdt.empty:
            selected = usdt
    equity = selected["total"].map(_number)
    equity.index = pd.to_datetime(equity.index, utc=True)
    equity = equity[~equity.index.duplicated(keep="last")].sort_index()

    analyzer = engine.portfolio.analyzer
    native_returns = analyzer.returns().dropna().sort_index()
    position_returns = analyzer.position_returns().dropna().sort_index()
    portfolio_returns = analyzer.portfolio_returns().dropna().sort_index()
    return_series = pd.concat(
        [native_returns.rename("primary"), position_returns.rename("position"), portfolio_returns.rename("portfolio")],
        axis=1,
    ).sort_index()
    return_series.to_parquet(artifact_dir / "returns.parquet")

    stats = {
        "pnls": backtest_result.stats_pnls,
        "returns": analyzer.get_performance_stats_returns(),
        "position_returns": analyzer.get_performance_stats_position_returns(),
        "portfolio_returns": analyzer.get_performance_stats_portfolio_returns(),
        "general": analyzer.get_performance_stats_general(),
    }
    native_result = asdict(backtest_result)
    (artifact_dir / "backtest_result.json").write_text(json.dumps(_safe(native_result), ensure_ascii=False, indent=2), encoding="utf-8")
    (artifact_dir / "analyzer_statistics.json").write_text(json.dumps(_safe(stats), ensure_ascii=False, indent=2), encoding="utf-8")
    (artifact_dir / "report_manifest.json").write_text(json.dumps(report_manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    charts = _chart_data(equity, portfolio_returns, position_returns)
    equity_frame = pd.DataFrame(
        {"equity": equity, "drawdown": equity / equity.cummax() * 100 - 100},
        index=equity.index,
    )
    equity_frame.to_parquet(artifact_dir / "equity.parquet")

    positions = reports["positions"]
    closed = positions[positions["ts_closed"].notna()] if not positions.empty and "ts_closed" in positions else positions
    pnl = closed.get("realized_pnl", pd.Series(dtype=float)).map(_number).dropna()
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    profit_factor = float(wins.sum() / abs(losses.sum())) if not losses.empty else None
    daily_returns = equity.resample("D").last().ffill().pct_change().dropna()
    sharpe = float(daily_returns.mean() / daily_returns.std() * math.sqrt(365)) if len(daily_returns) > 1 and daily_returns.std() else None
    drawdown = equity / equity.cummax() * 100 - 100
    metrics = {
        "total_return": round((equity.iloc[-1] / equity.iloc[0] - 1) * 100, 4),
        "max_drawdown": round(float(drawdown.min()), 4),
        "sharpe": round(sharpe, 4) if sharpe is not None else None,
        "sharpe_ratio": round(sharpe, 4) if sharpe is not None else None,
        "win_rate": round(float((pnl > 0).mean() * 100), 4) if len(pnl) else None,
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "trades": int(len(closed)),
        "total_trades": int(len(closed)),
        "orders": int(backtest_result.total_orders),
        "events": int(backtest_result.total_events),
    }
    contribution = []
    if not closed.empty and {"instrument_id", "realized_pnl"}.issubset(closed.columns):
        grouped = closed.assign(_pnl=closed["realized_pnl"].map(_number)).groupby("instrument_id")["_pnl"].sum()
        contribution = [{"symbol": str(key), "value": round(float(value), 4)} for key, value in grouped.items()]
    result = {
        "native": _safe(native_result),
        "statistics": _safe(stats),
        "series": {
            "primary_returns": _series_payload(native_returns),
            "position_returns": _series_payload(position_returns),
            "portfolio_returns": _series_payload(portfolio_returns),
        },
        "reports": report_manifest,
        "charts": charts,
        "plot_config": plot_config,
        "contribution": contribution,
        # Keep the legacy keys so existing historical detail code remains compatible.
        "equity": [item["value"] for item in charts["equity"]],
        "drawdown": [item["value"] for item in charts["drawdown"]],
        "timestamps": [item["timestamp"] for item in charts["equity"]],
        "stats_pnls": _safe(backtest_result.stats_pnls),
        "stats_returns": _safe(backtest_result.stats_returns),
    }
    return metrics, result
