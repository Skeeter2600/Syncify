import asyncio
import datetime
import logging
import urllib.parse
import httpx

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlmodel import Session, select

from config import Config
from db import DownloadQueue, User, get_session
from services.jellyfin import JellyfinClient
from services.listenbrainz import ListenBrainzClient
from services.playlist import PlaylistService
from services.musicbrainz import MusicBrainzClient
from services.downloader import (
    queue_download,
    queue_album_download,
    preview_deezer_search,
    process_queue,
    wake_queue_worker,
)

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _redirect(url: str, **params: str) -> RedirectResponse:
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    return RedirectResponse(url=url, status_code=303)


def _require_login(request: Request) -> int:
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(status_code=303, headers={"Location": "/music/"})
    return uid


# ── Landing / Login ───────────────────────────────────────────────────────────

@router.get("/")
async def index(request: Request, db: Session = Depends(get_session)):
    uid = request.session.get("user_id")
    if uid:
        user = db.get(User, uid)
        if user:
            return templates.TemplateResponse(
                request=request,
                name="index.html",
                context={
                    "session": request.session,
                    "user": user,
                    "jellyfin_url": Config.JELLYFIN_URL,
                }
            )
    return templates.TemplateResponse(request=request, name="index.html", context={"session": None})


@router.get("/search-artists")
async def search_artists_route(request: Request, q: str = "", mode: str = "artist"):
    _require_login(request)
    mode = mode if mode in ("artist", "track", "album") else "artist"
    results = []

    if q.strip():
        if mode == "artist":
            mb = MusicBrainzClient()
            raw = await mb.search_artists(q.strip())

            async def with_thumb(artist: dict) -> dict:
                _, thumb = await mb._fetch_wiki_bio_by_mbid(artist["mbid"])
                return {**artist, "thumbnail": thumb}

            results = list(await asyncio.gather(*[with_thumb(a) for a in raw]))
        else:
            # Track or album — use Deezer via thread executor (blocking call)
            results = await asyncio.get_event_loop().run_in_executor(
                None, lambda: preview_deezer_search(q.strip(), search_type=mode)
            )

    return templates.TemplateResponse(
        request=request,
        name="search_results.html",
        context={"session": request.session, "query": q, "mode": mode, "results": results}
    )


@router.get("/artist/{mbid}")
async def artist_details_route(request: Request, mbid: str):
    _require_login(request)
    mb = MusicBrainzClient()
    details = await mb.get_artist_details(mbid)
    if not details:
        raise HTTPException(status_code=404, detail="Artist not found")

    jf = JellyfinClient()
    artist_name = details["name"]

    # Check all releases against the Jellyfin library in parallel
    async def check_release(rel: dict) -> dict:
        jellyfin_id = await jf.search_album(rel["title"], artist_name)
        return {**rel, "in_library": bool(jellyfin_id), "jellyfin_id": jellyfin_id or ""}

    catalog_with_status = {}
    for group_type, releases in details["catalog"].items():
        catalog_with_status[group_type] = await asyncio.gather(*[check_release(r) for r in releases])

    details["catalog"] = catalog_with_status
    return templates.TemplateResponse(
        request=request,
        name="artist.html",
        context={"session": request.session, "artist": details, "jellyfin_url": Config.JELLYFIN_URL}
    )


@router.post("/api/queue-download-direct")
async def queue_download_direct(
    request: Request,
    artist_name: str = Form(...),
    request_type: str = Form(...),  # 'album' or 'track'
    title: str = Form(...),
):
    uid = _require_login(request)

    if request_type == "album":
        queued = queue_album_download(album_name=title, artist=artist_name, user_id=uid)
    else:
        queued = queue_download(track_name=title, artist=artist_name, album="", user_id=uid)

    if queued:
        # Signal the persistent background download thread to start immediately
        wake_queue_worker()
        return JSONResponse({"status": "success", "message": f"Queued {request_type} '{title}' by {artist_name}"})
    return JSONResponse({"status": "error", "message": "Already pending in queue"}, status_code=400)


def _run_queue_bg() -> None:
    """Legacy helper kept for compatibility — use wake_queue_worker() for new code."""
    try:
        process_queue()
    except Exception:
        logger.exception("Background queue processing failed")


@router.get("/api/search-deezer")
async def search_deezer_api(request: Request, q: str = "", type: str = "track"):
    """Proxy Deezer search for the frontend — returns track/album results."""
    _require_login(request)
    if not q.strip():
        return {"results": []}
    results = await asyncio.get_event_loop().run_in_executor(
        None, lambda: preview_deezer_search(q.strip(), search_type=type)
    )
    return {"results": results}


@router.get("/api/queue-count")
def queue_count(request: Request, db: Session = Depends(get_session)):
    if not request.session.get("user_id"):
        return {"pending": 0, "downloading": 0, "total": 0}
    pending = db.scalar(select(func.count(DownloadQueue.id)).where(DownloadQueue.status == "pending")) or 0
    downloading = db.scalar(select(func.count(DownloadQueue.id)).where(DownloadQueue.status == "downloading")) or 0
    return {"pending": pending, "downloading": downloading, "total": pending + downloading}

