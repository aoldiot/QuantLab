from __future__ import annotations

from collections import deque
from decimal import Decimal
from math import log10
from typing import Any

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


class MacdCrossTripleFilterTrendShortConfig(StrategyConfig, frozen=True):
    instrument_ids: list[InstrumentId]
    bar_types: list[BarType]
    trade_size: Decimal = Decimal("0.001")
    macd_short_period: int = 12
    macd_long_period: int = 26
    macd_signal_period: int = 9
    ma_short_period: int = 5
    ma_long_period: int = 20
    atr_current_period: int = 14
    atr_lookback_period: int = 10
    atr_filter_threshold: float = 0.8
    chop_period: int = 14
    chop_threshold: float = 0.4
    stop_loss_atr_multiplier: float = 1.5
    take_profit_atr_multiplier: float = 3.0
    risk_per_trade_pct: float = 0.01
    max_position_pct: float = 0.1
    daily_loss_limit_pct: float = 0.02
    enable_trailing_stop: bool = True


class _Ema:
    """递归 EMA 计算器，与 ``pd.Series.ewm(span=period, adjust=False).mean()`` 等价。"""

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


class _Atr:
    """流式 ATR（Wilder 平滑）计算器。"""

    def __init__(self, period: int) -> None:
        if period <= 0:
            raise ValueError("ATR 周期必须大于 0")
        self.period = period
        self._previous_close: float | None = None
        self._value: float | None = None
        self._count = 0

    def update(self, high: float, low: float, close: float) -> float | None:
        if self._previous_close is None:
            tr = high - low
        else:
            tr = max(
                high - low,
                abs(high - self._previous_close),
                abs(low - self._previous_close),
            )
        self._previous_close = close
        self._count += 1
        if self._count < self.period:
            return None
        if self._value is None:
            self._value = tr
        else:
            self._value = (self._value * (self.period - 1) + tr) / self.period
        return self._value


