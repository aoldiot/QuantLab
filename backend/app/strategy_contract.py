from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from importlib import import_module, reload
from typing import Any

import pandas as pd


class StrategyMode(StrEnum):
    SINGLE_INSTRUMENT = "SINGLE_INSTRUMENT"
    PORTFOLIO = "PORTFOLIO"


@dataclass(frozen=True)
class ParameterSpec:
    title: str
    type: str
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    description: str = ""

    def to_schema(self) -> dict[str, Any]:
        data = asdict(self)
        data["min"] = data.pop("minimum")
        data["max"] = data.pop("maximum")
        return {k: v for k, v in data.items() if v is not None}


@dataclass(frozen=True)
class StrategyManifest:
    slug: str
    name: str
    version: str
    description: str
    category: str
    strategy_path: str
    config_path: str
    parameters: dict[str, ParameterSpec]
    timeframes: tuple[str, ...]
    primary_timeframe: str
    plot_config: dict[str, Any]
    mode: StrategyMode = StrategyMode.SINGLE_INSTRUMENT
    supports_short: bool = True
    requires_funding: bool = False

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
    module = reload(import_module(module_path))
    manifest = getattr(module, "STRATEGY_MANIFEST", None)
    if not isinstance(manifest, StrategyManifest):
        raise TypeError(f"{module_path} 必须导出 StrategyManifest 类型的 STRATEGY_MANIFEST")
    validate_plot_contract(module, manifest)
    return manifest


def validate_plot_contract(module: Any, manifest: StrategyManifest) -> None:
    config = manifest.plot_config
    if not isinstance(config, dict) or not isinstance(config.get("main_plot", {}), dict) or not isinstance(config.get("subplots", {}), dict):
        raise TypeError("plot_config 必须包含字典类型的 main_plot 和 subplots")
    calculate = getattr(module, "calculate_indicators", None)
    if not callable(calculate):
        raise TypeError(f"{module.__name__} 必须导出 calculate_indicators(dataframe, parameters)")
    supported = {"line", "histogram", "area", "baseline"}
    series = list(config.get("main_plot", {}).items())
    for pane, pane_series in config.get("subplots", {}).items():
        if not isinstance(pane, str) or not isinstance(pane_series, dict):
            raise TypeError("plot_config.subplots 必须是面板名称到序列配置的映射")
        series.extend(pane_series.items())
    if not series:
        raise ValueError("plot_config 至少需要声明一个指标序列")
    for column, spec in series:
        if not isinstance(column, str) or not isinstance(spec, dict):
            raise TypeError("指标列名必须是字符串，序列配置必须是字典")
        if spec.get("type", "line") not in supported:
            raise ValueError(f"指标 {column} 使用了不支持的图形类型: {spec.get('type')}")


def calculate_plot_indicators(module_path: str, frame: pd.DataFrame, parameters: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
    module = reload(import_module(module_path))
    manifest = getattr(module, "STRATEGY_MANIFEST")
    validate_plot_contract(module, manifest)
    result = module.calculate_indicators(frame.copy(), parameters.copy())
    if not isinstance(result, pd.DataFrame) or len(result) != len(frame):
        raise ValueError("calculate_indicators 必须返回行数不变的 pandas DataFrame")
    required = set(manifest.plot_config.get("main_plot", {}))
    required.update(column for pane in manifest.plot_config.get("subplots", {}).values() for column in pane)
    missing = required - set(result.columns)
    if missing:
        raise ValueError(f"plot_config 引用的指标列不存在: {', '.join(sorted(missing))}")
    for column in required:
        result[column] = pd.to_numeric(result[column], errors="coerce")
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
