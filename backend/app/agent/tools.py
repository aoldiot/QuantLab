from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import py_compile
import re
import shutil
import subprocess
from datetime import UTC, datetime, date
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import SessionLocal
from ..git_versions import code_hash, manifest_hash
from ..llm_config import get_config, sdk_env
from ..models import BacktestRun, ResearchProject, RunStatus, Strategy, StrategyVersion
from ..runner import execute_backtest
from ..strategy_contract import load_manifest, validate_parameters
from ..strategy_files import _path, save_strategy_code, STRATEGY_DIR
from .strategy_verifier import verify_strategy_file, VerificationResult

logger = logging.getLogger(__name__)

NAUTILUS_DEVELOPER_GUIDE = """
【NautilusTrader 策略开发核心速查表与规范】
1. 依赖与模块导入规范：
```python
import pandas as pd
import numpy as np
from decimal import Decimal

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, PositionSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import StrategyManifest, ParameterSpec, StrategyMode
```

2. 核心四大导出结构规范（严禁遗漏任何一项）：
- 结构 1：`StrategyConfig` 子类：包含 `instrument_id: str`、`bar_type: str` 及策略参数定义。
- 结构 2：`Strategy` 子类：
  - `__init__(self, config)`：保存属性与初始化状态。
  - `on_start(self)`：获取 `self.instrument = self.cache.instrument(self.instrument_id)` 并执行 `self.subscribe_bars(self.bar_type)`。
  - `on_bar(self, bar: Bar)`：基于历史 Bar 或指标执行入场、出场、止损止盈与持仓管理。
  - `on_stop(self)`：执行 `self.unsubscribe_bars(self.bar_type)`。
- 结构 3：`calculate_indicators(df: pd.DataFrame, parameters: dict) -> pd.DataFrame`：
  - 向量化计算所有指标。
  - **CRITICAL：必须计算并在返回的 DataFrame 中包含 `plot_config` 中声明的所有指标列！**
- 结构 4：`STRATEGY_MANIFEST = StrategyManifest(...)`：
  - 声明元数据、`parameters` 规范字典（类型为 `ParameterSpec`）及 `plot_config`。
  - **CRITICAL：`plot_config` 必须严格遵循双层嵌套字典规范：**
    ```python
    plot_config = {
        "main_plot": {
            "close": {"type": "line", "color": "#ffffff"},
            "fast_ma": {"type": "line", "color": "#ffaa00"},
            "slow_ma": {"type": "line", "color": "#00aaff"},
        },
        "subplots": {
            # 必须是两层嵌套字典：第一层为面板名称，第二层为 DataFrame 指标列名
            "ATR": {
                "atr": {"type": "line", "color": "#ff55ff"}
            }
        }
3. Pre-Flight 4 级运行期沙盒校验准则（代码将由沙盒全自动检测，请严格遵守）：
- Level 1 语法与结构：必须包含 StrategyConfig 子类、Strategy 子类、calculate_indicators 函数与 STRATEGY_MANIFEST 实例。
- Level 2 契约与实例化：StrategyConfig 子类必须声明所有在 STRATEGY_MANIFEST.parameters 中定义的参数，字段名与类型一致；plot_config 必须为双层嵌套字典，图形类型必须为 line/histogram/bar/area/baseline。
- Level 3 指标计算：calculate_indicators 必须保持输入 DataFrame 行数不变（严禁使用 dropna 裁剪行数），必须计算并在 DataFrame 中新增 plot_config 中声明的全部指标列，且指标不能全为 NaN 或包含 Inf。
- Level 4 运行时契约：Strategy 子类 __init__ 必须调用 super().__init__(config)，必须实现 on_bar(self, bar: Bar) 处理行情事件与下单。
"""

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "execute_backtest",
            "description": "执行 NautilusTrader 策略回测并获取完整的绩效指标报告或报错日志。在策略代码编写/修改成功后调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy_name": {
                        "type": "string",
                        "description": "要回测的策略名称（例如 btc_ema_atr）",
                    },
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "交易标的列表，例如 [\"BTCUSDT\"]",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "回测开始日期，格式 YYYY-MM-DD，如 2024-01-01",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "回测结束日期，格式 YYYY-MM-DD，如 2024-06-30",
                    },
                    "initial_balance": {
                        "type": "number",
                        "description": "初始资金（USDT），默认 10000.0",
                    },
                    "leverage": {
                        "type": "number",
                        "description": "杠杆倍数，默认 1.0",
                    },
                    "parameters": {
                        "type": "object",
                        "description": "策略参数覆盖字典（可选，若不填则使用默认参数）",
                    },
                },
                "required": ["strategy_name", "symbols", "start_date", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_strategy_code",
            "description": "读取当前指定策略的 Python 源代码",
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy_name": {"type": "string", "description": "策略名称"}
                },
                "required": ["strategy_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_available_data",
            "description": "查询本地行情数据 Catalog 中可用的交易标的、K线周期与历史时间跨度",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_backtest_params",
            "description": "向用户提出回测参数方案并生成交互式回测配置卡片。注意：仅当用户在对话中明确说明/要求进行回测时才调用此工具生成方案供用户在弹窗中确认或修改；在用户未明确提出回测请求前严禁擅自调用此工具，更严禁在未确认前直接运行 execute_backtest。",
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy_name": {
                        "type": "string",
                        "description": "策略标识符（如 btc_ema_atr）",
                    },
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "建议的回测标的列表，如 [\"BTCUSDT\"]",
                    },
                    "timeframes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "建议的K线周期列表，如 [\"15m\"] 或 [\"1h\"]",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "回测开始日期，格式 YYYY-MM-DD，如 2024-01-01",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "回测结束日期，格式 YYYY-MM-DD，如 2024-06-30",
                    },
                    "initial_balance": {
                        "type": "number",
                        "description": "初始资金（USDT），默认 10000.0",
                    },
                    "leverage": {
                        "type": "number",
                        "description": "杠杆倍数，默认 1.0",
                    },
                    "parameters": {
                        "type": "object",
                        "description": "策略参数建议字典（如 {\"fast_period\": 12, \"slow_period\": 26, \"atr_period\": 14}）",
                    },
                    "execution_model": {
                        "type": "string",
                        "description": "执行模型，默认 CONSERVATIVE",
                    },
                },
                "required": ["strategy_name", "symbols", "start_date", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_code_approval",
            "description": "【策略编码审批发起】：当策略讨论与逻辑设计完成、准备写码时，必须先调用此工具向用户发起编码审批请求。用户在前端界面批准后，Hermes 将开始编写策略代码。",
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy_name": {
                        "type": "string",
                        "description": "建议的策略命名（小写下划线，如 btc_ema_atr_trend）",
                    },
                    "strategy_summary": {
                        "type": "string",
                        "description": "策略核心构想与逻辑简述",
                    },
                    "key_rules": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "核心规则要点列表（标的周期、入场信号、出场止损、资金管理等）",
                    },
                    "parameter_specs": {
                        "type": "object",
                        "description": "预设参数及其默认值字典",
                    },
                },
                "required": ["strategy_name", "strategy_summary", "key_rules"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_strategy_with_claude",
            "description": "执行策略编写与 4 级 Pre-Flight 运行期沙盒验证。在用户批准编码或明确要求修复时，由 Hermes 调度此工具生成或修复 NautilusTrader 策略代码。",
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy_name": {
                        "type": "string",
                        "description": "策略小写下划线标识符（如 macd_triple_filter_trend）",
                    },
                    "instructions": {
                        "type": "string",
                        "description": "详细的策略编写需求、逻辑定义、指标计算公式及规则说明",
                    },
                    "specification": {
                        "type": "object",
                        "description": "可选的结构化策略规格（包含参数表、图表指标列表、进出场规则）",
                    },
                    "is_fix": {
                        "type": "boolean",
                        "description": "是否为报错修复模式，默认 False",
                    },
                    "error_context": {
                        "type": "string",
                        "description": "若为修复模式，提供回测或验证报错堆栈",
                    },
                },
                "required": ["strategy_name", "instructions"],
            },
        },
    },
]


