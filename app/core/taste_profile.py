"""
Builds a two-layer taste profile for a user:

  - long_term: aggregated from overall top artists (stable, broad taste)
  - short_term: aggregated from recent tracks (current mood/phase)

Each layer is represented as:
  - a weighted tag vector (dict[tag] -> weight), built by pulling each
    artist's top tags from Last.fm and weighting by the user's playcount
    for that artist
  - the raw artist list that fed it, for graph-walk seeding later

This intentionally does NOT collapse everything into a single centroid,
because a single centroid would wash out real outlier interests (e.g. one
K-pop artist sitting inside an otherwise all-indie top 10). Outliers are
flagged separately so the recommender can choose to wander from them on
purpose.
"""

import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from app.core.lastfm_client import LastFMClient, LastFMError
from app.core.artist_graph import ArtistGraphBuilder


@dataclass
class TasteLayer:
    tag_weights: dict[str, float]
    artists: list[str]
    artist_playcounts: dict[str, int]


@dataclass
class TasteProfile:
    username: str
    long_term: TasteLayer
    short_term: TasteLayer
    outlier_artists: list[str] = field(default_factory=list)
    connectivity_scores: dict[str, float] = field(default_factory=dict)


class TasteProfileBuilder:
    def __init__(self, client: LastFMClient | None = None):
        self.client = client or LastFMClient()
        self.graph_builder = ArtistGraphBuilder(self.client)

    async def _tag_weights_for_artists(
        self, artist_playcounts: dict[str, int], tags_per_artist_limit: int = 8
    ) -> dict[str, float]:
        """
        For each artist, fetch top tags and add to a running tag score,
        weighted by how much the user actually plays that artist.
        Concurrent fetches since these are independent API calls.
        """
        artists = list(artist_playcounts.keys())

        async def fetch_tags(artist: str):
            try:
                return artist, await self.client.get_artist_top_tags(artist, limit=tags_per_artist_limit)
            except LastFMError:
                return artist, []

        results = await asyncio.gather(*(fetch_tags(a) for a in artists))

        tag_scores: dict[str, float] = defaultdict(float)
        max_playcount = max(artist_playcounts.values(), default=1)

        for artist, tags in results:
            # normalize this artist's influence relative to the user's most-played artist,
            # so one heavily-played artist doesn't totally dominate the vector
            artist_weight = artist_playcounts[artist] / max_playcount
            for t in tags:
                # Last.fm tag 'count' is itself a 0-100 relative weight for that artist
                tag_scores[t["tag"]] += artist_weight * (t["count"] / 100)

        return dict(tag_scores)

    def _tag_relevance_scores(
        self,
        tag_weights: dict[str, float],
        artist_tags: dict[str, set[str]],
        weight_score_threshold: float = 0.35,
    ) -> dict[str, float]:
        """
        Returns each artist's tag-based relevance score (0-1) to the
        profile's dominant taste, using a weight-magnitude dominant set
        with a document-frequency discount on generic tags (see module
        docstring history / README for why this replaced a simpler
        rank-percentage cutoff and a raw-overlap-fraction score).

        This now returns scores rather than a final outlier list, because
        the actual outlier decision is made by combining this signal with
        graph connectivity in _combine_outlier_signals - tag relevance
        alone proved unreliable (a single generic tag like "rock" could
        rescue an artist that should have been flagged).
        """
        if not tag_weights:
            return {a: 0.0 for a in artist_tags}

        max_weight = max(tag_weights.values())
        dominant_tags = {t: w for t, w in tag_weights.items() if w >= max_weight * weight_score_threshold}

        total_artists = len(artist_tags)
        doc_freq = {
            tag: sum(1 for tags in artist_tags.values() if tag in tags)
            for tag in dominant_tags
        }
        specificity = {
            tag: 1.0 - (doc_freq[tag] / total_artists) for tag in dominant_tags
        }
        discounted_dominant = {tag: w * specificity[tag] for tag, w in dominant_tags.items()}
        dominant_weight_total = sum(discounted_dominant.values())

        scores = {}
        for artist, tags in artist_tags.items():
            if not tags or not dominant_weight_total:
                scores[artist] = 0.0
                continue
            matched_weight = sum(discounted_dominant.get(t, 0.0) for t in tags)
            scores[artist] = matched_weight / dominant_weight_total
        return scores

    def _combine_outlier_signals(
        self,
        tag_relevance: dict[str, float],
        connectivity: dict[str, float],
        tag_threshold: float = 0.2,
        connectivity_threshold: float = 0.1,
    ) -> list[str]:
        """
        An artist is flagged as a real outlier only if BOTH signals agree
        it doesn't belong to the user's dominant cluster: low tag relevance
        AND low graph connectivity. Requiring both is deliberately
        conservative - each signal has its own failure mode (tags: generic
        genre words inflate relevance; graph: Last.fm's similarity edges
        reflect mainstream listening co-occurrence, not pure content
        similarity), so agreement between two differently-biased signals is
        a stronger claim than either alone.

        tag_threshold=0.2 (raised from an initial 0.05) based on real
        testing: a known outlier pair (Porcupine Tree 0.176, TXT 0.103)
        scored too close to genuine core artists for a 0.05 cutoff to
        separate them at all. 0.2 catches both while still being safely
        below clear core artists' scores (e.g. Bon Iver 0.181 is borderline
        on tag score alone, but its connectivity of 0.252 comfortably
        clears the connectivity_threshold, so it is correctly NOT flagged
        even though its tag score alone is close to the outliers' range -
        this is exactly the scenario the two-signal AND-gate exists for.
        """
        outliers = []
        all_artists = set(tag_relevance) | set(connectivity)
        for artist in all_artists:
            t_score = tag_relevance.get(artist, 0.0)
            c_score = connectivity.get(artist, 0.0)
            if t_score < tag_threshold and c_score < connectivity_threshold:
                outliers.append(artist)
        return outliers

    async def build(
        self, username: str, long_term_limit: int = 25, short_term_limit: int = 20, long_term_period: str = "12month"
    ) -> TasteProfile:
        """
        long_term_period defaults to "12month" rather than "overall".

        "overall" conflates genuinely stable current taste with old phases a
        user has since moved past (e.g. a K-pop group with 787 overall plays
        but only 122 in the last 12 months - clearly a closed chapter, not
        part of current identity). Using overall caused the outlier detector
        to never flag such artists, since their large historical playcount
        gave them too much weight in the profile despite barely featuring in
        actual recent listening. "12month" is still broader/slower-moving
        than the short-term recent-tracks layer, but far more honest as a
        baseline for "what does this person currently listen to."
        """
        top_artists = await self.client.get_top_artists(username, period=long_term_period, limit=long_term_limit)
        recent_tracks = await self.client.get_recent_tracks(username, limit=short_term_limit)

        long_term_playcounts = {a["name"]: a["playcount"] for a in top_artists}

        # recent tracks have no playcount per-call; weight by recency position instead
        # (most recent = highest weight), and dedupe by artist
        recent_artist_order: list[str] = []
        for t in recent_tracks:
            if t["artist"] and t["artist"] not in recent_artist_order:
                recent_artist_order.append(t["artist"])
        n = len(recent_artist_order)
        short_term_playcounts = {
            artist: (n - idx) for idx, artist in enumerate(recent_artist_order)
        }

        long_term_tags, short_term_tags = await asyncio.gather(
            self._tag_weights_for_artists(long_term_playcounts),
            self._tag_weights_for_artists(short_term_playcounts),
        )

        # fetch per-artist tag sets again for outlier detection (small, already cached by Last.fm CDN-side typically)
        async def artist_tag_set(artist: str) -> tuple[str, set[str]]:
            try:
                tags = await self.client.get_artist_top_tags(artist, limit=8)
                return artist, {t["tag"] for t in tags}
            except LastFMError:
                return artist, set()

        lt_tag_sets = dict(await asyncio.gather(*(artist_tag_set(a) for a in long_term_playcounts)))

        graph_result = await self.graph_builder.build_profile_graph(list(long_term_playcounts.keys()))

        tag_relevance = self._tag_relevance_scores(long_term_tags, lt_tag_sets)
        outliers = self._combine_outlier_signals(tag_relevance, graph_result.connectivity_scores)

        return TasteProfile(
            username=username,
            long_term=TasteLayer(
                tag_weights=long_term_tags,
                artists=list(long_term_playcounts.keys()),
                artist_playcounts=long_term_playcounts,
            ),
            short_term=TasteLayer(
                tag_weights=short_term_tags,
                artists=recent_artist_order,
                artist_playcounts=short_term_playcounts,
            ),
            outlier_artists=outliers,
            connectivity_scores=graph_result.connectivity_scores,
        )
