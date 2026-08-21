from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from .models import BacktestRun, RunStatus, StrategyVersion
from .runner import check_data_integrity_and_wait, execute_backtest
from .schemas import BacktestCreate
from .strategy_contract import load_manifest, validate_parameters

DEFAULT_FUNDING_RATE_EIGHT_HOURS = 0.0001
BINANCE_DEFAULT_FEES = {
    "spot": {"maker": 0.001, "taker": 0.001},
    "um": {"maker": 0.0002, "taker": 0.0005},
}


def _build_strategy_payload(version: StrategyVersion, strategy_code: str, manifest) -> dict:
    return {
        "module": version.entrypoint,
        "code": strategy_code,
        "code_hash": version.code_hash,
        "data_requirements": manifest.data_requirements(),
    }


def _strategy_snapshot(version: StrategyVersion, manifest) -> dict:
    """Freeze every manifest-derived value used by one historical run."""
    return {
        "entrypoint": version.entrypoint,
        "version": version.version,
        "code_hash": version.code_hash,
        "manifest_hash": version.manifest_hash,
        "parameter_schema": version.parameter_schema,
        "data_requirements": version.data_requirements or manifest.data_requirements(),
        "manifest": manifest.data_requirements(),
    }


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
        research_project_id=research_project_id or data.research_project_id,
    )
    run.config["strategy_parameters"] = resolved_parameters
    # Coverage warnings are mandatory: missing data may be accepted by the user,
    # but it must never be silently omitted from a run.
    run.config["check_data_integrity"] = True
    run.config["strategy_version"] = {
        "version": version.version,
        "code_hash": version.code_hash,
        "manifest_hash": version.manifest_hash,
    }
    run.config["strategy_snapshot"] = _strategy_snapshot(version, manifest)
    run.config["fee_snapshot"] = BINANCE_DEFAULT_FEES[data.market_type]
    run.config["funding_snapshot"] = {
        "enabled": data.market_type == "um",
        "rate_per_8h": DEFAULT_FUNDING_RATE_EIGHT_HOURS,
        "settlement_hours_utc": [0, 8, 16],
        "end_time": f"{data.end_date}T23:59:59.999999999Z",
        "long_pays_when_positive": True,
        "method": "fixed_rate_position_notional",
    }

    strategy_payload = _build_strategy_payload(version, strategy_code, manifest)

    run.stage = "准备检查数据完整性"
    run.progress = 0
    run.config["waiting_confirmation"] = False
    db.add(run)
    await db.commit()
    await db.refresh(run)
    asyncio.create_task(check_data_integrity_and_wait(run.id, strategy_payload))

    return run


async def confirm_and_start_backtest(run_id: str, db: AsyncSession, ignore_missing_data: bool = True) -> BacktestRun:
    run = await db.get(BacktestRun, run_id)
    if not run:
        raise HTTPException(404, "回测不存在")
    if run.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
        raise HTTPException(409, "任务已结束，无法重复启动")

    version = await db.get(StrategyVersion, run.strategy_version_id)
    if not version:
        raise HTTPException(400, "策略版本不存在")

    strategy_code = version.code
    if not strategy_code:
        module_name = version.entrypoint.partition(":")[0].rsplit(".", 1)[-1]
        disk_file = Path(__file__).resolve().parent / "strategies" / f"{module_name}.py"
        if disk_file.exists():
            strategy_code = disk_file.read_text(encoding="utf-8")
        else:
            raise HTTPException(409, f"未找到该策略版本的源代码文件：{module_name}")

    config = dict(run.config)
    config["waiting_confirmation"] = False
    config["ignore_missing_data"] = ignore_missing_data
    run.config = config
    run.stage = "已确认，准备执行"
    run.status = RunStatus.RUNNING
    run.progress = 5

    await db.commit()
    await db.refresh(run)

    # Do not re-import the mutable workspace Manifest here. The worker imports
    # the versioned source in its sandbox and analytics receives this snapshot.
    snapshot = run.config.get("strategy_snapshot") or {}
    strategy_payload = {
        "module": version.entrypoint,
        "code": strategy_code,
        "code_hash": version.code_hash,
        "data_requirements": snapshot.get("data_requirements", version.data_requirements),
    }
    asyncio.create_task(execute_backtest(run.id, strategy_payload))
    return run
