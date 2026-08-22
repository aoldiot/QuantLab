from __future__ import annotations

from datetime import date
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from typing import Any

from nautilus_trader.backtest.config import MarginModelConfig
from nautilus_trader.config import (
    BacktestDataConfig,
    BacktestEngineConfig,
    BacktestRunConfig,
    BacktestVenueConfig,
    ImportableFeeModelConfig,
    ImportableFillModelConfig,
    ImportableStrategyConfig,
    LoggingConfig,
)
from nautilus_trader.model.data import Bar
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

from app.config import settings
from app.strategy_contract import StrategyMode, load_manifest, validate_parameters

FIXED_EXECUTION_MODEL = "CONSERVATIVE"
BINANCE_DEFAULT_FEES = {
    "spot": (Decimal("0.001"), Decimal("0.001")),
    "um": (Decimal("0.0002"), Decimal("0.0005")),
}


def timeframe_to_bar_spec(timeframe: str) -> str:
    units = {"m": "MINUTE", "h": "HOUR", "d": "DAY"}
    try:
        return f"{int(timeframe[:-1])}-{units[timeframe[-1]]}-LAST"
    except (KeyError, ValueError):
        raise ValueError(f"不支持的周期: {timeframe}") from None


def instrument_id(symbol: str, venue: str, market_type: str = "um") -> str:
    if "." in symbol:
        return symbol
    suffix = "-PERP" if market_type == "um" else ""
    return f"{symbol}{suffix}.{venue}"


def inclusive_day_end(value: str | date) -> str:
    day = date.fromisoformat(value) if isinstance(value, str) else value
    return f"{day}T23:59:59.999999999Z"


