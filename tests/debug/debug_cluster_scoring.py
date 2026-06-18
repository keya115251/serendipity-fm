"""
Diagnostic: check what happened to candidates discovered via an outlier
seed (e.g. My Chemical Romance), specifically whether cluster-aware
scoring is actually being applied to them and what their resulting scores
look like, to find out why a structural scoring change produced no
visible effect on final output.

Run with: python -m tests.debug.debug_cluster_scoring <username> <outlier_seed_name>
"""

import asyncio
import sys

from app.core.config import settings
from app.core.lastfm_client import LastFMClient
from app.core.taste_profile import TasteProfileBuilder
from app.core.tag_relevance import build_discounted_dominant_tags
from app.core.discovery_walk import DiscoveryWalk
from app.core.seeding import build_seeds
from app.db.artist_cache import ArtistDataCache


async def main(username: str, outlier_seed: str):
    client = LastFMClient()
    profile_builder = TasteProfileBuilder(client)
    cache = ArtistDataCache(settings.database_path, client)
    walk = DiscoveryWalk(cache, similar_limit=20, max_hops=3)

    try:
        profile = await profile_builder.build(username)
        known_artists = set(profile.long_term.artists) | set(profile.short_term.artists)
        print(f"Detected outliers: {profile.outlier_artists}")

        seeds = build_seeds(profile.long_term.artists, profile.outlier_artists)
        print(f"Seeds: {seeds}\n")

        if outlier_seed not in seeds:
            print(f"WARNING: '{outlier_seed}' is not actually in the seed list. Stopping here.")
            return

        candidates = await walk.expand(seeds, known_artists)

        # find every candidate whose path actually originates from the outlier seed
        via_outlier = {name: c for name, c in candidates.items() if c.path and c.path[0] == outlier_seed}
        print(f"Total candidates: {len(candidates)}")
        print(f"Candidates whose path[0] == '{outlier_seed}': {len(via_outlier)}")
        for name, c in list(via_outlier.items())[:15]:
            print(f"  {name}  (hop {c.hop_distance}, path: {' -> '.join(c.path)})")

        if not via_outlier:
            print(f"\nNo candidates trace back to '{outlier_seed}' at all - the issue is in expand()/path tracking, not scoring.")
            return

        async def artist_tag_set(artist: str):
            tags = await cache.get_artist_tags(artist, limit=8)
            return artist, {t["tag"] for t in tags}

        lt_tag_sets = dict(await asyncio.gather(*(artist_tag_set(a) for a in profile.long_term.artists)))
        global_dominant_tags = build_discounted_dominant_tags(profile.long_term.tag_weights, lt_tag_sets)

        outlier_tags_raw = await cache.get_artist_tags(outlier_seed, limit=10)
        outlier_dominant_tags = {t["tag"]: t["count"] / 100 for t in outlier_tags_raw}

        dominant_tags_by_seed = {s: global_dominant_tags for s in seeds if s not in profile.outlier_artists}
        dominant_tags_by_seed[outlier_seed] = outlier_dominant_tags

        print(f"\n'{outlier_seed}' own tag profile used for scoring: {outlier_dominant_tags}\n")

        scored = await walk.score_candidates(candidates, dominant_tags_by_seed, target_hop_distance=2)

        print(f"Top 5 scored candidates overall (for comparison):")
        for c in scored[:5]:
            print(f"  {c.artist}: score={c.final_score:.4f}  path={' -> '.join(c.path)}")

        scored_via_outlier = [c for c in scored if c.path and c.path[0] == outlier_seed]
        print(f"\nAll scored candidates via '{outlier_seed}' ({len(scored_via_outlier)} survived filters):")
        for c in sorted(scored_via_outlier, key=lambda c: c.final_score, reverse=True)[:10]:
            print(f"  {c.artist}: tag_relevance={c.tag_relevance:.4f}  final_score={c.final_score:.4f}  listeners={c.listeners:,}")

    finally:
        await cache.close()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python -m tests.debug.debug_cluster_scoring <username> <outlier_seed_name>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
