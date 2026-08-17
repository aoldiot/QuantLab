from __future__ import annotations

from pathlib import Path
import pytest

from app.agent.strategy_verifier import verify_strategy_file, verify_strategy_source


def test_verify_canonical_macd_strategy():
    strategy_path = Path(__file__).resolve().parent.parent / "app/strategies/macd_triple_filter_trend.py"
    if strategy_path.exists():
        result = verify_strategy_file(strategy_path, strategy_name="macd_triple_filter_trend")
    else:
        sample_code = '''
import pandas as pd
import numpy as np
from nautilus_trader.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import InstrumentId
from app.strategy_contract import StrategyManifest, ParameterSpec, StrategyMode

class CanonicalMacdConfig(StrategyConfig):
    instrument_id: str
    bar_type: str
    fast_period: int = 12

class CanonicalMacdStrategy(Strategy):
    def __init__(self, config: CanonicalMacdConfig):
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(config.bar_type)

    def on_start(self):
        self.subscribe_bars(self.bar_type)

    def on_bar(self, bar: Bar):
        pass

    def on_stop(self):
        self.unsubscribe_bars(self.bar_type)

def calculate_indicators(df: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    df = df.copy()
    fast_p = int(parameters.get("fast_period", 12))
    df["fast_ma"] = df["close"].rolling(window=fast_p, min_periods=1).mean()
    return df

STRATEGY_MANIFEST = StrategyManifest(
    slug="canonical_macd",
    name="Canonical MACD",
    description="",
    category="trend",
    strategy_path="test:CanonicalMacdStrategy",
    config_path="test:CanonicalMacdConfig",
    parameters={"fast_period": ParameterSpec(title="Fast", type="integer", default=12)},
    timeframes=("1h",),
    primary_timeframe="1h",
    plot_config={"main_plot": {"close": {"type": "line", "color": "#fff"}, "fast_ma": {"type": "line", "color": "#f00"}}},
    mode=StrategyMode.SINGLE_INSTRUMENT,
    supports_short=True,
    requires_funding=True,
)
'''
        result = verify_strategy_source(sample_code, "canonical_macd")
    assert result.ok is True
    assert len(result.steps) == 4
    for step in result.steps:
        assert step.ok is True




def test_verify_l1_syntax_error():
    bad_syntax = """
import pandas as pd
def calculate_indicators(df, params)
    return df
"""
    result = verify_strategy_source(bad_syntax, "bad_syntax")
    assert result.ok is False
    assert result.failed_level == "L1"
    assert "语法错误" in result.summary


def test_verify_l1_missing_manifest():
    missing_manifest = """
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy

class DemoConfig(StrategyConfig):
    instrument_id: str
    bar_type: str

class DemoStrategy(Strategy):
    def on_start(self): pass
    def on_bar(self, bar): pass
    def on_stop(self): pass

def calculate_indicators(df, parameters):
    return df
"""
    result = verify_strategy_source(missing_manifest, "missing_manifest")
    assert result.ok is False
    assert result.failed_level == "L1"
    assert "STRATEGY_MANIFEST" in result.error_message


def test_verify_l2_invalid_subplots_nesting():
    bad_subplots = """
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import StrategyManifest, ParameterSpec, StrategyMode

class DemoConfig(StrategyConfig):
    instrument_id: str
    bar_type: str

class DemoStrategy(Strategy):
    def on_start(self): pass
    def on_bar(self, bar): pass
    def on_stop(self): pass

def calculate_indicators(df, parameters):
    df['atr'] = 1.0
    return df

STRATEGY_MANIFEST = StrategyManifest(
    slug="demo_strat",
    name="Demo",
    version="1.0.0",
    strategy_path="app.strategies.demo_strat:DemoStrategy",
    config_path="app.strategies.demo_strat:DemoConfig",
    parameters={},
    timeframes=("15m",),
    primary_timeframe="15m",
    plot_config={
        "main_plot": {"close": {"type": "line"}},
        # Error: Flat dict instead of nested panel title
        "subplots": {"atr": {"type": "line"}}
    },
    mode=StrategyMode.SINGLE_INSTRUMENT,
)
"""
    result = verify_strategy_source(bad_subplots, "demo_strat")
    assert result.ok is False
    assert result.failed_level == "L2"
    assert "subplots" in result.error_message


