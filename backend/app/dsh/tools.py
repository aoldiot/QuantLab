from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.quant.backtest import run_nautilus_backtest
from app.quant.experiment import run_vectorized_experiment
from app.quant.factor_analysis import compute_technical_factor, evaluate_factor
from app.quant.market_data import (
    compute_market_stats,
    get_catalog_instruments,
    load_market_bars,
)
from app.quant.parameter_sweep import run_parameter_sweep
from app.quant.robustness import (
    run_monte_carlo_stress_test,
    run_walk_forward_analysis,
)
from app.quant.strategy_manager import (
    ensure_strategy_db_record,
    get_strategy_code,
    save_strategy_file,
    verify_strategy_code,
)

logger = logging.getLogger(__name__)

DSH_TOOL_DEFINITIONS = [
    {
        "name": "quant_market_data_query",
        "description": "查询 QuantLab 本地 Parquet 行情数据目录中的标的、时间周期、数据跨度及历史行情统计特征",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list_instruments", "get_market_stats", "load_bars"],
                    "description": "操作类型：list_instruments(查询可用标的及周期), get_market_stats(获取标的统计特征), load_bars(加载OHLCV数据摘要)",
                },
                "symbol": {"type": "string", "description": "交易标的，例如 BTCUSDT"},
                "timeframe": {"type": "string", "description": "K线周期，如 1h, 4h, 1d, 15m"},
                "start_date": {"type": "string", "description": "起始日期，格式 YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "结束日期，格式 YYYY-MM-DD"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "quant_factor_analysis",
        "description": "在真实历史行情上执行量化 Alpha 因子计算、IC / Rank IC 统计检验、分位数收益率利差及因子衰减评估",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "标的代号，如 BTCUSDT"},
                "timeframe": {"type": "string", "description": "K线周期，如 1h, 4h, 1d, 15m"},
                "factor_name": {
                    "type": "string",
                    "description": "因子名称，例如 momentum, ema_spread, rsi, atr, bollinger_pct_b, macd_hist, volatility_ratio, volume_price_trend",
                },
                "factor_params": {
                    "type": "object",
                    "description": "因子计算参数字典，例如 {\"fast_period\": 12, \"slow_period\": 26}",
                },
                "forward_periods": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "前向收益率周期列表（以Bar为单位），默认 [1, 5, 10, 20]",
                },
                "quantiles": {"type": "integer", "description": "分位数分组数，默认 5"},
            },
            "required": ["symbol", "timeframe", "factor_name"],
        },
    },
    {
        "name": "quant_run_experiment",
        "description": "在历史行情上运行高速向量化策略假设实验，快速验证策略假设收益率、夏普比率、最大回撤、胜率和盈亏比",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "标的代号，如 BTCUSDT"},
                "timeframe": {"type": "string", "description": "K线周期，如 1h, 4h, 1d, 15m"},
                "factor_name": {
                    "type": "string",
                    "description": "信号驱动因子名称，例如 ema_spread, momentum, rsi, bollinger_pct_b",
                },
                "factor_params": {
                    "type": "object",
                    "description": "因子参数字典",
                },
                "threshold_long": {"type": "number", "description": "做多信号阈值，默认 0.0"},
                "threshold_short": {"type": "number", "description": "做空信号阈值，默认 None（对称阈值）"},
                "allow_short": {"type": "boolean", "description": "是否允许做空，默认 True"},
                "initial_capital": {"type": "number", "description": "初始资金，默认 10000.0"},
            },
            "required": ["symbol", "timeframe", "factor_name"],
        },
    },
    {
        "name": "quant_save_strategy_code",
        "description": "保存或更新 NautilusTrader 策略 Python 源代码到 backend/app/strategies/{strategy_name}.py",
        "parameters": {
            "type": "object",
            "properties": {
                "strategy_name": {"type": "string", "description": "策略标识符（如 btc_ema_atr）"},
                "code": {"type": "string", "description": "完整的 Python 源代码"},
            },
            "required": ["strategy_name", "code"],
        },
    },
    {
        "name": "quant_preflight_verify",
        "description": "执行 QuantLab 4 级 Pre-Flight 运行期沙盒验证（L1 语法结构 -> L2 契约参数 -> L3 指标覆盖与NaN检测 -> L4 Nautilus 实例化）",
        "parameters": {
            "type": "object",
            "properties": {
                "strategy_name": {"type": "string", "description": "策略标识符"},
            },
            "required": ["strategy_name"],
        },
    },
    {
        "name": "quant_execute_backtest",
        "description": "执行确定性的 NautilusTrader 事件驱动正式回测，并获取完整绩效指标（收益率、夏普、最大回撤、交易记录）",
        "parameters": {
            "type": "object",
            "properties": {
                "strategy_name": {"type": "string", "description": "策略标识符"},
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "交易标的列表，例如 [\"BTCUSDT\"]",
                },
                "start_date": {"type": "string", "description": "回测起始日期 YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "回测结束日期 YYYY-MM-DD"},
                "initial_balance": {"type": "number", "description": "初始资金，默认 10000.0"},
                "leverage": {"type": "number", "description": "杠杆倍数，默认 1.0"},
                "parameters": {"type": "object", "description": "策略参数覆盖字典"},
            },
            "required": ["strategy_name", "symbols", "start_date", "end_date"],
        },
    },
    {
        "name": "quant_parameter_sweep",
        "description": "在历史数据上执行多维参数敏感性扫描与过拟合分析，检测参数高原与脆弱断崖",
        "parameters": {
            "type": "object",
            "properties": {
                "factor_name": {"type": "string", "description": "因子或策略类型"},
                "param_grid": {
                    "type": "object",
                    "description": "参数网格字典，例如 {\"fast_period\": [8, 12, 16], \"slow_period\": [21, 26, 35]}",
                },
                "symbol": {"type": "string", "description": "标的代号，如 BTCUSDT"},
                "timeframe": {"type": "string", "description": "K线周期，如 1h"},
                "start_date": {"type": "string", "description": "起始日期"},
                "end_date": {"type": "string", "description": "结束日期"},
            },
            "required": ["symbol", "timeframe"],
        },
    },
    {
        "name": "quant_robustness_test",
        "description": "执行策略稳健性评估：包括 Walk-Forward 样本外向前推进检验、Monte Carlo 蒙特卡洛极端重抽样压力测试与 Deflated Sharpe Ratio 检验",
        "parameters": {
            "type": "object",
            "properties": {
                "test_type": {
                    "type": "string",
                    "enum": ["walk_forward", "monte_carlo", "all"],
                    "description": "稳健性测试类型：walk_forward(向前推进测试), monte_carlo(蒙特卡洛压力测试), all(全套稳健性测试)",
                },
                "symbol": {"type": "string", "description": "标的代号，如 BTCUSDT"},
                "timeframe": {"type": "string", "description": "K线周期，如 1h"},
                "factor_name": {"type": "string", "description": "因子名称，默认 ema_spread"},
                "factor_params": {"type": "object", "description": "因子参数字典"},
                "start_date": {"type": "string", "description": "起始日期"},
                "end_date": {"type": "string", "description": "结束日期"},
                "n_splits": {"type": "integer", "description": "Walk-Forward 切分段数，默认 4"},
                "n_simulations": {"type": "integer", "description": "蒙特卡洛模拟次数，默认 1000"},
            },
            "required": ["symbol", "timeframe"],
        },
    },
    {
        "name": "quant_get_strategy",
        "description": "获取指定策略的 Python 源代码与状态",
        "parameters": {
            "type": "object",
            "properties": {
                "strategy_name": {"type": "string", "description": "策略标识符"},
            },
            "required": ["strategy_name"],
        },
    },
]


