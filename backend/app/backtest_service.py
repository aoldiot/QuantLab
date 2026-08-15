from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from .models import BacktestRun, StrategyVersion
from .runner import execute_backtest
from .schemas import BacktestCreate
from .strategy_contract import load_manifest, validate_parameters


async def create_backtest_run(data: BacktestCreate, db: AsyncSession, research_project_id: str | None = None) -> BacktestRun:
    version = await db.get(StrategyVersion, data.strategy_version_id)
    if not version:
        raise HTTPException(400, "策略版本不存在")
    try:
        manifest = load_manifest(version.entrypoint)
        resolved_parameters = validate_parameters(manifest, data.strategy_parameters)
    except (ImportError, TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc

    strategy_code = version.code
    if not strategy_code:
        module_name = version.entrypoint.partition(":")[0].rsplit(".", 1)[-1]
        disk_file = Path(__file__).resolve().parent / "strategies" / f"{module_name}.py"
        if disk_file.exists():
            strategy_code = disk_file.read_text(encoding="utf-8")
        else:
            raise HTTPException(409, f"未找到该策略版本的源代码文件：{module_name}")

    run = BacktestRun(
        name=data.name,
        strategy_version_id=data.strategy_version_id,
        config=data.model_dump(mode="json"),
        research_project_id=research_project_id,
    )
    run.config["strategy_parameters"] = resolved_parameters
    run.config["strategy_version"] = {
        "version": version.version,
        "code_hash": version.code_hash,
        "manifest_hash": version.manifest_hash,
    }
    db.add(run)
    await db.commit()
    await db.refresh(run)

    asyncio.create_task(
        execute_backtest(
            run.id,
            {
                "module": version.entrypoint,
                "code": strategy_code,
                "code_hash": version.code_hash,
                "data_requirements": manifest.data_requirements(),
            },
        )
    )
    return run
