"""
Smoke test for the no-account multi-artist discovery walk against LIVE
Last.fm data.

Run with: python -m tests.test_multi_seed <artist1> <artist2> ... (up to 7) [--niche 0.0-1.0]

--niche is a flag, not a positional argument, since the artist name list
is variable-length (1-7 names) - there's no way to tell "is this the 8th
artist or the niche_level value" from position alone the way the
fixed-arity test_discovery_walk.py could.
"""

import asyncio
import sys

from app.core.config import settings
from app.core.lastfm_client import LastFMClient
from app.core.multi_seed import recommend_from_artists
from app.db.artist_cache import ArtistDataCache


async def main(artist_names: list[str], niche_level: float = 0.5):
    client = LastFMClient()
    cache = ArtistDataCache(settings.database_path, client)

    try:
        print(f"Seeding from: {artist_names}  (niche_level={niche_level})\n")
        results = await recommend_from_artists(artist_names, cache, niche_level=niche_level)

        print(f"--- Top {len(results)} recommendations ---")
        for i, c in enumerate(results, 1):
            print(f"\n{i}. {c.artist}")
            print(f"   start here: {c.entry_point_album or '(none)'}")
            print(f"   genres: {', '.join(c.tags) if c.tags else '(none)'}")
            print(f"   hop distance: {c.hop_distance}  |  listeners: {c.listeners:,}  |  score: {c.final_score:.4f}")
            print(f"   path: {' -> '.join(c.path)}")

    finally:
        await cache.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m tests.test_multi_seed <artist1> <artist2> ... (up to 7) [--niche 0.0-1.0]")
        sys.exit(1)

    raw_args = sys.argv[1:]
    niche = 0.5
    if "--niche" in raw_args:
        idx = raw_args.index("--niche")
        niche = float(raw_args[idx + 1])
        raw_args = raw_args[:idx] + raw_args[idx + 2 :]

    asyncio.run(main(raw_args[:7], niche))
