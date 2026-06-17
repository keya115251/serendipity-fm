"""
Debug script: run the REAL TasteProfileBuilder pipeline using the current
weight-magnitude outlier logic, and print the full diagnostic trace -
dominant set, dominant weight total, and every artist's relevance score -
so we can see exactly which number is wrong rather than guessing.

Run with: python -m tests.debug.debug_txt_tags <username>
"""

import asyncio
import sys
from app.core.lastfm_client import LastFMClient
from app.core.taste_profile import TasteProfileBuilder


async def main(username: str):
    client = LastFMClient()
    builder = TasteProfileBuilder(client)
    try:
        top_artists = await client.get_top_artists(username, period="12month", limit=25)
        long_term_playcounts = {a["name"]: a["playcount"] for a in top_artists}

        long_term_tags = await builder._tag_weights_for_artists(long_term_playcounts)

        max_weight = max(long_term_tags.values())
        threshold = 0.35
        dominant_tags = {t: w for t, w in long_term_tags.items() if w >= max_weight * threshold}
        dominant_weight_total = sum(dominant_tags.values())

        print(f"max_weight (top tag): {max_weight:.3f}")
        print(f"weight cutoff (max_weight * {threshold}): {max_weight * threshold:.3f}")
        print(f"dominant_tags ({len(dominant_tags)}): {dominant_tags}")
        print(f"dominant_weight_total: {dominant_weight_total:.3f}")
        print()

        async def artist_tag_set(artist: str):
            tags = await client.get_artist_top_tags(artist, limit=8)
            return artist, {t["tag"] for t in tags}

        results = await asyncio.gather(*(artist_tag_set(a) for a in long_term_playcounts))

        print(f"{'Artist':<25} {'tags matched':<40} {'matched_wt':<12} {'relevance_score'}")
        for artist, tags in results:
            matched = {t: dominant_tags[t] for t in tags if t in dominant_tags}
            matched_weight = sum(matched.values())
            relevance = matched_weight / dominant_weight_total if dominant_weight_total else 0
            flag = "  <-- OUTLIER" if relevance < 0.05 else ""
            print(f"{artist:<25} {str(list(matched.keys())):<40} {matched_weight:<12.3f} {relevance:.4f}{flag}")

    finally:
        await client.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m tests.debug.debug_txt_tags <username>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))



if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m tests.debug.debug_txt_tags <username>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))



if __name__ == "__main__":
    asyncio.run(main())



if __name__ == "__main__":
    asyncio.run(main())
