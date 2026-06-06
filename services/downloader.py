import logging
import threading
import urllib.parse
import urllib.request
import json
import subprocess
import time
import sys
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from sqlmodel import Session, select
from db import engine, DownloadQueue
from config import Config

logger = logging.getLogger(__name__)

# ── Queue lock — prevents two threads running process_queue() simultaneously ──
_queue_lock = threading.Lock()

# ── Background worker event — set this to wake the persistent download thread ──
_work_event = threading.Event()

# Handle resource package for file descriptor limits (Unix only)
try:
    import resource as _resource
except ImportError:
    _resource = None  # type: ignore

# ─── ARL failure signals from streamrip stderr ────────────────────────────────
_ARL_FAIL_PATTERNS = ("arl", "login", "cookie", "unauthorized", "403", "expired", "none, b''")


# ─── Deezer search ────────────────────────────────────────────────────────────

def _deezer_get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; Syncify/1.0)"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def search_deezer_track(track_name: str, artist_name: str) -> str | None:
    query = urllib.parse.quote(f"{artist_name} {track_name}")
    try:
        data = _deezer_get(f"https://api.deezer.com/search/track?q={query}&limit=1")
        if data.get("data"):
            return data["data"][0]["link"]
    except Exception as exc:
        logger.warning(f"Deezer track search failed ({artist_name} – {track_name}): {exc}")
    return None


def search_deezer_album(album_name: str, artist_name: str) -> tuple[str | None, str | None]:
    query = urllib.parse.quote(f"{artist_name} {album_name}")
    try:
        data = _deezer_get(f"https://api.deezer.com/search/album?q={query}&limit=1")
        if data.get("data"):
            item = data["data"][0]
            return item["link"], str(item["id"])
    except Exception as exc:
        logger.warning(f"Deezer album search failed ({artist_name} – {album_name}): {exc}")
    return None, None


def preview_deezer_search(query: str, search_type: str = "track") -> list[dict]:
    """
    Lightweight Deezer search for the UI preview panel.
    Returns up to 5 results with title, artist, album, cover, and link.
    search_type: 'track' | 'album'
    """
    encoded = urllib.parse.quote(query)
    results = []
    try:
        if search_type == "album":
            data = _deezer_get(f"https://api.deezer.com/search/album?q={encoded}&limit=5")
            for item in data.get("data", []):
                results.append({
                    "type": "album",
                    "title": item.get("title", ""),
                    "artist": item.get("artist", {}).get("name", ""),
                    "cover": item.get("cover_medium") or item.get("cover", ""),
                    "link": item.get("link", ""),
                    "nb_tracks": item.get("nb_tracks", "?"),
                })
        else:
            data = _deezer_get(f"https://api.deezer.com/search/track?q={encoded}&limit=5")
            for item in data.get("data", []):
                results.append({
                    "type": "track",
                    "title": item.get("title", ""),
                    "artist": item.get("artist", {}).get("name", ""),
                    "album": item.get("album", {}).get("title", ""),
                    "cover": item.get("album", {}).get("cover_medium", ""),
                    "link": item.get("link", ""),
                    "duration": item.get("duration", 0),
                })
    except Exception as exc:
        logger.warning(f"Deezer preview search failed: {exc}")
    return results


# ─── streamrip config injection ───────────────────────────────────────────────

def get_streamrip_config_path() -> Path:
    """Return the centralized path to our managed streamrip config.toml."""
    return Config.DATA_DIR / "streamrip_config.toml"


