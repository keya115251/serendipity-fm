"""
Rediscover mode: surfaces artists from a listener's OLD phases on
purpose, rather than treating them as noise to exclude.

The rest of this project deliberately uses 12-month playcount instead of
"overall" as the basis for the user's taste profile (see
TasteProfileBuilder.build's docstring and the README's "Why two taste
layers" section) - because overall conflates genuinely current taste
with closed chapters, and including stale artists in the main profile
was actively harmful (it suppressed real outlier detection, since a
large historical playcount gave a dormant artist too much weight despite
barely featuring in actual recent listening).

This module exists to do the OPPOSITE thing on purpose: rather than
exclude stale artists, deliberately go find them. An artist with a large
"overall" playcount but a small (or zero) 12-month playcount represents
a genuine old phase - something the listener clearly cared about once,
which is a different, legitimate kind of discovery from the rest of this
project's "walk away from current taste" thesis. This is "walk
backwards in time" instead.
"""

from app.core.lastfm_client import LastFMClient
import asyncio


async def find_old_phases(
    username: str,
    client: LastFMClient,
    min_overall_playcount: int = 50,
    max_recent_ratio: float = 0.2,
    limit: int = 50,
    limit_results: int = 7,
) -> list[dict]:
    """
    Returns artists representing an old, dormant listening phase: a real
    overall playcount (not a one-off), but only a small fraction of that
    playcount in the last 12 months.

    Calls LastFMClient.get_top_artists directly for both periods, rather
    than going through TasteProfileBuilder.build - that builder also
    fetches per-artist tags and builds a similarity graph for outlier
    detection, none of which this function needs. Only raw playcounts per
    period matter here, so this skips work TasteProfileBuilder would
    otherwise do twice for no benefit.

    min_overall_playcount filters out artists the listener barely played
    even historically (a playcount of 2-3 isn't "an old phase," it's
    noise/a single curious listen).

    max_recent_ratio is the ceiling on (12month_playcount /
    overall_playcount) for an artist to count as "old" - 0.2 means an
    artist needs at least 80% of its total plays to predate the last 12
    months. An artist with NO 12-month plays at all has a ratio of 0.0,
    comfortably under any reasonable threshold.

    Returns a list of {artist, overall_playcount, recent_playcount,
    recent_ratio}, sorted by overall_playcount descending (the listener's
    biggest old-phase artists first), capped at limit_results (default 7,
    matching the top-7 convention used everywhere else in this project).
    """
    overall_artists, recent_artists = await asyncio.gather(
        client.get_top_artists(username, period="overall", limit=limit),
        client.get_top_artists(username, period="12month", limit=limit),
    )

    overall_counts = {a["name"]: a["playcount"] for a in overall_artists}
    recent_counts = {a["name"]: a["playcount"] for a in recent_artists}

    old_phases = []
    for artist, overall_count in overall_counts.items():
        if overall_count < min_overall_playcount:
            continue
        recent_count = recent_counts.get(artist, 0)
        ratio = recent_count / overall_count if overall_count else 0.0
        if ratio > max_recent_ratio:
            continue
        old_phases.append(
            {
                "artist": artist,
                "overall_playcount": overall_count,
                "recent_playcount": recent_count,
                "recent_ratio": ratio,
            }
        )

    old_phases.sort(key=lambda a: a["overall_playcount"], reverse=True)
    return old_phases[:limit_results]
