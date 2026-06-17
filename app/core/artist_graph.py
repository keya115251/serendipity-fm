"""
Builds an artist similarity graph from Last.fm's artist.getSimilar data,
and computes how well-connected each of a user's artists is to the rest
of their profile. This serves two purposes:

  1. A more honest outlier signal than tags alone - an artist with high
     tag-relevance but near-zero connectivity to the user's other artists
     in Last.fm's similarity graph is suspicious in a different way than
     low-tag-relevance, and requiring BOTH signals to be low before
     flagging an outlier is a stronger claim than either alone.

  2. The actual backbone structure the walk-based recommendation engine
     will traverse later - this isn't throwaway diagnostic code, it's the
     first real piece of the core engine.

Last.fm's similarity edges are themselves derived from listening
co-occurrence across all Last.fm users, not pure audio/content similarity,
so this signal has its own bias (mainstream co-occurrence) - which is
exactly why it's paired with tags rather than used alone.
"""

import asyncio
import networkx as nx
from dataclasses import dataclass

from app.core.lastfm_client import LastFMClient, LastFMError


@dataclass
class GraphConnectivity:
    graph: nx.Graph
    connectivity_scores: dict[str, float]  # artist -> 0-1 score, how connected to the rest of the profile


class ArtistGraphBuilder:
    def __init__(self, client: LastFMClient | None = None):
        self.client = client or LastFMClient()

    async def build_profile_graph(self, artists: list[str], similar_limit: int = 30) -> GraphConnectivity:
        """
        For each artist in the profile, fetch its similar-artists list and
        add edges (weighted by Last.fm's match score) for any similar
        artist that is ALSO in the user's own profile artist list. This
        gives a subgraph restricted to the user's own artists, which is
        what we need to measure internal connectivity, rather than the
        full unbounded similarity graph (which would be enormous and
        mostly irrelevant here).
        """
        profile_set = set(artists)

        async def fetch_similar(artist: str):
            try:
                return artist, await self.client.get_similar_artists(artist, limit=similar_limit)
            except LastFMError:
                return artist, []

        results = await asyncio.gather(*(fetch_similar(a) for a in artists))

        graph = nx.Graph()
        graph.add_nodes_from(artists)

        for artist, similar_list in results:
            for s in similar_list:
                if s["name"] in profile_set and s["name"] != artist:
                    # undirected edge; if it already exists (added from the
                    # other direction), keep the stronger match weight
                    if graph.has_edge(artist, s["name"]):
                        existing = graph[artist][s["name"]]["weight"]
                        graph[artist][s["name"]]["weight"] = max(existing, s["match"])
                    else:
                        graph.add_edge(artist, s["name"], weight=s["match"])

        connectivity_scores = self._compute_connectivity(graph, artists)

        return GraphConnectivity(graph=graph, connectivity_scores=connectivity_scores)

    def _compute_connectivity(self, graph: nx.Graph, artists: list[str]) -> dict[str, float]:
        """
        Connectivity score per artist: sum of edge weights to other artists
        in the profile, but normalized by PERCENTILE RANK within this
        profile's own score distribution rather than by the single max
        score.

        Why percentile rank instead of max-normalization: max-normalization
        is sensitive to genre density bias. Niche-genre profiles (e.g.
        prog rock / metal) have sparser similarity graphs on Last.fm simply
        because fewer users generate those similarity edges, not because
        the listener's taste is less coherent. Under max-normalization,
        even a profile's most-central artist might have a fairly low raw
        weighted-degree if the whole genre's graph is sparse, which then
        compresses everyone else's score unpredictably and makes scores
        incomparable across profiles with different genre densities.

        Percentile rank instead asks "what fraction of this person's OTHER
        artists does this artist out-connect", which is robust to overall
        graph sparsity - a sparse-but-internally-coherent niche profile and
        a dense mainstream profile can both produce a sensible 0-1 spread,
        since the comparison is always relative to peers within the same
        profile, never against an absolute scale.

        An artist with no edges at all still scores 0 (the floor case is
        unaffected by this change) - that remains the strongest "outlier"
        signal this module can give.
        """
        raw_scores = {}
        for artist in artists:
            if artist in graph:
                weighted_degree = sum(data["weight"] for _, _, data in graph.edges(artist, data=True))
                raw_scores[artist] = weighted_degree
            else:
                raw_scores[artist] = 0.0

        if not raw_scores or max(raw_scores.values()) == 0:
            return {a: 0.0 for a in artists}

        sorted_scores = sorted(raw_scores.values())
        n = len(sorted_scores)

        def percentile_rank(value: float) -> float:
            # fraction of OTHER artists this value is >= to (strictly
            # counting ties as not "beaten" keeps a tied bottom group at 0
            # rather than artificially inflating them)
            if n <= 1:
                return 1.0 if value > 0 else 0.0
            count_lower = sum(1 for s in sorted_scores if s < value)
            return count_lower / (n - 1)

        return {a: percentile_rank(raw_scores[a]) for a in artists}
