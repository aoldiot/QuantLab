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


@pytest.mark.anyio
async def test_sync_strategy_code_ignores_fragments():
    from app.research import _sync_strategy_code_if_present
    from unittest.mock import MagicMock

    mock_proj = MagicMock()
    mock_proj.id = "proj12345"
    mock_db = MagicMock()

    # Partial fragment should NOT be synced
    fragment = """
```python
# Just a partial class
class BrokenConfig(StrategyConfig):
    period: int = 10
```
"""
    slug = await _sync_strategy_code_if_present(fragment, mock_proj, mock_db)
    assert slug is None


def test_ast_catches_nautilus_hallucinations():
    bad_code = """
from decimal import Decimal
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode

class BadConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType

class BadStrategy(Strategy):
    def __init__(self, config: BadConfig) -> None:
        super().__init__(config)
    def on_bar(self, bar) -> None:
        bal = self.portfolio.account_balance()
        flat = self.portfolio.is_net_flat(self.instrument_id)
        qty = self.instrument.round_quantity(1.0)
        self.close_position(self.instrument_id)

def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    return dataframe

STRATEGY_MANIFEST = StrategyManifest(
    slug="bad_strat",
    strategy_path="app.strategies.bad_strat:BadStrategy",
    config_path="app.strategies.bad_strat:BadConfig",
    mode=StrategyMode.SINGLE_INSTRUMENT,
    plot_config={"main_plot": {}, "subplots": {}},
)
"""
    res = verify_strategy_source(bad_code, strategy_name="bad_strat")
    assert res.ok is False
    assert res.failed_level == "L1"
    assert "account_balance" in res.error_message
    assert "is_net_flat" in res.error_message
    assert "round_quantity" in res.error_message
    assert "close_position" in res.error_message


def test_l4_simulation_catches_runtime_on_bar_crash():
    crash_code = """
from decimal import Decimal
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode

class CrashConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType

class CrashStrategy(Strategy):
    def __init__(self, config: CrashConfig) -> None:
        super().__init__(config)
        self.instrument_id = config.instrument_id
        self.bar_type = config.bar_type
    def on_start(self) -> None:
        self.subscribe_bars(self.bar_type)
    def on_bar(self, bar) -> None:
        val = 1 / 0

def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    return dataframe

STRATEGY_MANIFEST = StrategyManifest(
    slug="crash_strat",
    strategy_path="app.strategies.crash_strat:CrashStrategy",
    config_path="app.strategies.crash_strat:CrashConfig",
    mode=StrategyMode.SINGLE_INSTRUMENT,
    plot_config={"main_plot": {}, "subplots": {}},
)
"""
    res = verify_strategy_source(crash_code, strategy_name="crash_strat")
    assert res.ok is False
    assert res.failed_level == "L4"
    assert "division by zero" in res.error_message


def test_bollinger_mean_reversion_strategy_passes_verification():
    code = """from decimal import Decimal
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from app.strategy_base import QuantLabStrategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode
from app.quant.indicators import calc_standard_indicators

class BollingerMeanReversionConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    bb_period: int = 20
    bb_std: float = 2.0
    trade_size: Decimal = Decimal("0.01")

class BollingerMeanReversionStrategy(QuantLabStrategy):
    def on_bar(self, bar) -> None:
        super().on_bar(bar)
        closes = self.get_close_series()
        if len(closes) < self.config.bb_period + 5:
            return
        ma = closes.rolling(self.config.bb_period).mean().iloc[-1]
        std = closes.rolling(self.config.bb_period).std().iloc[-1]
        upper = ma + self.config.bb_std * std
        lower = ma - self.config.bb_std * std
        self.record("bb_upper", upper)
        self.record("bb_lower", lower)
        self.record("bb_mid", ma)
        if bar.close < lower and not self.is_long():
            self.buy_market(trade_size=self.config.trade_size)
        elif bar.close > upper and self.is_long():
            self.close_position()

def calculate_indicators(df: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    return calc_standard_indicators(df, parameters)

STRATEGY_MANIFEST = StrategyManifest(
    slug="bollinger_mean_reversion",
    name="Bollinger Mean Reversion",
    version="1.0.0",
    description="Bollinger mean reversion strategy",
    category="mean_reversion",
    strategy_path="app.strategies.bollinger_mean_reversion:BollingerMeanReversionStrategy",
    config_path="app.strategies.bollinger_mean_reversion:BollingerMeanReversionConfig",
    parameters={
        "bb_period": ParameterSpec(title="布林带周期", type="integer", default=20, minimum=5, maximum=100),
        "bb_std": ParameterSpec(title="布林带标准差", type="number", default=2.0, minimum=0.5, maximum=5.0),
        "trade_size": ParameterSpec(title="下单数量", type="number", default=0.01, minimum=0.0001, maximum=100.0),
    },
    timeframes=("15m", "1h"),
    primary_timeframe="1h",
    plot_config={
        "main_plot": {
            "close": {"type": "line", "color": "#ffffff"},
            "bb_upper": {"type": "line", "color": "#00aaff"},
            "bb_lower": {"type": "line", "color": "#00aaff"},
            "bb_mid": {"type": "line", "color": "#ffaa00"},
        },
        "subplots": {},
    },
    mode=StrategyMode.SINGLE_INSTRUMENT,
)
"""
    res = verify_strategy_source(code, strategy_name="bollinger_mean_reversion")
    assert res.ok is True
    assert all(step.ok for step in res.steps)


