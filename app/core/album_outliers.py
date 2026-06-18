"""
Finds album-level outliers within a single artist's own discography: the
album(s) whose tags diverge most from what that artist is typically
known for, using the same document-frequency-discounted tag-relevance
logic validated for artist-level outlier detection (see
app/core/taste_profile.py and the README's "Debugging the outlier
detector" section).

This is a genuinely different flavor of discovery than the artist-level
discovery walk: "you know this artist mainly for X, but they have an
album that's actually Y" - lower-risk in one sense (the listener already
likes the artist), but still a real surprise in what gets surfaced.

KNOWN LIMITATION: artist-level outlier detection used TWO independently-
biased signals (tag relevance AND graph connectivity) specifically
because tag relevance alone proved unreliable on its own in testing.
There is no album-level equivalent of artist.getSimilar (Last.fm has no
album.getSimilar endpoint), so this module is tag-relevance only. Given
the real, validated history of tag-only scoring producing false
positives/negatives at the artist level, album-level results here should
be treated as a weaker, single-signal heuristic, not a directly
equivalent guarantee.
"""

from app.core.album_selection import deduped_album_groups, representative_for_group, group_colon_subtitle_families
from app.core.tag_relevance import build_discounted_dominant_tags, score_relevance_against_dominant_tags


async def find_album_outliers(
    artist_name: str,
    fetch_top_albums,
    fetch_album_tags,
    max_albums: int = 15,
    outlier_threshold: float = 0.2,
    min_tags_for_outlier_eligibility: int = 3,
) -> dict:
    """
    Returns a dict with:
      - "typical_tags": the artist's aggregate discounted dominant tags
        (for display/explainability - "this artist is typically: X, Y, Z")
      - "albums": list of {name, tag_relevance, tags, tag_count,
        is_outlier, insufficient_data} for every deduped album considered,
        sorted by tag_relevance ascending (most outlier-like first)
      - "outliers": the subset of "albums" where is_outlier is True

    An album is only flagged as a real outlier if it has BOTH low
    relevance AND at least min_tags_for_outlier_eligibility tags (default
    3). This exists because real testing found sparse/empty-tag albums
    (e.g. a deluxe-edition fragment with only 1 tag, or a promotional
    release with 0 tags) scoring a flat 0.0 relevance purely from having
    nothing to score against, not from genuine musical divergence -
    without this gate, "we have no data on this album" and "this album is
    a real stylistic outlier" were indistinguishable. Albums below the
    tag-count threshold still appear in "albums" with
    insufficient_data=True, for transparency about what was excluded and
    why, rather than silently dropping them.

    fetch_top_albums: async callable (artist_name) -> list[{"name", "playcount", "mbid"}],
    matching LastFMClient.get_artist_top_albums.
    fetch_album_tags: async callable (artist_name, album_name) ->
    list[{"tag", "count"}], matching LastFMClient.get_album_info's tags
    (normalized to the same shape used elsewhere in this project).

    Both are injected as parameters rather than imported directly, same
    pattern as pick_entry_point_album_with_tags - this module has no
    dependency on the Last.fm client itself.

    Albums are deduped first using the same logic validated in
    app/core/album_selection.py (edition-suffix stripping + fuzzy
    punctuation/spelling matching), since a fragmented duplicate entry
    with sparse tag data could otherwise falsely look like an outlier
    simply from having less tag information, not because it's musically
    different.
    """
    raw_albums = await fetch_top_albums(artist_name)
    if not raw_albums:
        return {"typical_tags": {}, "albums": [], "outliers": []}

    groups = deduped_album_groups(raw_albums)

    # Colon-subtitle merging (e.g. "Unreal Unearth: Unheard" ->
    # "Unreal Unearth") is applied here but NOT in album_selection.py's
    # entry-point picker - see group_colon_subtitle_families' docstring
    # for why this over-merge risk is acceptable for outlier detection
    # specifically (a false merge just means one fewer album considered,
    # whereas in entry-point selection it could mean recommending the
    # wrong release entirely).
    groups = group_colon_subtitle_families(groups)

    ranked_groups = sorted(groups.values(), key=lambda g: sum(a["playcount"] for a in g), reverse=True)
    representatives = [representative_for_group(g) for g in ranked_groups[:max_albums]]

    async def fetch_one(album: dict) -> tuple[str, list[dict]]:
        tags = await fetch_album_tags(artist_name, album["name"])
        return album["name"], tags

    results = {}
    for album in representatives:
        name, tags = await fetch_one(album)
        results[name] = tags

    # build the artist's aggregate tag profile across all considered
    # albums, then discount by document frequency across ALBUMS (not
    # artists, as in the original use of this function) - an album-tag
    # that every one of this artist's albums shares (e.g. their own
    # genre signature) is appropriately treated as non-discriminating,
    # the same principle as the original artist-level fix, just one
    # level down
    aggregate_tag_weights: dict[str, float] = {}
    album_tag_sets: dict[str, set[str]] = {}
    for name, tags in results.items():
        tag_set = {t["tag"] for t in tags}
        album_tag_sets[name] = tag_set
        for t in tags:
            aggregate_tag_weights[t["tag"]] = aggregate_tag_weights.get(t["tag"], 0.0) + (t["count"] / 100)

    discounted_dominant_tags = build_discounted_dominant_tags(aggregate_tag_weights, album_tag_sets)

    scored_albums = []
    for name, tags in results.items():
        relevance = score_relevance_against_dominant_tags(tags, discounted_dominant_tags)
        has_enough_data = len(tags) >= min_tags_for_outlier_eligibility
        scored_albums.append(
            {
                "name": name,
                "tag_relevance": relevance,
                "tags": [t["tag"] for t in tags],
                "tag_count": len(tags),
                # an album needs BOTH low relevance AND enough tag data to
                # be trusted - real testing found albums like "Unreal
                # Unearth: Unending" (0 tags) and "Unaired" (1 tag)
                # scoring a flat 0.0 relevance purely because there was
                # nothing to score against, not because they're musically
                # different. Without this gate, sparse-data albums look
                # identical to genuine outliers.
                "is_outlier": relevance < outlier_threshold and has_enough_data,
                "insufficient_data": not has_enough_data,
            }
        )

    scored_albums.sort(key=lambda a: a["tag_relevance"])
    outliers = [a for a in scored_albums if a["is_outlier"]]

    return {
        "typical_tags": discounted_dominant_tags,
        "albums": scored_albums,
        "outliers": outliers,
    }