async def ensure_strategy_db_record(
    strategy_name: str,
    db: AsyncSession,
    project_id: str | None = None,
) -> tuple[Strategy, StrategyVersion] | None:
    """Ensure Strategy and StrategyVersion exist in the DB and link to ResearchProject if specified."""
    try:
        strategy_name = strategy_name.strip().lower()
        source_path = _path(strategy_name)
        if not source_path.exists():
            return None
        code = source_path.read_text(encoding="utf-8")
        if not code.strip():
            return None

        module = f"app.strategies.{strategy_name}"
        c_hash = code_hash(code)
        try:
            manifest = load_manifest(module)
            s_name = manifest.name
            s_slug = manifest.slug
            s_desc = manifest.description
            s_cat = manifest.category
            s_ver = manifest.version
            s_pschema = manifest.parameter_schema()
            s_dreq = manifest.data_requirements()
            m_hash = manifest_hash(manifest)
        except Exception as m_exc:
            logger.warning("解析策略 Manifest 降级处理 (%s): %s", strategy_name, m_exc)
            s_name = strategy_name.replace("_", " ").title()
            s_slug = strategy_name
            s_desc = "Hermes 量化研究策略"
            s_cat = "trend"
            s_ver = "1.0.0"
            s_pschema = {}
            s_dreq = {"timeframes": ["15m"], "primary_timeframe": "15m", "multi_symbol": True, "funding": True, "supports_short": True}
            m_hash = c_hash

        strat = await db.scalar(select(Strategy).where(Strategy.slug == s_slug))
        if strat is None:
            strat = Strategy(
                name=s_name,
                slug=s_slug,
                description=s_desc,
                category=s_cat,
            )
            db.add(strat)
            await db.flush()

        await db.refresh(strat, ["versions"])

        version_obj = None
        for v in strat.versions:
            if v.code_hash == c_hash:
                version_obj = v
                break

        if not version_obj:
            v_name = s_ver
            if any(item.version == v_name for item in strat.versions):
                v_name = f"{s_ver}.{len(strat.versions) + 1}"
            version_obj = StrategyVersion(
                strategy_id=strat.id,
                version=v_name,
                entrypoint=module,
                code=code,
                code_hash=c_hash,
                parameter_schema=s_pschema,
                data_requirements=s_dreq,
                manifest_hash=m_hash,
                description="Hermes 研究生成",
            )
            db.add(version_obj)
            await db.flush()
            await db.refresh(version_obj)

        if project_id:
            project = await db.get(ResearchProject, project_id)
            if project:
                project.strategy_id = strat.id
                await db.flush()

        await db.commit()
        return strat, version_obj
    except Exception as exc:
        logger.warning("同步策略数据库记录失败 (%s): %s", strategy_name, exc)
        return None


