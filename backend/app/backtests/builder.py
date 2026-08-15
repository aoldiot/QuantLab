from __future__ import annotations

from pathlib import Path
from typing import Any
from importlib import import_module

from nautilus_trader.config import (
    BacktestDataConfig,
    BacktestEngineConfig,
    BacktestRunConfig,
    BacktestVenueConfig,
    ImportableFeeModelConfig,
    ImportableStrategyConfig,
    LoggingConfig,
)
from nautilus_trader.model.data import Bar

from app.config import settings
from app.strategy_contract import StrategyMode, load_manifest, validate_parameters


def timeframe_to_bar_spec(timeframe: str) -> str:
    units = {"m": "MINUTE", "h": "HOUR", "d": "DAY"}
    try:
        return f"{int(timeframe[:-1])}-{units[timeframe[-1]]}-LAST"
    except (KeyError, ValueError):
        raise ValueError(f"不支持的周期: {timeframe}") from None


def instrument_id(symbol: str, venue: str) -> str:
    if "." in symbol:
        return symbol
    return settings.instrument_id_template.format(symbol=symbol, venue=venue)


def strategy_config_fields(config_path: str) -> set[str]:
    module_path, class_name = config_path.split(":", 1)
    config_class = getattr(import_module(module_path), class_name)
    return set(getattr(config_class, "__struct_fields__", ()))


def build_run_config(payload: dict[str, Any]) -> tuple[BacktestRunConfig, list[str]]:
    config = payload["config"]
    strategy_info = payload["strategy"]
    manifest = load_manifest(strategy_info["module"])
    parameters = validate_parameters(manifest, config["strategy_parameters"])
    catalog = Path(config.get("catalog_path") or settings.catalog_path).resolve()
    if not catalog.exists():
        raise FileNotFoundError(f"Nautilus Catalog 不存在: {catalog}")
    ids = [instrument_id(symbol, config["venue"]) for symbol in config["symbols"]]
    required_timeframes = list(dict.fromkeys(manifest.timeframes))
    missing = set(required_timeframes) - set(config["timeframes"])
    if missing:
        raise ValueError(f"缺少策略要求的数据周期: {', '.join(sorted(missing))}")

    strategies = []
    primary_spec = timeframe_to_bar_spec(manifest.primary_timeframe)
    if manifest.mode == StrategyMode.PORTFOLIO:
        portfolio_config = {
            **parameters,
            "instrument_ids": ids,
            "bar_types": [f"{iid}-{primary_spec}-EXTERNAL" for iid in ids],
            "order_id_tag": "001",
        }
        if "data_bar_types" in strategy_config_fields(manifest.config_path):
            portfolio_config["data_bar_types"] = [
                f"{iid}-{timeframe_to_bar_spec(timeframe)}-EXTERNAL"
                for timeframe in required_timeframes
                for iid in ids
            ]
        instance_configs = [portfolio_config]
    else:
        instance_configs = [{
            **parameters,
            "instrument_id": iid,
            "bar_type": f"{iid}-{primary_spec}-EXTERNAL",
            "order_id_tag": f"{index + 1:03d}",
        } for index, iid in enumerate(ids)]
    for instance_config in instance_configs:
        strategies.append(ImportableStrategyConfig(
            strategy_path=manifest.strategy_path,
            config_path=manifest.config_path,
            config=instance_config,
        ))

    data = [
        BacktestDataConfig(
            catalog_path=str(catalog),
            # NautilusTrader 1.227.0 checks ``self.data_cls is Bar`` when it
            # builds BarType identifiers. Passing the import-path string makes
            # it silently drop ``bar_spec`` and scan every timeframe for the
            # instrument, which can then fail while merging Parquet precision
            # metadata from unrelated bar directories.
            data_cls=Bar,
            instrument_ids=ids,
            bar_spec=timeframe_to_bar_spec(timeframe),
            start_time=f"{config['start_date']}T00:00:00Z",
            end_time=f"{config['end_date']}T23:59:59Z",
        )
        for timeframe in required_timeframes
    ]
    venue = BacktestVenueConfig(
        name=config["venue"],
        oms_type="HEDGING",
        account_type="MARGIN",
        base_currency="USDT",
        starting_balances=[f"{config['initial_balance']} USDT"],
        default_leverage=config["leverage"],
        bar_adaptive_high_low_ordering=config["execution_model"] != "FAST",
        fee_model=ImportableFeeModelConfig(
            fee_model_path="nautilus_trader.backtest.models:MakerTakerFeeModel",
            config_path="nautilus_trader.backtest.config:MakerTakerFeeModelConfig",
            config={},
        ),
    )
    run = BacktestRunConfig(
        venues=[venue],
        data=data,
        engine=BacktestEngineConfig(
            trader_id="QUANTLAB-001",
            logging=LoggingConfig(log_level="INFO"),
            strategies=strategies,
            run_analysis=True,
        ),
        start=f"{config['start_date']}T00:00:00Z",
        end=f"{config['end_date']}T23:59:59Z",
        chunk_size=config.get("chunk_size"),
        raise_exception=True,
        dispose_on_completion=False,
    )
    return run, ids
