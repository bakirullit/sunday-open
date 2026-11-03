from collections.abc import AsyncGenerator
import os

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Force test database URL before importing backend modules
TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/sunday_open_test",
)
os.environ["DATABASE_URL"] = TEST_DB_URL

from backend.config import settings  # noqa: E402
from backend.database import Base, get_session  # noqa: E402
from backend.app import app  # noqa: E402
from backend.bot import default_state_store  # noqa: E402

settings.DATABASE_URL = TEST_DB_URL


@pytest_asyncio.fixture()
async def db_engine() -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(TEST_DB_URL, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "TRUNCATE TABLE records, history, proposals, email_notified, "
                "bot_users, authors, contacts, rsvp, kv RESTART IDENTITY CASCADE"
            )
        )
    default_state_store._mem.clear()
    default_state_store._redis = None
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture()
async def session_maker(
    db_engine: AsyncEngine,
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    maker = async_sessionmaker(
        bind=db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    async def _override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _override_get_session
    yield maker
    app.dependency_overrides.clear()


@pytest_asyncio.fixture()
async def db_session(
    session_maker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    async with session_maker() as session:
        yield session