@router.get("/album/{mbid}")
async def album_details_route(request: Request, mbid: str):
    """Dedicated album detail page — tracks + per-track Jellyfin library status."""
    _require_login(request)
    mb = MusicBrainzClient()
    jf = JellyfinClient()

    # Fetch release group metadata using direct httpx calls
    _MB_BASE = "https://musicbrainz.org/ws/2"
    _MB_HEADERS = {"User-Agent": "Syncify/1.0.0 (https://beckandersonmedia.com; contact@beckandersonmedia.com)"}

    async with httpx.AsyncClient(headers=_MB_HEADERS, timeout=10.0) as client:
        # Release group metadata
        rg_resp = await client.get(f"{_MB_BASE}/release-group/{mbid}", params={"inc": "releases+artists", "fmt": "json"})
        if rg_resp.status_code != 200:
            raise HTTPException(status_code=404, detail="Album not found")
        rg = rg_resp.json()

        releases = rg.get("releases", [])
        artist_name = rg.get("artist-credit", [{}])[0].get("artist", {}).get("name", "Unknown Artist") if rg.get("artist-credit") else "Unknown Artist"
        artist_mbid = rg.get("artist-credit", [{}])[0].get("artist", {}).get("id", "") if rg.get("artist-credit") else ""
        album_title = rg.get("title", "")
        album_date = rg.get("first-release-date", "") or ""
        album_year = album_date[:4] if album_date else "—"
        cover_url = f"https://coverartarchive.org/release-group/{mbid}/front-500"

        # Fetch full track list from first release
        tracks = []
        if releases:
            rel_resp = await client.get(f"{_MB_BASE}/release/{releases[0]['id']}", params={"inc": "recordings", "fmt": "json"})
            if rel_resp.status_code == 200:
                for medium in rel_resp.json().get("media", []):
                    for t in medium.get("tracks", []):
                        dur_ms = t.get("length")
                        dur_str = ""
                        if dur_ms:
                            mins = dur_ms // 60000
                            secs = (dur_ms % 60000) // 1000
                            dur_str = f"{mins}:{secs:02d}"
                        tracks.append({
                            "position": t.get("position"),
                            "title": t.get("title", ""),
                            "length_ms": dur_ms,
                            "duration": dur_str,
                            "recording_id": t.get("recording", {}).get("id", ""),
                        })

    # Check each track against Jellyfin in parallel
    async def check_track(track: dict) -> dict:
        jf_id = await jf.search_library(track["title"], artist_name)
        return {**track, "jellyfin_id": jf_id or "", "in_library": bool(jf_id)}

    if tracks:
        tracks = list(await asyncio.gather(*[check_track(t) for t in tracks]))

    # Check if the whole album is in Jellyfin
    album_jellyfin_id = await jf.search_album(album_title, artist_name)
    album_in_library = bool(album_jellyfin_id)

    return templates.TemplateResponse(
        request=request,
        name="album.html",
        context={
            "session": request.session,
            "mbid": mbid,
            "album_title": album_title,
            "album_year": album_year,
            "artist_name": artist_name,
            "artist_mbid": artist_mbid,
            "cover_url": cover_url,
            "tracks": tracks,
            "album_in_library": album_in_library,
            "album_jellyfin_id": album_jellyfin_id or "",
            "jellyfin_url": Config.JELLYFIN_URL,
        }
    )


@router.get("/api/jellyfin-stream/{item_id}")
async def jellyfin_stream_url(item_id: str, request: Request):
    """Return a direct Jellyfin audio streaming URL for an item."""
    _require_login(request)
    jf = JellyfinClient()
    # Jellyfin universal audio stream endpoint
    stream_url = f"{Config.JELLYFIN_URL}/Audio/{item_id}/universal?api_key={Config.JELLYFIN_API_KEY}&audioCodec=aac&maxStreamingBitrate=320000"
    return JSONResponse({"url": stream_url})



@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_session),
):
    jf = JellyfinClient()
    auth_data = await jf.authenticate_user(username, password)
    if not auth_data:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"session": None, "error": "Invalid credentials or Jellyfin server unreachable."},
            status_code=401,
        )

    # Upsert user record
    user = db.exec(select(User).where(User.jellyfin_user_id == auth_data["user_id"])).first()
    if not user:
        user = User(
            jellyfin_user_id=auth_data["user_id"],
            jellyfin_username=auth_data["username"],
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        # Refresh username in case it was renamed in Jellyfin
        if user.jellyfin_username != auth_data["username"]:
            user.jellyfin_username = auth_data["username"]
            db.add(user)
            db.commit()

    request.session.update({
        "user_id": user.id,
        "jellyfin_user_id": user.jellyfin_user_id,
        "username": user.jellyfin_username,
        "is_admin": auth_data["is_admin"],
    })
    logger.info(f"User '{user.jellyfin_username}' logged in (admin={auth_data['is_admin']})")
    return _redirect("/music/")


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return _redirect("/music/")


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/dashboard")
async def dashboard(request: Request, db: Session = Depends(get_session)):
    uid = request.session.get("user_id")
    if not uid:
        return _redirect("/music/")

    user = db.get(User, uid)
    if not user:
        request.session.clear()
        return _redirect("/music/")

    downloads = db.exec(
        select(DownloadQueue)
        .where(DownloadQueue.requested_by == user.id)
        .order_by(DownloadQueue.created_at.desc())
        .limit(20)
    ).all()

    # Stats for the header
    pending_count = sum(1 for d in downloads if d.status == "pending")
    done_count = sum(1 for d in downloads if d.status == "done")
    failed_count = sum(1 for d in downloads if d.status == "failed")

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "session": request.session,
            "user": user,
            "downloads": downloads,
            "pending_count": pending_count,
            "done_count": done_count,
            "failed_count": failed_count,
            "msg": request.query_params.get("msg"),
            "err": request.query_params.get("err"),
        },
    )


