"""
MACD 金叉死叉 + 三重过滤趋势交易策略（短预热优化版）。

设计要点：
1. MACD（DIFF/DEA）金叉死叉为核心交易信号；
2. 均线方向过滤：短期均线上/下穿长期均线，确认多/空趋势；
3. ATR 波动率过滤：当前 ATR 不低于近期 ATR 中位数 * 阈值，过滤低波动率假信号；
4. Choppiness 震荡过滤：CHOP 值（0~1 归一化）低于阈值，仅保留趋势区间信号。

支持做多做空、可对每个标的单独运行或组合运行；风险控制：
- 强制止损（ATR 倍数）+ 止盈（ATR 倍数）+ 可选追踪止损；
- 单笔风险敞口按账户资金比例计算，受最大仓位占比约束；
- 单日累计亏损超过账户 2% 暂停当日新开仓；
- MA 方向逆转立即平仓；
- 顺势金字塔加仓（加仓金额为上一笔 1/2，累计不超过 2 倍最大仓位）。
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
from math import log10
from statistics import median

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


# ============================================================
# 流式技术指标
# ============================================================


class Ema:
    """指数移动平均（流式）。首根 K 线直接以价格为初始值，之后递推更新。"""

    def __init__(self, period: int) -> None:
        if period <= 0:
            raise ValueError("EMA 周期必须大于 0")
        self.alpha = 2.0 / (period + 1.0)
        self.value: float | None = None

    def update(self, price: float) -> float:
        if self.value is None:
            self.value = price
        else:
            self.value += self.alpha * (price - self.value)
        return self.value


class Atr:
    """平均真实波幅（Wilder 平滑流式）。"""

    def __init__(self, period: int) -> None:
        if period <= 0:
            raise ValueError("ATR 周期必须大于 0")
        self.period = period
        self.value: float | None = None
        self._previous_close: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self._previous_close is None:
            tr = high - low
        else:
            tr = max(
                high - low,
                abs(high - self._previous_close),
                abs(low - self._previous_close),
            )

        if self.value is None:
            # 第一根 K 线的 TR 即作为 ATR 初值
            self.value = tr
        else:
            self.value = (self.value * (self.period - 1) + tr) / self.period

        self._previous_close = close
        return self.value


class ChoppinessNormalized:
    """Choppiness Index（归一化 0~1）。

    值越接近 1 表示越震荡，越接近 0 表示越趋势。
    在标准公式 ``100 * log10(sum_TR / range) / log10(period)`` 的基础上除以 100，
    使阈值可以用小数（如 0.4）直接表达。
    """

    def __init__(self, period: int) -> None:
        if period <= 1:
            raise ValueError("CHOP 周期必须大于 1")
        self.period = period
        self._tr_sum = 0.0
        self._tr_window: deque[float] = deque(maxlen=period)
        self._highs: deque[float] = deque(maxlen=period)
        self._lows: deque[float] = deque(maxlen=period)
        self._previous_close: float | None = None
        self.value: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self._previous_close is None:
            tr = high - low
        else:
            tr = max(
                high - low,
                abs(high - self._previous_close),
                abs(low - self._previous_close),
            )

        if len(self._tr_window) == self.period:
            self._tr_sum -= self._tr_window[0]
        self._tr_window.append(tr)
        self._tr_sum += tr
        self._highs.append(high)
        self._lows.append(low)
        self._previous_close = close

        if len(self._highs) < self.period:
            return None

        highest = max(self._highs)
        lowest = min(self._lows)
        price_range = highest - lowest

        if price_range <= 0.0 or self._tr_sum <= 0.0:
            self.value = 1.0  # 退化为完全震荡
        else:
            self.value = log10(self._tr_sum / price_range) / log10(self.period)

        return self.value


# ============================================================
# 标的级状态：每个 instrument 维护独立的指标与持仓跟踪
# ============================================================


class _SymbolState:
    """单个标的的指标状态与持仓跟踪。"""

    def __init__(
        self,
        instrument_id: InstrumentId,
        bar_type: BarType,
        macd_short_period: int,
        macd_long_period: int,
        macd_signal_period: int,
        ma_short_period: int,
        ma_long_period: int,
        atr_period: int,
        atr_lookback: int,
        chop_period: int,
    ) -> None:
        self.instrument_id = instrument_id
        self.bar_type = bar_type

        self.macd_short = Ema(macd_short_period)
        self.macd_long = Ema(macd_long_period)
        self.macd_signal = Ema(macd_signal_period)
        self.ma_short = Ema(ma_short_period)
        self.ma_long = Ema(ma_long_period)
        self.atr = Atr(atr_period)
        self.atr_window: deque[float] = deque(maxlen=atr_lookback)
        self.chop = ChoppinessNormalized(chop_period)

        self.bar_count = 0
        self.prev_diff: float | None = None
        self.prev_dea: float | None = None

        # 持仓跟踪
        self.position_side: str | None = None  # "LONG" | "SHORT"
        self.entry_price: float | None = None
        self.entry_atr: float | None = None
        self.stop_loss: float | None = None
        self.take_profit: float | None = None
        self.trailing_stop: float | None = None
        self.total_qty: float = 0.0
        self.last_add_qty: float = 0.0
        self.add_count: int = 0


# ============================================================
# 策略配置
# ============================================================


class MacdCrossMaAtrChopFilterTrendConfig(StrategyConfig, frozen=True):
    instrument_ids: list[InstrumentId]
    bar_types: list[BarType]

    # MACD 参数
    macd_short_period: int = 12
    macd_long_period: int = 26
    macd_signal_period: int = 9

    # 均线方向过滤
    ma_short_period: int = 5
    ma_long_period: int = 20

    # ATR 波动率过滤
    atr_current_period: int = 14
    atr_lookback_period: int = 10
    atr_filter_threshold: float = 0.8

    # Choppiness 震荡过滤
    chop_period: int = 14
    chop_threshold: float = 0.4

    # 出场参数
    stop_loss_atr_multiplier: float = 1.5
    take_profit_atr_multiplier: float = 3.0
    use_trailing_stop: bool = True

    # 仓位与风控参数
    trade_size: Decimal = Decimal("0.001")
    risk_per_trade_pct: float = 0.01  # 单笔风险占账户 1%
    max_position_pct: float = 0.10  # 单仓最大资金占用 10%
    max_daily_loss_pct: float = 0.02  # 单日累计亏损 2% 暂停开仓
    max_pyramid_adds: int = 1  # 至多顺势加仓 1 次（金字塔 1+0.5=1.5 倍）


# ============================================================
# 策略主体
# ============================================================


class MacdCrossMaAtrChopFilterTrendStrategy(Strategy):
    """MACD 金叉死叉 + 三重过滤趋势交易策略。"""

    def __init__(self, config: MacdCrossMaAtrChopFilterTrendConfig) -> None:
        super().__init__(config)

        # ---- 参数合法性校验 ----
        if len(config.instrument_ids) != len(config.bar_types):
            raise ValueError("instrument_ids 与 bar_types 数量必须一致")

        if not 0 < config.macd_short_period < config.macd_long_period:
            raise ValueError("MACD 周期必须满足 0 < 快线 < 慢线")
        if config.macd_signal_period <= 0:
            raise ValueError("MACD 信号线周期必须大于 0")

        if not 0 < config.ma_short_period < config.ma_long_period:
            raise ValueError("均线周期必须满足 0 < 短期 < 长期")

        if config.atr_current_period <= 0 or config.atr_lookback_period <= 0:
            raise ValueError("ATR 周期与回看期数必须大于 0")
        if config.atr_filter_threshold <= 0:
            raise ValueError("ATR 过滤阈值必须大于 0")

        if config.chop_period <= 1:
            raise ValueError("CHOP 周期必须大于 1")
        if not 0.0 < config.chop_threshold < 1.0:
            raise ValueError("CHOP 阈值必须位于 (0, 1) 区间内")

        if config.stop_loss_atr_multiplier <= 0:
            raise ValueError("止损 ATR 倍数必须大于 0")
        if config.take_profit_atr_multiplier <= 0:
            raise ValueError("止盈 ATR 倍数必须大于 0")
        if config.take_profit_atr_multiplier <= config.stop_loss_atr_multiplier:
            raise ValueError("止盈 ATR 倍数必须大于止损 ATR 倍数")

        if not 0 < config.risk_per_trade_pct < 1:
            raise ValueError("单笔风险比例必须位于 (0, 1) 区间内")
        if not 0 < config.max_position_pct < 1:
            raise ValueError("最大仓位占比必须位于 (0, 1) 区间内")
        if not 0 < config.max_daily_loss_pct < 1:
            raise ValueError("单日最大亏损比例必须位于 (0, 1) 区间内")
        if config.max_pyramid_adds < 0:
            raise ValueError("金字塔加仓次数不能为负数")

        # ---- 每个标的的独立状态 ----
        self._states: dict[InstrumentId, _SymbolState] = {}
        for instrument_id, bar_type in zip(
            config.instrument_ids, config.bar_types
        ):
            self._states[instrument_id] = _SymbolState(
                instrument_id=instrument_id,
                bar_type=bar_type,
                macd_short_period=config.macd_short_period,
                macd_long_period=config.macd_long_period,
                macd_signal_period=config.macd_signal_period,
                ma_short_period=config.ma_short_period,
                ma_long_period=config.ma_long_period,
                atr_period=config.atr_current_period,
                atr_lookback=config.atr_lookback_period,
                chop_period=config.chop_period,
            )

        # ---- 日级风控追踪 ----
        self._current_date: datetime | None = None
        self._daily_start_equity: float | None = None

    # --------------------------------------------------------
    # 生命周期
    # --------------------------------------------------------

    def on_start(self) -> None:
        for instrument_id in self.config.instrument_ids:
            instrument = self.cache.instrument(instrument_id)
            if instrument is None:
                self.log.error(f"找不到交易标的 {instrument_id}")
                self.stop()
                return

        for bar_type in self.config.bar_types:
            self.subscribe_bars(bar_type)

        self._current_date = None
        self._daily_start_equity = self._equity()

    def on_stop(self) -> None:
        for instrument_id in self.config.instrument_ids:
            self.cancel_all_orders(instrument_id)
            if not self.portfolio.is_flat(instrument_id):
                self.close_all_positions(instrument_id)
        for bar_type in self.config.bar_types:
            self.unsubscribe_bars(bar_type)

    # --------------------------------------------------------
    # 主循环
    # --------------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        instrument_id = bar.bar_type.instrument_id
        state = self._states.get(instrument_id)
        if state is None:
            return

        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            return

        high = float(bar.high)
        low = float(bar.low)
        close = float(bar.close)

        # ---- 1. 更新指标 ----
        diff = state.macd_short.update(close) - state.macd_long.update(close)
        dea = state.macd_signal.update(diff)
        ma_short = state.ma_short.update(close)
        ma_long = state.ma_long.update(close)
        current_atr = state.atr.update(high, low, close)
        chop_value = state.chop.update(high, low, close)

        state.bar_count += 1
        if current_atr is not None:
            state.atr_window.append(current_atr)

        # ---- 2. 日级风控重置 ----
        self._refresh_daily_baseline(bar)

        # ---- 3. 已有仓位：检查止盈/止损/追踪/MA 方向逆转 ----
        if not self.portfolio.is_flat(instrument_id):
            self._manage_open_position(
                state=state,
                high=high,
                low=low,
                close=close,
                ma_short=ma_short,
                ma_long=ma_long,
                current_atr=current_atr,
            )

        prev_diff = state.prev_diff
        prev_dea = state.prev_dea
        state.prev_diff = diff
        state.prev_dea = dea

        # ---- 4. 预热检查：核心指标与 ATR 中位数都需就绪 ----
        if not self._indicators_ready(state, current_atr):
            return

        # ---- 5. 公共过滤：波动率 + 震荡 + 当日风控 ----
        if not self._volatility_ok(current_atr, state):
            return
        if chop_value is None or chop_value >= self.config.chop_threshold:
            return
        if self._daily_loss_breached():
            return

        # ---- 6. 计算完整入场信号 ----
        long_signal = (
            ma_short > ma_long
            and prev_diff is not None
            and prev_dea is not None
            and prev_diff <= prev_dea
            and diff > dea
        )
        short_signal = (
            ma_short < ma_long
            and prev_diff is not None
            and prev_dea is not None
            and prev_diff >= prev_dea
            and diff < dea
        )

        if not long_signal and not short_signal:
            return

        is_long = self.portfolio.is_net_long(instrument_id)
        is_short = self.portfolio.is_net_short(instrument_id)

        # 反向完整信号：先平后反向开仓
        if is_long and short_signal:
            self.close_all_positions(instrument_id)
            self._reset_position_state(state)
            self._open_position(
                state=state,
                side="SHORT",
                close=close,
                current_atr=current_atr,
            )
            return

        if is_short and long_signal:
            self.close_all_positions(instrument_id)
            self._reset_position_state(state)
            self._open_position(
                state=state,
                side="LONG",
                close=close,
                current_atr=current_atr,
            )
            return

        # 空仓时的首次开仓
        if self.portfolio.is_flat(instrument_id):
            self._open_position(
                state=state,
                side="LONG" if long_signal else "SHORT",
                close=close,
                current_atr=current_atr,
            )
            return

        # 已持仓：顺势金字塔加仓（仓位已在浮盈且信号同向）
        if (is_long and long_signal) or (is_short and short_signal):
            self._maybe_pyramid(
                state=state,
                side="LONG" if is_long else "SHORT",
                close=close,
                current_atr=current_atr,
            )

    # --------------------------------------------------------
    # 持仓管理
    # --------------------------------------------------------

    def _manage_open_position(
        self,
        state: _SymbolState,
        high: float,
        low: float,
        close: float,
        ma_short: float,
        ma_long: float,
        current_atr: float | None,
    ) -> None:
        instrument_id = state.instrument_id
        side = state.position_side
        if side not in ("LONG", "SHORT"):
            return

        # MA 方向逆转：立即平仓
        if side == "LONG" and ma_short < ma_long:
            self.close_all_positions(instrument_id)
            self._reset_position_state(state)
            return
        if side == "SHORT" and ma_short > ma_long:
            self.close_all_positions(instrument_id)
            self._reset_position_state(state)
            return

        # 止损 / 止盈：使用本周期 high/low 判断是否被触发
        if state.stop_loss is not None:
            if side == "LONG" and low <= state.stop_loss:
                self.close_all_positions(instrument_id)
                self._reset_position_state(state)
                return
            if side == "SHORT" and high >= state.stop_loss:
                self.close_all_positions(instrument_id)
                self._reset_position_state(state)
                return

        if state.take_profit is not None:
            if side == "LONG" and high >= state.take_profit:
                self.close_all_positions(instrument_id)
                self._reset_position_state(state)
                return
            if side == "SHORT" and low <= state.take_profit:
                self.close_all_positions(instrument_id)
                self._reset_position_state(state)
                return

        # 追踪止损：仅在浮盈且开启追踪止损时更新
        if (
            self.config.use_trailing_stop
            and current_atr is not None
            and state.entry_atr is not None
        ):
            sl_distance = (
                self.config.stop_loss_atr_multiplier * current_atr
            )
            if side == "LONG":
                if state.entry_price is not None and close > state.entry_price:
                    new_stop = close - sl_distance
                    if (
                        state.trailing_stop is None
                        or new_stop > state.trailing_stop
                    ):
                        state.trailing_stop = new_stop
                # 用更紧的止损替换固定止损
                if (
                    state.trailing_stop is not None
                    and (
                        state.stop_loss is None
                        or state.trailing_stop > state.stop_loss
                    )
                ):
                    state.stop_loss = state.trailing_stop
            else:  # SHORT
                if state.entry_price is not None and close < state.entry_price:
                    new_stop = close + sl_distance
                    if (
                        state.trailing_stop is None
                        or new_stop < state.trailing_stop
                    ):
                        state.trailing_stop = new_stop
                if (
                    state.trailing_stop is not None
                    and (
                        state.stop_loss is None
                        or state.trailing_stop < state.stop_loss
                    )
                ):
                    state.stop_loss = state.trailing_stop

    # --------------------------------------------------------
    # 开仓 / 加仓
    # --------------------------------------------------------

    def _open_position(
        self,
        state: _SymbolState,
        side: str,
        close: float,
        current_atr: float,
    ) -> None:
        instrument = self.cache.instrument(state.instrument_id)
        if instrument is None or current_atr is None or current_atr <= 0.0:
            return

        qty = self._calculate_entry_size(
            instrument=instrument,
            close=close,
            current_atr=current_atr,
        )
        if qty <= 0.0:
            return

        order_side = OrderSide.BUY if side == "LONG" else OrderSide.SELL
        order = self.order_factory.market(
            instrument_id=state.instrument_id,
            order_side=order_side,
            quantity=instrument.make_qty(Decimal(str(qty))),
            time_in_force=TimeInForce.GTC,
        )
        self.submit_order(order)

        # 记录开仓信息（即使订单尚未成交也按预期入场价计算风控线）
        sl_distance = self.config.stop_loss_atr_multiplier * current_atr
        tp_distance = self.config.take_profit_atr_multiplier * current_atr
        state.position_side = side
        state.entry_price = close
        state.entry_atr = current_atr
        state.stop_loss = (
            close - sl_distance if side == "LONG" else close + sl_distance
        )
        state.take_profit = (
            close + tp_distance if side == "LONG" else close - tp_distance
        )
        state.trailing_stop = None
        state.total_qty = qty
        state.last_add_qty = qty
        state.add_count = 0

    def _maybe_pyramid(
        self,
        state: _SymbolState,
        side: str,
        close: float,
        current_atr: float,
    ) -> None:
        if state.position_side != side:
            return
        if state.entry_price is None or state.entry_atr is None:
            return
        if state.add_count >= self.config.max_pyramid_adds:
            return

        # 仅在浮盈时加仓（顺势）
        in_profit = (
            (side == "LONG" and close > state.entry_price)
            or (side == "SHORT" and close < state.entry_price)
        )
        if not in_profit:
            return

        instrument = self.cache.instrument(state.instrument_id)
        if instrument is None:
            return

        add_qty = state.last_add_qty / 2.0
        if add_qty <= 0.0:
            return

        # 总仓位不得超过 2 倍最大仓位限制
        equity = self._equity() or 0.0
        max_total_notional = (
            2.0 * self.config.max_position_pct * equity
            if equity > 0
            else float("inf")
        )
        price = float(close)
        proposed_total = (state.total_qty + add_qty) * price
        if (
            equity > 0
            and proposed_total > max_total_notional
        ):
            # 按可用额度缩放加仓数量
            available_notional = max(
                0.0, max_total_notional - state.total_qty * price
            )
            if available_notional <= 0.0:
                return
            add_qty = available_notional / price

        if add_qty <= 0.0:
            return

        order_side = OrderSide.BUY if side == "LONG" else OrderSide.SELL
        order = self.order_factory.market(
            instrument_id=state.instrument_id,
            order_side=order_side,
            quantity=instrument.make_qty(Decimal(str(add_qty))),
            time_in_force=TimeInForce.GTC,
        )
        self.submit_order(order)

        state.last_add_qty = add_qty
        state.total_qty += add_qty
        state.add_count += 1

    # --------------------------------------------------------
    # 仓位大小计算
    # --------------------------------------------------------

    def _calculate_entry_size(
        self,
        instrument,
        close: float,
        current_atr: float,
    ) -> float:
        equity = self._equity()
        if equity is None or equity <= 0.0 or close <= 0.0:
            return 0.0

        # 基于风险敞口的下单量
        risk_qty = (
            equity * self.config.risk_per_trade_pct
        ) / (
            self.config.stop_loss_atr_multiplier * current_atr
        )
        # 基于最大仓位占比的下单量
        max_qty = (
            equity * self.config.max_position_pct
        ) / close

        qty = min(risk_qty, max_qty)
        if qty <= 0.0:
            return 0.0

        # 按合约最小变动单位向下取整，避免超过可用资金
        try:
            step = float(instrument.size_increment.as_decimal())
        except Exception:
            step = 0.0
        if step > 0.0:
            qty = (qty // step) * step
        return max(qty, 0.0)

    # --------------------------------------------------------
    # 工具方法
    # --------------------------------------------------------

    def _indicators_ready(
        self,
        state: _SymbolState,
        current_atr: float | None,
    ) -> bool:
        if state.prev_diff is None or state.prev_dea is None:
            return False
        if current_atr is None:
            return False
        if state.chop.value is None:
            return False
        if len(state.atr_window) < self.config.atr_lookback_period:
            return False
        # 均线与 MACD 长周期需要足够的历史
        required = max(
            self.config.macd_long_period + self.config.macd_signal_period,
            self.config.ma_long_period,
        )
        if state.bar_count < required:
            return False
        return True

    def _volatility_ok(
        self,
        current_atr: float | None,
        state: _SymbolState,
    ) -> bool:
        if current_atr is None:
            return False
        if len(state.atr_window) < self.config.atr_lookback_period:
            return False
        median_atr = median(state.atr_window)
        if median_atr <= 0.0:
            return False
        return current_atr >= median_atr * self.config.atr_filter_threshold

    def _reset_position_state(self, state: _SymbolState) -> None:
        state.position_side = None
        state.entry_price = None
        state.entry_atr = None
        state.stop_loss = None
        state.take_profit = None
        state.trailing_stop = None
        state.total_qty = 0.0
        state.last_add_qty = 0.0
        state.add_count = 0

    def _equity(self) -> float | None:
        """获取当前账户净值（计价货币单位）。"""
        try:
            account = self.portfolio.account(
                self.cache.venue_for_instrument(
                    self.config.instrument_ids[0]
                )
            )
        except Exception:
            return None
        if account is None:
            return None
        try:
            money = account.equity()
        except Exception:
            return None
        if money is None:
            return None
        try:
            return float(money.as_decimal())
        except Exception:
            return None

    def _refresh_daily_baseline(self, bar: Bar) -> None:
        bar_date = datetime.fromtimestamp(
            bar.ts_event // 1_000_000_000, tz=timezone.utc
        ).date()
        if self._current_date != bar_date:
            self._current_date = bar_date
            self._daily_start_equity = self._equity()

    def _daily_loss_breached(self) -> bool:
        if self._daily_start_equity is None or self._daily_start_equity <= 0:
            return False
        equity = self._equity()
        if equity is None:
            return False
        loss_ratio = (
            self._daily_start_equity - equity
        ) / self._daily_start_equity
        return loss_ratio >= self.config.max_daily_loss_pct


# ============================================================
# 指标批量计算（用于回测结果展示）
# ============================================================


def calculate_indicators(
    dataframe: pd.DataFrame,
    parameters: dict,
) -> pd.DataFrame:
    """
    为回测结果页面批量计算指标序列。

    返回的 DataFrame 行数与时间顺序必须保持不变。
    plot_config 引用的所有列都必须在这里生成。
    """
    macd_short = int(parameters.get("macd_short_period", 12))
    macd_long = int(parameters.get("macd_long_period", 26))
    macd_signal = int(parameters.get("macd_signal_period", 9))
    ma_short_p = int(parameters.get("ma_short_period", 5))
    ma_long_p = int(parameters.get("ma_long_period", 20))
    atr_p = int(parameters.get("atr_current_period", 14))
    atr_lookback = int(parameters.get("atr_lookback_period", 10))
    chop_p = int(parameters.get("chop_period", 14))

    if not 0 < macd_short < macd_long:
        raise ValueError("MACD 周期必须满足 0 < 快线 < 慢线")
    if macd_signal <= 0:
        raise ValueError("MACD 信号线周期必须大于 0")
    if not 0 < ma_short_p < ma_long_p:
        raise ValueError("均线周期必须满足 0 < 短期 < 长期")
    if atr_p <= 0 or atr_lookback <= 0:
        raise ValueError("ATR 周期与回看期数必须大于 0")
    if chop_p <= 1:
        raise ValueError("CHOP 周期必须大于 1")

    close = pd.to_numeric(dataframe["close"], errors="coerce")
    high = pd.to_numeric(
        dataframe.get("high", close), errors="coerce"
    )
    low = pd.to_numeric(
        dataframe.get("low", close), errors="coerce"
    )

    # MACD
    dataframe["macd_short_ema"] = close.ewm(
        span=macd_short, adjust=False
    ).mean()
    dataframe["macd_long_ema"] = close.ewm(
        span=macd_long, adjust=False
    ).mean()
    dataframe["diff"] = (
        dataframe["macd_short_ema"] - dataframe["macd_long_ema"]
    )
    dataframe["dea"] = dataframe["diff"].ewm(
        span=macd_signal, adjust=False
    ).mean()
    dataframe["macd_hist"] = dataframe["diff"] - dataframe["dea"]

    # 均线
    dataframe["ma_short"] = close.ewm(
        span=ma_short_p, adjust=False
    ).mean()
    dataframe["ma_long"] = close.ewm(
        span=ma_long_p, adjust=False
    ).mean()

    # ATR (Wilder 平滑，与事件驱动一致)
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr_series = true_range.ewm(
        alpha=1.0 / atr_p, adjust=False
    ).mean()
    dataframe["atr"] = atr_series
    dataframe["atr_median"] = atr_series.rolling(
        window=atr_lookback
    ).median()

    # Choppiness Index (0~1 归一化)
    tr_sum = true_range.rolling(window=chop_p).sum()
    highest_high = high.rolling(window=chop_p).max()
    lowest_low = low.rolling(window=chop_p).min()
    price_range = highest_high - lowest_low
    safe_tr = tr_sum.where(tr_sum > 0.0)
    safe_range = price_range.where(price_range > 0.0)
    ratio = safe_tr / safe_range
    log_ratio = np.log10(ratio.where(ratio > 0.0))
    chop = log_ratio / log10(chop_p)
    chop = chop.fillna(1.0)
    dataframe["chop"] = chop

    return dataframe


# ============================================================
# 策略清单
# ============================================================


STRATEGY_MANIFEST = StrategyManifest(
    slug="macd-cross-ma-atr-chop-filter-trend",
    name="MACD 三重过滤趋势策略",
    version="0.1.0",
    description=(
        "MACD 金叉死叉 + 均线方向 + ATR 波动率 + Choppiness 震荡"
        "三重过滤的趋势交易策略。"
        "均线多头排列下金叉做多，空头排列下死叉做空；"
        "ATR 过滤剔除低波动率窄幅震荡，CHOP 过滤剔除震荡区间；"
        "固定 ATR 倍数止损止盈，可选追踪止损；"
        "支持顺势金字塔加仓与单日亏损风控。"
    ),
    category="趋势",
    strategy_path=(
        "app.strategies.macd_cross_ma_atr_chop_filter_trend:"
        "MacdCrossMaAtrChopFilterTrendStrategy"
    ),
    config_path=(
        "app.strategies.macd_cross_ma_atr_chop_filter_trend:"
        "MacdCrossMaAtrChopFilterTrendConfig"
    ),
    parameters={
        "macd_short_period": ParameterSpec(
            "MACD 快线周期",
            "integer",
            12,
            2,
            100,
        ),
        "macd_long_period": ParameterSpec(
            "MACD 慢线周期",
            "integer",
            26,
            3,
            200,
        ),
        "macd_signal_period": ParameterSpec(
            "MACD 信号线周期",
            "integer",
            9,
            2,
            100,
        ),
        "ma_short_period": ParameterSpec(
            "短期均线周期",
            "integer",
            5,
            2,
            100,
        ),
        "ma_long_period": ParameterSpec(
            "长期均线周期",
            "integer",
            20,
            3,
            200,
        ),
        "atr_current_period": ParameterSpec(
            "ATR 计算周期",
            "integer",
            14,
            2,
            200,
        ),
        "atr_lookback_period": ParameterSpec(
            "ATR 中位数回看期数",
            "integer",
            10,
            2,
            200,
        ),
        "atr_filter_threshold": ParameterSpec(
            "ATR 波动率过滤阈值",
            "number",
            0.8,
            0.1,
            5.0,
            "当前 ATR 须 >= 过去 N 周期 ATR 中位数 * 阈值",
        ),
        "chop_period": ParameterSpec(
            "Choppiness 周期",
            "integer",
            14,
            2,
            200,
        ),
        "chop_threshold": ParameterSpec(
            "Choppiness 震荡阈值（0~1）",
            "number",
            0.4,
            0.05,
            0.95,
            "CHOP 低于阈值视为趋势区间",
        ),
        "stop_loss_atr_multiplier": ParameterSpec(
            "止损 ATR 倍数",
            "number",
            1.5,
            0.1,
            10.0,
        ),
        "take_profit_atr_multiplier": ParameterSpec(
            "止盈 ATR 倍数",
            "number",
            3.0,
            0.1,
            20.0,
        ),
        "use_trailing_stop": ParameterSpec(
            "是否启用追踪止损",
            "boolean",
            True,
        ),
        "trade_size": ParameterSpec(
            "默认下单数量（仅在无法计算风险敞口时使用）",
            "number",
            0.001,
            0.000001,
            1000,
        ),
        "risk_per_trade_pct": ParameterSpec(
            "单笔风险占总资金比例",
            "number",
            0.01,
            0.001,
            0.1,
        ),
        "max_position_pct": ParameterSpec(
            "单仓最大资金占用比例",
            "number",
            0.10,
            0.01,
            1.0,
        ),
        "max_daily_loss_pct": ParameterSpec(
            "单日最大亏损占比（达到后当日停止新开仓）",
            "number",
            0.02,
            0.005,
            0.5,
        ),
        "max_pyramid_adds": ParameterSpec(
            "顺势金字塔加仓次数",
            "integer",
            1,
            0,
            5,
        ),
    },
    timeframes=("4h", "1d"),
    primary_timeframe="4h",
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
                "lineWidth": 2,
            },
        },
        "subplots": {
            "MACD": {
                "diff": {
                    "name": "DIFF",
                    "type": "line",
                    "color": "#43a5ff",
                },
                "dea": {
                    "name": "DEA",
                    "type": "line",
                    "color": "#f0b44d",
                },
                "macd_hist": {
                    "name": "Histogram",
                    "type": "histogram",
                },
            },
            "ATR": {
                "atr": {
                    "name": "ATR",
                    "type": "line",
                    "color": "#1abc9c",
                },
                "atr_median": {
                    "name": "ATR 中位数",
                    "type": "line",
                    "color": "#9b59b6",
                },
            },
            "CHOP": {
                "chop": {
                    "name": "Choppiness",
                    "type": "line",
                    "color": "#9b59b6",
                },
            },
        },
    },
    mode=StrategyMode.PORTFOLIO,
    supports_short=True,
    requires_funding=False,
)
