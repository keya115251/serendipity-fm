"""
Quick manual smoke test for the Last.fm client.
Run with: python -m tests.test_lastfm_client <lastfm_username>
"""

import asyncio
import sys
from app.core.lastfm_client import LastFMClient, LastFMError


async def main(username: str):
    client = LastFMClient()
    try:
        print(f"--- Top artists for '{username}' ---")
        top_artists = await client.get_top_artists(username, period="overall", limit=10)
        for a in top_artists:
            print(f"  {a['rank']:>2}. {a['name']} (playcount: {a['playcount']})")

        if not top_artists:
            print("  (no top artists returned)")
            return

        print(f"\n--- Top tracks for '{username}' ---")
        top_tracks = await client.get_top_tracks(username, period="overall", limit=15)
        for t in top_tracks:
            print(f"  {t['rank']:>2}. {t['artist']} - {t['track']} (playcount: {t['playcount']})")

        print(f"\n--- Recent tracks for '{username}' ---")
        recent = await client.get_recent_tracks(username, limit=15)
        for r in recent:
            print(f"  {r['artist']} - {r['track']}")

        first_artist = top_artists[0]["name"]
        first_artist = top_artists[0]["name"]
        print(f"\n--- Artists similar to '{first_artist}' ---")
        similar = await client.get_similar_artists(first_artist, limit=10)
        for s in similar:
            print(f"  {s['name']} (match: {s['match']:.2f})")

        print(f"\n--- Top tags for '{first_artist}' ---")
        tags = await client.get_artist_top_tags(first_artist, limit=10)
        for t in tags:
            print(f"  {t['tag']} (weight: {t['count']})")

        print(f"\n--- Artist info for '{first_artist}' ---")
        info = await client.get_artist_info(first_artist)
        print(f"  listeners: {info['listeners']:,}  |  playcount: {info['playcount']:,}")

    except LastFMError as e:
        print(f"Last.fm API error (code {e.code}): {e}")
    finally:
        await client.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m tests.test_lastfm_client <lastfm_username>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
