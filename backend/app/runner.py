from __future__ import annotations

import asyncio
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from .config import settings
from .db import SessionLocal
from .models import BacktestRun, ResearchProject, ResearchStatus, RunStatus


def append_log(run_id: str, message: str) -> None:
    """Append a log line to artifacts/<run_id>/backtest.log."""
    try:
        work_dir = settings.artifact_root.resolve() / run_id
        work_dir.mkdir(parents=True, exist_ok=True)
        log_file = work_dir / "backtest.log"
        with log_file.open("a", encoding="utf-8") as f:
            f.write(message if message.endswith("\n") else message + "\n")
            f.flush()
    except Exception:
        pass


def get_backtest_logs(run_id: str) -> str:
    """Read the backtest log file for the given run_id."""
    work_dir = settings.artifact_root.resolve() / run_id
    log_file = work_dir / "backtest.log"
    if log_file.exists():
        try:
            return log_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
    return ""


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


async def check_data_integrity_and_wait(run_id: str, strategy: dict) -> None:
    try:
        await _check_data_integrity_and_wait(run_id, strategy)
    except Exception as exc:
        append_log(run_id, f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] 数据完整性检查异常: {type(exc).__name__}: {exc}")
        await _update(
            run_id,
            status=RunStatus.FAILED,
            stage="数据检查异常",
            progress=100,
            error_message=f"{type(exc).__name__}: {exc}",
            finished_at=datetime.now(UTC),
        )


