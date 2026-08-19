"""QuantLab High-Level Strategy SDK.

Provides QuantLabStrategy, a batteries-included base strategy class for NautilusTrader
that eliminates Cython boilerplate, manages multi-timeframe caching, wraps clean order
submission, and provides runtime indicator probe recording for effortless plotting.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, PositionSide
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy


class QuantLabStrategy(Strategy):
    """Modernized high-level strategy base class for QuantLab strategies.

    Features:
    1. Zero-boilerplate trading primitives: `buy_market()`, `sell_market()`, `close_position()`.
    2. Seamless portfolio queries: `is_long()`, `is_short()`, `is_flat()`, `get_equity()`.
    3. Multi-timeframe bar caching and pandas Series extractors (`get_close_series()`, etc.).
    4. Runtime metric probe recording (`self.record("indicator_name", value)`) that eliminates
       the need to duplicate complex math in a separate calculate_indicators function!
    """

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self.instrument_id: InstrumentId = getattr(config, "instrument_id", None)
        self.bar_type: BarType = getattr(config, "bar_type", None)

        self.instrument = None
        self.bars: list[Bar] = []
        self.bars_map: dict[BarType, list[Bar]] = {}

        # Runtime metric probes for automatic chart rendering: {metric_name: [(ts_event, value), ...]}
        self.recorded_metrics: dict[str, list[tuple[int, float]]] = {}
        self._last_ts: int = 0

    def on_start(self) -> None:
        """Default start hook: resolves instrument and subscribes to declared bar types."""
        if self.instrument_id:
            self.instrument = self.cache.instrument(self.instrument_id)

        # Auto-subscribe single or multiple bar types
        if self.bar_type:
            self.subscribe_bars(self.bar_type)

        bar_types = getattr(self.config, "bar_types", None)
        if bar_types and isinstance(bar_types, (list, tuple)):
            for bt in bar_types:
                self.subscribe_bars(bt)

    def on_bar(self, bar: Bar) -> None:
        """Default on_bar: records bar into rolling lists and tracks timestamp."""
        self._last_ts = bar.ts_event
        self.bars.append(bar)
        if bar.bar_type not in self.bars_map:
            self.bars_map[bar.bar_type] = []
        self.bars_map[bar.bar_type].append(bar)

    def on_stop(self) -> None:
        """Default stop hook: unsubscribes bars."""
        if self.bar_type:
            self.unsubscribe_bars(self.bar_type)

        bar_types = getattr(self.config, "bar_types", None)
        if bar_types and isinstance(bar_types, (list, tuple)):
            for bt in bar_types:
                self.unsubscribe_bars(bt)

    # =========================================================================
    # Runtime Metric Probes (Eliminates duplicate calculate_indicators)
    # =========================================================================

    def record(self, name: str, value: float | int | bool, ts_event: int | None = None) -> None:
        """Record an arbitrary calculated indicator or metric value on the current bar.

        The recorded values are automatically harvested by the QuantLab chart engine
        without needing a separate vectorized calculate_indicators function!
        """
        ts = ts_event if ts_event is not None else self._last_ts
        if name not in self.recorded_metrics:
            self.recorded_metrics[name] = []
        try:
            val_float = float(value)
        except Exception:
            val_float = 0.0
        self.recorded_metrics[name].append((ts, val_float))

    def get_recorded_metrics(self) -> dict[str, list[tuple[int, float]]]:
        """Return all recorded metric series."""
        return self.recorded_metrics

    # =========================================================================
    # High-Level Trading Primitives
    # =========================================================================

    def make_qty(self, amount: float | Decimal | int | str) -> Quantity:
        """Convert float/int/Decimal to a valid Quantity conforming to instrument precision."""
        if self.instrument is None and self.instrument_id:
            self.instrument = self.cache.instrument(self.instrument_id)

        dec_amt = Decimal(str(amount)) if not isinstance(amount, Decimal) else amount
        if self.instrument and hasattr(self.instrument, "make_qty"):
            return self.instrument.make_qty(dec_amt)
        return Quantity.from_str(str(round(float(dec_amt), 8)))

    def get_equity(self, venue: Venue | str | None = None) -> float:
        """Get total account equity on the primary venue."""
        v = venue or (self.instrument_id.venue if self.instrument_id else None) or Venue("BINANCE")
        if isinstance(v, str):
            v = Venue(v)
        try:
            eq_dict = self.portfolio.equity(v)
            if eq_dict:
                return float(sum(m.as_double() for m in eq_dict.values()))
        except Exception:
            pass
        return 10000.0

    def is_long(self, instrument_id: InstrumentId | str | None = None) -> bool:
        """Check if currently net long the instrument."""
        iid = self._resolve_instrument_id(instrument_id)
        if not iid or getattr(self, "portfolio", None) is None:
            return False
        return self.portfolio.is_net_long(iid)

    def is_short(self, instrument_id: InstrumentId | str | None = None) -> bool:
        """Check if currently net short the instrument."""
        iid = self._resolve_instrument_id(instrument_id)
        if not iid or getattr(self, "portfolio", None) is None:
            return False
        return self.portfolio.is_net_short(iid)

    def is_flat(self, instrument_id: InstrumentId | str | None = None) -> bool:
        """Check if currently flat (no position) on the instrument."""
        iid = self._resolve_instrument_id(instrument_id)
        if not iid or getattr(self, "portfolio", None) is None:
            return True
        return self.portfolio.is_flat(iid)

    def get_net_position(self, instrument_id: InstrumentId | str | None = None) -> float:
        """Return net position size as float (positive for long, negative for short, 0.0 for flat)."""
        iid = self._resolve_instrument_id(instrument_id)
        if not iid or getattr(self, "portfolio", None) is None:
            return 0.0
        pos = self.portfolio.net_position(iid)
        if pos is None:
            return 0.0
        signed_qty = pos.signed_qty() if hasattr(pos, "signed_qty") else pos.quantity.as_double()
        return float(signed_qty)


    def buy_market(
        self,
        trade_size: float | Decimal | int | str | None = None,
        pct: float | None = None,
        leverage: float = 1.0,
        instrument_id: InstrumentId | str | None = None,
    ) -> Any:
        """Submit a Market BUY order, automatically computing quantity and respecting precision."""
        iid = self._resolve_instrument_id(instrument_id)
        qty = self._resolve_order_quantity(trade_size, pct, leverage, iid)
        order = self.order_factory.market(
            instrument_id=iid,
            order_side=OrderSide.BUY,
            quantity=qty,
        )
        self.submit_order(order)
        return order

    def sell_market(
        self,
        trade_size: float | Decimal | int | str | None = None,
        pct: float | None = None,
        leverage: float = 1.0,
        instrument_id: InstrumentId | str | None = None,
    ) -> Any:
        """Submit a Market SELL order, automatically computing quantity and respecting precision."""
        iid = self._resolve_instrument_id(instrument_id)
        qty = self._resolve_order_quantity(trade_size, pct, leverage, iid)
        order = self.order_factory.market(
            instrument_id=iid,
            order_side=OrderSide.SELL,
            quantity=qty,
        )
        self.submit_order(order)
        return order

    def close_position(self, instrument_id: InstrumentId | str | None = None) -> None:
        """Close all open positions for the instrument."""
        iid = self._resolve_instrument_id(instrument_id)
        if iid and not self.is_flat(iid):
            self.close_all_positions(iid)

    # =========================================================================
    # Pandas Series Extractors (For effortless vectorized indicators in on_bar)
    # =========================================================================

    def _get_bar_list(self, bar_type: BarType | None = None) -> list[Bar]:
        if bar_type is not None:
            return self.bars_map.get(bar_type, [])
        return self.bars

    def get_close_series(self, bar_type: BarType | None = None) -> pd.Series:
        """Return pandas Series of close prices."""
        bars = self._get_bar_list(bar_type)
        return pd.Series([b.close.as_double() for b in bars], dtype=float)

    def get_high_series(self, bar_type: BarType | None = None) -> pd.Series:
        """Return pandas Series of high prices."""
        bars = self._get_bar_list(bar_type)
        return pd.Series([b.high.as_double() for b in bars], dtype=float)

    def get_low_series(self, bar_type: BarType | None = None) -> pd.Series:
        """Return pandas Series of low prices."""
        bars = self._get_bar_list(bar_type)
        return pd.Series([b.low.as_double() for b in bars], dtype=float)

    def get_open_series(self, bar_type: BarType | None = None) -> pd.Series:
        """Return pandas Series of open prices."""
        bars = self._get_bar_list(bar_type)
        return pd.Series([b.open.as_double() for b in bars], dtype=float)

    def get_volume_series(self, bar_type: BarType | None = None) -> pd.Series:
        """Return pandas Series of volume."""
        bars = self._get_bar_list(bar_type)
        return pd.Series([b.volume.as_double() for b in bars], dtype=float)

    def get_df(self, bar_type: BarType | None = None) -> pd.DataFrame:
        """Return full OHLCV DataFrame."""
        bars = self._get_bar_list(bar_type)
        if not bars:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return pd.DataFrame({
            "open": [b.open.as_double() for b in bars],
            "high": [b.high.as_double() for b in bars],
            "low": [b.low.as_double() for b in bars],
            "close": [b.close.as_double() for b in bars],
            "volume": [b.volume.as_double() for b in bars],
        })

    # =========================================================================
    # Internal Helpers
    # =========================================================================

    def _resolve_instrument_id(self, instrument_id: InstrumentId | str | None) -> InstrumentId | None:
        if instrument_id is None:
            return self.instrument_id
        if isinstance(instrument_id, str):
            return InstrumentId.from_str(instrument_id)
        return instrument_id

    def _resolve_order_quantity(
        self,
        trade_size: float | Decimal | int | str | None,
        pct: float | None,
        leverage: float,
        instrument_id: InstrumentId | None,
    ) -> Quantity:
        if trade_size is not None:
            return self.make_qty(trade_size)

        if pct is not None:
            equity = self.get_equity()
            notional = equity * float(pct) * float(leverage)
            last_price = self.bars[-1].close.as_double() if self.bars else 1.0
            raw_qty = notional / last_price if last_price > 0 else 0.001
            return self.make_qty(raw_qty)

        # Fallback to config trade_size if declared
        config_size = getattr(self.config, "trade_size", None)
        if config_size is not None:
            return self.make_qty(config_size)

        return self.make_qty(0.01)
