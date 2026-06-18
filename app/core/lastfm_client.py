"""
Thin async client for the Last.fm public API.

Docs: https://www.last.fm/api

All methods here use read-only public endpoints, so only an API key
is required (no shared secret, no OAuth, no signed requests).
"""

import asyncio
import httpx
from app.core.config import settings

BASE_URL = "https://ws.audioscrobbler.com/2.0/"


class LastFMError(Exception):
    """Raised when Last.fm returns an error payload (bad user, bad key, etc.)."""

    def __init__(self, message: str, code: int | None = None):
        self.code = code
        super().__init__(message)


class LastFMClient:
    def __init__(self, api_key: str | None = None, timeout: float = 20.0, max_concurrent: int = 8):
        self.api_key = api_key or settings.lastfm_api_key
        # explicit pool limits: httpx's defaults can leave later requests in
        # a burst waiting for a free connection slot, which can exceed the
        # timeout under load. A semaphore caps how many requests we even
        # attempt at once, which is gentler on both our connection pool and
        # Last.fm's server (avoids looking like a burst/abuse pattern).
        limits = httpx.Limits(max_connections=max_concurrent, max_keepalive_connections=max_concurrent)
        self._client = httpx.AsyncClient(timeout=timeout, limits=limits)
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def close(self):
        await self._client.aclose()

    async def _call(self, method: str, retries: int = 2, **params) -> dict:
        query = {
            "method": method,
            "api_key": self.api_key,
            "format": "json",
            **params,
        }

        async with self._semaphore:
            last_exc = None
            for attempt in range(retries + 1):
                try:
                    resp = await self._client.get(BASE_URL, params=query)
                    data = resp.json()
                    if "error" in data:
                        raise LastFMError(data.get("message", "Unknown Last.fm error"), data.get("error"))
                    return data
                except httpx.TimeoutException as exc:
                    last_exc = exc
                    if attempt < retries:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    raise LastFMError(f"Request to Last.fm timed out after {retries + 1} attempts") from exc
            raise LastFMError("Unexpected retry loop exit") from last_exc

    # ---- user-level methods ----

    async def get_top_artists(self, username: str, period: str = "overall", limit: int = 50) -> list[dict]:
        """
        period: overall | 7day | 1month | 3month | 6month | 12month
        Returns list of {name, playcount, mbid, rank}
        """
        data = await self._call(
            "user.gettopartists",
            user=username,
            period=period,
            limit=limit,
        )
        artists = data.get("topartists", {}).get("artist", [])
        return [
            {
                "name": a["name"],
                "playcount": int(a.get("playcount", 0)),
                "mbid": a.get("mbid", ""),
                "rank": int(a.get("@attr", {}).get("rank", 0)),
            }
            for a in artists
        ]

    async def get_top_tracks(self, username: str, period: str = "overall", limit: int = 50) -> list[dict]:
        """
        period: overall | 7day | 1month | 3month | 6month | 12month
        Returns list of {artist, track, playcount, rank}
        """
        data = await self._call(
            "user.gettoptracks",
            user=username,
            period=period,
            limit=limit,
        )
        tracks = data.get("toptracks", {}).get("track", [])
        return [
            {
                "artist": t.get("artist", {}).get("name", ""),
                "track": t.get("name", ""),
                "playcount": int(t.get("playcount", 0)),
                "rank": int(t.get("@attr", {}).get("rank", 0)),
            }
            for t in tracks
        ]

    async def get_recent_tracks(self, username: str, limit: int = 50) -> list[dict]:
        data = await self._call(
            "user.getrecenttracks",
            user=username,
            limit=limit,
        )
        tracks = data.get("recenttracks", {}).get("track", [])
        return [
            {
                "artist": t.get("artist", {}).get("#text", ""),
                "track": t.get("name", ""),
                "album": t.get("album", {}).get("#text", ""),
            }
            for t in tracks
        ]

    async def get_top_tags_for_user(self, username: str, period: str = "overall", limit: int = 20) -> list[dict]:
        """
        Last.fm has no direct 'user top tags' endpoint, so this is built by the
        caller from get_top_artists + get_artist_top_tags. Left as a placeholder
        method name for clarity in the pipeline; actual aggregation happens in
        the taste profile builder.
        """
        raise NotImplementedError("Aggregate via TasteProfileBuilder instead.")

    # ---- artist-level methods ----

    async def get_similar_artists(self, artist: str, limit: int = 30) -> list[dict]:
        """Returns list of {name, match} where match is a 0-1 similarity score from Last.fm."""
        data = await self._call(
            "artist.getsimilar",
            artist=artist,
            limit=limit,
            autocorrect=1,
        )
        similar = data.get("similarartists", {}).get("artist", [])
        return [
            {"name": a["name"], "match": float(a.get("match", 0))}
            for a in similar
        ]

    async def get_artist_top_tags(self, artist: str, limit: int = 10) -> list[dict]:
        """Returns list of {tag, count} - count is Last.fm's relative weight, not a raw frequency."""
        data = await self._call(
            "artist.gettoptags",
            artist=artist,
            autocorrect=1,
        )
        tags = data.get("toptags", {}).get("tag", [])[:limit]
        return [
            {"tag": t["name"].lower(), "count": int(t.get("count", 0))}
            for t in tags
        ]

    async def get_artist_info(self, artist: str) -> dict:
        """
        Returns basic info including global listener/playcount (used for
        popularity-inverse weighting) and mbid (MusicBrainz ID).

        mbid is included specifically because real testing showed it's a
        reliable signal for distinguishing legitimate catalogued artists
        from one-off collaboration/compilation credits: e.g. "Hayley
        Williams" (real solo discography) has a real MBID, while
        "Selena Gomez, benny blanco & The Marías" (a multi-artist single
        credit bundled as one Last.fm entry) has none. Used by
        ArtistDataCache/DiscoveryWalk to filter these out of
        recommendations - see app/core/discovery_walk.py.
        """
        data = await self._call(
            "artist.getinfo",
            artist=artist,
            autocorrect=1,
        )
        info = data.get("artist", {})
        stats = info.get("stats", {})
        return {
            "name": info.get("name", artist),
            "listeners": int(stats.get("listeners", 0)),
            "playcount": int(stats.get("playcount", 0)),
            "mbid": info.get("mbid") or None,
        }
