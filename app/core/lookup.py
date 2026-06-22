"""
Standalone artist/album lookup for the no-Last.fm-account use case: a
visitor gives a single artist or album, gets back similar
artists/albums, no user profile or taste cluster involved.

This is deliberately separate from the personalized discovery walk
(app/core/discovery_walk.py) - there's no taste profile to seed from or
score against here, so the relevant pieces are reused (mbid filtering,
entry-point album selection, the diversify-by-cap pattern) without
pulling in anything that depends on a Last.fm username.
"""

import asyncio

from app.core.album_selection import (
    pick_entry_point_album,
    deduped_album_groups,
    representative_for_group,
    merge_high_tag_overlap_editions,
)


async def find_similar_artists(
    artist_name: str,
    fetch_similar_artists,
    fetch_artist_info,
    fetch_top_albums,
    n: int = 7,
) -> list[dict]:
    """
    Returns up to n similar artists, each with a suggested entry-point
    album (via pick_entry_point_album - the plain popularity-based
    selector, not the tag-aware one, since there's no user taste cluster
    to score against in this no-account use case).

    Reuses the same mbid filter validated in the discovery walk
    (app/core/discovery_walk.py / README "The alias problem") to exclude
    collaboration/compilation credits - e.g. looking up "Jeff Buckley"
    shouldn't surface "Jeff Buckley & Gary Lucas" as a "similar artist."

    fetch_similar_artists: async callable (artist_name) ->
    list[{"name", "match"}], matching LastFMClient.get_similar_artists.
    fetch_artist_info: async callable (artist_name) -> {"mbid", ...},
    matching LastFMClient.get_artist_info.
    fetch_top_albums: async callable (artist_name) ->
    list[{"name", "playcount", "mbid"}], matching
    LastFMClient.get_artist_top_albums.
    """
    similar = await fetch_similar_artists(artist_name)

    results = []
    for candidate in similar:
        if len(results) >= n:
            break
        info = await fetch_artist_info(candidate["name"])
        if not info.get("mbid"):
            continue  # likely a collaboration/compilation credit, not a real artist

        albums = await fetch_top_albums(candidate["name"])
        entry_point = pick_entry_point_album(albums)

        results.append(
            {
                "artist": candidate["name"],
                "match": candidate["match"],
                "entry_point_album": entry_point["name"] if entry_point else None,
            }
        )

    return results


