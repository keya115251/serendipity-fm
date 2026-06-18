"""
Cache-aware data access layer sitting between the raw LastFMClient and
anything that needs artist data (taste profile builder, graph builder,
and eventually the discovery walk). Checks SQLite first; only calls the
live API on a cache miss or a stale entry.

Staleness window defaults to 14 days. Similarity edges and tags change
slowly enough that this is a safe tradeoff; artist info (listener counts)
grows continuously but slowly enough that a 14-day-old count is still a
perfectly reasonable signal for a soft popularity penalty - we don't need
today's exact number, just roughly how mainstream an artist is.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from app.core.lastfm_client import LastFMClient, LastFMError
from app.db.models import ArtistInfo, ArtistTag, SimilarityEdge, get_session_factory


class ArtistDataCache:
    def __init__(
        self,
        database_path: str,
        client: LastFMClient | None = None,
        staleness_days: int = 14,
        max_concurrent_db_ops: int = 12,
    ):
        self.client = client or LastFMClient()
        self.SessionFactory = get_session_factory(database_path)
        self.staleness_window = timedelta(days=staleness_days)
        # SQLite doesn't handle large numbers of truly concurrent
        # connections well, regardless of the SQLAlchemy pool size. This
        # semaphore caps how many DB-touching cache methods run at once,
        # which is the direct fix for a real sqlalchemy.exc.TimeoutError
        # hit during discovery-walk testing once concurrent hop expansion
        # started issuing dozens of simultaneous get_similar_artists calls.
        self._db_semaphore = asyncio.Semaphore(max_concurrent_db_ops)

    def _is_fresh(self, fetched_at: datetime) -> bool:
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - fetched_at < self.staleness_window

    async def get_artist_info(self, artist: str) -> dict:
        async with self._db_semaphore:
            session = self.SessionFactory()
            try:
                row = session.query(ArtistInfo).filter_by(artist_name=artist).first()
                if row and self._is_fresh(row.fetched_at):
                    return {"name": artist, "listeners": row.listeners, "playcount": row.playcount, "mbid": row.mbid}
                cached_fallback = (
                    {"name": artist, "listeners": row.listeners, "playcount": row.playcount, "mbid": row.mbid}
                    if row
                    else None
                )
            finally:
                session.close()

        # live API call happens OUTSIDE the semaphore, so a slow network
        # request doesn't hold a DB pool slot idle while waiting
        try:
            info = await self.client.get_artist_info(artist)
        except LastFMError:
            # if the live call fails but we have ANY cached value (even
            # stale), serving stale data is better than failing the
            # whole request - popularity is a soft signal, not a
            # correctness-critical one
            return cached_fallback or {"name": artist, "listeners": 0, "playcount": 0, "mbid": None}

        async with self._db_semaphore:
            session = self.SessionFactory()
            try:
                row = session.query(ArtistInfo).filter_by(artist_name=artist).first()
                if row:
                    row.listeners = info["listeners"]
                    row.playcount = info["playcount"]
                    row.mbid = info.get("mbid")
                    row.fetched_at = datetime.now(timezone.utc)
                else:
                    session.add(
                        ArtistInfo(
                            artist_name=artist,
                            listeners=info["listeners"],
                            playcount=info["playcount"],
                            mbid=info.get("mbid"),
                        )
                    )
                session.commit()
                return info
            finally:
                session.close()

    async def get_artist_tags(self, artist: str, limit: int = 8) -> list[dict]:
        async with self._db_semaphore:
            session = self.SessionFactory()
            try:
                rows = session.query(ArtistTag).filter_by(artist_name=artist).all()
                if rows and self._is_fresh(rows[0].fetched_at):
                    return [{"tag": r.tag, "count": r.weight} for r in rows[:limit]]
                cached_fallback = [{"tag": r.tag, "count": r.weight} for r in rows[:limit]] if rows else None
            finally:
                session.close()

        try:
            tags = await self.client.get_artist_top_tags(artist, limit=limit)
        except LastFMError:
            return cached_fallback or []

        async with self._db_semaphore:
            session = self.SessionFactory()
            try:
                # replace existing rows for this artist rather than accumulating duplicates
                session.query(ArtistTag).filter_by(artist_name=artist).delete()
                for t in tags:
                    session.add(ArtistTag(artist_name=artist, tag=t["tag"], weight=t["count"]))
                session.commit()
                return tags
            finally:
                session.close()

    async def get_similar_artists(self, artist: str, limit: int = 30) -> list[dict]:
        async with self._db_semaphore:
            session = self.SessionFactory()
            try:
                rows = session.query(SimilarityEdge).filter_by(source_artist=artist).all()
                if rows and self._is_fresh(rows[0].fetched_at):
                    sorted_rows = sorted(rows, key=lambda r: r.match, reverse=True)[:limit]
                    return [{"name": r.similar_artist, "match": r.match} for r in sorted_rows]
                cached_fallback = (
                    [
                        {"name": r.similar_artist, "match": r.match}
                        for r in sorted(rows, key=lambda r: r.match, reverse=True)[:limit]
                    ]
                    if rows
                    else None
                )
            finally:
                session.close()

        try:
            similar = await self.client.get_similar_artists(artist, limit=limit)
        except LastFMError:
            return cached_fallback or []

        async with self._db_semaphore:
            session = self.SessionFactory()
            try:
                session.query(SimilarityEdge).filter_by(source_artist=artist).delete()
                for s in similar:
                    session.add(SimilarityEdge(source_artist=artist, similar_artist=s["name"], match=s["match"]))
                session.commit()
                return similar
            finally:
                session.close()

    async def close(self):
        await self.client.close()
