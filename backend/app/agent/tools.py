from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..git_versions import code_hash, manifest_hash
from ..models import ResearchProject, Strategy, StrategyVersion
from ..strategy_contract import load_manifest, sanitize_strategy_slug
from ..strategy_files import _path

logger = logging.getLogger(__name__)


async def ensure_strategy_db_record(
    strategy_name: str,
    db: AsyncSession,
    project_id: str | None = None,
) -> tuple[Strategy, StrategyVersion] | None:
    """Ensure Strategy and StrategyVersion exist in the DB and link to ResearchProject if specified."""
    try:
        raw_name = strategy_name.strip() if strategy_name else ""
        slug = sanitize_strategy_slug(raw_name)
        source_path = _path(slug)
        if not source_path.exists():
            return None
        code = source_path.read_text(encoding="utf-8")
        if not code.strip():
            return None

        module = f"app.strategies.{slug}"
        c_hash = code_hash(code)
        try:
            manifest = load_manifest(module)
            s_name = manifest.name
            s_slug = manifest.slug or slug
            s_desc = manifest.description
            s_cat = manifest.category
            s_ver = manifest.version
            s_pschema = manifest.parameter_schema()
            s_dreq = manifest.data_requirements()
            m_hash = manifest_hash(manifest)
        except Exception as m_exc:
            logger.warning("解析策略 Manifest 降级处理 (%s): %s", strategy_name, m_exc)
            s_name = slug.replace("_", " ").title()
            s_slug = slug
            s_desc = "QuantLab 量化研究策略"
            s_cat = "trend"
            s_ver = "1.0.0"
            s_pschema = {}
            s_dreq = {"timeframes": ["15m"], "primary_timeframe": "15m", "multi_symbol": True, "funding": True, "supports_short": True}
            m_hash = c_hash

        slug_candidates = list(dict.fromkeys(filter(None, [
            s_slug,
            slug,
            raw_name,
            s_slug.replace("-", "_") if s_slug else "",
            s_slug.replace("_", "-") if s_slug else "",
            raw_name.replace("-", "_") if raw_name else "",
            raw_name.replace("_", "-") if raw_name else "",
        ])))
        strat = await db.scalar(select(Strategy).where(Strategy.slug.in_(slug_candidates)))
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
                description="QuantLab 研究生成",
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


WRITING_STATUS: dict[str, dict[str, Any]] = {}


def get_writing_log_tool(project_id: str) -> dict[str, Any]:
    """Retrieve the real-time writing progress and log of strategy generator."""
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


def get_strategy_code_tool(strategy_name: str) -> dict[str, Any]:
    """Retrieve the Python code of a strategy."""
    clean_name = strategy_name.strip().lower().replace("-", "_")
    try:
        p = _path(clean_name)
    except Exception as e:
        return {"ok": False, "error": f"策略名称不合法或不存在: {e}"}
    if not p.exists():
        return {"ok": False, "error": f"策略文件不存在：{clean_name}.py"}
    code = p.read_text(encoding="utf-8")
    return {"ok": True, "strategy_name": clean_name, "code": code}