"""
Smoke test for rediscover mode against LIVE Last.fm data.

Run with: python -m tests.test_rediscover <username>
"""

import asyncio
import sys

from app.core.lastfm_client import LastFMClient
from app.core.rediscover import find_old_phases


async def main(username: str):
    client = LastFMClient()
    try:
        old_phases = await find_old_phases(username, client)

        print(f"--- Old phases for '{username}' ---")
        if not old_phases:
            print("  (none found - either a stable taste, or thresholds need tuning)")
        for a in old_phases:
            print(
                f"  {a['artist']:<30} overall: {a['overall_playcount']:<6} "
                f"recent: {a['recent_playcount']:<6} ratio: {a['recent_ratio']:.3f}"
            )
    finally:
        await client.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m tests.test_rediscover <username>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
