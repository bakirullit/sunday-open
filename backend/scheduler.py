import logging
from typing import Callable, Awaitable
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.database import async_session_factory
from backend.moderation import send_sunday_notifications

logger = logging.getLogger("backend.scheduler")

LOCK_ID_EMAIL_DIGEST = 700105
LOCK_ID_DAILY_JOB = 700109


async def try_with_db_advisory_lock(
    session: AsyncSession,
    lock_id: int,
    fn: Callable[[AsyncSession], Awaitable[str]],
) -> str:
    """
    Acquire a transaction-level PostgreSQL advisory lock (or execute directly on SQLite)
    so scheduled jobs never fire concurrently or duplicate across instances.
    """
    dialect = session.bind.dialect.name if session.bind else ""
    if dialect == "postgresql":
        res = await session.execute(
            text("SELECT pg_try_advisory_xact_lock(:id)"), {"id": lock_id}
        )
        acquired = bool(res.scalar())
        if not acquired:
            return "Busy; retry on the next trigger"
    return await fn(session)


async def run_email_digest_job() -> str:
    async with async_session_factory() as session:
        async with session.begin():
            return await try_with_db_advisory_lock(
                session, LOCK_ID_EMAIL_DIGEST, send_sunday_notifications
            )


def create_scheduler(
    daily_job_fn: Callable[[], Awaitable[str]] | None = None,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.TZ)
    scheduler.add_job(
        run_email_digest_job,
        trigger=IntervalTrigger(minutes=5, timezone=settings.TZ),
        id="sunday_email_digest",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if daily_job_fn is not None:
        scheduler.add_job(
            daily_job_fn,
            trigger=CronTrigger(hour=9, minute=0, timezone=settings.TZ),
            id="sunday_bot_daily",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    return scheduler
