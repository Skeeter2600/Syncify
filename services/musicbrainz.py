import httpx
import logging
import urllib.parse
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# Shared async client — MusicBrainz guidelines require a descriptive User-Agent
_MB_HEADERS = {
    "User-Agent": "Syncify/1.0.0 (https://beckandersonmedia.com; contact@beckandersonmedia.com)"
}
_MB_BASE = "https://musicbrainz.org/ws/2"

_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            headers=_MB_HEADERS,
            timeout=httpx.Timeout(10.0, connect=5.0)
        )
    return _client


class MusicBrainzClient:

    async def search_artists(self, query: str) -> List[Dict[str, Any]]:
        """Search for artists by name. Returns up to 10 matches."""
        client = _get_client()
        try:
            resp = await client.get(
                f"{_MB_BASE}/artist",
                params={"query": query, "fmt": "json", "limit": 10}
            )
            if resp.status_code != 200:
                logger.warning("MusicBrainz artist search returned %s", resp.status_code)
                return []
            return [
                {
                    "mbid": a.get("id"),
                    "name": a.get("name"),
                    "type": a.get("type") or "Unknown",
                    "disambiguation": a.get("disambiguation", ""),
                    "country": a.get("country", ""),
                }
                for a in resp.json().get("artists", [])
            ]
        except Exception:
            logger.exception("MusicBrainz artist search failed")
            return []

    async def get_artist_details(self, mbid: str) -> Optional[Dict[str, Any]]:
        """Fetch artist info, release-group catalog, and Wikipedia bio in parallel."""
        client = _get_client()
        try:
            resp = await client.get(
                f"{_MB_BASE}/artist/{mbid}",
                params={"inc": "url-rels+release-groups", "fmt": "json"}
            )
            if resp.status_code != 200:
                logger.warning("MusicBrainz artist/%s returned %s", mbid, resp.status_code)
                return None
            data = resp.json()
        except Exception:
            logger.exception("MusicBrainz get_artist_details failed for %s", mbid)
            return None

        # Build catalog grouped by release type
        catalog: Dict[str, List[Dict[str, Any]]] = {
            "Album": [], "Single": [], "EP": [], "Other": []
        }
        for rg in data.get("release-groups", []):
            group = rg.get("primary-type") or "Other"
            if group not in catalog:
                group = "Other"
            date = rg.get("first-release-date") or ""
            catalog[group].append({
                "mbid": rg["id"],
                "title": rg.get("title", ""),
                "date": date or "Unknown",
                "cover_url": f"https://coverartarchive.org/release-group/{rg['id']}/front-250",
            })

        # Sort each group newest-first; unknown dates sink to the bottom
        for releases in catalog.values():
            releases.sort(key=lambda r: r["date"] if r["date"] != "Unknown" else "0000", reverse=True)

        # Extract Wikidata QID from relations
        qid = next(
            (
                rel["url"]["resource"].split("/")[-1]
                for rel in data.get("relations", [])
                if rel.get("type") == "wikidata" and "wikidata.org/wiki/" in rel.get("url", {}).get("resource", "")
            ),
            None
        )

        bio, thumbnail = await self._fetch_wiki_bio(qid) if qid else ("", None)

        return {
            "mbid": data.get("id"),
            "name": data.get("name", ""),
            "disambiguation": data.get("disambiguation", ""),
            "bio": bio,
            "profile_image": thumbnail,
            "catalog": catalog,
        }

    async def get_release_tracks(self, release_group_mbid: str) -> List[Dict[str, Any]]:
        """Fetch the track list for the first release in a release group."""
        client = _get_client()
        try:
            # Step 1: get a release ID from the release-group
            rg_resp = await client.get(
                f"{_MB_BASE}/release-group/{release_group_mbid}",
                params={"inc": "releases", "fmt": "json"}
            )
            releases = rg_resp.json().get("releases", []) if rg_resp.status_code == 200 else []
            if not releases:
                return []

            # Step 2: get recordings for the first release
            rel_resp = await client.get(
                f"{_MB_BASE}/release/{releases[0]['id']}",
                params={"inc": "recordings", "fmt": "json"}
            )
            if rel_resp.status_code != 200:
                return []

            tracks = [
                {
                    "title": t.get("title", ""),
                    "position": t.get("position"),
                    "length_ms": t.get("length"),
                }
                for medium in rel_resp.json().get("media", [])
                for t in medium.get("tracks", [])
            ]
            return tracks
        except Exception:
            logger.exception("MusicBrainz get_release_tracks failed for %s", release_group_mbid)
            return []

    async def _fetch_wiki_bio_by_mbid(self, mbid: str) -> tuple[str, Optional[str]]:
        """Lightweight helper: look up Wikidata QID for an artist MBID, then fetch thumbnail."""
        client = _get_client()
        try:
            resp = await client.get(
                f"{_MB_BASE}/artist/{mbid}",
                params={"inc": "url-rels", "fmt": "json"}
            )
            if resp.status_code != 200:
                return "", None
            qid = next(
                (
                    rel["url"]["resource"].split("/")[-1]
                    for rel in resp.json().get("relations", [])
                    if rel.get("type") == "wikidata" and "wikidata.org/wiki/" in rel.get("url", {}).get("resource", "")
                ),
                None
            )
            if qid:
                return await self._fetch_wiki_bio(qid)
        except Exception:
            logger.debug("_fetch_wiki_bio_by_mbid failed for %s", mbid)
        return "", None

    async def _fetch_wiki_bio(self, qid: str) -> tuple[str, Optional[str]]:
        """Return (bio_text, thumbnail_url) from Wikipedia via Wikidata QID."""
        client = _get_client()
        try:
            wd = await client.get(
                "https://www.wikidata.org/w/api.php",
                params={
                    "action": "wbgetentities", "ids": qid,
                    "props": "sitelinks/urls", "sitefilter": "enwiki",
                    "format": "json"
                },
                timeout=5.0
            )
            title = (
                wd.json().get("entities", {}).get(qid, {})
                .get("sitelinks", {}).get("enwiki", {}).get("title")
            )
            if not title:
                return "", None

            wiki = await client.get(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title)}",
                timeout=5.0
            )
            if wiki.status_code == 200:
                body = wiki.json()
                return body.get("extract", ""), body.get("thumbnail", {}).get("source")
        except Exception:
            logger.debug("Wikipedia bio fetch failed for QID %s", qid)
        return "", None
