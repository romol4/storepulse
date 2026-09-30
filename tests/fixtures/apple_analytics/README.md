# Apple analytics report segment fixtures — SYNTHETIC

**These files are synthetic, not real report segments.** No real App Store Connect
account was reachable when Phase 6a was built, and the exact column names below are a
best-effort guess pending verification against a real segment (see
`core/sources/apple_analytics.py`'s module docstring). They were written by hand to
follow the tab-separated, gzip-compressed shape every other Apple report in this
codebase uses.

Replace them with a scrubbed real segment (identifiers and names replaced) once one is
available, correct `REQUIRED_COLUMNS`/column names in `core/sources/apple_analytics.py`
to match, and update the expected totals in `tests/test_apple_analytics.py`.

| File | Contents |
| --- | --- |
| `segment_normal.tsv.gz` | Two apps (`deskFT` 1000000001, `billFT` 1000000002) across US/GB/blank-territory rows, plus an app not yet known to Storepulse (unmapped) and a blank `Territory` (maps to `ZZ`). |
| `segment_empty.tsv.gz` | Header only, zero rows. |
| `segment_malformed.tsv.gz` | Missing the `Territory` and `Page Views` columns. |
