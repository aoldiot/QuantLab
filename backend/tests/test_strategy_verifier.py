from __future__ import annotations

from pathlib import Path
import pytest
from app.agent.strategy_verifier import verify_strategy_source, verify_strategy_file


def test_valid_strategy_passes_all_4_levels():
    code = """from decimal import Decimal
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode

class TestTrendConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    trade_size: Decimal = Decimal("0.001")
    ema_period: int = 20

class TestTrendStrategy(Strategy):
    def __init__(self, config: TestTrendConfig) -> None:
        super().__init__(config)
    def on_start(self) -> None:
        self.subscribe_bars(self.config.bar_type)
    def on_bar(self, bar) -> None:
        pass

def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    dataframe["ema_20"] = pd.to_numeric(dataframe["close"]).ewm(span=parameters.get("ema_period", 20), adjust=False).mean()
    return dataframe

STRATEGY_MANIFEST = StrategyManifest(
    slug="test_trend",
    name="Test Trend",
    version="1.0.0",
    description="Valid strategy",
    category="trend",
    strategy_path="app.strategies.test_trend:TestTrendStrategy",
    config_path="app.strategies.test_trend:TestTrendConfig",
    parameters={
        "trade_size": ParameterSpec("下单数量", "number", 0.001, 0.000001, 1000),
        "ema_period": ParameterSpec("EMA周期", "integer", 20, 2, 200),
    },
    timeframes=("15m",),
    primary_timeframe="15m",
    plot_config={
        "main_plot": {
            "ema_20": {"name": "EMA 20", "type": "line", "color": "#43a5ff"},
        },
        "subplots": {},
    },
    mode=StrategyMode.SINGLE_INSTRUMENT,
)
"""
    res = verify_strategy_source(code, strategy_name="test_trend")
    assert res.ok is True
    assert len(res.steps) == 4
    assert all(s.ok for s in res.steps)


def test_future_annotations_type_hints_not_broken_by_pop():
    # BUG-1: sys.modules.pop should not break typing.get_type_hints in strategy
    code = """from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
import pandas as pd
from typing import get_type_hints
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode

@dataclass
class IndicatorParam:
    period: int

@dataclass
class StrategyContext:
    param: IndicatorParam

class AnnotatedConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType

class AnnotatedStrategy(Strategy):
    def __init__(self, config: AnnotatedConfig) -> None:
        super().__init__(config)
        # Type hint reflection on forward annotation
        hints = get_type_hints(StrategyContext)
        assert "param" in hints

    def on_bar(self, bar) -> None:
        pass

def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    return dataframe

STRATEGY_MANIFEST = StrategyManifest(
    slug="annotated",
    strategy_path="app.strategies.annotated:AnnotatedStrategy",
    config_path="app.strategies.annotated:AnnotatedConfig",
    plot_config={"main_plot": {}, "subplots": {}},
)
"""
    res = verify_strategy_source(code, strategy_name="annotated")
    assert res.ok is True


def test_strategy_path_prefix_validation():
    # BUG-2: strategy_path="demo:DemoStrategy" must fail L2
    code = """from decimal import Decimal
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode

class PrefixConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType

class PrefixStrategy(Strategy):
    def __init__(self, config: PrefixConfig) -> None:
        super().__init__(config)
    def on_bar(self, bar) -> None:
        pass

def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    return dataframe

STRATEGY_MANIFEST = StrategyManifest(
    slug="prefix_strat",
    strategy_path="prefix_strat:PrefixStrategy",  # Invalid: missing app.strategies.
    config_path="app.strategies.prefix_strat:PrefixConfig",
    plot_config={"main_plot": {}, "subplots": {}},
)
"""
    res = verify_strategy_source(code, strategy_name="prefix_strat")
    assert res.ok is False
    assert res.failed_level == "L2"
    assert "app.strategies.prefix_strat" in res.error_message


def test_parameters_dict_without_parameterspec():
    # BUG-3: raw dict in parameters must fail L2 gracefully
    code = """from decimal import Decimal
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import StrategyManifest, StrategyMode

class DictParamConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    period: int = 10

class DictParamStrategy(Strategy):
    def __init__(self, config: DictParamConfig) -> None:
        super().__init__(config)
    def on_bar(self, bar) -> None:
        pass

def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    return dataframe

STRATEGY_MANIFEST = StrategyManifest(
    slug="dict_param",
    strategy_path="app.strategies.dict_param:DictParamStrategy",
    config_path="app.strategies.dict_param:DictParamConfig",
    parameters={
        "period": {"title": "Period", "default": 10},
    },
    plot_config={"main_plot": {}, "subplots": {}},
)
"""
    res = verify_strategy_source(code, strategy_name="dict_param")
    assert res.ok is False
    assert res.failed_level == "L2"
    assert "ParameterSpec" in res.error_message


