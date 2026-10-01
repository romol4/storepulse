# Storepulse

Storepulse is an open-source tool that pulls your own App Store Connect and Google
Play data into a local SQLite database. It works in two modes:

- **Local:** runs on your Mac, Windows or Linux machine and sends a daily email.
- **Hosted:** self-hosted with Docker, adding a web dashboard.

> **Status: early development (v0.3 — Apple Analytics & lifetime installs).** What works
> today:
> - **Local mode:** guided setup (`storepulse init`), credential checks
>   (`storepulse doctor`) and historical loads (`storepulse backfill`) for Apple sales,
>   Apple subscription state and subscription events, and Google Play installs, sales,
>   earnings and vitals; a daily email digest (`storepulse run`,
>   `storepulse digest --dry-run`) and OS-native scheduling (`storepulse schedule install`).
>   Apple Analytics (impressions, page views per iOS app) is collected automatically
>   alongside sales, using the same Apple key — no separate setup — and appears in the
>   digest. A lifetime installs total (everything ever collected, not just the recent
>   window) appears in both the digest and, in hosted mode, the dashboard.
> - **Hosted mode:** the same collectors and digest in a Docker container with a web
>   dashboard, web setup, an admin login with optional two-factor, and a built-in
>   scheduler (see "Hosted mode" below).
>
> Not built yet:
> - `storepulse status`, a weekly summary email, an optional CSV attachment on the
>   digest, and a 30-day cache of raw downloaded report files.
> - RevenueCat — deferred, no current plans to build it (see `docs/SPEC.md`, Phase 6b).
>
> See [`docs/SPEC.md`](docs/SPEC.md) for the full design and roadmap.

## Quickstart (local mode)

