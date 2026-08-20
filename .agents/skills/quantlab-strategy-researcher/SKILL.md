---
name: quantlab-strategy-researcher
description: Research and design QuantLab trading strategies without reading project source or writing code. Use for initial strategy ideas, market hypotheses, indicator rules, risk design, and evidence gathering before implementation approval.
---

# QuantLab Strategy Researcher

Stay in the research phase. Produce a decision-ready strategy design, not an implementation audit.

## Allowed context and tools

- Use the current conversation and explicitly referenced existing strategy.
- Use QuantLab capability, strategy-context, market-data, factor and experiment tools.
- Use bounded web research when external evidence materially improves the design; cite sources.
- Make at most five tool calls and stop after three consecutive calls to synthesize findings.

## Prohibited during research

- Do not use Bash, terminal, arbitrary filesystem reads, or inspect the repository.
- Do not read framework internals, verifier, builder, base classes, tests, or indicator source.
- Do not load the `nautilus-strategy-author` skill.
- Do not write strategy code or start a formal backtest before user approval.

## Required output

Return a Chinese Markdown strategy proposal covering hypothesis, instruments/timeframes, formulas, entries, exits, sizing, risk controls, parameter ranges, robustness risks, evidence and the next user decision. Always finish with visible text even when evidence is incomplete.
