"""
The discovery walk: expands outward from one or more seed artists through
Last.fm's similarity graph, several hops deep, and scores every newly
discovered candidate artist (one NOT already in the user's existing
profile) for serendipitous fit.

This is the core engine the whole project exists to validate: instead of
recommending directly-similar artists (hop 1, the "obvious" tier), it
deliberately looks further out, while still requiring candidates to
connect back to the user's actual tag profile, so the result is discovery
rather than noise.

Scoring formula per candidate:

    final_score = tag_relevance * hop_distance_factor * popularity_penalty

  - tag_relevance: how well the candidate's own tags match the user's
    dominant taste tags (reuses the same document-frequency-discounted
    dominant-tag-set logic validated in TasteProfileBuilder).
  - hop_distance_factor: peaks at the user's chosen target hop distance
    (the "moderate distance" slider), falls off on both sides - hop 1 is
    too obvious, very distant hops are usually irrelevant.
  - popularity_penalty: soft, continuous downweight based on global
    listener count, so mainstream artists aren't excluded outright but
    are deprioritized relative to less-mainstream candidates of similar
    relevance.
"""

import asyncio
import math
from dataclasses import dataclass, field

from app.db.artist_cache import ArtistDataCache
from app.core.tag_relevance import score_relevance_against_dominant_tags


@dataclass
class WalkCandidate:
    artist: str
    hop_distance: int
    tag_relevance: float = 0.0
    listeners: int = 0
    final_score: float = 0.0
    discovered_via: str = ""  # immediate parent - kept for backward compat / quick display
    tags: list[str] = field(default_factory=list)  # candidate's own top tags, for display/explainability
    path: list[str] = field(default_factory=list)  # full chain from seed to candidate, e.g. ["Bon Iver", "Phoebe Bridgers", "The Marías", "Not For Radio"]