def test_missing_strategy_class_detected_in_l1():
    # BUG-4: file with only XxxStrategyConfig must fail in L1
    code = """import pandas as pd
from nautilus_trader.config import StrategyConfig
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode

class MyStrategyConfig(StrategyConfig, frozen=True):
    pass

def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    return dataframe

STRATEGY_MANIFEST = StrategyManifest(
    slug="demo",
    strategy_path="app.strategies.demo:MyStrategy",
    config_path="app.strategies.demo:MyStrategyConfig",
)
"""
    res = verify_strategy_source(code, strategy_name="demo")
    assert res.ok is False
    assert res.failed_level == "L1"
    assert "缺少继承自 Strategy 的策略类声明" in res.error_message


def test_parameter_default_out_of_bounds():
    # BUG-5: default violating minimum/maximum must fail L2
    code = """from decimal import Decimal
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode

class BoundsConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    period: int = 5

class BoundsStrategy(Strategy):
    def __init__(self, config: BoundsConfig) -> None:
        super().__init__(config)
    def on_bar(self, bar) -> None:
        pass

def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    return dataframe

STRATEGY_MANIFEST = StrategyManifest(
    slug="bounds",
    strategy_path="app.strategies.bounds:BoundsStrategy",
    config_path="app.strategies.bounds:BoundsConfig",
    parameters={
        "period": ParameterSpec("周期", "integer", default=5, minimum=10, maximum=50),
    },
    plot_config={"main_plot": {}, "subplots": {}},
)
"""
    res = verify_strategy_source(code, strategy_name="bounds")
    assert res.ok is False
    assert res.failed_level == "L2"
    assert "不能小于" in res.error_message or "10" in res.error_message


def test_main_plot_invalid_spec_format():
    # BUG-6: main_plot with non-dict spec must fail L2
    code = """from decimal import Decimal
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode

class PlotSpecConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType

class PlotSpecStrategy(Strategy):
    def __init__(self, config: PlotSpecConfig) -> None:
        super().__init__(config)
    def on_bar(self, bar) -> None:
        pass

def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    dataframe["close_val"] = 1.0
    return dataframe

STRATEGY_MANIFEST = StrategyManifest(
    slug="plot_spec",
    strategy_path="app.strategies.plot_spec:PlotSpecStrategy",
    config_path="app.strategies.plot_spec:PlotSpecConfig",
    plot_config={"main_plot": {"close_val": "line"}, "subplots": {}},
)
"""
    res = verify_strategy_source(code, strategy_name="plot_spec")
    assert res.ok is False
    assert res.failed_level == "L2"
    assert "main_plot" in res.error_message


def test_primary_timeframe_not_in_timeframes():
    # BUG-7: primary_timeframe not in timeframes must fail L2
    code = """from decimal import Decimal
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode

class TfConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType

class TfStrategy(Strategy):
    def __init__(self, config: TfConfig) -> None:
        super().__init__(config)
    def on_bar(self, bar) -> None:
        pass

def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    return dataframe

STRATEGY_MANIFEST = StrategyManifest(
    slug="tf_test",
    strategy_path="app.strategies.tf_test:TfStrategy",
    config_path="app.strategies.tf_test:TfConfig",
    timeframes=("15m", "30m"),
    primary_timeframe="1h",  # Not in timeframes!
    plot_config={"main_plot": {}, "subplots": {}},
)
"""
    res = verify_strategy_source(code, strategy_name="tf_test")
    assert res.ok is False
    assert res.failed_level == "L2"
    assert "primary_timeframe" in res.error_message


