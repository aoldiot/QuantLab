from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.strategy_verifier import verify_strategy_file
from app.git_versions import code_hash, manifest_hash
from app.models import ResearchProject, Strategy, StrategyVersion
from app.strategy_contract import load_manifest, sanitize_strategy_slug
from app.strategy_files import _path, save_strategy_code

logger = logging.getLogger(__name__)


def get_strategy_code(strategy_name: str) -> str | None:
    """Retrieve strategy python code by name."""
    strategy_name = strategy_name.strip().lower()
    p = _path(strategy_name)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def save_strategy_file(strategy_name: str, code: str) -> Path:
    """Save code into backend/app/strategies/{strategy_name}.py."""
    strategy_name = strategy_name.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", strategy_name):
        raise ValueError(f"无效的策略名称: {strategy_name}")
    save_strategy_code(strategy_name, code)
    return _path(strategy_name)


def verify_strategy_code(strategy_name: str, custom_path: Path | None = None) -> dict[str, Any]:
    """Run the 4-level Pre-Flight sandbox verification."""
    strategy_name = strategy_name.strip().lower()
    target = custom_path or _path(strategy_name)
    if not target.exists():
        return {
            "ok": False,
            "error_message": f"未找到策略文件: {target}",
            "failed_level": "FILE_NOT_FOUND",
            "steps": [],
        }

    res = verify_strategy_file(target, strategy_name=strategy_name)
    return res.to_dict()


async def ensure_strategy_db_record(
    strategy_name: str,
    db: AsyncSession,
    project_id: str | None = None,
) -> tuple[Strategy, StrategyVersion] | None:
    """Ensure Strategy and StrategyVersion exist in DB and link with project."""
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
            s_desc = "QuantLab 研究策略"
            s_cat = "trend"
            s_ver = "1.0.0"
            s_pschema = {}
            s_dreq = {
                "timeframes": ["15m"],
                "primary_timeframe": "15m",
                "multi_symbol": True,
                "funding": True,
                "supports_short": True,
            }
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
                description="QuantLab 策略开发发布",
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
