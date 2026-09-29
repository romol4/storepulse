# Apple subscription event report fixtures — SYNTHETIC

**These files are synthetic, not real reports.** LogicFT's FT apps have no confirmed
subscription products, so no real report was available when Phase 3b was built. They
were written by hand to follow Apple's documented SUBSCRIPTION_EVENT / SUMMARY
(version 1_3) layout: the same column names, tab separation, and gzip compression.

Replace them with scrubbed real reports (identifiers and names replaced) once a
subscription-bearing app is available, and update the expected totals in
`tests/test_apple_subscription_events.py` to match.

| File | Contents |
| --- | --- |
| `summary_normal.tsv.gz` | Two apps (`deskFT` 1000000001, `billFT` 1000000002): churn events (`Cancel`, `Refund`, `Canceled from Billing Retry`) across US/GB/blank-country segments, a non-churn recognized event (`Renew`), an unrecognized event (`SomeFutureEvent`), and a `Cancel` for an app not yet known to Storepulse (unmapped). |
| `summary_empty.tsv.gz` | Header only, zero rows. |
| `summary_malformed.tsv.gz` | Missing the `Country` and `Quantity` columns. |
