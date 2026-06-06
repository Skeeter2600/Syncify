import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from routers import auth, admin
from db import init_db
from config import Config
from services.downloader import inject_arl, reset_stale_downloads, start_background_worker, wake_queue_worker
from scheduler import start_scheduler

Config.configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Modern lifespan context manager (replaces deprecated on_event)."""
    # ── Startup ──────────────────────────────────────────────────────────────
    logger.info("Initialising database...")
    init_db()
    reset_stale_downloads()  # Fix orphaned 'downloading' items from prior crash/reload

    if Config.DEEZER_ARL:
        try:
            inject_arl(Config.DEEZER_ARL)
            logger.info("Deezer ARL injected into streamrip config.")
        except Exception as exc:
            logger.warning(f"Could not inject ARL on startup: {exc}")

    start_scheduler()
    start_background_worker()
    logger.info("Service ready.")

    yield  # ── running ──────────────────────────────────────────────────────

    # ── Shutdown ─────────────────────────────────────────────────────────────
    from scheduler import scheduler
    if scheduler.running:
        scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped.")


app = FastAPI(
    root_path="/music",
    title="Syncify – Jellyfin Playlist Automation",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(SessionMiddleware, secret_key=Config.SECRET_KEY, max_age=60 * 60 * 24 * 7)  # 7-day session

app.include_router(auth.router)
app.include_router(admin.router)
