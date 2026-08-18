from decimal import Decimal
import numpy as np
import pandas as pd

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, PositionSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode


class MultiFilterBreakoutConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    ma_fast_period: int = 20
    ma_slow_period: int = 50
    adx_period: int = 14
    atr_period: int = 14
    vol_ma_period: int = 20
    bb_period: int = 20
    bb_multiplier: float = 2.0
    kc_period: int = 20
    kc_multiplier: float = 2.0
    donchian_period: int = 20
    squeeze_min_bars: int = 5
    squeeze_timeout: int = 6
    adx_threshold: float = 20.0
    atr_move_threshold: float = 0.8
    volume_ratio_threshold: float = 1.5
    up_k_count_min: int = 5
    first_pct_equity: float = 0.05
    add_pct_equity: float = 0.03
    max_add_count: int = 10
    max_total_pct: float = 0.35
    fixed_sl_pct: float = 0.15
    trail_atr_multi: float = 2.0
    hard_sl_atr_multi: float = 1.5
    trail_pct_enabled: bool = True
    trail_pct_trigger: float = 0.20
    trail_pct_drawdown: float = 0.30
    bbkc_short_scale: float = 0.5
    vol_72h_max_amp: float = 1.0
    atr_price_max_pct: float = 0.10
    default_leverage: float = 3.0