Requires Python 3.11+ and [pipx](https://pipx.pypa.io/).

```bash
# Once v0.1 is tagged, `pipx install storepulse` will work directly from PyPI.
# Until then, install from GitHub:
pipx install git+https://github.com/romol4/storepulse

storepulse init     # guided setup for Apple and/or Google Play and email; validates
                    # each key and sends a test email before saving
storepulse doctor   # re-check every saved credential (reads metadata only; the
                    # Apple check requests one day's sales report)
storepulse run      # collect every configured source, then send the digest
```

`init` also asks for a daily run time and offers to install the schedule, so following
this quickstart end to end — install, `init`, `run` — gets you a received digest.

`init` offers a first backfill. To load more later:

```bash
storepulse backfill --source apple_sales --days 90
storepulse backfill --source apple_subscriptions --days 90        # active subs and trials
storepulse backfill --source apple_subscription_events --days 90  # churn events
storepulse backfill --source play_installs --months 12
storepulse backfill --source play_sales --months 3      # provisional gross sales
storepulse backfill --source play_earnings --months 12  # final proceeds
storepulse backfill --source play_vitals --days 30
```

Re-running a backfill is safe: rows are replaced, never duplicated.

Other ways to pick dates:
- Day-based sources (`apple_sales`, `apple_subscriptions`, `apple_subscription_events`,
  `play_vitals`) take `--from YYYY-MM-DD [--to YYYY-MM-DD]`.
- Monthly Play sources take `--from YYYY-MM [--to YYYY-MM]`.

Limits:
- Apple only keeps daily reports (sales, subscriptions, subscription events) for about a
  year, so dates older than 365 days are skipped with a warning.
- Play months before the first report file are skipped silently.
- Apple Analytics has no backfill command: it has no equivalent of the other reports'
  "give me this specific past date," since an analytics report request only produces
  data going forward from when it's created. The daily run (or `storepulse run`) picks
  up whatever's available on its own.

**Android revenue comes in two stages:**
- The sales report updates daily. It gives *provisional* gross sales (`sales_gross`):
  the item price before Google's fee, in the buyer's currency. It is never mixed into
  `proceeds`, which is net everywhere.
- The monthly earnings report replaces it with *final* net `proceeds`. That report
  arrives early in the following month.
- Both need the optional financial permission (see "Google service account").

## Apple API key

Storepulse reads sales reports through the App Store Connect API. You need four
things:

1. **An API key with the Sales and Reports role.** In App Store Connect, go to
   Users and Access → Integrations → App Store Connect API and generate a
   *Team* key with the **Sales and Reports** role. Download the `.p8` file.
   Apple lets you download it only once.
2. **Issuer ID:** shown at the top of the same page.
3. **Key ID:** shown next to the key.
4. **Vendor number:** App Store Connect → Payments and Financial Reports, top
   left.

What the key can and can't do:
- **Can:** with Sales and Reports it can download sales and financial reports.
  Depending on Apple's rules, it can also list your apps.
- **Cannot:** change apps, prices, users, or anything else.

If the key can't list apps, Storepulse adds apps from the sales reports as they
appear.

**Apple Analytics (impressions, page views) needs one thing more.** Setting up the
one-time analytics report request for each app needs the **App Manager or Admin** role,
not just Sales and Reports — Apple's only source here that isn't a plain read. Storepulse
checks for an existing request first, so you have two options: grant the key App Manager
or Admin once, or create the ongoing analytics report request yourself in App Store
Connect (App Analytics → Reports → new ongoing request) and Storepulse will use it and
never ask again. Skip both and Storepulse just won't collect impressions/page views;
nothing else is affected.

`storepulse init` checks the key before saving anything:
1. It calls `GET /v1/apps` to confirm the key, key ID and issuer ID are accepted.
2. It requests the sales report from three days ago to confirm the role and the
   vendor number.

Errors say which of the four items to fix. After setup you can delete the
`.p8` file, because Storepulse keeps its own copy in the secret store.

## Google service account

Storepulse reads Play Console's bulk reports from Google Cloud Storage, and vitals
from the Play Developer Reporting API, using one service account:

1. **Create the service account.** In the [Google Cloud console](https://console.cloud.google.com/),
   pick or create a project.
   - Enable the **Google Play Developer Reporting API** for it.
   - Go to IAM & Admin → Service accounts → Create service account. It needs no
     Cloud roles.
   - Open it → Keys → Add key → JSON, and save the file.
2. **Grant it access in Play Console.** Go to Users and permissions → Invite new users.
   - Enter the service account's email.
   - Under account permissions, grant **View app information and download bulk
     reports (read-only)** for all apps. This is required: installs and vitals.
   - **Optional, for Android revenue only:** also grant **View financial data,
     orders, and cancellation survey responses**, set to *Global*. Without it
     everything else works; sales and earnings are skipped and `storepulse doctor`
     says so. If you have no paid apps or in-app purchases yet, there are no sales or
     earnings reports either, so expect the same warning and ignore it.
   - New service accounts can take **24–48 hours** before Play permissions work.
     `storepulse doctor` tells you when they do.
3. **Copy the bucket URI.** In Play Console, go to Download reports → Statistics →
   **Copy Cloud Storage URI**. It looks like `gs://pubsite_prod_1234567890/`.

What the service account can and can't do:
- **Can:** read the installs reports and vitals. With the optional financial
  permission it can also read the sales and earnings reports, and see order details
  in Play Console, including each buyer's city, state and postcode. Storepulse keeps
  only the buyer's country from those reports.
- **Cannot:** publish, change store listings, reply to reviews or manage users.

Once `storepulse init` (or `storepulse doctor`) shows your apps, you can delete the
JSON file, because Storepulse keeps its own copy in the secret store. To change other
settings later, run `init` again and press Enter at the key path to keep the saved key.

New apps are picked up automatically: every Play backfill, and `doctor`, re-runs app
discovery.

Compare installs like with like: `installs` is Play Console's **User acquisitions**
and `uninstalls` is **User losses**, not device acquisitions.

## Email and the daily digest

`storepulse run` collects from every source you've set up, then sends one digest email
covering the 7 days ending on the most recent day with data, compared with the 7 days
before, plus a lifetime installs total alongside it (everything ever collected, not just
the 7-day window — the same figure also appears on the hosted dashboard's Overview and
App pages). `storepulse init` walks you through SMTP setup — host, port, security,
username, password, from and to addresses — and sends a test email before saving
anything.

```bash
storepulse digest --dry-run                 # print the digest without sending it
storepulse digest --dry-run --html d.html   # also write the HTML version to a file
storepulse run                              # collect every source, then send the digest
storepulse run --no-email                   # collect and build the digest, don't send it
storepulse run --days 14                    # override how many days to re-pull
```

Proceeds are shown per currency, with no conversion and no new outbound calls. A
converted total appears only if you add fixed rates to `config.toml`:

```toml
[digest]
display_currency = "USD"

[digest.rates]
CAD = 0.73
EUR = 1.09
```

### SMTP setup

Storepulse sends through your own SMTP server — there's no Storepulse-run mail service.

- **Gmail:** create an [app password](https://myaccount.google.com/apppasswords) (needs
  2-Step Verification enabled). Host `smtp.gmail.com`, port `587`, security `starttls`,
  username your full Gmail address, password the 16-character app password.
- **Fastmail:** Settings → Password & Security → App passwords → create one scoped to
  SMTP. Host `smtp.fastmail.com`, port `587`, security `starttls`.
- **Amazon SES:** use the SMTP credentials from the SES console (not your AWS access
  key). Host is region-specific, e.g. `email-smtp.us-east-1.amazonaws.com`, port `587`,
  security `starttls`. Your sending identity must be verified first.
- **Postmark:** host `smtp.postmarkapp.com`, port `587`, security `starttls`; both the
  username and password are your server's API token.

`storepulse doctor` logs in and sends a NOOP to check the saved credentials without
sending mail.

### Schedule management

```bash
storepulse schedule install            # daily at the configured (or default) run time
storepulse schedule install --time 09:15
storepulse schedule show               # what's installed, and whether it will actually run
storepulse schedule remove
```

| OS | Mechanism |
| --- | --- |
| Linux (systemd) | a `systemd --user` timer |
| Linux (no systemd) | a marked line in your crontab |
| macOS | a `launchd` agent in `~/Library/LaunchAgents` |
| Windows | a Task Scheduler task |

A systemd user timer only fires while your session is active unless you linger. If
`loginctl` reports lingering is off, `schedule install` offers to run
`loginctl enable-linger $USER` for you — without it, a machine you reach only over SSH
may silently stop running the job. `schedule show` reports the current state.

If your secrets are in the encrypted file store (no OS keychain available), a scheduled
run can't prompt for the passphrase. `schedule install` offers to save it into a `0600`
file in the config directory and points the job at it with `STOREPULSE_PASSPHRASE_FILE`
(read only by `core/secrets.py`); anyone who can read that file and your secrets file can
unlock your credentials, so only agree to this on a machine you trust. Declining leaves
the schedule uninstalled. This isn't yet supported on Windows with the file store — set
up on a machine where Windows Credential Manager is reachable instead.

## Hosted mode

Hosted mode runs Storepulse on your own server, such as a small VPS or a home server,
in one Docker container. It sends the same daily email and adds a web dashboard. Your
keys stay on that server; nothing is sent anywhere except Apple, Google and your SMTP
server.

### Start it

Requires Docker with Compose. From a checkout (or just the `docker/` folder):

```bash
cd docker
# 1. The master key encrypts every credential you save. Make it once, keep a copy
#    somewhere safe (a password manager), and never commit it.
printf 'STOREPULSE_MASTER_KEY=%s\n' "$(openssl rand -base64 48)" > .env
chmod 600 .env
# 2. Start the container.
docker compose up -d
# 3. Copy the one-time setup token from the log.
docker compose logs storepulse | grep "setup token"
```

Open `http://127.0.0.1:8000/setup` (or your domain, see below), paste the token, and
create the admin account. Then **Settings** walks you through the same steps as
`storepulse init`: Apple, Google Play, email and preferences. Each credential is checked
live before it's saved. Next, **Sources → Load history** runs a first backfill. After that
the built-in scheduler runs every day at the time set in Settings, in the time zone you
choose there.

### The master key

- The master key is read only from `STOREPULSE_MASTER_KEY`, or from a Docker secret file
  named by `STOREPULSE_MASTER_KEY_FILE`. It must be at least 32 characters.
- Credentials are encrypted in the database under it (AES-GCM). The database alone
  reveals no key, password or usable fingerprint.
- **Lose the key and you re-enter your credentials; you don't lose data.** The app
  refuses to start with a different key, and says so.

### HTTPS and the reverse proxy

The container listens on `127.0.0.1:8000` only. Put a reverse proxy with HTTPS in front
of it:

- **Caddy (simplest):** copy `docker/Caddyfile.example` to `docker/Caddyfile` and set
  your domain. Caddy gets the certificate itself. You can also uncomment the `caddy`
  service in `compose.yaml` to run it in the same project.
- **nginx:** use `proxy_pass http://127.0.0.1:8000;`, and set `X-Forwarded-Proto` and
  `X-Forwarded-For`.
- **Apache:** use `ProxyPass / http://127.0.0.1:8000/`, `ProxyPassReverse`, and
  `RequestHeader set X-Forwarded-Proto https`.

If the proxy isn't on the same host, set `STOREPULSE_TRUSTED_PROXIES` in `docker/.env`
to its address, so the app sees HTTPS and the real client IPs. Login throttling uses
those IPs.

### Signing in

- There is one admin account. Turn on two-factor authentication in **Settings →
  Account**; it works with any authenticator app.
- Five wrong passwords, codes or setup tokens in 15 minutes lock that IP or account out
  for 15 minutes.
- A saved credential is never shown again. Settings shows only a fingerprint and the
  date it was saved, and replacing or removing one asks for the admin password.

### Commands inside the container

The CLI works in the container and uses the database's settings and secrets:

```bash
docker compose exec storepulse storepulse run          # collect and send the digest now
docker compose exec storepulse storepulse doctor       # test every credential
docker compose exec storepulse storepulse backfill --source apple_sales --days 90
```

`init` and `schedule` aren't used there: setup happens in the web app, and the container
has its own scheduler.

### Apps: names, hiding and pairing

- An iOS and an Android app with exactly the same store name (ignoring case) are shown
  as one app, when that match is unambiguous.
- Pair, unpair, rename or hide apps in **Settings → Apps**. Your pairing choices are
  never overridden.
- A hidden app leaves every list but still counts in totals, and still gets crash
  warnings.

### Upgrading

```bash
docker compose pull && docker compose up -d
```

Your data lives in the `storepulse-data` volume, and upgrades migrate it in place.

### Without Docker

Install the web extra, set `STOREPULSE_MASTER_KEY`, and run the server:

```bash
pipx install "storepulse[web]"
storepulse serve --host 127.0.0.1 --port 8000
```

Data then goes in the usual user data directory; set `STOREPULSE_DATA_DIR` to change it.

## Where secrets live

- **OS keychain (default):**
  - macOS Keychain
  - Windows Credential Manager
  - Linux Secret Service (GNOME Keyring, KWallet)
  - Large secrets such as the Google service-account JSON don't fit some keychains,
    notably Windows Credential Manager. They are encrypted into
    `keychain-wrapped.json` in the config directory, under a random key kept in the
    keychain. The file is useless without the keychain.
- **Encrypted file (fallback):** used when no keychain is available, for example
  on a headless server.
  - Location: `secrets.enc` in the config directory.
  - Encryption: AES-GCM, with a key derived from your passphrase using scrypt.
  - Unlocking: Storepulse prompts for the passphrase. For unattended runs, set
    `STOREPULSE_PASSPHRASE`, or let `storepulse schedule install` set up
    `STOREPULSE_PASSPHRASE_FILE` for you (see "Schedule management").

`storepulse init` prints which store it used. Later commands always use that
same store. If it isn't reachable, for example the keychain from a session with
no desktop login, they stop with an error rather than switching stores quietly.

Secrets are never written to `config.toml`, the database, or logs.

## Where everything else lives

| What | Where |
| --- | --- |
| Settings (`config.toml`, no secrets) | the user config dir: `~/.config/storepulse` on Linux, `~/Library/Application Support/storepulse` on macOS, `%LOCALAPPDATA%\storepulse` on Windows |
| Data (`storepulse.db`, SQLite) | the user data dir: the same places, with `~/.local/share/storepulse` on Linux |

To use other directories, set `STOREPULSE_CONFIG_DIR` and `STOREPULSE_DATA_DIR`.

## Development

```bash
pip install -r requirements-dev.lock && pip install -e . --no-deps
ruff format && ruff check
mypy src/
pytest -q
```

The two dependency files:
- `pyproject.toml` declares compatible version ranges, for installers.
- `requirements-dev.lock` pins exact versions for development and CI, including the `[web]` extra; the Docker image installs against it too.

To regenerate the lock file:
`uv pip compile pyproject.toml --extra dev --extra web --universal --python-version 3.11 -o requirements-dev.lock`

Tests never touch the network. See `tests/fixtures/` for the sample reports.

## Security

See [SECURITY.md](SECURITY.md) to report a vulnerability privately.

## License

Apache-2.0. See [LICENSE](LICENSE).