def test_verify_l3_missing_indicator_column():
    missing_col = """
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import StrategyManifest, ParameterSpec, StrategyMode

class DemoConfig(StrategyConfig):
    instrument_id: str
    bar_type: str

class DemoStrategy(Strategy):
    def on_start(self): pass
    def on_bar(self, bar): pass
    def on_stop(self): pass

def calculate_indicators(df, parameters):
    # calculate_indicators forgets to calculate fast_ma which is in main_plot
    df['slow_ma'] = df['close'] * 0.95
    return df

STRATEGY_MANIFEST = StrategyManifest(
    slug="demo_strat",
    name="Demo",
    version="1.0.0",
    strategy_path="app.strategies.demo_strat:DemoStrategy",
    config_path="app.strategies.demo_strat:DemoConfig",
    parameters={},
    timeframes=("15m",),
    primary_timeframe="15m",
    plot_config={
        "main_plot": {
            "close": {"type": "line"},
            "fast_ma": {"type": "line"},
            "slow_ma": {"type": "line"}
        },
        "subplots": {}
    },
    mode=StrategyMode.SINGLE_INSTRUMENT,
)
"""
    result = verify_strategy_source(missing_col, "demo_strat")
    assert result.ok is False
    assert result.failed_level == "L3"
    assert "fast_ma" in result.error_message


def test_verify_l3_row_count_mutation():
    mutating_len = """
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import StrategyManifest, ParameterSpec, StrategyMode

class DemoConfig(StrategyConfig):
    instrument_id: str
    bar_type: str

class DemoStrategy(Strategy):
    def on_start(self): pass
    def on_bar(self, bar): pass
    def on_stop(self): pass

def calculate_indicators(df, parameters):
    df['fast_ma'] = df['close']
    return df.dropna()  # Drops rows!

STRATEGY_MANIFEST = StrategyManifest(
    slug="demo_strat",
    name="Demo",
    version="1.0.0",
    strategy_path="app.strategies.demo_strat:DemoStrategy",
    config_path="app.strategies.demo_strat:DemoConfig",
    parameters={},
    timeframes=("15m",),
    primary_timeframe="15m",
    plot_config={"main_plot": {"close": {"type": "line"}, "fast_ma": {"type": "line"}}, "subplots": {}},
    mode=StrategyMode.SINGLE_INSTRUMENT,
)
"""
    # Note: if df has no NaNs, dropna doesn't drop, but if we drop rows:
    mutating_len2 = mutating_len.replace("return df.dropna()", "return df.iloc[10:]")
    result = verify_strategy_source(mutating_len2, "demo_strat")
    assert result.ok is False
    assert result.failed_level == "L3"
    assert "行数" in result.error_message


def test_verify_l4_missing_lifecycle_method():
    missing_on_bar = """
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import StrategyManifest, ParameterSpec, StrategyMode

class DemoConfig(StrategyConfig):
    instrument_id: str
    bar_type: str

class DemoStrategy(Strategy):
    def on_start(self): pass
    # Missing on_bar!
    def on_stop(self): pass

def calculate_indicators(df, parameters):
    df['ma'] = df['close']
    return df

STRATEGY_MANIFEST = StrategyManifest(
    slug="demo_strat",
    name="Demo",
    version="1.0.0",
    strategy_path="app.strategies.demo_strat:DemoStrategy",
    config_path="app.strategies.demo_strat:DemoConfig",
    parameters={},
    timeframes=("15m",),
    primary_timeframe="15m",
    plot_config={"main_plot": {"close": {"type": "line"}, "ma": {"type": "line"}}, "subplots": {}},
    mode=StrategyMode.SINGLE_INSTRUMENT,
)
"""
    result = verify_strategy_source(missing_on_bar, "demo_strat")
    assert result.ok is False
    assert result.failed_level == "L4"
    assert "on_bar" in result.error_message
