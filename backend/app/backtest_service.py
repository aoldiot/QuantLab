from __future__ import annotations

import asyncio

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from .git_versions import manifest_hash
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
    if not version.git_commit or not version.git_repo:
        raise HTTPException(409, "该策略版本未绑定 Git commit，请重新发布为 Git 版本")
    if not version.manifest_hash or manifest_hash(manifest) != version.manifest_hash:
        raise HTTPException(409, "策略工作区代码与所选 Git 版本不一致，请发布新的策略版本后再回测")
    run = BacktestRun(name=data.name, strategy_version_id=data.strategy_version_id,
                      config=data.model_dump(mode="json"), research_project_id=research_project_id)
    run.config["strategy_parameters"] = resolved_parameters
    run.config["strategy_revision"] = {"commit": version.git_commit, "ref": version.git_ref, "manifest_hash": version.manifest_hash}
    db.add(run)
    await db.commit()
    await db.refresh(run)
    asyncio.create_task(execute_backtest(run.id, {"module": version.entrypoint, "git_commit": version.git_commit, "git_repo": version.git_repo, "data_requirements": manifest.data_requirements()}))
    return run
