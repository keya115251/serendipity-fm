"""
No-account multi-album discovery: given up to 7 albums directly (artist
+ album name pairs, no Last.fm username), recommends similar albums
scored against the COMBINED tag signature of all 7 inputs together,
rather than any one of them individually.

Built as an extension of app/core/lookup.py's find_similar_albums
mechanism (artist-similarity blended with tag overlap), not the
discovery walk - albums aren't nodes in Last.fm's similarity graph
(there's no album.getSimilar), so there's nothing to walk through the
way app/core/multi_seed.py walks through artists. This module instead
unions each seed album's tags into one shared signature and scores
candidates against that, mirroring the "union, not best-match" design
decision made for this feature - the alternative (score each candidate
against whichever single seed album it matches best) was considered and
rejected, since it would let one strongly-matching seed dominate the
output the same way single-cluster dominance had to be fixed for the
artist-level discovery walk.
"""

import asyncio
import math

from app.core.album_selection import deduped_album_groups, representative_for_group, merge_high_tag_overlap_editions


async def recommend_from_albums(
    seed_albums: list[tuple[str, str]],
    fetch_album_tags,
    fetch_similar_artists,
    fetch_top_albums,
    n: int = 7,
    max_per_artist: int | None = None,
    similar_artist_count: int = 10,
    albums_per_artist: int = 5,
) -> list[dict]:
    """
    seed_albums: up to 7 (artist_name, album_name) tuples provided
    directly by the user.

    max_per_artist, if None (default), is computed the same way as
    app/core/multi_seed.py's max_per_cluster: ceil(n / number_of_seed_artists),
    floor of 1 - the original single-album lookup used a fixed
    max_per_artist=1, but that was tuned for a SINGLE seed's candidate
    pool; with up to 7 seed artists contributing candidates, a fixed cap
    of 1 could under-fill the result if some seed artists' candidate
    pools don't survive scoring, the same structural shortfall already
    caught and fixed for the multi-artist case.

    Tag signature: every seed album's own tags are pooled into one
    combined set (a UNION, not scored per-seed) - see module docstring
    for why union was chosen over best-match scoring.

    Candidate pool: each UNIQUE seed artist's own discography plus their
    top `similar_artist_count` similar artists' top `albums_per_artist`
    albums - deduplicated across seed artists, so if two seed albums
    happen to share an artist, that artist's candidate pool is only
    fetched once.

    final score per candidate = 0.3 * artist_similarity + 0.7 * tag_overlap,
    same formula and weighting as the single-album version (see
    lookup.find_similar_albums docstring for why 0.3/0.7, not the
    original 0.4/0.6, was the right split) - artist_similarity here is
    the candidate's best (max) similarity score across ALL seed artists,
    not just one, since a candidate related to ANY of the 7 inputs is a
    real connection worth crediting.
    """
    if not seed_albums:
        return []

    seed_albums = seed_albums[:7]
    seed_artist_names = list({artist for artist, _ in seed_albums})

    if max_per_artist is None:
        max_per_artist = max(1, math.ceil(n / len(seed_artist_names)))

    # union all seed albums' tags into one combined signature
    async def fetch_seed_tags(artist: str, album: str) -> set[str]:
        return set(await fetch_album_tags(artist, album))

    seed_tag_sets = await asyncio.gather(*(fetch_seed_tags(a, al) for a, al in seed_albums))
    combined_seed_tags: set[str] = set()
    for tag_set in seed_tag_sets:
        combined_seed_tags |= tag_set

    # for each unique seed artist, fetch their similar artists - build a
    # candidate-artist pool keyed by name, keeping the MAX match score if
    # the same candidate artist is similar to more than one seed artist.
    # Seed artists themselves are deliberately EXCLUDED from this pool
    # entirely (not just their seed album) - real testing surfaced two
    # separate problems with including them: (1) someone offering an
    # album as a taste signal has very likely already explored that
    # artist's other work, making "other albums by an artist you already
    # gave us" low-value compared to the artist-level lookup, where this
    # same logic doesn't apply as strongly; (2) a real bug where Last.fm's
    # autocorrect resolved a differently-capitalized seed album name
    # ("Riot!" -> "RIOT!") to the same album, but an exact-string
    # exclusion check against the literal user input didn't catch the
    # match, so the seed album recommended itself back. Excluding seed
    # artists outright sidesteps both issues at once rather than trying
    # to patch the name-matching logic to be more robust.
    similar_results = await asyncio.gather(*(fetch_similar_artists(a) for a in seed_artist_names))

    seed_artist_set = set(seed_artist_names)
    candidate_artist_matches: dict[str, float] = {}
    for similar_list in similar_results:
        for s in similar_list[:similar_artist_count]:
            if s["name"] in seed_artist_set:
                continue
            existing = candidate_artist_matches.get(s["name"], 0.0)
            candidate_artist_matches[s["name"]] = max(existing, s["match"])

    candidates = []  # (artist, album_dict, artist_similarity)
    for artist_name, artist_similarity in candidate_artist_matches.items():
        albums = await fetch_top_albums(artist_name)
        groups = deduped_album_groups(albums)

        # artist_name=artist_name forces EARLY binding (captured at
        # closure-creation time) instead of the default LATE binding
        # Python closures use - without this, every fetch_group_tags
        # call across every artist in this loop would reference the
        # SAME free variable, and since asyncio.gather runs them all
        # concurrently after the loop has already finished advancing,
        # they'd all silently use whatever artist_name happened to be
        # LAST by the time they actually executed. This was a real bug
        # caught in testing: real output showed every similar-artist
        # candidate missing from final results, with only the seed
        # artists' own albums surviving - consistent with every OTHER
        # artist's tag fetches being corrupted by this exact mistake.
        async def fetch_group_tags(group_key: str, group: list[dict], artist_name=artist_name):
            top_in_group = max(group, key=lambda a: a["playcount"])
            tags = set(await fetch_album_tags(artist_name, top_in_group["name"]))
            return group_key, tags

        group_tags = dict(await asyncio.gather(*(fetch_group_tags(k, g) for k, g in groups.items())))
        groups = merge_high_tag_overlap_editions(groups, group_tags)

        ranked = sorted(groups.values(), key=lambda g: sum(a["playcount"] for a in g), reverse=True)
        top_albums = [representative_for_group(g) for g in ranked[:albums_per_artist]]

        for album in top_albums:
            candidates.append((artist_name, album, artist_similarity))

    scored = []
    for artist, album, artist_similarity in candidates:
        tags = set(await fetch_album_tags(artist, album["name"]))
        tag_overlap = len(combined_seed_tags & tags) / len(combined_seed_tags) if combined_seed_tags else 0.0
        combined_score = 0.3 * artist_similarity + 0.7 * tag_overlap
        scored.append(
            {
                "artist": artist,
                "album": album["name"],
                "tag_overlap": tag_overlap,
                "artist_similarity": artist_similarity,
                "score": combined_score,
            }
        )

    scored.sort(key=lambda c: c["score"], reverse=True)

    selected = []
    per_artist_count: dict[str, int] = {}
    for c in scored:
        if len(selected) >= n:
            break
        count = per_artist_count.get(c["artist"], 0)
        if count >= max_per_artist:
            continue
        selected.append(c)
        per_artist_count[c["artist"]] = count + 1

    return selected
