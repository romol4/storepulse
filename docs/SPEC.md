# Storepulse — open-source App Store + Google Play stats collector (spec)

Sep 25, 2026 · @Oleg

## Overview and goals

Storepulse (working name) is an open-source tool any developer can run to pull their own App Store Connect and Google Play data into one place. It comes in two modes: **local** sends a daily email; **hosted** (self-hosted on the user's own server) sends the email and serves a web dashboard. LogicFT's FT apps are the first user.

**Goals**

- One place to see installs, proceeds, and stability for all of a developer's apps on iOS and Android.
- Safe credential handling: keys are entered once through a guided setup, stored encrypted or in the OS keychain, never logged, and only ever sent to Apple, Google, and the user's own mail server.
- Zero-config app list: apps are discovered from the credentials, not typed in.
- Fully automated: one scheduled run per day, idempotent re-pulls.
- Easy install: `pipx install storepulse` for local, one `docker compose up` for hosted.

**Non-goals (v1)**

- A shared multi-tenant SaaS that stores other people's store keys (see Security model).
- Real-time data. Store reports lag one to three days by design.
- Ad attribution, cohort analysis, or in-app event analytics.
- Replacing RevenueCat's own dashboards.

## Open source project

- **License:** Apache-2.0 (patent grant, business-friendly); see Open decisions.
- **Repo:** public GitHub repo; LogicFT's private git remains the dev mirror if preferred. Releases to PyPI and a Docker image on GHCR.
- **Docs:** README with a 10-minute quickstart per mode, plus step-by-step guides with screenshots for creating the Apple API key and the Google service account (the hardest part for new users).
- **Hygiene:** `SECURITY.md` with a private disclosure address, `CONTRIBUTING.md`, CI running tests and linting on every PR, pinned dependencies with Dependabot, signed release tags.
- **No telemetry.** Nothing phones home, including version checks, unless the user opts in.

## Security model

The tool holds keys that can read a developer's sales data, so credential handling is the core design constraint.

- **Least privilege.** Setup guides request only read roles: Sales and Reports on Apple, *View app information and download bulk reports* on Play. Android revenue additionally needs Play's *View financial data, orders, and cancellation survey responses* (global). It is **optional**: it also exposes order details and buyers' city, state and postcode (Storepulse keeps only the country), so without it Storepulse still collects installs and vitals and `doctor` warns that Android revenue is off. The docs say plainly what each key and permission can and cannot do.
- **Local mode:** secrets go in the OS keychain (macOS Keychain, Windows Credential Manager, Linux Secret Service) via the `keyring` library. On headless machines without a keychain, they go in an encrypted file unlocked by a passphrase or an env var.
  - Secrets over 1 KB, such as a service-account JSON, exceed Windows Credential Manager's size limit.
  - These are AES-GCM encrypted into `keychain-wrapped.json` under a random key kept in the keychain, and the keychain entry holds only a marker.
- **Hosted mode:** secrets are uploaded once through the setup page, encrypted at rest with AES-GCM under a master key supplied only by environment variable or Docker secret, and never displayed again (the UI shows a fingerprint, the upload date, and a Test connection button).
- **Everywhere:** secrets are redacted from logs and error messages; raw report caches contain no credentials; the database without the master key reveals no keys.
- **Why not a shared SaaS:** a service holding thousands of developers' store keys is a high-value target and a liability. Self-hosting keeps each user's keys on infrastructure they control.

## Architecture

One shared core (collectors, SQLite, digest) is wrapped by two thin shells: a CLI for local mode and a web app for hosted mode.

```mermaid
flowchart TD
  A[App Store Connect] --> C[Core: collectors]
  B[Play bulk reports bucket] --> C
  V[Play Reporting API vitals] --> C
  R[RevenueCat, optional] -.-> C
  C --> D[(SQLite)]
  D --> E[Email digest]
  D --> W[Web dashboard - hosted only]
  S[Secret store: keychain or encrypted DB] --> C
```

|  | Local mode | Hosted mode |
| --- | --- | --- |
| Runs on | the user's Mac, Windows or Linux machine | the user's own VPS or home server |
| Install | `pipx install storepulse` | `docker compose up -d` |
| Setup | `storepulse init` terminal wizard | first-run web setup page |
| Secrets | OS keychain, or passphrase-encrypted file | AES-GCM in SQLite, master key from env |
| Scheduling | `storepulse schedule install` writes a cron, launchd or Task Scheduler entry | built-in scheduler in the container |
| Output | daily email | daily email + dashboard |
| Auth | none (single machine user) | one admin account, password + optional TOTP |

**Package layout**

```
storepulse/
  core/
    sources/apple_client.py  apple_sales.py  apple_analytics.py  google_auth.py  google_client.py
            play_common.py  play_installs.py  play_sales.py  play_earnings.py  play_vitals.py  revenuecat.py
    discovery.py      list apps from each credential
    config.py         non-secret settings (config.toml) and local paths
    db.py             schema, migrations, upserts
    secrets.py        SecretStore interface: KeyringStore, EncryptedFileStore, DbEncryptedStore
    digest.py         builds the email (HTML + plain text)
    mailer.py         SMTP sender
    runner.py         one run: collect → store → digest
  cli/                init wizard, run, backfill, status, schedule
  web/                FastAPI app: setup, login, dashboard, settings
  docker/             Dockerfile, compose.yaml, Caddy example
```

Local mode keeps its data in the platform's user data dir (via `platformdirs`); hosted mode uses a mounted `/data` volume.

## Data sources and auth

Four sources, each behind its own module in `core/sources/`. Each is optional: a user with only Android apps sets up only Google. Endpoint names and CSV columns below reflect the APIs as of mid-2026; verify them against current Apple and Google docs before coding each module.

**App discovery.** After credentials are saved, `discovery.py` lists apps automatically: `GET /v1/apps` on App Store Connect, and the Reporting API's app search on Play. Apps with the same name on both platforms are auto-paired into one logical app; users can rename, re-pair, or hide apps in settings.

| Source | Gives us | Auth | Freshness |
| --- | --- | --- | --- |
| App Store Connect sales reports | units, proceeds, by app and country | ES256 JWT from .p8 key | \~1 day lag |
| App Store Connect analytics reports | impressions, page views, downloads, sessions | same JWT | \~1–2 day lag |
| Play Console bulk reports bucket | installs, uninstalls, earnings, sales | service account, GCS read | 1–3 day lag, revised within month |
| Play Developer Reporting API | crash rate, ANR rate | same service account | \~2 day lag |
| RevenueCat API v2 (optional) | MRR, active subs, trials | secret API key | near real time |

### Apple: App Store Connect

- Create an API key in App Store Connect (Users and Access → Integrations) with the Sales and Reports role, plus App Manager or Admin if the analytics report requests must be created by the collector. Store the `.p8`, key ID, issuer ID, and vendor number.
- Sign a JWT per run: header `alg: ES256`, `kid: <key id>`; payload `iss: <issuer id>`, `aud: appstoreconnect-v1`, `exp` ≤ 20 minutes out.
- **Sales:** `GET /v1/salesReports` with `filter[frequency]=DAILY`, `filter[reportType]=SALES`, `filter[reportSubType]=SUMMARY`, `filter[vendorNumber]`, `filter[reportDate]=YYYY-MM-DD`. Response is a gzipped TSV. Map app rows to apps by Apple Identifier and in-app purchase rows by Parent Identifier (the parent app's SKU, kept in `kv` as `apple_sku:<sku>`); split by Product Type Identifier into first-time downloads (`installs`), redownloads, and in-app purchases (`iap_units`). Update rows and `IA3` (restored in-app purchase) rows add no unit metric. An empty Country Code is stored as `ZZ`, never `ALL`. When a day's in-app purchase rows arrive before their parent app is known, they are re-mapped at the end of the run, once a later day has registered the parent. Unrecognized product types are logged and counted; their proceeds still count. Apps seen in app rows but not in discovery are added automatically; IAP rows never add apps.
  - **404s:** a 404 whose detail says there were no sales is stored as a zero-sales day (clears that day's rows). "Not available yet" is `not_ready`. Any other 404 is `not_ready` for dates up to 2 days old and inferred no-sales for older dates; an inferred no-sales day never deletes stored rows (it logs a warning instead).
  - **Retention:** Apple keeps daily reports for about a year, so backfills are clamped to the last 365 days (Pacific time); earlier dates are skipped with a warning and never recorded.
  - **Auth:** the JWT is re-signed after 15 minutes so long backfills never run on an expired token.
- **Subscriptions:** same endpoint with `reportType=SUBSCRIPTION` / `SUBSCRIPTION_EVENT` for apps with subscriptions.
- **Analytics:** one-time setup per app: `POST /v1/analyticsReportRequests` with `accessType: ONGOING`. Daily: list the request's reports, pick the needed ones (downloads, discovery and engagement), fetch `DAILY` instances, download segment files (gzipped CSV). Store the request IDs in the DB so setup never repeats.

### Google: Play Console bulk reports

- Create a GCP service account; invite its email in Play Console → Users and permissions with *View app information and download bulk reports* for all apps. Download its JSON key.
- Bucket URI comes from Play Console → Download reports → Copy Cloud Storage URI (`gs://pubsite_prod_…`).
- Files to read:
  - `stats/installs/installs_<package>_<YYYYMM>_overview.csv` and `_country.csv`: daily installs, uninstalls, active devices.
  - `earnings/earnings_<YYYYMM>_*.zip`: transaction-level earnings in payout currency.
  - `sales/salesreport_<YYYYMM>.zip`: orders, if earnings lag too far.
- Gotcha: the installs CSVs are UTF-16 encoded. The current month's file is rewritten daily, so always re-parse it whole.
- **Installs:**
  - `installs` = Daily User Installs, `uninstalls` = Daily User Uninstalls, `active_devices` = Active Device Installs. These match Play Console's user acquisitions and user losses, not device acquisitions.
  - The overview file gives the `ALL` rows and the country file gives per-country rows. Both are written in one replacement per app and month.
  - An empty cell is missing data, not zero.
- **Proceeds:**
  - `play_sales` writes `sales_gross`: provisional gross sales from the daily-updated sales report. Item price excluding tax, in the buyer's currency, before Google's fee. `Refund` rows subtract the absolute item price. Never stored as `proceeds`, which is net for every source.
  - `play_earnings` writes `proceeds`: final net proceeds from the monthly earnings report. Merchant currency, including fee and tax lines. Rows dated outside the file's month are moved to the month's nearest edge (never dropped), so the month still reconciles with the payout. Rows with no known package are skipped, and their amount per currency is recorded in the ingest note.
  - Loading a month's earnings deletes that month's `play_sales` rows in the same transaction, and later sales re-pulls skip the month.
  - Both need the optional financial permission. When an account-wide prefix (`sales/`, `earnings/`) lists no files at all, collection warns and names that permission instead of silently skipping.
- **Discovery:** every Play collection re-runs app discovery (`apps:search`, falling back to the packages in the bucket's installs file names), so apps launched after setup are collected. `doctor` registers the apps it sees.
- **Missing months:**
  - Months before the first file are skipped, with nothing logged. For installs the first file is per app; sales and earnings files cover the whole account, so their first file is account-wide.
  - A missing current month is `not_ready`, as is last month's earnings before the 15th. Any other missing past month is logged `ok` with 0 rows and a "no file" note.

### Google: Play Developer Reporting API (vitals)

- Scope `https://www.googleapis.com/auth/playdeveloperreporting`, same service account.
- Query `crashRateMetricSet` and `anrRateMetricSet` per package with a DAILY timeline; keep user-perceived crash and ANR rates plus distinct users.
  - Also keep the 28-day user-weighted rates (`userPerceivedCrashRate28dUserWeighted`, `userPerceivedAnrRate28dUserWeighted`). Google's bad-behavior thresholds apply to these, and alerts must use them. Daily rates for small apps swing too much.
- Check each metric set's freshness before requesting the latest days. DAILY `latestEndTime` is exclusive, and later days are `not_ready`.
- **Auth:** the service account JSON is exchanged for an OAuth token directly: an RS256 JWT assertion to `token_uri`. A 403 after a successful token exchange means Play Console hasn't granted the permission yet, which can take 24–48 hours for a new account.

### RevenueCat (optional)

- Secret v2 API key, read-only. Pull the project metrics overview once per run and store it as a dated snapshot (MRR, active subscriptions, active trials, revenue).

## Storage

One SQLite file (`storepulse.db` in the user data dir locally, `/data/storepulse.db` hosted; WAL mode) with a long, narrow fact table, so adding a metric never needs a migration.

```sql
CREATE TABLE apps (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,            -- 'deskFT'
  platform    TEXT NOT NULL CHECK (platform IN ('ios','android')),
  store_id    TEXT NOT NULL,            -- Apple ID or package name
  UNIQUE (platform, store_id)
);

CREATE TABLE daily_metrics (
  date        TEXT NOT NULL,            -- YYYY-MM-DD, store's reporting day
  app_id      INTEGER NOT NULL REFERENCES apps(id),
  country     TEXT NOT NULL DEFAULT 'ALL', -- ISO 3166 alpha-2 or 'ALL'
  metric      TEXT NOT NULL,
  currency    TEXT NOT NULL DEFAULT '', -- '' for non-money metrics
  value       REAL NOT NULL,
  source      TEXT NOT NULL,            -- 'apple_sales', 'play_installs', …
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (date, app_id, country, metric, currency)
);

CREATE TABLE snapshots (               -- point-in-time values (RevenueCat)
  taken_at TEXT NOT NULL, app_id INTEGER, metric TEXT NOT NULL,
  currency TEXT NOT NULL DEFAULT '', value REAL NOT NULL,
  PRIMARY KEY (taken_at, app_id, metric, currency)
);

CREATE TABLE ingest_log (
  id INTEGER PRIMARY KEY, source TEXT NOT NULL, report_date TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('ok','not_ready','error')),
  rows INTEGER, error TEXT,
  app_id INTEGER REFERENCES apps(id)  -- set for a per-app source (play_installs, play_vitals);
                                       -- NULL for an account-wide one (apple_sales, play_sales,
                                       -- play_earnings), where report_date alone identifies a pull
);

CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);  -- analytics request IDs, Apple SKU map, schema version
```

**Metric vocabulary** (fixed list in `db.py`; unknown metrics are rejected):

| Metric | Unit | Sources | Over several days |
| --- | --- | --- | --- |
| `installs` | count | apple\_sales (first-time), play\_installs | sum |
| `redownloads` | count | apple\_sales | sum |
| `uninstalls` | count | play\_installs | sum |
| `active_devices` | count (snapshot) | play\_installs | last value |
| `proceeds` | money, net | apple\_sales, play\_earnings | sum (per currency) |
| `sales_gross` | money, gross, provisional | play\_sales | sum (per currency) |
| `iap_units` | count | apple\_sales | sum |
| `impressions`, `page_views` | count | apple\_analytics | sum |
| `crash_rate`, `anr_rate` | ratio 0–1, daily | play\_vitals | mean weighted by `vitals_users` |
| `crash_rate_28d`, `anr_rate_28d` | ratio 0–1, 28-day user-weighted | play\_vitals | last value (already a 28-day window) |
| `vitals_users` | count (distinct users in the crash-rate metric set; a snapshot, not additive) | play\_vitals | last value |

**Rules**

- Every write is `INSERT … ON CONFLICT DO UPDATE`, so re-pulls overwrite and never duplicate.
- A re-pull for one source and date replaces that source's rows for that date inside one transaction (delete, then insert) so countries that dropped to zero don't linger.
- Money is stored in the original currency. The digest lists proceeds per currency; a converted total is shown only when `config.toml` sets fixed rates and a display currency (Phase 3 decision — see Open decisions).
- `country = 'ALL'` rows are written only where the source gives a total; otherwise the renderer sums countries. Per source: `play_installs` writes both, and readers use its `ALL` row and never add the countries to it; `apple_sales` writes no `ALL` rows, so readers sum its countries. Unknown countries are `ZZ`, never `ALL`.
- Month-based sources replace a whole month per app, or per account for sales and earnings, in one transaction.
- A row belongs to the source that wrote it. Another source writing the same key fails loudly and changes nothing; it never takes the row over. Provisional `play_sales` rows are the one planned hand-over: `play_earnings` deletes them explicitly in the same transaction.

**Hosted-mode additions**

```sql
CREATE TABLE secrets (
  name        TEXT PRIMARY KEY,   -- 'apple_p8', 'google_sa', 'smtp_password', …
  ciphertext  BLOB NOT NULL,      -- AES-GCM, key derived from STOREPULSE_MASTER_KEY
  nonce       BLOB NOT NULL,
  fingerprint TEXT NOT NULL,      -- shown in UI instead of the secret
  updated_at  TEXT NOT NULL
);

CREATE TABLE users (
  id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,    -- argon2id
  totp_secret_enc BLOB            -- optional, encrypted like secrets
);
```

The `apps` table gains `display_name`, `pair_key` (links an iOS and Android app into one logical app), and `hidden`.

## Scheduling and reliability

One run per day (default 10:30 in the user's time zone) re-pulls the last 7 days from every configured source, absorbing store lag and late revisions.

- **Local:** `storepulse schedule install` writes a systemd user timer (Linux, `Persistent=true`, falling back to a crontab line when systemd isn't available), a launchd agent (macOS), or a Task Scheduler task (Windows); `schedule remove` undoes it. If the machine was asleep at run time, the next run catches up because of the 7-day window.
  - A systemd user timer only fires while its session is active unless the user lingers. `schedule install` checks `loginctl show-user … -p Linger` and offers to run `loginctl enable-linger`, since a machine reached only over SSH would otherwise silently stop running the job; `schedule show` reports the current state.
  - A scheduled run can't prompt for a passphrase, so with the encrypted-file secret store `schedule install` offers to save it into a `0600` file in the config directory and points the job at it with `STOREPULSE_PASSPHRASE_FILE` (read only by `core/secrets.py`); declining leaves the schedule uninstalled. Not yet supported on Windows with the file store — Windows Credential Manager is the expected store there.
- **Hosted:** an in-process scheduler (APScheduler) in the container; the run time is set in settings. A Run now button triggers an immediate run.

**CLI** (both modes; in Docker via `docker compose exec`)

- `storepulse init` — guided setup (see Credentials and setup).
- `storepulse run` — pull last N days, store, send digest.
- `storepulse backfill --source apple_sales --from 2025-01-01` — historical load, rate-limited.
- `storepulse digest --dry-run` — print or open the digest instead of sending it.
- `storepulse status` — last result per source and date, highlighting gaps.
- `storepulse doctor` — tests every credential and SMTP without pulling data.

**Error handling**

- Sources run independently: one failing source never blocks the others or the digest.
- "Report not available yet" is logged as `not_ready`, not `error`, and is retried by tomorrow's window.
- HTTP 429 and 5xx retry with exponential backoff (3 attempts); auth errors fail fast with a message that says which credential and which permission to check. The message always includes the store's own reason. Apple's `FORBIDDEN.REQUIRED_AGREEMENTS_MISSING_OR_EXPIRED` means the Account Holder must accept an updated agreement; it is not a key-role problem.
- A new Google service account can take up to a day or two before Play permissions take effect; `doctor` detects this and says so instead of reporting a generic 403.
- Raw downloaded files are cached for 30 days so parsers can be fixed and re-run without re-downloading.
- A lock prevents overlapping runs.
- Any `error`, or a report that is overdue, is flagged in the digest. Overdue depends on the source's cadence:
  - daily sources (`apple_sales`, `play_vitals`): still `not_ready` 4 days after `report_date`;
  - `play_installs` and `play_sales` (monthly files rewritten daily, logged with the month's first day): still `not_ready` 4 days into the month;
  - `play_earnings` (published once, early in the following month): still `not_ready` after the 15th of the month following `report_date`.
- The error flag looks only at `report_date`s the daily run currently re-attempts (the
  last `[schedule].days` days for a daily source; the current and previous month for a
  monthly one; the two months before the current one for earnings) — an old, never-retried
  error from a one-off backfill outside that range does not flag the digest forever.
  For a per-app source (`play_installs`, `play_vitals`, one `ingest_log` row per app per
  `report_date`), the flag is per app: a later success for one app never hides another
  app's still-unresolved error for the same `report_date`.

## Outputs

### Daily email (both modes)

Sent through the user's own SMTP server (Gmail or Fastmail app password, SES, Postmark, etc.), so no Storepulse-run mail service is needed.

- HTML body with plain-text fallback; charts rendered server-side as small PNG images embedded inline (email clients strip SVG and JavaScript).
- **As-of day:** each configured platform's own latest day with loaded data is found independently; the digest's as-of day is the minimum of those. That single date drives the header and every combined figure, so Apple and Play lagging by different amounts never produces mismatched totals. Phase 3 compares the 7 days ending on it with the 7 days before; a separate "yesterday" figure was simplified out for now.
- Contents: 7-day totals vs the previous 7 days, per-app rows with a 30-day installs sparkline, proceeds listed per currency (no conversion; a converted total appears only when `config.toml`'s `[digest]` section sets fixed rates and a display currency — no new outbound calls), vitals warnings, and data freshness per source.
  - A configured platform that has never loaded data (e.g. Play bucket access still pending) shows as `—`, never `0`, and is left out of the combined totals and trend; its apps aren't listed. This is judged per platform, never against the shared as-of window, so a stalled platform can't make the other one read `—`. Vitals warnings cover every app, including collapsed ones and apps of a platform shown as `—`, since crash and ANR rates come from the Reporting API, not the installs reports. Apps with no installs or proceeds in either week collapse into one "+N more apps" line. Two apps sharing a name on one platform show their store ID. Vitals collected without any values from Google (common below its minimum user count) read `vitals ok (no data from Google)`.
- Local mode's `apps` table has no `pair_key` yet (that arrives with hosted mode; see Storage), so an iOS and Android build of the same app are two separate rows — never merged — until then.
- Optional weekly summary email and optional CSV attachment of the week's data.
- In hosted mode the email links to the dashboard.

```
Storepulse · Thu Sep 24
Installs  142 (+18% wk)   iOS 61 · Android 81
Proceeds  CA$213 (−4% wk)
Top app   deskFT 58 installs
Vitals    billFT Android crash rate 1.4% ⚠
Data      apple_sales ok · play_installs ok (Sep 22) · vitals ok
```

Warnings: the 28-day crash rate above 1.09% or the 28-day ANR rate above 0.47% (Google Play's bad-behavior thresholds, configurable; daily rates are shown for information only and never trigger a warning), a source erroring, or a source overdue per its cadence (Scheduling and reliability).

### Web dashboard (hosted only)

Server-rendered pages (FastAPI + Jinja, Chart.js bundled locally, no CDN), behind login.

- **Overview:** all apps combined — installs, proceeds, crash rate, with 7/30/90-day ranges.
- **App page:** iOS and Android side by side; installs, proceeds, countries, vitals.
- **Sources page:** freshness and last error per source; Run now.
- **Settings:** credentials (fingerprints only, replace or remove), SMTP, schedule, currency, thresholds, app pairing.
- Mobile-friendly; HTTPS is expected via a reverse proxy (Caddy example included; Apache and nginx snippets in docs).

## Credentials and setup

Setup is a guided flow that validates each credential live before saving it, so a user never finds out on day two that a key was wrong.

**Flow (same steps in the CLI wizard and the web setup page)**

1. **Apple (optional):** paste issuer ID and key ID, point to or upload the `.p8`, enter vendor number. Storepulse signs a test JWT, calls `GET /v1/apps`, and shows the apps it found.
2. **Google (optional):** upload the service account JSON, paste the bucket URI. Storepulse lists the bucket and calls the Reporting API, then shows the apps found — or explains a pending-permission delay.
3. **RevenueCat (optional):** paste a read-only v2 key and project ID.
4. **Email:** SMTP host, port, username, password, from and to addresses; sends a test email.
5. **Preferences:** currency, run time, time zone, backfill depth. Then an optional first backfill.
6. **Hosted only, first:** before any of the above, create the admin account (argon2id password, optional TOTP). The setup page is reachable only until an admin exists and requires a one-time setup token printed in the container log.

**Where each thing lives**

| Item | Local mode | Hosted mode |
| --- | --- | --- |
| `.p8`, service account JSON, API keys, SMTP password | OS keychain (or encrypted file) | `secrets` table, AES-GCM |
| Master key | n/a (keychain) or passphrase | `STOREPULSE_MASTER_KEY` env var or Docker secret |
| Non-secret settings | `config.toml` in the user config dir | `settings` table |
| Data | user data dir | `/data` volume |

- The original key files can be deleted after import; the docs recommend it.
- Secrets are never returned by the API or shown in the UI after saving; replacing one requires the admin password again.
- `storepulse export-config` writes non-secret settings only; secrets are re-entered on a new machine by design.
- Losing the hosted master key means re-entering credentials, not losing data.

## Build phases for Claude Code

Local mode ships first as v0.1 because it is the core plus a CLI; hosted mode wraps the same core as v0.2. Each phase is its own Claude Code task.

1. **Repo + core + Apple sales**
   - Public repo skeleton: license, README stub, `SECURITY.md`, CI (tests, ruff, mypy), `SecretStore` interface with `KeyringStore` and `EncryptedFileStore`.
   - SQLite schema with versioned migrations; Apple JWT, discovery, `salesReports` parser.
   - *Done when:* `storepulse init` saves Apple credentials to the keychain, a 90-day backfill loads, re-runs don't change row counts, and totals match App Store Connect for three sample days.
2. **Google: bulk reports + vitals**
   - GCS reader, UTF-16 installs parser, earnings parser, vitals metric sets, Play discovery, `doctor` checks.
   - *Done when:* last month's installs match Play Console within 1% and vitals match Android vitals.
3. **Email digest + scheduler → release v0.1 (local)**
   - SMTP mailer, HTML + text digest with inline PNG charts, thresholds, `schedule install` for Linux, macOS, and Windows.
   - *Done when:* a fresh machine goes from `pipx install` to a received digest in under 15 minutes following the README; published to PyPI.

**3b. Apple subscription reports** (added after Phase 3, so the later phase numbers stay the same)
- Daily `SALES/SUBSCRIPTION` and `SUBSCRIPTION_EVENT` reports from `GET /v1/salesReports`. Metrics for active subscriptions, trials and churn events are added to the vocabulary in this phase.
- Until then, auto-renewable subscription revenue still arrives through `IAY` rows in the daily SALES report; only the subscription state (active, trial, churn) is missing.
- Overlaps with the open "Revenue source" decision: if RevenueCat is chosen, this phase is limited to what RevenueCat doesn't cover.
- *Done when:* active subscriptions and trials match App Store Connect's Subscriptions dashboard for three sample days, and re-runs don't change row counts.

4. **Hosted shell → release v0.2 (hosted)**
   - FastAPI app, admin account + TOTP, setup token, `DbEncryptedStore`, web setup flow, dashboard pages, in-process scheduler, Dockerfile, compose file, Caddy example.
   - *Done when:* `docker compose up` to working dashboard and email in under 15 minutes; secrets are unreadable in the DB without the master key; image published to GHCR.
5. **Security review**
   - Dependency audit, secret-redaction tests (grep logs and error pages for key material), CSRF and session tests, rate-limited login.
   - *Done when:* checklist in `SECURITY.md` passes and a second reviewer (Claude review pass) finds no open high-severity issues.
6. **Apple analytics + RevenueCat → v0.3**
   - One-time analytics report request setup, segment downloads; RevenueCat snapshots.
   - *Done when:* impressions and page views appear per iOS app and setup is not repeated on later runs.

For every phase: parser unit tests against scrubbed sample reports in `tests/fixtures/`, no network calls in tests, and LogicFT's own FT apps as the live dogfood account.

## Open decisions

- [ ] **Revenue source.** Store reports for everything (simpler, messier for subscriptions) or stores for installs and vitals plus RevenueCat for revenue (cleaner MRR, trials, churn; one more key).
- [x] **Currency conversion** (Phase 3). Proceeds are shown per currency, with no conversion and no new outbound calls (no daily reference rate lookup). A converted total is shown only if the user sets fixed rates and a display currency in `config.toml`'s `[digest]` section.
- [ ] **Language.** Python (current draft: mature JWT, GCS, keyring, and web libraries; `pipx` install) or Go for a single static binary that non-Python users install more easily.
- [ ] **License.** Apache-2.0 (current draft) or MIT for maximum adoption, or AGPL-3.0 if anyone running a modified version as a public service should have to share their changes. This also affects whether LogicFT could later sell a managed, per-customer-isolated hosted version.
- [ ] **Name.** Storepulse is a placeholder; check PyPI, GitHub, and trademark conflicts before the first public release.
- [ ] **Backfill depth.** How far back to load on first run (Apple sales keeps daily reports for about a year; Play bucket files go back to each app's launch).
