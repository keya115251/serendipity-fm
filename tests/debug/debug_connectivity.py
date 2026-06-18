"""
Diagnostic: print the actual connectivity-to-known-artists scores computed
during walk expansion for specific artists, to verify the signal is doing
what it's supposed to before building a niche-ness slider on top of it.

Run with: python -m tests.debug.debug_connectivity <username>
"""

import asyncio
import sys

from app.core.config import settings
from app.core.lastfm_client import LastFMClient
from app.core.taste_profile import TasteProfileBuilder
from app.db.artist_cache import ArtistDataCache

CHECK_ARTISTS = ["Gregory Alan Isakov", "Flower Face", "Not For Radio", "Evanescence", "Matt Berninger"]


async def main(username: str):
    client = LastFMClient()
    profile_builder = TasteProfileBuilder(client)
    cache = ArtistDataCache(settings.database_path, client)

    try:
        profile = await profile_builder.build(username)
        known_artists = set(profile.long_term.artists) | set(profile.short_term.artists)
        print(f"Known artists ({len(known_artists)}): {sorted(known_artists)}\n")

        for artist in CHECK_ARTISTS:
            similar = await cache.get_similar_artists(artist, limit=20)
            overlap = [(s["name"], s["match"]) for s in similar if s["name"] in known_artists]
            connectivity = sum(s["match"] for s in similar if s["name"] in known_artists)
            combined_score_example = 1.0 * (0.2 + connectivity)  # assuming match=1.0 for illustration

            print(f"--- {artist} ---")
            print(f"  similar_artists fetched: {len(similar)}")
            print(f"  overlap with known_artists: {overlap}")
            print(f"  connectivity (sum of overlapping match scores): {connectivity:.4f}")
            print(f"  combined_score if match=1.0: {combined_score_example:.4f}")
            print()

    finally:
        await cache.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m tests.debug.debug_connectivity <username>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