async def dispatch_dsh_tool_call(
    tool_name: str,
    arguments: dict[str, Any],
    project_id: str | None = None,
    db: AsyncSession | None = None,
) -> dict[str, Any]:
    """Execute a QuantLab deterministic tool requested via DSH Tool Calling interface."""
    logger.info("DSH 工具调度: %s, 参数: %s", tool_name, arguments)

    if tool_name == "quant_market_data_query":
        action = arguments.get("action", "list_instruments")
        if action == "list_instruments":
            instruments = get_catalog_instruments()
            return {
                "ok": True,
                "action": action,
                "instruments": instruments,
                "total_instruments": len(instruments),
            }
        elif action == "get_market_stats":
            symbol = arguments.get("symbol", "BTCUSDT")
            timeframe = arguments.get("timeframe", "1h")
            df = load_market_bars(
                symbol=symbol,
                timeframe=timeframe,
                start_date=arguments.get("start_date"),
                end_date=arguments.get("end_date"),
            )
            stats = compute_market_stats(df)
            return {
                "ok": True,
                "action": action,
                "symbol": symbol,
                "timeframe": timeframe,
                "market_statistics": stats,
            }
        elif action == "load_bars":
            symbol = arguments.get("symbol", "BTCUSDT")
            timeframe = arguments.get("timeframe", "1h")
            df = load_market_bars(
                symbol=symbol,
                timeframe=timeframe,
                start_date=arguments.get("start_date"),
                end_date=arguments.get("end_date"),
            )
            if df.empty:
                return {"ok": False, "error": f"未找到标的 {symbol} 的行情数据"}
            return {
                "ok": True,
                "action": action,
                "symbol": symbol,
                "timeframe": timeframe,
                "total_bars": len(df),
                "start_time": str(df.index[0]),
                "end_time": str(df.index[-1]),
                "latest_close": float(df["close"].iloc[-1]),
            }

    elif tool_name == "quant_factor_analysis":
        symbol = arguments.get("symbol", "BTCUSDT")
        timeframe = arguments.get("timeframe", "1h")
        factor_name = arguments.get("factor_name", "ema_spread")
        factor_params = arguments.get("factor_params", {})
        forward_periods = arguments.get("forward_periods", [1, 5, 10, 20])
        quantiles = arguments.get("quantiles", 5)

        df = load_market_bars(symbol=symbol, timeframe=timeframe)
        if df.empty:
            return {"ok": False, "error": f"未能加载标的 {symbol} 的数据"}

        factor_series = compute_technical_factor(df, factor_name, factor_params)
        eval_res = evaluate_factor(
            df=df,
            factor_series=factor_series,
            forward_periods=forward_periods,
            quantiles=quantiles,
        )
        return {
            "ok": True,
            "symbol": symbol,
            "timeframe": timeframe,
            "factor_name": factor_name,
            "factor_analysis": eval_res,
        }

    elif tool_name == "quant_run_experiment":
        symbol = arguments.get("symbol", "BTCUSDT")
        timeframe = arguments.get("timeframe", "1h")
        factor_name = arguments.get("factor_name", "ema_spread")
        factor_params = arguments.get("factor_params", {})
        threshold_long = float(arguments.get("threshold_long", 0.0))
        threshold_short = (
            float(arguments["threshold_short"])
            if arguments.get("threshold_short") is not None
            else None
        )
        allow_short = bool(arguments.get("allow_short", True))
        initial_capital = float(arguments.get("initial_capital", 10000.0))

        df = load_market_bars(symbol=symbol, timeframe=timeframe)
        if df.empty:
            return {"ok": False, "error": f"未能加载标的 {symbol} 的数据"}

        exp_res = run_vectorized_experiment(
            df=df,
            factor_name=factor_name,
            factor_params=factor_params,
            threshold_long=threshold_long,
            threshold_short=threshold_short,
            allow_short=allow_short,
            initial_capital=initial_capital,
        )
        return {
            "ok": True,
            "symbol": symbol,
            "timeframe": timeframe,
            "experiment_result": exp_res,
        }

    elif tool_name == "quant_save_strategy_code":
        strategy_name = arguments.get("strategy_name", "")
        code = arguments.get("code", "")
        try:
            saved_path = save_strategy_file(strategy_name, code)
            v_res = verify_strategy_code(strategy_name, saved_path)
            if db is not None:
                await ensure_strategy_db_record(strategy_name, db, project_id=project_id)
            return {
                "ok": True,
                "strategy_name": strategy_name,
                "saved_path": str(saved_path),
                "verification": v_res,
            }
        except Exception as exc:
            return {"ok": False, "error": f"保存策略文件失败: {exc}"}

    elif tool_name == "quant_preflight_verify":
        strategy_name = arguments.get("strategy_name", "")
        v_res = verify_strategy_code(strategy_name)
        return {
            "ok": True,
            "strategy_name": strategy_name,
            "verification": v_res,
        }

    elif tool_name == "quant_execute_backtest":
        return await run_nautilus_backtest(
            strategy_name=arguments.get("strategy_name", ""),
            symbols=arguments.get("symbols", ["BTCUSDT"]),
            start_date=str(arguments.get("start_date", "2024-01-01")),
            end_date=str(arguments.get("end_date", "2024-06-30")),
            initial_balance=float(arguments.get("initial_balance", 10000.0)),
            leverage=float(arguments.get("leverage", 1.0)),
            parameters=arguments.get("parameters"),
            project_id=project_id,
            db=db,
        )

    elif tool_name == "quant_parameter_sweep":
        symbol = arguments.get("symbol", "BTCUSDT")
        timeframe = arguments.get("timeframe", "1h")
        factor_name = arguments.get("factor_name", "ema_spread")
        param_grid = arguments.get("param_grid")
        start_date = arguments.get("start_date")
        end_date = arguments.get("end_date")
        return run_parameter_sweep(
            factor_name=factor_name,
            param_grid=param_grid,
            symbol=symbol,
            timeframe=timeframe,
            start_date=start_date,
            end_date=end_date,
        )

    elif tool_name == "quant_robustness_test":
        test_type = arguments.get("test_type", "all")
        symbol = arguments.get("symbol", "BTCUSDT")
        timeframe = arguments.get("timeframe", "1h")
        factor_name = arguments.get("factor_name", "ema_spread")
        factor_params = arguments.get("factor_params")
        start_date = arguments.get("start_date")
        end_date = arguments.get("end_date")
        n_splits = int(arguments.get("n_splits", 4))
        n_simulations = int(arguments.get("n_simulations", 1000))

        result = {"ok": True, "symbol": symbol, "timeframe": timeframe}

        if test_type in ("walk_forward", "all"):
            wf_res = run_walk_forward_analysis(
                symbol=symbol,
                timeframe=timeframe,
                factor_name=factor_name,
                factor_params=factor_params,
                start_date=start_date,
                end_date=end_date,
                n_splits=n_splits,
            )
            result["walk_forward_analysis"] = wf_res

        if test_type in ("monte_carlo", "all"):
            df = load_market_bars(
                symbol=symbol,
                timeframe=timeframe,
                start_date=start_date,
                end_date=end_date,
            )
            mc_res = run_monte_carlo_stress_test(
                df=df,
                factor_name=factor_name,
                factor_params=factor_params,
                n_simulations=n_simulations,
            )
            result["monte_carlo_stress_test"] = mc_res

        return result

    elif tool_name == "quant_get_strategy":
        strategy_name = arguments.get("strategy_name", "")
        code = get_strategy_code(strategy_name)
        if code is None:
            return {"ok": False, "error": f"策略不存在: {strategy_name}.py"}
        return {"ok": True, "strategy_name": strategy_name, "code": code}

    else:
        return {"ok": False, "error": f"未知的 DSH 工具调用: {tool_name}"}