class _ChoppinessIndex:
    """Choppiness Index (CHOP) 流式计算器，区间 0~1。

    0 表示最强趋势，1 表示最强震荡。规格要求阈值在 (0, 1] 之间，
    因此将原始 0~100 的结果归一化到 0~1。
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
        self._value: float | None = None

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
        range_ = highest - lowest
        if range_ <= 0.0 or self._tr_sum <= 0.0:
            self._value = 1.0
        else:
            self._value = log10(self._tr_sum / range_) / log10(self.period)
        return self._value


def _median(values: deque[float]) -> float:
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n % 2 == 1:
        return sorted_vals[n // 2]
    return (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2.0


class _SymbolState:
    """每个交易标的维护一份独立的指标与仓位状态。"""

    def __init__(self, config: MacdCrossTripleFilterTrendShortConfig) -> None:
        self.macd_fast = _Ema(config.macd_short_period)
        self.macd_slow = _Ema(config.macd_long_period)
        self.macd_signal = _Ema(config.macd_signal_period)
        self.ma_short = _Ema(config.ma_short_period)
        self.ma_long = _Ema(config.ma_long_period)
        self.atr = _Atr(config.atr_current_period)
        self.chop = _ChoppinessIndex(config.chop_period)
        self.atr_history: deque[float] = deque(maxlen=config.atr_lookback_period)
        self.previous_diff: float | None = None
        self.previous_dea: float | None = None
        self.previous_close: float | None = None
        self.bar_count = 0
        # 仓位状态
        self.position_side: str = "FLAT"  # "LONG" | "SHORT" | "FLAT"
        self.entry_price: float | None = None
        self.entry_atr: float | None = None
        self.stop_loss_price: float | None = None
        self.take_profit_price: float | None = None
        self.trailing_stop_price: float | None = None


class MacdCrossTripleFilterTrendShortStrategy(Strategy):
    """MACD 金叉死叉 + 三重过滤趋势交易策略（短预热优化版）。

    进场条件：DIFF 与 DEA 形成金叉或死叉，且同时通过三层过滤：
      1. 均线方向过滤：顺势方向（多头仅在 MA_short > MA_long 时进场）；
      2. ATR 波动率过滤：当前 ATR >= 过去 atr_lookback_period 个周期 ATR 中位数 * atr_filter_threshold；
      3. Choppiness 震荡过滤：当前 CHOP 值 < chop_threshold（仅保留趋势区间信号）。

    出场条件：
      - 触及基于 ATR 的止损/止盈价位；
      - 启用跟踪止损后，每周期按最新收盘价与最新 ATR 重置止损；
      - 持仓方向与均线方向相反（趋势反转）时立即平仓；
      - 反向完整进场信号触发时，平掉当前持仓并开立反向仓位。

    风控约束：
      - 单日账户亏损超过日初权益的 daily_loss_limit_pct 时，停止当日新开仓；
      - 单仓不超过 max_position_pct 的账户权益，超过时按比例下调下单数量。
    """

    def __init__(self, config: MacdCrossTripleFilterTrendShortConfig) -> None:
        super().__init__(config)

        if config.macd_short_period <= 0 or config.macd_short_period >= config.macd_long_period:
            raise ValueError("MACD 周期必须满足 0 < 快线 < 慢线")
        if config.macd_signal_period <= 0:
            raise ValueError("MACD 信号线周期必须大于 0")
        if config.ma_short_period <= 0 or config.ma_short_period >= config.ma_long_period:
            raise ValueError("均线周期必须满足 0 < 短期 < 长期")
        if config.atr_current_period <= 0:
            raise ValueError("ATR 周期必须大于 0")
        if config.atr_lookback_period <= 0:
            raise ValueError("ATR 回看周期必须大于 0")
        if config.atr_filter_threshold <= 0.0:
            raise ValueError("ATR 过滤阈值必须大于 0")
        if config.chop_period <= 1:
            raise ValueError("Choppiness 周期必须大于 1")
        if not 0.0 < config.chop_threshold <= 1.0:
            raise ValueError("Choppiness 阈值必须位于 (0, 1] 区间内")
        if config.stop_loss_atr_multiplier <= 0:
            raise ValueError("止损 ATR 倍数必须大于 0")
        if config.take_profit_atr_multiplier <= 0:
            raise ValueError("止盈 ATR 倍数必须大于 0")
        if not 0.0 < config.risk_per_trade_pct < 1.0:
            raise ValueError("单笔风险比例必须位于 (0, 1) 区间内")
        if not 0.0 < config.max_position_pct <= 1.0:
            raise ValueError("最大仓位比例必须位于 (0, 1] 区间内")
        if not 0.0 < config.daily_loss_limit_pct < 1.0:
            raise ValueError("单日亏损限制比例必须位于 (0, 1) 区间内")
        if config.trade_size <= Decimal("0"):
            raise ValueError("trade_size 必须大于 0")

        self._states: dict[InstrumentId, _SymbolState] = {}
        self._instruments: dict[InstrumentId, Any] = {}
        self._daily_equity_open: dict[Any, float] = {}
        self._daily_loss_halted: dict[Any, bool] = {}

    # ------------------------------------------------------------------ lifecycle

    def on_start(self) -> None:
        for instrument_id in self.config.instrument_ids:
            instrument = self.cache.instrument(instrument_id)
            if instrument is None:
                self.log.error(f"找不到交易标的 {instrument_id}，跳过")
                continue
            self._instruments[instrument_id] = instrument
            self._states[instrument_id] = _SymbolState(self.config)
        for bar_type in self.config.bar_types:
            self.subscribe_bars(bar_type)

    def on_stop(self) -> None:
        for bar_type in self.config.bar_types:
            self.unsubscribe_bars(bar_type)

    # ----------------------------------------------------------------- bar entry

    def on_bar(self, bar: Bar) -> None:
        instrument_id = bar.bar_type.instrument_id
        state = self._states.get(instrument_id)
        if state is None:
            return

        high = float(bar.high)
        low = float(bar.low)
        close = float(bar.close)
        bar_ts = bar.ts_event
        bar_date = self._date_for(bar_ts)

        self._update_daily_equity_baseline(bar_date)
        if self._daily_loss_halted.get(bar_date, False):
            self._update_indicators(state, high, low, close)
            self._check_exit_only(state, instrument_id, high, low, close)
            state.previous_close = close
            return

        # 1. 推进指标
        ma_short = state.ma_short.update(close)
        ma_long = state.ma_long.update(close)
        macd_fast = state.macd_fast.update(close)
        macd_slow = state.macd_slow.update(close)
        diff = macd_fast - macd_slow
        dea = state.macd_signal.update(diff)
        atr_value = state.atr.update(high, low, close)
        chop_value = state.chop.update(high, low, close)
        state.bar_count += 1

        # 2. 持仓阶段：先评估出场（止损/止盈/跟踪/趋势反转/反向信号）
        if state.position_side != "FLAT":
            self._update_trailing_stop(state, close, atr_value)
            exit_reason = self._evaluate_exit(state, instrument_id, high, low, close, ma_short, ma_long, diff, dea, atr_value, chop_value)
            if exit_reason is not None:
                self._close_position(instrument_id, reason=exit_reason)
                state.previous_diff = diff
                state.previous_dea = dea
                state.previous_close = close
                if atr_value is not None:
                    self._push_atr(state, atr_value)
                return
        else:
            # 维护上一根 ATR 历史供过滤使用
            if atr_value is not None:
                self._push_atr(state, atr_value)

        # 3. 平仓后或无持仓：评估进场
        warmup_bars = max(
            self.config.macd_long_period,
            self.config.ma_long_period,
            self.config.atr_current_period,
            self.config.chop_period,
        )
        if state.bar_count < warmup_bars:
            state.previous_diff = diff
            state.previous_dea = dea
            state.previous_close = close
            return
        if (
            state.previous_diff is None
            or state.previous_dea is None
            or atr_value is None
            or chop_value is None
        ):
            state.previous_diff = diff
            state.previous_dea = dea
            return
        if len(state.atr_history) < self.config.atr_lookback_period:
            state.previous_diff = diff
            state.previous_dea = dea
            return

        atr_median = _median(state.atr_history)
        long_signal = self._long_signal(
            ma_short=ma_short,
            ma_long=ma_long,
            diff=diff,
            dea=dea,
            atr=atr_value,
            atr_median=atr_median,
            chop=chop_value,
            state=state,
        )
        short_signal = self._short_signal(
            ma_short=ma_short,
            ma_long=ma_long,
            diff=diff,
            dea=dea,
            atr=atr_value,
            atr_median=atr_median,
            chop=chop_value,
            state=state,
        )

        if long_signal and short_signal:
            # 极小概率事件（同一根 bar 同时金叉与死叉），按保守原则不开仓
            self.log.warning("同一周期同时触发金叉与死叉，跳过本次信号")
        elif long_signal:
            if state.position_side == "SHORT":
                self._close_position(instrument_id, reason="reverse_signal")
            self._open_long(instrument_id, close, atr_value)
        elif short_signal:
            if state.position_side == "LONG":
                self._close_position(instrument_id, reason="reverse_signal")
            self._open_short(instrument_id, close, atr_value)

        state.previous_diff = diff
        state.previous_dea = dea
        state.previous_close = close

    # ----------------------------------------------------------- signal helpers

    def _long_signal(
        self,
        ma_short: float,
        ma_long: float,
        diff: float,
        dea: float,
        atr: float,
        atr_median: float,
        chop: float,
        state: _SymbolState,
    ) -> bool:
        if ma_short <= ma_long:
            return False
        if state.previous_diff is None or state.previous_dea is None:
            return False
        if not (state.previous_diff <= state.previous_dea and diff > dea):
            return False
        if atr < atr_median * self.config.atr_filter_threshold:
            return False
        if chop >= self.config.chop_threshold:
            return False
        return True

    def _short_signal(
        self,
        ma_short: float,
        ma_long: float,
        diff: float,
        dea: float,
        atr: float,
        atr_median: float,
        chop: float,
        state: _SymbolState,
    ) -> bool:
        if ma_short >= ma_long:
            return False
        if state.previous_diff is None or state.previous_dea is None:
            return False
        if not (state.previous_diff >= state.previous_dea and diff < dea):
            return False
        if atr < atr_median * self.config.atr_filter_threshold:
            return False
        if chop >= self.config.chop_threshold:
            return False
        return True

    # ---------------------------------------------------------------- exits

    def _evaluate_exit(
        self,
        state: _SymbolState,
        instrument_id: InstrumentId,
        high: float,
        low: float,
        close: float,
        ma_short: float,
        ma_long: float,
        diff: float,
        dea: float,
        atr: float,
        chop: float,
    ) -> str | None:
        if state.position_side == "LONG":
            if state.stop_loss_price is not None and low <= state.stop_loss_price:
                return "stop_loss"
            if state.take_profit_price is not None and high >= state.take_profit_price:
                return "take_profit"
            if ma_short < ma_long:
                return "ma_reversal"
            if (
                state.previous_diff is not None
                and state.previous_dea is not None
                and atr is not None
                and chop is not None
                and len(state.atr_history) >= self.config.atr_lookback_period
            ):
                short_signal = self._short_signal(
                    ma_short=ma_short,
                    ma_long=ma_long,
                    diff=diff,
                    dea=dea,
                    atr=atr,
                    atr_median=_median(state.atr_history),
                    chop=chop,
                    state=state,
                )
                if short_signal:
                    return "reverse_signal"
        elif state.position_side == "SHORT":
            if state.stop_loss_price is not None and high >= state.stop_loss_price:
                return "stop_loss"
            if state.take_profit_price is not None and low <= state.take_profit_price:
                return "take_profit"
            if ma_short > ma_long:
                return "ma_reversal"
            if (
                state.previous_diff is not None
                and state.previous_dea is not None
                and atr is not None
                and chop is not None
                and len(state.atr_history) >= self.config.atr_lookback_period
            ):
                long_signal = self._long_signal(
                    ma_short=ma_short,
                    ma_long=ma_long,
                    diff=diff,
                    dea=dea,
                    atr=atr,
                    atr_median=_median(state.atr_history),
                    chop=chop,
                    state=state,
                )
                if long_signal:
                    return "reverse_signal"
        return None

    def _check_exit_only(
        self,
        state: _SymbolState,
        instrument_id: InstrumentId,
        high: float,
        low: float,
        close: float,
    ) -> None:
        if state.position_side == "FLAT":
            return
        ma_short = state.ma_short.value
        ma_long = state.ma_long.value
        if ma_short is None or ma_long is None:
            return
        if state.position_side == "LONG":
            if state.stop_loss_price is not None and low <= state.stop_loss_price:
                self._close_position(instrument_id, reason="stop_loss")
                return
            if state.take_profit_price is not None and high >= state.take_profit_price:
                self._close_position(instrument_id, reason="take_profit")
                return
            if ma_short < ma_long:
                self._close_position(instrument_id, reason="ma_reversal")
        elif state.position_side == "SHORT":
            if state.stop_loss_price is not None and high >= state.stop_loss_price:
                self._close_position(instrument_id, reason="stop_loss")
                return
            if state.take_profit_price is not None and low <= state.take_profit_price:
                self._close_position(instrument_id, reason="take_profit")
                return
            if ma_short > ma_long:
                self._close_position(instrument_id, reason="ma_reversal")

    def _update_trailing_stop(
        self,
        state: _SymbolState,
        close: float,
        atr_value: float | None,
    ) -> None:
        if not self.config.enable_trailing_stop:
            return
        if state.position_side == "FLAT" or state.entry_atr is None:
            return
        if atr_value is None or atr_value <= 0.0:
            return
        if state.position_side == "LONG":
            new_stop = close - self.config.stop_loss_atr_multiplier * atr_value
            if state.stop_loss_price is None or new_stop > state.stop_loss_price:
                state.stop_loss_price = new_stop
                state.trailing_stop_price = new_stop
        elif state.position_side == "SHORT":
            new_stop = close + self.config.stop_loss_atr_multiplier * atr_value
            if state.stop_loss_price is None or new_stop < state.stop_loss_price:
                state.stop_loss_price = new_stop
                state.trailing_stop_price = new_stop

    # --------------------------------------------------------------- order flow

    def _open_long(self, instrument_id: InstrumentId, price: float, atr_value: float | None) -> None:
        instrument = self._instruments.get(instrument_id)
        if instrument is None:
            return
        if atr_value is None or atr_value <= 0.0:
            return
        quantity = self._calculate_order_size(instrument, atr_value)
        if quantity <= Decimal("0"):
            return
        order = self.order_factory.market(
            instrument_id=instrument_id,
            order_side=OrderSide.BUY,
            quantity=quantity,
            time_in_force=TimeInForce.IOC,
        )
        self.submit_order(order)
        state = self._states[instrument_id]
        state.position_side = "LONG"
        state.entry_price = price
        state.entry_atr = atr_value
        state.stop_loss_price = price - self.config.stop_loss_atr_multiplier * atr_value
        state.take_profit_price = price + self.config.take_profit_atr_multiplier * atr_value
        state.trailing_stop_price = state.stop_loss_price

    def _open_short(self, instrument_id: InstrumentId, price: float, atr_value: float | None) -> None:
        instrument = self._instruments.get(instrument_id)
        if instrument is None:
            return
        if atr_value is None or atr_value <= 0.0:
            return
        quantity = self._calculate_order_size(instrument, atr_value)
        if quantity <= Decimal("0"):
            return
        order = self.order_factory.market(
            instrument_id=instrument_id,
            order_side=OrderSide.SELL,
            quantity=quantity,
            time_in_force=TimeInForce.IOC,
        )
        self.submit_order(order)
        state = self._states[instrument_id]
        state.position_side = "SHORT"
        state.entry_price = price
        state.entry_atr = atr_value
        state.stop_loss_price = price + self.config.stop_loss_atr_multiplier * atr_value
        state.take_profit_price = price - self.config.take_profit_atr_multiplier * atr_value
        state.trailing_stop_price = state.stop_loss_price

    def _close_position(self, instrument_id: InstrumentId, reason: str) -> None:
        state = self._states.get(instrument_id)
        if state is None or state.position_side == "FLAT":
            return
        try:
            self.close_all_positions(instrument_id, reduce_only=True)
        except TypeError:
            self.close_all_positions(instrument_id)
        self.log.info(f"平仓 {instrument_id}，原因: {reason}")
        state.position_side = "FLAT"
        state.entry_price = None
        state.entry_atr = None
        state.stop_loss_price = None
        state.take_profit_price = None
        state.trailing_stop_price = None

    # --------------------------------------------------------------- position sizing

    def _calculate_order_size(self, instrument: Any, atr_value: float) -> Decimal:
        equity = self._current_equity()
        if equity <= 0.0:
            return Decimal("0")
        # ATR 风险调整：size = (equity * risk_pct) / (sl_mult * ATR)
        risk_amount = equity * self.config.risk_per_trade_pct
        sl_distance = self.config.stop_loss_atr_multiplier * atr_value
        if sl_distance <= 0.0:
            return Decimal("0")
        price = float(self._last_price(instrument))
        if price <= 0.0:
            return Decimal("0")
        raw_size = risk_amount / sl_distance
        notional_cap = equity * self.config.max_position_pct
        max_size = notional_cap / price
        size_value = min(raw_size, max_size)
        if size_value <= 0.0:
            return Decimal("0")
        return instrument.make_qty(Decimal(str(size_value)))

    def _current_equity(self) -> float:
        account = self.portfolio.account(self.config.instrument_ids[0].venue) if self.config.instrument_ids else None
        if account is not None:
            try:
                return float(account.equity())
            except Exception:
                pass
        try:
            return float(self.portfolio.net_asset_value())
        except Exception:
            return 0.0

    def _last_price(self, instrument: Any) -> float:
        # 使用最近一次 bar 的 close 作为估算；优先缓存的最近价
        try:
            instrument_id = instrument.id
        except AttributeError:
            instrument_id = instrument
        state = self._states.get(instrument_id) if isinstance(instrument_id, InstrumentId) else None
        if state is not None and state.previous_close is not None:
            return state.previous_close
        return 0.0

    # --------------------------------------------------------------- daily guard

    def _update_indicators(self, state: _SymbolState, high: float, low: float, close: float) -> None:
        state.ma_short.update(close)
        state.ma_long.update(close)
        macd_fast = state.macd_fast.update(close)
        macd_slow = state.macd_slow.update(close)
        diff = macd_fast - macd_slow
        dea = state.macd_signal.update(diff)
        atr_value = state.atr.update(high, low, close)
        state.chop.update(high, low, close)
        state.bar_count += 1
        state.previous_diff = diff
        state.previous_dea = dea
        state.previous_close = close
        if atr_value is not None:
            self._push_atr(state, atr_value)

    def _push_atr(self, state: _SymbolState, atr_value: float) -> None:
        if len(state.atr_history) == self.config.atr_lookback_period:
            # deque(maxlen) 会在 append 时丢弃最旧元素，无需手动 pop
            pass
        state.atr_history.append(atr_value)

    def _date_for(self, ts_event: int) -> Any:
        try:
            from datetime import datetime, timezone
            return datetime.fromtimestamp(ts_event / 1_000_000_000, tz=timezone.utc).date()
        except Exception:
            return ts_event

    def _update_daily_equity_baseline(self, bar_date: Any) -> None:
        if bar_date in self._daily_equity_open:
            return
        equity = self._current_equity()
        if equity <= 0.0:
            return
        self._daily_equity_open[bar_date] = equity
        self._daily_loss_halted[bar_date] = False

    def _check_daily_loss(self, bar_date: Any) -> None:
        baseline = self._daily_equity_open.get(bar_date)
        if baseline is None or baseline <= 0.0:
            return
        equity = self._current_equity()
        if equity <= 0.0:
            return
        if (baseline - equity) / baseline >= self.config.daily_loss_limit_pct:
            self._daily_loss_halted[bar_date] = True
            self.log.warning(
                f"{bar_date} 当日亏损已达上限，暂停新开仓"
            )

    def on_trade(self, trade: Any) -> None:
        bar_date = self._date_for(trade.ts_event)
        self._update_daily_equity_baseline(bar_date)
        self._check_daily_loss(bar_date)


# --------------------------------------------------------------------- indicators


def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    """为回测结果页面批量计算指标序列。

    返回的 DataFrame 行数与时间顺序必须保持不变。
    ``plot_config`` 引用的所有列都必须在这里生成。
    """
    macd_short = int(parameters.get("macd_short_period", 12))
    macd_long = int(parameters.get("macd_long_period", 26))
    signal_period = int(parameters.get("macd_signal_period", 9))
    ma_short = int(parameters.get("ma_short_period", 5))
    ma_long = int(parameters.get("ma_long_period", 20))
    atr_period = int(parameters.get("atr_current_period", 14))
    chop_period = int(parameters.get("chop_period", 14))

    if not 0 < macd_short < macd_long:
        raise ValueError("MACD 周期必须满足 0 < 快线 < 慢线")
    if signal_period <= 0:
        raise ValueError("MACD 信号线周期必须大于 0")
    if not 0 < ma_short < ma_long:
        raise ValueError("均线周期必须满足 0 < 短期 < 长期")
    if atr_period <= 0:
        raise ValueError("ATR 周期必须大于 0")
    if chop_period <= 1:
        raise ValueError("Choppiness 周期必须大于 1")

    close = pd.to_numeric(dataframe["close"], errors="coerce")
    if "high" in dataframe.columns:
        high = pd.to_numeric(dataframe["high"], errors="coerce")
    else:
        high = close
    if "low" in dataframe.columns:
        low = pd.to_numeric(dataframe["low"], errors="coerce")
    else:
        low = close

    dataframe["ma_short"] = close.ewm(span=ma_short, adjust=False).mean()
    dataframe["ma_long"] = close.ewm(span=ma_long, adjust=False).mean()

    ema_fast = close.ewm(span=macd_short, adjust=False).mean()
    ema_slow = close.ewm(span=macd_long, adjust=False).mean()
    dataframe["macd_diff"] = ema_fast - ema_slow
    dataframe["macd_dea"] = dataframe["macd_diff"].ewm(span=signal_period, adjust=False).mean()
    dataframe["macd_histogram"] = dataframe["macd_diff"] - dataframe["macd_dea"]

    # ATR (Wilder)
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr_series = true_range.ewm(alpha=1.0 / atr_period, adjust=False).mean()
    dataframe["atr"] = atr_series

    # Choppiness Index (归一化到 0~1)
    tr_sum = true_range.rolling(window=chop_period).sum()
    highest_high = high.rolling(window=chop_period).max()
    lowest_low = low.rolling(window=chop_period).min()
    price_range = highest_high - lowest_low
    safe_tr_sum = tr_sum.where(tr_sum > 0.0)
    safe_range = price_range.where(price_range > 0.0)
    ratio = safe_tr_sum / safe_range
    log_ratio = np.log10(ratio.where(ratio > 0.0))
    chop_normalized = log_ratio / log10(chop_period)
    dataframe["chop"] = chop_normalized.fillna(1.0)

    return dataframe


# ---------------------------------------------------------------------- manifest


STRATEGY_MANIFEST = StrategyManifest(
    slug="macd-cross-triple-filter-trend-short",
    name="MacdCrossTripleFilterTrendShort",
    version="0.1.0",
    description=(
        "MACD 金叉死叉 + 均线方向 + ATR 中位数 + Choppiness 三重过滤趋势策略（短预热版）。"
        "多空对称：顺势方向 MA 过滤、低波动 ATR 过滤、高震荡 CHOP 过滤同时通过才进场；"
        "基于 ATR 的固定止损止盈、可选跟踪止损、均线方向反转立即平仓、单日亏损达上限停止开仓。"
    ),
    category="趋势",
    strategy_path=(
        "app.strategies.macd_cross_triple_filter_trend_short:"
        "MacdCrossTripleFilterTrendShortStrategy"
    ),
    config_path=(
        "app.strategies.macd_cross_triple_filter_trend_short:"
        "MacdCrossTripleFilterTrendShortConfig"
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
            "ATR 周期",
            "integer",
            14,
            2,
            200,
        ),
        "atr_lookback_period": ParameterSpec(
            "ATR 回看中位数窗口",
            "integer",
            10,
            2,
            200,
        ),
        "atr_filter_threshold": ParameterSpec(
            "ATR 过滤阈值系数",
            "number",
            0.8,
            0.1,
            5.0,
        ),
        "chop_period": ParameterSpec(
            "Choppiness 周期",
            "integer",
            14,
            2,
            200,
        ),
        "chop_threshold": ParameterSpec(
            "Choppiness 阈值（0~1）",
            "number",
            0.4,
            0.05,
            1.0,
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
        "risk_per_trade_pct": ParameterSpec(
            "单笔风险占账户比例",
            "number",
            0.01,
            0.0001,
            0.5,
        ),
        "max_position_pct": ParameterSpec(
            "单仓最大资金占比",
            "number",
            0.1,
            0.01,
            1.0,
        ),
        "daily_loss_limit_pct": ParameterSpec(
            "单日最大亏损占比",
            "number",
            0.02,
            0.001,
            0.5,
        ),
        "enable_trailing_stop": ParameterSpec(
            "是否启用跟踪止损",
            "boolean",
            True,
        ),
        "trade_size": ParameterSpec(
            "兜底下单数量",
            "number",
            0.001,
            0.000001,
            1000,
        ),
    },
    timeframes=("4h", "1d"),
    primary_timeframe="4h",
    plot_config={
        "main_plot": {
            "ma_short": {
                "name": "MA Short",
                "type": "line",
                "color": "#43a5ff",
                "lineWidth": 1,
            },
            "ma_long": {
                "name": "MA Long",
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
                "macd_histogram": {
                    "name": "Histogram",
                    "type": "histogram",
                },
            },
            "ATR": {
                "atr": {
                    "name": "ATR",
                    "type": "line",
                    "color": "#9b59b6",
                    "lineWidth": 1,
                },
            },
            "Choppiness": {
                "chop": {
                    "name": "Choppiness",
                    "type": "line",
                    "color": "#1abc9c",
                    "lineWidth": 1,
                },
            },
        },
    },
    mode=StrategyMode.PORTFOLIO,
    supports_short=True,
    requires_funding=False,
)
