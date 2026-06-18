"""
Smoke test for find_album_outliers against LIVE Last.fm data.

Run with: python -m tests.test_album_outliers <artist_name>
"""

import asyncio
import sys

from app.core.lastfm_client import LastFMClient
from app.core.album_outliers import find_album_outliers


async def main(artist: str):
    client = LastFMClient()

    async def fetch_top_albums(artist_name: str):
        return await client.get_artist_top_albums(artist_name, limit=20)

    async def fetch_album_tags(artist_name: str, album_name: str):
        info = await client.get_album_info(artist_name, album_name)
        return [{"tag": t, "count": 100} for t in info.get("tags", [])]

    try:
        result = await find_album_outliers(artist, fetch_top_albums, fetch_album_tags)

        print(f"--- '{artist}' typical tag profile ---")
        for tag, weight in sorted(result["typical_tags"].items(), key=lambda kv: kv[1], reverse=True)[:10]:
            print(f"  {tag:<20} {weight:.3f}")

        print(f"\n--- All albums considered (sorted by relevance, lowest = most outlier-like) ---")
        for a in result["albums"]:
            flag = "  <-- OUTLIER" if a["is_outlier"] else ""
            print(f"  {a['name']:<40} relevance: {a['tag_relevance']:.4f}{flag}")
            print(f"      tags: {', '.join(a['tags'][:8])}")

        print(f"\n--- Detected outliers ---")
        if result["outliers"]:
            for a in result["outliers"]:
                print(f"  {a['name']} (relevance: {a['tag_relevance']:.4f})")
        else:
            print("  (none detected)")

    finally:
        await client.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m tests.test_album_outliers <artist_name>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
