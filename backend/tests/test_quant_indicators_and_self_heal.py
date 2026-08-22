"""Tests for QuantLab Indicators, Strategy Base, Auto-Derivation, and AST Method Patching."""

from decimal import Decimal
from pathlib import Path
import tempfile
import numpy as np
import pandas as pd
import pytest

from app.agent.strategy_verifier import (
    extract_target_method_from_error,
    patch_strategy_method,
    verify_strategy_file,
)
from app.quant.indicators import (
    ATRTrailingStopTracker,
    IncWilderADX,
    SqueezeStateTracker,
    calc_standard_indicators,
)
from app.strategy_base import QuantLabStrategy
from app.strategy_contract import calculate_plot_indicators


def test_inc_wilder_adx_computation():
    """Verify incremental Wilder ADX state machine calculation."""
    adx_calc = IncWilderADX(period=14)
    np.random.seed(42)
    prices = 50000.0 + np.cumsum(np.random.randn(50) * 100)

    for p in prices:
        h = p + 50.0
        l = p - 50.0
        c = p + 10.0
        adx, pdi, mdi = adx_calc.update(h, l, c)

    assert adx_calc.is_ready is True
    assert 0.0 <= adx_calc.adx <= 100.0
    assert 0.0 <= adx_calc.plus_di <= 100.0
    assert 0.0 <= adx_calc.minus_di <= 100.0


def test_squeeze_state_tracker():
    """Verify BB & KC Squeeze energy accumulation and breakout."""
    tracker = SqueezeStateTracker(min_bars=3, expiry_bars=4)

    # 1. Squeeze inside: BB within KC
    for _ in range(3):
        is_ready = tracker.update(
            bb_upper=102.0,
            bb_lower=98.0,
            kc_upper=105.0,
            kc_lower=95.0,
            close=100.0,
        )
    assert is_ready is True
    assert tracker.squeeze_completed is True
    assert tracker.frozen_bb_upper == 102.0

    # 2. Breakout outside channel
    for _ in range(5):
        tracker.update(
            bb_upper=108.0,
            bb_lower=92.0,
            kc_upper=105.0,
            kc_lower=95.0,
            close=107.0,
        )
    # Exceeded expiry_bars -> reset
    assert tracker.squeeze_completed is False


def test_atr_trailing_stop_tracker():
    """Verify multi-tier ATR stop loss, trailing stop, and breakeven arming."""
    tracker = ATRTrailingStopTracker()

    # LONG position entry at 100 with ATR 2.0
    tracker.on_entry(
        side="LONG",
        entry_price=100.0,
        entry_atr=2.0,
        hard_atr_mult=2.5,
        trail_atr_mult=2.0,
        arm_atr_mult=1.5,
        fixed_sl_pct=0.10,
    )
    assert tracker.trailing_stop_price == 96.0  # 100 - 2*2
    assert tracker.hard_stop_price == 95.0      # 100 - 2.5*2
    assert tracker.fixed_stop_price == 90.0     # 100 * (1 - 0.1)

    # Price moves up to 104 -> triggers breakeven (104 - 100 >= 1.5*2 = 3.0)
    should_exit, reason = tracker.check_exit(close=104.0, high=104.5)
    assert should_exit is False
    assert tracker.breakeven_armed is True
    assert tracker.trailing_stop_price >= 100.0  # Breakeven armed

    # Addon at 105 -> updates avg_price and tightens stop
    tracker.on_addon(add_price=105.0, new_avg_price=102.5, current_atr=2.0)
    assert tracker.avg_price == 102.5

    # Price drops to 99 -> hits trailing stop
    should_exit, reason = tracker.check_exit(close=99.0)
    assert should_exit is True
    assert reason == "BREAKEVEN_STOP"


def test_calc_standard_indicators_vectorized():
    """Verify calc_standard_indicators produces all columns with 0 NaNs."""
    rows = 100
    dates = pd.date_range("2024-01-01", periods=rows, freq="1h")
    df = pd.DataFrame({
        "timestamp": dates,
        "open": 100.0 + np.random.randn(rows),
        "high": 102.0 + np.random.randn(rows),
        "low": 98.0 + np.random.randn(rows),
        "close": 100.5 + np.random.randn(rows),
        "volume": np.random.uniform(10, 100, size=rows),
    })

    result = calc_standard_indicators(df, {"fast_period": 10, "slow_period": 20, "atr_period": 14})

    expected_cols = [
        "fast_ma", "slow_ma", "ma_fast", "ma_slow",
        "tr", "atr", "plus_di", "minus_di", "adx",
        "bb_mid", "bb_upper", "bb_lower",
        "kc_mid", "kc_upper", "kc_lower",
        "donchian_upper", "donchian_lower", "donchian_mid",
        "vol_ma", "rsi",
    ]
    for col in expected_cols:
        assert col in result.columns
        assert result[col].isna().sum() == 0, f"Column {col} contains NaNs"


