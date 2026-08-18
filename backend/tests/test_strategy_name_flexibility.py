from pathlib import Path
from decimal import Decimal
import pandas as pd

from app.agent.strategy_verifier import verify_strategy_source
from app.strategy_contract import (
    ParameterSpec,
    StrategyManifest,
    StrategyMode,
    sanitize_strategy_slug,
)
from app.strategy_files import _path, save_strategy_code


def test_sanitize_strategy_slug():
    assert sanitize_strategy_slug("volatility-squeeze-breakout") == "volatility_squeeze_breakout"
    assert sanitize_strategy_slug("VolatilitySqueezeBreakout") == "volatility_squeeze_breakout"
    assert sanitize_strategy_slug("volatility_squeeze_breakout.py") == "volatility_squeeze_breakout"
    assert sanitize_strategy_slug("backend/app/strategies/volatility_squeeze_breakout.py") == "volatility_squeeze_breakout"
    assert sanitize_strategy_slug("  Awesome Trend Strategy!  ") == "awesome_trend_strategy"
    assert sanitize_strategy_slug("15m_ema_cross") == "s_15m_ema_cross"
    assert sanitize_strategy_slug("") == "custom_strategy"


def test_strategy_manifest_supported_modes_tolerance():
    # Test that passing supported_modes does not crash and auto-maps mode
    manifest = StrategyManifest(
        slug="volatility-squeeze-breakout",
        supported_modes=[StrategyMode.SINGLE_INSTRUMENT],
    )
    assert manifest.slug == "volatility-squeeze-breakout"
    assert manifest.mode == StrategyMode.SINGLE_INSTRUMENT

    manifest_str_mode = StrategyManifest(
        slug="test-portfolio",
        supported_modes=["PORTFOLIO"],
    )
    assert manifest_str_mode.mode == StrategyMode.PORTFOLIO


def test_save_and_resolve_flexible_strategy_names():
    test_code = """# test code
x = 1
"""
    saved_path = save_strategy_code("volatility-squeeze-breakout", test_code)
    assert saved_path.name == "volatility_squeeze_breakout.py"

    # Query with hyphenated name
    p1 = _path("volatility-squeeze-breakout")
    assert p1.exists()
    assert p1.name == "volatility_squeeze_breakout.py"

    # Query with underscore name
    p2 = _path("volatility_squeeze_breakout")
    assert p2.exists()

    # Query with .py suffix
    p3 = _path("volatility-squeeze-breakout.py")
    assert p3.exists()

    # Cleanup
    saved_path.unlink(missing_ok=True)


def test_verify_strategy_with_supported_modes_kwarg():
    code = '''from decimal import Decimal
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import StrategyManifest, ParameterSpec, StrategyMode

class VolSqueezeConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType

class VolSqueezeStrategy(Strategy):
    def __init__(self, config: VolSqueezeConfig) -> None:
        super().__init__(config)
    def on_start(self) -> None:
        self.subscribe_bars(self.config.bar_type)
    def on_bar(self, bar) -> None:
        pass

def calculate_indicators(df: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    res = df.copy()
    res["fast_ma"] = pd.to_numeric(res["close"]).ewm(span=10).mean()
    return res

STRATEGY_MANIFEST = StrategyManifest(
    slug="volatility-squeeze-breakout",
    name="Volatility Squeeze Breakout",
    strategy_path="app.strategies.volatility_squeeze_breakout:VolSqueezeStrategy",
    config_path="app.strategies.volatility_squeeze_breakout:VolSqueezeConfig",
    parameters={},
    timeframes=("15m", "1h"),
    primary_timeframe="15m",
    plot_config={
        "main_plot": {
            "close": {"type": "line", "color": "#ffffff"},
            "fast_ma": {"type": "line", "color": "#ffaa00"},
        },
        "subplots": {}
    },
    supported_modes=[StrategyMode.SINGLE_INSTRUMENT],
)
'''
    res = verify_strategy_source(code, strategy_name="volatility-squeeze-breakout")
    assert res.ok, f"Verification failed: {res.failed_level} - {res.error_message}"
