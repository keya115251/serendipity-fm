"""
Smoke test for ArtistDataCache - verifies cache miss (live fetch + write)
then cache hit (no live call needed) behavior, and prints timing for both
so the speedup is visible, not just claimed.

Run with: python -m tests.test_artist_cache <artist_name>
"""

import asyncio
import sys
import time
from app.core.config import settings
from app.core.lastfm_client import LastFMClient
from app.db.artist_cache import ArtistDataCache


async def main(artist: str):
    client = LastFMClient()
    cache = ArtistDataCache(settings.database_path, client)
    try:
        print(f"--- First call (expect cache MISS, live fetch) for '{artist}' ---")
        start = time.monotonic()
        info1 = await cache.get_artist_info(artist)
        tags1 = await cache.get_artist_tags(artist)
        similar1 = await cache.get_similar_artists(artist, limit=10)
        elapsed1 = time.monotonic() - start
        print(f"  info: {info1}")
        print(f"  tags: {tags1[:3]}...")
        print(f"  similar (first 3): {similar1[:3]}")
        print(f"  elapsed: {elapsed1:.3f}s")

        print(f"\n--- Second call (expect cache HIT, no live fetch) for '{artist}' ---")
        start = time.monotonic()
        info2 = await cache.get_artist_info(artist)
        tags2 = await cache.get_artist_tags(artist)
        similar2 = await cache.get_similar_artists(artist, limit=10)
        elapsed2 = time.monotonic() - start
        print(f"  info: {info2}")
        print(f"  tags: {tags2[:3]}...")
        print(f"  similar (first 3): {similar2[:3]}")
        print(f"  elapsed: {elapsed2:.3f}s")

        print(f"\nSpeedup: {elapsed1 / elapsed2:.1f}x faster on cache hit" if elapsed2 > 0 else "")
        assert info1 == info2, "info mismatch between cache miss and hit!"

    finally:
        await cache.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m tests.test_artist_cache <artist_name>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