def test_extract_python_strategy_code_with_various_llm_markdown_artifacts():
    from app.agent.strategy_verifier import extract_python_strategy_code
    import ast

    # Case 1: Markdown with colon path in info string (e.g. ```python:backend/app/strategies/my_strat.py)
    llm_output_1 = """这里是修改后的代码：
```python:backend/app/strategies/multi_filter_breakout.py
from decimal import Decimal
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode

class MyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType

class MyStrategy(Strategy):
    def on_bar(self, bar):
        pass

def calculate_indicators(df, p):
    return df

STRATEGY_MANIFEST = StrategyManifest(
    slug="my_strat",
    strategy_path="app.strategies.my_strat:MyStrategy",
    config_path="app.strategies.my_strat:MyConfig",
    mode=StrategyMode.SINGLE_INSTRUMENT,
    plot_config={"main_plot": {}, "subplots": {}},
)
```
希望对您有帮助！"""
    code1 = extract_python_strategy_code(llm_output_1)
    assert not code1.startswith(":")
    assert not code1.startswith("python:")
    assert code1.startswith("from decimal import Decimal")
    ast.parse(code1)

    # Case 2: Markdown with json block before python block
    llm_output_2 = """```json
{"parameters": {"fast": 12}}
```
```python
from decimal import Decimal
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode

class MyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType

class MyStrategy(Strategy):
    def on_bar(self, bar):
        pass

def calculate_indicators(df, p):
    return df

STRATEGY_MANIFEST = StrategyManifest(
    slug="my_strat",
    strategy_path="app.strategies.my_strat:MyStrategy",
    config_path="app.strategies.my_strat:MyConfig",
    mode=StrategyMode.SINGLE_INSTRUMENT,
    plot_config={"main_plot": {}, "subplots": {}},
)
```"""
    code2 = extract_python_strategy_code(llm_output_2)
    # Case 3: Nested code fences (```python\n```python\n...)
    llm_output_3 = """```python
```python
from decimal import Decimal
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode

class MyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType

class MyStrategy(Strategy):
    def on_bar(self, bar):
        pass

def calculate_indicators(df, p):
    return df

STRATEGY_MANIFEST = StrategyManifest(
    slug="my_strat",
    strategy_path="app.strategies.my_strat:MyStrategy",
    config_path="app.strategies.my_strat:MyConfig",
    mode=StrategyMode.SINGLE_INSTRUMENT,
    plot_config={"main_plot": {}, "subplots": {}},
)
```
```"""
    code3 = extract_python_strategy_code(llm_output_3)
    assert not code3.startswith("```")
    assert code3.startswith("from decimal import Decimal")
    ast.parse(code3)

    # Case 4: Conversational text inside code block on line 1
    llm_output_4 = """```python
这是为您生成的完整量化策略代码：
from decimal import Decimal
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode

class MyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType

class MyStrategy(Strategy):
    def on_bar(self, bar):
        pass

def calculate_indicators(df, p):
    return df

STRATEGY_MANIFEST = StrategyManifest(
    slug="my_strat",
    strategy_path="app.strategies.my_strat:MyStrategy",
    config_path="app.strategies.my_strat:MyConfig",
    mode=StrategyMode.SINGLE_INSTRUMENT,
    plot_config={"main_plot": {}, "subplots": {}},
)
```"""
    code4 = extract_python_strategy_code(llm_output_4)
    assert not code4.startswith("这是")
    assert code4.startswith("from decimal import Decimal")
    ast.parse(code4)

    # Case 5: Unicode BOM and zero-width characters
    llm_output_5 = "\ufeff\u200b```python\nfrom decimal import Decimal\nimport pandas as pd\nfrom nautilus_trader.config import StrategyConfig\nfrom nautilus_trader.model.data import BarType\nfrom nautilus_trader.model.identifiers import InstrumentId\nfrom nautilus_trader.trading.strategy import Strategy\nfrom app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode\n\nclass MyConfig(StrategyConfig, frozen=True):\n    instrument_id: InstrumentId\n    bar_type: BarType\n\nclass MyStrategy(Strategy):\n    def on_bar(self, bar):\n        pass\n\ndef calculate_indicators(df, p):\n    return df\n\nSTRATEGY_MANIFEST = StrategyManifest(\n    slug=\"my_strat\",\n    strategy_path=\"app.strategies.my_strat:MyStrategy\",\n    config_path=\"app.strategies.my_strat:MyConfig\",\n    mode=StrategyMode.SINGLE_INSTRUMENT,\n    plot_config={\"main_plot\": {}, \"subplots\": {}},\n)\n```"
    code5 = extract_python_strategy_code(llm_output_5)
    assert "\ufeff" not in code5
    assert "\u200b" not in code5
    assert code5.startswith("from decimal import Decimal")
    ast.parse(code5)

    # Case 6: Unclosed fence / truncated LLM generation
    llm_output_6 = """好的，请查收策略代码：
```python
from decimal import Decimal
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode

class MyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType

class MyStrategy(Strategy):
    def on_bar(self, bar):
        pass

def calculate_indicators(df, p):
    return df

STRATEGY_MANIFEST = StrategyManifest(
    slug="my_strat",
    strategy_path="app.strategies.my_strat:MyStrategy",
    config_path="app.strategies.my_strat:MyConfig",
    mode=StrategyMode.SINGLE_INSTRUMENT,
    plot_config={"main_plot": {}, "subplots": {}},
)
"""
    code6 = extract_python_strategy_code(llm_output_6)
    assert not code6.startswith("好的")
    assert not code6.startswith("```")
    assert code6.startswith("from decimal import Decimal")
    ast.parse(code6)

    # Case 7: File header tags and comment lines
    llm_output_7 = """```python
# filepath: backend/app/strategies/my_strat.py
// File: my_strat.py
[my_strat.py]
from decimal import Decimal
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode

class MyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType

class MyStrategy(Strategy):
    def on_bar(self, bar):
        pass

def calculate_indicators(df, p):
    return df

STRATEGY_MANIFEST = StrategyManifest(
    slug="my_strat",
    strategy_path="app.strategies.my_strat:MyStrategy",
    config_path="app.strategies.my_strat:MyConfig",
    mode=StrategyMode.SINGLE_INSTRUMENT,
    plot_config={"main_plot": {}, "subplots": {}},
)
```"""
    code7 = extract_python_strategy_code(llm_output_7)
    assert not code7.startswith("# filepath")
    assert not code7.startswith("//")
    assert not code7.startswith("[")
    assert code7.startswith("from decimal import Decimal")
    ast.parse(code7)


