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
  thread within a profile (used later to seed alternate starting points
  for the discovery walk, rather than always wandering from the dominant
  cluster).

The recommendation engine itself (the actual walk-and-score logic) is the
next piece under active development.

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

## Tech stack

Python, FastAPI (planned, for the deployed service layer), httpx (async
Last.fm client), networkx (similarity graph + future walk logic), SQLite
(planned, for a persistent artist-graph cache), scikit-learn (planned, for
the content-embedding side of the hybrid recommendation engine).

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
```

See `tests/debug/README.md` for the diagnostic scripts referenced in the
debugging narrative above.