class MultiFilterBreakoutStrategy(Strategy):
    def __init__(self, config: MultiFilterBreakoutConfig) -> None:
        super().__init__(config)
        self.instrument_id = config.instrument_id
        self.bar_type = config.bar_type
        self.bars: list[Bar] = []
        self.instrument = None

        # Squeeze state machine
        self.squeeze_count: int = 0
        self.squeeze_ready: bool = False
        self.squeeze_age: int = 0
        self.frozen_bb_up: float | None = None
        self.frozen_bb_down: float | None = None
        self.frozen_kc_up: float | None = None
        self.frozen_kc_down: float | None = None

        # Execution / Position tracking
        self.pending_order: dict | None = None
        self.entry_price: float | None = None
        self.avg_entry_price: float | None = None
        self.entry_atr: float | None = None
        self.total_margin_used: float = 0.0
        self.current_size_scale: float = 1.0
        self.addon_count: int = 0
        self.triggered_tiers: set[int] = set()
        self.stop_loss_price: float | None = None
        self.atr_trail_line: float | None = None
        self.peak_floating_profit: float = 0.0
        self.trail_profit_activated: bool = False
        self.extreme_price: float | None = None

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.instrument_id)
        self.subscribe_bars(self.bar_type)

    def _reset_trade_state(self) -> None:
        self.pending_order = None
        self.entry_price = None
        self.avg_entry_price = None
        self.entry_atr = None
        self.total_margin_used = 0.0
        self.current_size_scale = 1.0
        self.addon_count = 0
        self.triggered_tiers.clear()
        self.stop_loss_price = None
        self.atr_trail_line = None
        self.peak_floating_profit = 0.0
        self.trail_profit_activated = False
        self.extreme_price = None

    def _get_equity(self) -> float:
        equity_dict = self.portfolio.equity(self.instrument_id.venue)
        if equity_dict:
            val = sum(m.as_double() for m in equity_dict.values())
            return max(100.0, val)
        return 10000.0

    def _make_qty(self, notional: float, price: float) -> Quantity:
        raw_qty = notional / price if price > 0 else 0.001
        if self.instrument:
            return self.instrument.make_qty(Decimal(str(round(raw_qty, 8))))
        return Quantity.from_str(str(round(raw_qty, 4)))

    def _execute_pending_order(self, open_price: float) -> None:
        if not self.pending_order:
            return

        action = self.pending_order.get("action")
        size_scale = self.pending_order.get("size_scale", 1.0)
        equity = self._get_equity()

        # Check volatility dampening filters
        vol_scale = 1.0
        if len(self.bars) >= 72:
            high_72 = max(b.high.as_double() for b in self.bars[-72:])
            low_72 = min(b.low.as_double() for b in self.bars[-72:])
            if low_72 > 0 and ((high_72 - low_72) / low_72) > self.config.vol_72h_max_amp:
                vol_scale *= 0.5

        atr_val = self.pending_order.get("atr", 0.0)
        if open_price > 0 and (atr_val / open_price) > self.config.atr_price_max_pct:
            vol_scale *= 0.5

        if action in ("ENTRY_LONG", "ENTRY_SHORT"):
            margin = equity * self.config.first_pct_equity * size_scale * vol_scale
            notional = margin * self.config.default_leverage
            qty = self._make_qty(notional, open_price)
            side = OrderSide.BUY if action == "ENTRY_LONG" else OrderSide.SELL

            order = self.order_factory.market(
                instrument_id=self.instrument_id,
                order_side=side,
                quantity=qty,
            )
            self.submit_order(order)

            self.entry_price = open_price
            self.avg_entry_price = open_price
            self.entry_atr = max(atr_val, open_price * 0.005)
            self.total_margin_used = margin
            self.current_size_scale = size_scale
            self.addon_count = 0
            self.triggered_tiers.clear()
            self.extreme_price = open_price
            self.peak_floating_profit = 0.0
            self.trail_profit_activated = False

            if action == "ENTRY_LONG":
                self.stop_loss_price = open_price - 2.0 * self.entry_atr
                self.atr_trail_line = open_price - self.config.trail_atr_multi * self.entry_atr
            else:
                self.stop_loss_price = open_price + 2.0 * self.entry_atr
                self.atr_trail_line = open_price + self.config.trail_atr_multi * self.entry_atr

        elif action in ("ADDON_LONG", "ADDON_SHORT"):
            margin = equity * self.config.add_pct_equity * self.current_size_scale * vol_scale
            notional = margin * self.config.default_leverage
            qty = self._make_qty(notional, open_price)
            side = OrderSide.BUY if action == "ADDON_LONG" else OrderSide.SELL

            order = self.order_factory.market(
                instrument_id=self.instrument_id,
                order_side=side,
                quantity=qty,
            )
            self.submit_order(order)

            if self.avg_entry_price is not None and self.total_margin_used > 0:
                new_margin = self.total_margin_used + margin
                self.avg_entry_price = (
                    self.avg_entry_price * self.total_margin_used + open_price * margin
                ) / new_margin
                self.total_margin_used = new_margin
            else:
                self.avg_entry_price = open_price
                self.total_margin_used = margin

            self.addon_count += 1

            # Move structural stop loss to addon price (tighten only)
            if action == "ADDON_LONG":
                if self.stop_loss_price is None or open_price > self.stop_loss_price:
                    self.stop_loss_price = open_price
            else:
                if self.stop_loss_price is None or open_price < self.stop_loss_price:
                    self.stop_loss_price = open_price

        self.pending_order = None

    def on_bar(self, bar: Bar) -> None:
        # 1. Execute pending orders on this bar's open price
        if self.pending_order is not None:
            self._execute_pending_order(bar.open.as_double())

        self.bars.append(bar)
        min_required = max(70, self.config.ma_slow_period + 20)
        if len(self.bars) < min_required:
            return

        # 2. Extract series and calculate technical indicators
        closes = pd.Series([b.close.as_double() for b in self.bars])
        highs = pd.Series([b.high.as_double() for b in self.bars])
        lows = pd.Series([b.low.as_double() for b in self.bars])
        opens = pd.Series([b.open.as_double() for b in self.bars])
        volumes = pd.Series([b.volume.as_double() for b in self.bars])

        close_val = closes.iloc[-1]
        prev_close_val = closes.iloc[-2]
        open_val = opens.iloc[-1]
        vol_val = volumes.iloc[-1]

        # Moving Averages
        ma_fast_s = closes.rolling(window=self.config.ma_fast_period).mean()
        ma_slow_s = closes.rolling(window=self.config.ma_slow_period).mean()
        ma_fast = ma_fast_s.iloc[-1]
        ma_slow = ma_slow_s.iloc[-1]

        # ATR (14)
        prev_c = closes.shift(1)
        tr = pd.concat([highs - lows, (highs - prev_c).abs(), (lows - prev_c).abs()], axis=1).max(axis=1)
        atr_s = tr.ewm(alpha=1.0 / self.config.atr_period, adjust=False).mean()
        atr_val = atr_s.iloc[-1]

        # Wilder ADX (14)
        up_move = highs - highs.shift(1)
        down_move = lows.shift(1) - lows
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        smooth_tr = tr.ewm(alpha=1.0 / self.config.adx_period, adjust=False).mean()
        smooth_pdm = pd.Series(plus_dm).ewm(alpha=1.0 / self.config.adx_period, adjust=False).mean()
        smooth_mdm = pd.Series(minus_dm).ewm(alpha=1.0 / self.config.adx_period, adjust=False).mean()

        denom_tr = smooth_tr.replace(0, np.nan)
        plus_di_s = (100.0 * smooth_pdm / denom_tr).fillna(0.0)
        minus_di_s = (100.0 * smooth_mdm / denom_tr).fillna(0.0)
        sum_di = (plus_di_s + minus_di_s).replace(0, np.nan)
        dx_s = (100.0 * (plus_di_s - minus_di_s).abs() / sum_di).fillna(0.0)
        adx_s = dx_s.ewm(alpha=1.0 / self.config.adx_period, adjust=False).mean()

        adx_val = adx_s.iloc[-1]
        plus_di_val = plus_di_s.iloc[-1]
        minus_di_val = minus_di_s.iloc[-1]

        # Volume SMA
        vol_ma = volumes.rolling(window=self.config.vol_ma_period).mean().iloc[-1]

        # Bollinger Bands (20, 2.0)
        bb_mid = closes.rolling(window=self.config.bb_period).mean().iloc[-1]
        bb_std = closes.rolling(window=self.config.bb_period).std(ddof=0).iloc[-1]
        bb_up = bb_mid + self.config.bb_multiplier * bb_std
        bb_down = bb_mid - self.config.bb_multiplier * bb_std

        # Keltner Channel (EMA20 ± 2.0 * ATR)
        kc_mid = closes.ewm(span=self.config.kc_period, adjust=False).mean().iloc[-1]
        kc_up = kc_mid + self.config.kc_multiplier * atr_val
        kc_down = kc_mid - self.config.kc_multiplier * atr_val

        # Donchian Channel (past 20 bars excluding current)
        donchian_high = highs.iloc[-self.config.donchian_period - 1 : -1].max()
        donchian_low = lows.iloc[-self.config.donchian_period - 1 : -1].min()

        # Up / Down bars in cache (close[i] > open[i-1])
        up_k_count = sum(1 for i in range(-20, 0) if closes.iloc[i] > opens.iloc[i - 1])
        down_k_count = sum(1 for i in range(-20, 0) if closes.iloc[i] < opens.iloc[i - 1])

        # 3. Position & Risk Management
        is_long = self.portfolio.is_net_long(self.instrument_id)
        is_short = self.portfolio.is_net_short(self.instrument_id)
        is_flat = self.portfolio.is_flat(self.instrument_id)

        # Handle Exits for Open Positions
        if not is_flat and self.avg_entry_price is not None:
            should_exit = False
            exit_reason = ""
            entry_atr = self.entry_atr or atr_val

            if is_long:
                pnl_pct = (close_val - self.avg_entry_price) / self.avg_entry_price
                floating_profit = pnl_pct * self.total_margin_used * self.config.default_leverage
                self.peak_floating_profit = max(self.peak_floating_profit, floating_profit)

                # Update ATR trailing stop line
                if self.extreme_price is None or bar.high.as_double() > self.extreme_price:
                    self.extreme_price = bar.high.as_double()
                    new_trail = self.extreme_price - self.config.trail_atr_multi * entry_atr
                    self.atr_trail_line = max(self.atr_trail_line or new_trail, new_trail)

                # Exit checks
                if pnl_pct <= -self.config.fixed_sl_pct:
                    should_exit = True
                    exit_reason = "Fixed Stop Loss (-15%)"
                elif self.atr_trail_line is not None and close_val <= self.atr_trail_line:
                    should_exit = True
                    exit_reason = "ATR Trailing Stop"
                elif self.stop_loss_price is not None and close_val <= self.stop_loss_price:
                    should_exit = True
                    exit_reason = "Structural Stop Loss"
                elif (self.avg_entry_price - close_val) >= self.config.hard_sl_atr_multi * entry_atr:
                    should_exit = True
                    exit_reason = "Hard Reverse Volatility Stop"
                elif close_val < ma_fast:
                    should_exit = True
                    exit_reason = "MA20 Breakdown"
                elif self.config.trail_pct_enabled:
                    if self.peak_floating_profit > (self.total_margin_used * self.config.trail_pct_trigger):
                        self.trail_profit_activated = True
                    if self.trail_profit_activated and self.peak_floating_profit > 0:
                        dd = (self.peak_floating_profit - floating_profit) / self.peak_floating_profit
                        if dd >= self.config.trail_pct_drawdown:
                            should_exit = True
                            exit_reason = "Profit Peak Retracement Stop (30%)"

            elif is_short:
                pnl_pct = (self.avg_entry_price - close_val) / self.avg_entry_price
                floating_profit = pnl_pct * self.total_margin_used * self.config.default_leverage
                self.peak_floating_profit = max(self.peak_floating_profit, floating_profit)

                # Update ATR trailing stop line
                if self.extreme_price is None or bar.low.as_double() < self.extreme_price:
                    self.extreme_price = bar.low.as_double()
                    new_trail = self.extreme_price + self.config.trail_atr_multi * entry_atr
                    self.atr_trail_line = min(self.atr_trail_line or new_trail, new_trail)

                # Exit checks
                if pnl_pct <= -self.config.fixed_sl_pct:
                    should_exit = True
                    exit_reason = "Fixed Stop Loss (-15%)"
                elif self.atr_trail_line is not None and close_val >= self.atr_trail_line:
                    should_exit = True
                    exit_reason = "ATR Trailing Stop"
                elif self.stop_loss_price is not None and close_val >= self.stop_loss_price:
                    should_exit = True
                    exit_reason = "Structural Stop Loss"
                elif (close_val - self.avg_entry_price) >= self.config.hard_sl_atr_multi * entry_atr:
                    should_exit = True
                    exit_reason = "Hard Reverse Volatility Stop"
                elif close_val > ma_fast:
                    should_exit = True
                    exit_reason = "MA20 Breakup"
                elif self.config.trail_pct_enabled:
                    if self.peak_floating_profit > (self.total_margin_used * self.config.trail_pct_trigger):
                        self.trail_profit_activated = True
                    if self.trail_profit_activated and self.peak_floating_profit > 0:
                        dd = (self.peak_floating_profit - floating_profit) / self.peak_floating_profit
                        if dd >= self.config.trail_pct_drawdown:
                            should_exit = True
                            exit_reason = "Profit Peak Retracement Stop (30%)"

            if should_exit:
                self.close_all_positions(self.instrument_id)
                self._reset_trade_state()
                return

            # Check Addon Conditions (if not exiting and no pending order)
            if self.pending_order is None and self.addon_count < self.config.max_add_count:
                equity = self._get_equity()
                if self.total_margin_used < equity * self.config.max_total_pct:
                    can_add = False
                    if is_long and close_val >= ma_fast and self.entry_price is not None and close_val >= self.entry_price:
                        # a. ATR interval addon
                        if (close_val - self.entry_price) >= (entry_atr * (self.addon_count + 1)):
                            can_add = True
                        # b. Tiered floating profit addon (5%, 10%, 15%, 20%, 25%)
                        for tier in (5, 10, 15, 20, 25):
                            if pnl_pct >= (tier / 100.0) and tier not in self.triggered_tiers:
                                self.triggered_tiers.add(tier)
                                can_add = True
                        if can_add:
                            self.pending_order = {"action": "ADDON_LONG", "atr": entry_atr}

                    elif is_short and close_val <= ma_fast and self.entry_price is not None and close_val <= self.entry_price:
                        # a. ATR interval addon
                        if (self.entry_price - close_val) >= (entry_atr * (self.addon_count + 1)):
                            can_add = True
                        # b. Tiered floating profit addon
                        for tier in (5, 10, 15, 20, 25):
                            if pnl_pct >= (tier / 100.0) and tier not in self.triggered_tiers:
                                self.triggered_tiers.add(tier)
                                can_add = True
                        if can_add:
                            self.pending_order = {"action": "ADDON_SHORT", "atr": entry_atr}

        # 4. Squeeze State Machine Update (runs every bar)
        bb_width = bb_up - bb_down
        kc_width = kc_up - kc_down
        in_kc = (close_val >= kc_down) and (close_val <= kc_up)
        if in_kc and (bb_width < kc_width):
            self.squeeze_count += 1
            if self.squeeze_count >= self.config.squeeze_min_bars:
                self.squeeze_ready = True
                self.squeeze_age = 0
                self.frozen_bb_up = bb_up
                self.frozen_bb_down = bb_down
                self.frozen_kc_up = kc_up
                self.frozen_kc_down = kc_down
        else:
            self.squeeze_count = 0

        if self.squeeze_ready:
            self.squeeze_age += 1
            if self.squeeze_age > self.config.squeeze_timeout:
                self.squeeze_ready = False
                self.squeeze_count = 0

        # 5. Check Entry Signals (when Flat and no pending order)
        if is_flat and self.pending_order is None:
            # A. Donchian Breakout Signal
            donchian_long = (
                (close_val > ma_fast > ma_slow)
                and (adx_val > self.config.adx_threshold)
                and (plus_di_val > minus_di_val)
                and (close_val > donchian_high)
                and ((close_val - prev_close_val) > self.config.atr_move_threshold * atr_val)
                and (up_k_count >= self.config.up_k_count_min)
                and (vol_val > self.config.volume_ratio_threshold * vol_ma)
            )
            donchian_short = (
                (close_val < ma_fast < ma_slow)
                and (adx_val > self.config.adx_threshold)
                and (minus_di_val > plus_di_val)
                and (close_val < donchian_low)
                and ((prev_close_val - close_val) > self.config.atr_move_threshold * atr_val)
                and (down_k_count >= self.config.up_k_count_min)
                and (vol_val > self.config.volume_ratio_threshold * vol_ma)
            )

            # B. Squeeze (BBKC) Momentum Breakout Signal
            bbkc_long = False
            bbkc_short = False
            if self.squeeze_ready and self.frozen_bb_up is not None and self.frozen_kc_up is not None:
                bbkc_long = (
                    (close_val > self.frozen_bb_up)
                    and (close_val > self.frozen_kc_up)
                    and (abs(close_val - open_val) > 0.2 * atr_val)
                    and (adx_val > self.config.adx_threshold)
                    and (vol_val >= self.config.volume_ratio_threshold * vol_ma)
                )
                bbkc_short = (
                    (close_val < self.frozen_bb_down)
                    and (close_val < self.frozen_kc_down)
                    and (abs(close_val - open_val) > 0.2 * atr_val)
                    and (adx_val > self.config.adx_threshold)
                    and (vol_val >= self.config.volume_ratio_threshold * vol_ma)
                    and (ma_fast < ma_slow)
                )

            if donchian_long:
                self.pending_order = {
                    "action": "ENTRY_LONG",
                    "size_scale": 1.0,
                    "atr": atr_val,
                    "source": "DONCHIAN",
                }
            elif donchian_short:
                self.pending_order = {
                    "action": "ENTRY_SHORT",
                    "size_scale": 1.0,
                    "atr": atr_val,
                    "source": "DONCHIAN",
                }
            elif bbkc_long:
                self.pending_order = {
                    "action": "ENTRY_LONG",
                    "size_scale": 1.0,
                    "atr": atr_val,
                    "source": "BBKC",
                }
                self.squeeze_ready = False
                self.squeeze_count = 0
            elif bbkc_short:
                self.pending_order = {
                    "action": "ENTRY_SHORT",
                    "size_scale": self.config.bbkc_short_scale,
                    "atr": atr_val,
                    "source": "BBKC",
                }
                self.squeeze_ready = False
                self.squeeze_count = 0

    def on_stop(self) -> None:
        self.close_all_positions(self.instrument_id)
        self._reset_trade_state()
        self.unsubscribe_bars(self.bar_type)


