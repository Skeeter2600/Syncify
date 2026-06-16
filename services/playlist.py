import asyncio
import json
import logging
from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

from sqlmodel import Session, select

from db import ListenSync, PlaylistJob, DownloadQueue, User, engine
from services.downloader import queue_download, wake_queue_worker
from services.jellyfin import JellyfinClient
from services.listenbrainz import ListenBrainzClient
from services.themes import assign_themes, theme_to_genre_filter

logger = logging.getLogger(__name__)

# Emoji prefixes used by this service — used for cleanup (including radio 📻)
_MANAGED_PREFIXES = (
    "\U0001f501", "\U0001f30d", "\U0001f3b8", "\U0001f3a4", "\U0001f33f", "\U0001f920", "\U0001f3b9",
    "\U0001f3b7", "\U0001f57a", "\u26a1", "\u2728", "\U0001f3bb", "\U0001f3a7", "\U0001f4fc", "\U0001f579\ufe0f", "\U0001fa69",
    "\U0001f4fb",
)


class LibraryResolver:
    def __init__(self, items: List[Dict]):
        # Group items by artist name (lowercase) to narrow down the search space
        self.by_artist = {}
        for item in items:
            artists = item.get("Artists") or []
            album_artist = item.get("AlbumArtist") or ""
            artist_names = [a.lower() for a in artists]
            if album_artist:
                artist_names.append(album_artist.lower())
            
            # De-duplicate artist names
            artist_names = list(set(artist_names))
            
            for artist in artist_names:
                if artist not in self.by_artist:
                    self.by_artist[artist] = []
                self.by_artist[artist].append(item)

    def resolve(self, track_name: str, artist_name: str) -> Optional[str]:
        artist_lower = artist_name.lower()
        track_lower = track_name.lower()
        
        # Find all artists that match (fuzzy check)
        matching_artists = []
        for lib_artist in self.by_artist.keys():
            if artist_lower in lib_artist or lib_artist in artist_lower:
                matching_artists.append(lib_artist)
                
        # Search for the track under matching artists
        for artist in matching_artists:
            for item in self.by_artist[artist]:
                item_name = item.get("Name", "").lower()
                # Fuzzy match: either equal or one contains the other
                if track_lower == item_name or track_lower in item_name or item_name in track_lower:
                    return item.get("Id")
        return None


# ─── Smart curation ───────────────────────────────────────────────────────────

def curate_smart_mix(
    library_items: List[Dict],
    genre_filter: Optional[List[str]] = None,
    artist_filter: Optional[str] = None,
    count: int = 30,
) -> List[str]:
    """
    Build a locally-curated playlist from the Jellyfin library using a
    25 / 25 / 50  split:
      - 25 % Heavy-rotation  (PlayCount >= 5 — favourites)
      - 25 % Low-rotation    (PlayCount 1-4)
      - 50 % Unplayed        (PlayCount == 0)

    Optionally filter by a list of genre substrings (OR logic) or by a
    single artist substring.  Returns a list of Jellyfin item IDs.
    """
    import random

    heavy: List[Dict] = []
    low: List[Dict] = []
    unplayed: List[Dict] = []

    artist_lower = artist_filter.lower() if artist_filter else None

    for item in library_items:
        item_id = item.get("Id")
        if not item_id:
            continue

        # ── Artist filter ─────────────────────────────────────────────────────
        if artist_lower:
            item_artists = [
                a.lower() for a in (item.get("Artists") or [])
            ]
            album_artist = (item.get("AlbumArtist") or "").lower()
            all_artists = item_artists + ([album_artist] if album_artist else [])
            if not any(
                artist_lower in a or a in artist_lower for a in all_artists
            ):
                continue

        # ── Genre filter ──────────────────────────────────────────────────────
        if genre_filter:
            item_genres = [g.lower() for g in (item.get("Genres") or [])]
            if not any(
                fg in ig or ig in fg
                for fg in genre_filter
                for ig in item_genres
            ):
                continue

        # ── Bucket by play count ──────────────────────────────────────────────
        play_count = (item.get("UserData") or {}).get("PlayCount", 0) or 0
        if play_count >= 5:
            heavy.append(item_id)
        elif play_count >= 1:
            low.append(item_id)
        else:
            unplayed.append(item_id)

    # Calculate per-bucket targets (25 / 25 / 50)
    n_heavy   = max(1, round(count * 0.25))
    n_low     = max(1, round(count * 0.25))
    n_unplayed = count - n_heavy - n_low

    random.shuffle(heavy)
    random.shuffle(low)
    random.shuffle(unplayed)

    picked = (
        heavy[:n_heavy]
        + low[:n_low]
        + unplayed[:n_unplayed]
    )
    # If any bucket came up short, fill from the others
    if len(picked) < count:
        remainder = heavy[n_heavy:] + low[n_low:] + unplayed[n_unplayed:]
        random.shuffle(remainder)
        picked += remainder[: count - len(picked)]

    random.shuffle(picked)  # final shuffle so buckets aren't obviously grouped
    return picked


