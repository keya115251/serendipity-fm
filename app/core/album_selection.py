"""
Picks a sensible "start here" album from an artist's Last.fm discography.

Naive top-by-playcount doesn't work on its own - real testing against
Last.fm's actual artist.getTopAlbums data showed three concrete failure
modes:

1. Shared-mbid duplicates: "Hozier" and "Hozier (Special Edition)" share
   the exact same MusicBrainz ID, meaning Last.fm has split one real
   album's plays across multiple catalogue entries.

2. Edition-suffix fragmentation: "Waving at the Sky" vs "Waving at the
   Sky (24-bit HD audio)" - distinct catalogue rows, no shared mbid,
   needing a name-pattern heuristic instead.

3. Small-artist punctuation/spelling fragmentation: a niche K-pop-
   adjacent artist had the SAME album catalogued as "1.Got Hooked: An
   Addictive Symphony" (the dominant entry, real mbid), "1. Got Hooked:
   An Addictive Symphony - EP", "1. Got Hooked: An Addictive Symphony"
   (extra space after the period), and "Got Hooked: An Addictive
   Symphony" (no leading "1." at all) - none of which are "edition"
   variants in any sense the suffix regex understands, just inconsistent
   manual data entry. This happened to not change the outcome in testing
   (one entry was dominant enough to win regardless), but a more evenly
   split case could have under-counted the real album's popularity or
   picked a malformed variant as the representative.
"""

import re
import asyncio
from difflib import SequenceMatcher

EDITION_SUFFIX_PATTERN = re.compile(
    r"\s*[\(\[][^()\[\]]*?(deluxe|expanded|special|anniversary|remaster(ed)?|"
    r"\d+(st|nd|rd|th)?\s*anniversary|hd audio|\d+-bit|bonus track|"
    r"live|acoustic|instrumental|edition|version)[^)\]]*[\)\]]\s*$",
    re.IGNORECASE,
)

# loose suffix markers that aren't bracketed (e.g. "... - EP", "... EP")
LOOSE_SUFFIX_PATTERN = re.compile(r"\s*[-:]?\s*(ep|single)\s*$", re.IGNORECASE)

FUZZY_MATCH_THRESHOLD = 0.85

COLON_SUBTITLE_PATTERN = re.compile(r"^(.+?)\s*:\s*.+$")

# unanchored keyword check, deliberately looser than EDITION_SUFFIX_PATTERN
# (which requires a clean trailing-bracket structure) - used only as a
# supporting signal alongside tag overlap in merge_high_tag_overlap_editions,
# not as a standalone merge trigger on its own
EDITION_KEYWORD_PATTERN = re.compile(
    r"deluxe|expanded|special edition|anniversary|remaster|hd audio|"
    r"\d+-bit|bonus track|live|acoustic|instrumental|edition|version",
    re.IGNORECASE,
)

HIGH_TAG_OVERLAP_THRESHOLD = 0.90