def calculate_indicators(df: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    result = df.copy()
    fast_p = int(parameters.get("ma_fast_period", 20))
    slow_p = int(parameters.get("ma_slow_period", 50))
    adx_p = int(parameters.get("adx_period", 14))
    atr_p = int(parameters.get("atr_period", 14))
    vol_p = int(parameters.get("vol_ma_period", 20))
    bb_p = int(parameters.get("bb_period", 20))
    bb_mult = float(parameters.get("bb_multiplier", 2.0))
    kc_p = int(parameters.get("kc_period", 20))
    kc_mult = float(parameters.get("kc_multiplier", 2.0))

    close = pd.to_numeric(result["close"], errors="coerce")
    high = pd.to_numeric(result["high"], errors="coerce")
    low = pd.to_numeric(result["low"], errors="coerce")
    vol = pd.to_numeric(result["volume"], errors="coerce")

    # Fast & Slow MAs
    result["ma_fast"] = close.rolling(window=fast_p).mean().bfill().fillna(0.0)
    result["ma_slow"] = close.rolling(window=slow_p).mean().bfill().fillna(0.0)

    # ATR
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / atr_p, adjust=False).mean()
    result["atr"] = atr.bfill().fillna(0.0)

    # Bollinger Bands
    bb_mid = close.rolling(window=bb_p).mean()
    bb_std = close.rolling(window=bb_p).std(ddof=0)
    result["bb_upper"] = (bb_mid + bb_mult * bb_std).bfill().fillna(0.0)
    result["bb_lower"] = (bb_mid - bb_mult * bb_std).bfill().fillna(0.0)

    # Keltner Channel
    kc_mid = close.ewm(span=kc_p, adjust=False).mean()
    result["kc_upper"] = (kc_mid + kc_mult * atr).bfill().fillna(0.0)
    result["kc_lower"] = (kc_mid - kc_mult * atr).bfill().fillna(0.0)

    # ADX & Directional Indicators
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    smooth_tr = tr.ewm(alpha=1.0 / adx_p, adjust=False).mean()
    smooth_pdm = pd.Series(plus_dm, index=close.index).ewm(alpha=1.0 / adx_p, adjust=False).mean()
    smooth_mdm = pd.Series(minus_dm, index=close.index).ewm(alpha=1.0 / adx_p, adjust=False).mean()

    denom = smooth_tr.replace(0, np.nan)
    plus_di = (100.0 * smooth_pdm / denom).fillna(0.0)
    minus_di = (100.0 * smooth_mdm / denom).fillna(0.0)
    sum_di = (plus_di + minus_di).replace(0, np.nan)
    dx = (100.0 * (plus_di - minus_di).abs() / sum_di).fillna(0.0)
    adx = dx.ewm(alpha=1.0 / adx_p, adjust=False).mean()

    result["plus_di"] = plus_di.bfill().fillna(0.0)
    result["minus_di"] = minus_di.bfill().fillna(0.0)
    result["adx"] = adx.bfill().fillna(0.0)

    # Volume MA
    result["vol_ma"] = vol.rolling(window=vol_p).mean().bfill().fillna(0.0)

    return result


