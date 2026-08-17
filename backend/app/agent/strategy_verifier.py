from __future__ import annotations

import ast
import importlib.util
import logging
import py_compile
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy

from ..strategy_contract import StrategyManifest, StrategyMode, validate_plot_contract

logger = logging.getLogger(__name__)


@dataclass
class VerificationStepResult:
    level: str  # "L1", "L2", "L3", "L4"
    name: str
    ok: bool
    message: str = "OK"
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerificationResult:
    ok: bool
    summary: str
    steps: list[VerificationStepResult] = field(default_factory=list)
    failed_level: str | None = None
    error_message: str | None = None
    suggestion: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "failed_level": self.failed_level,
            "error_message": self.error_message,
            "suggestion": self.suggestion,
            "steps": [
                {
                    "level": s.level,
                    "name": s.name,
                    "ok": s.ok,
                    "message": s.message,
                    "details": s.details,
                }
                for s in self.steps
            ],
        }


def _create_sample_ohlcv(rows: int = 200) -> pd.DataFrame:
    """Generate a realistic synthetic OHLCV DataFrame for testing indicator calculation."""
    np.random.seed(42)
    base_price = 50000.0
    returns = np.random.normal(0.0002, 0.005, size=rows)
    close_prices = base_price * np.cumprod(1 + returns)

    high_noise = np.abs(np.random.normal(0, 0.003, size=rows))
    low_noise = np.abs(np.random.normal(0, 0.003, size=rows))
    high_prices = np.maximum(close_prices * (1 + high_noise), close_prices)
    low_prices = np.minimum(close_prices * (1 - low_noise), close_prices)

    open_prices = np.roll(close_prices, 1)
    open_prices[0] = base_price

    # Ensure high >= max(open, close) and low <= min(open, close)
    high_prices = np.maximum(high_prices, np.maximum(open_prices, close_prices))
    low_prices = np.minimum(low_prices, np.minimum(open_prices, close_prices))

    volume = np.random.uniform(10.0, 500.0, size=rows)

    dates = pd.date_range("2024-01-01", periods=rows, freq="15min", tz="UTC")

    return pd.DataFrame(
        {
            "timestamp": dates,
            "open": open_prices,
            "high": high_prices,
            "low": low_prices,
            "close": close_prices,
            "volume": volume,
        }
    )


def verify_strategy_source(source_code: str, strategy_name: str = "temp_strategy") -> VerificationResult:
    """Verify strategy from source code string without writing to final destination."""
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".py", encoding="utf-8", delete=False) as tf:
        tf.write(source_code)
        temp_path = Path(tf.name)

    try:
        return verify_strategy_file(temp_path, strategy_name=strategy_name)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


