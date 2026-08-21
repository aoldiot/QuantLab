from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.git_versions import code_hash, manifest_hash
from app.models import BacktestRun, RunStatus, Strategy, StrategyVersion
from app.runner import execute_backtest
from app.strategy_contract import load_manifest, validate_parameters
from app.strategy_files import _path

logger = logging.getLogger(__name__)


async def run_nautilus_backtest(
    strategy_name: str,
    symbols: list[str],
    start_date: str,
    end_date: str,
    initial_balance: float = 10000.0,
    leverage: float = 1.0,
    parameters: dict[str, Any] | None = None,
    project_id: str | None = None,
    db: AsyncSession | None = None,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Execute a deterministic NautilusTrader event-driven backtest."""
    strategy_name = strategy_name.strip().lower()
    module = f"app.strategies.{strategy_name}"

    try:
        manifest = load_manifest(module)
    except Exception as exc:
        return {"ok": False, "error": f"加载策略 Manifest 失败：{exc}"}

    source_path = _path(strategy_name)
    code = source_path.read_text(encoding="utf-8") if source_path.exists() else ""
    if not code:
        return {"ok": False, "error": f"未找到策略代码文件：{strategy_name}.py"}

    c_hash = code_hash(code)
    m_hash = manifest_hash(manifest)

    async def _execute_with_session(s: AsyncSession) -> dict[str, Any]:
        # 1. Ensure Strategy & StrategyVersion
        strat = await s.scalar(select(Strategy).where(Strategy.slug == manifest.slug))
        if strat is None:
            strat = Strategy(
                name=manifest.name,
                slug=manifest.slug,
                description=manifest.description,
                category=manifest.category,
            )
            s.add(strat)
            await s.flush()

        await s.refresh(strat, ["versions"])

        version_obj = None
        for v in strat.versions:
            if v.code_hash == c_hash:
                version_obj = v
                break

        if not version_obj:
            v_name = manifest.version
            if any(item.version == v_name for item in strat.versions):
                v_name = f"{manifest.version}.{len(strat.versions) + 1}"
            version_obj = StrategyVersion(
                strategy_id=strat.id,
                version=v_name,
                entrypoint=module,
                code=code,
                code_hash=c_hash,
                parameter_schema=manifest.parameter_schema(),
                data_requirements=manifest.data_requirements(),
                manifest_hash=m_hash,
                description="QuantLab 回测引擎发布",
            )
            s.add(version_obj)
            await s.flush()
            await s.refresh(version_obj)

        resolved_params = validate_parameters(manifest, parameters or {})

        clean_symbols = [s_item.strip() for s_item in symbols if s_item.strip()]
        timeframes = manifest.data_requirements().get("timeframes", ["15m"])
        run_name = f"{manifest.name}_{start_date}_{end_date}"

        config_dict = {
            "name": run_name,
            "strategy_name": strategy_name,
            "strategy_version_id": version_obj.id,
            "strategy_parameters": resolved_params,
            "venue": "BINANCE",
            "symbols": clean_symbols,
            "timeframes": timeframes,
            "start_date": start_date,
            "end_date": end_date,
            "initial_balance": initial_balance,
            "leverage": leverage,
            "execution_model": "CONSERVATIVE",
            # Automated research runs cannot ask for interactive confirmation,
            # so fail closed instead of silently skipping catalog gaps.
            "ignore_missing_data": False,
            "strategy_version": {
                "version": version_obj.version,
                "code_hash": version_obj.code_hash,
                "manifest_hash": version_obj.manifest_hash,
            },
        }

        # Check if research project exists
        p_id = None
        if project_id:
            from app.models import ResearchProject
            proj_record = await s.get(ResearchProject, project_id)
            if proj_record:
                p_id = project_id

        run = BacktestRun(
            name=run_name,
            strategy_version_id=version_obj.id,
            config=config_dict,
            research_project_id=p_id,
            stage="正在启动回测",
            progress=5,
            status=RunStatus.RUNNING,
        )
        s.add(run)
        await s.flush()
        if p_id and proj_record:
            proj_record.latest_backtest_id = run.id
        await s.commit()
        await s.refresh(run)

        strategy_payload = {
            "module": version_obj.entrypoint,
            "code": code,
            "code_hash": version_obj.code_hash,
            "data_requirements": manifest.data_requirements(),
        }

        # Launch backtest in background task
        asyncio.create_task(execute_backtest(run.id, strategy_payload))

        # Poll for completion
        poll_interval = 1.5
        start_time = asyncio.get_event_loop().time()

        while (asyncio.get_event_loop().time() - start_time) < timeout_seconds:
            await asyncio.sleep(poll_interval)
            await s.refresh(run)
            if run.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                break

        if run.status == RunStatus.COMPLETED:
            metrics = run.metrics or {}
            return {
                "ok": True,
                "status": "COMPLETED",
                "run_id": run.id,
                "strategy_name": strategy_name,
                "metrics": metrics,
                "summary": f"回测成功完成！总收益率: {metrics.get('total_return', 'N/A')}, 夏普比率: {metrics.get('sharpe_ratio', 'N/A')}, 最大回撤: {metrics.get('max_drawdown', 'N/A')}, 胜率: {metrics.get('win_rate', 'N/A')}, 交易次数: {metrics.get('total_trades', 'N/A')}",
            }
        elif run.status == RunStatus.FAILED:
            return {
                "ok": False,
                "status": "FAILED",
                "run_id": run.id,
                "strategy_name": strategy_name,
                "error_message": run.error_message or "回测执行失败",
                "stage": run.stage,
            }
        else:
            return {
                "ok": True,
                "status": "RUNNING",
                "run_id": run.id,
                "message": f"回测已在后台运行中 (当前进度 {run.progress}%)",
            }

    if db is not None:
        return await _execute_with_session(db)
    else:
        async with SessionLocal() as session:
            return await _execute_with_session(session)