def inject_arl(arl: str) -> None:
    """
    Write a complete streamrip config.toml by starting from the bundled default
    and overlaying our settings (ARL, download folder, filepaths).
    This ensures all required sections (e.g. [misc]) are always present.
    """
    config_path = get_streamrip_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Load the bundled default config shipped with streamrip ────────────────
    bundled_config_path = Path("/usr/local/lib/python3.11/site-packages/streamrip/config.toml")
    config_data: dict = {}
    if bundled_config_path.exists():
        try:
            try:
                import tomllib
                config_data = tomllib.loads(bundled_config_path.read_text(encoding="utf-8"))
            except ImportError:
                import tomli
                config_data = tomli.loads(bundled_config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"Could not read bundled streamrip config: {exc}")
    else:
        # Fallback: try to read from existing managed config
        if config_path.exists():
            try:
                try:
                    import tomllib
                    config_data = tomllib.loads(config_path.read_text(encoding="utf-8"))
                except ImportError:
                    import tomli
                    config_data = tomli.loads(config_path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning(f"Could not read existing streamrip config: {exc}")

    # Ensure required sections exist
    for section in ("downloads", "deezer", "conversion", "filepaths", "misc", "cli",
                    "qobuz", "tidal", "soundcloud", "youtube", "database",
                    "artwork", "metadata", "lastfm", "qobuz_filters"):
        config_data.setdefault(section, {})

    # ── Overlay our settings ──────────────────────────────────────────────────
    config_data["downloads"].update({
        "folder": Config.MUSIC_LIBRARY_PATH,
        "max_connections": 3,
        "requests_per_minute": 30,
        "concurrency": True,
    })
    config_data["deezer"].update({"quality": 1, "arl": arl})
    config_data["conversion"]["enabled"] = False   # Avoid FFmpeg OGG cover-art issues
    config_data["filepaths"].update({
        "add_singles_to_folder": True,
        "folder_format": "{albumartist}/{title}",
        "track_format": "{tracknumber:02} - {title}",
        "restrict_characters": False,
        "truncate_to": 120,
    })
    # streamrip requires non-empty paths for its internal download-tracking DB
    db_dir = config_path.parent
    config_data["database"].update({
        "downloads_enabled": True,
        "downloads_path": str(db_dir / "streamrip_downloads.db"),
        "failed_downloads_enabled": True,
        "failed_downloads_path": str(db_dir / "streamrip_failed.db"),
    })
    # Ensure the required version key in [misc] is present
    config_data["misc"].setdefault("version", "2.0.6")
    config_data["misc"].setdefault("check_for_updates", False)

    # ── Serialize back to TOML ────────────────────────────────────────────────
    def format_val(val):
        if isinstance(val, bool):
            return "true" if val else "false"
        elif isinstance(val, (int, float)):
            return str(val)
        elif isinstance(val, list):
            items = ", ".join(format_val(v) for v in val)
            return f"[{items}]"
        elif isinstance(val, str):
            escaped = val.replace('\\', '\\\\').replace('"', '\\"')
            return f'"{escaped}"'
        return f'"{val}"'

    lines = []
    # Write top-level scalar fields first
    for k, v in config_data.items():
        if not isinstance(v, dict):
            lines.append(f"{k} = {format_val(v)}")

    # Write each section table
    for section_name, section_dict in config_data.items():
        if isinstance(section_dict, dict):
            if lines:
                lines.append("")
            lines.append(f"[{section_name}]")
            for k, v in section_dict.items():
                lines.append(f"{k} = {format_val(v)}")

    toml_str = "\n".join(lines) + "\n"
    config_path.write_text(toml_str, encoding="utf-8")
    logger.info(f"streamrip config.toml written to {config_path} (folder={Config.MUSIC_LIBRARY_PATH!r})")


# ─── streamrip runner ─────────────────────────────────────────────────────────

def get_rip_executable() -> str:
    # 1. Check same directory as python executable (virtualenv)
    py_dir = Path(sys.executable).parent
    exe_name = "rip.exe" if sys.platform == "win32" else "rip"
    candidate = py_dir / exe_name
    if candidate.exists():
        return str(candidate)

    # 2. Check project root .venv folder
    project_root = Path(__file__).resolve().parent.parent
    venv_dir = project_root / ".venv"
    if venv_dir.exists():
        bin_dir = venv_dir / "Scripts" if sys.platform == "win32" else venv_dir / "bin"
        candidate = bin_dir / exe_name
        if candidate.exists():
            return str(candidate)

    return "rip"


def _raise_fd_limit() -> None:
    if _resource:
        try:
            _resource.setrlimit(_resource.RLIMIT_NOFILE, (8192, 8192))
        except Exception:
            pass


def run_rip(url: str) -> tuple[bool, str]:
    """Download a single Deezer URL via streamrip. Returns (success, combined_output)."""
    _raise_fd_limit()
    rip_cmd = get_rip_executable()
    config_path = get_streamrip_config_path()
    logger.info(f"Spawning streamrip: {rip_cmd} --config-path {config_path} url {url}")
    try:
        # On Windows with shell=True, pass a string; otherwise pass a list.
        if sys.platform == "win32":
            cmd = f'"{rip_cmd}" --config-path "{config_path}" url "{url}"'
        else:
            cmd = [rip_cmd, "--config-path", str(config_path), "url", url]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            shell=(sys.platform == "win32"),
            timeout=300,  # 5-minute max per track
        )
        output = (result.stdout or "") + (result.stderr or "")
        logger.info(f"streamrip exit code {result.returncode} for {url}")
        if output.strip():
            # Log up to 500 chars of output for visibility
            logger.info(f"streamrip output: {output[:500]}")

        if result.returncode != 0 or "Download completed with 0" in output:
            _check_arl_failure(output)
            logger.warning(f"streamrip download failed (rc={result.returncode}) for {url}. Output tail: {output[-300:]}")
            return False, output
        logger.info(f"streamrip download succeeded for {url}")
        return True, output
    except subprocess.TimeoutExpired:
        logger.error(f"streamrip timed out after 300s for {url}")
        return False, "Timeout after 300s"
    except Exception as exc:
        logger.exception(f"Failed to spawn streamrip for {url}: {exc}")
        return False, f"Exception: {str(exc)}"


