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
                lambda value: (
                    json.dumps(value, ensure_ascii=False, sort_keys=True)
                    if isinstance(value, (dict, list))
                    else value
                ),
            )
    return frame


def _series_payload(series: pd.Series) -> list[dict[str, Any]]:
    return [
        {"timestamp": str(timestamp), "value": round(float(value), 10)}
        for timestamp, value in series.dropna().sort_index().items()
        if math.isfinite(float(value))
    ]


def _chart_data(
    equity: pd.Series, portfolio_returns: pd.Series, position_returns: pd.Series
) -> dict[str, Any]:
    daily_equity = equity.resample("D").last().ffill()
    daily_returns = daily_equity.pct_change().dropna()
    returns = portfolio_returns if not portfolio_returns.empty else daily_returns
    drawdown = (equity / equity.cummax() - 1.0) * 100
    monthly = (1.0 + daily_returns).resample("ME").prod() - 1.0
    yearly = (1.0 + daily_returns).resample("YE").prod() - 1.0
    values = position_returns.dropna() if not position_returns.empty else returns.dropna()
    counts, edges = (
        np.histogram(
            values.astype(float),
            bins=min(20, max(5, int(math.sqrt(len(values))))),
        )
        if len(values)
        else ([], [])
    )
    return {
        "equity": _series_payload(equity),
        "drawdown": _series_payload(drawdown),
        "monthly_returns": [{"year": int(ts.year), "month": int(ts.month), "value": round(float(value)*100, 6)} for ts, value in monthly.items()],
        "yearly_returns": [{"year": int(ts.year), "value": round(float(value)*100, 6)} for ts, value in yearly.items()],
        "returns_distribution": [{"from": round(float(edges[i])*100, 6), "to": round(float(edges[i+1])*100, 6), "count": int(count)} for i, count in enumerate(counts)],
        "rolling_sharpe": [],
    }


def native_metrics(backtest_result: Any) -> dict[str, Any]:
    """Map headline metrics without recalculating NT's performance results."""
    pnl_stats = next(iter(backtest_result.stats_pnls.values()), {})
    return_stats = backtest_result.stats_returns or {}
    win_rate = pnl_stats.get("Win Rate")
    return {
        "total_return": pnl_stats.get("PnL% (total)"),
        "max_drawdown": return_stats.get("Max Drawdown"),
        "sharpe": return_stats.get("Sharpe Ratio (252 days)"),
        "sharpe_ratio": return_stats.get("Sharpe Ratio (252 days)"),
        "win_rate": float(win_rate) * 100 if win_rate is not None else None,
        # NT computes trade/PnL statistics from realized PnLs, not returns.
        "profit_factor": pnl_stats.get("Profit Factor"),
        "trades": int(backtest_result.total_positions),
        "total_trades": int(backtest_result.total_positions),
        "orders": int(backtest_result.total_orders),
        "events": int(backtest_result.total_events),
        "source": "NautilusTrader BacktestResult",
        "unavailable": {
            "max_drawdown": "NT 当前结果未提供该统计时，QuantLab 不自行计算"
        },
    }


def _sharpe_365(equity: pd.Series) -> float | None:
    daily = equity.resample("D").last().ffill().pct_change().dropna()
    if len(daily) < 2 or daily.std() == 0:
        return None
    return float(daily.mean() / daily.std() * math.sqrt(365))


def fixed_funding_cost(positions: pd.DataFrame, snapshot: dict[str, Any]) -> tuple[float, int]:
    """Approximate fixed 8-hour funding from each position's held notional.

    It deliberately uses the frozen rate and average entry price, making the
    rule deterministic even when no historical funding feed is available.
    """
    if not snapshot.get("enabled") or positions.empty:
        return 0.0, 0
    required = {"ts_opened", "quantity", "side"}
    if not required.issubset(positions.columns):
        return 0.0, 0
    rate = float(snapshot.get("rate_per_8h", 0.0001))
    hours = snapshot.get("settlement_hours_utc", [0, 8, 16])
    total = 0.0
    settlements = 0
    for _, position in positions.iterrows():
        opened = pd.to_datetime(position["ts_opened"], utc=True, errors="coerce")
        closed = pd.to_datetime(position.get("ts_closed"), utc=True, errors="coerce")
        if pd.isna(opened):
            continue
        closed = closed if not pd.isna(closed) else pd.to_datetime(
            snapshot.get("end_time"), utc=True, errors="coerce"
        )
        if pd.isna(closed):
            continue
        notional = abs(_number(position.get("quantity", 0))) * abs(_number(position.get("avg_px_open", 0)))
        if notional == 0:
            continue
        for day in pd.date_range(opened.normalize(), closed.normalize(), freq="D", tz="UTC"):
            for hour in hours:
                settlement = day + pd.Timedelta(hours=int(hour))
                if opened < settlement <= closed:
                    # Positive funding: longs pay, shorts receive.
                    sign = -1.0 if "LONG" in str(position["side"]).upper() else 1.0
                    total += sign * notional * rate
                    settlements += 1
    return total, settlements