class PlaylistService:
    def __init__(self) -> None:
        self.jf = JellyfinClient()
        self.lb = ListenBrainzClient()

    # ─── Helpers ─────────────────────────────────────────────────────────────

    async def delete_managed_playlists(self, jellyfin_user_id: str) -> None:
        """Delete all playlists whose name starts with a managed emoji prefix."""
        playlists = await self.jf.get_user_playlists(jellyfin_user_id)
        coros = [
            self.jf.delete_playlist(pl["Id"])
            for pl in playlists
            if pl.get("Name", "").startswith(_MANAGED_PREFIXES)
        ]
        if coros:
            await asyncio.gather(*coros, return_exceptions=True)

    async def _get_genre_counts(
        self, jellyfin_user_id: str, days: int
    ) -> Dict[str, int]:
        history = await self.jf.get_play_history(jellyfin_user_id, days=days)
        counts: Counter = Counter()
        for track in history:
            for genre in track.get("genres", []):
                counts[genre] += track.get("play_count", 1)
        return dict(counts)

    # ─── Playlist builders ───────────────────────────────────────────────────

    async def get_on_repeat_ids(self, jellyfin_user_id: str) -> List[str]:
        """Top-30 most-played track IDs from the last 7 days."""
        history = await self.jf.get_play_history(jellyfin_user_id, days=7)
        if not history:
            return []
        counts: Counter = Counter()
        for item in history:
            counts[item["id"]] += item.get("play_count", 1)
        return [tid for tid, _ in counts.most_common(30)]

    async def pick_daily_themes(
        self, jellyfin_user_id: str
    ) -> List[Tuple[str, str]]:
        """Select 5 daily themes from the user's 14-day genre history."""
        genre_counts = await self._get_genre_counts(jellyfin_user_id, days=14)
        return assign_themes(genre_counts)

    async def build_themed_ids(
        self,
        jellyfin_user_id: str,
        theme: str,
        history: Optional[List[Dict]] = None,
        count: int = 30,
    ) -> List[str]:
        """Filter user history to tracks matching theme genres."""
        genres = theme_to_genre_filter(theme)
        if history is None:
            history = await self.jf.get_play_history(jellyfin_user_id, days=30)

        seen: set = set()
        track_ids: List[str] = []
        for track in history:
            tid = track.get("id")
            if not tid or tid in seen:
                continue
            track_genres = track.get("genres", [])
            if any(fg in tg for fg in genres for tg in track_genres):
                seen.add(tid)
                track_ids.append(tid)
                if len(track_ids) >= count:
                    break
        return track_ids

    # ─── Main sync entry points ───────────────────────────────────────────────

    async def sync_user_playlists(self, user: User) -> None:
        """
        Full daily sync for one user.
        - Submits listens to ListenBrainz (skipping short scrobbles).
        - Creates On Repeat immediately.
        - Generates 5 themed playlists and 3 artist radio playlists.
        """
        jf_id = user.jellyfin_user_id

        # 1. Submit daily listens to ListenBrainz
        if user.listenbrainz_token:
            await self._submit_listens(user)

        # 2. Clean yesterday's managed playlists
        await self.delete_managed_playlists(jf_id)

        # 3. On Repeat — uses only existing library tracks, no downloads needed
        on_repeat_ids = await self.get_on_repeat_ids(jf_id)
        if on_repeat_ids:
            await self.jf.create_playlist(jf_id, "\U0001f501 On Repeat", on_repeat_ids)
            logger.info(f"[{user.jellyfin_username}] Created 'On Repeat' ({len(on_repeat_ids)} tracks)")

        # Fetch full library once — used for both themed playlists AND artist radio
        logger.info(f"[{user.jellyfin_username}] Fetching library audio for smart curation...")
        library_items = await self.jf.get_all_library_audio(jf_id)
        if not library_items:
            logger.warning(f"[{user.jellyfin_username}] Library empty — skipping playlist generation.")
            return

        # ─── A. Daily Themed Playlists (5 themes, 100% local) ─────────────────
        themes = await self.pick_daily_themes(jf_id)
        for theme_name, emoji in themes:
            genre_filter = theme_to_genre_filter(theme_name)
            track_ids = curate_smart_mix(
                library_items,
                genre_filter=genre_filter,
                count=30,
            )
            playlist_name = f"{emoji} {theme_name}"
            if track_ids:
                await self.jf.create_playlist(jf_id, playlist_name, track_ids)
                logger.info(
                    f"[{user.jellyfin_username}] '{playlist_name}' created "
                    f"({len(track_ids)} tracks, all local)"
                )
            else:
                logger.warning(
                    f"[{user.jellyfin_username}] No local tracks found for theme '{theme_name}' — skipped."
                )

        # ─── B. Top 3 Artist Radio Playlists (100% local) ─────────────────────
        logger.info(f"[{user.jellyfin_username}] Calculating top 3 artists from past week...")
        history_7d = await self.jf.get_play_history(jf_id, days=7)
        artist_counts = Counter(item["artist"] for item in history_7d if item.get("artist"))
        top_artists = [artist for artist, _ in artist_counts.most_common(3)]
        logger.info(f"[{user.jellyfin_username}] Top artists identified: {top_artists}")

        for artist in top_artists:
            playlist_name = f"\U0001f4fb {artist} Radio"
            track_ids = curate_smart_mix(
                library_items,
                artist_filter=artist,
                count=30,
            )
            if track_ids:
                await self.jf.create_playlist(jf_id, playlist_name, track_ids)
                logger.info(
                    f"[{user.jellyfin_username}] Artist radio '{playlist_name}' created "
                    f"({len(track_ids)} tracks, all local)"
                )
            else:
                logger.warning(
                    f"[{user.jellyfin_username}] No local tracks found for artist '{artist}' — skipped."
                )

    async def sync_weekly_exploration(self, user: User) -> None:
        """Fetch ListenBrainz Weekly Exploration, queue missing tracks, create PlaylistJob."""
        if not user.listenbrainz_token or not user.listenbrainz_user:
            return

        jf_id = user.jellyfin_user_id
        lb_tracks = await self.lb.get_weekly_exploration(user.listenbrainz_user)
        if not lb_tracks:
            logger.info(f"[{user.jellyfin_username}] No Weekly Exploration tracks found.")
            return

        playlist_name = "\U0001f30d Weekly Exploration"

        # Create a PlaylistJob for weekly exploration
        with Session(engine) as session:
            job = PlaylistJob(
                user_id=user.id,
                jellyfin_user_id=jf_id,
                playlist_name=playlist_name,
                lb_tracks_json=json.dumps(lb_tracks),
                status="waiting",
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            job_id = job.id

        # Use LibraryResolver to match in memory
        library_items = await self.jf.get_all_library_audio(jf_id)
        resolver = LibraryResolver(library_items)
        results = [resolver.resolve(t["track_name"], t["artist_name"]) for t in lb_tracks]

        needs_download = False
        for track, track_id in zip(lb_tracks, results):
            if not track_id:
                queued = queue_download(
                    track_name=track["track_name"],
                    artist=track["artist_name"],
                    album=track["album_name"],
                    user_id=user.id,
                    playlist_job_id=job_id,
                )
                if queued:
                    needs_download = True

        if not needs_download:
            local_ids = [tid for tid in results if tid]
            if local_ids:
                await self.jf.create_playlist(jf_id, playlist_name, local_ids)
                logger.info(
                    f"[{user.jellyfin_username}] Weekly Exploration created immediately ({len(local_ids)} tracks)"
                )
            with Session(engine) as session:
                j = session.get(PlaylistJob, job_id)
                if j:
                    j.status = "created" if local_ids else "skipped"
                    j.completed_at = datetime.now(timezone.utc)
                    session.add(j)
                    session.commit()
        else:
            wake_queue_worker()
            logger.info(
                f"[{user.jellyfin_username}] Weekly Exploration job #{job_id} waiting on downloads."
            )

    # ─── ListenBrainz submission ──────────────────────────────────────────────

    async def _submit_listens(self, user: User) -> None:
        """
        Retrieves recent playback activity logs, computes actual listening duration,
        and skips submitting tracks that were listened to for less than 30s
        (or less than 50% of the track, whichever is shorter).
        """
        raw_entries = await self.jf.get_activity_log(limit=2000)
        if not raw_entries:
            return

        with Session(engine) as session:
            last_sync = session.exec(
                select(ListenSync)
                .where(ListenSync.user_id == user.id)
                .order_by(ListenSync.last_synced_at.desc())
            ).first()

        cutoff = last_sync.last_synced_at if last_sync else (datetime.now(timezone.utc) - timedelta(days=1))
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)

        # Filter entries for this user and audio playback types
        user_entries = []
        for entry in raw_entries:
            if (
                entry.get("UserId") == user.jellyfin_user_id
                and entry.get("Type") in ("AudioPlayback", "AudioPlaybackStopped")
            ):
                user_entries.append(entry)

        # Sort chronologically (oldest first) so we can pair start and stop events
        user_entries.sort(key=lambda x: x.get("Date", ""))

        active_starts = {}  # item_id -> start_time
        paired_plays = []   # (item_id, start_time, duration_seconds)

        for entry in user_entries:
            item_id = entry.get("ItemId")
            entry_type = entry.get("Type")
            entry_date_str = entry.get("Date")
            if not item_id or not entry_type or not entry_date_str:
                continue

            try:
                entry_date = datetime.fromisoformat(entry_date_str.replace("Z", "+00:00"))
            except Exception:
                continue

            if entry_type == "AudioPlayback":
                active_starts[item_id] = entry_date
            elif entry_type == "AudioPlaybackStopped":
                if item_id in active_starts:
                    start_time = active_starts.pop(item_id)
                    # We only submit plays that completed (stopped) after the last sync cutoff
                    if entry_date > cutoff:
                        duration_sec = (entry_date - start_time).total_seconds()
                        paired_plays.append((item_id, start_time, duration_sec))

        if not paired_plays:
            logger.info(f"[{user.jellyfin_username}] No new playback stop events since last sync.")
            return

        # Fetch metadata in bulk for all played items to get names and actual track lengths
        unique_item_ids = list({p[0] for p in paired_plays})
        items_metadata = await self.jf.get_items_metadata(user.jellyfin_user_id, unique_item_ids)
        metadata_map = {}
        for item in items_metadata:
            metadata_map[item["Id"]] = {
                "artist": (item.get("Artists") or [""])[0] or item.get("AlbumArtist", ""),
                "name": item.get("Name", ""),
                "album": item.get("Album", ""),
                "duration_seconds": (item.get("RunTimeTicks", 0) or 0) / 10000000.0,
            }

        # Filter plays using the ListenBrainz rules
        payload = []
        for item_id, start_time, duration_sec in paired_plays:
            meta = metadata_map.get(item_id)
            if not meta or not meta["artist"] or not meta["name"]:
                continue

            track_dur = meta["duration_seconds"]
            # Standard ListenBrainz guidelines: 30s or 50% of track length (whichever is shorter)
            required_sec = min(30.0, track_dur * 0.5) if track_dur > 0 else 30.0

            if duration_sec >= required_sec:
                payload.append({
                    "listened_at": int(start_time.timestamp()),
                    "track_metadata": {
                        "artist_name": meta["artist"],
                        "track_name": meta["name"],
                        "release_name": meta["album"],
                    }
                })
            else:
                logger.info(
                    f"[{user.jellyfin_username}] Skipping short play: {meta['artist']} - {meta['name']} "
                    f"({duration_sec:.1f}s played, required {required_sec:.1f}s)"
                )

        if payload:
            ok = await self.lb.submit_listens(user.listenbrainz_token, payload)
            if ok:
                with Session(engine) as session:
                    session.add(
                        ListenSync(user_id=user.id, tracks_submitted=len(payload))
                    )
                    session.commit()
                logger.info(f"[{user.jellyfin_username}] Submitted {len(payload)} listens to ListenBrainz")
            else:
                logger.warning(f"[{user.jellyfin_username}] ListenBrainz listen submission failed")
        else:
            logger.info(f"[{user.jellyfin_username}] No new eligible listens to submit (all tracks skipped or short)")


