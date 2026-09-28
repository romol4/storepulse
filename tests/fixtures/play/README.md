# Google Play fixtures: SYNTHETIC

**These files are synthetic, not real reports.** They were written by hand to follow
Google's documented formats:
- the column names
- UTF-16 encoding for the installs CSVs
- zipped CSVs for sales and earnings
- the Reporting API JSON shapes

Replace them with scrubbed real files (package names, order numbers and titles replaced)
as soon as possible, and update the expected totals in `tests/test_play_*.py`.

All data is for `com.example.billft`.

| File | Contents |
| --- | --- |
| `installs_com.example.billft_202609_overview.csv` | UTF-16 with BOM. Sep 1–3, 2026. Day 3 has an empty `Daily User Uninstalls` cell, which means missing, not zero. |
| `installs_com.example.billft_202609_country.csv` | UTF-16 with BOM. Same days, split by US and DE (only US on day 3). |
| `installs_empty_overview.csv` / `installs_malformed_overview.csv` | Header only, and a file missing the `Date` column. |
| `salesreport_202609.zip` | Sales rows covering these cases: <ul><li>charged</li><li>refunds recorded both positive and negative</li><li>a cancelled order (ignored)</li><li>an unknown package</li><li>a row dated in August (outside the month)</li><li>a thousands separator in a price</li></ul> |
| `salesreport_empty.zip` / `salesreport_malformed.zip` | Header only, and missing columns. |
| `earnings_202608_1234567890-0.zip` | August earnings rows covering these cases: <ul><li>charge, Google fee and tax lines</li><li>`Aug 3, 2026` and `August 17, 2026` date formats</li><li>a row with no package</li><li>a row dated in July</li></ul> |
| `earnings_empty.zip` / `earnings_malformed.zip` | Header only, and missing columns. |
| `vitals_crash_query.json` / `vitals_anr_query.json` | `:query` responses for Sep 20–22. Day 22 has no daily crash rate. |
| `vitals_freshness.json` | Metric-set freshness. DAILY `latestEndTime` is Sep 23, so data is complete through Sep 22. |
