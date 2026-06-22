# serendipity-fm

A music recommender built on Last.fm data that optimizes for genuine
discovery rather than obvious similarity.

Standard recommenders rank by predicted relevance alone, which converges
on whatever's already popular and adjacent to a listener's taste - the
five most famous, most-tagged-similar acts you'd have found anyway. This
project optimizes for a second axis alongside relevance: unexpectedness.
It walks a moderate distance away from a listener's dominant taste
cluster instead of recommending from directly inside it, while still
requiring a relevance floor so the result is discovery, not noise.

**Contents:** [Architecture](#architecture) ·
[Features](#features) ·
[Design decisions and debugging history](#design-decisions-and-debugging-history) ·
[Tech stack](#tech-stack) · [Setup](#setup)

## Architecture

| Module | Role |
|---|---|
| `app/core/lastfm_client.py` | Async Last.fm API wrapper, capped concurrency, retry on timeout |
| `app/db/` | SQLite cache for artist info, tags, similarity edges, and per-user feedback |
| `app/core/taste_profile.py` | Two-layer (12-month + recent) taste profile, outlier detection |
| `app/core/artist_graph.py` | Similarity graph restricted to a user's own artists, connectivity scoring |
| `app/core/tag_relevance.py` | Shared document-frequency-discounted tag relevance logic |
| `app/core/seeding.py` | Mixes top-by-playcount artists with detected outliers for walk seeding |
| `app/core/discovery_walk.py` | The recommendation engine: expand, cluster-aware score, diversify |
| `app/core/album_selection.py` | Dedup logic + entry-point album selection (plain and tag-aware) |
| `app/core/album_outliers.py` | Album-level outlier detection - built, shelved, see below |
| `app/core/lookup.py` | Standalone artist/album lookup, no Last.fm account needed |
| `app/core/rediscover.py` | Surfaces a listener's old, dormant phases on purpose |
| `app/db/feedback_store.py` | Per-user like/dislike feedback on recommended artists and albums |
| `app/core/multi_seed.py` | No-account discovery walk seeded from up to 7 user-given artists |
| `app/core/multi_album.py` | No-account album recommendations from up to 7 user-given albums |

Full reasoning for each piece, including the real bugs found and fixed
along the way, lives in [Design decisions and debugging
history](#design-decisions-and-debugging-history) below - this table is
just the map.

## Features

**The personalized discovery walk** (needs a Last.fm username) - the
core feature. Builds a taste profile, seeds a graph walk from a
listener's dominant cluster plus any detected secondary clusters, and
returns 7 diversity-capped recommendations, each with a suggested
entry-point album.

**Niche-ness slider** - a 0.0-1.0 control on how obscure recommendations
skew, independent of how far (hop-wise) the walk wanders. Built on top
of the already-validated popularity floor/dampen scoring mechanism
rather than a new one - see [the niche-ness slider
section](#the-niche-ness-slider) below for why that mechanism was chosen
over two other real options that were considered and rejected.

**Multi-artist input** (no account needed) - give up to 7 artists
directly, get recommendations seeded from all of them at once. Reuses
the full discovery walk machinery (cluster-aware scoring, diversity cap,
niche-ness slider, entry-point albums) with the 7 artists as equal-
status seeds, no outlier detection step since all inputs are explicitly
chosen rather than inferred from listening history.

**Multi-album input** (no account needed) - give up to 7 albums
directly (as artist::album pairs), get similar albums back. Combines all
7 seed albums' tags into one shared signature and scores candidates
against that union, rather than treating each seed album independently.
Seed artists' own other albums are excluded from results on the
reasoning that someone offering an album as a taste signal has very
likely already explored that artist's discography. See [debugging the
multi-album input](#debugging-the-multi-album-input) below for two real
bugs found during initial testing.

**Multi-artist / multi-album input** (no account needed) - give up to 7
artists or up to 7 albums directly, get real discovery-walk-quality
recommendations back without needing a Last.fm profile at all. Built on
the same validated mechanisms as the personalized features (the
discovery walk for artists, lookup.py's tag+artist-similarity scoring
for albums) rather than separate, simpler logic - see [the multi-input
section](#multi-input-no-account-needed) below for the real bugs this
surfaced, including one that was quietly present in the original
single-album lookup too.

**Rediscover mode** - deliberately surfaces a listener's OLD, dormant
phases: artists with a large all-time playcount but almost no plays in
the last 12 months. The opposite of the rest of this project's "find
something new" thesis - this is "remember this?" on purpose.

**Feedback loop** - like or dislike a recommended artist or album.
Disliked artists are excluded from future recommendations entirely;
liked artists stay eligible but are down-weighted, and their tags get
folded into future taste-shaping. Disliked albums are down-weighted in
entry-point selection; liked albums get a small boost. Persisted per
Last.fm username in a local SQLite table - see [the feedback loop
section](#the-feedback-loop) below for why this design, not a simpler
one, was the right call.

**Album outlier detection** (built, shelved) - finds stylistically
divergent albums within a single known artist's discography. Real
testing kept surfacing cataloguing artifacts instead of genuine musical
divergence; see [why it's shelved](#album-outlier-detection-shelved)
below.

## Design decisions and debugging history

This project's README has historically documented real bugs and dead
ends as they were found, not just the final working version - that
history is preserved here, just collapsed by default so it doesn't bury
the parts most worth reading first. Click any section to expand it.

<details>
<summary><strong>Why two taste layers, and why 12-month, not all-time</strong></summary>

Last.fm's `overall` period reports all-time playcounts, which conflates
genuinely stable current taste with phases a listener has since moved
past entirely. In real testing, one test profile showed an artist with
787 all-time plays but only 122 in the last 12 months - clearly a closed
chapter, not part of current identity, yet `overall` weighted it almost
as heavily as artists still in active rotation. Switching the long-term
layer to the `12month` period fixed this directly. `overall` data is
still useful - it's now deliberately used by rediscover mode (see
below) to surface exactly these old phases on purpose, but it should
never silently feed the default recommendation logic.

</details>

<details>
<summary><strong>Debugging the outlier detector</strong></summary>

This section exists because the actual debugging process here is more
informative than the final result alone, and it's worth documenting
honestly rather than presenting the final version as if it worked first
try.

**Attempt 1: zero tag overlap.** The first version flagged an artist as
an outlier if its top tags shared *zero* overlap with the profile's
dominant tags (defined as the top 30% by rank). This failed immediately
on a known test case - a K-pop group sitting inside an otherwise all-indie
profile wasn't flagged, because Last.fm's crowdsourced tagging is noisy
enough that the group still picked up a generic "pop" tag, which was
enough to clear a strict zero-overlap bar.

**Attempt 2: proportional overlap with a rank-based dominant set.**
Switched to a fractional overlap score (what fraction of an artist's own
tags fall in the dominant set), still flagged nothing. Tracing the actual
numbers showed why: with ~25 artists each contributing up to 8 tags, the
real unique tag pool was 73 tags, not the ~15 visible in summary output.
A "top 40% by rank" cutoff against 73 tags pulls in 29 tags as "dominant"
- including low-weight tags that exist in the pool *only* because the
supposedly-outlier artist contributed them itself, which is circular: an
artist's own niche tag (e.g. "progressive rock") becomes "dominant" partly
because that artist pushed its weight up, and the artist then gets
credited for matching it.

**Attempt 3: weight-magnitude cutoff.** Redefined "dominant" by weight
magnitude relative to the single heaviest tag, rather than rank
percentage. This kept the dominant set tight regardless of total pool
size, but still didn't flag the known outlier, because the heaviest tag in
the profile was a generic word ("rock") that the outlier artist also
happened to carry - and that single heavy, generic tag alone was enough
to clear the relevance threshold.

**Attempt 4: document-frequency discount (the fix that actually worked
for tags).** Added an IDF-style discount: each dominant tag's contribution
is scaled down by how common that tag is across the user's *own* artist
list. A tag every artist shares (like "rock") contributes almost nothing
discriminating; a tag only one or two artists share keeps its full
weight. This correctly separated the two known outlier artists from
everything else in the profile by tag signal alone - but their scores
were still close enough to some genuine core artists' scores that a
single absolute threshold couldn't cleanly separate them without false
positives.

**Adding a second, independently-biased signal.** Rather than keep tuning
one signal, a graph-connectivity score was added using Last.fm's
`artist.getSimilar` data, restricted to the user's own artist set. An
artist is now only flagged as a real outlier if **both** signals agree:
low tag relevance *and* low graph connectivity. This matters because each
signal has a different failure mode - tags are noisy and biased toward
generic genre words; Last.fm's similarity graph reflects mainstream
listening co-occurrence, not pure content similarity - so requiring
agreement between two differently-biased signals is a stronger claim than
either alone. Two known outlier artists tested at a literal 0.0 graph
connectivity score, while genuine core artists with a borderline-low tag
score (close to the outliers' range) still cleared the connectivity bar
comfortably, correctly surviving.

**Validating against a second, structurally different profile.** Testing
against a second real Last.fm profile (a prog rock / metal-dominated
library) surfaced a genre-density bias: niche genres have sparser
similarity graphs on Last.fm simply because fewer users generate those
edges, not because the listener's taste is less coherent. Under
max-normalization, this made connectivity scores incomparable across
profiles with different genre densities. Switched to percentile-rank
normalization (what fraction of a user's *other* artists does this artist
out-connect) instead of dividing by the single highest score in the
profile, which is robust to overall graph sparsity since the comparison
is always relative to peers within the same profile.

**Known limitation.** The combined detector answers "is this artist
poorly connected to the rest of the profile by both signals" - it does
not yet distinguish a genuinely separate taste cluster (e.g. a K-pop
group sitting inside an indie profile) from simply the single
worst-fitting member of an otherwise tight, single-cluster profile (e.g.
the loosest-fitting classic rock act in a prog/metal library). Both cases
currently get flagged the same way. True cluster detection (is there more
than one dense sub-group in this person's graph) would be a more precise
answer and is a natural next step, but was deliberately deferred in favor
of keeping the current two-signal design simple and shipped.

</details>

<details>
<summary><strong>Debugging the discovery walk</strong></summary>

Once the walk was built and producing real recommendations, several
rounds of testing against real, structurally different listener profiles
surfaced problems that weren't visible from a single test case.

**The alias problem.** Early results included entries like "Jeff Buckley
& Gary Lucas" and "Selena Gomez, benny blanco & The Marías" - one-off
collaboration credits that Last.fm catalogues as standalone "artists,"
recommended as if they were a real, ongoing discography. Checked the raw
`artist.getInfo` response for known good cases (e.g. "Hayley Williams,"
a real independent solo act) versus known bad cases, and found a clean,
reliable signal: legitimate catalogued artists have a real MusicBrainz ID
(`mbid`); one-off collaboration credits consistently don't. Candidates
with no `mbid` are now excluded entirely.

**The weak-seed problem.** Seeding the walk from a listener's top 5
artists by playcount, then expanding through Last.fm's similarity graph,
kept surfacing via-artists the listener had only heard a handful of times
- e.g. a folk artist someone had played twice in their life became a
load-bearing node that fed several downstream recommendations. Two fixes
were tried and discarded before adding full path-tracking (every
candidate stores its complete seed-to-candidate chain, not just the
immediate parent) made the actual problem visible and explainable rather
than something to keep guessing at blind. Logged as a known, accepted
limitation rather than fully solved - see Tradeoffs below.

**The single-cluster seeding problem.** Tested against a second real
profile that was prog-rock/metal-dominant by playcount but had a genuine
secondary emo/pop-punk cluster (Paramore, My Chemical Romance, Pearl Jam,
The Smashing Pumpkins all present in their known artists). Seeding from
top-5-by-playcount alone picked 4 prog seeds and 1 emo seed, so the
resulting ~600 candidates were almost entirely more prog - the secondary
cluster's discovery potential was never explored. Fixed by mixing
detected outlier artists into the seed list (`app/core/seeding.py`), so a
real secondary cluster gets at least one seed slot even if none of its
artists crack the top N by raw playcount.

**The single-dominant-tags problem.** Diversifying seeds didn't change
the final output at all, which was the signal something else was wrong.
Every candidate, regardless of which seed it came from, was still scored
against ONE global dominant-tag profile - overwhelmingly prog/metal for
that listener. A candidate discovered via the My Chemical Romance seed
was being asked "does this fit the listener's overall aggregate taste"
instead of "does this fit the MCR-adjacent cluster specifically," and
structurally lost every time. Fixed by scoring each candidate against its
OWN originating cluster's tag profile (the global profile for primary
seeds, that artist's own tags for outlier seeds), using
`candidate.path[0]` to know which cluster a candidate actually came from.

**The cross-cluster popularity problem.** Cluster-aware tag scoring
fixed relevance, but emo-cluster candidates were still losing to
prog-cluster ones, because pop-punk/emo as a genre simply has more
mainstream-popular acts than underground prog does - so even a
great-fitting emo candidate (Bayside, the strongest tag match in its
cluster) scored far below niche prog candidates purely on an absolute
popularity scale that didn't mean the same thing across genres. First
attempted percentile-rank-within-cluster, but this just relocated the
unfairness: the single most-mainstream member of any cluster gets a raw
percentile of 0 regardless of fit, zeroing out its score entirely. The
actual fix combined a floor (popularity contribution can never go below
30%) with a dampening exponent (square root) on the remainder. Verified
directly: Bayside went from absent in the top 10 to scoring 4th overall.

**The outlier-dominance problem.** Fixing cross-cluster fairness swung
the bias the other way for a different profile: a listener with two
detected outlier seeds (K-pop and prog rock) ended up with 4 of the top
5 recommendations being K-pop, since that cluster's candidates happened
to sit at a favorable point in their own narrow popularity distribution
and nothing capped how much of the output any one cluster could claim.
Added `diversify_top_n`: a cap on max recommendations per cluster, plus
a guarantee that every cluster with a viable candidate gets at least one
slot. This still wasn't quite right alone - every detected outlier got
the identical guaranteed slot and ceiling regardless of how much CURRENT
signal it represented. A near-dormant K-pop outlier (787 all-time plays,
122 in the last 12 months) received the same allocation as an outlier
the listener still actively engaged with. Final fix: each outlier's slot
allocation is now scaled by its 12-month playcount relative to the
listener's most-played artist, and a cluster below a minimum relevance
threshold loses the guarantee entirely. The near-dormant cluster went
from 2 of 7 final recommendations to 1.

**Tradeoffs accepted rather than further chased.** A candidate reachable
from more than one seed only keeps the first path encountered during
traversal, order-dependent rather than necessarily "the most meaningful"
path - a real simplification, not a fully solved design. The weak-seed
problem is mitigated by path visibility, not eliminated. The diversity
cap operates at the seed-cluster level only - two final recommendations
from the same primary cluster can still share the same hop-1
intermediate bridge artist, the same one-branch-dominating pattern
recurring at a smaller scale, not yet addressed.

</details>

<details>
<summary><strong>Debugging album selection</strong></summary>

Once the discovery walk was producing solid artist recommendations, the
next question was obvious: recommend an artist, sure, but where should a
listener actually start? Real testing against `artist.getTopAlbums`
exposed why "just take the highest-playcount album" doesn't work.

**Shared-mbid duplicates.** Hozier's self-titled debut, its Expanded
Edition, and its Special Edition are catalogued as separate rows.
Expanded and Special share one MusicBrainz ID; the plain edition has a
DIFFERENT one - so grouping by mbid alone under-merges, splitting one
real album into two competing groups. Switched to grouping by
edition-suffix-stripped name as the primary key instead.

**Picking the wrong representative within a group.** Even after correct
grouping, naively taking the highest-individual-playcount row within the
winning group returned "Hozier (Expanded Edition)" instead of plain
"Hozier." Fixed by preferring a plain, no-suffix entry within the
winning group whenever one exists.

**Small-artist punctuation fragmentation.** A niche artist's debut album
was catalogued as four near-identical spellings ("1.Got Hooked: An
Addictive Symphony", with and without a leading "1.", with and without a
space after the period) - none of which are "edition" variants the
suffix regex understands, just inconsistent manual data entry. Added a
second, fuzzy-string-similarity merge pass (`difflib.SequenceMatcher`
against punctuation-stripped names) to catch this class of fragmentation
that exact matching can't.

**Tag-aware selection.** An artist gets recommended because they matched
a specific taste cluster, but their single most popular album might not
be their most representative one for THAT match. Added
`pick_entry_point_album_with_tags`: among an artist's top few albums by
combined playcount, score each against the SAME per-cluster dominant
tags used to score the artist recommendation itself, blending tag fit
with popularity rather than using either alone.

<a id="album-outlier-detection-shelved"></a>
**Album-level outlier detection, and why it's shelved.** Tried extending
the same outlier-detection thesis one level down - finding stylistically
divergent albums within a single known artist's own discography. Real
testing against a real discography (Hozier's) repeatedly surfaced false
positives that turned out to be cataloguing artifacts, not genuine
musical divergence: albums with 0-1 tags scoring a flat 0.0 relevance
purely from lacking data (fixed with a minimum-tag-count eligibility
gate); deluxe/extended-tracklist entries like "Unreal Unearth: Unheard"
that needed a colon-subtitle merging heuristic on top of the existing
dedup (scoped to outlier detection only, not entry-point selection,
since the over-merge risk is acceptable in one context and not the
other); and finally, albums that were neither pre-release singles nor
deluxe editions but post-release marketing repackagings (the same
tracklist resold as a separate "album" with one extra song) - which
slipped past every fix above because they have genuinely real, if
sparse, independent tag data. With no album-level equivalent of
`artist.getSimilar` to use as a second, independent validating signal,
and a real catalogue-data ceiling that kept producing artifacts no
matter how many heuristics were added, this mode is shelved rather than
shipped. The dedup infrastructure it shares with entry-point selection
is real and useful; the outlier-detection feature itself isn't
considered production-ready.

</details>

<details>
<summary><strong>Debugging the standalone lookup feature</strong></summary>

Built for visitors without a Last.fm account: give one artist or album,
get similar ones back, no profile or taste cluster involved. Reuses
validated pieces (mbid filtering, entry-point album selection) without
depending on a username.

**A title format the dedup logic had never seen.** Looking up "The
Black Parade" by My Chemical Romance surfaced "The Black Parade / Living
With Ghosts (The 10th Anniversary Edition)" as a top result - the seed
album recommending itself. Two separate bugs: first, the edition-suffix
regex required its keyword to appear immediately after the opening
bracket, so "(The 10th Anniversary Edition)" - with "The" in between -
didn't match at all; broadened the pattern to allow leading words before
the keyword. Second, even after that fix, what remained was a
slash-compound title that still didn't match the plain "The Black
Parade" group by name. Rather than keep special-casing title formats,
added a content-based merge instead: two same-artist albums with very
high tag overlap (90%+) AND an edition keyword present in one of the
original names get merged, regardless of how the title is structured.

**Artist similarity outweighing actual sound.** Initial scoring (0.4
artist-similarity, 0.6 tag-overlap) let "Stomachaches" by Frank Iero (an
MCR member's solo project, high artist-similarity but only 40% tag
overlap) outscore "Infinity on High" by Fall Out Boy (lower
artist-similarity but a real, comparable tag match) - "this person was
in the band" carried more weight than whether the music actually sounds
similar. Reweighted to 0.3/0.7 in favor of tag overlap; verified against
the real scores from this exact case that the ordering flips correctly.

**Diversity cap needed a guarantee, not just a ceiling.** A cap of 2
albums per artist still let MCR, Gerard Way, and The Used fill 6 of 7
results - the same cap-without-guarantee gap already learned from
`diversify_top_n`. Lowered to 1 per artist so every result is a
different act.

</details>

<details>
<summary><strong>Rediscover mode</strong></summary>

The rest of this project deliberately uses 12-month playcount instead of
`overall` for the default taste profile, because `overall` conflates
genuinely current taste with closed chapters (see "Why two taste
layers" above). Rediscover mode does the opposite on purpose: it finds
artists with a real `overall` playcount but only a small fraction of
that in the last 12 months, and surfaces them directly, no walk, no new
discovery, just "remember this?"

Built as a thin module (`app/core/rediscover.py`) that calls
`LastFMClient.get_top_artists` for both periods directly, rather than
going through the full `TasteProfileBuilder` - that builder also fetches
per-artist tags and builds a similarity graph for outlier detection,
none of which this feature needs, so the simpler, cheaper version was
built from the start rather than optimized later.

**A real edge case, deliberately left alone.** Testing against a real
profile flagged Hozier as an "old phase" (1221 all-time plays, 151 in
the last 12 months, a 0.124 ratio) despite Hozier also being that
listener's single biggest *current* 12-month artist - both things can be
true at once when an artist has a long enough history. The fix under
consideration was excluding any artist still in the user's current top N
from rediscover results regardless of ratio, but the listener's own
judgment ("I haven't been listening to Hozier much this year") was a
better signal than anything the ratio math could provide, so this was
deliberately left as a known characteristic rather than patched.

</details>

<a id="the-feedback-loop"></a>
<details>
<summary><strong>The feedback loop</strong></summary>

Lets a listener like or dislike a recommended artist or album, persisted
in a new SQLite table (`app/db/feedback_store.py`, `UserFeedback` in
`app/db/models.py`) keyed by Last.fm username - the natural identity key
already used everywhere else in this project, since there's no separate
account system.

**The rule, once it was actually worked out.** Disliked artists are
excluded entirely from future recommendations (folded into
`known_artists` at walk time, same as an artist the listener already
listens to). Liked artists stay eligible to be recommended again, but
are down-weighted in scoring (`final_score *= 0.4`) rather than excluded.
Disliked albums are down-weighted in entry-point selection, not
excluded - disliking one album shouldn't blacklist the whole artist.
Liked albums get a small boost. Separately, a liked artist's tags get
folded into the dominant tag profile with a modest weight, shaping what
OTHER candidates look appealing.

**A real design dead-end, caught before shipping.** The first version
excluded BOTH liked and disliked artists from `known_artists`, on the
reasoning that any artist with feedback (either direction) has already
been heard and shouldn't resurface as a "new" discovery. This created a
dead end: the album-feedback boost (push a liked artist's OTHER albums
harder) could never actually fire, since an excluded artist can never
reach the entry-point-selection step in the first place. Corrected to
the rule above - only disliked artists are excluded outright; liked
artists stay reachable specifically so the album-level boost has
something to apply to if the artist resurfaces with a different
suggested album.

**Why this is synchronous, not async like the rest of the cache
layer.** `ArtistDataCache` is fully async with a semaphore, built to
handle the discovery walk's high-fanout concurrent fetching (dozens of
simultaneous Last.fm calls per hop). Feedback reads/writes are
low-frequency, user-initiated actions (a single click), not a workload
that needs that complexity - a plain synchronous SQLAlchemy session is
the proportionate choice here, not an oversight.

</details>

<a id="the-niche-ness-slider"></a>
<details>
<summary><strong>The niche-ness slider</strong></summary>

How obscure should recommendations be, independent of how far the walk
conceptually wanders (hop distance). These had been treated as the same
thing earlier in this project's history, but they're not: a hop-2 result
can be either extremely mainstream or extremely obscure depending on
which path it took, so "niche-ness" needed its own dial.

**Three real options were considered.** (1) Tune `min_listeners`
directly - more intuitive to explain, but a hard cutoff, the same
bluntness that caused the original 0-listener bug this project already
fixed once; an artist would abruptly appear or vanish between slider
notches rather than gently reordering. (2) Build a new blended composite
score (popularity + hop distance + connectivity) - conceptually the most
"correct," but exactly the kind of fresh, untested composite that's
needed multiple real-data debugging rounds every other time it was tried
in this project (see the cluster-fairness saga above). (3) Tune the
existing popularity-factor floor (previously a fixed 0.3) - reuses a
mechanism already proven to handle graduated, fair comparison (the
Bayside case), at the cost of being a less intuitive lever to name to an
end user.

Went with option 3, on the reasoning that reusing validated machinery
has a much better track record in this project than building new scoring
logic from scratch, and the "needs an intuitive name" gap is a UI
labeling problem, not a mechanism problem.

**The mapping.** `niche_level` (0.0-1.0, default 0.5) maps to the
popularity floor via a piecewise-linear function chosen so that 0.5
reproduces the original validated floor of 0.3 EXACTLY - verified
algebraically, not just approximately, since every prior debugging-
history result in this project was tested against that specific value
and changing it even slightly would have silently invalidated months of
prior validation. Below 0.5 the floor rises toward 0.9 (popularity barely
penalized); above 0.5 it falls toward 0.0 (full sensitivity to
obscurity). Verified against a real profile at both extremes: at 0.0,
results included PJ Harvey (2.2M listeners), The Cardigans (3.7M), and
Lana Del Rey (5.3M); at 1.0, the same seeds produced Kingcrow (35K),
Feline (50K), and MODyssey (18K) - a real, visible shift in the intended
direction, with hop distance staying fixed at 2 throughout, confirming
the two axes are genuinely independent rather than one quietly dragging
the other.

Only the midpoint (0.3) has the same depth of historical validation as
the rest of this project's scoring formulas; the endpoints are a
reasonable extrapolation of the same mechanism, checked once against one
real profile, not independently proven the way 0.3 was across many test
cases.

</details>

<a id="debugging-the-multi-album-input"></a>
<details>
<summary><strong>Debugging the multi-album input</strong></summary>

The first real test run with 3 seed albums surfaced two bugs immediately.

**Every result came from the 3 seed artists' own discographies.** Root
cause: a classic Python late-binding closure bug inside a loop. The
`fetch_group_tags` inner function captured `artist_name` as a free
variable from the enclosing loop - by reference, not by value. Since
`asyncio.gather` runs all closures concurrently AFTER the loop has
already finished advancing, every closure was silently using whichever
`artist_name` happened to be LAST, corrupting tag data for every
similar-artist candidate. They all scored poorly and were filtered out,
while only the seed artists (processed separately with correct scoping)
survived. Fixed by binding `artist_name` as a default argument
(`artist_name=artist_name`), forcing early binding at closure-creation
time. The same fragile pattern was found and proactively fixed in
`lookup.py`'s single-album version too, where it happened to work
correctly by accident of execution order but was one refactor away from
the same failure.

**A seed album recommended itself back.** "Riot!" (typed by the user)
was recommended back as "RIOT!" because Last.fm's autocorrect resolved
the input to its actual catalogue capitalization, but the exact-string
seed-exclusion check compared against the raw user input, and the
mismatch slipped through. Rather than patch name-matching to handle
capitalization, seed artists' own albums are now excluded from the
candidate pool entirely upstream - fixing the self-recommendation bug
and the "seed artists' other albums are lower-value than fresh
discoveries" design concern at the same time.

</details>

## Tech stack

Python, FastAPI (planned, for the deployed service layer), httpx (async
Last.fm client), networkx (similarity graph + walk traversal), SQLAlchemy
+ SQLite (artist-graph/tag/info cache, plus per-user feedback storage).

## Setup

```
cp .env.example .env
# add your Last.fm API key to .env - get one free at
# https://www.last.fm/api/account/create
pip install -r requirements.txt
```

Smoke tests (require a real Last.fm username with public listening
history):

```
python -m tests.test_lastfm_client <username>
python -m tests.test_taste_profile <username>
python -m tests.test_artist_graph <username>
python -m tests.test_artist_cache <artist_name>
python -m tests.test_discovery_walk <username> [target_hop_distance] [max_depth] [max_hops] [niche_level]
python -m tests.test_album_selection <artist_name> [<artist_name> ...]
python -m tests.test_album_outliers <artist_name>
python -m tests.test_lookup artist <artist_name>
python -m tests.test_lookup album <artist_name> <album_name>
python -m tests.test_rediscover <username>
python -m tests.test_feedback artist <username> <artist_name> <liked|disliked>
python -m tests.test_feedback album <username> <artist_name> <album_name> <liked|disliked>
python -m tests.test_feedback show <username>
python -m tests.test_multi_seed <artist1> <artist2> ... (up to 7) [--niche 0.0-1.0]
python -m tests.test_multi_album "Artist1::Album1" "Artist2::Album2" ... (up to 7)
```

`test_discovery_walk` is the main end-to-end test: builds a real taste
profile, seeds the walk from top artists plus any detected outliers,
expands through the cached similarity graph, scores and diversity-caps
the results, and attaches a suggested entry-point album to each final
recommendation - now also factoring in any recorded like/dislike
feedback for that username. `test_album_outliers` exercises the shelved
album-outlier mode - functional, but not considered production-ready.

See `tests/debug/README.md` for the diagnostic scripts referenced in the
debugging history above.
