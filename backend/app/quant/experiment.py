from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from .factor_analysis import compute_technical_factor


@dataclass
class StrategyCandidate:
    """Standard quantitative strategy candidate specification produced by Researcher."""

    strategy_name: str
    category: str = "trend"
    hypothesis: str = ""
    symbols: list[str] = field(default_factory=lambda: ["BTCUSDT"])
    timeframe: str = "1h"
    factors: list[dict[str, Any]] = field(default_factory=list)
    entry_rules: list[str] = field(default_factory=list)
    exit_rules: list[str] = field(default_factory=list)
    risk_rules: list[str] = field(default_factory=list)
    parameters: dict[str, Any] = field(default_factory=dict)
    plot_indicators: list[str] = field(default_factory=list)
    experiment_metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_vectorized_experiment(
    df: pd.DataFrame,
    factor_name: str = "ema_spread",
    factor_params: dict[str, Any] | None = None,
    threshold_long: float = 0.0,
    threshold_short: float | None = None,
    allow_short: bool = True,
    initial_capital: float = 10000.0,
    fee_rate: float = 0.0005,
    slippage_rate: float = 0.0002,
) -> dict[str, Any]:
    """Run a fast vectorized backtest simulation on historical OHLCV data."""
    if df.empty or len(df) < 20:
        return {
            "ok": False,
            "error": "行情数据过少，无法执行实验",
        }

    close = df["close"].astype(float)
    factor_vals = compute_technical_factor(df, factor_name, factor_params)

    # Generate discrete trading position: +1 (Long), -1 (Short), 0 (Flat)
    positions = pd.Series(0.0, index=df.index)

    if threshold_short is None:
        threshold_short = -abs(threshold_long) if threshold_long != 0 else 0.0

    long_mask = factor_vals > threshold_long
    positions[long_mask] = 1.0

    if allow_short:
        short_mask = factor_vals < threshold_short
        positions[short_mask] = -1.0

    # Shift position by 1 bar to prevent look-ahead bias (trade occurs at next bar open/close)
    target_pos = positions.shift(1).fillna(0.0)

    price_returns = close.pct_change().fillna(0.0)
    strategy_gross_returns = target_pos * price_returns

    # Trade turnovers & transaction costs (fee + slippage)
    pos_changes = target_pos.diff().abs().fillna(0.0)
    total_cost_rate = fee_rate + slippage_rate
    costs = pos_changes * total_cost_rate

    strategy_net_returns = strategy_gross_returns - costs

    # Cumulative equity curve
    equity_series = (1.0 + strategy_net_returns).cumprod() * initial_capital

    # Peak equity & Drawdown
    peak_equity = equity_series.cummax()
    drawdown_series = (equity_series - peak_equity) / peak_equity
    max_drawdown = float(drawdown_series.min())

    total_return = float(equity_series.iloc[-1] / initial_capital - 1.0)

    # Annualization factor
    ann_factor = math.sqrt(365 * 24)
    std_ret = strategy_net_returns.std()
    mean_ret = strategy_net_returns.mean()

    sharpe = (
        float((mean_ret / (std_ret + 1e-9)) * ann_factor)
        if not math.isnan(std_ret) and std_ret > 0
        else 0.0
    )

    downside_returns = strategy_net_returns[strategy_net_returns < 0]
    downside_std = downside_returns.std()
    sortino = (
        float((mean_ret / (downside_std + 1e-9)) * ann_factor)
        if not math.isnan(downside_std) and downside_std > 0
        else 0.0
    )

    calmar = (
        float(total_return / (abs(max_drawdown) + 1e-9))
        if abs(max_drawdown) > 0
        else 0.0
    )

    # Trade statistics
    trade_events = pos_changes[pos_changes > 0]
    total_trades = len(trade_events)

    active_returns = strategy_net_returns[target_pos != 0]
    winning_bars = len(active_returns[active_returns > 0])
    losing_bars = len(active_returns[active_returns < 0])
    total_active_bars = winning_bars + losing_bars

    win_rate = (
        float(winning_bars / total_active_bars)
        if total_active_bars > 0
        else 0.0
    )

    gross_gains = strategy_net_returns[strategy_net_returns > 0].sum()
    gross_losses = abs(strategy_net_returns[strategy_net_returns < 0].sum())
    profit_factor = (
        float(gross_gains / (gross_losses + 1e-9))
        if gross_losses > 0
        else 1.0
    )

    # Sampling 30 points for fast equity visualization
    step = max(1, len(equity_series) // 30)
    sampled_curve = [
        {"timestamp": str(ts), "equity": round(float(val), 2)}
        for ts, val in equity_series.iloc[::step].items()
    ]

    is_viable = total_return > 0 and sharpe >= 1.0 and abs(max_drawdown) <= 0.30

    return {
        "ok": True,
        "total_bars": len(df),
        "total_trades": total_trades,
        "initial_capital": initial_capital,
        "final_capital": round(float(equity_series.iloc[-1]), 2),
        "total_return_pct": round(total_return * 100, 2),
        "annualized_return_pct": round(float(mean_ret * 365 * 24 * 100), 2),
        "sharpe_ratio": round(sharpe, 3),
        "sortino_ratio": round(sortino, 3),
        "calmar_ratio": round(calmar, 3),
        "max_drawdown_pct": round(abs(max_drawdown) * 100, 2),
        "win_rate_pct": round(win_rate * 100, 2),
        "profit_factor": round(profit_factor, 3),
        "is_viable_candidate": is_viable,
        "verdict": "STRONG_CANDIDATE" if sharpe >= 1.5 and abs(max_drawdown) <= 0.20 else ("ACCEPTABLE_CANDIDATE" if is_viable else "UNSATISFACTORY"),
        "equity_curve_preview": sampled_curve,
    }