def test_quantlab_strategy_base_probes_and_helpers():
    """Verify QuantLabStrategy metric probes, Series extractors, and trading helpers."""
    from nautilus_trader.config import StrategyConfig
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.model.data import Bar, BarType

    class DummyConfig(StrategyConfig, frozen=True):
        instrument_id: InstrumentId
        bar_type: BarType
        trade_size: Decimal = Decimal("0.05")

    dummy_config = DummyConfig(
        instrument_id=InstrumentId.from_str("BTCUSDT.BINANCE"),
        bar_type=BarType.from_str("BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL"),
        trade_size=Decimal("0.05"),
    )
    strat = QuantLabStrategy(dummy_config)
    assert strat.is_flat() is True

    # Test metric probe recording
    strat.record("fast_ma", 50000.0, ts_event=1000)
    strat.record("slow_ma", 49500.0, ts_event=1000)
    metrics = strat.get_recorded_metrics()
    assert "fast_ma" in metrics
    assert metrics["fast_ma"][0] == (1000, 50000.0)



def test_patch_strategy_method():
    """Verify AST method-level patching of strategy methods."""
    source_code = """from app.strategy_base import QuantLabStrategy

class DemoStrategy(QuantLabStrategy):
    def on_start(self) -> None:
        pass

    def on_bar(self, bar: Bar) -> None:
        # buggy calculation
        val = 1 / 0

    def on_stop(self) -> None:
        pass
"""
    new_method = """    def on_bar(self, bar: Bar) -> None:
        # fixed calculation
        closes = self.get_close_series()
        self.record("fast_ma", 100.0)
"""
    patched = patch_strategy_method(source_code, "on_bar", new_method)
    assert "val = 1 / 0" not in patched
    assert "fixed calculation" in patched
    assert "self.record(\"fast_ma\", 100.0)" in patched

    # Test target method extractor
    target = extract_target_method_from_error("ZeroDivisionError in on_bar line 8")
    assert target == "on_bar"


def test_quantlab_strategy_without_calculate_indicators(tmp_path: Path):
    """Verify a strategy inheriting from QuantLabStrategy passes all 4 Pre-Flight levels without calculate_indicators!"""
    strategy_code = """from decimal import Decimal
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import InstrumentId
from app.strategy_base import QuantLabStrategy
from app.strategy_contract import StrategyManifest, ParameterSpec, StrategyMode


class UltraPureTrendConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    fast_period: int = 12
    slow_period: int = 26
    trade_size: Decimal = Decimal("0.01")


class UltraPureTrendStrategy(QuantLabStrategy):
    def on_bar(self, bar: Bar) -> None:
        super().on_bar(bar)
        if len(self.bars) < self.config.slow_period + 5:
            return

        closes = self.get_close_series()
        fast_ma = closes.ewm(span=self.config.fast_period).mean().iloc[-1]
        slow_ma = closes.ewm(span=self.config.slow_period).mean().iloc[-1]
        
        self.record("fast_ma", fast_ma)
        self.record("slow_ma", slow_ma)

        if fast_ma > slow_ma and not self.is_long():
            self.buy_market(trade_size=self.config.trade_size)
        elif fast_ma < slow_ma and self.is_long():
            self.close_position()


STRATEGY_MANIFEST = StrategyManifest(
    slug="ultra_pure_trend",
    name="Ultra Pure Trend Strategy",
    description="Zero boilerplate QuantLab strategy with automated indicator derivation",
    version="1.0.0",
    category="trend",
    strategy_path="app.strategies.ultra_pure_trend:UltraPureTrendStrategy",
    config_path="app.strategies.ultra_pure_trend:UltraPureTrendConfig",
    parameters={
        "fast_period": ParameterSpec(title="快线周期", type="integer", default=12, minimum=2, maximum=100),
        "slow_period": ParameterSpec(title="慢线周期", type="integer", default=26, minimum=5, maximum=200),
        "trade_size": ParameterSpec(title="单笔下单数量", type="number", default=0.01, minimum=0.0001, maximum=100.0),
    },
    timeframes=("15m", "1h", "4h", "1d"),
    primary_timeframe="1h",
    plot_config={
        "main_plot": {
            "close": {"type": "line", "color": "#ffffff"},
            "fast_ma": {"type": "line", "color": "#ffaa00"},
            "slow_ma": {"type": "line", "color": "#00aaff"},
        }
    },
    mode=StrategyMode.SINGLE_INSTRUMENT,
    supports_short=True,
    requires_funding=True,
)
"""
    strat_file = tmp_path / "ultra_pure_trend.py"
    strat_file.write_text(strategy_code, encoding="utf-8")

    res = verify_strategy_file(strat_file, strategy_name="ultra_pure_trend")
    assert res.ok is True, f"Strategy verification failed: {res.failed_level} - {res.error_message}"
    assert len(res.steps) == 4
    for s in res.steps:
        assert s.ok is True, f"Step {s.level} failed: {s.message}"


def test_btc_bollinger_regime_mr_runtime_verification():
    """Verify btc_bollinger_regime_mr strategy passes all 4 Pre-Flight levels."""
    strat_path = (Path(__file__).resolve().parent.parent / "app/strategies/btc_bollinger_regime_mr.py").resolve()
    if not strat_path.exists():
        import pytest
        pytest.skip(f"Strategy file does not exist at {strat_path}")

    res = verify_strategy_file(strat_path, strategy_name="btc_bollinger_regime_mr")
    assert res.ok is True, f"Verification failed: {res.failed_level} - {res.error_message}"
    assert len(res.steps) == 4
    assert all(s.ok for s in res.steps)

