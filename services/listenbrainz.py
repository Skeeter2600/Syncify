import httpx
from typing import List, Dict, Any, Optional
import time

class ListenBrainzClient:
    def __init__(self):
        self.base_url = "https://api.listenbrainz.org/1"

    async def validate_token(self, token: str) -> Optional[str]:
        """Validate user token. Returns username if valid, else None."""
        url = f"{self.base_url}/validate-token"
        headers = {"Authorization": f"Token {token}"}
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("valid") is True:
                        return data.get("user_name")
            except Exception as e:
                print(f"ListenBrainz token validation error: {e}")
        return None

    async def submit_listens(self, token: str, listens: List[Dict[str, Any]]) -> bool:
        """Submit list of listens in import mode."""
        if not listens:
            return True
            
        url = f"{self.base_url}/submit-listens"
        headers = {
            "Authorization": f"Token {token}",
            "Content-Type": "application/json"
        }
        
        # listens list elements structure:
        # {
        #   "listened_at": int,
        #   "track_metadata": {
        #      "artist_name": str,
        #      "track_name": str,
        #      "release_name": str (optional)
        #   }
        # }
        payload = {
            "listen_type": "import",
            "payload": listens
        }
        
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(url, json=payload, headers=headers)
                return resp.status_code == 200
            except Exception as e:
                print(f"ListenBrainz listen submission error: {e}")
        return False

    async def get_weekly_exploration(self, username: str) -> List[Dict[str, str]]:
        """Fetch user's Weekly Exploration tracks from ListenBrainz."""
        # 1. Fetch user playlists
        url = f"{self.base_url}/user/{username}/playlists"
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    return []
                
                playlists = resp.json().get("playlists", [])
                exploration_playlist_mbid = None
                for pl in playlists:
                    title = pl.get("playlist", {}).get("title", "")
                    if "weekly exploration" in title.lower():
                        uri = pl.get("playlist", {}).get("identifier", "")
                        if "jspf:playlist:" in uri:
                            exploration_playlist_mbid = uri.split(":")[-1]
                        elif "/playlist/" in uri:
                            exploration_playlist_mbid = uri.split("/")[-1]
                        
                        if exploration_playlist_mbid:
                            break
                
                if not exploration_playlist_mbid:
                    return []
                
                # 2. Fetch tracks for the playlist
                pl_url = f"{self.base_url}/playlist/{exploration_playlist_mbid}"
                resp_pl = await client.get(pl_url)
                if resp_pl.status_code != 200:
                    return []
                
                pl_data = resp_pl.json().get("playlist", {})
                tracks = pl_data.get("track", [])
                result = []
                for t in tracks:
                    track_metadata = t.get("extension", {}).get("https://musicbrainz.org/doc/jspf#playlist", {})
                    track_name = t.get("title", "")
                    artist_name = t.get("creator", "")
                    album_name = track_metadata.get("release", "")
                    result.append({
                        "track_name": track_name,
                        "artist_name": artist_name,
                        "album_name": album_name
                    })
                return result
            except Exception as e:
                print(f"ListenBrainz weekly exploration fetch error: {e}")
        return []

    async def get_similar_artists(self, artist_name: str) -> List[str]:
        """Fetch similar artists from ListenBrainz / MusicBrainz tags if possible."""
        # We can look up similar artists using ListenBrainz or MusicBrainz endpoints
        # Let's perform a simple search or query standard tag info
        # To avoid failure, return empty list or basic mockup since this is a fallback for similar genres
        return []

    async def get_lb_radio_playlist(self, prompt: str, token: Optional[str] = None, mode: str = "medium") -> List[Dict[str, str]]:
        """Fetch a generated playlist from ListenBrainz LB Radio."""
        import logging as _logging
        _log = _logging.getLogger(__name__)
        url = f"{self.base_url}/explore/lb-radio"
        params = {"prompt": prompt, "mode": mode}
        headers = {}
        if token:
            headers["Authorization"] = f"Token {token}"
        async with httpx.AsyncClient() as client:
            try:
                _log.info(f"LB Radio request: GET {url} params={params}")
                resp = await client.get(url, params=params, headers=headers, timeout=15.0)
                _log.info(f"LB Radio response: HTTP {resp.status_code}")
                if resp.status_code != 200:
                    _log.warning(f"LB Radio non-200 for prompt={prompt!r}: {resp.text[:300]}")
                    return []
                
                data = resp.json()
                payload = data.get("payload", {})
                jspf = payload.get("jspf", {})
                playlist = jspf.get("playlist", {})
                tracks = playlist.get("track", [])
                _log.info(f"LB Radio returned {len(tracks)} tracks for prompt={prompt!r}")
                
                result = []
                for t in tracks:
                    track_metadata = t.get("extension", {}).get("https://musicbrainz.org/doc/jspf#playlist", {}) if t.get("extension") else {}
                    track_name = t.get("title", "")
                    artist_name = t.get("creator", "")
                    album_name = track_metadata.get("release", "") if track_metadata else ""
                    result.append({
                        "track_name": track_name,
                        "artist_name": artist_name,
                        "album_name": album_name
                    })
                return result
            except Exception as e:
                _log.error(f"LB Radio fetch error for prompt={prompt!r}: {e}")
        return []