def collect(
    engine,
    backtest_result,
    venue: str,
    artifact_dir: Path,
    strategy_module: str,
    strategy_parameters: dict[str, Any],
    primary_timeframe: str,
    run_config: dict[str, Any] | None = None,
) -> tuple[dict, dict]:
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
        report_manifest[name] = {
            "rows": len(frame),
            "columns": list(frame.columns),
            "file": f"{name}.parquet",
        }

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
        fragment = (
            f"-{primary_timeframe[:-1]}-{unit_names[primary_timeframe[-1].lower()]}-"
        )
        primary = all_bars[
            all_bars["bar_type"].astype(str).str.contains(fragment, regex=False)
        ].copy()
        indicator_frames = []
        for (_, bar_type), frame in primary.groupby(["symbol", "bar_type"], sort=False):
            frame = frame.sort_values("ts_init").reset_index(drop=True)
            calculated, plot_config = calculate_plot_indicators(
                strategy_module, frame, strategy_parameters
            )
            columns = ["ts_init", "symbol", "bar_type"]
            configured = list(plot_config.get("main_plot", {}))
            configured += [
                column
                for pane in plot_config.get("subplots", {}).values()
                for column in pane
            ]
            indicator_frames.append(calculated[columns + configured])
        if indicator_frames:
            pd.concat(indicator_frames, ignore_index=True).to_parquet(
                artifact_dir / "indicators.parquet"
            )
            (artifact_dir / "plot_config.json").write_text(
                json.dumps(plot_config, ensure_ascii=False, indent=2), encoding="utf-8"
            )

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
        [
            native_returns.rename("primary"),
            position_returns.rename("position"),
            portfolio_returns.rename("portfolio"),
        ],
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
    (artifact_dir / "backtest_result.json").write_text(
        json.dumps(_safe(native_result), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (artifact_dir / "analyzer_statistics.json").write_text(
        json.dumps(_safe(stats), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (artifact_dir / "report_manifest.json").write_text(
        json.dumps(report_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    charts = _chart_data(equity, portfolio_returns, position_returns)
    equity_frame = pd.DataFrame({"equity": equity}, index=equity.index)
    equity_frame.to_parquet(artifact_dir / "equity.parquet")

    metrics = native_metrics(backtest_result)
    metrics["max_drawdown"] = (
        float(metrics["max_drawdown"]) * 100
        if metrics["max_drawdown"] is not None
        else None
    )
    metrics["sharpe"] = metrics["sharpe_ratio"] = _sharpe_365(equity)
    funding, funding_settlements = fixed_funding_cost(
        reports["positions"], (run_config or {}).get("funding_snapshot", {})
    )
    if funding:
        starting_balance = float((run_config or {}).get("initial_balance", 0))
        if starting_balance:
            metrics["total_return"] = float(metrics["total_return"] or 0) + funding / starting_balance * 100
    metrics["funding_cost"] = funding
    metrics["funding_settlements"] = funding_settlements
    metrics["sharpe_basis_days"] = 365
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
        "funding": {
            "net_cost": funding,
            "settlements": funding_settlements,
            "snapshot": (run_config or {}).get("funding_snapshot", {}),
        },
        "contribution": [],
        # Keep the legacy keys so existing historical detail code remains compatible.
        "equity": [item["value"] for item in charts["equity"]],
        "drawdown": [item["value"] for item in charts["drawdown"]],
        "timestamps": [item["timestamp"] for item in charts["equity"]],
        "stats_pnls": _safe(backtest_result.stats_pnls),
        "stats_returns": _safe(backtest_result.stats_returns),
    }
    return metrics, result
