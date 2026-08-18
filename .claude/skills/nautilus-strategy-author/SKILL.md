---
name: nautilus-strategy-author
description: Write, modify, diagnose, and validate QuantLab NautilusTrader Python strategies. Use for natural-language requests to create trading strategies, change signals or risk controls, repair strategy code, interpret strategy-specific backtest results, or update STRATEGY_MANIFEST and chart indicators.
---

# Nautilus Strategy Author

Work only on the requested file under `backend/app/strategies/` and its strategy-specific tests. Treat other project files as read-only.

## 核心规范与代码文件头要求 (CRITICAL)

1. **第一行语法强约束**：
   - 必须且只能输出单一标准的 ```python ... ``` 完整代码块。
   - **代码第一行必须是 Python 导入语句（例如 `from decimal import Decimal`）**。
   - **严禁在代码块第一行输出任何中文解释、自然语言寒暄、文件路径标签（如 `:backend/app/...`、`[strategy.py]`、`# filepath:`）或嵌套重复的代码块标记（如 ```python）！**
2. **四大核心导出声明（不可遗漏）**：
   - `class <SlugPascalCase>Config(StrategyConfig, frozen=True)`
   - `class <SlugPascalCase>Strategy(Strategy)`
   - `calculate_indicators(df: pd.DataFrame, parameters: dict) -> pd.DataFrame`：必须计算并在返回的 DataFrame 中包含 `plot_config` 中声明的所有指标列，且使用 `.bfill().fillna(0.0)` 处理头部 NaN。
   - `STRATEGY_MANIFEST = StrategyManifest(...)`：`strategy_path` 与 `config_path` 必须带 `app.strategies.{slug}:` 前缀，`plot_config` 必须是双层嵌套字典规范。

## Workflow

1. Read the target strategy, `backend/app/strategy_contract.py`, and only the directly relevant tests.
2. Read [references/strategy-contract.md](references/strategy-contract.md) and [references/nautilus-api-reference.md](references/nautilus-api-reference.md) before changing the manifest or plotting contract.
3. State assumptions when the trading rule, sizing, or risk boundary is ambiguous.
4. Implement the smallest coherent change. Preserve existing behavior outside the request.
5. Keep configuration fields, manifest parameters, indicator calculation, and plot declarations consistent.
6. Run `python .claude/skills/nautilus-strategy-author/scripts/validate_strategy.py <strategy-file>`.
7. Run the relevant pytest tests and `ruff check` for changed Python files.
8. Summarize changed behavior, validation results, and trading risks. Do not claim profitability.

## Safety & Prohibited APIs

- ❌ 严禁调用 `self.portfolio.account_balance()`（使用 `self.portfolio.equity(self.instrument_id.venue)`）。
- ❌ 严禁调用 `self.portfolio.is_net_flat(...)`（使用 `self.portfolio.is_flat(self.instrument_id)`）。
- ❌ 严禁调用 `self.portfolio.position(...)`（使用 `self.portfolio.net_position(self.instrument_id)` 或 `self.portfolio.is_flat`）。
- ❌ 严禁调用 `self.close_position(...)`（使用 `self.close_all_positions(self.instrument_id)`）。
- ❌ 严禁调用 `self.instrument.round_quantity(...)`（使用 `self.instrument.make_qty(...)`）。
- ❌ 严禁向订单 `quantity` 传递裸 float/int（使用 `Quantity` 或 `self.instrument.make_qty(...)`）。