def _check_arl_failure(output: str) -> None:
    low = output.lower()
    if any(p in low for p in _ARL_FAIL_PATTERNS):
        logger.error("ARL token appears expired. Update it via the admin panel.")


def run_rip_file(urls_path: str) -> bool:
    """Download a file of Deezer URLs via streamrip `rip file`."""
    _raise_fd_limit()
    rip_cmd = get_rip_executable()
    config_path = get_streamrip_config_path()
    try:
        if sys.platform == "win32":
            cmd = f'"{rip_cmd}" --config-path "{config_path}" file "{urls_path}"'
        else:
            cmd = [rip_cmd, "--config-path", str(config_path), "file", urls_path]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            shell=(sys.platform == "win32"),
            timeout=600,
        )
        _check_arl_failure(result.stdout + result.stderr)
        return result.returncode == 0
    except Exception as exc:
        logger.exception(f"Failed to spawn streamrip command '{rip_cmd} --config-path {config_path} file {urls_path}': {exc}")
        return False


# ─── Queue management ─────────────────────────────────────────────────────────

def queue_download(
    track_name: str,
    artist: str,
    album: str,
    user_id: int,
    request_type: str = "track",
    notes: Optional[str] = None,
    deezer_url: Optional[str] = None,
    playlist_job_id: Optional[int] = None,
) -> bool:
    """
    Add a track or album to the download queue.
    Returns False if an identical pending/downloading entry already exists.
    """
    with Session(engine) as session:
        existing = session.exec(
            select(DownloadQueue).where(
                DownloadQueue.track_name == track_name,
                DownloadQueue.artist_name == artist,
                DownloadQueue.request_type == request_type,
                DownloadQueue.status.in_(["pending", "downloading"]),
            )
        ).first()
        if existing:
            # If this track is already queued but not linked to this job yet, link it
            if playlist_job_id and not existing.playlist_job_id:
                existing.playlist_job_id = playlist_job_id
                session.add(existing)
                session.commit()
            return False
        session.add(DownloadQueue(
            track_name=track_name,
            artist_name=artist,
            album_name=album,
            deezer_url=deezer_url,
            request_type=request_type,
            notes=notes,
            status="pending",
            requested_by=user_id,
            playlist_job_id=playlist_job_id,
        ))
        session.commit()
    return True


def queue_album_download(
    album_name: str,
    artist: str,
    user_id: int,
    notes: Optional[str] = None,
    deezer_url: Optional[str] = None,
) -> bool:
    """
    Convenience wrapper to queue an entire album for download.
    track_name is stored as "" to signal album-level download.
    """
    return queue_download(
        track_name="",
        artist=artist,
        album=album_name,
        user_id=user_id,
        request_type="album",
        notes=notes,
        deezer_url=deezer_url,
    )


