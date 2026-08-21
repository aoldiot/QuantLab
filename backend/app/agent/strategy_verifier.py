from __future__ import annotations

import ast
import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from ..strategy_contract import (
    ParameterSpec,
    StrategyManifest,
    StrategyMode,
    validate_parameters,
    validate_plot_contract,
)

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

    df = pd.DataFrame(
        {
            "timestamp": dates,
            "ts_init": [int(d.value) for d in dates],
            "open": open_prices,
            "high": high_prices,
            "low": low_prices,
            "close": close_prices,
            "volume": volume,
        }
    )
    return df


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


def _clean_code_lines(code: str) -> str:
    """Clean common LLM code formatting artifacts, repeated fences, headers, and conversational text."""
    if not code:
        return ""

    # Strip Unicode BOM and zero-width / invisible control characters
    code = (
        code.replace("\ufeff", "")
        .replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\u200d", "")
        .replace("\u2060", "")
    )
    lines = code.splitlines()

    # 1. Strip leading invalid lines (markdown fences, info strings, language tags, conversational intro)
    while lines:
        line = lines[0].strip()
        if not line:
            lines = lines[1:]
            continue

        # Repeated markdown code fence on line 1
        if line.startswith("```"):
            lines = lines[1:]
            continue

        # Language identifiers or file info lines
        line_lower = line.lower()
        if line_lower in ("python", "python3", "py", "python:", "py:"):
            lines = lines[1:]
            continue

        if (
            line.startswith(":")
            or line_lower.startswith("python:")
            or line_lower.startswith("filename=")
            or line_lower.startswith("file:")
            or line_lower.startswith("path:")
            or line_lower.startswith("filepath:")
            or line_lower.startswith("# filepath:")
            or line_lower.startswith("//")
            or (line.startswith("[") and line.endswith("]") and ".py" in line)
            or (line.startswith("#") and ".py" in line and not line.startswith("#!/") and "coding:" not in line)
            or (line.startswith("##") or line.startswith("###"))
        ):
            lines = lines[1:]
            continue

        # Check if line is a valid Python statement starter
        is_valid_python_starter = (
            line.startswith("from ")
            or line.startswith("import ")
            or line.startswith("class ")
            or line.startswith("def ")
            or line.startswith("@")
            or line.startswith("#")
            or line.startswith("STRATEGY_MANIFEST")
            or line.startswith('"""')
            or line.startswith("'''")
            or line.startswith("try:")
            or line.startswith("if ")
            or line.startswith("__")
        )
        if is_valid_python_starter:
            break

        # If it is not a standard Python starter, test if it compiles as a standalone Python statement
        try:
            compile(line, "<line_test>", "exec")
            break
        except SyntaxError:
            # It's non-Python conversational text on line 1, strip it!
            lines = lines[1:]

    # 2. Strip trailing closing backticks or conversational text
    while lines:
        line = lines[-1].strip()
        if not line or line.startswith("```") or line.startswith("'''"):
            lines = lines[:-1]
            continue
        # Check if trailing line is conversational text after manifest/definitions
        if not line.endswith(")") and not line.endswith("}") and not line.endswith("]") and not line.endswith(":") and not line.startswith("#"):
            try:
                compile(line, "<line_test>", "exec")
                break
            except SyntaxError:
                lines = lines[:-1]
                continue
        break

    return "\n".join(lines).strip()


def extract_target_method_from_error(error_msg: str, traceback_str: str = "") -> str | None:
    """Analyze an error message / traceback to identify which strategy method threw the error."""
    combined = f"{error_msg}\n{traceback_str}"
    import re
    # Match patterns like: in on_bar, in on_start, in _check_entry_conditions, in calculate_indicators
    match = re.search(r"in\s+([a-zA-Z_][a-zA-Z0-9_]*)\b", combined)
    if match:
        fn_name = match.group(1)
        if fn_name not in ("compile", "exec", "verify_strategy_file", "_simulate_strategy_execution"):
            return fn_name
    for candidate in ("on_bar", "on_start", "on_stop", "calculate_indicators", "_check_entry", "_check_exit"):
        if candidate in combined:
            return candidate
    return None


def patch_strategy_method(source_code: str, target_method_name: str, new_method_code: str) -> str:
    """Use AST to cleanly patch a single method inside a Python file."""
    if not source_code or not target_method_name or not new_method_code:
        return source_code

    try:
        tree = ast.parse(source_code)
    except Exception:
        return source_code

    lines = source_code.splitlines()

    # Find the target function definition node
    target_node: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == target_method_name:
            target_node = node
            break

    if target_node is None or not hasattr(target_node, "lineno") or not hasattr(target_node, "end_lineno"):
        return source_code

    start_idx = target_node.lineno - 1  # 0-indexed
    end_idx = target_node.end_lineno    # slice end

    # Detect indentation of the original method
    orig_first_line = lines[start_idx]
    indent_len = len(orig_first_line) - len(orig_first_line.lstrip())
    indent_str = " " * indent_len

    # Clean new_method_code
    new_method_clean = _clean_code_lines(new_method_code)
    new_lines = new_method_clean.splitlines()
    if not new_lines:
        return source_code

    # Re-indent new_lines to match indent_str
    first_new_line = new_lines[0]
    base_new_indent = len(first_new_line) - len(first_new_line.lstrip())
    formatted_new_lines = []
    for l in new_lines:
        if not l.strip():
            formatted_new_lines.append("")
        else:
            cur_indent = len(l) - len(l.lstrip())
            rel_indent = max(0, cur_indent - base_new_indent)
            formatted_new_lines.append(indent_str + (" " * rel_indent) + l.lstrip())

    # Splice
    patched_lines = lines[:start_idx] + formatted_new_lines + lines[end_idx:]
    patched_source = "\n".join(patched_lines)

    # Validate AST
    try:
        ast.parse(patched_source)
        return patched_source
    except Exception:
        return source_code


