import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from app.db import SessionLocal
from app.config import settings


# Tables that must be preserved
PRESERVED_TABLES = {"llm_configuration", "alembic_version"}


async def clean_business_tables(clean_disk_artifacts: bool = True) -> None:
    """Truncate all tables in the database except LLM configuration and Alembic version."""
    async with SessionLocal() as session:
        print("正在查询数据库中的所有表...")
        result = await session.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE';"
            )
        )
        all_tables = [row[0] for row in result.fetchall()]

        # Filter out preserved tables
        tables_to_truncate = [t for t in all_tables if t not in PRESERVED_TABLES]

        if not tables_to_truncate:
            print("⚠️ 未发现需要清空的业务数据表。")
        else:
            print(f"正在清空数据表 ({len(tables_to_truncate)} 张表，已保留 LLM 配置)...")
            truncate_sql = f"TRUNCATE TABLE {', '.join(tables_to_truncate)} CASCADE;"
            await session.execute(text(truncate_sql))
            await session.commit()
            print(f"✅ 成功清空 {len(tables_to_truncate)} 张表（LLM 配置已完整保留）：")
            for table in sorted(tables_to_truncate):
                print(f"   - {table}")

    if clean_disk_artifacts:
        # 1. Clean backtest artifacts
        artifact_dir = settings.artifact_root.resolve()
        if artifact_dir.exists():
            for child in artifact_dir.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                elif child.is_file() and child.name != ".gitkeep":
                    child.unlink(missing_ok=True)
            print(f"✅ 已清理回测运行产物目录: {artifact_dir}")

        # 2. Clean agent worktrees, transcripts, and baselines
        agent_dir = (settings.data_root / "agent").resolve()
        if agent_dir.exists():
            for sub in ("worktrees", "transcripts", "baselines"):
                sub_dir = agent_dir / sub
                if sub_dir.exists():
                    shutil.rmtree(sub_dir, ignore_errors=True)
                    sub_dir.mkdir(parents=True, exist_ok=True)
            print(f"✅ 已清理 Agent 临时工作区与会话日志目录: {agent_dir}")

        # 3. Clean downloads cache/tasks
        downloads_dir = (settings.data_root / "downloads").resolve()
        if downloads_dir.exists():
            for child in downloads_dir.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                elif child.is_file() and child.name != ".gitkeep":
                    child.unlink(missing_ok=True)
            print(f"✅ 已清理数据下载任务与缓存目录: {downloads_dir}")

        # 4. Clean custom user strategies in persistent data dir
        strategies_dir = (settings.data_root / "strategies").resolve()
        if strategies_dir.exists():
            for child in strategies_dir.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                elif child.is_file() and child.name != ".gitkeep":
                    child.unlink(missing_ok=True)
            print(f"✅ 已清理持久化策略目录: {strategies_dir}")


if __name__ == "__main__":
    asyncio.run(clean_business_tables())
