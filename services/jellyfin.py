import logging
import httpx
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from config import Config

logger = logging.getLogger(__name__)

# Shared async client – reuse across requests (connection pool)
_client: Optional[httpx.AsyncClient] = None

def _get_shared_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
    return _client


class JellyfinClient:
    def __init__(self):
        self.base_url = Config.JELLYFIN_URL
        self.api_key = Config.JELLYFIN_API_KEY

    def _auth_header(self, token: Optional[str] = None) -> Dict[str, str]:
        parts = (
            'MediaBrowser Client="Syncify", '
            'Device="Server", DeviceId="syncify-fastapi", '
            'Version="1.0.0"'
        )
        resolved_token = token or self.api_key
        if resolved_token:
            parts += f', Token="{resolved_token}"'
        return {
            "X-Emby-Authorization": parts,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ──────────────────────────── Auth ───────────────────────────────────────

    async def authenticate_user(
        self, username: str, password: str
    ) -> Optional[Dict[str, Any]]:
        """Authenticate a Jellyfin user. Returns auth info dict or None."""
        url = f"{self.base_url}/Users/AuthenticateByName"
        payload = {"Username": username, "Pw": password}
        client = _get_shared_client()
        try:
            resp = await client.post(url, json=payload, headers=self._auth_header())
            resp.raise_for_status()
            data = resp.json()
            user_obj = data.get("User", {})
            return {
                "token": data.get("AccessToken"),
                "user_id": user_obj.get("Id"),
                "username": user_obj.get("Name"),
                "is_admin": user_obj.get("Policy", {}).get("IsAdministrator", False),
            }
        except httpx.HTTPStatusError as exc:
            logger.warning(f"Jellyfin auth failed ({exc.response.status_code}): {username}")
        except Exception as exc:
            logger.error(f"Jellyfin auth error: {exc}")
        return None

    # ──────────────────────────── Users ──────────────────────────────────────

    async def get_all_users(self) -> List[Dict[str, Any]]:
        client = _get_shared_client()
        try:
            resp = await client.get(f"{self.base_url}/Users", headers=self._auth_header())
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error(f"get_all_users failed: {exc}")
        return []

    # ──────────────────────────── History ────────────────────────────────────

    async def get_play_history(
        self, user_id: str, days: int = 7
    ) -> List[Dict[str, Any]]:
        """Return audio items played within the last `days` days."""
        url = f"{self.base_url}/Users/{user_id}/Items"
        params = {
            "Recursive": "true",
            "IncludeItemTypes": "Audio",
            "Filters": "IsPlayed",
            "SortBy": "DatePlayed",
            "SortOrder": "Descending",
            "Fields": "UserData,Genres,Artists,AlbumArtist",
            "Limit": 500,
        }
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        client = _get_shared_client()
        try:
            resp = await client.get(url, headers=self._auth_header(), params=params)
            resp.raise_for_status()
        except Exception as exc:
            logger.error(f"get_play_history failed for {user_id}: {exc}")
            return []

        history: List[Dict[str, Any]] = []
        for item in resp.json().get("Items", []):
            user_data = item.get("UserData", {})
            last_played_str = user_data.get("LastPlayedDate")
            if not last_played_str:
                continue
            try:
                last_played = datetime.fromisoformat(last_played_str.replace("Z", "+00:00"))
                if last_played < cutoff:
                    # Items are sorted descending; once past cutoff we can stop
                    break
                history.append({
                    "id": item.get("Id"),
                    "name": item.get("Name", ""),
                    "artist": (item.get("Artists") or [""])[0] or item.get("AlbumArtist", ""),
                    "album": item.get("Album", ""),
                    "genres": [g.lower() for g in item.get("Genres", [])],
                    "last_played": last_played,
                    "play_count": user_data.get("PlayCount", 1),
                })
            except (ValueError, TypeError):
                continue
        return history

    async def get_recently_played(self, user_id: str) -> List[Dict[str, Any]]:
        return await self.get_play_history(user_id, days=1)

    # ──────────────────────────── Library Search ──────────────────────────────

    async def search_library(
        self, track_name: str, artist: str
    ) -> Optional[str]:
        """Return Jellyfin item ID if track exists, else None."""
        params = {
            "Recursive": "true",
            "IncludeItemTypes": "Audio",
            "SearchTerm": track_name,
            "Limit": 20,
            "Fields": "Artists,AlbumArtist",
        }
        client = _get_shared_client()
        try:
            resp = await client.get(
                f"{self.base_url}/Items",
                headers=self._auth_header(),
                params=params,
            )
            resp.raise_for_status()
            artist_lower = artist.lower()
            for item in resp.json().get("Items", []):
                item_artist = (
                    (item.get("Artists") or [""])[0]
                    or item.get("AlbumArtist", "")
                ).lower()
                # Fuzzy match: either contains the other
                if artist_lower in item_artist or item_artist in artist_lower:
                    return item.get("Id")
        except Exception as exc:
            logger.error(f"search_library failed: {exc}")
        return None

    async def search_album(
        self, album_name: str, artist: str
    ) -> Optional[str]:
        """Return Jellyfin item ID if album exists, else None."""
        params = {
            "Recursive": "true",
            "IncludeItemTypes": "MusicAlbum",
            "SearchTerm": album_name,
            "Limit": 20,
            "Fields": "Artists,AlbumArtist",
        }
        client = _get_shared_client()
        try:
            resp = await client.get(
                f"{self.base_url}/Items",
                headers=self._auth_header(),
                params=params,
            )
            resp.raise_for_status()
            artist_lower = artist.lower()
            for item in resp.json().get("Items", []):
                item_artist = (
                    (item.get("Artists") or [""])[0]
                    or item.get("AlbumArtist", "")
                ).lower()
                if artist_lower in item_artist or item_artist in artist_lower:
                    return item.get("Id")
        except Exception as exc:
            logger.error(f"search_album failed: {exc}")
        return None

    # ──────────────────────────── Playlists ──────────────────────────────────

    async def get_user_playlists(self, user_id: str) -> List[Dict[str, Any]]:
        params = {"Recursive": "true", "IncludeItemTypes": "Playlist"}
        client = _get_shared_client()
        try:
            resp = await client.get(
                f"{self.base_url}/Users/{user_id}/Items",
                headers=self._auth_header(),
                params=params,
            )
            resp.raise_for_status()
            return resp.json().get("Items", [])
        except Exception as exc:
            logger.error(f"get_user_playlists failed: {exc}")
        return []

    async def delete_playlist(self, playlist_id: str) -> bool:
        client = _get_shared_client()
        try:
            resp = await client.delete(
                f"{self.base_url}/Items/{playlist_id}",
                headers=self._auth_header(),
            )
            return resp.status_code in (200, 204)
        except Exception as exc:
            logger.error(f"delete_playlist {playlist_id} failed: {exc}")
        return False

    async def create_playlist(
        self, user_id: str, name: str, track_ids: List[str]
    ) -> Optional[str]:
        """Create or recreate a Jellyfin playlist. Returns new playlist ID."""
        if not track_ids:
            return None
        params = {"Name": name, "Ids": ",".join(track_ids), "UserId": user_id}
        client = _get_shared_client()
        try:
            resp = await client.post(
                f"{self.base_url}/Playlists",
                headers=self._auth_header(),
                params=params,
            )
            if resp.status_code in (200, 201):
                return resp.json().get("Id")
            logger.warning(f"create_playlist got {resp.status_code} for '{name}'")
        except Exception as exc:
            logger.error(f"create_playlist failed for '{name}': {exc}")
        return None

    async def get_all_library_audio(self, user_id: str) -> List[Dict[str, Any]]:
        """Fetch all audio items in the user's library for caching."""
        url = f"{self.base_url}/Users/{user_id}/Items"
        params = {
            "Recursive": "true",
            "IncludeItemTypes": "Audio",
            "Fields": "Artists,AlbumArtist",
        }
        client = _get_shared_client()
        try:
            resp = await client.get(url, headers=self._auth_header(), params=params, timeout=60.0)
            resp.raise_for_status()
            return resp.json().get("Items", [])
        except Exception as exc:
            logger.exception("get_all_library_audio failed")
            return []

    async def get_activity_log(self, limit: int = 2000) -> List[Dict[str, Any]]:
        """Fetch system activity log entries."""
        url = f"{self.base_url}/System/ActivityLog/Entries"
        params = {"limit": limit}
        client = _get_shared_client()
        try:
            resp = await client.get(url, headers=self._auth_header(), params=params, timeout=30.0)
            resp.raise_for_status()
            return resp.json().get("Items", [])
        except Exception as exc:
            logger.error(f"get_activity_log failed: {exc}")
            return []

    async def get_items_metadata(self, user_id: str, item_ids: List[str]) -> List[Dict[str, Any]]:
        """Fetch metadata for a list of item IDs in a single batch query."""
        if not item_ids:
            return []
        url = f"{self.base_url}/Users/{user_id}/Items"
        params = {
            "Ids": ",".join(item_ids),
            "Fields": "UserData,Genres,Artists,AlbumArtist",
        }
        client = _get_shared_client()
        try:
            resp = await client.get(url, headers=self._auth_header(), params=params, timeout=30.0)
            resp.raise_for_status()
            return resp.json().get("Items", [])
        except Exception as exc:
            logger.error(f"get_items_metadata failed: {exc}")
            return []

