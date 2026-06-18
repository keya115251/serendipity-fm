"""
End-to-end smoke test for the discovery walk: builds a real taste profile
for a Last.fm user, expands the walk from their top artists (plus any
detected outlier/secondary-cluster artists, see app/core/seeding.py),
scores candidates using the same validated tag-relevance logic as outlier
detection, and prints the top serendipitous recommendations.

Run with: python -m tests.test_discovery_walk <username> [target_hop_distance] [max_depth] [max_hops]
- target_hop_distance: scoring bias toward this depth (default 2)
- max_depth: hard cap on how deep a recommendation can come from, even if
  the walk explored further (default: no extra filtering)
- max_hops: how many hops the walk itself explores (default 3) - this was
  previously hardcoded and not actually configurable from outside the code
"""

import asyncio
import sys

from app.core.config import settings
from app.core.lastfm_client import LastFMClient
from app.core.taste_profile import TasteProfileBuilder
from app.core.tag_relevance import build_discounted_dominant_tags
from app.core.discovery_walk import DiscoveryWalk, diversify_top_n
from app.core.seeding import build_seeds
from app.db.artist_cache import ArtistDataCache


async def main(
    username: str,
    target_hop_distance: int = 2,
    max_depth: int | None = None,
    max_hops: int = 3,
):
    client = LastFMClient()
    profile_builder = TasteProfileBuilder(client)
    cache = ArtistDataCache(settings.database_path, client)
    walk = DiscoveryWalk(cache, similar_limit=20, max_hops=max_hops)

    try:
        print(f"Building taste profile for '{username}'...")
        profile = await profile_builder.build(username)

        known_artists = set(profile.long_term.artists) | set(profile.short_term.artists)
        print(f"Known artists ({len(known_artists)}): {sorted(known_artists)}\n")
        if profile.outlier_artists:
            print(f"Detected outlier artists (secondary clusters): {profile.outlier_artists}\n")

        # seed from top artists by playcount PLUS detected outliers, so a
        # real secondary taste cluster gets explored even if none of its
        # artists crack the top-N by raw playcount (see app/core/seeding.py)
        seeds = build_seeds(profile.long_term.artists, profile.outlier_artists)
        print(f"Seeding walk from: {seeds}\n")

        print(f"Expanding walk (max_hops={walk.max_hops})...")
        candidates = await walk.expand(seeds, known_artists)
        print(f"Discovered {len(candidates)} candidate artists not already in the user's profile.\n")

        # Build a per-SEED dominant tag profile, not one global profile.
        # Primary seeds (from the dominant cluster) use the user's overall
        # long-term tag profile, same as before. Each OUTLIER seed instead
        # gets its own individual tag profile (just that artist's own
        # Last.fm tags, weighted by Last.fm's own per-tag weight) - this is
        # what makes cluster-aware scoring possible: a candidate discovered
        # via an outlier seed gets judged against "does this fit that
        # outlier's own taste signature," not the global aggregate, which
        # would be dominated by the primary cluster and unfairly penalize
        # genuinely good secondary-cluster discoveries (see
        # DiscoveryWalk.score_candidates docstring for the real test case
        # that exposed this).
        async def artist_tag_set(artist: str):
            tags = await cache.get_artist_tags(artist, limit=8)
            return artist, {t["tag"] for t in tags}

        lt_tag_sets = dict(await asyncio.gather(*(artist_tag_set(a) for a in profile.long_term.artists)))
        global_dominant_tags = build_discounted_dominant_tags(profile.long_term.tag_weights, lt_tag_sets)

        dominant_tags_by_seed: dict[str, dict[str, float]] = {}
        primary_seeds = [s for s in seeds if s not in profile.outlier_artists]
        for s in primary_seeds:
            dominant_tags_by_seed[s] = global_dominant_tags

        outlier_seeds_in_play = [s for s in seeds if s in profile.outlier_artists]
        if outlier_seeds_in_play:
            async def outlier_own_tags(artist: str):
                tags = await cache.get_artist_tags(artist, limit=10)
                return artist, {t["tag"]: t["count"] / 100 for t in tags}

            outlier_tag_results = await asyncio.gather(*(outlier_own_tags(s) for s in outlier_seeds_in_play))
            for artist, tag_weights in outlier_tag_results:
                dominant_tags_by_seed[artist] = tag_weights

        print(f"Scoring candidates (target_hop_distance={target_hop_distance}, max_depth={max_depth})...")
        scored = await walk.score_candidates(
            candidates, dominant_tags_by_seed, target_hop_distance=target_hop_distance, max_depth=max_depth
        )

        # Weight each outlier seed's allocation share by how much CURRENT
        # signal it actually represents (12-month playcount relative to
        # the user's most-played artist), rather than treating every
        # detected outlier as equally "alive." Primary seeds always get
        # full weight (1.0) since they're already the dominant cluster by
        # definition.
        max_playcount = max(profile.long_term.artist_playcounts.values(), default=1)
        cluster_relevance_weights: dict[str, float] = {}
        for s in primary_seeds:
            cluster_relevance_weights[s] = 1.0
        for s in outlier_seeds_in_play:
            playcount = profile.long_term.artist_playcounts.get(s, 0)
            cluster_relevance_weights[s] = playcount / max_playcount if max_playcount else 0.0

        print(f"Cluster relevance weights: {cluster_relevance_weights}\n")

        final_recommendations = diversify_top_n(
            scored, n=7, max_per_cluster=3, cluster_relevance_weights=cluster_relevance_weights
        )

        print(f"\n--- Top 7 serendipitous recommendations (diversity-capped) ---")
        for i, c in enumerate(final_recommendations, 1):
            print(f"\n{i}. {c.artist}")
            print(f"   genres: {', '.join(c.tags) if c.tags else '(none)'}")
            print(f"   hop distance: {c.hop_distance}  |  listeners: {c.listeners:,}  |  score: {c.final_score:.4f}")
            print(f"   path: {' -> '.join(c.path)}")

    finally:
        await cache.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "Usage: python -m tests.test_discovery_walk <username> "
            "[target_hop_distance] [max_depth] [max_hops]"
        )
        sys.exit(1)
    hop = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    depth = int(sys.argv[3]) if len(sys.argv) > 3 else None
    hops = int(sys.argv[4]) if len(sys.argv) > 4 else 3
    asyncio.run(main(sys.argv[1], hop, depth, hops))