def merge_high_tag_overlap_editions(
    groups: dict[str, list[dict]],
    album_tags: dict[str, set[str]],
) -> dict[str, list[dict]]:
    """
    Merges two groups (within the same artist's discography) if they
    share very high tag overlap (>= HIGH_TAG_OVERLAP_THRESHOLD) AND at
    least one of their names contains an edition-style keyword somewhere
    in the title.

    Built specifically because a real edition-variant title didn't fit
    any existing structural pattern: "The Black Parade / Living With
    Ghosts (The 10th Anniversary Edition)" is a SLASH-COMPOUND title with
    its edition marker correctly stripped by EDITION_SUFFIX_PATTERN, but
    what remains ("The Black Parade / Living With Ghosts") still doesn't
    match the plain "The Black Parade" group by name - no amount of
    title-pattern engineering can be expected to anticipate every
    Last.fm title format. This uses the actual CONTENT (tag overlap) as
    the merge signal instead of the name structure.

    The edition-keyword requirement exists to guard against a real risk:
    two genuinely DIFFERENT albums by the same artist can easily share
    90%+ tag overlap purely because an artist's overall style is
    consistent across their discography - that's not a duplicate, that's
    just an artist with a stable sound. Requiring an edition keyword
    somewhere in one of the names reduces (but does not eliminate) the
    chance of merging two real, distinct releases. This is a heuristic
    with real edge cases, not a guaranteed-safe rule like the mbid check.

    album_tags maps each group's key (same keys as `groups`) to that
    group's representative tag set, since groups themselves don't carry
    tag data - the caller is responsible for fetching tags for at least
    each group's representative album before calling this.
    """
    keys = list(groups.keys())
    merged = dict(groups)
    consumed: set[str] = set()

    for key in keys:
        if key in consumed or key not in merged:
            continue
        for other_key in keys:
            if other_key == key or other_key in consumed or other_key not in merged:
                continue
            tags_a = album_tags.get(key, set())
            tags_b = album_tags.get(other_key, set())
            if not tags_a or not tags_b:
                continue
            overlap = len(tags_a & tags_b) / max(len(tags_a), len(tags_b))
            if overlap < HIGH_TAG_OVERLAP_THRESHOLD:
                continue
            # check the ORIGINAL (unstripped) names within each group for
            # an edition keyword - the group KEYS themselves are already
            # edition-stripped by strip_edition_suffix during grouping,
            # so the keyword we're looking for has already been removed
            # from the key string by the time it gets here
            names_in_either_group = [a["name"] for a in merged[key] + merged[other_key]]
            if not any(EDITION_KEYWORD_PATTERN.search(name) for name in names_in_either_group):
                continue
            merged[key].extend(merged.pop(other_key))
            consumed.add(other_key)

    return merged


