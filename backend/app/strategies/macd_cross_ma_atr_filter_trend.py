"""
MACD 金叉死叉 + 双层过滤趋势交易策略

策略规格（与已确认的 QuantLab 策略保持一致）：

- 核心信号：MACD 指标 DIFF 与 DEA 的交叉
  - 金叉：DIFF 由下向上穿越 DEA
  - 死叉：DIFF 由上向下穿越 DEA
- 双层过滤器：
  - 均线方向过滤：短期均线 > 长期均线 视为多头趋势；< 视为空头趋势
  - ATR 波动率过滤：当前 ATR >= 过去 N 根 ATR 中位数 * 阈值，剔除低波动率假突破
- 双向交易：满足完整信号时开多/开空；持仓中触发反向完整信号时平仓并反手
- 风险控制：固定 ATR 止损、固定 ATR 止盈、ATR 移动止损（盈利后每根 K 线更新）
- 仓位管理：ATR 风险调整 + 单仓与总仓位上限；顺势金字塔加仓（每笔 1/2，封顶 2 倍）
- 日内风控：单日账户亏损超阈值后当日禁止新开仓
- 移动止损：多单 close - mult * ATR；空单 close + mult * ATR；只朝有利方向收紧
- 方向反转：持仓过程中均线方向反转立即平仓
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Optional

import numpy as np
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from app.strategy_contract import (
    ParameterSpec,
    StrategyManifest,
    StrategyMode,
)


# ---------------------------------------------------------------------------
# 流式指标计算器（与 batch 实现保持一致：EMA span/ATR Wilder）
# ---------------------------------------------------------------------------


class _Ema:
    """EMA 递推计算器：alpha = 2 / (span + 1)，与 ``ewm(span=span, adjust=False)`` 一致。"""

    __slots__ = ("alpha", "value")

    def __init__(self, period: int) -> None:
        if period <= 0:
            raise ValueError("EMA 周期必须大于 0")
        self.alpha = 2.0 / (period + 1.0)
        self.value: Optional[float] = None

    def update(self, x: float) -> float:
        if self.value is None:
            self.value = float(x)
        else:
            self.value += self.alpha * (float(x) - self.value)
        return self.value


class _Atr:
    """Wilder ATR 递推：alpha = 1 / period，与 ``ewm(alpha=1/period, adjust=False)`` 一致。"""

    __slots__ = ("alpha", "value", "_prev_close")

    def __init__(self, period: int) -> None:
        if period <= 0:
            raise ValueError("ATR 周期必须大于 0")
        self.alpha = 1.0 / period
        self.value: Optional[float] = None
        self._prev_close: Optional[float] = None

    def update(self, high: float, low: float, close: float) -> float:
        high = float(high)
        low = float(low)
        close = float(close)
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(
                high - low,
                abs(high - self._prev_close),
                abs(low - self._prev_close),
            )
        self._prev_close = close
        if self.value is None:
            self.value = tr
        else:
            self.value += self.alpha * (tr - self.value)
        return self.value


# ---------------------------------------------------------------------------
# 策略配置
# ---------------------------------------------------------------------------


class MacdCrossMaAtrFilterTrendConfig(StrategyConfig, frozen=True):
    """
    NautilusTrader 1.227 的 ``StrategyConfig`` 继承自 ``msgspec.Struct``，所有字段
    一旦声明就必须显式提供；``forbid_unknown_fields=True`` 会拒绝回测构建器注入
    的额外字段。因此 ``data_bar_types`` 在 PORTFOLIO 模式下由构建器显式传入。
    """

    instrument_ids: list[InstrumentId]
    bar_types: list[BarType]
    # 由 ``build_run_config`` 在 PORTFOLIO 模式下注入；不可缺省。
    data_bar_types: list[BarType]

    # MACD 参数
    macd_short_period: int = 12
    macd_long_period: int = 26
    macd_signal_period: int = 9

    # 均线方向过滤
    ma_short_period: int = 5
    ma_long_period: int = 20

    # ATR 波动率过滤
    atr_current_period: int = 14
    atr_lookback_period: int = 60
    atr_filter_threshold: float = 0.8

    # 止损 / 止盈（按 ATR 倍数）
    stop_loss_atr_multiplier: float = 1.5
    take_profit_atr_multiplier: float = 3.0
    use_trailing_stop: bool = True

    # 仓位管理
    risk_per_trade_pct: float = 0.01
    max_position_pct: float = 0.10
    max_total_position_pct: float = 0.20
    enable_pyramiding: bool = True

    # 日内风控
    daily_max_loss_pct: float = 0.02


# ---------------------------------------------------------------------------
# 持仓与每标的上下文
# ---------------------------------------------------------------------------


@dataclass
class _PositionState:
    """单标的当前持仓状态。"""

    side: str = "FLAT"  # FLAT / LONG / SHORT
    entry_price: float = 0.0
    entry_atr: float = 0.0
    base_qty: float = 0.0  # 首次开仓数量（金字塔基准）
    last_add_qty: float = 0.0
    add_count: int = 0
    stop_price: float = 0.0
    tp_price: float = 0.0
    trailing_stop: float = 0.0
    # 移动止损用极值跟踪（多头取最高价、空头取最低价）
    extreme_price: float = 0.0


@dataclass
class _InstrumentCtx:
    """每标的独立维护的指标与状态。"""

    instrument_id: InstrumentId
    bar_type: BarType
    fast_ema: _Ema
    slow_ema: _Ema
    signal_ema: _Ema
    ma_short_window: deque
    ma_long_window: deque
    atr: _Atr
    atr_window: deque
    prev_diff: Optional[float] = None
    prev_dea: Optional[float] = None
    bar_count: int = 0
    position: _PositionState = field(default_factory=_PositionState)


# ---------------------------------------------------------------------------
# 策略主体
# ---------------------------------------------------------------------------


class MacdCrossMaAtrFilterTrendStrategy(Strategy):
    """MACD 双层过滤趋势策略（多标的 PORTFOLIO 模式）。"""

    def __init__(self, config: MacdCrossMaAtrFilterTrendConfig) -> None:
        super().__init__(config)

        # ---- 参数合法性预检 ----
        if not (0 < config.macd_short_period < config.macd_long_period):
            raise ValueError("MACD 周期必须满足 0 < 快线 < 慢线")
        if config.macd_signal_period <= 0:
            raise ValueError("MACD 信号线周期必须大于 0")
        if config.ma_short_period <= 0 or config.ma_long_period <= 0:
            raise ValueError("均线周期必须大于 0")
        if config.atr_current_period <= 0 or config.atr_lookback_period <= 0:
            raise ValueError("ATR 周期必须大于 0")
        if config.atr_filter_threshold <= 0:
            raise ValueError("ATR 过滤阈值必须大于 0")
        if config.stop_loss_atr_multiplier <= 0:
            raise ValueError("止损 ATR 乘数必须大于 0")
        if config.take_profit_atr_multiplier <= 0:
            raise ValueError("止盈 ATR 乘数必须大于 0")
        if not (0 < config.risk_per_trade_pct < 1):
            raise ValueError("单笔风险比例必须在 (0, 1) 区间内")
        if not (0 < config.max_position_pct <= 1):
            raise ValueError("单仓最大资金占用比例必须在 (0, 1] 区间内")
        if config.max_total_position_pct < config.max_position_pct:
            raise ValueError("总仓位上限不能小于单仓上限")
        if not (0 < config.daily_max_loss_pct < 1):
            raise ValueError("单日最大亏损比例必须在 (0, 1) 区间内")
        if len(config.instrument_ids) != len(config.bar_types):
            raise ValueError("instrument_ids 与 bar_types 数量必须一致")

        # ---- 每标的上下文 ----
        self._ctxs: dict[BarType, _InstrumentCtx] = {}
        for instrument_id, bar_type in zip(
            config.instrument_ids, config.bar_types
        ):
            self._ctxs[bar_type] = _InstrumentCtx(
                instrument_id=instrument_id,
                bar_type=bar_type,
                fast_ema=_Ema(config.macd_short_period),
                slow_ema=_Ema(config.macd_long_period),
                signal_ema=_Ema(config.macd_signal_period),
                ma_short_window=deque(maxlen=config.ma_short_period),
                ma_long_window=deque(maxlen=config.ma_long_period),
                atr=_Atr(config.atr_current_period),
                atr_window=deque(maxlen=config.atr_lookback_period),
            )

        # ---- 跨标的共享的日内风控状态 ----
        self._daily_date: Optional[pd.Timestamp] = None
        self._day_start_equity: Optional[float] = None
        self._daily_loss_exceeded: bool = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        for bar_type in self.config.bar_types:
            self.subscribe_bars(bar_type)

    def on_stop(self) -> None:
        for bar_type in self.config.bar_types:
            self.cancel_all_orders(self._ctxs[bar_type].instrument_id)
            if not self.portfolio.is_flat(
                self._ctxs[bar_type].instrument_id,
            ):
                self.close_all_positions(self._ctxs[bar_type].instrument_id)
            self.unsubscribe_bars(bar_type)

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        ctx = self._ctxs.get(bar.bar_type)
        if ctx is None:
            return

        high = float(bar.high)
        low = float(bar.low)
        close = float(bar.close)
        # Nautilus timestamps are Unix nanoseconds.  Passing the integer straight
        # to ``Timestamp`` creates a tz-naive value, so ``tz_convert`` would fail.
        bar_time = pd.Timestamp(bar.ts_init, unit="ns", tz="UTC").tz_localize(None)

        # 1) 推进所有指标
        diff = ctx.fast_ema.update(close) - ctx.slow_ema.update(close)
        dea = ctx.signal_ema.update(diff)
        ctx.ma_short_window.append(close)
        ctx.ma_long_window.append(close)
        atr_value = ctx.atr.update(high, low, close)
        if atr_value is not None and not math.isnan(atr_value):
            ctx.atr_window.append(atr_value)
        ctx.bar_count += 1

        # 2) 暖启动期不参与信号
        if not self._is_warmup_complete(ctx):
            ctx.prev_diff = diff
            ctx.prev_dea = dea
            return

        # 3) 派生当日基础量
        ma_short = sum(ctx.ma_short_window) / len(ctx.ma_short_window)
        ma_long = sum(ctx.ma_long_window) / len(ctx.ma_long_window)
        atr_median = self._median(ctx.atr_window)

        # 4) 日内风控推进
        if self._daily_date is None or bar_time.normalize() != self._daily_date:
            self._on_new_day(bar_time)
        if not self._daily_loss_exceeded:
            self._update_daily_loss_check(bar_time)

        # 当日已超损：仅允许平仓，不开新仓
        if self._daily_loss_exceeded:
            if ctx.position.side != "FLAT":
                self._close_position(ctx, reason="daily_loss_exceeded")
            ctx.prev_diff = diff
            ctx.prev_dea = dea
            return

        # 5) 计算本根 MACD 交叉与波动率过滤
        prev_diff = ctx.prev_diff
        prev_dea = ctx.prev_dea
        golden_cross = (
            prev_diff is not None
            and prev_dea is not None
            and prev_diff <= prev_dea
            and diff > dea
        )
        death_cross = (
            prev_diff is not None
            and prev_dea is not None
            and prev_diff >= prev_dea
            and diff < dea
        )
        volatility_ok = (
            atr_value is not None
            and atr_median is not None
            and atr_value >= atr_median * float(self.config.atr_filter_threshold)
        )

        # 6) 持仓期管理（先管退出，再处理反手 / 加仓）
        if ctx.position.side == "LONG":
            self._manage_long_position(
                ctx, bar, close, ma_short, ma_long, atr_value
            )
            if ctx.position.side == "LONG":
                # 反手完整信号 → 平多并开空
                if (
                    death_cross
                    and ma_short < ma_long
                    and volatility_ok
                ):
                    self._reverse_to_short(ctx, close, atr_value)
        elif ctx.position.side == "SHORT":
            self._manage_short_position(
                ctx, bar, close, ma_short, ma_long, atr_value
            )
            if ctx.position.side == "SHORT":
                if (
                    golden_cross
                    and ma_short > ma_long
                    and volatility_ok
                ):
                    self._reverse_to_long(ctx, close, atr_value)
        else:
            # 7) 空仓时按多空信号分别入场
            if golden_cross and ma_short > ma_long and volatility_ok:
                self._open_long(ctx, close, atr_value)
            elif death_cross and ma_short < ma_long and volatility_ok:
                self._open_short(ctx, close, atr_value)

        ctx.prev_diff = diff
        ctx.prev_dea = dea

    # ------------------------------------------------------------------
    # 暖启动 / 日内风控
    # ------------------------------------------------------------------

    def _is_warmup_complete(self, ctx: _InstrumentCtx) -> bool:
        """所有指标都已就绪才允许发信号。"""
        return (
            ctx.fast_ema.value is not None
            and ctx.slow_ema.value is not None
            and ctx.signal_ema.value is not None
            and len(ctx.ma_short_window) >= self.config.ma_short_period
            and len(ctx.ma_long_window) >= self.config.ma_long_period
            and len(ctx.atr_window) >= self.config.atr_lookback_period
            and ctx.prev_diff is not None
            and ctx.prev_dea is not None
        )

    def _on_new_day(self, bar_time: pd.Timestamp) -> None:
        """切日时记录当日初始权益。"""
        self._daily_date = bar_time.normalize()
        equity = self._account_equity()
        self._day_start_equity = equity
        self._daily_loss_exceeded = False

    def _update_daily_loss_check(self, bar_time: pd.Timestamp) -> None:
        equity = self._account_equity()
        if self._day_start_equity is None or self._day_start_equity <= 0:
            return
        loss = self._day_start_equity - equity
        if loss >= self._day_start_equity * float(self.config.daily_max_loss_pct):
            self._daily_loss_exceeded = True
            self.log.warning(
                f"单日亏损达 {loss:.2f}，超过总资金的 "
                f"{self.config.daily_max_loss_pct:.2%}，当日禁止新开仓"
            )

    def _account_equity(self) -> float:
        """获取账户总权益（以 USDT 计），回退到初始余额。"""
        try:
            accounts = list(self.portfolio.accounts())
        except Exception:
            accounts = []
        for account in accounts:
            try:
                equity = account.equity()
            except Exception:
                continue
            if equity is not None and float(equity) > 0:
                return float(equity)
        # 兜底：使用初始余额
        try:
            starting = self.config.initial_balance  # type: ignore[attr-defined]
        except AttributeError:
            starting = 1_000_000.0
        return float(starting)

    # ------------------------------------------------------------------
    # 持仓管理：多单
    # ------------------------------------------------------------------

    def _manage_long_position(
        self,
        ctx: _InstrumentCtx,
        bar: Bar,
        close: float,
        ma_short: float,
        ma_long: float,
        atr_value: Optional[float],
    ) -> None:
        pos = ctx.position
        # 1) 均线方向反转 → 立即平仓（不允许先被止损处理，避免信号冲突）
        if ma_short < ma_long:
            self._close_position(ctx, reason="ma_reverse_long")
            return

        # 2) 止损 / 止盈检查：bar 内触发按不利价成交
        if pos.stop_price > 0 and float(bar.low) <= pos.stop_price:
            self._close_position(ctx, reason="stop_loss_long", price=pos.stop_price)
            return
        if pos.tp_price > 0 and float(bar.high) >= pos.tp_price:
            self._close_position(ctx, reason="take_profit_long", price=pos.tp_price)
            return

        # 3) 移动止损：盈利后每根 K 线收紧
        if (
            self.config.use_trailing_stop
            and atr_value is not None
            and pos.entry_price > 0
        ):
            pos.extreme_price = max(pos.extreme_price, float(bar.high))
            new_trail = pos.extreme_price - float(
                self.config.stop_loss_atr_multiplier
            ) * atr_value
            # 只朝有利方向收紧
            pos.trailing_stop = max(pos.trailing_stop, new_trail)
            if pos.trailing_stop > pos.stop_price and float(bar.low) <= pos.trailing_stop:
                self._close_position(
                    ctx,
                    reason="trailing_stop_long",
                    price=pos.trailing_stop,
                )
                return

        # 4) 顺势金字塔加仓：盈利 + 均线仍多头 + 未达总仓位上限
        if (
            self.config.enable_pyramiding
            and atr_value is not None
            and pos.add_count < 1  # 最多加仓 1 次（总仓位 <= 1.5x，仍受 max_total 限制）
            and close > pos.entry_price
            and ma_short > ma_long
        ):
            add_qty = pos.base_qty * 0.5
            if add_qty > 0 and self._can_increase_position(
                ctx, side="LONG", add_qty=add_qty, price=close
            ):
                instrument = self.cache.instrument(ctx.instrument_id)
                if instrument is None:
                    return
                order = self.order_factory.market(
                    instrument_id=ctx.instrument_id,
                    order_side=OrderSide.BUY,
                    quantity=instrument.make_qty(Decimal(str(add_qty))),
                    time_in_force=TimeInForce.IOC,
                )
                self.submit_order(order)
                # 顺势加仓，更新均价与基准
                total_qty = pos.base_qty + add_qty
                pos.entry_price = (
                    pos.entry_price * pos.base_qty + close * add_qty
                ) / total_qty
                pos.base_qty = total_qty
                pos.last_add_qty = add_qty
                pos.add_count += 1
                self.log.info(
                    f"{ctx.instrument_id} 多单金字塔加仓 {add_qty:.6f} @ {close:.4f}"
                )

    # ------------------------------------------------------------------
    # 持仓管理：空单
    # ------------------------------------------------------------------

    def _manage_short_position(
        self,
        ctx: _InstrumentCtx,
        bar: Bar,
        close: float,
        ma_short: float,
        ma_long: float,
        atr_value: Optional[float],
    ) -> None:
        pos = ctx.position
        if ma_short > ma_long:
            self._close_position(ctx, reason="ma_reverse_short")
            return

        if pos.stop_price > 0 and float(bar.high) >= pos.stop_price:
            self._close_position(ctx, reason="stop_loss_short", price=pos.stop_price)
            return
        if pos.tp_price > 0 and float(bar.low) <= pos.tp_price:
            self._close_position(ctx, reason="take_profit_short", price=pos.tp_price)
            return

        if (
            self.config.use_trailing_stop
            and atr_value is not None
            and pos.entry_price > 0
        ):
            pos.extreme_price = min(pos.extreme_price, float(bar.low))
            new_trail = pos.extreme_price + float(
                self.config.stop_loss_atr_multiplier
            ) * atr_value
            pos.trailing_stop = min(
                pos.trailing_stop if pos.trailing_stop > 0 else new_trail,
                new_trail,
            )
            if pos.trailing_stop > 0 and float(bar.high) >= pos.trailing_stop:
                self._close_position(
                    ctx,
                    reason="trailing_stop_short",
                    price=pos.trailing_stop,
                )
                return

        if (
            self.config.enable_pyramiding
            and atr_value is not None
            and pos.add_count < 1
            and close < pos.entry_price
            and ma_short < ma_long
        ):
            add_qty = pos.base_qty * 0.5
            if add_qty > 0 and self._can_increase_position(
                ctx, side="SHORT", add_qty=add_qty, price=close
            ):
                instrument = self.cache.instrument(ctx.instrument_id)
                if instrument is None:
                    return
                order = self.order_factory.market(
                    instrument_id=ctx.instrument_id,
                    order_side=OrderSide.SELL,
                    quantity=instrument.make_qty(Decimal(str(add_qty))),
                    time_in_force=TimeInForce.IOC,
                )
                self.submit_order(order)
                total_qty = pos.base_qty + add_qty
                pos.entry_price = (
                    pos.entry_price * pos.base_qty + close * add_qty
                ) / total_qty
                pos.base_qty = total_qty
                pos.last_add_qty = add_qty
                pos.add_count += 1
                self.log.info(
                    f"{ctx.instrument_id} 空单金字塔加仓 {add_qty:.6f} @ {close:.4f}"
                )

    # ------------------------------------------------------------------
    # 开仓 / 平仓 / 反手
    # ------------------------------------------------------------------

    def _open_long(
        self,
        ctx: _InstrumentCtx,
        close: float,
        atr_value: float,
    ) -> None:
        if ctx.position.side != "FLAT":
            return
        instrument = self.cache.instrument(ctx.instrument_id)
        if instrument is None:
            self.log.error(f"找不到标的 {ctx.instrument_id}")
            return
        qty = self._compute_position_size(
            ctx.instrument_id, close, atr_value
        )
        if qty <= 0:
            return
        order = self.order_factory.market(
            instrument_id=ctx.instrument_id,
            order_side=OrderSide.BUY,
            quantity=instrument.make_qty(Decimal(str(qty))),
            time_in_force=TimeInForce.IOC,
        )
        self.submit_order(order)
        self._record_position(
            ctx, side="LONG", price=close, atr=atr_value, qty=qty
        )
        self.log.info(
            f"{ctx.instrument_id} MACD 金叉开多 qty={qty:.6f} @ {close:.4f}"
        )

    def _open_short(
        self,
        ctx: _InstrumentCtx,
        close: float,
        atr_value: float,
    ) -> None:
        if ctx.position.side != "FLAT":
            return
        instrument = self.cache.instrument(ctx.instrument_id)
        if instrument is None:
            self.log.error(f"找不到标的 {ctx.instrument_id}")
            return
        qty = self._compute_position_size(
            ctx.instrument_id, close, atr_value
        )
        if qty <= 0:
            return
        order = self.order_factory.market(
            instrument_id=ctx.instrument_id,
            order_side=OrderSide.SELL,
            quantity=instrument.make_qty(Decimal(str(qty))),
            time_in_force=TimeInForce.IOC,
        )
        self.submit_order(order)
        self._record_position(
            ctx, side="SHORT", price=close, atr=atr_value, qty=qty
        )
        self.log.info(
            f"{ctx.instrument_id} MACD 死叉开空 qty={qty:.6f} @ {close:.4f}"
        )

    def _reverse_to_long(
        self,
        ctx: _InstrumentCtx,
        close: float,
        atr_value: float,
    ) -> None:
        self._close_position(ctx, reason="reverse_to_long")
        self._open_long(ctx, close, atr_value)

    def _reverse_to_short(
        self,
        ctx: _InstrumentCtx,
        close: float,
        atr_value: float,
    ) -> None:
        self._close_position(ctx, reason="reverse_to_short")
        self._open_short(ctx, close, atr_value)

    def _close_position(
        self,
        ctx: _InstrumentCtx,
        reason: str,
        price: Optional[float] = None,
    ) -> None:
        if ctx.position.side == "FLAT":
            return
        self.close_all_positions(ctx.instrument_id)
        fill_price = price if price is not None else ctx.position.entry_price
        self.log.info(
            f"{ctx.instrument_id} 平仓 reason={reason} @ {fill_price:.4f}"
        )
        ctx.position = _PositionState()

    # ------------------------------------------------------------------
    # 仓位大小计算
    # ------------------------------------------------------------------

    def _compute_position_size(
        self,
        instrument_id: InstrumentId,
        price: float,
        atr_value: float,
    ) -> float:
        if price <= 0 or atr_value <= 0:
            return 0.0
        equity = self._account_equity()
        if equity <= 0:
            return 0.0
        risk_dollars = equity * float(self.config.risk_per_trade_pct)
        stop_distance = float(self.config.stop_loss_atr_multiplier) * atr_value
        if stop_distance <= 0:
            return 0.0
        risk_qty = risk_dollars / stop_distance
        risk_value = risk_qty * price
        max_single_value = equity * float(self.config.max_position_pct)
        if risk_value > max_single_value:
            capped_qty = max_single_value / price
        else:
            capped_qty = risk_qty
        return max(capped_qty, 0.0)

    def _can_increase_position(
        self,
        ctx: _InstrumentCtx,
        side: str,
        add_qty: float,
        price: float,
    ) -> bool:
        """加仓后总仓位不超过 max_total_position_pct。"""
        equity = self._account_equity()
        if equity <= 0 or price <= 0:
            return False
        current_qty = ctx.position.base_qty
        if side == "LONG":
            total_qty = current_qty + add_qty
        else:
            total_qty = current_qty + add_qty
        total_value = total_qty * price
        return total_value <= equity * float(self.config.max_total_position_pct)

    def _record_position(
        self,
        ctx: _InstrumentCtx,
        side: str,
        price: float,
        atr: float,
        qty: float,
    ) -> None:
        stop_distance = float(self.config.stop_loss_atr_multiplier) * atr
        tp_distance = float(self.config.take_profit_atr_multiplier) * atr
        pos = ctx.position
        pos.side = side
        pos.entry_price = price
        pos.entry_atr = atr
        pos.base_qty = qty
        pos.last_add_qty = qty
        pos.add_count = 0
        pos.extreme_price = price
        if side == "LONG":
            pos.stop_price = price - stop_distance
            pos.tp_price = price + tp_distance
            pos.trailing_stop = pos.stop_price
        else:
            pos.stop_price = price + stop_distance
            pos.tp_price = price - tp_distance
            pos.trailing_stop = 0.0

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    @staticmethod
    def _median(values: Iterable[float]) -> Optional[float]:
        values = list(values)
        n = len(values)
        if n == 0:
            return None
        sorted_vals = sorted(values)
        if n % 2 == 1:
            return sorted_vals[n // 2]
        return 0.5 * (sorted_vals[n // 2 - 1] + sorted_vals[n // 2])


# ---------------------------------------------------------------------------
# 离线指标计算（供回测图表与单元测试复用）
# ---------------------------------------------------------------------------


def _coerce_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def calculate_indicators(
    dataframe: pd.DataFrame,
    parameters: dict,
) -> pd.DataFrame:
    """
    为图表与单元测试统一生成所有 ``plot_config`` 引用的列。

    返回的 DataFrame 行数与 ``dataframe`` 保持一致；空值用 ``NaN`` 表达。
    """
    macd_short = int(parameters.get("macd_short_period", 12))
    macd_long = int(parameters.get("macd_long_period", 26))
    macd_signal = int(parameters.get("macd_signal_period", 9))
    ma_short = int(parameters.get("ma_short_period", 5))
    ma_long = int(parameters.get("ma_long_period", 20))
    atr_period = int(parameters.get("atr_current_period", 14))
    atr_lookback = int(parameters.get("atr_lookback_period", 60))

    if not (0 < macd_short < macd_long):
        raise ValueError("MACD 周期必须满足 0 < 快线 < 慢线")
    if macd_signal <= 0:
        raise ValueError("MACD 信号线周期必须大于 0")
    if ma_short <= 0 or ma_long <= 0:
        raise ValueError("均线周期必须大于 0")
    if atr_period <= 0 or atr_lookback <= 0:
        raise ValueError("ATR 周期必须大于 0")

    close = _coerce_numeric(dataframe["close"]) if "close" in dataframe else None
    high = (
        _coerce_numeric(dataframe["high"])
        if "high" in dataframe
        else close
    )
    low = (
        _coerce_numeric(dataframe["low"])
        if "low" in dataframe
        else close
    )

    if close is None:
        raise ValueError("calculate_indicators 需要 close 列")

    # 均线（简单移动平均，与策略中 sum(deque)/N 一致）
    dataframe["ma_short"] = close.rolling(window=ma_short, min_periods=ma_short).mean()
    dataframe["ma_long"] = close.rolling(window=ma_long, min_periods=ma_long).mean()

    # MACD（EMA）
    fast_ema = close.ewm(span=macd_short, adjust=False).mean()
    slow_ema = close.ewm(span=macd_long, adjust=False).mean()
    diff = fast_ema - slow_ema
    dea = diff.ewm(span=macd_signal, adjust=False).mean()
    histogram = diff - dea
    dataframe["macd_diff"] = diff
    dataframe["macd_dea"] = dea
    dataframe["macd_hist"] = histogram

    # ATR（Wilder）
    prev_close = close.shift(1)
    tr_components = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    )
    true_range = tr_components.max(axis=1)
    atr = true_range.ewm(alpha=1.0 / atr_period, adjust=False).mean()
    dataframe["atr"] = atr
    dataframe["atr_median"] = atr.rolling(window=atr_lookback, min_periods=atr_lookback).median()

    return dataframe


# ---------------------------------------------------------------------------
# 策略清单
# ---------------------------------------------------------------------------


STRATEGY_MANIFEST = StrategyManifest(
    slug="macd-cross-ma-atr-filter-trend",
    name="MACD 金叉死叉 + 双层过滤趋势策略",
    version="0.1.0",
    description=(
        "MACD 金叉死叉叠加双层过滤（均线方向 + ATR 波动率）的多空趋势策略；"
        "固定 ATR 止损与止盈、可选移动止损、顺势金字塔加仓、"
        "日内亏损超限禁开仓、均线方向反转即平仓。"
    ),
    category="趋势",
    strategy_path=(
        "app.strategies.macd_cross_ma_atr_filter_trend:"
        "MacdCrossMaAtrFilterTrendStrategy"
    ),
    config_path=(
        "app.strategies.macd_cross_ma_atr_filter_trend:"
        "MacdCrossMaAtrFilterTrendConfig"
    ),
    parameters={
        "macd_short_period": ParameterSpec(
            "MACD 快线周期", "integer", 12, 2, 100
        ),
        "macd_long_period": ParameterSpec(
            "MACD 慢线周期", "integer", 26, 3, 200
        ),
        "macd_signal_period": ParameterSpec(
            "MACD 信号线周期", "integer", 9, 2, 100
        ),
        "ma_short_period": ParameterSpec(
            "短期均线周期", "integer", 5, 2, 100
        ),
        "ma_long_period": ParameterSpec(
            "长期均线周期", "integer", 20, 3, 200
        ),
        "atr_current_period": ParameterSpec(
            "ATR 计算周期", "integer", 14, 2, 100
        ),
        "atr_lookback_period": ParameterSpec(
            "ATR 中位数回看周期", "integer", 60, 5, 500
        ),
        "atr_filter_threshold": ParameterSpec(
            "ATR 波动率过滤阈值（>=中位数*阈值）",
            "number",
            0.8,
            0.01,
            5.0,
        ),
        "stop_loss_atr_multiplier": ParameterSpec(
            "止损 ATR 乘数", "number", 1.5, 0.1, 10.0
        ),
        "take_profit_atr_multiplier": ParameterSpec(
            "止盈 ATR 乘数", "number", 3.0, 0.1, 20.0
        ),
        "use_trailing_stop": ParameterSpec(
            "是否启用 ATR 移动止损", "boolean", True
        ),
        "risk_per_trade_pct": ParameterSpec(
            "单笔风险占总资金比例", "number", 0.01, 0.0001, 0.5
        ),
        "max_position_pct": ParameterSpec(
            "单仓最大资金占用比例", "number", 0.10, 0.001, 1.0
        ),
        "max_total_position_pct": ParameterSpec(
            "单标的总仓位最大资金占用比例", "number", 0.20, 0.001, 2.0
        ),
        "enable_pyramiding": ParameterSpec(
            "是否允许顺势金字塔加仓", "boolean", True
        ),
        "daily_max_loss_pct": ParameterSpec(
            "单日最大亏损占总资金比例", "number", 0.02, 0.001, 0.5
        ),
    },
    timeframes=("4h", "1d"),
    primary_timeframe="1d",
    plot_config={
        "main_plot": {
            "ma_short": {
                "name": "MA 短",
                "type": "line",
                "color": "#43a5ff",
                "lineWidth": 1,
            },
            "ma_long": {
                "name": "MA 长",
                "type": "line",
                "color": "#f0b44d",
                "lineWidth": 1,
            },
        },
        "subplots": {
            "MACD": {
                "macd_diff": {
                    "name": "DIFF",
                    "type": "line",
                    "color": "#43a5ff",
                    "lineWidth": 1,
                },
                "macd_dea": {
                    "name": "DEA",
                    "type": "line",
                    "color": "#f0b44d",
                    "lineWidth": 1,
                },
                "macd_hist": {
                    "name": "Histogram",
                    "type": "histogram",
                },
            },
            "波动率": {
                "atr": {
                    "name": "ATR",
                    "type": "line",
                    "color": "#9b59b6",
                    "lineWidth": 1,
                },
                "atr_median": {
                    "name": "ATR 中位数",
                    "type": "line",
                    "color": "#2ecc71",
                    "lineWidth": 1,
                },
            },
        },
    },
    mode=StrategyMode.PORTFOLIO,
    supports_short=True,
    requires_funding=False,
)