STRATEGY_MANIFEST = StrategyManifest(
    slug="multi_filter_breakout",
    name="Multi-Filter Breakout Strategy (复合突破趋势策略)",
    description="唐奇安通道突破与布林肯特纳挤压双信号复合趋势策略，支持金字塔加仓与多重动态离场",
    version="1.0.0",
    category="trend",
    strategy_path="app.strategies.multi_filter_breakout:MultiFilterBreakoutStrategy",
    config_path="app.strategies.multi_filter_breakout:MultiFilterBreakoutConfig",
    parameters={
        "ma_fast_period": ParameterSpec(title="MA快线周期", type="integer", default=20, minimum=5, maximum=100),
        "ma_slow_period": ParameterSpec(title="MA慢线周期", type="integer", default=50, minimum=20, maximum=200),
        "adx_period": ParameterSpec(title="ADX周期", type="integer", default=14, minimum=5, maximum=30),
        "atr_period": ParameterSpec(title="ATR周期", type="integer", default=14, minimum=5, maximum=30),
        "vol_ma_period": ParameterSpec(title="成交量均线周期", type="integer", default=20, minimum=5, maximum=60),
        "bb_period": ParameterSpec(title="布林带周期", type="integer", default=20, minimum=10, maximum=50),
        "bb_multiplier": ParameterSpec(title="布林带标准差倍数", type="number", default=2.0, minimum=1.0, maximum=3.0),
        "kc_period": ParameterSpec(title="肯特纳EMA周期", type="integer", default=20, minimum=10, maximum=50),
        "kc_multiplier": ParameterSpec(title="肯特纳ATR倍数", type="number", default=2.0, minimum=1.0, maximum=3.0),
        "donchian_period": ParameterSpec(title="唐奇安周期", type="integer", default=20, minimum=10, maximum=50),
        "squeeze_min_bars": ParameterSpec(title="挤压蓄能最小K线数", type="integer", default=5, minimum=2, maximum=15),
        "squeeze_timeout": ParameterSpec(title="蓄能突破超时K线数", type="integer", default=6, minimum=3, maximum=20),
        "adx_threshold": ParameterSpec(title="ADX趋势强度阈值", type="number", default=20.0, minimum=10.0, maximum=40.0),
        "atr_move_threshold": ParameterSpec(title="ATR突破波动倍数", type="number", default=0.8, minimum=0.2, maximum=2.0),
        "volume_ratio_threshold": ParameterSpec(title="成交量均线倍数", type="number", default=1.5, minimum=1.0, maximum=3.0),
        "up_k_count_min": ParameterSpec(title="最小同向K线数", type="integer", default=5, minimum=2, maximum=20),
        "first_pct_equity": ParameterSpec(title="首仓保证金比例", type="number", default=0.05, minimum=0.01, maximum=0.15),
        "add_pct_equity": ParameterSpec(title="加仓保证金比例", type="number", default=0.03, minimum=0.01, maximum=0.10),
        "max_add_count": ParameterSpec(title="最大加仓次数", type="integer", default=10, minimum=1, maximum=20),
        "max_total_pct": ParameterSpec(title="最大总保证金比例", type="number", default=0.35, minimum=0.1, maximum=0.5),
        "fixed_sl_pct": ParameterSpec(title="固定止损比例", type="number", default=0.15, minimum=0.05, maximum=0.30),
        "trail_atr_multi": ParameterSpec(title="ATR追踪止损倍数", type="number", default=2.0, minimum=0.5, maximum=4.0),
        "hard_sl_atr_multi": ParameterSpec(title="硬性反向ATR倍数", type="number", default=1.5, minimum=0.5, maximum=3.0),
        "trail_pct_enabled": ParameterSpec(title="开启比例追踪止盈", type="boolean", default=True),
        "trail_pct_trigger": ParameterSpec(title="比例追踪触发门槛", type="number", default=0.20, minimum=0.1, maximum=0.5),
        "trail_pct_drawdown": ParameterSpec(title="最高浮盈回撤平仓比例", type="number", default=0.30, minimum=0.1, maximum=0.5),
        "bbkc_short_scale": ParameterSpec(title="挤压空头仓位系数", type="number", default=0.5, minimum=0.1, maximum=1.0),
        "vol_72h_max_amp": ParameterSpec(title="72H最大振幅减仓阈值", type="number", default=1.0, minimum=0.5, maximum=2.0),
        "atr_price_max_pct": ParameterSpec(title="ATR价格比减仓阈值", type="number", default=0.10, minimum=0.03, maximum=0.20),
        "default_leverage": ParameterSpec(title="默认杠杆倍数", type="number", default=3.0, minimum=1.0, maximum=10.0),
    },
    timeframes=("15m", "1h", "4h", "1d"),
    primary_timeframe="1h",
    plot_config={
        "main_plot": {
            "close": {"type": "line", "color": "#ffffff"},
            "ma_fast": {"type": "line", "color": "#ffaa00"},
            "ma_slow": {"type": "line", "color": "#00aaff"},
            "bb_upper": {"type": "line", "color": "#ff8800"},
            "bb_lower": {"type": "line", "color": "#ff8800"},
            "kc_upper": {"type": "line", "color": "#00ffcc"},
            "kc_lower": {"type": "line", "color": "#00ffcc"},
        },
        "subplots": {
            "ADX": {
                "adx": {"type": "line", "color": "#ff00aa"},
                "plus_di": {"type": "line", "color": "#00ff88"},
                "minus_di": {"type": "line", "color": "#ff4444"},
            },
            "ATR": {
                "atr": {"type": "line", "color": "#ff55ff"},
            },
            "Volume": {
                "vol_ma": {"type": "line", "color": "#ffcc00"},
            },
        },
    },
    mode=StrategyMode.SINGLE_INSTRUMENT,
    supports_short=True,
    requires_funding=True,
)