"""
Print the user's full top 25 overall artists with rank and playcount,
to see where TXT and other potential outliers actually sit, and whether
their playcount is recent-feeling or clearly historical/stale.

Run with: python -m tests.debug.show_full_top_artists <username>
"""

import asyncio
import sys
from app.core.lastfm_client import LastFMClient


async def main(username: str):
    client = LastFMClient()
    try:
        artists = await client.get_top_artists(username, period="overall", limit=25)
        print(f"--- Full top 25 overall artists for '{username}' ---")
        for a in artists:
            print(f"  {a['rank']:>2}. {a['name']:<30} playcount: {a['playcount']}")

        # also pull a shorter period to compare - if TXT/Porcupine Tree don't
        # show up here at all, that's a strong signal they're stale/historical
        print(f"\n--- Top 25 artists, last 12 months, for '{username}' ---")
        recent_artists = await client.get_top_artists(username, period="12month", limit=25)
        for a in recent_artists:
            print(f"  {a['rank']:>2}. {a['name']:<30} playcount: {a['playcount']}")

    finally:
        await client.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m tests.debug.show_full_top_artists <username>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
