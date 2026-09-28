# Security policy

Storepulse holds API keys that can read a developer's sales data, so we treat
security reports as a priority.

## Reporting a vulnerability

Please **don't open a public issue**. Report it privately through GitHub:
**Security → Report a vulnerability** on this repository.

We aim to acknowledge reports within 3 working days. Please include:
- the affected version or commit
- steps to reproduce
- the impact you see

## Supported versions

Storepulse is pre-release. Only the latest commit on the default branch
receives fixes.

## What Storepulse promises about secrets

- **Where secrets are kept:**
  - The OS keychain, when one is available. Secrets too large for a keychain entry
    are AES-GCM encrypted into a file under a random key held in the keychain.
  - Otherwise a local file encrypted with AES-GCM, with a key derived from your
    passphrase using scrypt. Each entry is bound to its name.
  - The SMTP password goes through the same store as the Apple and Google keys —
    it is a secret like any other, never written to `config.toml`.
- **The unattended-run passphrase file is a deliberate, narrow exception.**
  `storepulse schedule install` can save the encrypted-file store's passphrase into
  its own `0600` file in the config directory, so a scheduled run can unlock secrets
  without a prompt (`STOREPULSE_PASSPHRASE_FILE`, read only by `core/secrets.py`).
  Unlike everything else on this page, that file holds its content in **plaintext**:
  anyone who can read it and the encrypted secrets file can decrypt your
  credentials. Storepulse only writes it after your explicit confirmation, explains
  the trade-off first, and `schedule remove` deletes it. Prefer the OS keychain when
  you can reach it — it has no such file. Not currently offered on Windows with the
  file store; use Windows Credential Manager there instead.
- **Where secrets never go:**
  - `config.toml`, the SQLite database, raw report caches, or logs.
  - Log lines and error messages pass through a redaction filter.
- **Who Storepulse talks to:**
  - Apple and Google.
  - RevenueCat, only if you enable it.
  - Your own SMTP server, to send the digest. No currency-conversion or other new
    outbound call was added for the digest: proceeds are shown per currency, and a
    converted total (optional) is computed from fixed rates you set locally.
  - No telemetry, no version checks.
- **What API keys it asks for:**
  - Setup guides request read-only roles only: Sales and Reports on Apple, and
    *View app information and download bulk reports* on Google Play.
  - Android revenue optionally needs Play's *View financial data, orders, and
    cancellation survey responses*, which also exposes order details and buyers'
    city, state and postcode. Storepulse keeps only the buyer's country, and works
    without that permission (no Android revenue).