def extract_python_strategy_code(content: str) -> str:

    """Robustly extract the full Python strategy code block from LLM output.

    Handles:
    1. Unicode BOM, zero-width spaces, and control artifacts.
    2. Multiple code blocks (e.g. ```json ..., ```markdown ..., ```python ...) by scoring blocks
       based on Python strategy keywords (STRATEGY_MANIFEST, StrategyConfig, calculate_indicators, Strategy).
    3. Code fence info strings and repeated fences (e.g. ```python:backend/app/..., ```python\\n```python).
    4. Conversational / markdown headers or commentary inside or outside code fences.
    5. Fallback for unclosed code fences or raw Python code.
    6. Multi-chunk strategy stitching when LLM divides imports, config, strategy, calculate_indicators, manifest across blocks.
    """
    if not content or not content.strip():
        return ""

    import re

    # Strip BOM / zero-width characters upfront
    content = (
        content.replace("\ufeff", "")
        .replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\u200d", "")
        .replace("\u2060", "")
    )

    # 1. Regex to match all fenced blocks: ```[info_string]\n[code_content]```
    fenced_pattern = r"```([^\n]*)\n([\s\S]*?)```"
    matches = re.findall(fenced_pattern, content)

    candidates: list[tuple[int, str]] = []
    python_blocks: list[str] = []

    for info, raw_code in matches:
        info_lower = info.strip().lower()
        cleaned_code = _clean_code_lines(raw_code)
        if not cleaned_code:
            continue

        # Score candidate
        score = 0
        if "STRATEGY_MANIFEST" in cleaned_code:
            score += 10
        if "StrategyConfig" in cleaned_code:
            score += 5
        if "Strategy" in cleaned_code:
            score += 5
        if "calculate_indicators" in cleaned_code:
            score += 5
        if any(lang in info_lower for lang in ("python", "py")):
            score += 3
        if "from decimal import Decimal" in cleaned_code or "import pandas as pd" in cleaned_code:
            score += 2

        candidates.append((score, cleaned_code))
        if any(lang in info_lower for lang in ("python", "py")) or "import " in cleaned_code or "class " in cleaned_code:
            python_blocks.append(cleaned_code)

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        best_score, best_code = candidates[0]
        # If the best single block has all essential elements, return it
        if best_score >= 20 or (best_score >= 10 and "STRATEGY_MANIFEST" in best_code):
            return best_code

        # If LLM split strategy into multiple code blocks, stitch them together
        if len(python_blocks) > 1:
            stitched = "\n\n".join(python_blocks)
            cleaned_stitched = _clean_code_lines(stitched)
            if "STRATEGY_MANIFEST" in cleaned_stitched and "calculate_indicators" in cleaned_stitched:
                return cleaned_stitched

        if best_score > 0:
            return best_code

    # 2. Fallback if no clean ``` ``` was closed, but content contains Strategy definitions
    if "class " in content and "Strategy" in content and "STRATEGY_MANIFEST" in content:
        # If there's an opening ```python fence somewhere, extract from that point forward
        match_fence = re.search(r"```(?:python[^\n]*|py[^\n]*|[^\n]*)\n", content, flags=re.IGNORECASE)
        if match_fence:
            after_fence = content[match_fence.end() :]
            # Remove any trailing closing backticks
            after_fence = re.sub(r"\n```[^\n]*$", "", after_fence.strip())
            cleaned_after = _clean_code_lines(after_fence)
            if "STRATEGY_MANIFEST" in cleaned_after:
                return cleaned_after

        # Otherwise clean content directly
        naked = re.sub(r"^```[^\n]*\n", "", content.strip(), flags=re.MULTILINE)
        naked = re.sub(r"\n```$", "", naked, flags=re.MULTILINE)
        return _clean_code_lines(naked)

    # 3. Fallback for raw python code without fences
    if "from " in content or "import " in content or "class " in content:
        return _clean_code_lines(content)

    return ""