async def find_similar_albums(
    artist_name: str,
    album_name: str,
    fetch_album_tags,
    fetch_similar_artists,
    fetch_top_albums,
    n: int = 7,
    max_per_artist: int = 1,
    similar_artist_count: int = 10,
    albums_per_artist: int = 5,
) -> list[dict]:
    """
    Returns up to n albums similar to the given seed album, scored by a
    hybrid of artist-similarity (how closely the candidate's artist
    relates to the seed artist, via Last.fm's own match score) and
    album-level tag overlap with the seed album specifically (not a
    user's dominant tags - there's no user profile in this no-account
    use case, so each lookup is scored against the ONE seed album's own
    tags directly).

    Candidate pool: the seed artist's own discography (so the seed
    artist's OTHER albums are eligible too) plus each of the seed
    artist's top `similar_artist_count` similar artists' top
    `albums_per_artist` albums by playcount.

    max_per_artist defaults to 1 (every result from a different artist),
    not the more permissive cap tried initially. Real testing with
    max_per_artist=2 showed three closely-related artists (the seed
    artist, the seed artist's frontman's solo project, and one other
    similar artist) filling 6 of 7 final slots - a cap alone limits
    dominance but doesn't guarantee breadth, the same lesson learned with
    DiscoveryWalk.diversify_top_n's cap-vs-guarantee distinction. A
    listener asking for "similar albums" most likely wants varied
    artists, not 2 deep cuts each from the same 3 acts.

    final score per candidate = 0.3 * artist_similarity + 0.7 * tag_overlap,
    where artist_similarity is 1.0 for the seed artist's own other albums
    (maximally "related" by definition) and Last.fm's own match score
    (0-1) for every other candidate's artist, and tag_overlap is the
    fraction of the seed album's own tags also present on the candidate
    album.

    Originally 0.4/0.6 - shifted to 0.3/0.7 after real testing showed
    artist similarity could outweigh a much weaker tag match: "Stomachaches"
    by Frank Iero (an MCR member's solo project, artist_similarity=0.75)
    scored above "Infinity on High" by Fall Out Boy (artist_similarity=0.42
    but a real, comparable tag_overlap), purely because "this person was
    in the band" carried more weight than the actual sonic tag match. Tag
    overlap is the more direct signal for "does this actually sound
    similar"; artist similarity is now mostly a tiebreaker/sanity check
    rather than a co-equal factor. This split still hasn't been validated
    against a wide range of albums or genres - it's a deliberate, larger
    correction in the right direction based on one real test case, not a
    tuned, proven value.
    """
    seed_tags = set(await fetch_album_tags(artist_name, album_name))

    similar_artists = await fetch_similar_artists(artist_name)
    similar_artists = similar_artists[:similar_artist_count]

    candidate_artists = [{"name": artist_name, "match": 1.0}] + [
        {"name": a["name"], "match": a["match"]} for a in similar_artists
    ]

    candidates = []  # (artist, album_dict, artist_similarity)
    for ca in candidate_artists:
        albums = await fetch_top_albums(ca["name"])
        groups = deduped_album_groups(albums)

        # fetch tags for each group's top-playcount member (not just the
        # eventual survivors) so the tag-overlap edition merge below has
        # something to compare - this is real added cost (one extra tag
        # fetch per group, not just per final candidate), accepted because
        # there's no structural way to anticipate every edition-variant
        # title format Last.fm's catalogue uses (see
        # merge_high_tag_overlap_editions docstring for the real case -
        # a slash-compound anniversary-edition title - that motivated this)
        # ca=ca forces EARLY binding instead of Python's default late
        # binding for closures - this happens to work correctly TODAY
        # since the inner asyncio.gather below resolves before the outer
        # loop advances to the next artist, but it's the exact same
        # fragile pattern that caused a real bug in the multi-artist
        # version of this logic (app/core/multi_album.py) once multiple
        # artists' fetches needed to be concurrent with each other, not
        # just within one artist's own processing. Fixed proactively here
        # rather than waiting for a refactor to break it the same way.
        async def fetch_group_tags(group_key: str, group: list[dict], ca=ca):
            top_in_group = max(group, key=lambda a: a["playcount"])
            tags = set(await fetch_album_tags(ca["name"], top_in_group["name"]))
            return group_key, tags

        group_tags = dict(await asyncio.gather(*(fetch_group_tags(k, g) for k, g in groups.items())))
        groups = merge_high_tag_overlap_editions(groups, group_tags)

        ranked = sorted(groups.values(), key=lambda g: sum(a["playcount"] for a in g), reverse=True)
        top_albums = [representative_for_group(g) for g in ranked[:albums_per_artist]]

        for album in top_albums:
            if ca["name"] == artist_name and album["name"] == album_name:
                continue  # exact literal match - kept as a cheap first check
            # also exclude if this candidate ended up merged into the SAME
            # group as the seed album itself, even under a different name
            # (e.g. the seed album is "The Black Parade" and this
            # candidate's group also contains an anniversary-edition
            # variant of it) - checking literal name equality alone isn't
            # enough once groups can span multiple differently-named rows
            if ca["name"] == artist_name:
                seed_group = next((g for g in groups.values() if any(a["name"] == album_name for a in g)), None)
                candidate_group = next((g for g in groups.values() if album in g), None)
                if seed_group is not None and seed_group is candidate_group:
                    continue
            candidates.append((ca["name"], album, ca["match"]))

    scored = []
    for artist, album, artist_similarity in candidates:
        tags = set(await fetch_album_tags(artist, album["name"]))
        tag_overlap = len(seed_tags & tags) / len(seed_tags) if seed_tags else 0.0
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

    # diversity cap: no single artist (including the seed artist's own
    # discography) gets more than max_per_artist of the final n slots
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
