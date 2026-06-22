"""
No-account discovery walk: given up to 7 artist names directly (no
Last.fm username, no listening history), runs the SAME validated
discovery walk machinery (app/core/discovery_walk.py) to produce real
serendipitous recommendations.

This reuses the walk rather than building a separate, simpler mechanism
because the walk's machinery - alias/collaboration-credit filtering,
cluster-aware scoring, diversity capping, the niche-ness slider, entry-
point albums - is already validated against real data through a long
debugging history (see README). A from-scratch simpler mechanism would
need its own debugging cycle for problems the walk has already solved.

One real structural difference from the personalized walk: there is no
outlier-detection step here. Outlier detection is built from a
LISTENING HISTORY (12-month vs overall playcount divergence), which
doesn't exist for freely-typed artist names - there's no "secondary
cluster within someone's taste" to detect, because all 7 inputs are
already explicit, equally-weighted seeds by definition. Every input
artist is treated the way an OUTLIER seed is treated in the personalized
walk: its own individual tags become its dominant_tags_by_seed entry,
not a shared/aggregated global profile - there's no broader listening
history to aggregate from here, so there's no equivalent of a "primary"
seed at all. All 7 are equal-status seeds.
"""

from app.core.discovery_walk import DiscoveryWalk, diversify_top_n
from app.db.artist_cache import ArtistDataCache
import asyncio


async def recommend_from_artists(
    artist_names: list[str],
    cache: ArtistDataCache,
    n: int = 7,
    max_per_cluster: int | None = None,
    target_hop_distance: int = 2,
    max_depth: int | None = None,
    max_hops: int = 3,
    niche_level: float = 0.5,
    attach_albums: bool = True,
) -> list:
    """
    artist_names: up to 7 artist names provided directly by the user
    (no Last.fm account needed). More than 7 is accepted but not
    recommended - the walk's candidate pool grows with each additional
    seed, and 7 was chosen to match the existing top-7 output convention
    used everywhere else in this project, not as a hard technical limit.

    max_per_cluster, if None (default), is computed automatically as
    ceil(n / number_of_seeds_given), with a floor of 2. This exists
    because a FIXED cap doesn't scale down with fewer seeds: real testing
    with only 3 input artists and a fixed max_per_cluster=2 structurally
    capped total output at 3*2=6, one short of the target n=7, even
    though every seed had more than 2 viable candidates available. With
    fewer seeds, each one needs to be ALLOWED more slots to reliably
    reach n; with more seeds (e.g. the full 7), the auto-computed cap
    naturally tightens back down, since ceil(7/7)=1, though the floor of
    2 means it won't go below that even at the full seed count - this
    floor hasn't been validated against real 7-seed output yet and may
    need revisiting once tested.

    Returns the diversity-capped list of WalkCandidate objects (see
    app/core/discovery_walk.py), with entry_point_album populated if
    attach_albums=True.

    Deliberately does NOT pass cluster_relevance_weights to
    diversify_top_n (unlike the personalized walk, which scales an
    outlier's slot allocation by 12-month-vs-overall playcount ratio) -
    that concept depends on listening-history data that doesn't exist
    for freely-typed artist names. All 7 inputs are equally "alive" by
    definition here, so they get equal treatment in the diversity cap.
    """
    if not artist_names:
        return []

    seeds = artist_names[:7]

    if max_per_cluster is None:
        import math

        max_per_cluster = max(2, math.ceil(n / len(seeds)))

    known_artists = set(seeds)  # the only "known" artists here are what the user told us directly

    walk = DiscoveryWalk(cache, similar_limit=20, max_hops=max_hops)
    candidates = await walk.expand(seeds, known_artists)

    # every seed is treated as an "outlier"-style seed: its own
    # individual tags, not a shared aggregate - there's no listening
    # history to aggregate FROM here, so there's no equivalent of a
    # "primary" seed the way the personalized walk has one
    async def seed_tags(artist: str) -> tuple[str, dict[str, float]]:
        tags = await cache.get_artist_tags(artist, limit=10)
        return artist, {t["tag"]: t["count"] / 100 for t in tags}

    dominant_tags_by_seed = dict(await asyncio.gather(*(seed_tags(s) for s in seeds)))

    scored = await walk.score_candidates(
        candidates,
        dominant_tags_by_seed,
        target_hop_distance=target_hop_distance,
        max_depth=max_depth,
        niche_level=niche_level,
    )

    final = diversify_top_n(scored, n=n, max_per_cluster=max_per_cluster)

    if attach_albums:
        final = await walk.attach_entry_point_albums(final, dominant_tags_by_seed)

    return final
