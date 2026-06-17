"""
Manual smoke test for TasteProfileBuilder.
Run with: python -m tests.test_taste_profile <lastfm_username>
"""

import asyncio
import sys
from app.core.lastfm_client import LastFMClient
from app.core.taste_profile import TasteProfileBuilder


async def main(username: str):
    client = LastFMClient()
    builder = TasteProfileBuilder(client)
    try:
        profile = await builder.build(username)

        print(f"=== Taste profile for '{username}' ===\n")

        print("--- Long-term layer ---")
        print(f"Artists: {', '.join(profile.long_term.artists[:10])}")
        print("\nTop tags (by aggregate weight):")
        sorted_tags = sorted(profile.long_term.tag_weights.items(), key=lambda kv: kv[1], reverse=True)
        for tag, weight in sorted_tags[:15]:
            print(f"  {tag:<20} {weight:.2f}")

        print("\n--- Short-term layer (recent) ---")
        print(f"Artists: {', '.join(profile.short_term.artists[:10])}")
        print("\nTop tags (by aggregate weight):")
        sorted_tags_st = sorted(profile.short_term.tag_weights.items(), key=lambda kv: kv[1], reverse=True)
        for tag, weight in sorted_tags_st[:15]:
            print(f"  {tag:<20} {weight:.2f}")

        print("\n--- Detected outlier artists (long-term) ---")
        if profile.outlier_artists:
            for a in profile.outlier_artists:
                print(f"  {a}")
        else:
            print("  (none detected)")

    finally:
        await client.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m tests.test_taste_profile <lastfm_username>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))