import re
import shutil
import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from importlib.util import find_spec
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import AuthMiddleware
from .auth import router as auth_router
from .backtest_service import confirm_and_start_backtest, create_backtest_run
from .backtests.chart_data import load_chart
from .config import settings
from .data_downloads import router as data_downloads_router
from .db import SessionLocal, get_db
from .dsh import bridge_router as dsh_bridge_router
from .git_config import router as git_config_router
from .git_versions import code_hash, manifest_hash
from .llm_config import router as llm_config_router
from .models import (
    BacktestRun,
    LlmConfiguration,
    ResearchProject,
    ResearchStatus,
    RunStatus,
    Strategy,
    StrategyStatus,
    StrategyVersion,
)
from .research import router as research_router
from .runner import append_log, cancel_active_backtest, get_backtest_logs
from .schemas import (
    BacktestConfirmRequest,
    BacktestCreate,
    BacktestLogsOut,
    BacktestOut,
    CatalogCheckRequest,
    CatalogCheckResponse,
    CatalogMissingDetail,
    DashboardRecentRun,
    DashboardStatsOut,
    BacktestStats,
    CatalogStats,
    ResearchStats,
    StrategyStats,
    SystemHealthStats,
    StrategyCreate,
    StrategyOut,
    StrategyUpdate,
    StrategyVersionCreate,
    StrategyVersionOut,
)
from .strategy_contract import load_manifest
from .strategy_files import list_files
from .data_downloads import scan_catalog_summary
from .strategy_files import router as strategy_files_router