def _validate_strategy_file(file_path: Path) -> tuple[bool, str]:
    """Compatibility wrapper that calls the 4-level Pre-Flight strategy verifier."""
    res = verify_strategy_file(file_path)
    if res.ok:
        return True, "OK"
    return False, res.error_message or res.summary


WRITING_STATUS: dict[str, dict[str, Any]] = {}


def get_writing_log_tool(project_id: str) -> dict[str, Any]:
    """Retrieve the real-time writing progress and log of Claude strategy generator."""
    if project_id in WRITING_STATUS:
        return WRITING_STATUS[project_id]
    log_file = settings.artifact_root.resolve() / f"research_{project_id}" / "writing.log"
    if log_file.exists():
        try:
            return {
                "status": "COMPLETED",
                "stage": "编写已完成",
                "progress": 100,
                "strategy_name": "",
                "logs": log_file.read_text(encoding="utf-8", errors="replace"),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        except Exception:
            pass
    return {
        "status": "IDLE",
        "stage": "就绪",
        "progress": 0,
        "strategy_name": "",
        "logs": "",
        "updated_at": datetime.now(UTC).isoformat(),
    }


async def write_strategy_with_claude(
    strategy_name: str,
    instructions: str,
    is_fix: bool = False,
    error_context: str | None = None,
    specification: dict[str, Any] | None = None,
    project_id: str | None = None,
    db: AsyncSession | None = None,
) -> dict[str, Any]:
    """Execute Claude Code to write or repair NautilusTrader strategy code with 4-level pre-flight verification."""
    strategy_name = strategy_name.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", strategy_name):
        return {
            "ok": False,
            "error": "策略名称格式不合法，必须使用小写字母、数字和下划线，且以字母开头",
        }

    repo_path = settings.strategy_repo_path.resolve()
    target_file = (repo_path / "backend/app/strategies" / f"{strategy_name}.py").resolve()
    target_file.parent.mkdir(parents=True, exist_ok=True)

    # Initialize artifact writing log directory & file
    work_dir = settings.artifact_root.resolve() / f"research_{project_id or 'default'}"
    work_dir.mkdir(parents=True, exist_ok=True)
    log_file = work_dir / "writing.log"
    log_file.write_text("", encoding="utf-8")

    def _update_status(stage: str, progress: int, status: str = "RUNNING", log_line: str | None = None, steps: list[dict[str, Any]] | None = None):
        if log_line:
            timestamp = datetime.now().strftime("%H:%M:%S")
            formatted = f"[{timestamp}] {log_line}\n"
            with log_file.open("a", encoding="utf-8") as f:
                f.write(formatted)
                f.flush()
        if project_id:
            try:
                full_logs = log_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                full_logs = ""
            status_data: dict[str, Any] = {
                "status": status,
                "stage": stage,
                "progress": progress,
                "strategy_name": strategy_name,
                "logs": full_logs,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            if steps is not None:
                status_data["steps"] = steps
            WRITING_STATUS[project_id] = status_data

    _update_status("正在构建策略开发规范与上下文...", 10, log_line=f"开始为策略「{strategy_name}」构建开发规范与提示词...")

    existing_code = ""
    if target_file.exists():
        existing_code = target_file.read_text(encoding="utf-8")

    spec_section = ""
    if specification:
        spec_section = f"\n【结构化策略规格定义 (Specification)】\n```json\n{json.dumps(specification, ensure_ascii=False, indent=2)}\n```\n"

    # Build detailed prompt for Claude
    fix_prompt = ""
    if is_fix and error_context:
        fix_prompt = f"""
【回测报错修复模式】
本次回测运行时报错，请仔细分析以下错误堆栈并修复 backend/app/strategies/{strategy_name}.py 中的代码：
{error_context}

现有代码参考：
```python
{existing_code[:10000]}
```
"""

    base_prompt = f"""
你正在为 QuantLab 量化交易系统编写/修改 NautilusTrader 策略文件：`backend/app/strategies/{strategy_name}.py`。

{NAUTILUS_DEVELOPER_GUIDE}

【编写任务与需求】
{instructions}
{spec_section}
{fix_prompt}

【严格要求】
1. 只修改或生成 `backend/app/strategies/{strategy_name}.py` 文件。
2. 必须包含 StrategyConfig 子类、Strategy 子类、calculate_indicators 函数和 STRATEGY_MANIFEST 对象。
3. 确保所有指标和信号严格向量化与事件驱动计算正确，杜绝未来函数。
4. 编写完成后，确保通过 Python 语法和 QuantLab 4 级契约校验。
"""

    # Retrieve environment variables for Claude CLI
    env = os.environ.copy()
    if db:
        try:
            cfg = await get_config(db)
            env.update(sdk_env(cfg))
        except Exception:
            pass

    # Ensure PATH contains typical locations for claude binary
    extra_paths = [
        str(Path.home() / ".local/bin"),
        "/usr/local/bin",
        "/opt/homebrew/bin",
        "/usr/bin",
        "/bin",
    ]
    cur_path = env.get("PATH", "")
    for p in extra_paths:
        if p not in cur_path:
            cur_path = f"{p}:{cur_path}"
    env["PATH"] = cur_path

    claude_bin = (
        shutil.which("claude", path=cur_path)
        or (Path.home() / ".local/bin/claude").as_posix()
        or "/usr/local/bin/claude"
        or "/opt/homebrew/bin/claude"
        or "claude"
    )

    max_self_heal_turns = 2
    current_prompt = base_prompt
    stdout_lines: list[str] = []

    for heal_turn in range(max_self_heal_turns + 1):
        is_healing = heal_turn > 0
        if is_healing:
            _update_status(
                f"正在执行第 {heal_turn}/{max_self_heal_turns} 轮自动自愈修复...",
                min(80, 50 + heal_turn * 15),
                log_line=f"[SELF-HEAL] 触发自闭环自愈修复 (第 {heal_turn} 轮)...",
            )
        else:
            _update_status("已启动代码编写进程，正在分析并编写策略代码...", 30, log_line=f"启动策略编写: target={strategy_name}.py")

        cmd = [
            claude_bin,
            "-p",
            current_prompt,
            "--dangerously-skip-permissions",
        ]

        logger.info("正在调用代码编写进程：%s (turn=%d) ...", strategy_name, heal_turn)

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(repo_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )

            async def _stream_claude_output():
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace")
                    stdout_lines.append(text)
                    with log_file.open("a", encoding="utf-8") as f:
                        f.write(text)
                        f.flush()
                    cur_prog = min(85, 30 + len(stdout_lines))
                    _update_status("代码生成与文件修改中...", cur_prog)

            await asyncio.wait_for(
                asyncio.gather(process.wait(), _stream_claude_output()),
                timeout=300,
            )
        except asyncio.TimeoutError:
            try:
                process.kill()
            except Exception:
                pass
            err_msg = "Claude CLI 编写代码超时 (300s)"
            _update_status("编写超时", 100, status="FAILED", log_line=f"[ERROR] {err_msg}")
            return {"ok": False, "error": err_msg}
        except Exception as exc:
            err_msg = f"调用 Claude CLI 失败：{exc}"
            _update_status("调用失败", 100, status="FAILED", log_line=f"[ERROR] {err_msg}")
            return {"ok": False, "error": err_msg}

        _update_status(
            "代码生成完成，正在执行 4 级 Pre-Flight 策略验证沙盒...",
            88,
            log_line="[VERIFICATION] 代码生成退出，开始执行 4 级 Pre-Flight 运行期沙盒检测...",
        )

        # Check canonical location
        eval_file = target_file
        if not eval_file.exists():
            alt_file = (STRATEGY_DIR / f"{strategy_name}.py").resolve()
            if alt_file.exists():
                eval_file = alt_file

        # Run 4-level pre-flight verification
        v_res = verify_strategy_file(eval_file, strategy_name=strategy_name)

        # Log individual step results
        for step in v_res.steps:
            mark = "✓" if step.ok else "✗"
            _update_status(
                f"验证阶段: {step.level} {step.name}",
                90,
                log_line=f"[{mark} {step.level}] {step.name}: {step.message}",
            )

        if v_res.ok:
            # Code is fully verified, sync to persistent storage
            generated_code = eval_file.read_text(encoding="utf-8")
            save_strategy_code(strategy_name, generated_code)

            _update_status(
                "策略代码已成功生成并通过 4 级 Pre-Flight 校验！",
                100,
                status="COMPLETED",
                log_line=f"[SUCCESS] 策略 {strategy_name}.py 4 级 Pre-Flight 校验全部通过，已成功保存与同步数据库！",
                steps=[s.__dict__ for s in v_res.steps],
            )

            # Sync to DB Strategy & StrategyVersion tables
            if db is not None:
                await ensure_strategy_db_record(strategy_name, db, project_id=project_id)
            else:
                async with SessionLocal() as session:
                    await ensure_strategy_db_record(strategy_name, session, project_id=project_id)

            full_stdout = "".join(stdout_lines)
            return {
                "ok": True,
                "status": "SUCCESS",
                "strategy_name": strategy_name,
                "message": f"策略 {strategy_name}.py 代码已成功生成并通过 4 级 Pre-Flight 沙盒验证！",
                "validation": v_res.to_dict(),
                "code_snippet": generated_code[:3000] + ("\n...(已截断)" if len(generated_code) > 3000 else ""),
                "code_length": len(generated_code),
                "log_preview": full_stdout[-2000:] if len(full_stdout) > 2000 else full_stdout,
            }

        # If verification failed and we have healing attempts left, feed the error back
        if heal_turn < max_self_heal_turns:
            _update_status(
                f"Pre-Flight 校验未通过 ({v_res.failed_level})，正在准备自动修复...",
                85,
                log_line=f"[WARN] 校验未通过 ({v_res.failed_level}): {v_res.error_message}。准备触发自闭环自愈修复...",
            )
            current_code = eval_file.read_text(encoding="utf-8") if eval_file.exists() else ""
            current_prompt = f"""
【QuantLab 策略 Pre-Flight 自动化验证沙盒未通过 ({v_res.failed_level})】
你在编写 `backend/app/strategies/{strategy_name}.py` 时，沙盒在 `{v_res.failed_level}` 级别检测到以下错误：

- 错误摘要: {v_res.summary}
- 错误详情: {v_res.error_message}
- 修复建议: {v_res.suggestion}

当前代码内容如下：
```python
{current_code[:12000]}
```

【修复任务】
请针对上述具体错误与建议，直接修改并修复 `backend/app/strategies/{strategy_name}.py` 文件，确保满足所有契约要求，消除上述报错。
"""
        else:
            # All healing attempts exhausted
            _update_status(
                f"策略 4 级校验未通过 ({v_res.failed_level})",
                100,
                status="FAILED",
                log_line=f"[ERROR] 策略 4 级校验未通过: [{v_res.failed_level}] {v_res.error_message}",
                steps=[s.__dict__ for s in v_res.steps],
            )
            full_stdout = "".join(stdout_lines)
            return {
                "ok": False,
                "status": "VALIDATION_FAILED",
                "strategy_name": strategy_name,
                "error": f"Pre-Flight 4 级校验未通过 [{v_res.failed_level}]: {v_res.error_message}\n修复建议: {v_res.suggestion}\n\nClaude 输出：\n{full_stdout[:1000]}",
                "validation": v_res.to_dict(),
            }

    return {"ok": False, "error": "未知状态退出"}


async def execute_backtest_tool(
    strategy_name: str,
    symbols: list[str],
    start_date: str,
    end_date: str,
    initial_balance: float = 10000.0,
    leverage: float = 1.0,
    parameters: dict[str, Any] | None = None,
    project_id: str | None = None,
    db: AsyncSession | None = None,
) -> dict[str, Any]:
    """Publish strategy version and execute NautilusTrader backtest."""
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

    # Use provided session or create a new one
    async def _run_in_db(s: AsyncSession) -> dict[str, Any]:
        # 1. Ensure Strategy & StrategyVersion exist
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

        # Check existing versions
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
                description="Hermes 自主研究所发布",
            )
            s.add(version_obj)
            await s.flush()
            await s.refresh(version_obj)

        resolved_params = validate_parameters(manifest, parameters or {})

        # 2. Create BacktestRun
        clean_symbols = [s_item.strip() for s_item in symbols if s_item.strip()]
        timeframes = manifest.data_requirements().get("timeframes", ["15m"])
        run_name = f"{manifest.name}_{start_date}_{end_date}"

        config_dict = {
            "name": run_name,
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
            "funding": bool(manifest.data_requirements().get("funding", True)),
            "ignore_missing_data": True,
            "strategy_version": {
                "version": version_obj.version,
                "code_hash": version_obj.code_hash,
                "manifest_hash": version_obj.manifest_hash,
            },
        }

        run = BacktestRun(
            name=run_name,
            strategy_version_id=version_obj.id,
            config=config_dict,
            research_project_id=project_id,
            stage="正在启动回测",
            progress=5,
            status=RunStatus.RUNNING,
        )
        s.add(run)
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

        # Poll for completion with timeout
        timeout_seconds = 300
        poll_interval = 2.0
        start_time = asyncio.get_event_loop().time()

        while (asyncio.get_event_loop().time() - start_time) < timeout_seconds:
            await asyncio.sleep(poll_interval)
            await s.refresh(run)
            if run.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                break

        if run.status == RunStatus.COMPLETED:
            return {
                "ok": True,
                "status": "COMPLETED",
                "run_id": run.id,
                "strategy_name": strategy_name,
                "metrics": run.metrics or {},
                "summary": f"回测成功完成！收益率: {run.metrics.get('total_return', 'N/A') if run.metrics else 'N/A'}, 夏普比率: {run.metrics.get('sharpe_ratio', 'N/A') if run.metrics else 'N/A'}, 最大回撤: {run.metrics.get('max_drawdown', 'N/A') if run.metrics else 'N/A'}, 胜率: {run.metrics.get('win_rate', 'N/A') if run.metrics else 'N/A'}, 交易次数: {run.metrics.get('total_trades', 'N/A') if run.metrics else 'N/A'}",
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
                "message": f"回测已在后台运行中 (当前进度 {run.progress}%)，可稍后查询结果",
            }

    if db is not None:
        return await _run_in_db(db)
    else:
        async with SessionLocal() as session:
            return await _run_in_db(session)


def get_strategy_code_tool(strategy_name: str) -> dict[str, Any]:
    """Retrieve the Python code of a strategy."""
    strategy_name = strategy_name.strip().lower()
    p = _path(strategy_name)
    if not p.exists():
        return {"ok": False, "error": f"策略文件不存在：{strategy_name}.py"}
    code = p.read_text(encoding="utf-8")
    return {"ok": True, "strategy_name": strategy_name, "code": code}


def get_available_data_tool() -> dict[str, Any]:
    """Check catalog coverage for available backtest symbols."""
    catalog_path = settings.catalog_path.resolve()
    if not catalog_path.exists():
        return {"ok": False, "error": f"行情目录未找到：{catalog_path}"}

    instruments = []
    for d in catalog_path.iterdir():
        if d.is_dir() and not d.name.startswith("."):
            instruments.append(d.name)

    return {
        "ok": True,
        "catalog_path": str(catalog_path),
        "available_instruments": instruments[:50],
        "total_instruments": len(instruments),
    }


async def dispatch_tool_call(
    tool_name: str,
    arguments: dict[str, Any],
    project_id: str | None = None,
    db: AsyncSession | None = None,
) -> dict[str, Any]:
    """Dispatch a tool call requested by Hermes Agent."""
    logger.info("执行 Hermes 工具调用：%s, 参数：%s", tool_name, arguments)
    if tool_name == "write_strategy_with_claude":
        return await write_strategy_with_claude(
            strategy_name=arguments.get("strategy_name", ""),
            instructions=arguments.get("instructions", ""),
            is_fix=arguments.get("is_fix", False),
            error_context=arguments.get("error_context"),
            specification=arguments.get("specification"),
            project_id=project_id,
            db=db,
        )
    elif tool_name == "propose_code_approval":
        return {
            "ok": True,
            "status": "PENDING_USER_APPROVAL",
            "message": "策略设计方案与代码结构已就绪，已向用户发起编码审批请求，等待用户确认。",
            "approval_data": arguments,
        }
    elif tool_name == "propose_backtest_params":
        return {
            "ok": True,
            "status": "PROPOSED",
            "message": "回测参数方案已生成，已展示在界面供用户确认或在回测管理中微调。",
            "backtest_params": arguments,
        }
    elif tool_name == "execute_backtest":
        return await execute_backtest_tool(
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
    elif tool_name == "get_strategy_code":
        return get_strategy_code_tool(arguments.get("strategy_name", ""))
    elif tool_name == "get_available_data":
        return get_available_data_tool()
    else:
        return {"ok": False, "error": f"未知的工具名称：{tool_name}"}
