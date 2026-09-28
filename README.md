# Storepulse

Storepulse is an open-source tool that pulls your own App Store Connect (and, soon,
Google Play) data into a local SQLite database. It works in two modes:

- **Local:** runs on your Mac, Windows or Linux machine and sends a daily email.
- **Hosted:** self-hosted with Docker, adding a web dashboard.

> **Status: early development (Phase 1).** What works today:
> - Apple sales data: guided setup (`storepulse init`) and historical loads (`storepulse backfill`).
>
> Not built yet:
> - Google Play, the daily email, scheduling and the hosted dashboard.
>
> See [`docs/SPEC.md`](docs/SPEC.md) for the full design and roadmap.

## Quickstart (local mode)

Requires Python 3.11+ and [pipx](https://pipx.pypa.io/).

```bash
# Nothing is on PyPI until v0.1; install from GitHub:
pipx install git+https://github.com/romol4/storepulse

storepulse init                                    # guided Apple setup, validates your key
storepulse backfill --source apple_sales --days 90 # load the last 90 days
```

Re-running a backfill is safe: each day's rows are replaced, never duplicated.
Other ways to pick dates:
- `--from YYYY-MM-DD [--to YYYY-MM-DD]` loads a date range.
- Apple only keeps daily sales reports for about a year, so dates older than
  365 days are skipped with a warning.

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

## Where secrets live

- **OS keychain (default):**
  - macOS Keychain
  - Windows Credential Manager
  - Linux Secret Service (GNOME Keyring, KWallet)
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
