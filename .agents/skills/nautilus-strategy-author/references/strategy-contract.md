# QuantLab Strategy Contract Specification

Every strategy Python module under `backend/app/strategies/<slug>.py` must export exactly 4 core elements:

1. **`StrategyConfig` Subclass**:
   - Must inherit from `nautilus_trader.config.StrategyConfig` with `frozen=True`.
   - Must define `instrument_id: InstrumentId` (or `str`) and `bar_type: BarType` (or `str`).
   - Must declare strategy trading parameters (`trade_size: Decimal`, periods, thresholds) matching `STRATEGY_MANIFEST.parameters`.

2. **`Strategy` Subclass**:
   - Must inherit from `nautilus_trader.trading.strategy.Strategy`.
   - `__init__(self, config: XxxConfig)` must call `super().__init__(config)` and initialize variables.
   - `on_start(self)` subscribes to bars via `self.subscribe_bars(self.bar_type)`.
   - `on_bar(self, bar: Bar)` processes bar data and handles trading logic.
   - Order submission: use `self.order_factory.market(instrument_id=..., order_side=..., quantity=self.instrument.make_qty(...))`.
   - Position closure: use `self.close_all_positions(self.instrument_id)`.

3. **`calculate_indicators(df: pd.DataFrame, parameters: dict) -> pd.DataFrame`**:
   - Returns a DataFrame with the EXACT same length as the input (never call `dropna()` or truncate).
   - Must compute and assign every column declared in `STRATEGY_MANIFEST.plot_config`.
   - Use `.bfill().fillna(0.0)` for rolling/ewm indicators to prevent lingering NaNs after warmup.
   - Guard against division by zero using `.replace(0, np.nan)` or `clip`.

4. **`STRATEGY_MANIFEST = StrategyManifest(...)`**:
   - `slug`: lowercase snake_case identifier matching file basename.
   - `name`: Human-readable Chinese title.
   - `strategy_path`: `"app.strategies.<slug>:<PascalCaseClass>Strategy"`.
   - `config_path`: `"app.strategies.<slug>:<PascalCaseClass>Config"`.
   - `parameters`: Dict of `ParameterSpec(title=..., type=..., default=..., minimum=..., maximum=...)` where `minimum <= default <= maximum`.
   - `timeframes`: Tuple of supported timeframes, e.g. `("15m", "1h", "4h", "1d")`.
   - `primary_timeframe`: Default timeframe, must be included in `timeframes`.
   - `plot_config`: Two-level dict:
     - `main_plot`: `{"close": {"type": "line", "color": "#ffffff"}, ...}`
     - `subplots`: `{"SubplotTitle": {"indicator_col": {"type": "line", "color": "#ff55ff"}}}`