class DiscoveryWalk:
    def __init__(
        self, cache: ArtistDataCache, similar_limit: int = 20, max_hops: int = 3, max_frontier_per_hop: int = 60
    ):
        self.cache = cache
        self.similar_limit = similar_limit
        self.max_hops = max_hops
        self.max_frontier_per_hop = max_frontier_per_hop

    async def expand(
        self,
        seed_artists: list[str],
        known_artists: set[str],
    ) -> dict[str, WalkCandidate]:
        """
        BFS-style expansion from seed_artists out to self.max_hops, fetching
        each hop level concurrently (capped by the cache/client's own
        semaphore underneath). known_artists (the user's existing profile)
        are tracked to avoid recommending something they already listen to,
        but are still traversed THROUGH if encountered, since they're valid
        bridges to further candidates even though they can't be final
        recommendations themselves.

        max_frontier_per_hop caps how many artists get expanded at each
        hop. This went through two earlier versions before landing here:

        1. Raw Last.fm match strength alone - let weak signal poison later
           hops. An artist with a strong match score to ONE seed could
           still be tangential to the user overall, and once in the
           frontier, its OWN similarity neighbors (unrelated to the user)
           became hop-2 candidates.

        2. Match strength combined with aggregate tag-relevance to the
           user's dominant taste - this didn't actually fix the problem in
           testing. The reason: an artist the user has only heard 2-3
           times (e.g. a folk/singer-songwriter act) can still score WELL
           on tag relevance, because tag relevance measures genre-fit to
           the aggregate profile, not whether the user has any real
           connection to that specific artist. Tag relevance and "is this
           a real bridge into the user's actual listening" are different
           questions; conflating them didn't change the results at all in
           a real before/after test.

        This version instead weights truncation by CONNECTIVITY TO THE
        USER'S KNOWN ARTIST SET: for each newly-found artist, fetch its own
        similar-artists list and check how much of it overlaps with
        known_artists (weighted by match strength), the same connectivity
        concept used in outlier detection (app/core/artist_graph.py), just
        applied here to candidates rather than the user's existing
        artists. An artist that's a genuine bridge into the user's real
        taste will show meaningful overlap with known_artists; one that's
        only tangentially linked via a single seed won't, regardless of
        how well its aggregate tags happen to match.
        """
        candidates: dict[str, WalkCandidate] = {}
        visited: set[str] = set(seed_artists)
        # paths tracks the full chain from a seed to every visited artist,
        # so candidates can store the complete reasoning trail (e.g.
        # ["Bon Iver", "Phoebe Bridgers", "The Marías", "Not For Radio"])
        # rather than just the immediate parent - this surfaced the
        # "Not For Radio" case as a thin, one-hop-removed side-credit
        # rather than a genuine multi-step discovery, which wasn't visible
        # from discovered_via alone.
        paths: dict[str, list[str]] = {seed: [seed] for seed in seed_artists}
        frontier = list(seed_artists)

        for hop in range(1, self.max_hops + 1):
            if not frontier:
                break

            async def fetch_for(artist: str):
                similar = await self.cache.get_similar_artists(artist, limit=self.similar_limit)
                return artist, similar

            results = await asyncio.gather(*(fetch_for(a) for a in frontier))

            newly_found: list[tuple[str, float, str]] = []  # (name, match, source_artist)
            for source_artist, similar_list in results:
                for s in similar_list:
                    name = s["name"]
                    if name in visited:
                        continue
                    visited.add(name)
                    newly_found.append((name, s["match"], source_artist))
                    paths[name] = paths[source_artist] + [name]

                    if name not in known_artists:
                        candidates[name] = WalkCandidate(
                            artist=name,
                            hop_distance=hop,
                            discovered_via=source_artist,
                            path=paths[name],
                        )

            if not newly_found:
                frontier = []
                continue

            # fetch each newly-found artist's OWN similar-artists list, to
            # measure connectivity back to the user's real known artists
            async def fetch_own_similar(name: str):
                own_similar = await self.cache.get_similar_artists(name, limit=self.similar_limit)
                return name, own_similar

            connectivity_results = await asyncio.gather(
                *(fetch_own_similar(name) for name, _, _ in newly_found)
            )

            scored_for_truncation = []
            match_by_name = {name: match for name, match, _ in newly_found}
            for name, own_similar in connectivity_results:
                connectivity = sum(s["match"] for s in own_similar if s["name"] in known_artists)
                match = match_by_name[name]
                # combine the original match strength (link quality to the
                # frontier artist) with connectivity to the user's broader
                # known set (is this actually part of the user's real
                # taste, not just adjacent to one seed)
                combined_score = match * (0.2 + connectivity)
                scored_for_truncation.append((name, combined_score))

            scored_for_truncation.sort(key=lambda x: x[1], reverse=True)
            frontier = [name for name, _ in scored_for_truncation[: self.max_frontier_per_hop]]

        return candidates

    async def score_candidates(
        self,
        candidates: dict[str, WalkCandidate],
        dominant_tags_by_seed: dict[str, dict[str, float]],
        target_hop_distance: int = 2,
        hop_sigma: float = 1.0,
        min_listeners: int = 5000,
        max_depth: int | None = None,
    ) -> list[WalkCandidate]:
        """
        Fetches tags + popularity for every candidate (concurrently, cache-
        backed) and computes final_score per the module-level formula.

        dominant_tags_by_seed maps each SEED artist to its own already-
        discounted dominant tag set (build each with
        app.core.tag_relevance.build_discounted_dominant_tags), and each
        candidate is scored against the entry for its OWN originating seed
        (candidate.path[0]), not a single global tag profile.

        This replaced a single shared discounted_dominant_tags parameter
        after real testing on a multi-cluster profile (prog/metal-dominant
        with a real secondary emo/alt-rock thread including My Chemical
        Romance) showed it doesn't work: seeding was already fixed to
        include MCR as an outlier seed, correctly exploring that secondary
        cluster, but every candidate - regardless of which seed it came
        from - was still scored against the SAME aggregate dominant tags,
        which were overwhelmingly prog/metal for that profile. Candidates
        discovered via the MCR branch were structurally penalized on
        tag_relevance even when they were a great fit for an MCR-adjacent
        listener, simply because "good fit for MCR" and "good fit for the
        global aggregate" are different questions, and only the second one
        was being asked. The result: the MCR seed had zero visible effect
        on final output, despite working correctly at the expansion stage.

        Each candidate now answers "does this fit the cluster it was
        actually discovered through," which is the question that matters
        for a multi-cluster listener.

        hop_distance_factor uses a gaussian centered on target_hop_distance,
        so hop distances near the target score near 1.0 and distances far
        from it fall off smoothly rather than via a hard cutoff - this
        avoids an awkward cliff at the boundary, and gives the eventual
        slider a continuous, predictable effect on results.

        min_listeners is a hard floor (default 5000): candidates below it
        are excluded entirely, not just penalized. This was added after
        real testing surfaced a result with 0 listeners at the very top of
        the list - the original unbounded 1/log(listeners) popularity
        penalty rewards obscurity without limit, so it was overpowering
        tag relevance for artists that are likely data artifacts, one-off
        side projects, or otherwise not real, reachable recommendations,
        rather than genuine "lesser-known but real" discoveries. Beyond
        the floor, the popularity factor is also now bounded (clamped to a
        reasonable max) rather than growing without limit as listener count
        shrinks, so two candidates both comfortably above the floor are
        compared mostly on tag fit and hop distance, not on a popularity
        race to the bottom.

        Candidates with no MusicBrainz ID (mbid) are also excluded
        entirely - real testing against Last.fm's actual artist.getInfo
        responses showed this reliably distinguishes legitimate catalogued
        artists from one-off collaboration/compilation credits bundled as
        a single Last.fm "artist" entry: "Hayley Williams" (real
        independent solo discography after Paramore - a legitimate
        recommendation) has a real mbid, while "Selena Gomez, benny blanco
        & The Marías" (a single track's credited artists bundled together)
        and "Jeff Buckley & Gary Lucas" (a one-off collaboration credit,
        not Jeff Buckley's actual discography) both have none.

        max_depth, if given, filters out candidates discovered beyond that
        many hops - e.g. max_depth=2 only allows candidates reached within
        2 steps from a seed, even though expand() may have walked further
        and discovered deeper candidates too. This lets a person choose
        "stop the chain here" directly, rather than only being able to
        bias scoring toward a target depth via target_hop_distance while
        still allowing deeper candidates through. None (default) applies
        no depth filtering beyond whatever expand() already walked.

        POPULARITY NORMALIZATION IS PER-CLUSTER, not global. Real testing
        on a multi-cluster profile (prog/metal-dominant with a real
        secondary emo/pop-punk cluster via My Chemical Romance) exposed
        why a single global popularity formula doesn't work even after
        cluster-aware TAG scoring was fixed: emo/pop-punk as a genre simply
        has more mainstream-popular acts (Fall Out Boy, Green Day, Jimmy
        Eat World - millions of listeners) than underground prog/metal
        does, so emo-branch candidates were systematically penalized on
        popularity_factor relative to prog-branch candidates, REGARDLESS
        of how well they fit their own cluster. A candidate that's a
        genuinely deep cut FOR EMO (e.g. Bayside at ~576K listeners) was
        losing to a prog candidate at ~9K listeners, not because it was a
        worse discovery, but because absolute listener count doesn't mean
        the same thing across genres with different overall popularity
        ecosystems. Popularity is now scored as a PERCENTILE RANK within
        the candidate's own cluster (same principle as the percentile-rank
        fix applied to graph connectivity in app/core/artist_graph.py),
        so each cluster's own most-obscure-to-most-mainstream spread is
        what determines relative ranking, not a shared absolute scale.
        """
        if not dominant_tags_by_seed:
            return []

        async def fetch_candidate_data(artist: str):
            tags, info = await asyncio.gather(
                self.cache.get_artist_tags(artist, limit=10),
                self.cache.get_artist_info(artist),
            )
            return artist, tags, info

        eligible = (
            candidates
            if max_depth is None
            else {name: c for name, c in candidates.items() if c.hop_distance <= max_depth}
        )

        results = await asyncio.gather(*(fetch_candidate_data(name) for name in eligible))

        # PASS 1: apply hard filters (listener floor, mbid) and group
        # survivors by origin cluster, so we can compute each cluster's own
        # listener-count distribution before scoring anyone
        survivors = []  # (artist, tags, info)
        for artist, tags, info in results:
            if info["listeners"] < min_listeners:
                continue
            if not info.get("mbid"):
                continue
            survivors.append((artist, tags, info))

        listeners_by_cluster: dict[str, list[int]] = {}
        for artist, tags, info in survivors:
            candidate = eligible[artist]
            origin_seed = candidate.path[0] if candidate.path else None
            listeners_by_cluster.setdefault(origin_seed, []).append(info["listeners"])

        def popularity_percentile(listeners: int, cluster_listeners: list[int]) -> float:
            # fraction of OTHER cluster members this candidate is more
            # obscure than (lower listener count = more obscure = higher
            # percentile here, since we want LESS mainstream to score
            # HIGHER, consistent with the original popularity_factor intent)
            n = len(cluster_listeners)
            if n <= 1:
                return 1.0
            more_mainstream_count = sum(1 for l in cluster_listeners if l > listeners)
            return more_mainstream_count / (n - 1)

        # PASS 2: score using the cluster-relative popularity percentile
        scored = []
        for artist, tags, info in survivors:
            candidate = eligible[artist]
            candidate.listeners = info["listeners"]
            candidate.tags = [t["tag"] for t in tags]

            origin_seed = candidate.path[0] if candidate.path else None
            cluster_dominant_tags = dominant_tags_by_seed.get(origin_seed, {})
            if not cluster_dominant_tags:
                # fall back to whichever cluster's tags we DO have, rather
                # than silently scoring 0 - shouldn't normally happen since
                # every seed should have an entry, but a missing mapping
                # is a real bug worth not masking entirely
                cluster_dominant_tags = next(iter(dominant_tags_by_seed.values()), {})

            candidate.tag_relevance = score_relevance_against_dominant_tags(tags, cluster_dominant_tags)

            hop_distance_factor = math.exp(
                -((candidate.hop_distance - target_hop_distance) ** 2) / (2 * hop_sigma**2)
            )

            cluster_listeners = listeners_by_cluster.get(origin_seed, [info["listeners"]])
            raw_popularity_percentile = popularity_percentile(info["listeners"], cluster_listeners)

            # floor + dampen: a raw percentile of 0.0 (most-mainstream
            # member of its own cluster) previously zeroed out the WHOLE
            # score regardless of tag fit - real testing showed a strong
            # cluster fit (Bayside, tag_relevance=0.69) losing to a much
            # weaker absolute fit elsewhere purely because it happened to
            # be the most mainstream artist in its specific cluster
            # sample. A floor (0.3) means popularity can never fully
            # override tag fit, and the **0.5 (square root) dampens the
            # remaining spread so popularity differentiates similar-fit
            # candidates without dominating the comparison the way a
            # linear or harsher term did.
            floored_percentile = 0.3 + 0.7 * raw_popularity_percentile
            popularity_factor = floored_percentile**0.5

            candidate.final_score = candidate.tag_relevance * hop_distance_factor * popularity_factor
            scored.append(candidate)

        return sorted(scored, key=lambda c: c.final_score, reverse=True)