class _ResearchPollingAccessFilter(logging.Filter):
    """Keep high-frequency successful UI polling out of the Uvicorn access log."""

    _paths = (
        "/dsh/events",
        "/dsh/pending",
        "/writing-log",
        "/thinking-status",
        "/messages",
        "/backtests",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        if not settings.dsh_quiet_poll_access_logs:
            return True
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        method, path, status = str(args[1]), str(args[2]), str(args[4])
        clean_path = path.split("?", 1)[0]
        is_project_snapshot = bool(re.fullmatch(r"/api/research/[0-9a-f-]{36}", clean_path))
        is_poll = any(clean_path.endswith(item) for item in self._paths) or is_project_snapshot
        return not (method == "GET" and status.startswith("2") and is_poll)


logging.getLogger("uvicorn.access").addFilter(_ResearchPollingAccessFilter())


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
            version.manifest_hash = manifest_hash(manifest)
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
    from .strategy_files import ensure_strategy_storage
    ensure_strategy_storage()
    await fail_interrupted_backtests()
    from .workflow.task_service import recover_interrupted_tasks
    async with SessionLocal() as db:
        await recover_interrupted_tasks(db)
    await seed()
    yield
    from .dsh import shutdown_all
    shutdown_all()


app = FastAPI(title="QuantLab API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AuthMiddleware)
app.include_router(auth_router)
app.include_router(strategy_files_router)
app.include_router(llm_config_router)
app.include_router(git_config_router)
app.include_router(dsh_bridge_router)
app.include_router(data_downloads_router)
app.include_router(research_router)


@app.get("/api/health")
async def health(): return {"status": "ok"}


@app.get("/api/dashboard/stats", response_model=DashboardStatsOut)
async def get_dashboard_stats(db: AsyncSession = Depends(get_db)):
    # 1. Strategies stats
    files = list_files()
    db_strategies = (await db.scalars(select(Strategy))).unique().all()
    for s in db_strategies:
        await db.refresh(s, ["versions"])

    registered_count = len(db_strategies)
    total_files_count = len(files)
    db_modules = {s.slug for s in db_strategies}
    drafts_count = len([f for f in files if f["name"] not in db_modules])

    cat_counts: dict[str, int] = {}
    for f in files:
        cat = f.get("draft_category") or "未分类"
        matched = next((s for s in db_strategies if s.slug == f["name"]), None)
        if matched and matched.category:
            cat = matched.category
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    # 2. Backtest stats
    runs = (await db.scalars(select(BacktestRun).order_by(BacktestRun.created_at.desc()))).all()
    total_runs = len(runs)
    running_runs = len([r for r in runs if r.status in {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.ANALYZING}])
    completed_runs = len([r for r in runs if r.status == RunStatus.COMPLETED])
    failed_runs = len([r for r in runs if r.status == RunStatus.FAILED])
    canceled_runs = len([r for r in runs if r.status == RunStatus.CANCELED])

    completed_returns = []
    completed_sharpes = []
    for r in runs:
        if r.status == RunStatus.COMPLETED and r.metrics:
            ret = r.metrics.get("total_return")
            if ret is not None:
                try:
                    completed_returns.append(float(ret))
                except (ValueError, TypeError):
                    pass
            shp = r.metrics.get("sharpe")
            if shp is not None:
                try:
                    completed_sharpes.append(float(shp))
                except (ValueError, TypeError):
                    pass

    win_rate = round(len([ret for ret in completed_returns if ret > 0]) / len(completed_returns) * 100, 1) if completed_returns else None
    avg_return = round(sum(completed_returns) / len(completed_returns), 2) if completed_returns else None
    avg_sharpe = round(sum(completed_sharpes) / len(completed_sharpes), 2) if completed_sharpes else None

    version_map: dict[str, str] = {}
    for s in db_strategies:
        for v in s.versions:
            version_map[v.id] = s.name

    recent_runs = []
    for r in runs[:6]:
        strat_name = version_map.get(r.strategy_version_id, "已删除策略")
        cfg = r.config or {}
        symbols = cfg.get("symbols", []) if isinstance(cfg.get("symbols"), list) else []
        timeframes = cfg.get("timeframes", []) if isinstance(cfg.get("timeframes"), list) else []
        venue = cfg.get("venue")
        metrics = r.metrics or {}
        total_ret = None
        sharpe_val = None
        if metrics.get("total_return") is not None:
            try:
                total_ret = float(metrics["total_return"])
            except (ValueError, TypeError):
                pass
        if metrics.get("sharpe") is not None:
            try:
                sharpe_val = float(metrics["sharpe"])
            except (ValueError, TypeError):
                pass

        recent_runs.append(DashboardRecentRun(
            id=r.id,
            name=r.name,
            status=r.status.value,
            stage=r.stage,
            progress=r.progress,
            strategy_name=strat_name,
            venue=venue,
            timeframes=timeframes,
            symbols=symbols,
            total_return=total_ret,
            sharpe=sharpe_val,
            created_at=r.created_at,
        ))

    # 3. Research projects
    projects = (await db.scalars(select(ResearchProject))).all()
    total_projects = len(projects)
    active_projects = len([p for p in projects if p.status != ResearchStatus.ARCHIVED])
    archived_projects = len([p for p in projects if p.status == ResearchStatus.ARCHIVED])

    # 4. Catalog stats
    cat_summary = scan_catalog_summary(settings.catalog_path, page=1, page_size=0)
    total_symbols = cat_summary.get("all_symbols_count", 0)
    total_bars = cat_summary.get("all_bars_count", 0)
    total_size_bytes = cat_summary.get("all_size_bytes", 0)
    available_timeframes = cat_summary.get("available_timeframes", [])

    # 5. System stats
    llm_conf = await db.get(LlmConfiguration, 1)
    llm_configured = bool(llm_conf and llm_conf.model and (llm_conf.api_key_encrypted or llm_conf.auth_type == "none"))
    engine_status = "BUSY" if running_runs > 0 else "IDLE"

    return DashboardStatsOut(
        strategies=StrategyStats(
            total_strategies=max(total_files_count, registered_count),
            registered_strategies=registered_count,
            draft_strategies=drafts_count,
            categories=cat_counts,
        ),
        backtests=BacktestStats(
            total_runs=total_runs,
            running_runs=running_runs,
            completed_runs=completed_runs,
            failed_runs=failed_runs,
            canceled_runs=canceled_runs,
            win_rate=win_rate,
            avg_return=avg_return,
            avg_sharpe=avg_sharpe,
            recent_runs=recent_runs,
        ),
        research=ResearchStats(
            total_projects=total_projects,
            active_projects=active_projects,
            archived_projects=archived_projects,
        ),
        catalog=CatalogStats(
            total_symbols=total_symbols,
            total_bars=total_bars,
            total_size_bytes=total_size_bytes,
            available_timeframes=available_timeframes,
        ),
        system=SystemHealthStats(
            llm_configured=llm_configured,
            db_ok=True,
            engine_status=engine_status,
        ),
    )


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
    module_name = data.module.partition(":")[0].rsplit(".", 1)[-1]
    source_path = Path(__file__).resolve().parent / "strategies" / f"{module_name}.py"
    code = source_path.read_text(encoding="utf-8") if source_path.exists() else ""
    c_hash = code_hash(code) if code else None
    m_hash = manifest_hash(manifest)
    s = Strategy(name=manifest.name, slug=manifest.slug, description=manifest.description, category=manifest.category)
    s.versions.append(StrategyVersion(
        version=manifest.version,
        entrypoint=data.module,
        code=code,
        code_hash=c_hash,
        parameter_schema=manifest.parameter_schema(),
        data_requirements=manifest.data_requirements(),
        manifest_hash=m_hash,
        description=data.version_description,
    ))
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
    if run_ids:
        await db.execute(
            update(ResearchProject)
            .where(ResearchProject.latest_backtest_id.in_(run_ids))
            .values(latest_backtest_id=None)
        )
    await db.execute(
        update(ResearchProject)
        .where(ResearchProject.strategy_id == strategy_id)
        .values(strategy_id=None)
    )
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
        code=version.code or "",
        code_hash=version.code_hash,
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

    module_name = data.module.partition(":")[0].rsplit(".", 1)[-1]
    source_path = Path(__file__).resolve().parent / "strategies" / f"{module_name}.py"
    code = source_path.read_text(encoding="utf-8") if source_path.exists() else ""
    c_hash = code_hash(code) if code else None
    m_hash = manifest_hash(manifest)

    latest = latest_version(strategy)
    if latest and latest.code_hash and latest.code_hash == c_hash:
        raise HTTPException(400, "策略代码未发生改变，无需重复发布版本")

    if any(version.version == manifest.version for version in strategy.versions):
        candidate = next_patch_version(latest.version)
        while any(version.version == candidate for version in strategy.versions):
            candidate = next_patch_version(candidate)
        manifest = replace(manifest, version=candidate)

    version = StrategyVersion(
        strategy_id=strategy.id,
        version=manifest.version,
        entrypoint=data.module,
        code=code,
        code_hash=c_hash,
        parameter_schema=manifest.parameter_schema(),
        data_requirements=manifest.data_requirements(),
        manifest_hash=m_hash,
        description=data.description,
    )
    db.add(version)
    strategy.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(strategy, ["versions"])
    return version_out(version, latest_version(strategy).id)


@app.post("/api/strategies/{strategy_id}/versions/{version_id}/restore")
async def restore_strategy_version(strategy_id: str, version_id: str, db: AsyncSession = Depends(get_db)):
    strategy = await get_strategy_or_404(strategy_id, db)
    version = next((item for item in strategy.versions if item.id == version_id), None)
    if not version:
        raise HTTPException(404, "策略版本不存在")
    if not version.code:
        raise HTTPException(400, "该历史版本未记录源码，无法还原")
    module_name = version.entrypoint.partition(":")[0].rsplit(".", 1)[-1]
    source_path = Path(__file__).resolve().parent / "strategies" / f"{module_name}.py"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(version.code, encoding="utf-8")
    return {"ok": True, "message": f"已将代码成功还原至版本 v{version.version}"}


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
        return load_chart(artifact_dir, symbol, start, end, min(max(limit, 100), 10000), timeframe)
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
    await db.execute(
        update(ResearchProject)
        .where(ResearchProject.latest_backtest_id == run_id)
        .values(latest_backtest_id=None)
    )
    await db.delete(r)
    await db.commit()
    artifact_root = settings.artifact_root.resolve()
    artifact = (artifact_root / run_id).resolve()
    if artifact.parent == artifact_root:
        shutil.rmtree(artifact, ignore_errors=True)


def check_catalog_coverage_batch(req: CatalogCheckRequest) -> CatalogCheckResponse:
    from nautilus_trader.model.data import Bar
    from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog
    from .backtests.builder import instrument_id, timeframe_to_bar_spec
    from .backtests.coverage import date_bounds, query_coverage
    import logging

    resolved_path = Path(req.catalog_path or settings.catalog_path).expanduser().resolve()
    if not resolved_path.exists():
        missing = [s.strip() for s in req.symbols if s.strip()]
        details = [
            CatalogMissingDetail(
                symbol=s,
                instrument_id=instrument_id(s, req.venue, req.market_type),
                timeframe=tf,
                status="MISSING_DATA",
                message=f"Catalog 目录不存在: {resolved_path}",
            )
            for s in missing
            for tf in req.timeframes
        ]
        return CatalogCheckResponse(
            ok=False,
            has_missing=True,
            catalog_exists=False,
            catalog_path=str(resolved_path),
            missing_symbols=missing,
            details=details,
            summary_text=f"Catalog 目录不存在：{resolved_path}",
        )

    registered_instruments = set()
    catalog = None
    try:
        catalog = ParquetDataCatalog(str(resolved_path))
        for inst in catalog.instruments():
            registered_instruments.add(inst.id.value)
    except Exception as err:
        logging.getLogger("uvicorn.error").warning("读取 Catalog Instruments 失败: %s", err)

    start_ns, end_exclusive_ns = date_bounds(req.start_date, req.end_date)

    details: list[CatalogMissingDetail] = []
    missing_symbol_set: set[str] = set()

    for s in [sym.strip() for sym in req.symbols if sym.strip()]:
        inst_id = instrument_id(s, req.venue, req.market_type)
        is_inst_registered = inst_id in registered_instruments

        for tf in req.timeframes:
            try:
                spec = timeframe_to_bar_spec(tf)
            except Exception:
                details.append(
                    CatalogMissingDetail(
                        symbol=s,
                        instrument_id=inst_id,
                        timeframe=tf,
                        status="MISSING_DATA",
                        message=f"不支持的数据周期: {tf}",
                    )
                )
                missing_symbol_set.add(s)
                continue

            bar_type = f"{inst_id}-{spec}-EXTERNAL"
            if not is_inst_registered:
                details.append(
                    CatalogMissingDetail(
                        symbol=s,
                        instrument_id=inst_id,
                        timeframe=tf,
                        status="MISSING_INSTRUMENT",
                        message="未在 Catalog 中找到该标的的交易对定义及行情数据",
                    )
                )
                missing_symbol_set.add(s)
            else:
                coverage = query_coverage(catalog, Bar, bar_type, start_ns, end_exclusive_ns, tf)
                status = "OK" if coverage.complete else ("MISSING_DATA" if coverage.actual_count == 0 else "PARTIAL_RANGE")
                details.append(CatalogMissingDetail(
                    symbol=s,
                    instrument_id=inst_id,
                    timeframe=tf,
                    status=status,
                    message=coverage.message,
                ))
                if not coverage.complete:
                    missing_symbol_set.add(s)

    missing_symbols = sorted(missing_symbol_set)
    has_missing = len(missing_symbols) > 0
    if has_missing:
        summary_text = f"检测到 {len(missing_symbols)} 个品种数据不完整，可确认后带警告继续：{', '.join(missing_symbols)}"
    else:
        summary_text = "所有标的 Catalog 数据均已完备"

    return CatalogCheckResponse(
        ok=not has_missing,
        has_missing=has_missing,
        catalog_exists=True,
        catalog_path=str(resolved_path),
        missing_symbols=missing_symbols,
        details=details,
        summary_text=summary_text,
    )


@app.post("/api/backtests/check-catalog", response_model=CatalogCheckResponse)
async def check_backtest_catalog(req: CatalogCheckRequest):
    return check_catalog_coverage_batch(req)


@app.get("/api/backtests/{run_id}/logs", response_model=BacktestLogsOut)
async def get_backtest_logs_endpoint(run_id: str, db: AsyncSession = Depends(get_db)):
    r = await db.get(BacktestRun, run_id)
    if not r:
        raise HTTPException(404, "回测不存在")
    logs = get_backtest_logs(run_id)
    return BacktestLogsOut(
        id=r.id,
        status=r.status.value,
        stage=r.stage,
        progress=r.progress,
        logs=logs,
        error_message=r.error_message,
    )


@app.post("/api/backtests", response_model=BacktestOut)
async def create_backtest(data: BacktestCreate, db: AsyncSession = Depends(get_db)):
    return run_out(await create_backtest_run(data, db, research_project_id=data.research_project_id))


@app.post("/api/backtests/{run_id}/confirm", response_model=BacktestOut)
async def confirm_backtest(run_id: str, req: BacktestConfirmRequest | None = None, db: AsyncSession = Depends(get_db)):
    ignore_missing = req.ignore_missing_data if req is not None else True
    run = await confirm_and_start_backtest(run_id, db, ignore_missing_data=ignore_missing)
    return run_out(run)


@app.post("/api/backtests/{run_id}/cancel", response_model=BacktestOut)
async def cancel_backtest(run_id: str, db: AsyncSession = Depends(get_db)):
    r = await db.get(BacktestRun, run_id)
    if not r: raise HTTPException(404, "回测不存在")
    if r.status in {RunStatus.COMPLETED, RunStatus.FAILED}: raise HTTPException(409, "任务已结束")
    stopped = await cancel_active_backtest(run_id, (r.config or {}).get("worker_pid"))
    r.status, r.stage = RunStatus.CANCELED, "已取消"
    r.finished_at = datetime.now(UTC)
    append_log(run_id, f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] 回测任务已被取消{'，worker 已终止' if stopped else ''}。")
    if r.research_project_id:
        project = await db.get(ResearchProject, r.research_project_id)
        if project and project.status != ResearchStatus.ARCHIVED:
            project.status = ResearchStatus.READY_FOR_BACKTEST
    await db.commit(); await db.refresh(r)
    return run_out(r)
