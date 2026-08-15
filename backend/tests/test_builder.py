from nautilus_trader.config import BacktestDataConfig
from nautilus_trader.model.data import Bar
from nautilus_trader.trading.config import StrategyFactory

from app.backtests.builder import build_run_config, strategy_config_fields, timeframe_to_bar_spec


def test_timeframe_to_bar_spec():
    assert timeframe_to_bar_spec("15m") == "15-MINUTE-LAST"
    assert timeframe_to_bar_spec("4h") == "4-HOUR-LAST"


def test_bar_data_config_keeps_timeframe_in_catalog_identifier():
    config = BacktestDataConfig(
        catalog_path="/tmp/catalog",
        data_cls=Bar,
        instrument_ids=["BTCUSDT-PERP.BINANCE"],
        bar_spec="1-HOUR-LAST",
    )

    assert config.query["identifiers"] == [
        "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
    ]


def test_builds_real_importable_strategy(tmp_path):
    payload = {
        "config": {
            "strategy_parameters": {}, "catalog_path": str(tmp_path),
            "symbols": ["BTCUSDT"], "venue": "BINANCE", "timeframes": ["15m"],
            "start_date": "2024-01-01", "end_date": "2024-02-01",
            "initial_balance": 10_000, "leverage": 4, "execution_model": "CONSERVATIVE",
            "chunk_size": None,
        },
        "strategy": {"module": "app.strategies.atr_trend"},
    }
    config, _ = build_run_config(payload)
    strategy = StrategyFactory.create(config.engine.strategies[0])
    assert str(strategy.id) == "ATRTrendStrategy-001"
    assert str(strategy.config.instrument_id) == "BTCUSDT-PERP.BINANCE"


def test_portfolio_mode_builds_one_strategy_for_entire_universe(tmp_path):
    symbols = [f"COIN{i}USDT" for i in range(100)]
    payload = {
        "config": {
            "strategy_parameters": {}, "catalog_path": str(tmp_path),
            "symbols": symbols, "venue": "BINANCE", "timeframes": ["1h"],
            "start_date": "2024-01-01", "end_date": "2024-02-01",
            "initial_balance": 10_000, "leverage": 4, "execution_model": "CONSERVATIVE",
            "chunk_size": None,
        },
        "strategy": {"module": "app.strategies.momentum_rotation"},
    }

    config, instrument_ids = build_run_config(payload)

    assert len(config.engine.strategies) == 1
    assert len(instrument_ids) == 100
    strategy = StrategyFactory.create(config.engine.strategies[0])
    assert str(strategy.id) == "MomentumRotationStrategy-001"
    assert len(strategy.config.instrument_ids) == 100
    assert len(strategy.config.bar_types) == 100


def test_detects_optional_portfolio_config_fields():
    fields = strategy_config_fields(
        "app.strategies.macd_cross_ma_atr_chop_filter_trend:MacdCrossMaAtrChopFilterTrendConfig"
    )
    assert "bar_types" in fields
    assert "data_bar_types" not in fields