async def _check_data_integrity_and_wait(run_id: str, strategy: dict) -> None:
    """Check catalog data coverage and update progress live. On completion, wait for user confirmation."""
    from nautilus_trader.model.data import Bar
    from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog
    from .backtests.builder import instrument_id, timeframe_to_bar_spec

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    append_log(run_id, f"[{timestamp}] [INFO] 开始数据完整性检查...")

    async with SessionLocal() as db:
        run = await db.get(BacktestRun, run_id)
        if run is None:
            return
        config = dict(run.config)

    symbols = [s.strip() for s in config.get("symbols", []) if s.strip()]
    timeframes = config.get("timeframes", [])
    venue = config.get("venue", "BINANCE")
    start_date = str(config.get("start_date", ""))
    end_date = str(config.get("end_date", ""))
    catalog_path = config.get("catalog_path") or settings.catalog_path

    resolved_path = Path(catalog_path).expanduser().resolve()

    if not resolved_path.exists():
        missing_symbols = symbols
        details = [
            {
                "symbol": s,
                "instrument_id": instrument_id(s, venue),
                "timeframe": tf,
                "status": "MISSING_DATA",
                "message": f"Catalog 目录不存在: {resolved_path}",
            }
            for s in missing_symbols
            for tf in timeframes
        ]
        check_result = {
            "ok": False,
            "has_missing": True,
            "catalog_exists": False,
            "catalog_path": str(resolved_path),
            "missing_symbols": missing_symbols,
            "details": details,
            "summary_text": f"Catalog 目录不存在：{resolved_path}",
        }
        append_log(run_id, f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] Catalog 目录不存在: {resolved_path}")
        config["catalog_check"] = check_result
        config["waiting_confirmation"] = True
        await _update(run_id, config=config, stage="数据检查完成，等待确认", progress=100)
        return

    registered_instruments = set()
    catalog = None
    try:
        catalog = ParquetDataCatalog(str(resolved_path))
        for inst in catalog.instruments():
            registered_instruments.add(inst.id.value)
    except Exception as err:
        append_log(run_id, f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] 读取 Catalog Instruments 失败: {err}")

    bar_dir = resolved_path / "data" / "bar"
    start_str = f"{start_date}T00:00:00Z"
    end_str = f"{end_date}T23:59:59Z"

    details = []
    missing_symbol_set = set()
    total_checks = max(1, len(symbols) * len(timeframes))
    current_check = 0

    for s in symbols:
        inst_id = instrument_id(s, venue)
        is_inst_registered = inst_id in registered_instruments

        for tf in timeframes:
            current_check += 1
            progress = int((current_check / total_checks) * 100)

            await _update(
                run_id,
                stage=f"正在检查数据完整性 ({current_check}/{total_checks}): {s} {tf}",
                progress=min(progress, 99),
            )

            try:
                spec = timeframe_to_bar_spec(tf)
            except Exception:
                details.append({
                    "symbol": s,
                    "instrument_id": inst_id,
                    "timeframe": tf,
                    "status": "MISSING_DATA",
                    "message": f"不支持的数据周期: {tf}",
                })
                missing_symbol_set.add(s)
                append_log(run_id, f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] [{current_check}/{total_checks}] {s} {tf} -> 不支持的数据周期")
                continue

            bar_dir_name = f"{inst_id}-{spec}-EXTERNAL"
            bar_path = bar_dir / bar_dir_name

            files = []
            if bar_path.is_dir() and catalog is not None:
                try:
                    files = catalog._query_files(Bar, [bar_dir_name], start_str, end_str)
                except Exception:
                    files = list(bar_path.glob("*.parquet"))

            if not is_inst_registered and not bar_path.exists():
                details.append({
                    "symbol": s,
                    "instrument_id": inst_id,
                    "timeframe": tf,
                    "status": "MISSING_INSTRUMENT",
                    "message": "未在 Catalog 中找到该标的的交易对定义及行情数据",
                })
                missing_symbol_set.add(s)
                append_log(run_id, f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] [{current_check}/{total_checks}] {s} {tf} -> 未找到交易对定义及行情数据")
            elif not bar_path.exists() or len(list(bar_path.glob("*.parquet"))) == 0:
                details.append({
                    "symbol": s,
                    "instrument_id": inst_id,
                    "timeframe": tf,
                    "status": "MISSING_DATA",
                    "message": f"缺少 {tf} 周期 Parquet 行情数据",
                })
                missing_symbol_set.add(s)
                append_log(run_id, f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] [{current_check}/{total_checks}] {s} {tf} -> 缺少 {tf} 周期行情数据")
            elif not files:
                details.append({
                    "symbol": s,
                    "instrument_id": inst_id,
                    "timeframe": tf,
                    "status": "PARTIAL_RANGE",
                    "message": f"{tf} 周期在请求时间范围 ({start_date} ~ {end_date}) 内未找到可用数据",
                })
                missing_symbol_set.add(s)
                append_log(run_id, f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] [{current_check}/{total_checks}] {s} {tf} -> 请求时间范围内无可用数据")
            else:
                details.append({
                    "symbol": s,
                    "instrument_id": inst_id,
                    "timeframe": tf,
                    "status": "OK",
                    "message": "数据完整",
                })
                append_log(run_id, f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [{current_check}/{total_checks}] {s} {tf} -> 数据完整 ({len(files)} 个 Parquet 文件)")

            # Brief non-blocking yield so UI gets smooth progress updates
            await asyncio.sleep(0.02)

    missing_symbols = sorted(missing_symbol_set)
    has_missing = len(missing_symbols) > 0
    if has_missing:
        summary_text = f"检测到 {len(missing_symbols)} 个标的缺少数据：{', '.join(missing_symbols)}"
    else:
        summary_text = "所有标的 Catalog 数据均已完备"

    check_result = {
        "ok": not has_missing,
        "has_missing": has_missing,
        "catalog_exists": True,
        "catalog_path": str(resolved_path),
        "missing_symbols": missing_symbols,
        "details": details,
        "summary_text": summary_text,
    }

    append_log(run_id, f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] 数据检查完成：{summary_text}。等待用户确认启动回测。")

    config["catalog_check"] = check_result
    config["waiting_confirmation"] = True
    await _update(run_id, config=config, stage="数据检查完成，等待确认", progress=100)


async def execute_backtest(run_id: str, strategy: dict) -> None:
    try:
        await _execute_backtest(run_id, strategy)
    except Exception as exc:
        append_log(run_id, f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] 平台执行异常: {type(exc).__name__}: {exc}")
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
    append_log(run_id, f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] 正在初始化 NautilusTrader 执行环境...")
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
    snapshot_backend = source_root / "backend"
    strategies_dir = snapshot_backend / "app" / "strategies"
    strategies_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_backend / "app" / "__init__.py").write_text("", encoding="utf-8")
    (strategies_dir / "__init__.py").write_text("", encoding="utf-8")

    module_entry = strategy.get("module") or strategy.get("entrypoint", "")
    module_name = module_entry.partition(":")[0].rsplit(".", 1)[-1]
    strategy_code = strategy.get("code")
    if not strategy_code:
        disk_file = Path(__file__).resolve().parent / "strategies" / f"{module_name}.py"
        if disk_file.exists():
            strategy_code = disk_file.read_text(encoding="utf-8")
        else:
            err_msg = f"数据库和工作区中均未找到策略代码：{module_name}"
            append_log(run_id, f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] {err_msg}")
            await _update(run_id, status=RunStatus.FAILED, stage="策略代码读取失败", progress=100,
                          error_message=err_msg)
            return

    (strategies_dir / f"{module_name}.py").write_text(strategy_code, encoding="utf-8")

    # Copy platform runtime files into sandbox
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

    append_log(run_id, f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] 启动 NautilusTrader BacktestNode 子进程...")
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

    log_file = work_dir / "backtest.log"
    log_lines: list[str] = []

    async def stream_output():
        with log_file.open("a", encoding="utf-8") as f:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                f.write(text)
                f.flush()
                log_lines.append(text)

    try:
        await asyncio.wait_for(
            asyncio.gather(process.wait(), stream_output()),
            timeout=settings.backtest_timeout_seconds,
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        append_log(run_id, f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] 回测执行超时 (超过最大运行时间 {settings.backtest_timeout_seconds} 秒)")
        await _update(run_id, status=RunStatus.FAILED, stage="执行超时", progress=100, error_message="回测超过最大运行时间", finished_at=datetime.now(UTC))
        return

    if process.returncode != 0 or not output_path.exists():
        full_log = "".join(log_lines)
        message = full_log[-8000:] or "回测子进程异常退出"
        append_log(run_id, f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] 回测子进程异常退出 (code={process.returncode})")
        await _update(run_id, status=RunStatus.FAILED, stage="回测失败", progress=100, error_message=message, finished_at=datetime.now(UTC))
        return

    try:
        worker_result = json.loads(output_path.read_text(encoding="utf-8"))
    except Exception as exc:
        err_msg = f"解析回测结果失败: {exc}"
        append_log(run_id, f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] {err_msg}")
        await _update(run_id, status=RunStatus.FAILED, stage="回测失败", progress=100, error_message=err_msg, finished_at=datetime.now(UTC))
        return

    if not worker_result.get("ok"):
        err_msg = worker_result.get("error", "未知错误")
        append_log(run_id, f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] 回测失败: {err_msg}")
        await _update(run_id, status=RunStatus.FAILED, stage="回测失败", progress=100, error_message=err_msg, finished_at=datetime.now(UTC))
        return

    append_log(run_id, f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] 回测执行完成，正在分析与保存绩效报告...")
    await _update(run_id, status=RunStatus.ANALYZING, stage="保存报告与绩效指标", progress=90)
    append_log(run_id, f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] 回测任务已全部完成！")
    await _update(
        run_id,
        status=RunStatus.COMPLETED,
        stage="已完成",
        progress=100,
        metrics=worker_result["metrics"],
        result=worker_result["result"],
        finished_at=datetime.now(UTC),
    )
