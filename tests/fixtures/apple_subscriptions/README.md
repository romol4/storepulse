# Apple subscription report fixtures — SYNTHETIC

**These files are synthetic, not real reports.** LogicFT's FT apps have no confirmed
subscription products, so no real report was available when Phase 3b was built. They
were written by hand to follow Apple's documented SUBSCRIPTION / SUMMARY (version 1_3)
layout: the same column names, tab separation, and gzip compression.

Replace them with scrubbed real reports (identifiers and names replaced) once a
subscription-bearing app is available, and update the expected totals in
`tests/test_apple_subscriptions.py` to match.

| File | Contents |
| --- | --- |
| `summary_normal.tsv.gz` | Two apps (`deskFT` 1000000001, `billFT` 1000000002) across US/GB/blank-country segments: standard-price, promotional-offer, offer-code and win-back paid columns, a free-trial-only segment, `Marketing Opt-Ins`/`Billing Retry`/`Grace Period` counts, an app not yet known to Storepulse (unmapped), and a blank `Country` (maps to `ZZ`). |
| `summary_empty.tsv.gz` | Header only, zero rows. |
| `summary_malformed.tsv.gz` | Missing the `Country` and `Active Standard Price Subscriptions` columns. |