def group_colon_subtitle_families(groups: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """
    Merges groups whose name matches "Parent: Subtitle" with a group
    whose name is exactly "Parent" - e.g. "Unreal Unearth: Unheard" and
    "Unreal Unearth: Unending" both merge into "Unreal Unearth".

    This is DELIBERATELY NOT part of the core dedup pipeline
    (deduped_album_groups) used by pick_entry_point_album and
    pick_entry_point_album_with_tags, and is not called anywhere in this
    module - it's opt-in, used only by app/core/album_outliers.py.

    The reason: a colon-subtitle is genuinely ambiguous in a way the
    bracketed edition-suffix pattern isn't. "Album: Subtitle" is
    structurally identical to how many completely standalone, unrelated
    album titles are formatted - this heuristic WILL over-merge in some
    cases (a legitimately separate "B-Sides" or concept-sequel release
    sharing a colon-prefixed name with its parent would incorrectly
    collapse here). It's accepted specifically for outlier detection,
    where the cost of a false merge (one fewer album considered) is much
    lower than the cost of a false outlier flag from a sparse-data
    fragment, but NOT accepted for entry-point selection, where merging
    two genuinely different releases could mean recommending the wrong
    one entirely.
    """
    merged = dict(groups)
    for key in list(merged.keys()):
        match = COLON_SUBTITLE_PATTERN.match(key)
        if not match:
            continue
        parent_key = match.group(1).strip()
        if parent_key in merged and parent_key != key:
            merged[parent_key].extend(merged.pop(key))
    return merged


def strip_edition_suffix(album_name: str) -> str:
    """
    Strips a trailing parenthetical/bracketed edition marker AND loose
    "- EP"/"Single" suffixes from an album name, e.g.
    "Hozier (Expanded Edition)" -> "Hozier",
    "Got Hooked: An Addictive Symphony - EP" -> "Got Hooked: An Addictive Symphony".
    Used as the dedup key for albums that don't share an mbid but are
    still clearly the same underlying release.
    """
    stripped = EDITION_SUFFIX_PATTERN.sub("", album_name)
    stripped = LOOSE_SUFFIX_PATTERN.sub("", stripped)
    return stripped.strip()


def _normalize_for_fuzzy_match(name: str) -> str:
    """Lowercase and collapse non-alphanumeric characters, so '1.Got Hooked' and '1. Got Hooked' normalize identically."""
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _merge_fuzzy_duplicate_groups(groups: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """
    Second merge pass: collapses groups whose keys are near-identical
    after aggressive normalization (punctuation/spacing stripped) and
    pass a fuzzy string similarity threshold. This catches the kind of
    small-artist catalogue fragmentation exact-match grouping misses
    entirely - e.g. "1.gothooked..." and "1.gothooked...ep" and
    "gothooked..." (missing the leading "1.") are different strings even
    after edition-suffix stripping, but are clearly the same release.

    O(k^2) in the number of distinct groups k, which is fine here since k
    is at most the number of albums passed in (typically under 20).
    """
    keys = list(groups.keys())
    normalized = {k: _normalize_for_fuzzy_match(k) for k in keys}

    merged: dict[str, list[dict]] = {}
    consumed: set[str] = set()

    for key in keys:
        if key in consumed:
            continue
        bucket = list(groups[key])
        consumed.add(key)
        for other_key in keys:
            if other_key in consumed:
                continue
            similarity = SequenceMatcher(None, normalized[key], normalized[other_key]).ratio()
            if similarity >= FUZZY_MATCH_THRESHOLD:
                bucket.extend(groups[other_key])
                consumed.add(other_key)
        merged[key] = bucket

    return merged


def deduped_album_groups(albums: list[dict]) -> dict[str, list[dict]]:
    """
    Runs both merge passes (edition-suffix stripping, then fuzzy
    punctuation/spelling matching) and returns the deduped groups keyed
    by their first-pass normalized name. Shared by both the plain
    popularity-based selector and the tag-aware one below, since the
    dedup logic itself doesn't depend on whether the final pick considers
    tags.
    """
    groups: dict[str, list[dict]] = {}
    for album in albums:
        key = strip_edition_suffix(album["name"]).lower()
        groups.setdefault(key, []).append(album)
    return _merge_fuzzy_duplicate_groups(groups)


def representative_for_group(group: list[dict]) -> dict:
    """Within a deduped group, prefer a PLAIN entry (no edition suffix) over a higher-playcount edition variant."""
    plain_entries = [a for a in group if strip_edition_suffix(a["name"]) == a["name"]]
    candidates_pool = plain_entries or group
    return max(candidates_pool, key=lambda a: a["playcount"])


def pick_entry_point_album(albums: list[dict]) -> dict | None:
    """
    Given a raw artist.getTopAlbums-shaped list (each dict with name,
    playcount, mbid), returns the single best "start here" album by
    PURE POPULARITY: real duplicate entries are collapsed first (see
    module docstring for the three real fragmentation bugs this dedup
    logic was built to fix), then the highest combined-playcount group's
    representative entry is returned.

    This is the popularity-only selector. For a version that also
    considers tag relevance to a specific listener's taste cluster, see
    pick_entry_point_album_with_tags - that version requires fetching
    each candidate album's own tags (an extra API call per album), so it
    isn't always the right default; this one stays available as the
    cheaper, dependency-free option.

    Returns None if the input list is empty.
    """
    if not albums:
        return None

    groups = deduped_album_groups(albums)
    best_group = max(groups.values(), key=lambda group: sum(a["playcount"] for a in group))
    return representative_for_group(best_group)


async def pick_entry_point_album_with_tags(
    artist_name: str,
    albums: list[dict],
    dominant_tags: dict[str, float],
    fetch_album_tags,
    consider_top_n: int = 4,
    tag_weight: float = 0.6,
    album_feedback: dict[tuple[str, str], str] | None = None,
) -> dict | None:
    """
    Tag-aware version of pick_entry_point_album: among an artist's most
    popular albums (after the same dedup as the plain version), picks
    whichever one best matches the LISTENER'S OWN dominant tags, not just
    whichever is most generically popular.

    Rationale: an artist gets recommended because they matched a
    specific taste cluster, but their single most popular album might
    not be their most representative one for THAT specific match - an
    artist with a wide stylistic range across their discography could
    have a more mainstream album that doesn't actually sound like why
    they were recommended in the first place. Scoring candidate albums
    against the same dominant_tags used to score the artist recommendation
    itself keeps the "start here" pick aligned with the actual reason
    this artist surfaced.

    Only the top `consider_top_n` groups by combined playcount are
    considered for tag fetching (default 4) - fetching tags for an
    artist's ENTIRE discography to find a slightly-better-matching deep
    cut isn't worth the extra API calls or the risk of recommending
    something genuinely obscure as a "start here" pick, which defeats the
    purpose of an entry point.

    final score per candidate = (1 - tag_weight) * popularity_score +
    tag_weight * tag_relevance, where popularity_score is each
    candidate's combined playcount normalized against the most popular
    candidate in the considered set (so the most popular one always
    scores 1.0 on that axis, not an arbitrary absolute number).

    album_feedback, if given, maps (artist_name, album_name) -> "liked"
    or "disliked" (see app/db/feedback_store.py - the keys are the user's
    PAST feedback on albums they've previously seen). A disliked candidate
    is down-weighted (multiplied by 0.5), NOT excluded outright - the
    feedback loop's design treats disliked ALBUMS as a steer-away signal,
    not a full block, unlike disliked ARTISTS which ARE fully excluded at
    the discovery-walk level (see DiscoveryWalk / FeedbackStore docstrings
    for that distinction). A liked candidate gets a small boost (×1.15).
    This only affects candidates the user has actually voted on before -
    everything else is scored exactly as it would be without feedback.

    fetch_album_tags is an async callable: (artist_name, album_name) ->
    list[{"tag": str, "count": int}], matching LastFMClient.get_album_info
    or an equivalent cache-backed wrapper. Injected as a parameter rather
    than imported directly, since this module otherwise has zero
    dependency on the Last.fm client itself and shouldn't gain one just
    for this optional path.

    Falls back to pick_entry_point_album's pure-popularity result if
    dominant_tags is empty (nothing to score against) or if no candidate
    album returns any tags at all.
    """
    if not albums:
        return None

    album_feedback = album_feedback or {}

    groups = deduped_album_groups(albums)
    if not dominant_tags:
        best_group = max(groups.values(), key=lambda group: sum(a["playcount"] for a in group))
        return representative_for_group(best_group)

    ranked_groups = sorted(groups.values(), key=lambda group: sum(a["playcount"] for a in group), reverse=True)
    top_groups = ranked_groups[:consider_top_n]
    representatives = [representative_for_group(g) for g in top_groups]

    async def score_one(rep: dict, group: list[dict]) -> tuple[dict, float, float]:
        tags = await fetch_album_tags(artist_name, rep["name"])
        relevance = sum(dominant_tags.get(t["tag"], 0.0) * (t["count"] / 100) for t in tags) if tags else 0.0
        playcount = sum(a["playcount"] for a in group)
        return rep, relevance, playcount

    scored = await asyncio.gather(*(score_one(rep, group) for rep, group in zip(representatives, top_groups)))

    max_playcount = max((pc for _, _, pc in scored), default=1) or 1
    max_relevance = max((rel for _, rel, _ in scored), default=0.0)

    if max_relevance == 0.0:
        # nothing returned usable tag data - fall back to pure popularity
        # rather than pretending tag scoring did something it didn't
        best_group = max(groups.values(), key=lambda group: sum(a["playcount"] for a in group))
        return representative_for_group(best_group)

    def combined_score(rep: dict, relevance: float, playcount: int) -> float:
        popularity_score = playcount / max_playcount
        tag_score = relevance / max_relevance
        score = (1 - tag_weight) * popularity_score + tag_weight * tag_score

        sentiment = album_feedback.get((artist_name, rep["name"]))
        if sentiment == "disliked":
            score *= 0.5
        elif sentiment == "liked":
            score *= 1.15
        return score

    best = max(scored, key=lambda triple: combined_score(triple[0], triple[1], triple[2]))
    return best[0]
