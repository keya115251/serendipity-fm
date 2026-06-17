# Debug scripts

These are not unit tests. They're diagnostic scripts written during actual
development to track down real bugs in the outlier-detection logic
(`TasteProfileBuilder._tag_relevance_scores` and `_combine_outlier_signals`
in `app/core/taste_profile.py`).

Kept here deliberately rather than deleted, because the debugging process
itself is part of the project's story: the first two outlier-detection
approaches (rank-percentage tag cutoff, then a naive zero-overlap check)
both failed silently against real listening data, and these scripts are
what surfaced *why*, with actual numbers, rather than guesswork. See the
"Debugging the outlier detector" section in the main README for the full
narrative these scripts were written to support.

Run with (from the project root, with `.env` configured and deps installed):

```
python -m tests.debug.show_full_top_artists <lastfm_username>
python -m tests.debug.debug_txt_tags <lastfm_username>
python -m tests.debug.debug_combine <lastfm_username>
```

`show_full_top_artists.py` - prints a user's full top 25 artists for both
`overall` and `12month` periods side by side. Originally used to confirm
that an artist with a high `overall` playcount can be almost entirely
historical (e.g. a K-pop phase from years ago), which is why the taste
profile builder uses `12month` as the long-term layer instead of `overall`.

`debug_txt_tags.py` - runs the real `TasteProfileBuilder` tag-aggregation
pipeline and prints the actual dominant tag set it computes, plus the
overlap math for two specific artists. Originally used to catch a bug
where a rank-percentage cutoff for "dominant tags" pulled in far more
(noisier) tags than expected once the unique tag pool grew past ~70 tags.

`debug_combine.py` - runs the full `build()` pipeline up to (but not
including) the final outlier decision, and prints the tag-relevance score
and graph-connectivity score side by side for every artist, plus the
actual output of `_combine_outlier_signals()`. Used to verify the two
signals were combining correctly and to calibrate the `tag_threshold` and
`connectivity_threshold` values against real observed score distributions
rather than arbitrary numbers.