def _execution_fill_model(name: str) -> ImportableFillModelConfig:
    models = {
        "FAST": "BestPriceFillModel",
        "STANDARD": "ProbabilisticFillModel",
        "CONSERVATIVE": "OneTickSlippageFillModel",
    }
    # NT's ProbabilisticFillModel defaults to zero slippage probability.  Give
    # the standard preset an explicit, deterministic native one-tick chance.
    config: dict[str, Any] = {"random_seed": 42}
    if name == "STANDARD":
        config["prob_slippage"] = 0.5
    return ImportableFillModelConfig(
        fill_model_path=f"nautilus_trader.backtest.models:{models[name]}",
        config_path="nautilus_trader.backtest.config:FillModelConfig",
        config=config,
    )


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
    market_type = config.get("market_type", "um")
    if market_type == "um" and manifest.requires_funding:
        raise ValueError(
            "该策略要求资金费率，但 QuantLab 当前只向 NautilusTrader 提供 Bar 数据，"
            "无法原生结算资金费；为避免失真，已拒绝执行该永续回测"
        )
    requested_execution_model = config.get("execution_model", FIXED_EXECUTION_MODEL)
    if requested_execution_model != FIXED_EXECUTION_MODEL:
        raise ValueError(
            f"QuantLab 回测框架固定使用 NT {FIXED_EXECUTION_MODEL} 成交模型，"
            f"不支持 {requested_execution_model}"
        )
    ids = [
        instrument_id(symbol, config["venue"], market_type)
        for symbol in config["symbols"]
    ]
    catalog_reader = ParquetDataCatalog(str(catalog))
    catalog_instruments = {str(item.id): item for item in catalog_reader.instruments()}
    missing_instruments = [iid for iid in ids if iid not in catalog_instruments]
    if missing_instruments:
        raise ValueError(f"Catalog 缺少 Instrument：{', '.join(missing_instruments)}")
    instruments = [catalog_instruments[iid] for iid in ids]
    expected_maker, expected_taker = BINANCE_DEFAULT_FEES[market_type]
    fee_mismatch = [
        str(item.id)
        for item in instruments
        if Decimal(str(item.maker_fee)) != expected_maker
        or Decimal(str(item.taker_fee)) != expected_taker
    ]
    if fee_mismatch:
        raise ValueError(
            "Catalog 手续费必须是固定 Binance VIP0 默认费率 "
            f"(maker {expected_maker}, taker {expected_taker})：{', '.join(fee_mismatch)}"
        )
    mismatched = [
        str(item.id)
        for item in instruments
        if item.info.get("market_type") != market_type
    ]
    if mismatched:
        raise ValueError(
            f"Instrument 市场类型与回测配置不一致：{', '.join(mismatched)}"
        )
    zero_fee = [
        str(item.id)
        for item in instruments
        if item.maker_fee == 0 and item.taker_fee == 0
    ]
    if zero_fee:
        raise ValueError(
            f"Instrument 手续费为 0，请在数据管理中重新校准：{', '.join(zero_fee)}"
        )
    if market_type == "um":
        zero_margin = [
            str(item.id)
            for item in instruments
            if item.margin_init == 0 or item.margin_maint == 0
        ]
        if zero_margin:
            raise ValueError(
                f"永续合约保证金参数为 0，请在数据管理中重新校准：{', '.join(zero_margin)}"
            )
    if manifest.mode == StrategyMode.PORTFOLIO and "data_bar_types" in strategy_config_fields(manifest.config_path):
        required_timeframes = list(dict.fromkeys(manifest.timeframes))
        missing = set(required_timeframes) - set(config["timeframes"])
        if missing:
            raise ValueError(f"缺少策略要求的数据周期: {', '.join(sorted(missing))}")
    else:
        required_timeframes = list(dict.fromkeys(config.get("timeframes") or [manifest.primary_timeframe]))

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
        instance_configs = [
            {
                **parameters,
                "instrument_id": iid,
                "bar_type": f"{iid}-{primary_spec}-EXTERNAL",
                "order_id_tag": f"{index + 1:03d}",
            }
            for index, iid in enumerate(ids)
        ]
    for instance_config in instance_configs:
        strategies.append(
            ImportableStrategyConfig(
                strategy_path=manifest.strategy_path,
                config_path=manifest.config_path,
                config=instance_config,
            )
        )

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
            end_time=inclusive_day_end(config["end_date"]),
        )
        for timeframe in required_timeframes
    ]
    currencies = {
        str(item.settlement_currency if market_type == "um" else item.quote_currency)
        for item in instruments
    }
    if len(currencies) != 1:
        raise ValueError(
            f"同一回测账户暂不支持混合结算币种：{', '.join(sorted(currencies))}"
        )
    account_currency = next(iter(currencies))
    venue = BacktestVenueConfig(
        name=config["venue"],
        oms_type="HEDGING" if market_type == "um" else "NETTING",
        account_type="MARGIN" if market_type == "um" else "CASH",
        base_currency=account_currency if market_type == "um" else None,
        starting_balances=[f"{config['initial_balance']} {account_currency}"],
        default_leverage=config["leverage"] if market_type == "um" else 1.0,
        margin_model=MarginModelConfig(model_type="leveraged")
        if market_type == "um"
        else None,
        # Framework-wide fixed bar simulation: adaptive OHLC path plus NT's
        # one-tick slippage model.  Keeping one model makes runs comparable.
        bar_adaptive_high_low_ordering=True,
        fill_model=_execution_fill_model(FIXED_EXECUTION_MODEL),
        fee_model=ImportableFeeModelConfig(
            fee_model_path="nautilus_trader.backtest.models:MakerTakerFeeModel",
            config_path="nautilus_trader.backtest.config:MakerTakerFeeModelConfig",
            config={},
        ),
        # Use NautilusTrader's native margin-liquidation path.  The venue
        # liquidates when equity reaches maintenance margin (ratio=1.0).
        liquidation_enabled=market_type == "um",
        liquidation_trigger_ratio=1.0,
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
        end=inclusive_day_end(config["end_date"]),
        chunk_size=config.get("chunk_size"),
        raise_exception=True,
        dispose_on_completion=False,
    )
    return run, ids
