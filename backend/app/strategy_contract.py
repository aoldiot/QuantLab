from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from importlib import import_module, reload
from pathlib import Path
from typing import Any

import pandas as pd


def sanitize_strategy_slug(name: str) -> str:
    """Normalize any user/LLM provided strategy name/filename into a standard safe Python identifier slug.

    Examples:
        'volatility-squeeze-breakout' -> 'volatility_squeeze_breakout'
        'VolatilitySqueezeBreakout' -> 'volatility_squeeze_breakout'
        'volatility_squeeze_breakout.py' -> 'volatility_squeeze_breakout'
        'backend/app/strategies/volatility_squeeze_breakout.py' -> 'volatility_squeeze_breakout'
        '  Awesome Trend Strategy!  ' -> 'awesome_trend_strategy'
    """
    if not name:
        return "custom_strategy"
    clean = str(name).strip()
    if "/" in clean or "\\" in clean:
        clean = Path(clean).stem
    if clean.endswith(".py"):
        clean = clean[:-3]
    # Convert CamelCase to snake_case if applicable
    clean = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", clean)
    # Replace non-alphanumeric chars (hyphens, spaces, dots, etc.) with underscore
    clean = re.sub(r"[^a-zA-Z0-9_]", "_", clean)
    clean = clean.lower()
    # Collapse multiple consecutive underscores
    clean = re.sub(r"_+", "_", clean).strip("_")
    if not clean:
        return "custom_strategy"
    if clean[0].isdigit():
        clean = f"s_{clean}"
    return clean[:64]


class StrategyMode(StrEnum):
    SINGLE_INSTRUMENT = "SINGLE_INSTRUMENT"
    PORTFOLIO = "PORTFOLIO"
    SINGLE = "SINGLE_INSTRUMENT"
    BOTH = "SINGLE_INSTRUMENT"
    BACKTEST_AND_LIVE = "SINGLE_INSTRUMENT"
    LIVE = "SINGLE_INSTRUMENT"
    BACKTEST = "SINGLE_INSTRUMENT"


@dataclass
class ParameterSpec:
    title: str = ""
    type: str = "number"
    default: Any = 0
    minimum: float | None = None
    maximum: float | None = None
    description: str = ""
    step: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    min: float | None = None
    max: float | None = None
    name: str = ""
    options: list[Any] | None = None

    def __post_init__(self):
        if not self.title and self.name:
            self.title = self.name
        if self.minimum is None:
            if self.min_value is not None:
                self.minimum = self.min_value
            elif self.min is not None:
                self.minimum = self.min
        if self.maximum is None:
            if self.max_value is not None:
                self.maximum = self.max_value
            elif self.max is not None:
                self.maximum = self.max

    def to_schema(self) -> dict[str, Any]:
        p_type = self.type
        if isinstance(p_type, type):
            p_type = p_type.__name__

        def _clean(val: Any) -> Any:
            from decimal import Decimal
            if isinstance(val, Decimal):
                return float(val)
            if isinstance(val, type):
                return val.__name__
            return val

        return {
            k: _clean(v)
            for k, v in {
                "title": self.title,
                "type": p_type,
                "default": self.default,
                "min": self.minimum,
                "max": self.maximum,
                "step": self.step,
                "description": self.description,
            }.items()
            if v is not None
        }


@dataclass(frozen=True)
class StrategyManifest:
    slug: str = ""
    name: str = ""
    description: str = ""
    version: str = "1.0.0"
    category: str = "trend"
    strategy_path: str = ""
    config_path: str = ""
    parameters: dict[str, ParameterSpec] = field(default_factory=dict)
    timeframes: tuple[str, ...] = ("1m", "5m", "15m", "1h", "4h", "1d")
    primary_timeframe: str = "15m"
    plot_config: dict[str, Any] = field(
        default_factory=lambda: {
            "main_plot": {"close": {"type": "line", "color": "#ffffff"}},
            "subplots": {},
        }
    )
    mode: StrategyMode = StrategyMode.SINGLE_INSTRUMENT
    supports_short: bool = True
    requires_funding: bool = True
    strategy_id: str = ""
    id: str = ""
    supported_modes: Any = None

    def __post_init__(self):
        if self.supported_modes is not None:
            raw_modes = self.supported_modes if isinstance(self.supported_modes, (list, tuple, set)) else [self.supported_modes]
            if raw_modes:
                first = next(iter(raw_modes))
                if isinstance(first, StrategyMode):
                    object.__setattr__(self, "mode", first)
                elif isinstance(first, str):
                    try:
                        object.__setattr__(self, "mode", StrategyMode(first.upper()))
                    except Exception:
                        pass

        # Normalize parameters if passed as list or tuple of ParameterSpec
        if isinstance(self.parameters, (list, tuple)):
            converted_params: dict[str, Any] = {}
            for i, p in enumerate(self.parameters):
                if isinstance(p, ParameterSpec):
                    k = getattr(p, "name", "") or getattr(p, "title", "") or f"param_{i}"
                    converted_params[k] = p
                else:
                    converted_params[f"param_{i}"] = p
            object.__setattr__(self, "parameters", converted_params)

        raw_slug = self.slug or self.strategy_id or self.id or "strategy"
        actual_slug = sanitize_strategy_slug(raw_slug)
        if not self.slug:
            object.__setattr__(self, "slug", actual_slug)
        if not self.name:
            object.__setattr__(self, "name", actual_slug.replace("_", " ").title())
        if not self.strategy_path:
            strat_class = "".join(w.capitalize() for w in actual_slug.split("_")) + "Strategy"
            object.__setattr__(self, "strategy_path", f"app.strategies.{actual_slug}:{strat_class}")
        if not self.config_path:
            config_class = "".join(w.capitalize() for w in actual_slug.split("_")) + "Config"
            object.__setattr__(self, "config_path", f"app.strategies.{actual_slug}:{config_class}")
        if not self.plot_config:
            object.__setattr__(
                self,
                "plot_config",
                {"main_plot": {"close": {"type": "line", "color": "#ffffff"}}, "subplots": {}},
            )

    def parameter_schema(self) -> dict[str, dict[str, Any]]:
        return {name: spec.to_schema() for name, spec in self.parameters.items()}

    def data_requirements(self) -> dict[str, Any]:
        return {
            "timeframes": list(self.timeframes),
            "primary_timeframe": self.primary_timeframe,
            "multi_symbol": True,
            "funding": self.requires_funding,
            "supports_short": self.supports_short,
            "config_path": self.config_path,
            "mode": self.mode.value,
            "plot_config": self.plot_config,
        }


