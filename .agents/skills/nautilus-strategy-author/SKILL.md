---
name: nautilus-strategy-author
description: Write, modify, diagnose, and validate QuantLab NautilusTrader Python strategies. Use for natural-language requests to create trading strategies, change signals or risk controls, repair strategy code, interpret strategy-specific backtest results, or update STRATEGY_MANIFEST and chart indicators.
---

# Nautilus Strategy Author

Work only on the requested file under `backend/app/strategies/` and its strategy-specific tests. Treat other project files as read-only.

## Workflow

1. Read the target strategy, `backend/app/strategy_contract.py`, and only the directly relevant tests.
2. Read [references/strategy-contract.md](references/strategy-contract.md) before changing the manifest or plotting contract.
3. State assumptions when the trading rule, sizing, or risk boundary is ambiguous.
4. Implement the smallest coherent change. Preserve existing behavior outside the request.
5. Keep configuration fields, manifest parameters, indicator calculation, and plot declarations consistent:
   - `class XxxConfig(StrategyConfig, frozen=True)`
   - `class XxxStrategy(Strategy)`
   - `calculate_indicators(df, parameters) -> pd.DataFrame` with `.bfill()` / `.fillna(0.0)` for warmup
   - `STRATEGY_MANIFEST = StrategyManifest(...)` with `app.strategies.{slug}:` prefix for paths and two-level dict for `plot_config`.
6. Run `python .claude/skills/nautilus-strategy-author/scripts/validate_strategy.py <strategy-file>`.
7. Run the relevant pytest tests and `ruff check` for changed Python files.
8. Summarize changed behavior, validation results, and trading risks. Do not claim profitability.
