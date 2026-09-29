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

## Security review checklist

Tracks `docs/SPEC.md`'s Phase 5a (local mode and supply chain) and 5b (hosted
surface, once Phase 4 lands) "Done when" criteria. 5b's items stay unchecked
until that phase's own PR does the corresponding work.

### Secrets and data at rest
- [x] Local-mode keychain/encrypted-file paths, as documented above.
- [ ] Hosted-mode `DbEncryptedStore`: secrets unreadable in the DB without the master
      key. (5b)
- [x] The SQLite file and its directory are 0600/0700, not just relying on encryption
      for the columns that have it — the file also holds unencrypted sales data and,
      in hosted mode, session token hashes.
- [x] `.gitignore` covers every secret file type this document and `CLAUDE.md` name.

### Secrets never appear in...
- [x] `config.toml`, the SQLite database's plaintext columns, raw report caches, or
      logs.
- [x] Log output or error messages, across every CLI command and both mail-send
      paths — swept broadly, not just unit-tested in isolation.
- [ ] Hosted-mode error pages (404/405/500) and JSON responses. (5b)

### Supply chain
- [x] Runtime dependencies pinned to compatible ranges, audited clean by `pip-audit`.
- [x] Dependabot configured for pip and for GitHub Actions.
- [x] Every GitHub Actions `uses:` pinned to a commit SHA, not a mutable tag —
      especially `release.yml`, which holds PyPI trusted publishing permissions.
- [x] A secret-scanning step runs in CI.
- [ ] The base Docker image is pinned by digest; the built image is scanned for
      known vulnerabilities in CI. (5b)

### Least privilege / no unexpected calls
- [x] Setup guides request only the documented read-only roles; Android revenue is
      opt-in.
- [x] Only Apple, Google, RevenueCat (opt-in), and the user's own SMTP server are
      contacted — no telemetry.

### Hosted auth surface (5b)
- [ ] Login, TOTP, and the setup token are all rate-limited; documented as
      in-memory and reset on restart, or persisted.
- [ ] A TOTP code cannot be replayed within its validity window.
- [ ] CSRF is rejected on every state-changing route; a GET never requires a token.
- [ ] A new session token is issued on login (no session fixation from a pre-auth
      cookie).
- [ ] Session expiry is enforced server-side end-to-end, not only via cookie
      `Max-Age`.
- [ ] `X-Forwarded-For` is not trusted for rate-limiting/throttle keys unless a
      trusted-proxies setting is explicitly configured.
- [ ] Chart JSON embedded via `|safe` cannot break out of its `<script>` block for
      any app-controlled label (test: an app literally named `</script><script>...`).
- [ ] The SMTP-test-connection feature's ability to reach internal hosts from an
      admin session is a documented, deliberate decision, not an oversight.

### Second review (5b)
- [ ] A full Claude review pass over the hosted surface finds no open high-severity
      issue.
