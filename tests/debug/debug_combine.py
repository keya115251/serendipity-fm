"""
Direct diagnostic: run the exact same code path as TasteProfileBuilder.build(),
but print the intermediate tag_relevance and connectivity dicts before they
get combined, so we can see exactly why known outliers aren't being flagged.

Run with: python -m tests.debug.debug_combine <username>
"""

import asyncio
import sys
from app.core.lastfm_client import LastFMClient, LastFMError
from app.core.taste_profile import TasteProfileBuilder


async def main(username: str):
    client = LastFMClient()
    builder = TasteProfileBuilder(client)
    try:
        top_artists = await client.get_top_artists(username, period="12month", limit=25)
        long_term_playcounts = {a["name"]: a["playcount"] for a in top_artists}

        long_term_tags = await builder._tag_weights_for_artists(long_term_playcounts)

        async def artist_tag_set(artist: str):
            try:
                tags = await client.get_artist_top_tags(artist, limit=8)
                return artist, {t["tag"] for t in tags}
            except LastFMError:
                return artist, set()

        lt_tag_sets = dict(await asyncio.gather(*(artist_tag_set(a) for a in long_term_playcounts)))

        graph_result = await builder.graph_builder.build_profile_graph(list(long_term_playcounts.keys()))

        tag_relevance = builder._tag_relevance_scores(long_term_tags, lt_tag_sets)
        connectivity = graph_result.connectivity_scores

        print(f"tag_relevance keys ({len(tag_relevance)}): {sorted(tag_relevance.keys())}")
        print()
        print(f"connectivity keys ({len(connectivity)}): {sorted(connectivity.keys())}")
        print()

        print(f"{'Artist':<25} {'tag_relevance':<18} {'connectivity':<15} {'would flag?'}")
        all_artists = set(tag_relevance) | set(connectivity)
        for artist in sorted(all_artists):
            t = tag_relevance.get(artist, 0.0)
            c = connectivity.get(artist, 0.0)
            flag = "YES" if (t < 0.05 and c < 0.1) else "no"
            print(f"{artist:<25} {t:<18.4f} {c:<15.4f} {flag}")

        outliers = builder._combine_outlier_signals(tag_relevance, connectivity)
        print(f"\nActual _combine_outlier_signals() output: {outliers}")

    finally:
        await client.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m tests.debug.debug_combine <username>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
