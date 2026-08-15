import asyncio
import re
import shutil
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from importlib.util import find_spec

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .agent.service import cleanup_expired_worktrees, repair_agent_session_paths
from .agent.service import router as agent_router
from .backtest_service import create_backtest_run
from .backtests.chart_data import load_chart
from .config import settings
from .data_downloads import router as data_downloads_router
from .db import SessionLocal, get_db
from .git_config import push_credentials
from .git_config import router as git_config_router
from .git_versions import GitVersionError, publish_revision, resolve_revision
from .llm_config import router as llm_config_router
from .models import BacktestRun, ResearchProject, ResearchStatus, RunStatus, Strategy, StrategyStatus, StrategyVersion
from .research import router as research_router
from .schemas import (
    BacktestCreate,
    BacktestOut,
    StrategyCreate,
    StrategyOut,
    StrategyUpdate,
    StrategyVersionCreate,
    StrategyVersionOut,
)
from .strategy_contract import load_manifest
from .strategy_files import router as strategy_files_router


async def seed():
    async with SessionLocal() as db:
        for module_path in ("app.strategies.atr_trend", "app.strategies.momentum_rotation"):
            # Built-in examples are regular user-manageable strategies. Once a
            # user deletes the source file, startup must not recreate or import it.
            if find_spec(module_path) is None:
                continue
            manifest = load_manifest(module_path)
            strategy = await db.scalar(select(Strategy).where(Strategy.slug == manifest.slug))
            if strategy is None:
                strategy = Strategy(name=manifest.name, slug=manifest.slug, description=manifest.description, category=manifest.category)
                db.add(strategy)
                await db.flush()
            version = await db.scalar(select(StrategyVersion).where(StrategyVersion.strategy_id == strategy.id, StrategyVersion.version == manifest.version))
            if version is None:
                version = StrategyVersion(strategy_id=strategy.id, version=manifest.version, entrypoint=module_path, parameter_schema={}, data_requirements={})
                db.add(version)
            version.entrypoint = module_path
            version.parameter_schema = manifest.parameter_schema()
            version.data_requirements = manifest.data_requirements()
            if not version.git_commit:
                try:
                    revision = resolve_revision(manifest, require_clean=False)
                    version.git_commit = revision.commit
                    version.git_ref = revision.ref
                    version.git_repo = str(revision.repo)
                    version.manifest_hash = revision.manifest_hash
                except GitVersionError:
                    pass
        await db.commit()


async def fail_interrupted_backtests():
    async with SessionLocal() as db:
        runs = (await db.scalars(select(BacktestRun).where(BacktestRun.status.in_((RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.ANALYZING))))).all()
        for run in runs:
            run.status = RunStatus.FAILED
            run.stage = "服务重启，任务已中断"
            run.progress = 100
            run.error_message = "回测进程未正常结束，请重新创建回测"
            run.finished_at = datetime.now(UTC)
            if run.research_project_id:
                project = await db.get(ResearchProject, run.research_project_id)
                if project and project.status != ResearchStatus.ARCHIVED:
                    project.status = ResearchStatus.READY_FOR_BACKTEST
        if runs:
            await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.artifact_root.resolve().mkdir(parents=True, exist_ok=True)
    await fail_interrupted_backtests()
    await seed()
    await repair_agent_session_paths()
    await asyncio.to_thread(cleanup_expired_worktrees)
    yield


app = FastAPI(title="QuantLab API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])
app.include_router(strategy_files_router)
app.include_router(llm_config_router)
app.include_router(git_config_router)
app.include_router(agent_router)
app.include_router(data_downloads_router)
app.include_router(research_router)


@app.get("/api/health")
async def health(): return {"status": "ok"}


def version_key(version: str) -> tuple:
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in re.split(r"[.-]", version))


def next_patch_version(version: str) -> str:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise HTTPException(409, f"版本 {version} 已存在，且不是可自动递增的 x.y.z 格式")
    major, minor, patch = map(int, match.groups())
    return f"{major}.{minor}.{patch + 1}"


def latest_version(s: Strategy) -> StrategyVersion:
    if not s.versions:
        raise HTTPException(409, "策略还没有可用版本")
    return max(s.versions, key=lambda item: version_key(item.version))


def strategy_out(s: Strategy) -> StrategyOut:
    v = latest_version(s)
    return StrategyOut(id=s.id, name=s.name, slug=s.slug, description=s.description, category=s.category,
        status=s.status.value, latest_version_id=v.id, version=v.version, parameter_schema=v.parameter_schema,
        data_requirements=v.data_requirements, version_count=len(s.versions), module=v.entrypoint,
        created_at=s.created_at, updated_at=s.updated_at)


async def get_strategy_or_404(strategy_id: str, db: AsyncSession) -> Strategy:
    strategy = await db.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(404, "策略不存在")
    await db.refresh(strategy, ["versions"])
    return strategy