def _check_nautilus_ast_rules(tree: ast.AST) -> list[str]:
    """Check for forbidden or commonly hallucinated NautilusTrader API calls."""
    errors: list[str] = []
    # Check if strategy uses QuantLabStrategy
    uses_quantlab_base = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if any("QuantLabStrategy" in getattr(alias, "name", "") for alias in getattr(node, "names", [])):
                uses_quantlab_base = True
        elif isinstance(node, ast.ClassDef):
            for base in node.bases:
                if (isinstance(base, ast.Name) and "QuantLabStrategy" in base.id) or (
                    isinstance(base, ast.Attribute) and "QuantLabStrategy" in base.attr
                ):
                    uses_quantlab_base = True

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            # Check self.portfolio.xxx attributes/calls
            if isinstance(node.value, ast.Attribute) and node.value.attr == "portfolio":
                if node.attr in ("account_balance", "balance", "get_balance"):
                    errors.append(
                        f"第 {node.lineno} 行: 禁止调用 self.portfolio.{node.attr}()。NautilusTrader Portfolio 没有 {node.attr} 方法。如需获取账户净值请使用 self.portfolio.equity(self.instrument_id.venue)，或使用配置参数 self.config.trade_size 结合 self.instrument.make_qty(...) 进行下单。"
                    )
                elif node.attr == "is_net_flat":
                    errors.append(
                        f"第 {node.lineno} 行: 禁止调用 self.portfolio.is_net_flat()。NautilusTrader Portfolio 没有 is_net_flat 方法，请替换为 self.portfolio.is_flat(self.instrument_id)。"
                    )
                elif node.attr == "position":
                    errors.append(
                        f"第 {node.lineno} 行: 禁止调用 self.portfolio.position()。NautilusTrader 没有 self.portfolio.position 方法，请使用 self.portfolio.net_position(self.instrument_id) 或 self.portfolio.is_flat/is_net_long/is_net_short。"
                    )
            # Check self.instrument.xxx attributes/calls
            elif isinstance(node.value, ast.Attribute) and node.value.attr == "instrument":
                if node.attr in ("round_quantity", "round_qty"):
                    errors.append(
                        f"第 {node.lineno} 行: 禁止调用 self.instrument.{node.attr}()。NautilusTrader Instrument 没有 {node.attr} 方法，请使用 self.instrument.make_qty(...) 直接生成规范精度的 Quantity 对象。"
                    )
                elif node.attr in ("round_price",):
                    errors.append(
                        f"第 {node.lineno} 行: 禁止调用 self.instrument.{node.attr}()。NautilusTrader Instrument 没有 {node.attr} 方法，请使用 self.instrument.make_price(...)。"
                    )
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            mod_name = getattr(node, "module", "") or ""
            for alias in getattr(node, "names", []):
                full_name = f"{mod_name}.{alias.name}" if mod_name else alias.name
                if "nautilus_trader.indicators.average" in full_name or full_name == "nautilus_trader.indicators.average":
                    errors.append(
                        f"第 {node.lineno} 行: 禁止导入不存在的模块 `{full_name}`。NautilusTrader 移动平均指标模块为 `nautilus_trader.indicators.averages`（注意是 averages 复数），推荐直接使用 `from nautilus_trader.indicators import SimpleMovingAverage, ExponentialMovingAverage` 或 `from app.quant.indicators import ...`。"
                    )
            if mod_name == "nautilus_trader.indicators.average" or mod_name.startswith("nautilus_trader.indicators.average."):
                errors.append(
                    f"第 {node.lineno} 行: 禁止导入不存在的模块 `{mod_name}`。NautilusTrader 移动平均指标模块为 `nautilus_trader.indicators.averages`（注意是 averages 复数），推荐直接使用 `from nautilus_trader.indicators import SimpleMovingAverage, ExponentialMovingAverage` 或 `from app.quant.indicators import ...`。"
                )
            elif mod_name.startswith("app.quant.library."):
                errors.append(
                    f"第 {node.lineno} 行: 禁止导入不存在的模块 `{mod_name}`。QuantLab 策略契约请使用 `from app.strategy_contract import StrategyManifest, ParameterSpec, StrategyMode`，指标请使用 `from app.quant.indicators import ...`。"
                )
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "close_position":
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "self":
                    if not uses_quantlab_base:
                        errors.append(
                            f"第 {node.lineno} 行: Strategy.close_position 只能接收 Position 实例对象。若要按标的平仓全部头寸，请使用 self.close_all_positions(self.instrument_id) 或继承 QuantLabStrategy。"
                        )
    return errors



