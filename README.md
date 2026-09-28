# Storepulse

Storepulse is an open-source tool that pulls your own App Store Connect and Google
Play data into a local SQLite database. It works in two modes:

- **Local:** runs on your Mac, Windows or Linux machine and sends a daily email.
- **Hosted:** self-hosted with Docker, adding a web dashboard.

> **Status: early development (Phase 2).** What works today:
> - Guided setup (`storepulse init`), credential checks (`storepulse doctor`) and
>   historical loads (`storepulse backfill`) for:
>   - Apple sales
>   - Google Play installs, sales, earnings and vitals
>
> Not built yet:
> - The daily email, scheduling and the hosted dashboard.
>
> See [`docs/SPEC.md`](docs/SPEC.md) for the full design and roadmap.

## Quickstart (local mode)

Requires Python 3.11+ and [pipx](https://pipx.pypa.io/).

```bash
# Nothing is on PyPI until v0.1; install from GitHub:
pipx install git+https://github.com/romol4/storepulse

storepulse init     # guided setup for Apple and/or Google Play; validates each key
storepulse doctor   # re-check every saved credential (reads metadata only; the
                    # Apple check requests one day's sales report)
```

`init` offers a first backfill. To load more later:

```bash
storepulse backfill --source apple_sales --days 90
storepulse backfill --source play_installs --months 12
storepulse backfill --source play_sales --months 3      # provisional gross sales
storepulse backfill --source play_earnings --months 12  # final proceeds
storepulse backfill --source play_vitals --days 30
```

Re-running a backfill is safe: rows are replaced, never duplicated.

Other ways to pick dates:
- Day-based sources (`apple_sales`, `play_vitals`) take `--from YYYY-MM-DD [--to YYYY-MM-DD]`.
- Monthly Play sources take `--from YYYY-MM [--to YYYY-MM]`.

Limits:
- Apple only keeps daily sales reports for about a year, so dates older than 365 days
  are skipped with a warning.
- Play months before the first report file are skipped silently.

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
     says so.
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
    `STOREPULSE_PASSPHRASE`.

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
- `requirements-dev.lock` pins exact versions for development and CI.

To regenerate the lock file:
`uv pip compile pyproject.toml --extra dev --universal --python-version 3.11 -o requirements-dev.lock`

Tests never touch the network. See `tests/fixtures/` for the sample reports.

## Security

See [SECURITY.md](SECURITY.md) to report a vulnerability privately.

## License

Apache-2.0. See [LICENSE](LICENSE).
