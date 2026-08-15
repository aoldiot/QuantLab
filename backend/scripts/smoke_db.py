import asyncio

from sqlalchemy import func, select

from app.db import SessionLocal
from app.main import seed
from app.models import Strategy


async def main() -> None:
    """Smoke-check the database. Run `alembic upgrade head` first: Alembic owns
    the schema, so this script never creates tables."""
    await seed()
    async with SessionLocal() as db:
        count = await db.scalar(select(func.count()).select_from(Strategy))
        print(f"strategies={count}")


if __name__ == "__main__":
    asyncio.run(main())
