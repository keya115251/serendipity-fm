"""
Smoke test for the standalone artist/album lookup feature (no Last.fm
account needed).

Run with:
  python -m tests.test_lookup artist <artist_name>
  python -m tests.test_lookup album <artist_name> <album_name>
"""

import asyncio
import sys

from app.core.lastfm_client import LastFMClient
from app.core.lookup import find_similar_artists, find_similar_albums


async def run_artist_lookup(client: LastFMClient, artist_name: str):
    async def fetch_similar_artists(name: str):
        return await client.get_similar_artists(name, limit=20)

    results = await find_similar_artists(
        artist_name, fetch_similar_artists, client.get_artist_info, client.get_artist_top_albums
    )

    print(f"--- Similar artists to '{artist_name}' ---")
    for r in results:
        print(f"  {r['artist']:<30} match: {r['match']:.3f}  start here: {r['entry_point_album']}")


async def run_album_lookup(client: LastFMClient, artist_name: str, album_name: str):
    async def fetch_album_tags(artist: str, album: str) -> list[str]:
        info = await client.get_album_info(artist, album)
        return info.get("tags", [])

    async def fetch_similar_artists(name: str):
        return await client.get_similar_artists(name, limit=20)

    results = await find_similar_albums(
        artist_name, album_name, fetch_album_tags, fetch_similar_artists, client.get_artist_top_albums
    )

    print(f"--- Albums similar to '{album_name}' by {artist_name} ---")
    for r in results:
        print(
            f"  {r['album']:<35} by {r['artist']:<25} score: {r['score']:.3f}  "
            f"(tag_overlap: {r['tag_overlap']:.2f}, artist_sim: {r['artist_similarity']:.2f})"
        )


async def main():
    if len(sys.argv) < 3:
        print("Usage:")
        print("  python -m tests.test_lookup artist <artist_name>")
        print("  python -m tests.test_lookup album <artist_name> <album_name>")
        sys.exit(1)

    mode = sys.argv[1]
    client = LastFMClient()
    try:
        if mode == "artist":
            await run_artist_lookup(client, sys.argv[2])
        elif mode == "album":
            if len(sys.argv) < 4:
                print("album mode needs both <artist_name> and <album_name>")
                sys.exit(1)
            await run_album_lookup(client, sys.argv[2], sys.argv[3])
        else:
            print(f"Unknown mode '{mode}', expected 'artist' or 'album'")
            sys.exit(1)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