def reset_stale_downloads() -> None:
    """Reset any 'downloading' items to 'pending' — handles crash/reload orphans."""
    with Session(engine) as session:
        stale = session.exec(
            select(DownloadQueue).where(DownloadQueue.status == "downloading")
        ).all()
        if stale:
            logger.warning(f"Resetting {len(stale)} stale 'downloading' item(s) to 'pending'.")
            for item in stale:
                item.status = "pending"
                session.add(item)
            session.commit()


def extract_deezer_id(url: str) -> Optional[str]:
    match = re.search(r"track/(\d+)", url)
    return match.group(1) if match else None


def process_queue() -> int:
    """
    Process a small chunk of pending queue entries.
    - Resolves Deezer URLs concurrently in a thread pool.
    - Batches track downloads into a single streamrip run (max 15 tracks).
    - Processes albums sequentially (at most 1 album per batch).
    Guarded by a lock so only one run can be active at a time.
    Returns the number of items attempted.
    """
    if not _queue_lock.acquire(blocking=False):
        logger.debug("process_queue already running — skipping.")
        return 0

    attempted = 0
    try:
        with Session(engine) as session:
            # Only process up to 15 tracks or 1 album in a single run to prevent CPU pegging
            tracks = session.exec(
                select(DownloadQueue)
                .where(DownloadQueue.status == "pending", DownloadQueue.request_type == "track")
                .limit(15)
            ).all()
            
            albums = []
            if not tracks:
                # Only process albums if no tracks are pending in this batch
                albums = session.exec(
                    select(DownloadQueue)
                    .where(DownloadQueue.status == "pending", DownloadQueue.request_type == "album")
                    .limit(1)
                ).all()

            if not tracks and not albums:
                return 0

            # ── 1. Process Tracks (Batch of max 15) ──────────────────────────
            if tracks:
                logger.info(f"Processing batch of {len(tracks)} pending tracks...")
                attempted += len(tracks)

                # Resolve URLs concurrently in a thread pool
                def resolve_track_url(item: DownloadQueue) -> tuple[int, Optional[str]]:
                    if item.deezer_url:
                        return item.id, item.deezer_url
                    url = search_deezer_track(item.track_name, item.artist_name)
                    # Small delay to respect rate limit
                    time.sleep(0.3)
                    return item.id, url

                # Run URL resolutions in parallel
                with ThreadPoolExecutor(max_workers=5) as executor:
                    results = list(executor.map(resolve_track_url, tracks))

                resolved_urls: dict[int, Optional[str]] = dict(results)

                # Update URLs in database and filter out those that didn't resolve
                to_download: list[DownloadQueue] = []
                for item in tracks:
                    url = resolved_urls.get(item.id)
                    if not url:
                        logger.warning(f"No Deezer URL found for track: {item.artist_name} - {item.track_name}")
                        item.status = "failed"
                        item.notes = "Could not resolve Deezer URL"
                        item.completed_at = datetime.now(timezone.utc)
                        session.add(item)
                    else:
                        item.deezer_url = url
                        item.status = "downloading"
                        session.add(item)
                        to_download.append(item)
                session.commit()

                if to_download:
                    # Write URLs to a temp file for streamrip
                    temp_urls_file = Config.DATA_DIR / "pending_tracks_urls.txt"
                    try:
                        temp_urls_file.write_text("\n".join(item.deezer_url for item in to_download if item.deezer_url), encoding="utf-8")
                        
                        logger.info(f"Starting batch download of {len(to_download)} tracks using rip file...")
                        success = run_rip_file(str(temp_urls_file))
                        logger.info(f"Batch download process finished (success={success}).")
                    finally:
                        if temp_urls_file.exists():
                            try:
                                temp_urls_file.unlink()
                            except Exception:
                                pass

                    # Read streamrip's SQLite databases to verify which items succeeded
                    downloaded_ids = set()
                    failed_ids = set()

                    downloads_db = Config.DATA_DIR / "streamrip_downloads.db"
                    failed_db = Config.DATA_DIR / "streamrip_failed.db"

                    if downloads_db.exists():
                        try:
                            with sqlite3.connect(downloads_db) as conn:
                                cur = conn.cursor()
                                cur.execute("SELECT id FROM downloads;")
                                downloaded_ids = {row[0] for row in cur.fetchall()}
                        except Exception as exc:
                            logger.error(f"Failed to read streamrip downloads DB: {exc}")

                    if failed_db.exists():
                        try:
                            with sqlite3.connect(failed_db) as conn:
                                cur = conn.cursor()
                                cur.execute("SELECT id FROM failed_downloads;")
                                failed_ids = {row[0] for row in cur.fetchall()}
                        except Exception as exc:
                            logger.error(f"Failed to read streamrip failed DB: {exc}")

                    # Update database statuses for each download queue item
                    for item in to_download:
                        track_id = extract_deezer_id(item.deezer_url) if item.deezer_url else None
                        
                        if track_id and track_id in downloaded_ids:
                            item.status = "done"
                            item.notes = "Success"
                        elif track_id and track_id in failed_ids:
                            item.status = "failed"
                            item.notes = "Streamrip reported failure"
                        else:
                            if success:
                                item.status = "done"
                                item.notes = "Success"
                            else:
                                item.status = "failed"
                                item.notes = "Failed during batch download run"
                        item.completed_at = datetime.now(timezone.utc)
                        session.add(item)
                    session.commit()

            # ── 2. Process Albums (Sequential) ───────────────────────────────
            for item in albums:
                attempted += 1
                item.status = "downloading"
                session.add(item)
                session.commit()

                logger.info(f"Starting download for [album]: {item.artist_name} - {item.album_name}")

                try:
                    url = item.deezer_url
                    if not url:
                        logger.info(f"Resolving Deezer URL for album {item.artist_name} - {item.album_name}...")
                        url, _ = search_deezer_album(item.album_name, item.artist_name)
                        time.sleep(0.3)

                    if not url:
                        logger.warning(f"No Deezer URL found for [album]: {item.artist_name} – {item.album_name}")
                        item.status = "failed"
                        item.notes = "Could not resolve Deezer URL"
                        item.completed_at = datetime.now(timezone.utc)
                        session.add(item)
                        session.commit()
                        continue

                    item.deezer_url = url
                    session.add(item)
                    session.commit()

                    logger.info(f"Spawning streamrip download for album {url}...")
                    success, output = run_rip(url)
                    
                    if success:
                        logger.info(f"Streamrip download succeeded for album {item.artist_name} - {item.album_name}")
                        item.status = "done"
                        item.notes = "Success"
                    else:
                        logger.warning(f"Streamrip download failed for album {item.artist_name} - {item.album_name}. Output: {output[:300]}")
                        item.status = "failed"
                        item.notes = f"Streamrip failed: {output[-150:]}"

                    item.completed_at = datetime.now(timezone.utc)
                    session.add(item)
                    session.commit()

                except Exception as exc:
                    logger.error(f"Error processing album queue item {item.id} ({item.artist_name} - {item.album_name}): {exc}", exc_info=True)
                    item.status = "failed"
                    item.notes = f"Exception: {str(exc)}"
                    item.completed_at = datetime.now(timezone.utc)
                    session.add(item)
                    session.commit()
    finally:
        _queue_lock.release()

    return attempted


def wake_queue_worker() -> None:
    """Signal the persistent background download thread to process the queue now."""
    _work_event.set()


def _background_download_worker() -> None:
    """
    Persistent daemon thread: sleeps until _work_event is set, then drains the
    queue in batches of 15 with a 5-second cooldown in between.
    """
    logger.info("Background download worker started.")
    while True:
        # Block until signalled, or wake every 60s as a safety net
        _work_event.wait(timeout=60.0)
        _work_event.clear()
        
        while True:
            try:
                attempted = process_queue()
                if not attempted:
                    # Queue is empty, go back to waiting
                    break
                logger.info(f"Background worker processed batch of {attempted} queue item(s).")
                # Sleep 5 seconds to cool down CPU/network between batches
                time.sleep(5.0)
            except Exception:
                logger.exception("Background download worker encountered an error.")
                time.sleep(10.0)
                break


def start_background_worker() -> None:
    """Launch the persistent download worker thread (call once at startup)."""
    t = threading.Thread(target=_background_download_worker, daemon=True, name="DownloadWorker")
    t.start()
    logger.info("Background download worker thread launched.")