@app.get("/api/strategies", response_model=list[StrategyOut])
async def list_strategies(db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(Strategy).order_by(Strategy.created_at.desc()))).unique().all()
    for row in rows: await db.refresh(row, ["versions"])
    return [strategy_out(row) for row in rows]


@app.get("/api/strategies/{strategy_id}", response_model=StrategyOut)
async def get_strategy(strategy_id: str, db: AsyncSession = Depends(get_db)):
    return strategy_out(await get_strategy_or_404(strategy_id, db))


@app.post("/api/strategies", response_model=StrategyOut)
async def create_strategy(data: StrategyCreate, db: AsyncSession = Depends(get_db)):
    try:
        manifest = load_manifest(data.module)
    except (ImportError, AttributeError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if await db.scalar(select(Strategy.id).where(Strategy.slug == manifest.slug)):
        raise HTTPException(409, "策略已经注册")
    try:
        revision = publish_revision(manifest, data.version_description, await push_credentials(db))
    except GitVersionError as exc:
        raise HTTPException(400, str(exc)) from exc
    s = Strategy(name=manifest.name, slug=manifest.slug, description=manifest.description, category=manifest.category)
    s.versions.append(StrategyVersion(version=manifest.version, entrypoint=data.module,
        parameter_schema=manifest.parameter_schema(), data_requirements=manifest.data_requirements(),
        git_commit=revision.commit, git_ref=revision.ref, git_repo=str(revision.repo),
        manifest_hash=revision.manifest_hash, description=data.version_description))
    db.add(s); await db.commit(); await db.refresh(s, ["versions"])
    return strategy_out(s)


@app.patch("/api/strategies/{strategy_id}", response_model=StrategyOut)
async def update_strategy(strategy_id: str, data: StrategyUpdate, db: AsyncSession = Depends(get_db)):
    strategy = await get_strategy_or_404(strategy_id, db)
    changes = data.model_dump(exclude_unset=True)
    if "status" in changes:
        changes["status"] = StrategyStatus(changes["status"])
    for field, value in changes.items():
        setattr(strategy, field, value)
    await db.commit()
    # updated_at is generated by the database on UPDATE. Refresh scalar
    # columns before response serialization to avoid an async lazy load.
    await db.refresh(strategy)
    await db.refresh(strategy, ["versions"])
    return strategy_out(strategy)


@app.delete("/api/strategies/{strategy_id}", status_code=204)
async def delete_strategy(strategy_id: str, db: AsyncSession = Depends(get_db)):
    strategy = await get_strategy_or_404(strategy_id, db)
    version_ids = [version.id for version in strategy.versions]
    runs = list((await db.scalars(select(BacktestRun).where(BacktestRun.strategy_version_id.in_(version_ids)))).all())
    active = [run for run in runs if run.status in {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.ANALYZING}]
    if active:
        raise HTTPException(409, f"该策略还有 {len(active)} 个回测正在运行或排队，请等待结束或先取消")
    run_ids = [run.id for run in runs]
    for run in runs:
        await db.delete(run)
    await db.flush()
    await db.delete(strategy)
    await db.commit()
    artifact_root = settings.artifact_root.resolve()
    for run_id in run_ids:
        artifact = (artifact_root / run_id).resolve()
        if artifact.parent == artifact_root:
            shutil.rmtree(artifact, ignore_errors=True)


def version_out(version: StrategyVersion, current_id: str) -> StrategyVersionOut:
    return StrategyVersionOut(
        id=version.id,
        strategy_id=version.strategy_id,
        version=version.version,
        entrypoint=version.entrypoint,
        parameter_schema=version.parameter_schema,
        data_requirements=version.data_requirements,
        is_latest=version.id == current_id,
        git_commit=version.git_commit,
        git_ref=version.git_ref,
        manifest_hash=version.manifest_hash,
        created_at=version.created_at,
        description=version.description or "",
    )


@app.get("/api/strategies/{strategy_id}/versions", response_model=list[StrategyVersionOut])
async def list_strategy_versions(strategy_id: str, db: AsyncSession = Depends(get_db)):
    strategy = await get_strategy_or_404(strategy_id, db)
    current_id = latest_version(strategy).id
    versions = sorted(strategy.versions, key=lambda item: version_key(item.version), reverse=True)
    return [version_out(version, current_id) for version in versions]


@app.post("/api/strategies/{strategy_id}/versions", response_model=StrategyVersionOut)
async def create_strategy_version(strategy_id: str, data: StrategyVersionCreate, db: AsyncSession = Depends(get_db)):
    strategy = await get_strategy_or_404(strategy_id, db)
    try:
        manifest = load_manifest(data.module)
    except (ImportError, AttributeError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if manifest.slug != strategy.slug:
        raise HTTPException(400, f"Manifest slug 必须是 {strategy.slug}")
    if any(version.version == manifest.version for version in strategy.versions):
        candidate = next_patch_version(latest_version(strategy).version)
        while any(version.version == candidate for version in strategy.versions):
            candidate = next_patch_version(candidate)
        manifest = replace(manifest, version=candidate)
    try:
        revision = publish_revision(manifest, data.description, await push_credentials(db))
    except GitVersionError as exc:
        raise HTTPException(400, str(exc)) from exc
    version = StrategyVersion(
        strategy_id=strategy.id,
        version=manifest.version,
        entrypoint=data.module,
        parameter_schema=manifest.parameter_schema(),
        data_requirements=manifest.data_requirements(),
        git_commit=revision.commit,
        git_ref=revision.ref,
        git_repo=str(revision.repo),
        manifest_hash=revision.manifest_hash,
        description=data.description,
    )
    db.add(version)
    strategy.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(strategy, ["versions"])
    return version_out(version, latest_version(strategy).id)


@app.delete("/api/strategies/{strategy_id}/versions/{version_id}", status_code=204)
async def delete_strategy_version(strategy_id: str, version_id: str, db: AsyncSession = Depends(get_db)):
    strategy = await get_strategy_or_404(strategy_id, db)
    version = next((item for item in strategy.versions if item.id == version_id), None)
    if not version:
        raise HTTPException(404, "策略版本不存在")
    if len(strategy.versions) == 1:
        raise HTTPException(409, "不能删除策略的唯一版本")
    if await db.scalar(select(BacktestRun.id).where(BacktestRun.strategy_version_id == version.id).limit(1)):
        raise HTTPException(409, "该版本已有回测记录，不能删除")
    await db.delete(version)
    await db.commit()


def run_out(r: BacktestRun) -> BacktestOut:
    return BacktestOut(id=r.id, name=r.name, status=r.status.value, stage=r.stage, progress=r.progress,
        config=r.config, metrics=r.metrics, result=r.result, error_message=r.error_message,
        research_project_id=r.research_project_id, created_at=r.created_at)


@app.get("/api/backtests", response_model=list[BacktestOut])
async def list_backtests(db: AsyncSession = Depends(get_db)):
    return [run_out(r) for r in (await db.scalars(select(BacktestRun).order_by(BacktestRun.created_at.desc()))).all()]


@app.get("/api/backtests/{run_id}", response_model=BacktestOut)
async def get_backtest(run_id: str, db: AsyncSession = Depends(get_db)):
    r = await db.get(BacktestRun, run_id)
    if not r: raise HTTPException(404, "回测不存在")
    return run_out(r)


@app.get("/api/backtests/{run_id}/chart")
async def get_backtest_chart(run_id: str, symbol: str | None = None, start: int | None = None,
                             end: int | None = None, limit: int = 5000, timeframe: str | None = None,
                             db: AsyncSession = Depends(get_db)):
    r = await db.get(BacktestRun, run_id)
    if not r:
        raise HTTPException(404, "回测不存在")
    artifact_dir = (settings.artifact_root.resolve() / run_id).resolve()
    if artifact_dir.parent != settings.artifact_root.resolve():
        raise HTTPException(400, "无效回测路径")
    try:
        configured = r.config.get("timeframes") or []
        primary_timeframe = timeframe or (configured[0] if configured else None)
        return load_chart(artifact_dir, symbol, start, end, min(max(limit, 100), 10000), primary_timeframe)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/backtests/{run_id}", status_code=204)
async def delete_backtest(run_id: str, db: AsyncSession = Depends(get_db)):
    r = await db.get(BacktestRun, run_id)
    if not r:
        raise HTTPException(404, "回测不存在")
    if r.status in {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.ANALYZING}:
        raise HTTPException(409, "运行中或排队中的回测不能删除，请先取消并等待任务结束")
    await db.delete(r)
    await db.commit()
    artifact_root = settings.artifact_root.resolve()
    artifact = (artifact_root / run_id).resolve()
    if artifact.parent == artifact_root:
        shutil.rmtree(artifact, ignore_errors=True)


@app.post("/api/backtests", response_model=BacktestOut)
async def create_backtest(data: BacktestCreate, db: AsyncSession = Depends(get_db)):
    return run_out(await create_backtest_run(data, db))


@app.post("/api/backtests/{run_id}/cancel", response_model=BacktestOut)
async def cancel_backtest(run_id: str, db: AsyncSession = Depends(get_db)):
    r = await db.get(BacktestRun, run_id)
    if not r: raise HTTPException(404, "回测不存在")
    if r.status in {RunStatus.COMPLETED, RunStatus.FAILED}: raise HTTPException(409, "任务已结束")
    r.status, r.stage = RunStatus.CANCELED, "已取消"
    r.finished_at = datetime.now(UTC)
    if r.research_project_id:
        project = await db.get(ResearchProject, r.research_project_id)
        if project and project.status != ResearchStatus.ARCHIVED:
            project.status = ResearchStatus.READY_FOR_BACKTEST
    await db.commit(); await db.refresh(r)
    return run_out(r)
