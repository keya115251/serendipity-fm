"""
End-to-end smoke test for the discovery walk: builds a real taste profile
for a Last.fm user, expands the walk from their top artists (plus any
detected outlier/secondary-cluster artists, see app/core/seeding.py),
scores candidates using the same validated tag-relevance logic as outlier
detection, and prints the top serendipitous recommendations.

Run with: python -m tests.test_discovery_walk <username> [target_hop_distance] [max_depth] [max_hops] [niche_level]
- target_hop_distance: scoring bias toward this depth (default 2)
- max_depth: hard cap on how deep a recommendation can come from, even if
  the walk explored further (default: no extra filtering)
- max_hops: how many hops the walk itself explores (default 3) - this was
  previously hardcoded and not actually configurable from outside the code
- niche_level: 0.0-1.0, how obscure results should skew (default 0.5,
  the original validated value - see DiscoveryWalk.score_candidates'
  docstring for the mapping and why only the default is fully validated)
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
from app.db.feedback_store import FeedbackStore


async def main(
    username: str,
    target_hop_distance: int = 2,
    max_depth: int | None = None,
    max_hops: int = 3,
    niche_level: float = 0.5,
):
    client = LastFMClient()
    profile_builder = TasteProfileBuilder(client)
    cache = ArtistDataCache(settings.database_path, client)
    walk = DiscoveryWalk(cache, similar_limit=20, max_hops=max_hops)
    feedback_store = FeedbackStore(settings.database_path)

    try:
        print(f"Building taste profile for '{username}'...")
        profile = await profile_builder.build(username)

        known_artists = set(profile.long_term.artists) | set(profile.short_term.artists)

        # only DISLIKED artists are fully excluded - the user has heard
        # them and didn't like them, so they shouldn't be recommended
        # again at all. LIKED artists stay eligible to be recommended
        # again (down-weighted in scoring below, not excluded) - if a
        # liked artist resurfaces, the listener may want a DIFFERENT
        # album from them than whatever they already rated, which is
        # exactly what the album-feedback boost in
        # attach_entry_point_albums is for. Excluding liked artists
        # entirely would make that boost permanently unreachable.
        disliked_artists = feedback_store.get_disliked_artists(username)
        liked_artists = feedback_store.get_liked_artists(username)
        if disliked_artists:
            print(f"Excluding {len(disliked_artists)} previously-disliked artist(s): {sorted(disliked_artists)}\n")
        known_artists |= disliked_artists

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
        # fold liked artists' tags into the dominant tag profile with a
        # modest weight, separately from the down-weight applied during
        # scoring below (see score_candidates' previously_liked_artists
        # parameter). The two mechanisms do different things: the
        # down-weight stops a liked artist from outranking genuinely new
        # discoveries if it resurfaces as its own candidate, while THIS
        # tag-fold shapes what OTHER, different candidates look
        # appealing - "more like what you already liked," independent of
        # whether the liked artist itself happens to resurface.
        # liked_artists was already fetched above for the down-weight.
        long_term_tag_weights = dict(profile.long_term.tag_weights)
        lt_tag_sets_input = dict()

        async def artist_tag_set(artist: str):
            tags = await cache.get_artist_tags(artist, limit=8)
            return artist, {t["tag"] for t in tags}, tags

        base_results = await asyncio.gather(*(artist_tag_set(a) for a in profile.long_term.artists))
        for artist, tag_set, _ in base_results:
            lt_tag_sets_input[artist] = tag_set

        if liked_artists:
            print(f"Folding in {len(liked_artists)} previously-liked artist(s) into taste profile: {sorted(liked_artists)}\n")
            LIKED_ARTIST_WEIGHT = 0.3  # modest relative to a real listening-driven weight
            liked_results = await asyncio.gather(*(artist_tag_set(a) for a in liked_artists))
            for artist, tag_set, tags in liked_results:
                lt_tag_sets_input[artist] = tag_set
                for t in tags:
                    long_term_tag_weights[t["tag"]] = long_term_tag_weights.get(t["tag"], 0.0) + (
                        LIKED_ARTIST_WEIGHT * (t["count"] / 100)
                    )

        global_dominant_tags = build_discounted_dominant_tags(long_term_tag_weights, lt_tag_sets_input)

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

        print(f"Scoring candidates (target_hop_distance={target_hop_distance}, max_depth={max_depth}, niche_level={niche_level})...")
        scored = await walk.score_candidates(
            candidates,
            dominant_tags_by_seed,
            target_hop_distance=target_hop_distance,
            max_depth=max_depth,
            previously_liked_artists=liked_artists,
            niche_level=niche_level,
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

        print("Fetching entry-point albums for final recommendations...")
        album_feedback = feedback_store.get_album_feedback(username)
        final_recommendations = await walk.attach_entry_point_albums(
            final_recommendations, dominant_tags_by_seed, album_feedback
        )

        print(f"\n--- Top 7 serendipitous recommendations (diversity-capped) ---")
        for i, c in enumerate(final_recommendations, 1):
            print(f"\n{i}. {c.artist}")
            print(f"   start here: {c.entry_point_album or '(no album data found)'}")
            print(f"   genres: {', '.join(c.tags) if c.tags else '(none)'}")
            print(f"   hop distance: {c.hop_distance}  |  listeners: {c.listeners:,}  |  score: {c.final_score:.4f}")
            print(f"   path: {' -> '.join(c.path)}")

    finally:
        await cache.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "Usage: python -m tests.test_discovery_walk <username> "
            "[target_hop_distance] [max_depth] [max_hops] [niche_level]\n"
            "Pass the literal word None for max_depth to explicitly skip it "
            "while still setting max_hops/niche_level, e.g.:\n"
            "  python -m tests.test_discovery_walk myuser 2 None 3 0.0"
        )
        sys.exit(1)

    def parse_optional_int(value: str) -> int | None:
        return None if value.lower() == "none" else int(value)

    hop = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    depth = parse_optional_int(sys.argv[3]) if len(sys.argv) > 3 else None
    hops = int(sys.argv[4]) if len(sys.argv) > 4 else 3
    niche = float(sys.argv[5]) if len(sys.argv) > 5 else 0.5
    asyncio.run(main(sys.argv[1], hop, depth, hops, niche))


