"""
Smoke test for pick_entry_point_album against LIVE Last.fm data (not the
hand-transcribed test cases used during initial debugging).

Run with: python -m tests.test_album_selection <artist_name> [<artist_name> ...]
"""

import asyncio
import sys

from app.core.lastfm_client import LastFMClient
from app.core.album_selection import pick_entry_point_album


async def main(artists: list[str]):
    client = LastFMClient()
    try:
        for artist in artists:
            albums = await client.get_artist_top_albums(artist, limit=15)
            print(f"--- '{artist}' raw top albums ---")
            for a in albums:
                print(f"  {a['name']:<45} playcount: {a['playcount']:,}  mbid: {a['mbid']}")

            entry_point = pick_entry_point_album(albums)
            print(f"\n  >> Chosen entry point: {entry_point['name'] if entry_point else '(none)'}\n")

    finally:
        await client.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m tests.test_album_selection <artist_name> [<artist_name> ...]")
        sys.exit(1)
    asyncio.run(main(sys.argv[1:]))