def test_save_strategy_file_auto_sanitizes_markdown_wrapping(tmp_path, monkeypatch):
    from app.strategy_files import save_strategy_code
    import app.strategy_files as sf

    monkeypatch.setattr(sf, "STRATEGY_DIR", tmp_path / "strategies")
    monkeypatch.setattr(sf, "PERSISTENT_STRATEGY_DIR", tmp_path / "persist")

    dirty_code = """```python
from decimal import Decimal
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode

class TestStratConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType

class TestStratStrategy(Strategy):
    def on_bar(self, bar):
        pass

def calculate_indicators(df, p):
    return df

STRATEGY_MANIFEST = StrategyManifest(
    slug="test_strat",
    strategy_path="app.strategies.test_strat:TestStratStrategy",
    config_path="app.strategies.test_strat:TestStratConfig",
    mode=StrategyMode.SINGLE_INSTRUMENT,
    plot_config={"main_plot": {}, "subplots": {}},
)
```"""
    saved_path = save_strategy_code("test_strat", dirty_code)
    disk_content = saved_path.read_text(encoding="utf-8")
    assert not disk_content.startswith("```")
    assert disk_content.startswith("from decimal import Decimal")
    # Verify it compiles cleanly
    compile(disk_content, str(saved_path), "exec")


def test_invalid_nautilus_indicator_average_import_rejected():
    code = """from decimal import Decimal
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.indicators.average import ExponentialMovingAverage
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode

class MyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType

class MyStrategy(Strategy):
    def on_bar(self, bar):
        pass

STRATEGY_MANIFEST = StrategyManifest(
    slug="my_strat",
    strategy_path="app.strategies.my_strat:MyStrategy",
    config_path="app.strategies.my_strat:MyConfig",
    mode=StrategyMode.SINGLE_INSTRUMENT,
    plot_config={"main_plot": {}, "subplots": {}},
)
"""
    res = verify_strategy_source(code, strategy_name="my_strat")
    assert res.ok is False
    assert res.failed_level == "L1"
    assert "nautilus_trader.indicators.average" in res.error_message


def test_parameter_spec_name_and_list_parameters_auto_converted():
    code = """from decimal import Decimal
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from app.strategy_base import QuantLabStrategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode
from app.quant.indicators import calc_standard_indicators

class CustomConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    fast_period: int = 12

class CustomStrategy(QuantLabStrategy):
    def on_bar(self, bar):
        pass

def calculate_indicators(df, p):
    return calc_standard_indicators(df, p)

STRATEGY_MANIFEST = StrategyManifest(
    slug="custom_strat",
    strategy_path="app.strategies.custom_strat:CustomStrategy",
    config_path="app.strategies.custom_strat:CustomConfig",
    parameters=[
        ParameterSpec(name="fast_period", type="integer", default=12, minimum=2, maximum=100),
    ],
    mode=StrategyMode.SINGLE_INSTRUMENT,
    plot_config={"main_plot": {}, "subplots": {}},
)
"""
    res = verify_strategy_source(code, strategy_name="custom_strat")
    assert res.ok is True
    assert all(step.ok for step in res.steps)

