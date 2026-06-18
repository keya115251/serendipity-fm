# serendipity-fm

A music recommender built on Last.fm data that optimizes for genuine
discovery rather than obvious similarity.

## The problem with most recommenders

Standard recommenders rank candidates by predicted relevance alone, which
naturally converges on whatever is already popular and adjacent to a
listener's existing taste. Ask a typical system what to listen to next
based on an artist you love, and it hands back the five most famous,
most-tagged-similar acts in that genre - things you'd have found anyway
through radio, ads, or friends.

This project optimizes for a second axis alongside relevance:
unexpectedness. The core idea is to walk a moderate distance away from a
listener's dominant taste cluster, rather than recommending from directly
inside it, while still requiring a relevance floor so the result is
discovery rather than noise.

## Architecture

- **Last.fm client** (`app/core/lastfm_client.py`) - async wrapper around
  Last.fm's public read API. Capped concurrency (semaphore + connection
  pool limits) and automatic retry on timeout, since early testing hit
  real `ConnectTimeout` errors under a burst of ~25 simultaneous requests.
- **Persistent artist data cache** (`app/db/`) - SQLite-backed cache for
  artist info, tags, and similarity edges, sitting between the live
  Last.fm client and everything else. Exists because hop-expansion fans
  out fast: a single seed with a similarity limit of 20 can produce
  hundreds of candidates within two hops, each potentially needing its
  own info/tags/similarity lookups, which isn't viable to re-fetch live on
  every request.
- **Taste profile builder** (`app/core/taste_profile.py`) - builds a
  two-layer profile per user: a `long_term` layer from the last 12 months
  of top artists, and a `short_term` layer from recent tracks. See below
  for why "overall" history was deliberately rejected as the long-term
  basis.
- **Artist similarity graph** (`app/core/artist_graph.py`) - builds a
  graph restricted to a user's own artist set, using Last.fm's
  `artist.getSimilar` data, and scores each artist's connectivity to the
  rest of the user's profile.
- **Outlier detection** - combines tag-based relevance and graph
  connectivity to flag artists that represent a genuinely different taste
  thread within a profile.
- **Shared tag relevance utility** (`app/core/tag_relevance.py`) - the
  document-frequency-discounted dominant-tag-set logic, extracted into
  its own module so outlier detection and the discovery walk both use the
  identical, validated implementation instead of two copies that could
  drift apart.
- **Seed selection** (`app/core/seeding.py`) - builds the discovery
  walk's seed list from a mix of top-by-playcount artists and detected
  outliers, so a real secondary taste cluster gets explored even if none
  of its artists crack the top N by raw playcount.
- **Discovery walk** (`app/core/discovery_walk.py`) - the actual
  recommendation engine. Expands outward from seed artists through the
  cached similarity graph several hops deep, tracks the full path from
  seed to candidate (for explainability and debugging), and scores
  candidates on tag relevance to their own originating cluster, hop
  distance, and a cluster-relative popularity factor. A final
  `diversify_top_n` selection step caps how much of the output any single
  cluster can claim, with each cluster's allocation scaled by how much
  current listening signal it actually represents. See "Debugging the
  discovery walk" below for the multi-stage process that got this design
  to its current state.

## Why two taste layers, and why 12-month, not all-time

Last.fm's `overall` period reports all-time playcounts, which conflates
genuinely stable current taste with phases a listener has since moved
past entirely. In real testing, one test profile showed an artist with
787 all-time plays but only 122 in the last 12 months - clearly a closed
chapter, not part of current identity, yet `overall` weighted it almost as
heavily as artists still in active rotation. Switching the long-term
layer to the `12month` period fixed this directly. `overall` data is still
worth using eventually for an explicit, opt-in "rediscover an old phase"
mode, but it should never silently feed the default recommendation logic.

## Debugging the outlier detector

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

## Debugging the discovery walk

Once the walk was built and producing real recommendations, two more
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
than something to keep guessing at blind. This is logged as a known,
accepted limitation rather than fully solved - see the "Tradeoffs" note
below.

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
The cause: every candidate, regardless of which seed it came from, was
still scored against ONE global dominant-tag profile - which, for a
prog/metal-dominant listener, was overwhelmingly prog/metal. A candidate
discovered via the My Chemical Romance seed was being asked "does this
fit the listener's overall aggregate taste" instead of "does this fit the
MCR-adjacent cluster specifically," and structurally lost every time.
Fixed by scoring each candidate against its OWN originating cluster's tag
profile (the global profile for primary seeds, that artist's own tags for
outlier seeds), using `candidate.path[0]` to know which cluster a
candidate actually came from.

