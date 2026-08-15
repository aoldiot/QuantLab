from __future__ import annotations

import asyncio
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from .config import settings
from .db import SessionLocal
from .git_versions import GitVersionError, export_revision, resolve_export_repo
from .models import BacktestRun, ResearchProject, ResearchStatus, RunStatus


def research_status_for_run(status: RunStatus) -> ResearchStatus:
    if status in {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.ANALYZING}:
        return ResearchStatus.BACKTESTING
    if status == RunStatus.COMPLETED:
        return ResearchStatus.READY_FOR_ANALYSIS
    return ResearchStatus.READY_FOR_BACKTEST


async def _update(run_id: str, **values) -> None:
    async with SessionLocal() as db:
        run = await db.get(BacktestRun, run_id)
        if run is None:
            return
        for key, value in values.items():
            setattr(run, key, value)
        if run.research_project_id and "status" in values:
            project = await db.get(ResearchProject, run.research_project_id)
            if project and project.status != ResearchStatus.ARCHIVED:
                project.status = research_status_for_run(values["status"])
        await db.commit()


async def execute_backtest(run_id: str, strategy: dict) -> None:
    try:
        await _execute_backtest(run_id, strategy)
    except Exception as exc:
        await _update(
            run_id,
            status=RunStatus.FAILED,
            stage="平台执行异常",
            progress=100,
            error_message=f"{type(exc).__name__}: {exc}",
            finished_at=datetime.now(UTC),
        )


async def _execute_backtest(run_id: str, strategy: dict) -> None:
    """Run one real NautilusTrader backtest in an isolated Python process."""
    await _update(
        run_id,
        status=RunStatus.RUNNING,
        stage="验证 Catalog 与构建回测配置",
        progress=10,
        started_at=datetime.now(UTC),
        error_message=None,
    )
    async with SessionLocal() as db:
        run = await db.get(BacktestRun, run_id)
        if run is None:
            return
        # The worker runs from an exported Git snapshot, so a relative catalog
        # path would otherwise be resolved against ``artifacts/<id>/source/backend``.
        # Freeze the path in the parent process before crossing that boundary.
        config = dict(run.config)
        configured_catalog = config.get("catalog_path") or settings.catalog_path
        config["catalog_path"] = str(Path(configured_catalog).expanduser().resolve())
        payload = {"run_id": run_id, "config": config, "strategy": strategy}

    work_dir = settings.artifact_root.resolve() / run_id
    work_dir.mkdir(parents=True, exist_ok=True)
    payload_path = work_dir / "payload.json"
    output_path = work_dir / "worker-result.json"
    payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    source_root = work_dir / "source"
    try:
        repo = resolve_export_repo(Path(strategy["git_repo"]), strategy["git_commit"])
        export_revision(repo, strategy["git_commit"], source_root)
    except (GitVersionError, KeyError) as exc:
        await _update(run_id, status=RunStatus.FAILED, stage="Git 版本导出失败", progress=100, error_message=str(exc))
        return
    snapshot_backend = source_root / "backend"
    if not snapshot_backend.exists():
        await _update(run_id, status=RunStatus.FAILED, stage="Git 版本导出失败", progress=100,
                      error_message="Git 快照中不存在 backend 目录")
        return

    # Strategy source is pinned to its Git revision. Platform runtime files are
    # versioned by the running QuantLab service and must move together; copying
    # only analytics can otherwise mix incompatible worker/contract signatures.
    current_app = Path(__file__).resolve().parent
    runtime_files = (
        (current_app / "config.py", snapshot_backend / "app" / "config.py"),
        (current_app / "strategy_contract.py", snapshot_backend / "app" / "strategy_contract.py"),
        (current_app / "backtests" / "builder.py", snapshot_backend / "app" / "backtests" / "builder.py"),
        (current_app / "backtests" / "worker.py", snapshot_backend / "app" / "backtests" / "worker.py"),
        (current_app / "backtests" / "analytics.py", snapshot_backend / "app" / "backtests" / "analytics.py"),
    )
    for current_file, snapshot_file in runtime_files:
        snapshot_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(current_file, snapshot_file)

    await _update(run_id, stage="运行 NautilusTrader BacktestNode", progress=35)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.backtests.worker",
        str(payload_path),
        str(output_path),
        cwd=str(snapshot_backend),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(),
            timeout=settings.backtest_timeout_seconds,
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        await _update(run_id, status=RunStatus.FAILED, stage="执行超时", progress=100, error_message="回测超过最大运行时间")
        return

    log = stdout.decode("utf-8", errors="replace")
    (work_dir / "backtest.log").write_text(log, encoding="utf-8")
    if process.returncode != 0 or not output_path.exists():
        message = log[-8000:] or "回测子进程异常退出"
        await _update(run_id, status=RunStatus.FAILED, stage="回测失败", progress=100, error_message=message)
        return

    worker_result = json.loads(output_path.read_text(encoding="utf-8"))
    if not worker_result.get("ok"):
        await _update(run_id, status=RunStatus.FAILED, stage="回测失败", progress=100, error_message=worker_result.get("error", "未知错误"))
        return

    await _update(run_id, status=RunStatus.ANALYZING, stage="保存报告与绩效指标", progress=90)
    await _update(
        run_id,
        status=RunStatus.COMPLETED,
        stage="已完成",
        progress=100,
        metrics=worker_result["metrics"],
        result=worker_result["result"],
        finished_at=datetime.now(UTC),
    )