def diversify_top_n(
    scored_candidates: list[WalkCandidate],
    n: int = 7,
    max_per_cluster: int = 3,
    guarantee_min_one_per_cluster: bool = True,
    cluster_relevance_weights: dict[str, float] | None = None,
    min_relevance_for_guarantee: float = 0.1,
) -> list[WalkCandidate]:
    """
    Selects the final top-N recommendations with a cap on how many can
    come from any single seed cluster (candidate.path[0]), optionally
    guaranteeing every cluster with at least one viable candidate a slot.

    cluster_relevance_weights, if given, maps each seed/cluster to a 0-1
    relevance weight reflecting how much CURRENT signal that cluster
    actually represents in the user's listening (e.g. an outlier's
    12-month playcount relative to the user's most-played artist). Each
    cluster's effective max slot count is max_per_cluster scaled by its
    weight (rounded, minimum 1 if guaranteed), and a cluster below
    min_relevance_for_guarantee does NOT get the guaranteed slot at all,
    even though it's still eligible to earn slots on pure score merit.

    This exists because treating "detected as an outlier" as a flat
    yes/no with equal downstream rights doesn't match reality: real
    testing found a user whose outlier-detected K-pop cluster was almost
    entirely historical (787 all-time plays, only 122 in the last 12
    months - the same stale-history pattern that motivated using 12month
    instead of overall for the long-term taste layer in the first place),
    yet it received the same guaranteed slot and max-3 ceiling as a
    cluster the user still actively listens to. Two K-pop recommendations
    out of 7 felt wrong to the user specifically because that cluster's
    real current relevance is low, even though it cleared the binary
    outlier-detection bar. Weighting allocation by actual recent signal
    fixes this without re-litigating outlier detection itself - an artist
    can still BE a real secondary cluster while having a smaller, more
    proportionate claim on the final output.

    Without ANY of this (cap, guarantee, or weighting), a flat sort by
    final_score can let one cluster dominate the entire output regardless
    of whether it's the primary cluster or an outlier - real testing
    showed both a secondary cluster getting zero representation, and
    later, after fixing cluster-aware scoring and per-cluster popularity
    normalization, an outlier cluster completely dominating instead (4 of
    5 top results were K-pop for a listener whose primary cluster was
    alt/indie). The cap and guarantee resolved that, but didn't address
    the separate problem of an outlier's allocation being disproportionate
    to how alive that cluster actually still is - hence this weighting.
    """
    cluster_relevance_weights = cluster_relevance_weights or {}

    def effective_max(cluster: str) -> int:
        weight = cluster_relevance_weights.get(cluster, 1.0)
        scaled = round(max_per_cluster * weight)
        return max(scaled, 1 if weight > 0 else 0)

    def eligible_for_guarantee(cluster: str) -> bool:
        weight = cluster_relevance_weights.get(cluster, 1.0)
        return weight >= min_relevance_for_guarantee

    by_cluster: dict[str, list[WalkCandidate]] = {}
    for candidate in scored_candidates:
        origin_seed = candidate.path[0] if candidate.path else None
        by_cluster.setdefault(origin_seed, []).append(candidate)

    selected: list[WalkCandidate] = []
    cluster_counts: dict[str, int] = {}
    already_selected_ids = set()

    if guarantee_min_one_per_cluster:
        # reserve each ELIGIBLE cluster's single best candidate first, in
        # order of that candidate's own score - low-relevance clusters
        # (e.g. an almost-entirely-historical outlier) don't get this
        # guarantee, though they can still earn slots on pure merit below
        guaranteed = sorted(
            (
                candidates[0]
                for cluster, candidates in by_cluster.items()
                if candidates and eligible_for_guarantee(cluster)
            ),
            key=lambda c: c.final_score,
            reverse=True,
        )
        for candidate in guaranteed:
            if len(selected) >= n:
                break
            origin_seed = candidate.path[0] if candidate.path else None
            selected.append(candidate)
            already_selected_ids.add(id(candidate))
            cluster_counts[origin_seed] = cluster_counts.get(origin_seed, 0) + 1

    # fill remaining slots greedily by overall score, respecting each
    # cluster's OWN effective max (not a single shared max_per_cluster)
    for candidate in scored_candidates:
        if len(selected) >= n:
            break
        if id(candidate) in already_selected_ids:
            continue
        origin_seed = candidate.path[0] if candidate.path else None
        count = cluster_counts.get(origin_seed, 0)
        if count >= effective_max(origin_seed):
            continue
        selected.append(candidate)
        cluster_counts[origin_seed] = count + 1

    # re-sort the final selection by score, since the guarantee pass above
    # may have placed a lower-scoring guaranteed pick ahead of higher-
    # scoring candidates that got bumped past the cap
    return sorted(selected, key=lambda c: c.final_score, reverse=True)
