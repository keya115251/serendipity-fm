"""
Builds the seed artist list for a discovery walk.

Originally seeding was just "top N artists by 12-month playcount." This
undersells listeners whose taste has more than one real cluster: tested
against a real profile that was prog-rock/metal-dominant by playcount but
also had a genuine secondary emo/alt-rock thread (Paramore, My Chemical
Romance, The Smashing Pumpkins, Pearl Jam all present in their known
artists), pure playcount-based seeding picked 4 prog seeds and only 1 from
the secondary cluster, so the resulting 597 candidates were almost
entirely more prog/metal - the secondary cluster's potential for discovery
was never explored.

This module mixes in outlier-detected artists (already computed by
TasteProfileBuilder via tag-relevance + graph-connectivity, see
app/core/taste_profile.py and the README's "Debugging the outlier
detector" section for how that signal was validated) as additional seeds,
so a real secondary cluster gets at least one seed slot even if none of
its artists crack the top-N by raw playcount.
"""


def build_seeds(
    top_artists_by_playcount: list[str],
    outlier_artists: list[str],
    primary_seed_count: int = 4,
    max_outlier_seeds: int = 2,
) -> list[str]:
    """
    Returns a deduplicated seed list: the top `primary_seed_count` artists
    by playcount, plus up to `max_outlier_seeds` detected outliers (in the
    order TasteProfileBuilder returned them) that aren't already included.

    primary_seed_count defaults to 4 rather than 5 (the original fixed
    seed count) specifically to make room for at least one outlier seed
    by default without increasing total seed count - the goal is
    diversifying WHERE seeds come from, not just adding more of them.
    """
    seeds = list(top_artists_by_playcount[:primary_seed_count])
    seed_set = set(seeds)

    added_outliers = 0
    for artist in outlier_artists:
        if added_outliers >= max_outlier_seeds:
            break
        if artist not in seed_set:
            seeds.append(artist)
            seed_set.add(artist)
            added_outliers += 1

    return seeds