def verify_strategy_file(file_path: Path, strategy_name: str | None = None) -> VerificationResult:
    """Execute 4-level pre-flight verification on a strategy Python file.

    Levels:
    L1: AST static syntax and required structural declarations
    L2: Dynamic import, StrategyManifest validation, StrategyConfig schema validation
    L3: Vectorized calculate_indicators sandbox execution with sample OHLCV DataFrame
    L4: NautilusTrader strategy instantiation and contract conformance check
    """
    file_path = file_path.resolve()
    if not file_path.exists():
        return VerificationResult(
            ok=False,
            summary=f"策略文件不存在: {file_path}",
            failed_level="L1",
            error_message=f"文件不存在: {file_path}",
            suggestion="请检查策略文件生成路径是否正确。",
        )

    strategy_slug = strategy_name or file_path.stem
    steps: list[VerificationStepResult] = []

    # =========================================================================
    # Level 1: AST static syntax and essential declarations
    # =========================================================================
    source_text = file_path.read_text(encoding="utf-8")
    if not source_text.strip():
        return VerificationResult(
            ok=False,
            summary="策略文件为空",
            failed_level="L1",
            error_message="策略文件为空内容",
            suggestion="请检查代码生成过程是否被提前截断。",
        )

    try:
        py_compile.compile(str(file_path), doraise=True)
    except Exception as exc:
        step = VerificationStepResult(
            level="L1",
            name="Python 语法编译",
            ok=False,
            message=f"Python 语法错误: {exc}",
        )
        steps.append(step)
        return VerificationResult(
            ok=False,
            summary="Python 语法错误 (L1 静态语法失败)",
            steps=steps,
            failed_level="L1",
            error_message=str(exc),
            suggestion="请检查 Python 代码缩进、括号匹配与语法合规性。",
        )

    try:
        tree = ast.parse(source_text, filename=str(file_path))
    except Exception as exc:
        step = VerificationStepResult(
            level="L1",
            name="AST 解析",
            ok=False,
            message=f"AST 解析失败: {exc}",
        )
        steps.append(step)
        return VerificationResult(
            ok=False,
            summary="AST 解析失败 (L1 静态结构失败)",
            steps=steps,
            failed_level="L1",
            error_message=str(exc),
            suggestion="代码存在无法解析的 AST 结构，请修复代码语法。",
        )

    assignments = {
        target.id
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }
    functions = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}

    l1_errors = []
    if "STRATEGY_MANIFEST" not in assignments:
        l1_errors.append("缺少 STRATEGY_MANIFEST 对象定义")
    if "calculate_indicators" not in functions:
        l1_errors.append("缺少 calculate_indicators(df, parameters) 函数定义")
    if not any(c.endswith("Config") or "Config" in c for c in classes):
        l1_errors.append("缺少继承自 StrategyConfig 的配置类声明")
    if not any(c.endswith("Strategy") or "Strategy" in c for c in classes):
        l1_errors.append("缺少继承自 Strategy 的策略类声明")

    if l1_errors:
        step = VerificationStepResult(
            level="L1",
            name="核心导出结构检查",
            ok=False,
            message="；".join(l1_errors),
        )
        steps.append(step)
        return VerificationResult(
            ok=False,
            summary="缺少核心导出结构 (L1 结构检查失败)",
            steps=steps,
            failed_level="L1",
            error_message="；".join(l1_errors),
            suggestion="策略代码必须导出 StrategyConfig 子类、Strategy 子类、calculate_indicators 函数和 STRATEGY_MANIFEST 对象。",
        )

    steps.append(
        VerificationStepResult(
            level="L1",
            name="AST 静态语法与核心导出结构",
            ok=True,
            message="Python 语法与四大核心导出声明校验通过",
        )
    )

    # =========================================================================
    # Level 2: Dynamic module loading, Manifest & Parameter contracts
    # =========================================================================
    module_name = f"_quantlab_verify_{strategy_slug}_{file_path.stat().st_mtime_ns}"
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None or spec.loader is None:
        step = VerificationStepResult(
            level="L2",
            name="动态模块加载",
            ok=False,
            message="无法创建模块规范",
        )
        steps.append(step)
        return VerificationResult(
            ok=False,
            summary="动态加载模块失败 (L2 契约校验失败)",
            steps=steps,
            failed_level="L2",
            error_message="无法创建 importlib 模块规范",
            suggestion="请检查模块命名与文件路径。",
        )

    try:
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
    except Exception as exc:
        step = VerificationStepResult(
            level="L2",
            name="模块执行加载",
            ok=False,
            message=f"执行模块发生异常: {exc}",
        )
        steps.append(step)
        return VerificationResult(
            ok=False,
            summary="模块执行加载失败 (L2 契约校验失败)",
            steps=steps,
            failed_level="L2",
            error_message=f"模块执行加载异常: {exc}",
            suggestion="检查模块顶级代码是否存在未捕获异常或错误的全局初始化逻辑。",
        )
    finally:
        sys.modules.pop(module_name, None)

    manifest = getattr(mod, "STRATEGY_MANIFEST", None)
    if not isinstance(manifest, StrategyManifest):
        step = VerificationStepResult(
            level="L2",
            name="STRATEGY_MANIFEST 契约",
            ok=False,
            message="STRATEGY_MANIFEST 必须是 StrategyManifest 的实例",
        )
        steps.append(step)
        return VerificationResult(
            ok=False,
            summary="STRATEGY_MANIFEST 类型非法 (L2 契约校验失败)",
            steps=steps,
            failed_level="L2",
            error_message="STRATEGY_MANIFEST 不是 app.strategy_contract.StrategyManifest 实例",
            suggestion="确保从 app.strategy_contract 导入 StrategyManifest 并正确实例化。",
        )

    # Validate parameters in manifest
    default_parameters: dict[str, Any] = {}
    for param_name, param_spec in manifest.parameters.items():
        if param_spec.default is None:
            step = VerificationStepResult(
                level="L2",
                name="参数默认值",
                ok=False,
                message=f"参数 {param_name} 未设置 default 默认值",
            )
            steps.append(step)
            return VerificationResult(
                ok=False,
                summary=f"参数 {param_name} 缺少默认值",
                steps=steps,
                failed_level="L2",
                error_message=f"ParameterSpec {param_name} default 不能为空",
                suggestion="为每个 ParameterSpec 显式指定合法的 default 默认值。",
            )
        default_parameters[param_name] = param_spec.default

    # Validate plot_config
    plot_cfg = manifest.plot_config or {}
    if not isinstance(plot_cfg, dict):
        step = VerificationStepResult(
            level="L2",
            name="plot_config 格式",
            ok=False,
            message="plot_config 必须是字典",
        )
        steps.append(step)
        return VerificationResult(
            ok=False,
            summary="plot_config 格式错误",
            steps=steps,
            failed_level="L2",
            error_message="plot_config 不是字典",
            suggestion="遵循 plot_config={'main_plot': {...}, 'subplots': {...}} 规范。",
        )

    main_plot = plot_cfg.get("main_plot", {})
    if not isinstance(main_plot, dict):
        step = VerificationStepResult(
            level="L2",
            name="plot_config.main_plot 格式",
            ok=False,
            message="main_plot 必须是字典",
        )
        steps.append(step)
        return VerificationResult(
            ok=False,
            summary="main_plot 格式错误",
            steps=steps,
            failed_level="L2",
            error_message="main_plot 必须是字典",
            suggestion="main_plot 中每个指标映射为 {'type': 'line', 'color': '#hex'}。",
        )

    subplots = plot_cfg.get("subplots", {})
    if not isinstance(subplots, dict):
        step = VerificationStepResult(
            level="L2",
            name="plot_config.subplots 格式",
            ok=False,
            message="subplots 必须是两层嵌套字典",
        )
        steps.append(step)
        return VerificationResult(
            ok=False,
            summary="subplots 格式错误",
            steps=steps,
            failed_level="L2",
            error_message="subplots 必须是两层嵌套字典",
            suggestion="subplots 必须按 subplots={'PanelTitle': {'indicator_col': {'type': 'line', ...}}} 组织。",
        )

    # Check for flat subplots error (common LLM hallucination)
    for pane_name, pane_val in subplots.items():
        if not isinstance(pane_val, dict):
            step = VerificationStepResult(
                level="L2",
                name="subplots 嵌套层级",
                ok=False,
                message=f"subplots['{pane_name}'] 不是嵌套字典。正确格式为 subplots={{'{pane_name}': {{'{pane_name}': {{'type': 'line'}}}}}}",
            )
            steps.append(step)
            return VerificationResult(
                ok=False,
                summary="subplots 缺少面板外层字典 (L2 契约校验失败)",
                steps=steps,
                failed_level="L2",
                error_message=f"subplots['{pane_name}'] 不是指标字典。LLM 常见错误是漏掉了面板名称外层。",
                suggestion="副图指标必须是两层嵌套字典：第一层为面板标题 (如 'ATR')，第二层为 DataFrame 指标列名 (如 'atr')。",
            )
        # Check that inside each pane, each item is an indicator spec dict
        for ind_name, ind_spec in pane_val.items():
            if not isinstance(ind_spec, dict):
                step = VerificationStepResult(
                    level="L2",
                    name="subplots 嵌套层级",
                    ok=False,
                    message=f"subplots['{pane_name}']['{ind_name}'] 的值不是指标配置字典。副图必须是两层嵌套字典。",
                )
                steps.append(step)
                return VerificationResult(
                    ok=False,
                    summary="subplots 缺少面板外层字典 (L2 契约校验失败)",
                    steps=steps,
                    failed_level="L2",
                    error_message=f"subplots['{pane_name}'] 不是嵌套面板字典（内部包含非字典值 '{ind_name}': {ind_spec}）。",
                    suggestion="副图指标必须是两层嵌套字典：第一层为面板标题 (如 'ATR')，第二层为 DataFrame 指标列名 (如 'atr')。",
                )

    try:
        validate_plot_contract(mod, manifest)
    except Exception as exc:
        step = VerificationStepResult(
            level="L2",
            name="plot_config 契约校验",
            ok=False,
            message=f"plot_config 契约未通过: {exc}",
        )
        steps.append(step)
        return VerificationResult(
            ok=False,
            summary="plot_config 契约校验失败 (L2 契约校验失败)",
            steps=steps,
            failed_level="L2",
            error_message=str(exc),
            suggestion="请检查 plot_config 声明的指标图形类型是否为 line / histogram / bar / area / baseline 等合法类型。",
        )

    # Resolve Strategy and Config classes
    strategy_cls = None
    config_cls = None

    # First preference: check manifest config_path / strategy_path
    if manifest.config_path and ":" in manifest.config_path:
        c_name = manifest.config_path.split(":", 1)[1]
        candidate = getattr(mod, c_name, None)
        if isinstance(candidate, type) and issubclass(candidate, StrategyConfig) and candidate is not StrategyConfig:
            config_cls = candidate

    if manifest.strategy_path and ":" in manifest.strategy_path:
        s_name = manifest.strategy_path.split(":", 1)[1]
        candidate = getattr(mod, s_name, None)
        if isinstance(candidate, type) and issubclass(candidate, Strategy) and candidate is not Strategy:
            strategy_cls = candidate

    # Second preference: scan module __dict__
    if not config_cls:
        for attr_name, attr in mod.__dict__.items():
            if isinstance(attr, type) and issubclass(attr, StrategyConfig) and attr is not StrategyConfig:
                config_cls = attr
                break

    if not strategy_cls:
        for attr_name, attr in mod.__dict__.items():
            if isinstance(attr, type) and issubclass(attr, Strategy) and attr is not Strategy:
                strategy_cls = attr
                break

    if not config_cls:
        step = VerificationStepResult(
            level="L2",
            name="StrategyConfig 类解析",
            ok=False,
            message="未能定位到 StrategyConfig 配置类",
        )
        steps.append(step)
        return VerificationResult(
            ok=False,
            summary="未能定位配置类 (L2 契约校验失败)",
            steps=steps,
            failed_level="L2",
            error_message="未找到继承自 StrategyConfig 的自定义配置类定义",
            suggestion="确保定义了继承自 StrategyConfig 的配置类并在 manifest 中正确配置 config_path。",
        )

    # Try instantiating StrategyConfig with default parameters
    test_config_kwargs = {
        **default_parameters,
    }
    if manifest.mode == StrategyMode.PORTFOLIO:
        test_config_kwargs["instrument_ids"] = ["BTCUSDT-PERP.BINANCE"]
        test_config_kwargs["bar_types"] = ["BTCUSDT-PERP.BINANCE-15-MINUTE-LAST-EXTERNAL"]
    else:
        test_config_kwargs["instrument_id"] = "BTCUSDT-PERP.BINANCE"
        test_config_kwargs["bar_type"] = "BTCUSDT-PERP.BINANCE-15-MINUTE-LAST-EXTERNAL"

    try:
        instantiated_config = config_cls(**test_config_kwargs)
    except Exception as exc:
        step = VerificationStepResult(
            level="L2",
            name="StrategyConfig 实例化",
            ok=False,
            message=f"使用默认参数实例化 StrategyConfig 失败: {exc}",
        )
        steps.append(step)
        return VerificationResult(
            ok=False,
            summary="StrategyConfig 实例化失败 (L2 契约校验失败)",
            steps=steps,
            failed_level="L2",
            error_message=str(exc),
            suggestion="检查 StrategyConfig 中的字段声明是否与 ParameterSpec 和默认值类型一致。",
        )

    steps.append(
        VerificationStepResult(
            level="L2",
            name="动态模块加载与契约校验",
            ok=True,
            message="StrategyManifest、参数规范、plot_config 结构与 StrategyConfig 实例化完全合规",
        )
    )

    # =========================================================================
    # Level 3: Vectorized calculate_indicators sandbox execution
    # =========================================================================
    calculate_indicators_fn = getattr(mod, "calculate_indicators", None)
    if not callable(calculate_indicators_fn):
        step = VerificationStepResult(
            level="L3",
            name="指标计算函数调用",
            ok=False,
            message="calculate_indicators 不是可调用的函数",
        )
        steps.append(step)
        return VerificationResult(
            ok=False,
            summary="缺少可调用的 calculate_indicators 函数",
            steps=steps,
            failed_level="L3",
            error_message="calculate_indicators 不是函数",
            suggestion="实现 calculate_indicators(df: pd.DataFrame, parameters: dict) -> pd.DataFrame 函数。",
        )

    sample_df = _create_sample_ohlcv(rows=200)
    try:
        calculated_df = calculate_indicators_fn(sample_df.copy(), default_parameters)
    except Exception as exc:
        step = VerificationStepResult(
            level="L3",
            name="指标向量化计算沙盒运行",
            ok=False,
            message=f"运行 calculate_indicators 发生异常: {exc}",
        )
        steps.append(step)
        return VerificationResult(
            ok=False,
            summary="指标计算沙盒运行异常 (L3 指标计算失败)",
            steps=steps,
            failed_level="L3",
            error_message=f"calculate_indicators 执行抛出异常: {exc}",
            suggestion="检查指标公式（如 rolling, ewm, np.where 等）是否存在空值处理不当、索引越界或除以零错误。",
        )

    if not isinstance(calculated_df, pd.DataFrame):
        step = VerificationStepResult(
            level="L3",
            name="指标计算返回值类型",
            ok=False,
            message=f"calculate_indicators 返回类型必须为 pd.DataFrame，当前为 {type(calculated_df)}",
        )
        steps.append(step)
        return VerificationResult(
            ok=False,
            summary="指标计算返回值不是 DataFrame",
            steps=steps,
            failed_level="L3",
            error_message=f"返回值类型错误: {type(calculated_df)}",
            suggestion="确保 calculate_indicators 返回 pandas DataFrame。",
        )

    if len(calculated_df) != len(sample_df):
        step = VerificationStepResult(
            level="L3",
            name="指标计算行数保持",
            ok=False,
            message=f"calculate_indicators 改变了 DataFrame 行数: 输入 {len(sample_df)} 行，返回 {len(calculated_df)} 行",
        )
        steps.append(step)
        return VerificationResult(
            ok=False,
            summary="指标计算改变了 DataFrame 行数 (L3 指标计算失败)",
            steps=steps,
            failed_level="L3",
            error_message=f"DataFrame 行数不一致: 输入 {len(sample_df)} 行，返回 {len(calculated_df)} 行",
            suggestion="严禁在 calculate_indicators 中执行 dropna() 或裁剪操作，必须保持原始行数不变。",
        )

    # Check required columns from plot_config
    required_cols: set[str] = set()
    if isinstance(main_plot, dict):
        required_cols.update(main_plot.keys())
    if isinstance(subplots, dict):
        for pane_name, pane_series in subplots.items():
            if isinstance(pane_series, dict):
                required_cols.update(pane_series.keys())

    missing_cols = required_cols - set(calculated_df.columns)
    if missing_cols:
        step = VerificationStepResult(
            level="L3",
            name="plot_config 指标列覆盖检查",
            ok=False,
            message=f"calculate_indicators 未生成 plot_config 中声明的指标列: {', '.join(sorted(missing_cols))}",
        )
        steps.append(step)
        return VerificationResult(
            ok=False,
            summary=f"指标列缺失: {', '.join(sorted(missing_cols))} (L3 图表契约失败)",
            steps=steps,
            failed_level="L3",
            error_message=f"plot_config 引用的指标列在 calculate_indicators 返回的 DataFrame 中不存在: {sorted(missing_cols)}",
            suggestion=f"请在 calculate_indicators 中计算并赋值以下列: {sorted(missing_cols)}，或修正 plot_config 中的列名。",
        )

    # Check for all-NaN columns or Inf values in required columns
    all_nan_cols = []
    inf_cols = []
    for col in required_cols:
        if col in ("open", "high", "low", "close", "volume"):
            continue
        series = calculated_df[col]
        # Ignore initial warmup, check the latter half of data
        latter_half = series.iloc[len(series) // 2 :]
        if latter_half.isna().all():
            all_nan_cols.append(col)
        if np.isinf(pd.to_numeric(series, errors="coerce")).any():
            inf_cols.append(col)

    if all_nan_cols:
        step = VerificationStepResult(
            level="L3",
            name="指标数值有效性 (NaN 检查)",
            ok=False,
            message=f"指标列在充分预热后全为 NaN: {', '.join(all_nan_cols)}",
        )
        steps.append(step)
        return VerificationResult(
            ok=False,
            summary=f"指标全为 NaN: {', '.join(all_nan_cols)} (L3 计算结果异常)",
            steps=steps,
            failed_level="L3",
            error_message=f"以下指标在 100 根 Bar 后仍全为 NaN: {all_nan_cols}",
            suggestion="检查指标公式中是否存在无效的 rolling/ewm 参数或错误的分母计算。",
        )

    if inf_cols:
        step = VerificationStepResult(
            level="L3",
            name="指标数值有效性 (Inf 检查)",
            ok=False,
            message=f"指标列包含无穷大 (Inf/-Inf): {', '.join(inf_cols)}",
        )
        steps.append(step)
        return VerificationResult(
            ok=False,
            summary=f"指标包含无穷大 Inf: {', '.join(inf_cols)} (L3 计算结果异常)",
            steps=steps,
            failed_level="L3",
            error_message=f"以下指标包含 Inf/-Inf 值: {inf_cols}",
            suggestion="检查指标计算中是否存在除以 0 的情况，使用 np.where 或 clip 保护分母。",
        )

    steps.append(
        VerificationStepResult(
            level="L3",
            name="向量化指标计算与图表契约",
            ok=True,
            message=f"200 根虚拟 Bar 指标计算成功，覆盖全部 {len(required_cols)} 个图表指标列，数值分布正常",
        )
    )

    # =========================================================================
    # Level 4: NautilusTrader Strategy Conformance & Initialization Simulation
    # =========================================================================
    if not strategy_cls:
        step = VerificationStepResult(
            level="L4",
            name="Strategy 类解析",
            ok=False,
            message="未能定位到 Strategy 策略类",
        )
        steps.append(step)
        return VerificationResult(
            ok=False,
            summary="未能定位 Strategy 策略类 (L4 运行契约失败)",
            steps=steps,
            failed_level="L4",
            error_message="未找到继承自 Strategy 的自定义策略类定义",
            suggestion="确保定义了继承自 nautilus_trader.trading.strategy.Strategy 的策略类。",
        )

    try:
        strategy_instance = strategy_cls(instantiated_config)
    except Exception as exc:
        step = VerificationStepResult(
            level="L4",
            name="Strategy 实例化",
            ok=False,
            message=f"Strategy.__init__(config) 抛出异常: {exc}",
        )
        steps.append(step)
        return VerificationResult(
            ok=False,
            summary="Strategy 构造函数异常 (L4 运行契约失败)",
            steps=steps,
            failed_level="L4",
            error_message=f"Strategy.__init__ 抛出异常: {exc}",
            suggestion="确保 Strategy.__init__(self, config) 先调用 super().__init__(config)，且初始化逻辑不要在无真实环境时过早访问 self.cache/self.portfolio。",
        )

    # Check that the strategy class actually implements on_bar or other event handlers
    has_custom_on_bar = (
        "on_bar" in strategy_cls.__dict__
        or getattr(strategy_cls, "on_bar", None) != getattr(Strategy, "on_bar", None)
    )
    if not has_custom_on_bar:
        step = VerificationStepResult(
            level="L4",
            name="on_bar 事件处理器实现",
            ok=False,
            message="Strategy 子类未实现自定义 on_bar(self, bar: Bar) 方法",
        )
        steps.append(step)
        return VerificationResult(
            ok=False,
            summary="Strategy 缺少 on_bar 实现 (L4 运行契约失败)",
            steps=steps,
            failed_level="L4",
            error_message="Strategy 类必须实现 on_bar(self, bar: Bar) 处理 K 线驱动逻辑",
            suggestion="在 Strategy 子类中定义 on_bar(self, bar: Bar) 方法。",
        )

    steps.append(
        VerificationStepResult(
            level="L4",
            name="NautilusTrader 运行时契约",
            ok=True,
            message="Strategy 实例化、生命周期钩子与配置注入校验通过",
        )
    )

    return VerificationResult(
        ok=True,
        summary="策略代码 4 级 Pre-Flight 校验全部通过",
        steps=steps,
    )
