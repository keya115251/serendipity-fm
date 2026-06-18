"""
Diagnostic: check what artist.getTopAlbums actually returns for a few
real artists, to verify whether "top album by Last.fm playcount" is a
sensible signal for picking an entry-point album, or whether it tends to
surface compilations/deluxe editions/live albums instead of an artist's
actual main studio work.

Run with: python -m tests.debug.debug_top_albums
"""

import asyncio

from app.core.lastfm_client import LastFMClient

CHECK_ARTISTS = ["Hozier", "Radiohead", "Bayside", "Avkrvst"]


async def main():
    client = LastFMClient()
    try:
        for artist in CHECK_ARTISTS:
            albums = await client.get_artist_top_albums(artist, limit=8)
            print(f"--- Top albums for '{artist}' ---")
            for a in albums:
                print(f"  {a['name']:<40} playcount: {a['playcount']:,}  mbid: {a['mbid']}")
            print()
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
