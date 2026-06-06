import logging
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlmodel import Session, select
from db import engine, User
from services.playlist import PlaylistService, check_ready_playlist_jobs
from services.downloader import wake_queue_worker

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone=ZoneInfo("America/Chicago"))


async def daily_job() -> None:
    logger.info("Scheduled daily playlist sync starting...")
    ps = PlaylistService()
    with Session(engine) as session:
        users = session.exec(select(User)).all()
    for user in users:
        try:
            await ps.sync_user_playlists(user)
        except Exception as exc:
            logger.error(f"Daily sync failed for {user.jellyfin_username}: {exc}", exc_info=True)
    logger.info("Daily playlist sync complete — downloads queued, playlists will be created when ready.")


async def weekly_job() -> None:
    logger.info("Scheduled weekly exploration sync starting...")
    ps = PlaylistService()
    with Session(engine) as session:
        users = session.exec(select(User)).all()
    for user in users:
        try:
            await ps.sync_weekly_exploration(user)
        except Exception as exc:
            logger.error(f"Weekly sync failed for {user.jellyfin_username}: {exc}", exc_info=True)
    logger.info("Weekly exploration sync complete — downloads queued, playlists will be created when ready.")


async def check_playlist_jobs() -> None:
    """Run every 5 minutes: create any playlists whose downloads have all finished."""
    try:
        await check_ready_playlist_jobs()
    except Exception as exc:
        logger.error(f"check_ready_playlist_jobs failed: {exc}", exc_info=True)


def start_scheduler() -> None:
    central = ZoneInfo("America/Chicago")

    # Daily at midnight CST
    scheduler.add_job(
        daily_job,
        trigger=CronTrigger(hour=0, minute=0, timezone=central),
        id="daily_sync",
        replace_existing=True,
        misfire_grace_time=300,
    )

    # Weekly Exploration every Monday at midnight CST
    scheduler.add_job(
        weekly_job,
        trigger=CronTrigger(day_of_week="mon", hour=0, minute=0, timezone=central),
        id="weekly_sync",
        replace_existing=True,
        misfire_grace_time=300,
    )

    # Check for ready playlist jobs every 5 minutes
    scheduler.add_job(
        check_playlist_jobs,
        trigger="interval",
        minutes=5,
        id="playlist_job_check",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("APScheduler started (timezone: America/Chicago).")
