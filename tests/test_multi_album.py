"""
Smoke test for the no-account multi-album discovery feature against LIVE
Last.fm data.

Run with: python -m tests.test_multi_album "Artist1::Album1" "Artist2::Album2" ... (up to 7)
(:: separator since both artist and album names can contain spaces)
"""

import asyncio
import sys

from app.core.lastfm_client import LastFMClient
from app.core.multi_album import recommend_from_albums


async def main(seed_pairs: list[tuple[str, str]]):
    client = LastFMClient()

    async def fetch_album_tags(artist: str, album: str) -> list[str]:
        info = await client.get_album_info(artist, album)
        return info.get("tags", [])

    async def fetch_similar_artists(name: str):
        return await client.get_similar_artists(name, limit=20)

    try:
        print(f"Seeding from: {seed_pairs}\n")
        results = await recommend_from_albums(
            seed_pairs, fetch_album_tags, fetch_similar_artists, client.get_artist_top_albums
        )

        print(f"--- Top {len(results)} album recommendations ---")
        for i, r in enumerate(results, 1):
            print(
                f"{i}. {r['album']:<35} by {r['artist']:<25} score: {r['score']:.3f}  "
                f"(tag_overlap: {r['tag_overlap']:.2f}, artist_sim: {r['artist_similarity']:.2f})"
            )

    finally:
        await client.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python -m tests.test_multi_album "Artist1::Album1" "Artist2::Album2" ... (up to 7)')
        sys.exit(1)

    pairs = []
    for arg in sys.argv[1:8]:
        if "::" not in arg:
            print(f"Skipping malformed argument (missing '::' separator): {arg}")
            continue
        artist, album = arg.split("::", 1)
        pairs.append((artist, album))

    asyncio.run(main(pairs))
