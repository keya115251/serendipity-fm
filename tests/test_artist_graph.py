"""
Smoke test for ArtistGraphBuilder - prints each artist's connectivity
score within the user's own profile graph.

Run with: python -m tests.test_artist_graph <username>
"""

import asyncio
import sys
from app.core.lastfm_client import LastFMClient
from app.core.artist_graph import ArtistGraphBuilder


async def main(username: str):
    client = LastFMClient()
    graph_builder = ArtistGraphBuilder(client)
    try:
        top_artists = await client.get_top_artists(username, period="12month", limit=25)
        artist_names = [a["name"] for a in top_artists]

        result = await graph_builder.build_profile_graph(artist_names)

        print(f"Graph: {result.graph.number_of_nodes()} nodes, {result.graph.number_of_edges()} edges\n")

        print(f"{'Artist':<25} {'connectivity_score':<20} {'edges within profile'}")
        sorted_scores = sorted(result.connectivity_scores.items(), key=lambda kv: kv[1], reverse=True)
        for artist, score in sorted_scores:
            neighbors = list(result.graph.neighbors(artist)) if artist in result.graph else []
            print(f"{artist:<25} {score:<20.4f} {neighbors}")

    finally:
        await client.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m tests.test_artist_graph <username>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
