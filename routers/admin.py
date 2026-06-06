import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from config import Config
from db import DownloadQueue, User, get_session
from services.downloader import inject_arl, process_queue
from services.playlist import PlaylistService

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _redirect(url: str, **params: str) -> RedirectResponse:
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    return RedirectResponse(url=url, status_code=303)


def _require_admin(request: Request) -> None:
    if not request.session.get("user_id") or not request.session.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required.")


# ── Admin index ───────────────────────────────────────────────────────────────

@router.get("/admin")
async def admin_index(request: Request, db: Session = Depends(get_session)):
    _require_admin(request)

    users = db.exec(select(User)).all()
    all_downloads = db.exec(select(DownloadQueue).order_by(DownloadQueue.created_at.desc()).limit(50)).all()

    # System stats
    pending = sum(1 for d in all_downloads if d.status == "pending")
    downloading = sum(1 for d in all_downloads if d.status == "downloading")
    failed = sum(1 for d in all_downloads if d.status == "failed")
    done = sum(1 for d in all_downloads if d.status == "done")

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "session": request.session,
            "users": users,
            "downloads": all_downloads,
            "arl_set": bool(Config.DEEZER_ARL),
            "stats": {"pending": pending, "downloading": downloading, "failed": failed, "done": done},
            "success_msg": request.query_params.get("msg"),
            "service_base_url": Config.SERVICE_BASE_URL,
        },
    )


# ── ARL update ────────────────────────────────────────────────────────────────

@router.post("/admin/update-arl")
async def update_arl(request: Request, deezer_arl: str = Form(...)):
    _require_admin(request)
    Config.DEEZER_ARL = deezer_arl.strip()
    try:
        inject_arl(Config.DEEZER_ARL)
        logger.info("Deezer ARL updated via admin panel.")
    except Exception as exc:
        logger.error(f"ARL update failed: {exc}")
        return _redirect("/music/admin", msg="ARL+update+failed.+Check+logs.")
    return _redirect("/music/admin", msg="Deezer+ARL+updated+successfully!")


# ── Trigger operations ────────────────────────────────────────────────────────

@router.post("/admin/trigger-sync")
async def trigger_sync(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_session),
):
    _require_admin(request)
    users = db.exec(select(User)).all()
    ps = PlaylistService()

    async def _run():
        for u in users:
            try:
                await ps.sync_user_playlists(u)
            except Exception as exc:
                logger.error(f"Admin trigger daily sync failed for {u.jellyfin_username}: {exc}")

    background_tasks.add_task(_run)
    return _redirect("/music/admin", msg="Daily+sync+triggered+in+background.")


@router.post("/admin/trigger-weekly")
async def trigger_weekly(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_session),
):
    _require_admin(request)
    users = db.exec(select(User)).all()
    ps = PlaylistService()

    async def _run():
        for u in users:
            try:
                await ps.sync_weekly_exploration(u)
            except Exception as exc:
                logger.error(f"Admin trigger weekly sync failed for {u.jellyfin_username}: {exc}")

    background_tasks.add_task(_run)
    return _redirect("/music/admin", msg="Weekly+exploration+triggered+in+background.")


@router.post("/admin/trigger-downloads")
async def trigger_downloads(request: Request, background_tasks: BackgroundTasks):
    _require_admin(request)

    async def _run():
        loop = asyncio.get_running_loop()
        count = await loop.run_in_executor(None, process_queue)
        logger.info(f"Admin-triggered download queue processed {count} item(s).")

    background_tasks.add_task(_run)
    return _redirect("/music/admin", msg="Download+queue+processing+started.")


@router.post("/admin/reset-failed-downloads")
async def reset_failed_downloads(request: Request, db: Session = Depends(get_session)):
    _require_admin(request)
    failed_items = db.exec(select(DownloadQueue).where(DownloadQueue.status == "failed")).all()
    count = len(failed_items)
    if count > 0:
        for item in failed_items:
            item.status = "pending"
            db.add(item)
        db.commit()
        logger.info(f"Reset {count} failed downloads to pending.")
    return _redirect("/music/admin", msg=f"Reset+{count}+failed+downloads+to+pending.")


@router.post("/admin/clear-history")
async def clear_history(request: Request, db: Session = Depends(get_session)):
    _require_admin(request)
    items = db.exec(select(DownloadQueue).where(DownloadQueue.status.in_(["done", "failed"]))).all()
    count = len(items)
    for item in items:
        db.delete(item)
    db.commit()
    logger.info(f"Cleared {count} completed/failed downloads from database.")
    return _redirect("/music/admin", msg=f"Cleared+{count}+history+items.")


@router.post("/admin/clear-all-queue")
async def clear_all_queue(request: Request, db: Session = Depends(get_session)):
    _require_admin(request)
    items = db.exec(select(DownloadQueue)).all()
    count = len(items)
    for item in items:
        db.delete(item)
    db.commit()
    logger.info(f"Cleared entire download queue ({count} items) from database.")
    return _redirect("/music/admin", msg=f"Cleared+entire+download+queue+({count}+items).")


