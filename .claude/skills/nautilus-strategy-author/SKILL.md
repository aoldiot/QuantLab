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
5. Keep configuration fields, manifest parameters, indicator calculation, and plot declarations consistent.
6. Run `python .claude/skills/nautilus-strategy-author/scripts/validate_strategy.py <strategy-file>`.
7. Run the relevant pytest tests and `ruff check` for changed Python files.
8. Summarize changed behavior, validation results, and trading risks. Do not claim profitability.

## Safety

- Never read `.env`, credentials, SSH files, or files outside the assigned worktree.
- Never run a backtest from Bash. Ask QuantLab to create a recorded backtest with explicit parameters.
- Never publish, commit, or push unless the user explicitly confirms the corresponding product action.
- Never add look-ahead data, future leakage, fabricated fills, or hidden fallback market data.
- Preserve deterministic behavior and reject invalid parameters early.
