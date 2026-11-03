from collections.abc import AsyncGenerator
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from backend.config import settings


class Base(DeclarativeBase):
    pass


def build_engine(database_url: str | None = None) -> AsyncEngine:
    url = database_url or settings.DATABASE_URL
    kwargs: dict = {"echo": False, "future": True}
    if url.startswith("postgresql"):
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 20
        kwargs["pool_pre_ping"] = True
    return create_async_engine(url, **kwargs)


engine: AsyncEngine = build_engine()
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session

