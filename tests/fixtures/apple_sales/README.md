# Apple sales report fixtures — SYNTHETIC

**These files are synthetic, not real reports.** No scrubbed real reports were
available when Phase 1 was built, so they were written by hand to follow
Apple's documented SALES / SUMMARY (daily) layout: the same column names, tab
separation, gzip compression, and product type identifiers.

Replace them with scrubbed real reports (identifiers, titles and SKUs replaced)
as soon as possible, and update the expected totals in
`tests/test_apple_sales.py` to match.

| File | Contents |
| --- | --- |
| `summary_normal.tsv.gz` | Two apps (`deskFT` 1000000001 / SKU `DESKFT`, `billFT` 1000000002 / SKU `BILLFT`) on 2026-09-20: first-time downloads (`1F`, `1`, `1T`, `F1`), a redownload (`3`), an update (`7`), IAP purchases and a refund (`IA1`), an auto-renewable subscription (`IAY`), an unknown code (`ZZ9`), an app bundle (`1-B`) and an IAP whose parent app is unknown. Currencies USD, GBP, CAD, EUR. |
| `summary_empty.tsv.gz` | Header only, zero rows. |
| `summary_malformed.tsv.gz` | Missing the `Units` and `Currency of Proceeds` columns. |