# ── ListenBrainz link ─────────────────────────────────────────────────────────

@router.post("/link-listenbrainz")
async def link_listenbrainz(
    request: Request,
    lb_username: str = Form(...),
    lb_token: str = Form(...),
    db: Session = Depends(get_session),
):
    uid = request.session.get("user_id")
    if not uid:
        return _redirect("/music/")

    user = db.get(User, uid)
    lb = ListenBrainzClient()
    validated_user = await lb.validate_token(lb_token)

    if not validated_user or validated_user.lower() != lb_username.strip().lower():
        return _redirect("/music/dashboard", err="Invalid+ListenBrainz+token+or+username")

    user.listenbrainz_token = lb_token
    user.listenbrainz_user = validated_user
    user.linked_at = datetime.datetime.now(datetime.timezone.utc)
    db.add(user)
    db.commit()
    logger.info(f"User '{user.jellyfin_username}' linked ListenBrainz account: {validated_user}")
    return _redirect("/music/dashboard", msg="ListenBrainz+account+linked+successfully!")


# ── Force Sync ────────────────────────────────────────────────────────────────

@router.post("/force-sync")
async def force_sync(
    request: Request,
    db: Session = Depends(get_session)
):
    uid = request.session.get("user_id")
    if not uid:
        return _redirect("/music/")

    user = db.get(User, uid)
    ps = PlaylistService()
    try:
        await ps.sync_user_playlists(user)
        await ps.sync_weekly_exploration(user)
        # Signal the background worker to start downloading immediately
        wake_queue_worker()
        return _redirect("/music/dashboard", msg="Playlists+regenerated+successfully!")
    except Exception as exc:
        logger.error(f"force_sync failed for {user.jellyfin_username}: {exc}", exc_info=True)
        return _redirect("/music/dashboard", err="Sync+failed.+Check+server+logs.")


# ── Music Request ─────────────────────────────────────────────────────────────

@router.post("/request-music")
async def request_music(
    request: Request,
    request_type: str = Form(...),        # 'track' | 'album'
    artist_name: str = Form(...),
    track_name: str = Form(""),           # required for track requests
    album_name: str = Form(""),           # required for album requests
    notes: str = Form(""),
    deezer_url: str = Form(""),           # pre-resolved URL from the preview
    db: Session = Depends(get_session),
):
    uid = request.session.get("user_id")
    if not uid:
        return _redirect("/music/")

    user = db.get(User, uid)
    artist_name = artist_name.strip()
    track_name = track_name.strip()
    album_name = album_name.strip()
    notes = notes.strip() or None
    deezer_url = deezer_url.strip() or None

    if request_type == "album":
        if not album_name or not artist_name:
            return _redirect("/music/dashboard", err="Album+name+and+artist+are+required.")
        queued = queue_album_download(
            album_name=album_name,
            artist=artist_name,
            user_id=uid,
            notes=notes,
            deezer_url=deezer_url,
        )
        label = f"{artist_name} — {album_name} (album)"
    else:
        if not track_name or not artist_name:
            return _redirect("/music/dashboard", err="Track+name+and+artist+are+required.")
        queued = queue_download(
            track_name=track_name,
            artist=artist_name,
            album=album_name,
            user_id=uid,
            request_type="track",
            notes=notes,
            deezer_url=deezer_url,
        )
        label = f"{artist_name} — {track_name}"

    if queued:
        logger.info(f"[{user.jellyfin_username}] Requested download: {label}")
        return _redirect("/music/dashboard", msg=f"Download+queued:+{urllib.parse.quote(label)}")
    else:
        return _redirect("/music/dashboard", err="Already+in+queue+or+currently+downloading.")


# ── Deezer preview search (JSON API for live UI) ──────────────────────────────

@router.get("/api/search-deezer")
async def api_search_deezer(q: str = "", type: str = "track"):
    """
    Lightweight JSON endpoint consumed by the request-music form.
    Returns up to 5 Deezer results for the given query.
    """
    if not q or len(q.strip()) < 2:
        return JSONResponse({"results": []})
    results = preview_deezer_search(q.strip(), search_type=type)
    return JSONResponse({"results": results})