def _simulate_strategy_execution(
    strategy_cls: type[Strategy],
    config_instance: Any,
    manifest: StrategyManifest,
) -> tuple[bool, str | None, str | None]:
    """Run an in-memory NautilusTrader BacktestEngine with synthetic bars to verify on_start() and on_bar() execution."""
    from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
    from nautilus_trader.config import LoggingConfig
    from nautilus_trader.model.currencies import USDT
    from nautilus_trader.model.data import Bar, BarSpecification, BarType
    from nautilus_trader.model.enums import (
        AccountType,
        BarAggregation,
        OmsType,
        PriceType,
    )
    from nautilus_trader.model.identifiers import TraderId, Venue
    from nautilus_trader.model.objects import Money
    from nautilus_trader.test_kit.providers import TestInstrumentProvider

    try:
        engine = BacktestEngine(
            config=BacktestEngineConfig(
                trader_id=TraderId("SIM-001"),
                logging=LoggingConfig(log_level="ERROR"),
            )
        )
        engine.add_venue(
            venue=Venue("BINANCE"),
            oms_type=OmsType.HEDGING,
            account_type=AccountType.MARGIN,
            base_currency=USDT,
            starting_balances=[Money(100000, USDT)],
        )
        test_instrument = TestInstrumentProvider.btcusdt_binance()
        engine.add_instrument(test_instrument)

        strat = strategy_cls(config_instance)
        engine.add_strategy(strat)

        bar_type = getattr(config_instance, "bar_type", None)
        if bar_type is None and hasattr(config_instance, "bar_types") and config_instance.bar_types:
            bar_type = config_instance.bar_types[0]
        if bar_type is None:
            bar_type = BarType(test_instrument.id, BarSpecification(1, BarAggregation.HOUR, PriceType.LAST))

        bars = []
        base_ts = 1704067200000000000
        prices = [50000.0, 50200.0, 49800.0, 50500.0, 51000.0, 50800.0, 51200.0, 49500.0]
        for i, p in enumerate(prices):
            ts = base_ts + i * 3600 * 1_000_000_000
            bar = Bar(
                bar_type,
                test_instrument.make_price(p),
                test_instrument.make_price(p + 100),
                test_instrument.make_price(p - 100),
                test_instrument.make_price(p + 50),
                test_instrument.make_qty(10),
                ts,
                ts,
            )
            bars.append(bar)

        engine.add_data(bars)
        engine.run()
        return True, None, None
    except Exception as exc:
        import traceback

        tb = traceback.format_exc()
        lines = [line.strip() for line in tb.splitlines() if line.strip()]
        error_msg = str(exc)
        suggestion = "请检查策略在 on_start 和 on_bar 中的 API 调用、持仓状态判断、下单数量转换及变量定义。"
        if "account_balance" in error_msg:
            suggestion = "NautilusTrader Portfolio 没有 account_balance 方法！请使用 self.portfolio.equity(self.instrument_id.venue) 获取账户净值，或使用 self.instrument.make_qty(self.config.trade_size) 进行下单。"
        elif "is_net_flat" in error_msg:
            suggestion = "NautilusTrader Portfolio 没有 is_net_flat 方法！请使用 self.portfolio.is_flat(self.instrument_id)。"
        elif "round_quantity" in error_msg:
            suggestion = "NautilusTrader Instrument 没有 round_quantity 方法！请使用 self.instrument.make_qty(...)。"
        elif "close_position" in error_msg:
            suggestion = "平仓指定标的请使用 self.close_all_positions(self.instrument_id)。"
        elif "quantity" in error_msg and "Quantity" in error_msg:
            suggestion = "下单数量 quantity 必须为 nautilus_trader.model.objects.Quantity 类型，请使用 self.instrument.make_qty(...)。"
        detail = f"{error_msg} ({lines[-2] if len(lines) >= 2 else ''})"
        return False, detail, suggestion


