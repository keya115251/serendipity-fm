"""
Shared tag-relevance scoring logic, extracted from TasteProfileBuilder so
that both outlier detection AND the discovery walk's candidate scoring use
the identical, validated implementation rather than two copies that could
drift apart.

The document-frequency discount here exists because of a concretely
observed failure: a generic tag like "rock" can have the highest raw
weight in a profile precisely because it's generic (it shows up across
most of a listener's artists), which made early outlier-detection logic
treat matching that one generic tag as strong evidence of fit - including
for artists that should have scored poorly. Discounting each dominant
tag's contribution by how common it is across the artist set being scored
against fixes this: a tag every artist shares contributes near nothing
discriminating, a tag only a couple of artists share keeps its full
weight. See the main project README's "Debugging the outlier detector"
section for the full narrative this fix came out of.
"""


def build_discounted_dominant_tags(
    tag_weights: dict[str, float],
    artist_tags: dict[str, set[str]],
    weight_score_threshold: float = 0.35,
) -> dict[str, float]:
    """
    Returns the dominant tag set (tags whose weight is at least
    weight_score_threshold of the single heaviest tag's weight), with each
    tag's weight discounted by its document frequency across artist_tags
    (1.0 - fraction of artists that carry that tag).
    """
    if not tag_weights:
        return {}

    max_weight = max(tag_weights.values())
    dominant_tags = {t: w for t, w in tag_weights.items() if w >= max_weight * weight_score_threshold}

    total_artists = len(artist_tags) or 1
    doc_freq = {tag: sum(1 for tags in artist_tags.values() if tag in tags) for tag in dominant_tags}
    specificity = {tag: 1.0 - (doc_freq[tag] / total_artists) for tag in dominant_tags}

    return {tag: w * specificity[tag] for tag, w in dominant_tags.items()}


def score_relevance_against_dominant_tags(
    candidate_tags: set[str] | list[dict],
    discounted_dominant_tags: dict[str, float],
) -> float:
    """
    Scores a single candidate's relevance (0-1) against an already-built
    discounted dominant tag set.

    candidate_tags can be either a plain set of tag strings (the shape
    used by outlier detection, where only presence/absence matters) or a
    list of {"tag": str, "count": int} dicts as returned directly by
    Last.fm (the shape used by the discovery walk, where each tag's own
    relative weight for that specific candidate also matters). When given
    the dict form, each tag's contribution is additionally scaled by its
    own count/100 (Last.fm's per-artist relative tag weight), so a
    candidate's WEAKLY-tagged match to a dominant tag counts for less than
    a STRONGLY-tagged one - this distinction didn't exist in the
    set-based outlier-detection usage, which only ever needed presence.
    """
    if not discounted_dominant_tags:
        return 0.0

    dominant_weight_total = sum(discounted_dominant_tags.values())
    if not dominant_weight_total:
        return 0.0

    if candidate_tags and isinstance(next(iter(candidate_tags)), dict):
        matched_weight = sum(
            discounted_dominant_tags.get(t["tag"], 0.0) * (t["count"] / 100) for t in candidate_tags
        )
    else:
        matched_weight = sum(discounted_dominant_tags.get(t, 0.0) for t in candidate_tags)

    return matched_weight / dominant_weight_total