def test_duplicate_column_names_in_indicators():
    # BUG-8: calculate_indicators with duplicate columns must fail L3 gracefully
    code = """from decimal import Decimal
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode

class DupColConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType

class DupColStrategy(Strategy):
    def __init__(self, config: DupColConfig) -> None:
        super().__init__(config)
    def on_bar(self, bar) -> None:
        pass

def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    extra = pd.DataFrame({"rsi": [50.0] * len(dataframe)})
    return pd.concat([dataframe, extra, extra], axis=1)

STRATEGY_MANIFEST = StrategyManifest(
    slug="dup_col",
    strategy_path="app.strategies.dup_col:DupColStrategy",
    config_path="app.strategies.dup_col:DupColConfig",
    plot_config={"main_plot": {}, "subplots": {"RSI": {"rsi": {"type": "line"}}}},
)
"""
    res = verify_strategy_source(code, strategy_name="dup_col")
    assert res.ok is False
    assert res.failed_level == "L3"
    assert "重复列名" in res.error_message or "rsi" in res.error_message


def test_custom_event_handlers():
    # BUG-9: Strategy implementing on_quote_tick instead of on_bar
    code = """from decimal import Decimal
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode

class TickConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType

class TickStrategy(Strategy):
    def __init__(self, config: TickConfig) -> None:
        super().__init__(config)
    def on_quote_tick(self, tick) -> None:
        pass

def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    return dataframe

STRATEGY_MANIFEST = StrategyManifest(
    slug="tick_strat",
    strategy_path="app.strategies.tick_strat:TickStrategy",
    config_path="app.strategies.tick_strat:TickConfig",
    plot_config={"main_plot": {}, "subplots": {}},
)
"""
    res = verify_strategy_source(code, strategy_name="tick_strat")
    assert res.ok is True


def test_empty_source_code_fails_l1():
    res = verify_strategy_source("", strategy_name="empty")
    assert res.ok is False
    assert res.failed_level == "L1"
    assert "为空" in res.summary


def test_syntax_error_fails_l1():
    code = "def calculate_indicators(df, params):\n  return df\nSTRATEGY_MANIFEST = {"
    res = verify_strategy_source(code, strategy_name="syntax_err")
    assert res.ok is False
    assert res.failed_level == "L1"


def test_calculate_indicators_all_nan_fails_l3():
    code = """from decimal import Decimal
import pandas as pd
import numpy as np
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode

class NanConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType

class NanStrategy(Strategy):
    def __init__(self, config: NanConfig) -> None:
        super().__init__(config)
    def on_bar(self, bar) -> None:
        pass

def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    dataframe["nan_ind"] = np.nan
    return dataframe

STRATEGY_MANIFEST = StrategyManifest(
    slug="nan_test",
    strategy_path="app.strategies.nan_test:NanStrategy",
    config_path="app.strategies.nan_test:NanConfig",
    plot_config={"main_plot": {"nan_ind": {"type": "line"}}, "subplots": {}},
)
"""
    res = verify_strategy_source(code, strategy_name="nan_test")
    assert res.ok is False
    assert res.failed_level == "L3"
    assert "NaN" in res.error_message or "nan_ind" in res.error_message


def test_calculate_indicators_missing_required_column_fails_l3():
    code = """from decimal import Decimal
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode

class MissingColConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType

class MissingColStrategy(Strategy):
    def __init__(self, config: MissingColConfig) -> None:
        super().__init__(config)
    def on_bar(self, bar) -> None:
        pass

def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    # Deliberately did not compute required 'missing_ema'
    return dataframe

STRATEGY_MANIFEST = StrategyManifest(
    slug="missing_col",
    strategy_path="app.strategies.missing_col:MissingColStrategy",
    config_path="app.strategies.missing_col:MissingColConfig",
    plot_config={"main_plot": {"missing_ema": {"type": "line"}}, "subplots": {}},
)
"""
    res = verify_strategy_source(code, strategy_name="missing_col")
    assert res.ok is False
    assert res.failed_level == "L3"
    assert "missing_ema" in res.error_message


def test_portfolio_mode_strategy_verification():
    code = """from decimal import Decimal
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode

class PortConfig(StrategyConfig, frozen=True):
    instrument_ids: list[InstrumentId]
    bar_types: list[BarType]

class PortStrategy(Strategy):
    def __init__(self, config: PortConfig) -> None:
        super().__init__(config)
    def on_bar(self, bar) -> None:
        pass

def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    return dataframe

STRATEGY_MANIFEST = StrategyManifest(
    slug="port_strat",
    strategy_path="app.strategies.port_strat:PortStrategy",
    config_path="app.strategies.port_strat:PortConfig",
    mode=StrategyMode.PORTFOLIO,
    plot_config={"main_plot": {}, "subplots": {}},
)
"""
    res = verify_strategy_source(code, strategy_name="port_strat")
    assert res.ok is True