async def check_ready_playlist_jobs() -> None:
    """
    Called by the scheduler every 5 minutes.
    Finds any PlaylistJob in 'waiting' state where ALL linked DownloadQueue
    items are done or failed (i.e. nothing is still pending/downloading).
    For each ready job, resolves tracks against Jellyfin and creates the playlist.
    """
    jf = JellyfinClient()

    with Session(engine) as session:
        waiting_jobs = session.exec(
            select(PlaylistJob).where(PlaylistJob.status == "waiting")
        ).all()

    if not waiting_jobs:
        return

    logger.info(f"Checking {len(waiting_jobs)} waiting playlist job(s)...")

    for job in waiting_jobs:
        with Session(engine) as session:
            # Are there any downloads for this job that are still in progress?
            active = session.exec(
                select(DownloadQueue).where(
                    DownloadQueue.playlist_job_id == job.id,
                    DownloadQueue.status.in_(["pending", "downloading"]),
                )
            ).first()

            if active:
                logger.debug(f"Job #{job.id} '{job.playlist_name}': still waiting on downloads.")
                continue

            # All downloads are done/failed (or there were none queued) — create the playlist
            logger.info(f"Job #{job.id} '{job.playlist_name}': all downloads complete, creating playlist...")
            lb_tracks: List[Dict] = json.loads(job.lb_tracks_json)
            # Trigger Jellyfin library scan and wait for indexing
            await jf.refresh_library()
            await asyncio.sleep(45)  # wait 45 seconds for Jellyfin to index new files
            # Bulk match in memory to optimize Jellyfin server load
            library_items = await jf.get_all_library_audio(job.jellyfin_user_id)

            # Bulk match in memory to optimize Jellyfin server load
            library_items = await jf.get_all_library_audio(job.jellyfin_user_id)
            resolver = LibraryResolver(library_items)
            results = [resolver.resolve(t["track_name"], t["artist_name"]) for t in lb_tracks]
            local_ids = [tid for tid in results if tid]

            # Delete any existing version of this playlist first (yesterday's copy)
            existing_playlists = await jf.get_user_playlists(job.jellyfin_user_id)
            delete_coros = [
                jf.delete_playlist(pl["Id"])
                for pl in existing_playlists
                if pl.get("Name") == job.playlist_name
            ]
            if delete_coros:
                await asyncio.gather(*delete_coros, return_exceptions=True)

            new_status = "skipped"
            if local_ids:
                await jf.create_playlist(job.jellyfin_user_id, job.playlist_name, local_ids)
                logger.info(
                    f"Job #{job.id} '{job.playlist_name}': created with {len(local_ids)} tracks."
                )
                new_status = "created"
            else:
                logger.warning(
                    f"Job #{job.id} '{job.playlist_name}': no tracks resolved, skipping."
                )

            # Mark job as complete
            with Session(engine) as update_session:
                j = update_session.get(PlaylistJob, job.id)
                if j:
                    j.status = new_status
                    j.completed_at = datetime.now(timezone.utc)
                    update_session.add(j)
                    update_session.commit()
