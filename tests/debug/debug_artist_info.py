"""
Diagnostic: print the FULL raw artist.getInfo response for known
collaboration/alias entries vs known legitimate solo artists, to find a
real, data-grounded signal for filtering collaboration credits rather
than guessing at a string-pattern heuristic.

Run with: python -m tests.debug.debug_artist_info
"""

import asyncio
import json

from app.core.lastfm_client import LastFMClient

# known problem cases (collaboration/compilation credits) vs known-good
# solo artist transitions (legitimate independent discography)
COLLAB_CASES = ["Jeff Buckley & Gary Lucas", "Selena Gomez, benny blanco & The Marías"]
SOLO_CASES = ["Hayley Williams", "Amy Lee"]


async def main():
    client = LastFMClient()
    try:
        for label, names in [("COLLABORATION CASES", COLLAB_CASES), ("LEGITIMATE SOLO CASES", SOLO_CASES)]:
            print(f"\n========== {label} ==========")
            for name in names:
                data = await client._call("artist.getinfo", artist=name, autocorrect=1)
                artist_info = data.get("artist", {})
                print(f"\n--- {name} ---")
                print(f"  name (as returned): {artist_info.get('name')}")
                print(f"  mbid: {artist_info.get('mbid')}")
                print(f"  ontour: {artist_info.get('ontour')}")
                bio = artist_info.get("bio", {})
                summary = bio.get("summary", "")
                print(f"  bio summary length: {len(summary)} chars")
                print(f"  bio summary (first 300 chars): {summary[:300]}")
                similar = artist_info.get("similar", {}).get("artist", [])
                print(f"  similar artists listed in getInfo response: {len(similar)}")
                tags = artist_info.get("tags", {}).get("tag", [])
                print(f"  tags listed in getInfo response: {[t.get('name') for t in tags]}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