**The cross-cluster popularity problem.** Cluster-aware tag scoring
fixed relevance, but emo-cluster candidates were still losing to
prog-cluster ones, because pop-punk/emo as a genre simply has more
mainstream-popular acts (millions of listeners) than underground prog
does - so even a great-fitting emo candidate (Bayside, the strongest tag
match in its cluster) was scoring far below niche prog candidates purely
on an absolute popularity scale that didn't mean the same thing across
genres. First attempted percentile-rank-within-cluster (same principle as
the connectivity-score fix from outlier detection), but this just
relocated the unfairness: the single most-mainstream member of any
cluster gets a raw percentile of 0 regardless of how well it fits,
zeroing out its score entirely. The actual fix combined a floor (a
candidate's popularity contribution can never go below 30% regardless of
percentile) with a dampening exponent (square root) on the remainder, so
popularity differentiates between similar-fit candidates without being
able to override a genuinely strong tag match. Verified directly: Bayside
went from not appearing in the top 10 at all, to scoring 4th overall,
ahead of three of the five prog/metal candidates that previously
dominated the list outright.

**The outlier-dominance problem.** Fixing cross-cluster fairness swung
the bias the other way for a different profile: a listener whose primary
cluster was alt/indie, with two detected outlier seeds (a K-pop group and
a prog rock act), ended up with 4 of the top 5 recommendations being
K-pop - the outlier cluster's candidates happened to sit at a more
favorable point in their own narrow popularity distribution, and nothing
in the scoring pipeline capped how much of the final output any one
cluster could claim. Added `diversify_top_n`: a cap on max recommendations
per cluster, plus a guarantee that every cluster with a viable candidate
gets at least one slot (so a weak-but-real secondary cluster can't
silently drop to zero just because it scored worse that particular run -
the same failure outlier seeding was built to prevent in the first place,
just resurfacing one stage later). This still wasn't quite right on its
own: every detected outlier was getting the identical guaranteed slot and
up-to-3 ceiling regardless of how much actual CURRENT signal it
represented. For one listener, the K-pop outlier was almost entirely
historical (the same stale-history pattern documented above - 787
all-time plays, only 122 in the last 12 months), yet it received the same
allocation as an outlier the listener still actively engaged with. Final
fix: each outlier's slot allocation is now scaled by its 12-month
playcount relative to the listener's most-played artist, and a cluster
below a minimum relevance threshold loses the guarantee entirely (though
it can still earn slots on pure score merit). The near-dormant K-pop
cluster went from 2 of 7 final recommendations down to 1, while a more
active secondary cluster in the same run kept its full guaranteed slot.

**Tradeoffs accepted rather than further chased.** A candidate reachable
from more than one seed only keeps the FIRST path encountered during
traversal, which is order-dependent rather than necessarily "the most
meaningful" path - this is a real simplification, not a fully solved
design. The weak-seed problem (load-bearing via-artists a listener barely
knows) is mitigated by path visibility, not eliminated; a real fix would
likely need per-hop relevance weighting keyed to something other than
aggregate tag similarity, which was tried once, didn't change results,
and was deprioritized in favor of the bigger, more clearly-broken
cluster-fairness issues above. Separately, the diversity cap operates at
the seed-cluster level only - two final recommendations from the same
primary cluster can still share the same hop-1 intermediate artist (e.g.
both reached via the same bridge artist), which is the same
one-branch-dominating pattern recurring at a smaller scale within a
single cluster, not yet addressed.

## Tech stack

Python, FastAPI (planned, for the deployed service layer), httpx (async
Last.fm client), networkx (similarity graph + walk traversal), SQLAlchemy
+ SQLite (persistent artist-graph/tag/info cache), scikit-learn (planned,
for the content-embedding side of the hybrid recommendation engine).

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
python -m tests.test_discovery_walk <username> [target_hop_distance] [max_depth] [max_hops]
```

`test_discovery_walk` is the main end-to-end test: builds a real taste
profile, seeds the walk from top artists plus any detected outliers,
expands through the cached similarity graph, and prints the top 7 scored
recommendations with their full discovery path and genre tags.

See `tests/debug/README.md` for the diagnostic scripts referenced in the
debugging narrative above.
