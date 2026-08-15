"""中盘波动率压缩突破双轨加仓趋势策略。

策略核心思路:
1. 聚焦流动性排名 20~300 位的中盘币,利用布林带宽度<肯特纳通道宽度识别
   波动率压缩状态,在压缩 3~20 根 K 线后,等待收盘价突破布林外沿
   (偏移 0.15 倍 ATR)产生多/空突破信号。
2. 仓位管理采用 1% 权益首仓试错,通过 ATR 价格阶梯与时序动能重启
   两个独立加仓通道对趋势健康仓位进行加码,顺势放大趋势收益。
3. 风险端通过三层止损(硬止损/保本/移动)+ 阶梯利润保护 + 时间止损
   形成多档风控;在 BTC 大幅波动后触发市场级静默,降低全市场
   同步假突破风险。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, PositionSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.indicators import (
    AverageTrueRange,
    BollingerBands,
    ExponentialMovingAverage,
    MovingAverageConvergenceDivergence,
    RelativeStrengthIndex,
)

from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode


# ---------------------------------------------------------------------------
# 配置与数据类
# ---------------------------------------------------------------------------


class VolatilityCompressionMidcapBreakoutConfig(StrategyConfig, frozen=True):
    """策略配置。"""

    instrument_ids: list[InstrumentId]
    bar_types: list[BarType]
    trade_size: Decimal

    # 布林带 / 肯特纳通道参数
    bb_period: int = 20
    bb_std: float = 2.0
    kc_period: int = 20
    kc_atr_mult: float = 1.5

    # ATR 系列
    atr_period: int = 14
    atr_short_period: int = 3
    atr_long_period: int = 20

    # 压缩与突破
    compression_min_bars: int = 3
    compression_max_bars: int = 20
    breakout_atr_offset: float = 0.15

    # 仓位规模
    initial_position_pct: float = 0.01
    add_position_pct: float = 0.02
    add_position_pct_upgraded: float = 0.03
    max_position_pct_per_symbol: float = 0.22
    max_total_exposure: float = 1.70
    max_leverage: float = 2.0
    max_impact_pct: float = 0.015

    # 三套分层止损参数
    hard_stop_unadded_pct: float = 0.25
    hard_stop_added_atr: float = 2.0
    breakeven_trigger_atr: float = 1.2
    breakeven_offset_atr: float = 0.2
    trailing_trigger_atr: float = 3.0
    trailing_atr_low: float = 2.0
    trailing_atr_mid: float = 2.5
    trailing_atr_high: float = 3.0

    # 阶梯利润保护
    profit_ladder_initial: float = 5.0
    profit_ladder_step: float = 1.0
    profit_ladder_rearm: float = 1.5  # 上一档之后再跨过 1.5 倍 ATR 才重新加档

    # 加仓通道
    atr_stair_max: int = 8  # 1..8 倍 ATR 阶梯
    timing_min_atr: float = 1.5
    timing_high_atr: float = 3.0
    timing_signal_quota: int = 4

    # 冷却
    cooldown_bars: int = 12
    btc_silence_bars: int = 12
    btc_volatility_trigger: float = 3.0

    # 时间止损(小时)
    time_stop_hours: int = 360
    zombie_stop_hours: int = 372
    breakeven_wait_hours: int = 8

    # 趋势健康过滤与加仓健康门
    health_body_min: float = 0.35
    health_vol_mult: float = 1.8
    health_slope_min: float = 0.0025
    health_rsi_ob: float = 80.0
    health_rsi_os: float = 20.0
    health_atr_pct_cap: float = 0.10

    # 市场热度过滤
    market_heat_threshold: float = 0.08

    # 突破后 GTC 止盈
    gtc_take_profit_pct: float = 0.005

    # 波动率档位分界(用 ATR/价格 衡量)
    vol_regime_low: float = 0.02
    vol_regime_high: float = 0.05


@dataclass
class PositionState:
    """单个标的方向的仓位状态。"""

    side: str  # "LONG" / "SHORT"
    first_entry_price: float
    avg_entry_price: float
    atr_at_entry: float
    bars_held: int = 0
    has_added: bool = False
    add_ladder_step: int = 0  # 已经触发的 ATR 阶梯档位(0~8)
    add_ladder_high_water: float = 0.0  # 当前 ATR 倍数(浮盈)
    timing_signals_used: int = 0
    timing_signals_mask: int = 0  # 4 位掩码,标识已触发的信号
    profit_protect_price: float = 0.0
    profit_protect_level: int = 0  # 已经触发的保护档位
    hard_stop_price: float = 0.0
    breakeven_active: bool = False
    trailing_active: bool = False
    trailing_stop_price: float = 0.0
    peak_floating_atr: float = 0.0  # 历史最大浮盈(以 ATR 倍数计)
    entry_bars: int = 0  # 距离首仓开仓的 bar 数(冷却判断)
    entry_time_bars: int = 0  # 同标的同方向冷却
    health_upgraded: bool = False
    gtc_tp_submitted: bool = False
    gtc_tp_price: float = 0.0
    gtc_tp_order_id: Optional[str] = None
    size_held: float = 0.0  # 累计名义仓位(以权益比例近似)
    pending_close: bool = False


@dataclass
class SymbolRuntime:
    """单个标的的运行时状态。"""

    bars: Deque[Bar] = field(default_factory=deque)
    closes: Deque[float] = field(default_factory=deque)
    highs: Deque[float] = field(default_factory=deque)
    lows: Deque[float] = field(default_factory=deque)
    opens: Deque[float] = field(default_factory=deque)
    volumes: Deque[float] = field(default_factory=deque)
    quote_volumes: Deque[float] = field(default_factory=deque)
    compression_count: int = 0
    is_compressed: bool = False
    last_compression_state: bool = False
    long_state: Optional[PositionState] = None
    short_state: Optional[PositionState] = None
    cooldown_long_until: int = 0
    cooldown_short_until: int = 0
    last_bar_index: int = 0
    adx_at_entry: float = 0.0
    last_breakout_dir: int = 0  # 1=long, -1=short


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _rolling_mean(values: Deque[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    arr = np.fromiter(values, dtype=float, count=period)
    return float(arr.mean())


def _rolling_std(values: Deque[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    arr = np.fromiter(values, dtype=float, count=period)
    return float(arr.std(ddof=0))


def _true_range(high: float, low: float, prev_close: Optional[float]) -> float:
    if prev_close is None:
        return high - low
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def _compute_atr_series(highs: List[float], lows: List[float], closes: List[float], period: int) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    trs: List[float] = []
    for i in range(1, len(closes)):
        trs.append(_true_range(highs[i], lows[i], closes[i - 1]))
    if len(trs) < period:
        return None
    arr = np.asarray(trs[-period:], dtype=float)
    return float(arr.mean())


# ---------------------------------------------------------------------------
# Strategy 子类
# ---------------------------------------------------------------------------


class VolatilityCompressionMidcapBreakout(Strategy):
    """中盘波动率压缩突破双轨加仓趋势策略。"""

    def __init__(self, config: VolatilityCompressionMidcapBreakoutConfig) -> None:
        super().__init__(config)

        self._instrument_ids: List[InstrumentId] = list(config.instrument_ids)
        self._primary_instrument: InstrumentId = self._instrument_ids[0]
        self._bar_types: List[BarType] = list(config.bar_types)
        self._primary_bar_type: BarType = self._bar_types[0]

        # 指标实例(给主标的用,其它标的由运行时自行计算)
        self.bb = BollingerBands(config.bb_period, config.bb_std)
        self.atr = AverageTrueRange(config.atr_period)
        self.ema20 = ExponentialMovingAverage(20)
        self.macd = MovingAverageConvergenceDivergence(12, 26, 9)
        self.rsi = RelativeStrengthIndex(14)

        # 运行时缓存
        self._runtime: Dict[InstrumentId, SymbolRuntime] = {}
        self._bar_index: int = 0
        self._btc_bar_history: Deque[Bar] = deque(maxlen=4)
        self._btc_silence_until: int = 0
        self._in_flight_notional: float = 0.0  # 在途敞口(已提交未成交)
        self._in_flight_lock = False

        # 提前注册指标 + 订阅
        for bar_type in self._bar_types:
            self.register_indicator_for_bars(self.bb, bar_type)
            self.register_indicator_for_bars(self.atr, bar_type)
            self.register_indicator_for_bars(self.ema20, bar_type)
            self.register_indicator_for_bars(self.macd, bar_type)
            self.register_indicator_for_bars(self.rsi, bar_type)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        for bar_type in self._bar_types:
            self.subscribe_bars(bar_type)
        self.log.info(
            f"启动中盘波动率压缩突破策略,订阅 {len(self._bar_types)} 个 bar 流"
        )

    def on_stop(self) -> None:
        self.cancel_all_orders()
        self.close_all_positions(self._primary_instrument)
        for iid in self._instrument_ids[1:]:
            self.close_all_positions(iid)
        self.log.info("策略停止,已清仓并撤单")

    def on_reset(self) -> None:
        self._runtime.clear()
        self._bar_index = 0
        self._btc_bar_history.clear()
        self._btc_silence_until = 0
        self._in_flight_notional = 0.0

    # ------------------------------------------------------------------
    # 账户/持仓辅助
    # ------------------------------------------------------------------

    def _current_equity(self) -> float:
        try:
            account = self.portfolio.account(self._primary_instrument.venue)
            return float(account.equity(self._primary_instrument.venue).as_double())
        except Exception:  # noqa: BLE001
            return 0.0

    def _current_total_exposure(self) -> float:
        """当前总名义敞口(以权益倍数表示)。"""
        equity = self._current_equity()
        if equity <= 0:
            return 0.0
        total_notional = self._in_flight_notional
        for iid in self._instrument_ids:
            pos = self.cache.position(iid)
            if pos is not None:
                total_notional += abs(float(pos.signed_qty)) * self._last_price(iid)
        return total_notional / equity

    def _last_price(self, instrument_id: InstrumentId) -> float:
        rt = self._runtime.get(instrument_id)
        if rt and rt.closes:
            return rt.closes[-1]
        try:
            instrument = self.cache.instrument(instrument_id)
            if instrument is not None:
                return float(instrument.last_price.as_double())
        except Exception:  # noqa: BLE001
            return 0.0
        return 0.0

    def _position_side(self, instrument_id: InstrumentId) -> Optional[PositionSide]:
        pos = self.cache.position(instrument_id)
        if pos is None:
            return None
        return pos.side

    def _signed_qty(self, instrument_id: InstrumentId) -> float:
        pos = self.cache.position(instrument_id)
        if pos is None:
            return 0.0
        return float(pos.signed_qty)

    # ------------------------------------------------------------------
    # 指标计算
    # ------------------------------------------------------------------

    def _push_bar_history(self, bar: Bar) -> SymbolRuntime:
        rt = self._runtime.get(bar.bar_type.instrument_id)
        if rt is None:
            rt = SymbolRuntime()
            self._runtime[bar.bar_type.instrument_id] = rt
        rt.bars.append(bar)
        rt.closes.append(float(bar.close.as_double()))
        rt.highs.append(float(bar.high.as_double()))
        rt.lows.append(float(bar.low.as_double()))
        rt.opens.append(float(bar.open.as_double()))
        vol = bar.volume.as_double() if bar.volume is not None else 0.0
        rt.volumes.append(float(vol))
        try:
            qv = float(bar.volume.as_double() * bar.close.as_double())
        except Exception:  # noqa: BLE001
            qv = 0.0
        rt.quote_volumes.append(qv)
        # 限制 deque 长度(避免内存膨胀,回测 1h 足够)
        maxlen = 600
        while len(rt.closes) > maxlen:
            rt.bars.popleft()
            rt.closes.popleft()
            rt.highs.popleft()
            rt.lows.popleft()
            rt.opens.popleft()
            rt.volumes.popleft()
            rt.quote_volumes.popleft()
        rt.last_bar_index = self._bar_index
        return rt

    def _compute_indicators_for_runtime(
        self, rt: SymbolRuntime
    ) -> Optional[Dict[str, float]]:
        """计算当前 K 线已闭合后的关键指标值。"""
        closes = list(rt.closes)
        highs = list(rt.highs)
        lows = list(rt.lows)
        opens = list(rt.opens)
        volumes = list(rt.volumes)
        if len(closes) < max(self.config.bb_period, self.config.kc_period) + 5:
            return None

        cfg = self.config
        n = len(closes)
        period = cfg.bb_period
        sma = float(np.mean(closes[-period:]))
        std = float(np.std(closes[-period:], ddof=0))
        bb_mid = sma
        bb_upper = sma + cfg.bb_std * std
        bb_lower = sma - cfg.bb_std * std
        bb_width = (bb_upper - bb_lower) / bb_mid if bb_mid else 0.0

        atr_val = _compute_atr_series(highs, lows, closes, cfg.atr_period)
        if atr_val is None or atr_val <= 0:
            return None

        # EMA
        ema_alpha = 2.0 / (cfg.kc_period + 1)
        ema_val = closes[0]
        for c in closes[1:]:
            ema_val = c * ema_alpha + ema_val * (1 - ema_alpha)
        kc_mid = ema_val
        kc_upper = kc_mid + cfg.kc_atr_mult * atr_val
        kc_lower = kc_mid - cfg.kc_atr_mult * atr_val
        kc_width = (kc_upper - kc_lower) / kc_mid if kc_mid else 0.0

        # EMA20 斜率
        ema20_alpha = 2.0 / (20 + 1)
        ema20 = closes[0]
        ema20_series: List[float] = []
        for c in closes:
            ema20 = c * ema20_alpha + ema20 * (1 - ema20_alpha)
            ema20_series.append(ema20)
        last3 = ema20_series[-3:]
        slope1 = (last3[-1] - last3[-2]) / last3[-2] if last3[-2] else 0.0
        slope2 = (last3[-2] - last3[-3]) / last3[-3] if len(last3) >= 3 and last3[-3] else 0.0
        ema20_slope = slope1
        ema20_accel = slope1 - slope2

        # MACD
        fast_alpha = 2.0 / (12 + 1)
        slow_alpha = 2.0 / (26 + 1)
        signal_alpha = 2.0 / (9 + 1)
        ema_fast = closes[0]
        ema_slow = closes[0]
        for c in closes[1:]:
            ema_fast = c * fast_alpha + ema_fast * (1 - fast_alpha)
            ema_slow = c * slow_alpha + ema_slow * (1 - slow_alpha)
        dif = ema_fast - ema_slow
        # 估算 DEA(用最近一段 dif)
        # 这里采用更简单的方式:递归计算 dif 序列再 EMA
        dif_series: List[float] = []
        ef = closes[0]
        es = closes[0]
        for c in closes:
            ef = c * fast_alpha + ef * (1 - fast_alpha)
            es = c * slow_alpha + es * (1 - slow_alpha)
            dif_series.append(ef - es)
        dea = dif_series[0]
        for d in dif_series[1:]:
            dea = d * signal_alpha + dea * (1 - signal_alpha)
        macd_hist = (dif - dea) * 2.0
        # DIF 斜率(用最后 2 个)
        dif_slope = dif_series[-1] - dif_series[-2]

        # RSI
        rsi_period = 14
        if len(closes) >= rsi_period + 1:
            diffs = np.diff(closes[-(rsi_period + 1):])
            gain = np.mean(np.clip(diffs, 0, None))
            loss = np.mean(np.clip(-diffs, 0, None))
            if loss == 0:
                rsi = 100.0
            else:
                rs = gain / loss
                rsi = 100.0 - 100.0 / (1.0 + rs)
        else:
            rsi = 50.0

        # ADX (简化版)
        adx_period = 14
        if len(closes) >= adx_period + 2:
            trs: List[float] = []
            plus_dm: List[float] = []
            minus_dm: List[float] = []
            for i in range(1, len(closes)):
                up = highs[i] - highs[i - 1]
                dn = lows[i - 1] - lows[i]
                plus_dm.append(max(up, 0.0) if up > dn and up > 0 else 0.0)
                minus_dm.append(max(dn, 0.0) if dn > up and dn > 0 else 0.0)
                trs.append(_true_range(highs[i], lows[i], closes[i - 1]))
            tr_smooth = float(np.mean(trs[-adx_period:]))
            plus_di = 100.0 * float(np.mean(plus_dm[-adx_period:])) / tr_smooth if tr_smooth else 0.0
            minus_di = 100.0 * float(np.mean(minus_dm[-adx_period:])) / tr_smooth if tr_smooth else 0.0
            di_sum = plus_di + minus_di
            dx = 100.0 * abs(plus_di - minus_di) / di_sum if di_sum else 0.0
            adx = dx  # 单次 DX 近似 ADX(避免再次平滑,这里用于趋势强弱)
        else:
            plus_di = minus_di = adx = 0.0

        # 压缩判定
        is_compressed = bb_width < kc_width and bb_mid > 0

        # 量比 & K线健康度
        vol_ma20 = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else 0.0
        vol_ratio = volumes[-1] / vol_ma20 if vol_ma20 else 0.0
        full_range = highs[-1] - lows[-1]
        body = abs(closes[-1] - opens[-1])
        body_ratio = body / full_range if full_range > 0 else 0.0
        upper_wick = highs[-1] - max(closes[-1], opens[-1])
        lower_wick = min(closes[-1], opens[-1]) - lows[-1]
        upper_wick_ratio = upper_wick / full_range if full_range > 0 else 0.0
        lower_wick_ratio = lower_wick / full_range if full_range > 0 else 0.0

        atr_pct = atr_val / closes[-1] if closes[-1] > 0 else 0.0
        atr_short = _compute_atr_series(highs, lows, closes, cfg.atr_short_period)
        atr_long = _compute_atr_series(highs, lows, closes, cfg.atr_long_period)
        atr_ratio = atr_short / atr_long if atr_short and atr_long else 0.0

        return {
            "bb_mid": bb_mid,
            "bb_upper": bb_upper,
            "bb_lower": bb_lower,
            "bb_width": bb_width,
            "kc_mid": kc_mid,
            "kc_upper": kc_upper,
            "kc_lower": kc_lower,
            "kc_width": kc_width,
            "atr": atr_val,
            "atr_pct": atr_pct,
            "atr_ratio": atr_ratio,
            "ema20": ema20_series[-1],
            "ema20_slope": ema20_slope,
            "ema20_accel": ema20_accel,
            "macd_dif": dif,
            "macd_dea": dea,
            "macd_hist": macd_hist,
            "macd_slope": dif_slope,
            "rsi": rsi,
            "adx": adx,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "is_compressed": is_compressed,
            "vol_ratio": vol_ratio,
            "body_ratio": body_ratio,
            "upper_wick_ratio": upper_wick_ratio,
            "lower_wick_ratio": lower_wick_ratio,
            "close": closes[-1],
            "open": opens[-1],
            "high": highs[-1],
            "low": lows[-1],
            "volume": volumes[-1],
            "quote_volume": rt.quote_volumes[-1] if rt.quote_volumes else 0.0,
        }

    # ------------------------------------------------------------------
    # 信号生成
    # ------------------------------------------------------------------

    def _update_compression(self, rt: SymbolRuntime, ind: Dict[str, float]) -> None:
        """维护压缩状态与已持续根数,仅当满足 3~20 根时标记可以突破。"""
        if ind["is_compressed"]:
            rt.compression_count += 1
        else:
            rt.compression_count = 0
        rt.last_compression_state = rt.is_compressed
        rt.is_compressed = ind["is_compressed"]

    def _has_breakout_signal(
        self,
        rt: SymbolRuntime,
        ind: Dict[str, float],
    ) -> int:
        """返回 1(多头)/-1(空头)/0(无)。仅当上一根仍处于压缩、当根结束压缩,
        且当根收盘突破布林外沿 +/- 0.15 ATR 时触发。"""
        cfg = self.config
        compressed_before = rt.last_compression_state
        compressed_now = rt.is_compressed
        if not (compressed_before and not compressed_now):
            return 0
        if not (cfg.compression_min_bars <= rt.compression_count - 1 + 1 <= cfg.compression_max_bars + 1):
            # 注意:compression_count 还在压缩时已递增;上一根为压缩态时该值>=3
            if not (cfg.compression_min_bars <= rt.compression_count <= cfg.compression_max_bars + 1):
                return 0
        if rt.compression_count < cfg.compression_min_bars:
            return 0
        if rt.compression_count > cfg.compression_max_bars + 1:
            return 0

        close = ind["close"]
        atr = ind["atr"]
        long_break = close > ind["bb_upper"] + cfg.breakout_atr_offset * atr
        short_break = close < ind["bb_lower"] - cfg.breakout_atr_offset * atr
        if long_break and not short_break:
            return 1
        if short_break and not long_break:
            return -1
        return 0

    def _check_market_heat(self) -> bool:
        """市场热度过滤:全币池中高波动率标的占比>=8%。"""
        if not self._runtime:
            return True
        high_vol = 0
        total = 0
        for iid, rt in self._runtime.items():
            if len(rt.closes) < self.config.atr_period + 1:
                continue
            ind = self._compute_indicators_for_runtime(rt)
            if ind is None:
                continue
            total += 1
            if ind["atr_pct"] >= self.config.vol_regime_high:
                high_vol += 1
        if total == 0:
            return True
        return (high_vol / total) >= self.config.market_heat_threshold

    def _check_btc_silence(self) -> bool:
        """BTC 大波动后静默过滤(可选)。"""
        if self._btc_silence_until <= 0:
            return False
        return self._bar_index <= self._btc_silence_until

    def _update_btc_silence(self, ind: Dict[str, float]) -> None:
        atr = ind["atr"]
        if len(self._btc_bar_history) < 3 or atr <= 0:
            return
        # 计算最近 3 根 K 线的累计位移
        move = abs(ind["close"] - self._btc_bar_history[-3].close.as_double())
        if move >= self.config.btc_volatility_trigger * atr:
            self._btc_silence_until = self._bar_index + self.config.btc_silence_bars

    def _trend_health_filter(self, side: int, ind: Dict[str, float]) -> Tuple[bool, int]:
        """趋势健康过滤:5 项满足 >=3 项。"""
        cfg = self.config
        conditions = 0
        # 1. 实体占比达标
        if ind["body_ratio"] >= cfg.health_body_min:
            conditions += 1
        # 2. 成交量较 20 日均量放大 1.8 倍
        if ind["vol_ratio"] >= cfg.health_vol_mult:
            conditions += 1
        # 3. 均线斜率同向且加速
        if side == 1:
            slope_ok = ind["ema20_slope"] >= cfg.health_slope_min and ind["ema20_accel"] >= 0
        else:
            slope_ok = ind["ema20_slope"] <= -cfg.health_slope_min and ind["ema20_accel"] <= 0
        if slope_ok:
            conditions += 1
        # 4. RSI 未进入极端区
        if cfg.health_rsi_os < ind["rsi"] < cfg.health_rsi_ob:
            conditions += 1
        # 5. 个体波动率环境适中
        if ind["atr_pct"] <= cfg.health_atr_pct_cap:
            conditions += 1
        return (conditions >= 3, conditions)

    def _bar_health_ok(self, side: int, ind: Dict[str, float]) -> bool:
        """K 线健康度:突破当根收盘位于 K 线上半部,逆向影线 < 40%, 实体 >= 35%。"""
        if ind["body_ratio"] < self.config.health_body_min:
            return False
        if side == 1:
            # 多头:下影线 < 40%
            if ind["lower_wick_ratio"] >= 0.40:
                return False
            # 收盘位于上半部
            if ind["close"] < (ind["open"] + ind["high"]) / 2:
                return False
        else:
            if ind["upper_wick_ratio"] >= 0.40:
                return False
            if ind["close"] > (ind["open"] + ind["low"]) / 2:
                return False
        return True

    def _macd_zero_axis_ok(self, side: int, ind: Dict[str, float]) -> bool:
        if side == 1:
            return ind["macd_dif"] > 0
        return ind["macd_dif"] < 0

    def _atr_consumption_ok(self, ind: Dict[str, float]) -> bool:
        return ind["atr_ratio"] < 2.5

    # ------------------------------------------------------------------
    # 下单/仓位
    # ------------------------------------------------------------------

    def _equity(self) -> float:
        return self._current_equity()

    def _can_open_new(self) -> bool:
        return self._current_total_exposure() < self.config.max_total_exposure

    def _submit_market(
        self, instrument_id: InstrumentId, side: OrderSide, qty: float
    ) -> None:
        if qty <= 0:
            return
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            return
        try:
            quantity = instrument.make_qty(Decimal(str(qty)))
        except Exception:  # noqa: BLE001
            return
        if quantity.as_decimal() <= 0:
            return
        order = self.order_factory.market(
            instrument_id=instrument_id,
            order_side=side,
            quantity=quantity,
        )
        # 在途敞口记账
        price = self._last_price(instrument_id)
        if price > 0:
            self._in_flight_notional += qty * price
        self.submit_order(order)
        self.log.info(
            f"提交市价单 {side.name} {instrument_id.symbol} qty={qty:.6f} price≈{price:.4f}"
        )

    def _submit_gtc_limit(
        self,
        instrument_id: InstrumentId,
        side: OrderSide,
        qty: float,
        price: float,
        reduce_only: bool = True,
    ) -> Optional[str]:
        if qty <= 0 or price <= 0:
            return None
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            return None
        try:
            quantity = instrument.make_qty(Decimal(str(qty)))
        except Exception:  # noqa: BLE001
            return None
        if quantity.as_decimal() <= 0:
            return None
        try:
            price_dec = instrument.make_price(Decimal(str(price)))
        except Exception:  # noqa: BLE001
            return None
        order = self.order_factory.limit(
            instrument_id=instrument_id,
            order_side=side,
            quantity=quantity,
            price=price_dec,
            post_only=False,
            reduce_only=reduce_only,
        )
        self.submit_order(order)
        return str(order.client_order_id)

    def _initial_qty(self, instrument_id: InstrumentId, ind: Dict[str, float]) -> float:
        equity = self._equity()
        if equity <= 0:
            return 0.0
        price = ind["close"]
        notional = equity * self.config.initial_position_pct
        qty_by_pct = notional / price if price > 0 else 0.0
        # 冲击成本上限
        avg_quote = sum(self._runtime[instrument_id].quote_volumes[-20:]) / min(
            20, max(1, len(self._runtime[instrument_id].quote_volumes))
        ) if self._runtime.get(instrument_id) else 0.0
        qty_by_impact = (avg_quote * self.config.max_impact_pct) / price if price > 0 else 0.0
        if qty_by_impact > 0:
            qty = min(qty_by_pct, qty_by_impact)
        else:
            qty = qty_by_pct
        return qty

    def _add_qty(self, instrument_id: InstrumentId, upgraded: bool) -> float:
        equity = self._equity()
        if equity <= 0:
            return 0.0
        price = self._last_price(instrument_id)
        pct = self.config.add_position_pct_upgraded if upgraded else self.config.add_position_pct
        notional = equity * pct
        qty = notional / price if price > 0 else 0.0
        return qty

    # ------------------------------------------------------------------
    # 出场判定
    # ------------------------------------------------------------------

    def _profit_protect_check(
        self,
        rt: SymbolRuntime,
        st: PositionState,
        ind: Dict[str, float],
        high: float,
        low: float,
    ) -> bool:
        """阶梯利润保护:首次触及 profit_ladder_initial*ATR 时锁定保护价,任一
        收盘跌破保护价则全平。再次触及更高档时上移保护价。"""
        if st.peak_floating_atr <= 0:
            return False
        atr = st.atr_at_entry
        cfg = self.config
        long_side = st.side == "LONG"
        # 上一档价格
        last_level = st.profit_protect_level
        if last_level == 0:
            trigger = cfg.profit_ladder_initial
        else:
            trigger = (cfg.profit_ladder_initial + 0.5) + (last_level - 1) * 1.0
            trigger = cfg.profit_ladder_initial + 0.5 + (last_level - 1) * cfg.profit_ladder_step
        if st.peak_floating_atr >= trigger:
            # 计算新保护价:以首仓开仓价 + level*ATR
            new_level = last_level + 1
            protect_price = st.first_entry_price + new_level * atr
            if long_side and protect_price > st.profit_protect_price:
                st.profit_protect_price = protect_price
                st.profit_protect_level = new_level
                self.log.info(
                    f"[{st.side} 阶梯保护] 浮盈 {st.peak_floating_atr:.2f}ATR, 保护价上移至 {protect_price:.4f}"
                )
            elif not long_side and (st.profit_protect_price == 0 or protect_price < st.profit_protect_price):
                st.profit_protect_price = protect_price
                st.profit_protect_level = new_level
                self.log.info(
                    f"[{st.side} 阶梯保护] 浮盈 {st.peak_floating_atr:.2f}ATR, 保护价下移至 {protect_price:.4f}"
                )
        # 收盘价 vs 保护价
        close = ind["close"]
        if st.profit_protect_price > 0:
            if long_side and close < st.profit_protect_price:
                self.log.info(f"[{st.side}] 收盘跌破阶梯保护价 {st.profit_protect_price:.4f}, 全平")
                return True
            if not long_side and close > st.profit_protect_price:
                self.log.info(f"[{st.side}] 收盘涨破阶梯保护价 {st.profit_protect_price:.4f}, 全平")
                return True
        return False

    def _layered_stop_check(
        self,
        rt: SymbolRuntime,
        st: PositionState,
        ind: Dict[str, float],
        high: float,
        low: float,
    ) -> bool:
        """三套分层止损:硬止损 / 保本止损 / 移动止损。仅向有利方向棘轮。"""
        cfg = self.config
        atr = st.atr_at_entry
        long_side = st.side == "LONG"
        # 硬止损
        if not st.has_added:
            # 未加仓:25% 反向变动
            if long_side:
                stop = st.first_entry_price * (1.0 - cfg.hard_stop_unadded_pct)
                st.hard_stop_price = stop
                if low <= stop:
                    self.log.info(f"[{st.side} 硬止损] 未加仓触及 {stop:.4f}, 全平")
                    return True
            else:
                stop = st.first_entry_price * (1.0 + cfg.hard_stop_unadded_pct)
                st.hard_stop_price = stop
                if high >= stop:
                    self.log.info(f"[{st.side} 硬止损] 未加仓触及 {stop:.4f}, 全平")
                    return True
        else:
            # 已加仓:首仓价反向 2 倍 ATR
            if long_side:
                stop = st.first_entry_price - cfg.hard_stop_added_atr * atr
                st.hard_stop_price = max(st.hard_stop_price, stop)
                if low <= st.hard_stop_price:
                    self.log.info(f"[{st.side} 硬止损] 已加仓触及 {st.hard_stop_price:.4f}, 全平")
                    return True
            else:
                stop = st.first_entry_price + cfg.hard_stop_added_atr * atr
                if st.hard_stop_price == 0 or stop < st.hard_stop_price:
                    st.hard_stop_price = stop
                if high >= st.hard_stop_price:
                    self.log.info(f"[{st.side} 硬止损] 已加仓触及 {st.hard_stop_price:.4f}, 全平")
                    return True

        # 保本止损
        if not st.breakeven_active and st.peak_floating_atr >= cfg.breakeven_trigger_atr:
            st.breakeven_active = True
            self.log.info(f"[{st.side}] 浮盈达到 {st.peak_floating_atr:.2f}ATR, 激活保本止损")
        if st.breakeven_active:
            if long_side:
                be_price = st.first_entry_price * (1.0 + cfg.breakeven_offset_atr * atr / st.first_entry_price)
                if low <= be_price:
                    self.log.info(f"[{st.side} 保本止损] 触及 {be_price:.4f}, 全平")
                    return True
            else:
                be_price = st.first_entry_price * (1.0 - cfg.breakeven_offset_atr * atr / st.first_entry_price)
                if high >= be_price:
                    self.log.info(f"[{st.side} 保本止损] 触及 {be_price:.4f}, 全平")
                    return True

        # 移动止损
        if not st.trailing_active and st.peak_floating_atr >= cfg.trailing_trigger_atr:
            st.trailing_active = True
            self.log.info(f"[{st.side}] 浮盈达到 {st.peak_floating_atr:.2f}ATR, 激活移动止损")
        if st.trailing_active:
            # 根据个体波动率档位确定回撤倍数
            atr_pct = ind["atr_pct"]
            if atr_pct <= cfg.vol_regime_low:
                trail_mult = cfg.trailing_atr_low
            elif atr_pct >= cfg.vol_regime_high:
                trail_mult = cfg.trailing_atr_high
            else:
                trail_mult = cfg.trailing_atr_mid
            if long_side:
                candidate = st.first_entry_price + (st.peak_floating_atr - trail_mult) * atr
                if st.trailing_stop_price == 0 or candidate > st.trailing_stop_price:
                    st.trailing_stop_price = candidate
                if low <= st.trailing_stop_price:
                    self.log.info(f"[{st.side} 移动止损] 触及 {st.trailing_stop_price:.4f}, 全平")
                    return True
            else:
                candidate = st.first_entry_price - (st.peak_floating_atr - trail_mult) * atr
                if st.trailing_stop_price == 0 or candidate < st.trailing_stop_price:
                    st.trailing_stop_price = candidate
                if high >= st.trailing_stop_price:
                    self.log.info(f"[{st.side} 移动止损] 触及 {st.trailing_stop_price:.4f}, 全平")
                    return True
        return False

    def _time_stop_check(self, rt: SymbolRuntime, st: PositionState) -> bool:
        cfg = self.config
        if not st.has_added:
            if st.peak_floating_atr < 1.0 and st.entry_bars >= cfg.time_stop_hours:
                self.log.info(
                    f"[{st.side} 时间止损] 浮盈未达 1ATR 且持仓 {st.entry_bars} 小时, 全平"
                )
                return True
            if (
                cfg.zombie_stop_hours > 0
                and st.peak_floating_atr >= 1.0
                and st.entry_bars >= cfg.zombie_stop_hours
            ):
                self.log.info(
                    f"[{st.side} 僵尸仓止损] 持仓 {st.entry_bars} 小时, 全平"
                )
                return True
        return False

    def _breakeven_wait_check(self, rt: SymbolRuntime, st: PositionState) -> bool:
        """未加仓 + 持仓 >= breakeven_wait_hours -> 挂 GTC 回本限价单。"""
        cfg = self.config
        if st.has_added:
            return False
        if st.entry_bars < cfg.breakeven_wait_hours:
            return False
        if st.gtc_tp_submitted:
            return False
        long_side = st.side == "LONG"
        target = st.first_entry_price * (1.0 + cfg.gtc_take_profit_pct)
        if not long_side:
            target = st.first_entry_price * (1.0 - cfg.gtc_take_profit_pct)
        side = OrderSide.SELL if long_side else OrderSide.BUY
        qty = abs(self._signed_qty(self._instrument_id_for(st, rt)))
        oid = self._submit_gtc_limit(
            self._instrument_id_for(st, rt),
            side,
            qty,
            target,
            reduce_only=True,
        )
        st.gtc_tp_submitted = True
        st.gtc_tp_price = target
        st.gtc_tp_order_id = oid
        self.log.info(
            f"[{st.side} GTC 限价] 挂单回本+0.5% 止盈: 价格={target:.4f}, qty={qty:.6f}"
        )
        return False

    def _instrument_id_for(self, st: PositionState, rt: SymbolRuntime) -> InstrumentId:
        # 通过 runtime 反查(假设每个 instrument_id 唯一对应一个 runtime)
        for iid, run in self._runtime.items():
            if run is rt:
                return iid
        return self._primary_instrument

    # ------------------------------------------------------------------
    # 加仓通道
    # ------------------------------------------------------------------

    def _atr_stair_channel(
        self,
        rt: SymbolRuntime,
        st: PositionState,
        ind: Dict[str, float],
    ) -> Optional[Tuple[float, bool]]:
        """ATR 价格阶梯加仓通道:浮盈基准为首仓价,每跨过 1 倍 ATR 触发一次。"""
        cfg = self.config
        long_side = st.side == "LONG"
        # 浮盈(以首仓为基准的 ATR 倍数)
        price = ind["close"]
        if long_side:
            floating_atr = (price - st.first_entry_price) / st.atr_at_entry
        else:
            floating_atr = (st.first_entry_price - price) / st.atr_at_entry
        if floating_atr < 1.0:
            return None
        # 跨过档位
        next_step = int(np.floor(floating_atr))
        if next_step > cfg.atr_stair_max:
            next_step = cfg.atr_stair_max
        if next_step <= st.add_ladder_step:
            return None
        # 健康门
        ok, _ = self._add_health_gate(rt, st, ind, long_side)
        if not ok:
            return None
        upgraded = self._should_upgrade(rt, st, ind)
        return (self._add_qty(self._instrument_id_for(st, rt), upgraded), upgraded)

    def _should_upgrade(self, rt: SymbolRuntime, st: PositionState, ind: Dict[str, float]) -> bool:
        if st.health_upgraded:
            return False
        # 健康组满 3 项 / 实体不萎缩 / 量能持续 -> 提档
        ok, count = self._trend_health_filter(1 if st.side == "LONG" else -1, ind)
        body_growing = ind["body_ratio"] >= self.config.health_body_min
        vol_growing = ind["vol_ratio"] >= 1.2
        if count >= 3 or body_growing or vol_growing:
            st.health_upgraded = True
            return True
        return False

    def _add_health_gate(
        self,
        rt: SymbolRuntime,
        st: PositionState,
        ind: Dict[str, float],
        long_side: bool,
    ) -> Tuple[bool, int]:
        """加仓健康门(全部满足 + 趋势健康组 >=2 项)。"""
        cfg = self.config
        atr = st.atr_at_entry
        # ADX 较入场时衰减不超过 5% 且绝对值 >= 20
        adx_ok = ind["adx"] >= 20 and ind["adx"] >= rt.adx_at_entry * 0.95
        # EMA20 近三根斜率同向且强度 >= 0.25%
        slope_threshold = cfg.health_slope_min
        if long_side:
            slope_ok = ind["ema20_slope"] >= slope_threshold
        else:
            slope_ok = ind["ema20_slope"] <= -slope_threshold
        # 当根不是反向 1.5 倍 ATR 大实体
        bar_move = (ind["close"] - ind["open"]) / atr if atr > 0 else 0.0
        if long_side:
            big_reverse = bar_move < -1.5
        else:
            big_reverse = bar_move > 1.5
        reverse_ok = not big_reverse
        # MACD 处于对应方向金叉/死叉
        if long_side:
            macd_ok = ind["macd_dif"] > ind["macd_dea"] and ind["macd_dif"] > 0
        else:
            macd_ok = ind["macd_dif"] < ind["macd_dea"] and ind["macd_dif"] < 0
        base_ok = adx_ok and slope_ok and reverse_ok and macd_ok
        # 趋势健康组 >=2 项
        _, count = self._trend_health_filter(1 if long_side else -1, ind)
        group_ok = count >= 2
        return (base_ok and group_ok, count)

    def _timing_signal_channel(
        self,
        rt: SymbolRuntime,
        st: PositionState,
        ind: Dict[str, float],
    ) -> Optional[Tuple[float, bool]]:
        """时序动能重启信号加仓通道。"""
        cfg = self.config
        if st.timing_signals_used >= cfg.timing_signal_quota:
            return None
        long_side = st.side == "LONG"
        atr = st.atr_at_entry
        # 浮盈基准为当前持仓均价
        if long_side:
            floating_atr = (ind["close"] - st.avg_entry_price) / atr
        else:
            floating_atr = (st.avg_entry_price - ind["close"]) / atr
        if floating_atr < cfg.timing_min_atr:
            return None
        # 四种信号,每位独立检测
        closes = list(rt.closes)
        highs = list(rt.highs)
        lows = list(rt.lows)
        volumes = list(rt.volumes)
        # 信号 0:波动扩张 + 创 5 根新高
        signal_0 = False
        if len(closes) >= 6 and atr > 0:
            recent_range = max(highs[-5:]) - min(lows[-5:])
            prev_range = max(highs[-10:-5]) - min(lows[-10:-5]) if len(closes) >= 11 else recent_range
            expansion = recent_range > prev_range * 1.2
            if long_side:
                signal_0 = expansion and ind["close"] >= max(highs[-6:-1])
            else:
                signal_0 = expansion and ind["close"] <= min(lows[-6:-1])
        # 信号 1:巨量单根 (|bar_return| >= 2*std_20) + 创 10 根新高 + EMA20 正确一侧
        signal_1 = False
        if len(closes) >= 21 and len(rt.closes) >= 11:
            bar_return = (closes[-1] - closes[-2]) / closes[-2]
            returns = np.diff(closes[-21:]) / np.array(closes[-21:][:-1])
            std_20 = float(np.std(returns, ddof=0))
            big_bar = abs(bar_return) >= 2.0 * std_20
            if long_side:
                new_high = ind["close"] >= max(highs[-11:-1])
                ema_side = ind["close"] > ind["ema20"]
            else:
                new_high = ind["close"] <= min(lows[-11:-1])
                ema_side = ind["close"] < ind["ema20"]
            signal_1 = big_bar and new_high and ema_side
        # 信号 2:收盘越过 20 根极值 + 0.5 ATR
        signal_2 = False
        if len(closes) >= 21:
            high_20 = max(highs[-21:-1])
            low_20 = min(lows[-21:-1])
            if long_side:
                signal_2 = ind["close"] >= high_20 + 0.5 * atr
            else:
                signal_2 = ind["close"] <= low_20 - 0.5 * atr
        # 信号 3:3 根连续同向累计 >= 1.5 ATR
        signal_3 = False
        if len(closes) >= 4 and atr > 0:
            moves = [closes[-i] - closes[-i - 1] for i in range(1, 4)]
            if long_side:
                signal_3 = all(m > 0 for m in moves) and sum(moves) >= 1.5 * atr
            else:
                signal_3 = all(m < 0 for m in moves) and abs(sum(moves)) >= 1.5 * atr
        signals = [signal_0, signal_1, signal_2, signal_3]
        for i, s in enumerate(signals):
            if s and (st.timing_signals_mask & (1 << i)) == 0:
                # 找到未触发的信号,触发一次
                upgraded = floating_atr >= cfg.timing_high_atr
                st.timing_signals_mask |= (1 << i)
                st.timing_signals_used += 1
                if not st.health_upgraded and upgraded:
                    st.health_upgraded = True
                return (self._add_qty(self._instrument_id_for(st, rt), upgraded), upgraded)
        return None

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        self._bar_index += 1
        instrument_id = bar.bar_type.instrument_id
        rt = self._push_bar_history(bar)
        ind = self._compute_indicators_for_runtime(rt)
        if ind is None:
            return

        # BTC 静默更新
        self._btc_bar_history.append(bar)
        if instrument_id.symbol.upper().endswith("BTC") or "BTC" in instrument_id.symbol.upper():
            self._update_btc_silence(ind)

        # 维护压缩状态
        self._update_compression(rt, ind)

        # 多/空状态对象
        long_state = rt.long_state
        short_state = rt.short_state

        # ---- 出场判定(优先级最高) ----
        high = ind["high"]
        low = ind["low"]
        close = ind["close"]
        for st in (long_state, short_state):
            if st is None:
                continue
            st.entry_bars += 1
            atr = st.atr_at_entry
            if atr <= 0:
                continue
            long_side = st.side == "LONG"
            if long_side:
                floating_atr = (high - st.first_entry_price) / atr
            else:
                floating_atr = (st.first_entry_price - low) / atr
            if floating_atr > st.peak_floating_atr:
                st.peak_floating_atr = floating_atr
            if self._profit_protect_check(rt, st, ind, high, low):
                self._close_position(instrument_id, st)
                continue
            if self._layered_stop_check(rt, st, ind, high, low):
                self._close_position(instrument_id, st)
                continue
            if self._time_stop_check(rt, st):
                self._close_position(instrument_id, st)
                continue
            # 挂 GTC 限价回本
            self._breakeven_wait_check(rt, st)

        # 重新取一次(可能已平仓)
        long_state = rt.long_state
        short_state = rt.short_state

        # ---- 部分止盈:阶梯保护价已触达时按当前价减仓(可选,这里保留 50% 减仓) ----
        for st in (long_state, short_state):
            if st is None or st.size_held <= 0:
                continue
            if st.profit_protect_level >= 2 and not getattr(st, "_partial_done", False):
                # 第一次达到第二档时减仓一半
                pos = self.cache.position(instrument_id)
                if pos is not None:
                    qty = abs(float(pos.signed_qty)) * 0.5
                    if qty > 0:
                        side = OrderSide.SELL if pos.side == PositionSide.LONG else OrderSide.BUY
                        self._submit_market(instrument_id, side, qty)
                        st._partial_done = True
                        self.log.info(f"[{st.side} 部分止盈] 减仓 50% @ {close:.4f}")

        # ---- 加仓通道 ----
        if self._can_open_new():
            for st in (long_state, short_state):
                if st is None or st.size_held <= 0:
                    continue
                # 单币种累计仓位上限
                equity = self._equity()
                if equity > 0 and (st.size_held + self.config.add_position_pct) > self.config.max_position_pct_per_symbol:
                    continue
                if st.add_ladder_step < self.config.atr_stair_max:
                    res = self._atr_stair_channel(rt, st, ind)
                    if res is not None:
                        qty, upgraded = res
                        if self._can_execute(instrument_id, qty):
                            side = OrderSide.BUY if st.side == "LONG" else OrderSide.SELL
                            self._submit_market(instrument_id, side, qty)
                            st.add_ladder_step += 1
                            st.has_added = True
                            # 更新平均成本
                            total_size = st.size_held + qty
                            if total_size > 0:
                                st.avg_entry_price = (
                                    st.avg_entry_price * st.size_held + ind["close"] * qty
                                ) / total_size
                            st.size_held = total_size
                            self.log.info(
                                f"[{st.side} ATR 阶梯加仓] 第 {st.add_ladder_step} 档, qty={qty:.6f} upgraded={upgraded}"
                            )
                            continue
                res = self._timing_signal_channel(rt, st, ind)
                if res is not None:
                    qty, upgraded = res
                    if self._can_execute(instrument_id, qty):
                        side = OrderSide.BUY if st.side == "LONG" else OrderSide.SELL
                        self._submit_market(instrument_id, side, qty)
                        st.has_added = True
                        total_size = st.size_held + qty
                        if total_size > 0:
                            st.avg_entry_price = (
                                st.avg_entry_price * st.size_held + ind["close"] * qty
                            ) / total_size
                        st.size_held = total_size
                        self.log.info(
                            f"[{st.side} 时序加仓] qty={qty:.6f} upgraded={upgraded} (已用 {st.timing_signals_used}/4)"
                        )

        # ---- 开首仓 ----
        signal = self._has_breakout_signal(rt, ind)
        if signal != 0 and self._can_open_new():
            side_int = signal
            long_side = side_int == 1
            # 冷却期
            if long_side and self._bar_index <= rt.cooldown_long_until:
                signal = 0
            if not long_side and self._bar_index <= rt.cooldown_short_until:
                signal = 0
            # BTC 静默
            if self._check_btc_silence():
                signal = 0
            # 已有同方向持仓 -> 跳过
            if long_side and rt.long_state is not None:
                signal = 0
            if not long_side and rt.short_state is not None:
                signal = 0
            # 硬过滤
            if signal != 0:
                if not self._bar_health_ok(side_int, ind):
                    signal = 0
            if signal != 0 and not self._macd_zero_axis_ok(side_int, ind):
                signal = 0
            if signal != 0 and not self._atr_consumption_ok(ind):
                signal = 0
            if signal != 0 and not self._check_market_heat():
                signal = 0
            if signal != 0:
                # 趋势健康过滤
                ok, _ = self._trend_health_filter(side_int, ind)
                if not ok:
                    signal = 0
            if signal != 0:
                qty = self._initial_qty(instrument_id, ind)
                if qty > 0 and self._can_execute(instrument_id, qty):
                    side = OrderSide.BUY if long_side else OrderSide.SELL
                    self._submit_market(instrument_id, side, qty)
                    state = PositionState(
                        side="LONG" if long_side else "SHORT",
                        first_entry_price=ind["close"],
                        avg_entry_price=ind["close"],
                        atr_at_entry=ind["atr"],
                        size_held=qty,
                    )
                    state.gtc_tp_price = ind["close"] * (1.0 + self.config.gtc_take_profit_pct) if long_side else ind["close"] * (1.0 - self.config.gtc_take_profit_pct)
                    rt.adx_at_entry = ind["adx"]
                    if long_side:
                        rt.long_state = state
                    else:
                        rt.short_state = state
                    rt.last_breakout_dir = side_int
                    self.log.info(
                        f"[{state.side} 首仓] 价格={ind['close']:.4f} qty={qty:.6f} ATR={ind['atr']:.4f}"
                    )

    def _can_execute(self, instrument_id: InstrumentId, qty: float) -> bool:
        equity = self._equity()
        if equity <= 0 or qty <= 0:
            return False
        price = self._last_price(instrument_id)
        if price <= 0:
            return False
        # 杠杆上限 2.0
        new_notional = (self._current_total_exposure() * equity) + qty * price
        if new_notional > self.config.max_leverage * equity:
            return False
        # 在途敞口叠加
        if (self._in_flight_notional + qty * price) / equity > self.config.max_total_exposure:
            return False
        return True

    def _close_position(self, instrument_id: InstrumentId, st: PositionState) -> None:
        pos = self.cache.position(instrument_id)
        if pos is None or float(pos.signed_qty) == 0.0:
            # 清除状态
            rt = self._runtime.get(instrument_id)
            if rt is not None:
                if st.side == "LONG":
                    rt.long_state = None
                else:
                    rt.short_state = None
            return
        # 撤 GTC 单
        if st.gtc_tp_submitted and st.gtc_tp_order_id is not None:
            try:
                self.cancel_order(st.gtc_tp_order_id)
            except Exception:  # noqa: BLE001
                pass
            st.gtc_tp_submitted = False
        # 市价平仓
        side = OrderSide.SELL if pos.side == PositionSide.LONG else OrderSide.BUY
        qty = abs(float(pos.signed_qty))
        self._submit_market(instrument_id, side, qty)
        # 启动冷却
        rt = self._runtime.get(instrument_id)
        if rt is not None:
            if st.side == "LONG":
                rt.cooldown_long_until = self._bar_index + self.config.cooldown_bars
                rt.long_state = None
            else:
                rt.cooldown_short_until = self._bar_index + self.config.cooldown_bars
                rt.short_state = None
        self.log.info(f"[{st.side} 平仓] 数量 {qty:.6f}, 启动 {self.config.cooldown_bars} 根 K 线冷却")

    def on_order_filled(self, event) -> None:  # noqa: D401
        # 释放在途敞口
        try:
            last_px = float(event.last_px.as_double()) if event.last_px is not None else 0.0
        except Exception:  # noqa: BLE001
            last_px = 0.0
        try:
            last_qty = float(event.last_qty.as_double()) if event.last_qty is not None else 0.0
        except Exception:  # noqa: BLE001
            last_qty = 0.0
        self._in_flight_notional = max(0.0, self._in_flight_notional - last_px * last_qty)

    def on_order_rejected(self, event) -> None:  # noqa: D401
        try:
            last_px = float(event.last_px.as_double()) if getattr(event, "last_px", None) is not None else 0.0
        except Exception:  # noqa: BLE001
            last_px = 0.0
        try:
            last_qty = float(event.last_qty.as_double()) if getattr(event, "last_qty", None) is not None else 0.0
        except Exception:  # noqa: BLE001
            last_qty = 0.0
        self._in_flight_notional = max(0.0, self._in_flight_notional - last_px * last_qty)


# ---------------------------------------------------------------------------
# 离线指标计算(用于回测/绘图)
# ---------------------------------------------------------------------------


def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    """向量化计算所有 plot_config 指标列。"""
    df = dataframe.copy()
    bb_period = int(parameters.get("bb_period", 20))
    bb_std = float(parameters.get("bb_std", 2.0))
    kc_period = int(parameters.get("kc_period", 20))
    kc_atr_mult = float(parameters.get("kc_atr_mult", 1.5))
    atr_period = int(parameters.get("atr_period", 14))
    atr_short = int(parameters.get("atr_short_period", 3))
    atr_long = int(parameters.get("atr_long_period", 20))
    ema_period = int(parameters.get("ema_period", 20))
    macd_fast = int(parameters.get("macd_fast", 12))
    macd_slow = int(parameters.get("macd_slow", 26))
    macd_signal = int(parameters.get("macd_signal", 9))
    rsi_period = int(parameters.get("rsi_period", 14))
    adx_period = int(parameters.get("adx_period", 14))
    vol_ma = int(parameters.get("vol_ma_period", 20))

    close = df["close"]
    high = df["high"]
    low = df["low"]
    vol = df["volume"]

    # 布林带
    df["bb_mid"] = close.rolling(bb_period).mean()
    df["bb_std"] = close.rolling(bb_period).std(ddof=0)
    df["bb_upper"] = df["bb_mid"] + bb_std * df["bb_std"]
    df["bb_lower"] = df["bb_mid"] - bb_std * df["bb_std"]
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]

    # ATR (Wilder)
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["atr"] = tr.rolling(atr_period).mean()
    df["atr_short"] = tr.rolling(atr_short).mean()
    df["atr_long"] = tr.rolling(atr_long).mean()
    df["atr_ratio"] = df["atr_short"] / df["atr_long"]
    df["atr_pct"] = df["atr"] / close

    # 肯特纳通道
    kc_mid = close.ewm(span=kc_period, adjust=False).mean()
    df["kc_mid"] = kc_mid
    df["kc_upper"] = kc_mid + kc_atr_mult * df["atr"]
    df["kc_lower"] = kc_mid - kc_atr_mult * df["atr"]
    df["kc_width"] = (df["kc_upper"] - df["kc_lower"]) / kc_mid

    # 压缩状态
    df["is_compressed"] = df["bb_width"] < df["kc_width"]

    # EMA20
    df["ema20"] = close.ewm(span=ema_period, adjust=False).mean()
    df["ema20_slope"] = df["ema20"].pct_change()
    df["ema20_accel"] = df["ema20_slope"].diff()

    # MACD
    ema_fast = close.ewm(span=macd_fast, adjust=False).mean()
    ema_slow = close.ewm(span=macd_slow, adjust=False).mean()
    df["macd_dif"] = ema_fast - ema_slow
    df["macd_dea"] = df["macd_dif"].ewm(span=macd_signal, adjust=False).mean()
    df["macd_hist"] = (df["macd_dif"] - df["macd_dea"]) * 2.0

    # RSI
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(rsi_period).mean()
    loss = (-delta.clip(upper=0)).rolling(rsi_period).mean()
    rs = gain / loss
    df["rsi"] = 100 - 100 / (1 + rs)

    # ADX 简化:先算 +DM/-DM/TR 的滚动均值
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr_smooth = tr.rolling(adx_period).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).rolling(adx_period).mean() / tr_smooth
    minus_di = 100 * pd.Series(minus_dm, index=df.index).rolling(adx_period).mean() / tr_smooth
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    df["adx"] = dx.rolling(adx_period).mean()
    df["plus_di"] = plus_di
    df["minus_di"] = minus_di

    # 量比 & K线健康度
    df["vol_ma"] = vol.rolling(vol_ma).mean()
    df["vol_ratio"] = vol / df["vol_ma"]
    full_range = (high - low).replace(0, np.nan)
    body = (close - df["open"]).abs()
    df["body_ratio"] = body / full_range
    df["upper_wick_ratio"] = (high - pd.concat([close, df["open"]], axis=1).max(axis=1)) / full_range
    df["lower_wick_ratio"] = (pd.concat([close, df["open"]], axis=1).min(axis=1) - low) / full_range

    # 收益率(用于时序信号)
    df["ret_1"] = close.pct_change()
    df["ret_std_20"] = df["ret_1"].rolling(20).std()

    return df


# ---------------------------------------------------------------------------
# StrategyManifest
# ---------------------------------------------------------------------------


STRATEGY_MANIFEST = StrategyManifest(
    slug="volatility_compression_midcap_breakout",
    name="中盘波动率压缩突破双轨加仓趋势策略",
    version="1.0.0",
    description=(
        "聚焦流动性排名 20~300 的中盘币,通过布林带/肯特纳通道宽度判定 "
        "波动率压缩状态,在压缩 3~20 根 K 线后等待收盘突破布林外沿"
        "(偏移 0.15 ATR)产生多/空信号,采用 1% 权益首仓 + ATR 价格阶梯 / "
        "时序动能重启双轨加仓,搭配三层止损、阶梯利润保护和时间止损, "
        "对 BTC 大幅波动进行市场级静默过滤,整体控制总敞口 170% 与 "
        "总杠杆 2.0 倍。"
    ),
    category="trend_breakout",
    strategy_path="backend.app.strategies.volatility_compression_midcap_breakout:VolatilityCompressionMidcapBreakout",
    config_path="backend.app.strategies.volatility_compression_midcap_breakout:VolatilityCompressionMidcapBreakoutConfig",
    parameters={
        "bb_period": ParameterSpec(title="布林带周期", type="integer", default=20, minimum=10, maximum=60, description="布林带均线周期"),
        "bb_std": ParameterSpec(title="布林带标准差倍数", type="number", default=2.0, minimum=1.0, maximum=3.5),
        "kc_period": ParameterSpec(title="肯特纳通道均线周期", type="integer", default=20, minimum=10, maximum=60),
        "kc_atr_mult": ParameterSpec(title="肯特纳通道 ATR 倍数", type="number", default=1.5, minimum=0.5, maximum=3.0),
        "atr_period": ParameterSpec(title="ATR 周期", type="integer", default=14, minimum=5, maximum=40),
        "atr_short_period": ParameterSpec(title="短期 ATR 周期", type="integer", default=3, minimum=2, maximum=10),
        "atr_long_period": ParameterSpec(title="长期 ATR 周期", type="integer", default=20, minimum=10, maximum=60),
        "compression_min_bars": ParameterSpec(title="压缩最小持续 K 线数", type="integer", default=3, minimum=2, maximum=20),
        "compression_max_bars": ParameterSpec(title="压缩最大持续 K 线数", type="integer", default=20, minimum=5, maximum=60),
        "breakout_atr_offset": ParameterSpec(title="突破外沿 ATR 偏移", type="number", default=0.15, minimum=0.0, maximum=0.5),
        "initial_position_pct": ParameterSpec(title="首仓权益占比", type="number", default=0.01, minimum=0.002, maximum=0.05),
        "add_position_pct": ParameterSpec(title="加仓权益占比(默认档)", type="number", default=0.02, minimum=0.005, maximum=0.05),
        "add_position_pct_upgraded": ParameterSpec(title="加仓权益占比(提档)", type="number", default=0.03, minimum=0.01, maximum=0.06),
        "max_position_pct_per_symbol": ParameterSpec(title="单币种累计仓位上限", type="number", default=0.22, minimum=0.05, maximum=0.5),
        "max_total_exposure": ParameterSpec(title="总敞口硬约束", type="number", default=1.70, minimum=1.0, maximum=2.5),
        "max_leverage": ParameterSpec(title="最大杠杆", type="number", default=2.0, minimum=1.0, maximum=3.0),
        "max_impact_pct": ParameterSpec(title="冲击成本上限占比", type="number", default=0.015, minimum=0.001, maximum=0.05),
        "hard_stop_unadded_pct": ParameterSpec(title="未加仓硬止损比例", type="number", default=0.25, minimum=0.05, maximum=0.5),
        "hard_stop_added_atr": ParameterSpec(title="已加仓硬止损 ATR 倍数", type="number", default=2.0, minimum=1.0, maximum=5.0),
        "breakeven_trigger_atr": ParameterSpec(title="保本触发 ATR 倍数", type="number", default=1.2, minimum=0.5, maximum=3.0),
        "breakeven_offset_atr": ParameterSpec(title="保本止盈偏移 ATR 倍数", type="number", default=0.2, minimum=0.0, maximum=1.0),
        "trailing_trigger_atr": ParameterSpec(title="移动止损触发 ATR 倍数", type="number", default=3.0, minimum=1.5, maximum=6.0),
        "profit_ladder_initial": ParameterSpec(title="阶梯保护初始 ATR 倍数", type="number", default=5.0, minimum=3.0, maximum=10.0),
        "cooldown_bars": ParameterSpec(title="冷却期 K 线数", type="integer", default=12, minimum=4, maximum=48),
        "btc_silence_bars": ParameterSpec(title="BTC 静默 K 线数", type="integer", default=12, minimum=4, maximum=48),
        "btc_volatility_trigger": ParameterSpec(title="BTC 静默触发 ATR 倍数", type="number", default=3.0, minimum=1.5, maximum=6.0),
        "time_stop_hours": ParameterSpec(title="时间止损小时数", type="integer", default=360, minimum=120, maximum=720),
        "zombie_stop_hours": ParameterSpec(title="僵尸仓止损小时数", type="integer", default=372, minimum=200, maximum=800),
        "breakeven_wait_hours": ParameterSpec(title="GTC 等待回本小时数", type="integer", default=8, minimum=2, maximum=48),
        "atr_stair_max": ParameterSpec(title="ATR 阶梯最大档位", type="integer", default=8, minimum=3, maximum=12),
        "timing_signal_quota": ParameterSpec(title="时序信号加仓配额", type="integer", default=4, minimum=1, maximum=6),
        "market_heat_threshold": ParameterSpec(title="市场热度阈值", type="number", default=0.08, minimum=0.02, maximum=0.2),
        "health_body_min": ParameterSpec(title="健康过滤-最小实体占比", type="number", default=0.35, minimum=0.2, maximum=0.6),
        "health_vol_mult": ParameterSpec(title="健康过滤-量能倍数", type="number", default=1.8, minimum=1.0, maximum=3.0),
        "health_slope_min": ParameterSpec(title="健康过滤-EMA 斜率阈值", type="number", default=0.0025, minimum=0.001, maximum=0.01),
    },
    timeframes=("1h",),
    primary_timeframe="1h",
    plot_config={
        "main_plot": {
            "bb_upper": {"type": "line", "color": "#3a8dde", "title": "布林上轨"},
            "bb_mid": {"type": "line", "color": "#1f4e8a", "title": "布林中轨"},
            "bb_lower": {"type": "line", "color": "#3a8dde", "title": "布林下轨"},
            "kc_upper": {"type": "line", "color": "#e07b39", "title": "肯特纳上轨"},
            "kc_mid": {"type": "line", "color": "#a35214", "title": "肯特纳中轨"},
            "kc_lower": {"type": "line", "color": "#e07b39", "title": "肯特纳下轨"},
            "ema20": {"type": "line", "color": "#f1c40f", "title": "EMA20"},
        },
        "subplots": {
            "波动率": {
                "atr": {"type": "line", "color": "#16a085", "title": "ATR"},
                "bb_width": {"type": "line", "color": "#2980b9", "title": "布林宽度"},
                "kc_width": {"type": "line", "color": "#d35400", "title": "肯特纳宽度"},
            },
            "MACD": {
                "macd_dif": {"type": "line", "color": "#2980b9", "title": "DIF"},
                "macd_dea": {"type": "line", "color": "#e67e22", "title": "DEA"},
                "macd_hist": {"type": "histogram", "color": "#27ae60", "title": "MACD Hist"},
            },
            "动量": {
                "rsi": {"type": "line", "color": "#8e44ad", "title": "RSI"},
                "adx": {"type": "line", "color": "#c0392b", "title": "ADX"},
                "atr_ratio": {"type": "line", "color": "#7f8c8d", "title": "ATR3/ATR20"},
            },
            "量能": {
                "vol_ratio": {"type": "line", "color": "#16a085", "title": "量比"},
                "body_ratio": {"type": "line", "color": "#34495e", "title": "实体占比"},
            },
        },
    },
    mode=StrategyMode.PORTFOLIO,
    supports_short=True,
    requires_funding=False,
)


# 显式导出(便于 reload/import)
__all__ = [
    "VolatilityCompressionMidcapBreakout",
    "VolatilityCompressionMidcapBreakoutConfig",
    "calculate_indicators",
    "STRATEGY_MANIFEST",
]
