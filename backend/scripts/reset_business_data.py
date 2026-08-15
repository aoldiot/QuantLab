import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from app.db import SessionLocal
from app.config import settings


async def clean_business_tables(clean_disk_artifacts: bool = True) -> None:
    """Truncate all business data tables while keeping configuration intact."""
    business_tables = [
        "research_decisions",
        "research_messages",
        "strategy_specifications",
        "agent_messages",
        "agent_sessions",
        "backtest_runs",
        "research_projects",
        "strategy_versions",
        "strategies",
    ]

    async with SessionLocal() as session:
        print("正在清空业务数据表...")
        truncate_sql = f"TRUNCATE TABLE {', '.join(business_tables)} CASCADE;"
        await session.execute(text(truncate_sql))
        await session.commit()
        print("✅ 9 张业务数据表已成功清空（配置表已完整保留）：")
        for table in business_tables:
            print(f"   - {table}")

    if clean_disk_artifacts:
        # Clean backtest artifacts
        artifact_dir = settings.artifact_root.resolve()
        if artifact_dir.exists():
            for child in artifact_dir.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                elif child.is_file() and child.name != ".gitkeep":
                    child.unlink(missing_ok=True)
            print(f"✅ 已清理回测运行临时产物目录: {artifact_dir}")

        # Clean agent worktrees, transcripts, and baselines
        agent_dir = (settings.data_root / "agent").resolve()
        if agent_dir.exists():
            for sub in ("worktrees", "transcripts", "baselines"):
                sub_dir = agent_dir / sub
                if sub_dir.exists():
                    shutil.rmtree(sub_dir, ignore_errors=True)
                    sub_dir.mkdir(parents=True, exist_ok=True)
            print(f"✅ 已清理 Agent 临时工作区与会话日志目录: {agent_dir}")


if __name__ == "__main__":
    asyncio.run(clean_business_tables())
