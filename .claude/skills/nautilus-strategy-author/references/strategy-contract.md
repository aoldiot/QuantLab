# QuantLab strategy contract

Every strategy module must export:

- a `StrategyConfig` subclass;
- a `Strategy` subclass;
- `calculate_indicators(dataframe, parameters)` returning a DataFrame with unchanged row count;
- `STRATEGY_MANIFEST` as a `StrategyManifest` instance.

The manifest must use importable `strategy_path` and `config_path`, declare every user parameter as a `ParameterSpec`, and declare at least one plotted indicator. Every column referenced by `plot_config.main_plot` or `plot_config.subplots` must be created by `calculate_indicators` and be numeric-coercible.

Use `SINGLE_INSTRUMENT` for one strategy instance per instrument. Use `PORTFOLIO` when one instance coordinates multiple instruments. Keep `timeframes`, `primary_timeframe`, `supports_short`, and `requires_funding` truthful.

Risk behavior must be explicit: order sizing, duplicate-order prevention, position checks, entry conditions, exit conditions, and stop behavior. Do not silently change defaults or parameter ranges.