def verify_strategy_file(file_path: Path | str, strategy_name: str | None = None) -> VerificationResult:
    """Verify strategy file on disk across all 4 levels.

    Levels:
    L1: AST static syntax and required structural declarations
    L2: Dynamic import, StrategyManifest validation, StrategyConfig schema validation
    L3: Vectorized calculate_indicators sandbox execution with sample OHLCV DataFrame
    L4: NautilusTrader strategy instantiation and contract conformance check
    """
    file_path = Path(file_path).resolve()
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

    # Auto-clean BOM, zero-width characters and stray markdown fences if present
    cleaned_source = _clean_code_lines(source_text)
    if "```" in source_text or "STRATEGY_MANIFEST" in source_text:
        extracted = extract_python_strategy_code(source_text)
        if extracted and "STRATEGY_MANIFEST" in extracted:
            cleaned_source = extracted

    if cleaned_source and cleaned_source != source_text:
        source_text = cleaned_source
        try:
            file_path.write_text(source_text, encoding="utf-8")
        except Exception:
            pass

    try:
        compile(source_text, str(file_path), "exec")
    except SyntaxError as exc:
        # Attempt auto-heal if error is at line 1 (e.g. stray comment, path tag, or header)
        healed = False
        if exc.lineno == 1:
            heal_attempt = _clean_code_lines("\n".join(source_text.splitlines()[1:]))
            if heal_attempt and "STRATEGY_MANIFEST" in heal_attempt:
                try:
                    compile(heal_attempt, str(file_path), "exec")
                    source_text = heal_attempt
                    file_path.write_text(source_text, encoding="utf-8")
                    healed = True
                except Exception:
                    pass

        if not healed:
            total_lines = len(source_text.splitlines())
            is_eof_error = exc.lineno is not None and (exc.lineno >= total_lines - 5 or total_lines > 400)
            trunc_hint = "（注意：若错误发生在末尾，极可能是输出超长截断，请使用 app.quant.indicators 精简代码）" if is_eof_error else ""
            step = VerificationStepResult(
                level="L1",
                name="Python 语法编译",
                ok=False,
                message=f"Python 语法错误 (第 {exc.lineno} 行): {exc.msg}",
            )
            steps.append(step)
            return VerificationResult(
                ok=False,
                summary="Python 语法错误 (L1 静态语法失败)",
                steps=steps,
                failed_level="L1",
                error_message=f"第 {exc.lineno} 行: {exc.msg}",
                suggestion=f"请检查 Python 代码缩进、括号匹配与语法合规性。代码第一行必须是 Python 导入语句。{trunc_hint}",
            )

    except Exception as exc:
        step = VerificationStepResult(
            level="L1",
            name="Python 语法编译",
            ok=False,
            message=f"Python 编译错误: {exc}",
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

    def _is_strategy_class_node(node: ast.ClassDef) -> bool:
        # Check base classes first
        for base in node.bases:
            if isinstance(base, ast.Name) and "Strategy" in base.id and not base.id.endswith("Config"):
                return True
            if isinstance(base, ast.Attribute) and "Strategy" in base.attr and not base.attr.endswith("Config"):
                return True
        # If class name indicates a config class, it is not a strategy
        if node.name.lower().endswith("config"):
            return False
        if "Strategy" in node.name or node.name.lower().endswith("strategy"):
            return True
        return False

    def _is_config_class_node(node: ast.ClassDef) -> bool:
        for base in node.bases:
            if isinstance(base, ast.Name) and ("Config" in base.id or "StrategyConfig" in base.id):
                return True
            if isinstance(base, ast.Attribute) and ("Config" in base.attr or "StrategyConfig" in base.attr):
                return True
        if node.name.lower().endswith("config") or "Config" in node.name:
            return True
        return False

    class_nodes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    has_config_class = any(_is_config_class_node(node) for node in class_nodes)
    has_strategy_class = any(_is_strategy_class_node(node) for node in class_nodes)

    l1_errors = []
    if "STRATEGY_MANIFEST" not in assignments:
        l1_errors.append("缺少 STRATEGY_MANIFEST 对象定义")
    # calculate_indicators is optional: QuantLab auto-derives indicators if omitted
    if not has_config_class:
        l1_errors.append("缺少继承自 StrategyConfig 的配置类声明 (例如 class XxxConfig(StrategyConfig))")
    if not has_strategy_class:
        l1_errors.append("缺少继承自 Strategy 的策略类声明 (例如 class XxxStrategy(QuantLabStrategy) 或 class Xxx(Strategy))")



    ast_rule_errors = _check_nautilus_ast_rules(tree)
    if ast_rule_errors:
        l1_errors.extend(ast_rule_errors)

    if l1_errors:
        step = VerificationStepResult(
            level="L1",
            name="核心导出结构与 AST 规范检查",
            ok=False,
            message="；".join(l1_errors),
        )
        steps.append(step)
        return VerificationResult(
            ok=False,
            summary="静态 AST 结构或 Nautilus 规范未通过 (L1 静态检查失败)",
            steps=steps,
            failed_level="L1",
            error_message="；".join(l1_errors),
            suggestion="请检查 NautilusTrader 语法规范，消除非法 API 调用与缺少的核心导出结构。",
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
    # Level 2 ~ Level 4: Dynamic Sandbox Execution
    # Module must remain registered in sys.modules during L2, L3, L4 for type resolution
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
        sys.modules.pop(module_name, None)
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

    try:
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
        if not isinstance(manifest.parameters, dict):
            step = VerificationStepResult(
                level="L2",
                name="parameters 格式",
                ok=False,
                message="manifest.parameters 必须是字典",
            )
            steps.append(step)
            return VerificationResult(
                ok=False,
                summary="parameters 格式非法 (L2 契约校验失败)",
                steps=steps,
                failed_level="L2",
                error_message="STRATEGY_MANIFEST.parameters 必须是 dict[str, ParameterSpec]",
                suggestion="确保 parameters 为字典格式，每个参数使用 ParameterSpec 定义。",
            )

        default_parameters: dict[str, Any] = {}
        for param_name, param_spec in manifest.parameters.items():
            if not isinstance(param_spec, ParameterSpec):
                step = VerificationStepResult(
                    level="L2",
                    name="ParameterSpec 类型校验",
                    ok=False,
                    message=f"参数 `{param_name}` 的定义不是 ParameterSpec 实例（当前为 {type(param_spec).__name__}）",
                )
                steps.append(step)
                return VerificationResult(
                    ok=False,
                    summary=f"参数 `{param_name}` 类型非法 (L2 契约校验失败)",
                    steps=steps,
                    failed_level="L2",
                    error_message=f"ParameterSpec `{param_name}` 必须从 app.strategy_contract 导入 ParameterSpec 并实例化",
                    suggestion="请使用 ParameterSpec(title=..., type=..., default=...) 定义策略参数。",
                )

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

        # Validate parameter default values against spec bounds
        try:
            validate_parameters(manifest, default_parameters)
        except Exception as exc:
            step = VerificationStepResult(
                level="L2",
                name="参数默认值合法性校验",
                ok=False,
                message=f"参数默认值超出范围或类型不符: {exc}",
            )
            steps.append(step)
            return VerificationResult(
                ok=False,
                summary="参数默认值校验未通过 (L2 契约校验失败)",
                steps=steps,
                failed_level="L2",
                error_message=str(exc),
                suggestion="确保每个 ParameterSpec 的 default 默认值在 [minimum, maximum] 范围内且符合类型约束。",
            )

        # Validate timeframes & primary_timeframe consistency
        if not manifest.timeframes:
            step = VerificationStepResult(
                level="L2",
                name="timeframes 契约校验",
                ok=False,
                message="STRATEGY_MANIFEST.timeframes 不能为空",
            )
            steps.append(step)
            return VerificationResult(
                ok=False,
                summary="缺少 timeframes 周期配置 (L2 契约校验失败)",
                steps=steps,
                failed_level="L2",
                error_message="timeframes 元组不能为空",
                suggestion="请在 STRATEGY_MANIFEST 中指定支持的时间周期，如 timeframes=('15m',)。",
            )

        if manifest.primary_timeframe not in manifest.timeframes:
            step = VerificationStepResult(
                level="L2",
                name="primary_timeframe 包含性校验",
                ok=False,
                message=f"主周期 `{manifest.primary_timeframe}` 未包含在 timeframes {list(manifest.timeframes)} 中",
            )
            steps.append(step)
            return VerificationResult(
                ok=False,
                summary="主周期未包含在支持周期中 (L2 契约校验失败)",
                steps=steps,
                failed_level="L2",
                error_message=f"primary_timeframe '{manifest.primary_timeframe}' 必须包含在 timeframes {list(manifest.timeframes)} 中",
                suggestion=f"请将 `{manifest.primary_timeframe}` 添加到 STRATEGY_MANIFEST.timeframes 元组中。",
            )

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

        supported_chart_types = {"line", "histogram", "bar", "area", "baseline"}

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

        for ind_name, ind_spec in main_plot.items():
            if not isinstance(ind_spec, dict):
                step = VerificationStepResult(
                    level="L2",
                    name="main_plot 指标配置规范",
                    ok=False,
                    message=f"main_plot['{ind_name}'] 的值不是字典。正确格式为 main_plot={{'{ind_name}': {{'type': 'line', 'color': '#ffffff'}}}}",
                )
                steps.append(step)
                return VerificationResult(
                    ok=False,
                    summary="main_plot 指标配置非法 (L2 契约校验失败)",
                    steps=steps,
                    failed_level="L2",
                    error_message=f"main_plot['{ind_name}'] 必须是字典配置，当前为 {type(ind_spec).__name__}",
                    suggestion="主图指标必须映射为指标配置字典，例如: {'type': 'line', 'color': '#ffffff'}。",
                )
            chart_type = ind_spec.get("type", "line")
            if chart_type not in supported_chart_types:
                step = VerificationStepResult(
                    level="L2",
                    name="main_plot 图表类型",
                    ok=False,
                    message=f"main_plot['{ind_name}'] 使用了不支持的图表类型 '{chart_type}'",
                )
                steps.append(step)
                return VerificationResult(
                    ok=False,
                    summary="不支持的图表类型 (L2 契约校验失败)",
                    steps=steps,
                    failed_level="L2",
                    error_message=f"图表类型 '{chart_type}' 不在支持列表 {sorted(supported_chart_types)} 中",
                    suggestion=f"请将图表类型修改为 {sorted(supported_chart_types)} 之一。",
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
                chart_type = ind_spec.get("type", "line")
                if chart_type not in supported_chart_types:
                    step = VerificationStepResult(
                        level="L2",
                        name="subplots 图表类型",
                        ok=False,
                        message=f"subplots['{pane_name}']['{ind_name}'] 使用了不支持的图表类型 '{chart_type}'",
                    )
                    steps.append(step)
                    return VerificationResult(
                        ok=False,
                        summary="不支持的副图图表类型 (L2 契约校验失败)",
                        steps=steps,
                        failed_level="L2",
                        error_message=f"图表类型 '{chart_type}' 不在支持列表 {sorted(supported_chart_types)} 中",
                        suggestion=f"请将副图图表类型修改为 {sorted(supported_chart_types)} 之一。",
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

        # Second preference: scan module __dict__, prioritizing local classes
        if not config_cls:
            for attr_name, attr in mod.__dict__.items():
                if (
                    isinstance(attr, type)
                    and issubclass(attr, StrategyConfig)
                    and attr is not StrategyConfig
                    and getattr(attr, "__module__", None) == mod.__name__
                ):
                    config_cls = attr
                    break
        if not config_cls:
            for attr_name, attr in mod.__dict__.items():
                if isinstance(attr, type) and issubclass(attr, StrategyConfig) and attr is not StrategyConfig:
                    config_cls = attr
                    break

        if not strategy_cls:
            for attr_name, attr in mod.__dict__.items():
                if (
                    isinstance(attr, type)
                    and issubclass(attr, Strategy)
                    and attr is not Strategy
                    and getattr(attr, "__module__", None) == mod.__name__
                ):
                    strategy_cls = attr
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

        # Validate strategy_path format & class name matching
        expected_mod_prefix = f"app.strategies.{strategy_slug}"
        if not manifest.strategy_path or ":" not in manifest.strategy_path:
            step = VerificationStepResult(
                level="L2",
                name="strategy_path 格式规范",
                ok=False,
                message=f"STRATEGY_MANIFEST.strategy_path ('{manifest.strategy_path}') 必须采用 'app.strategies.{strategy_slug}:StrategyClass' 格式",
            )
            steps.append(step)
            return VerificationResult(
                ok=False,
                summary="strategy_path 格式非法 (L2 契约校验失败)",
                steps=steps,
                failed_level="L2",
                error_message=f"strategy_path 格式错误: '{manifest.strategy_path}'",
                suggestion=f"请将 STRATEGY_MANIFEST.strategy_path 设置为 `{expected_mod_prefix}:{strategy_cls.__name__ if strategy_cls else 'XxxStrategy'}`。",
            )

        s_mod_part, s_name = manifest.strategy_path.split(":", 1)
        if s_mod_part != expected_mod_prefix and not s_mod_part.startswith("app.strategies."):
            step = VerificationStepResult(
                level="L2",
                name="strategy_path 模块前缀",
                ok=False,
                message=f"STRATEGY_MANIFEST.strategy_path 模块前缀 `{s_mod_part}` 错误，必须为 `{expected_mod_prefix}`",
            )
            steps.append(step)
            return VerificationResult(
                ok=False,
                summary="strategy_path 模块前缀错误 (L2 契约校验失败)",
                steps=steps,
                failed_level="L2",
                error_message=f"strategy_path 模块前缀 `{s_mod_part}` 无法在回测时正确导入，应为 `{expected_mod_prefix}`",
                suggestion=f"请将 STRATEGY_MANIFEST.strategy_path 修改为 `{expected_mod_prefix}:{s_name}`。",
            )

        if strategy_cls and strategy_cls.__name__ != s_name and not hasattr(mod, s_name):
            step = VerificationStepResult(
                level="L2",
                name="strategy_path 路径与类名匹配",
                ok=False,
                message=f"STRATEGY_MANIFEST.strategy_path 为 `{manifest.strategy_path}`（指向 `{s_name}`），但模块中实际策略类为 `{strategy_cls.__name__}`",
            )
            steps.append(step)
            return VerificationResult(
                ok=False,
                summary="strategy_path 类名不匹配 (L2 契约校验失败)",
                steps=steps,
                failed_level="L2",
                error_message=f"STRATEGY_MANIFEST.strategy_path 指向的 `{s_name}` 不存在，实际策略类名为 `{strategy_cls.__name__}`",
                suggestion=f"请将 STRATEGY_MANIFEST.strategy_path 修改为 `{expected_mod_prefix}:{strategy_cls.__name__}`，或将策略类名重命名为 `{s_name}`。",
            )

        # Validate config_path format & class name matching
        if not manifest.config_path or ":" not in manifest.config_path:
            step = VerificationStepResult(
                level="L2",
                name="config_path 格式规范",
                ok=False,
                message=f"STRATEGY_MANIFEST.config_path ('{manifest.config_path}') 必须采用 'app.strategies.{strategy_slug}:ConfigClass' 格式",
            )
            steps.append(step)
            return VerificationResult(
                ok=False,
                summary="config_path 格式非法 (L2 契约校验失败)",
                steps=steps,
                failed_level="L2",
                error_message=f"config_path 格式错误: '{manifest.config_path}'",
                suggestion=f"请将 STRATEGY_MANIFEST.config_path 设置为 `{expected_mod_prefix}:{config_cls.__name__}`。",
            )

        c_mod_part, c_name = manifest.config_path.split(":", 1)
        if c_mod_part != expected_mod_prefix and not c_mod_part.startswith("app.strategies."):
            step = VerificationStepResult(
                level="L2",
                name="config_path 模块前缀",
                ok=False,
                message=f"STRATEGY_MANIFEST.config_path 模块前缀 `{c_mod_part}` 错误，必须为 `{expected_mod_prefix}`",
            )
            steps.append(step)
            return VerificationResult(
                ok=False,
                summary="config_path 模块前缀错误 (L2 契约校验失败)",
                steps=steps,
                failed_level="L2",
                error_message=f"config_path 模块前缀 `{c_mod_part}` 无法在回测时正确导入，应为 `{expected_mod_prefix}`",
                suggestion=f"请将 STRATEGY_MANIFEST.config_path 修改为 `{expected_mod_prefix}:{c_name}`。",
            )

        if config_cls and config_cls.__name__ != c_name and not hasattr(mod, c_name):
            step = VerificationStepResult(
                level="L2",
                name="config_path 路径与类名匹配",
                ok=False,
                message=f"STRATEGY_MANIFEST.config_path 为 `{manifest.config_path}`（指向 `{c_name}`），但模块中实际配置类为 `{config_cls.__name__}`",
            )
            steps.append(step)
            return VerificationResult(
                ok=False,
                summary="config_path 类名不匹配 (L2 契约校验失败)",
                steps=steps,
                failed_level="L2",
                error_message=f"STRATEGY_MANIFEST.config_path 指向的 `{c_name}` 不存在，实际配置类名为 `{config_cls.__name__}`",
                suggestion=f"请将 STRATEGY_MANIFEST.config_path 修改为 `{expected_mod_prefix}:{config_cls.__name__}`，或将配置类名重命名为 `{c_name}`。",
            )

        # Try instantiating StrategyConfig with default parameters
        test_inst_id = InstrumentId.from_str("BTCUSDT.BINANCE")
        test_bar_type = BarType.from_str("BTCUSDT.BINANCE-1-HOUR-LAST-EXTERNAL")
        test_config_kwargs = {
            **default_parameters,
        }
        if manifest.mode == StrategyMode.PORTFOLIO:
            test_config_kwargs["instrument_ids"] = [test_inst_id]
            test_config_kwargs["bar_types"] = [test_bar_type]
            test_config_kwargs["order_id_tag"] = "001"
        else:
            test_config_kwargs["instrument_id"] = test_inst_id
            test_config_kwargs["bar_type"] = test_bar_type
            test_config_kwargs["order_id_tag"] = "001"

        try:
            instantiated_config = config_cls(**test_config_kwargs)
        except TypeError as t_exc:
            # Check if config_cls does not accept instrument_id or order_id_tag
            # Try filtering strictly to recognized fields
            try:
                instantiated_config = config_cls(**default_parameters)
            except Exception:
                step = VerificationStepResult(
                    level="L2",
                    name="StrategyConfig 实例化",
                    ok=False,
                    message=f"使用默认参数实例化 StrategyConfig 失败: {t_exc}",
                )
                steps.append(step)
                return VerificationResult(
                    ok=False,
                    summary="StrategyConfig 实例化失败 (L2 契约校验失败)",
                    steps=steps,
                    failed_level="L2",
                    error_message=str(t_exc),
                    suggestion="检查 StrategyConfig 中的字段声明是否与 ParameterSpec 和默认值类型一致，且包含 instrument_id 和 bar_type 字段。",
                )
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
        # Level 3: Vectorized calculate_indicators or auto-derivation sandbox execution
        # =========================================================================
        calculate_indicators_fn = getattr(mod, "calculate_indicators", None)
        sample_df = _create_sample_ohlcv(rows=200)

        if callable(calculate_indicators_fn):
            try:
                calculated_df = calculate_indicators_fn(sample_df.copy(), default_parameters.copy())
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
        else:
            # Auto-derive plot indicators using QuantLab standard vectorized calculation
            from ..quant.indicators import calc_standard_indicators
            calculated_df = calc_standard_indicators(sample_df.copy(), default_parameters.copy())

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

        if calculated_df.columns.has_duplicates:
            dup_cols = calculated_df.columns[calculated_df.columns.duplicated()].unique().tolist()
            step = VerificationStepResult(
                level="L3",
                name="DataFrame 列名唯一性检查",
                ok=False,
                message=f"calculate_indicators 返回的 DataFrame 包含重复列名: {dup_cols}",
            )
            steps.append(step)
            return VerificationResult(
                ok=False,
                summary=f"指标计算包含重复列名: {dup_cols} (L3 计算异常)",
                steps=steps,
                failed_level="L3",
                error_message=f"DataFrame 列名重复: {dup_cols}",
                suggestion="确保 calculate_indicators 中的指标赋值列名唯一，避免重复定义同名列。",
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
        if missing_cols and not callable(calculate_indicators_fn):
            # Auto-fill missing columns for auto-derived strategies
            for col in missing_cols:
                calculated_df[col] = 0.0
            missing_cols = set()
        elif missing_cols:
            # Check for runtime dynamic probes (e.g. stop loss, trailing lines, execution prices recorded in on_bar)
            runtime_probe_keywords = ("stop", "trail", "price", "pos", "pnl", "entry", "exit", "target", "fill", "level", "signal")
            runtime_dynamic_cols = {
                col for col in missing_cols
                if any(kw in col.lower() for kw in runtime_probe_keywords)
            }
            if runtime_dynamic_cols:
                for col in runtime_dynamic_cols:
                    calculated_df[col] = 0.0
                missing_cols = missing_cols - runtime_dynamic_cols

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
            if isinstance(series, pd.DataFrame):
                all_nan_cols.append(col)
                continue
            # Ignore initial warmup, check the latter half of data
            latter_half = series.iloc[len(series) // 2 :]
            if bool(latter_half.isna().all()):
                all_nan_cols.append(col)
            numeric_vals = pd.to_numeric(series, errors="coerce")
            if bool(np.isinf(numeric_vals).any()):
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

        # Check that the strategy class implements event handlers
        event_handlers = [
            "on_bar",
            "on_bars",
            "on_quote_tick",
            "on_trade_tick",
            "on_data",
            "on_order_book",
            "on_order_book_depth",
        ]
        has_custom_handler = any(
            h in strategy_cls.__dict__
            or getattr(strategy_cls, h, None) != getattr(Strategy, h, None)
            for h in event_handlers
        )
        if not has_custom_handler:
            step = VerificationStepResult(
                level="L4",
                name="事件处理器实现",
                ok=False,
                message="Strategy 子类未实现任何自定义事件处理器 (如 on_bar, on_bars, on_quote_tick 等)",
            )
            steps.append(step)
            return VerificationResult(
                ok=False,
                summary="Strategy 缺少事件驱动实现 (L4 运行契约失败)",
                steps=steps,
                failed_level="L4",
                error_message="Strategy 类必须实现 on_bar(self, bar: Bar) 处理 K 线驱动逻辑",
                suggestion="在 Strategy 子类中定义 on_bar(self, bar: Bar) 方法。",
            )

        # Run in-memory event-driven backtest simulation with synthetic bars
        sim_ok, sim_err, sim_suggestion = _simulate_strategy_execution(
            strategy_cls=strategy_cls,
            config_instance=instantiated_config,
            manifest=manifest,
        )
        if not sim_ok:
            step = VerificationStepResult(
                level="L4",
                name="运行时沙盒事件驱动模拟 (on_start/on_bar)",
                ok=False,
                message=f"策略在合成 Bar 事件驱动测试中抛出异常: {sim_err}",
            )
            steps.append(step)
            return VerificationResult(
                ok=False,
                summary="Strategy 事件循环执行异常 (L4 运行契约失败)",
                steps=steps,
                failed_level="L4",
                error_message=sim_err,
                suggestion=sim_suggestion or "检查 on_start 和 on_bar 中的数据处理与下单逻辑。",
            )

        steps.append(
            VerificationStepResult(
                level="L4",
                name="NautilusTrader 运行时契约与沙盒模拟",
                ok=True,
                message="Strategy 实例化、生命周期钩子、合成 Bar 事件驱动模拟执行全部通过",
            )
        )

        return VerificationResult(
            ok=True,
            summary="策略代码 4 级 Pre-Flight 校验全部通过",
            steps=steps,
        )

    except Exception as unhandled_exc:
        logger.exception("Pre-Flight 验证过程发生未捕获异常: %s", unhandled_exc)
        step = VerificationStepResult(
            level="L2",
            name="沙盒验证运行时",
            ok=False,
            message=f"Pre-Flight 验证发生未捕获异常: {unhandled_exc}",
        )
        steps.append(step)
        return VerificationResult(
            ok=False,
            summary=f"Pre-Flight 沙盒验证未捕获异常: {unhandled_exc}",
            steps=steps,
            failed_level="L2",
            error_message=str(unhandled_exc),
            suggestion="检查策略代码是否存在非常规全局定义或类型冲突。",
        )
    finally:
        sys.modules.pop(module_name, None)