def load_manifest(module_path: str) -> StrategyManifest:
    clean_module = module_path.partition(":")[0]
    module = reload(import_module(clean_module))
    manifest = getattr(module, "STRATEGY_MANIFEST", None)
    if not isinstance(manifest, StrategyManifest):
        raise TypeError(f"{clean_module} 必须导出 StrategyManifest 类型的 STRATEGY_MANIFEST")
    validate_plot_contract(module, manifest)
    return manifest


def validate_plot_contract(module: Any, manifest: StrategyManifest) -> None:
    config = manifest.plot_config or {"main_plot": {}, "subplots": {}}
    if not isinstance(config, dict):
        raise TypeError("plot_config 必须是字典类型")
    calculate = getattr(module, "calculate_indicators", None)
    if calculate is not None and not callable(calculate):
        raise TypeError(f"{module.__name__} calculate_indicators 必须是可调用的函数")
    supported = {"line", "histogram", "bar", "area", "baseline"}
    series = list(config.get("main_plot", {}).items()) if isinstance(config.get("main_plot"), dict) else []
    subplots = config.get("subplots", {})
    if isinstance(subplots, dict):
        for pane, pane_series in subplots.items():
            if isinstance(pane_series, dict):
                series.extend(pane_series.items())
    for column, spec in series:
        if not isinstance(spec, dict):
            raise TypeError(f"指标 {column} 的配置必须是字典类型，例如 {{'type': 'line', 'color': '#ffffff'}}")
        if spec.get("type", "line") not in supported:
            raise ValueError(f"指标 {column} 使用了不支持的图形类型: {spec.get('type')}")


def calculate_plot_indicators(module_path: str, frame: pd.DataFrame, parameters: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
    module = reload(import_module(module_path))
    manifest = getattr(module, "STRATEGY_MANIFEST")
    validate_plot_contract(module, manifest)

    calculate_fn = getattr(module, "calculate_indicators", None)
    if calculate_fn is not None and callable(calculate_fn):
        result = calculate_fn(frame.copy(), parameters.copy())
    else:
        # Auto-derive indicators using QuantLab standard vectorized calculation
        from .quant.indicators import calc_standard_indicators
        result = calc_standard_indicators(frame.copy(), parameters.copy())

    if not isinstance(result, pd.DataFrame) or len(result) != len(frame):
        raise ValueError("calculate_indicators 必须返回行数不变的 pandas DataFrame")

    required = set(manifest.plot_config.get("main_plot", {}))
    required.update(column for pane in manifest.plot_config.get("subplots", {}).values() for column in pane)
    missing = required - set(result.columns)
    for col in missing:
        result[col] = 0.0

    for column in required:
        result[column] = pd.to_numeric(result[column], errors="coerce").fillna(0.0)
    return result, manifest.plot_config



def validate_parameters(manifest: StrategyManifest, values: dict[str, Any]) -> dict[str, Any]:
    unknown = set(values) - set(manifest.parameters)
    if unknown:
        raise ValueError(f"未知策略参数: {', '.join(sorted(unknown))}")
    resolved: dict[str, Any] = {}
    for name, spec in manifest.parameters.items():
        value = values.get(name, spec.default)
        if spec.type == "integer":
            value = int(value)
        elif spec.type == "number":
            value = float(value)
        elif spec.type == "boolean":
            value = bool(value)
        if spec.minimum is not None and value < spec.minimum:
            raise ValueError(f"{spec.title} 不能小于 {spec.minimum}")
        if spec.maximum is not None and value > spec.maximum:
            raise ValueError(f"{spec.title} 不能大于 {spec.maximum}")
        resolved[name] = value
    return resolved
