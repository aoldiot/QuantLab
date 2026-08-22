from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool
from .config import settings


class Base(DeclarativeBase):
    pass


# Tasks and tests may use separate event loops; asyncpg pooled connections are
# bound to their creating loop. NullPool also prevents stale connections from
# being inherited across worker processes.
engine = create_async_engine(settings.database_url, poolclass=NullPool)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db():
    async with SessionLocal() as session:
        yield session
