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
  - `config.toml`, raw report caches, or logs.
  - The SQLite database in local mode. In hosted mode they go there only encrypted
    (below).
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

## Hosted mode (the Docker image)

- **The master key.** In hosted mode secrets are stored in the database's
  `secrets` table, each AES-GCM encrypted and bound to its name. The key is derived
  with HKDF-SHA256 from the master key (`STOREPULSE_MASTER_KEY`, or a Docker secret
  file named by `STOREPULSE_MASTER_KEY_FILE`, at least 32 characters) and a random
  per-database salt. The master key is never written to the database or the data
  volume, so a copy of `storepulse.db` or a volume backup alone doesn't reveal your
  credentials. Keep the key outside the volume and back it up separately.
- **Losing or changing the master key** loses no data, only the stored credentials:
  the app detects a different key at startup and refuses to start rather than
  decrypt garbage. Start with the original key, or start on a fresh volume and
  re-enter your credentials in Settings.
- **Fingerprints, not secrets.** No page, API response or email ever shows a stored
  secret. Settings shows a fingerprint (a keyed HMAC, so the database alone can't be
  used to test guesses), the upload date and a *Test connection* button. Replacing
  a saved credential asks for your password again.
- **The admin account.** Passwords are hashed with argon2id; TOTP is optional, its
  secret is stored encrypted like the credentials, and each code signs in only once. The first-run setup token is
  printed to the container log at each start until an admin exists, stored only as a
  hash, and stops working once the admin account is created.
- **Sessions and forms.** Sessions are server-side; the cookie holds a random token
  whose hash is stored, and it is `HttpOnly`, `SameSite=Strict`, and `Secure` behind
  HTTPS. Every form carries a CSRF token. Password, TOTP and setup-token attempts are
  throttled to 5 failures per 15 minutes per IP and per account, in memory — a
  restart clears it. Pages are served with a strict Content-Security-Policy and
  load no third-party scripts, fonts or CDNs (Chart.js is bundled).
- **Network exposure.** The compose file binds port 8000 to `127.0.0.1` only. Put
  a TLS reverse proxy in front before exposing it (see the README), and set
  `STOREPULSE_TRUSTED_PROXIES` so throttling sees real client addresses.
- **The SMTP "Test connection" feature can reach hosts on your private network, by
  design.** The admin submits arbitrary host/port values, and Storepulse connects to
  prove the settings work before saving them — the same trust level as whoever
  deployed the container, so reaching another host on the same private network (a
  self-hosted mail relay on the same Docker network or LAN, a common setup for a
  self-hosted tool) is accepted, not a gap. The one address range that's never a
  legitimate mail server is link-local (`169.254.0.0/16`) — AWS, GCP, Azure and
  DigitalOcean all serve their instance metadata, including IAM credentials, from
  `169.254.169.254` — so a literal link-local host is rejected outright before any
  connection is attempted. This check only inspects literal IP addresses, not
  resolved hostnames (adding a real DNS lookup to every send would mean the mailer's
  tests are no longer network-free); the realistic attack here submits the
  well-known metadata IP directly, not a hostname that happens to resolve to it.

## Security review checklist

Tracks `docs/SPEC.md`'s Phase 5a (local mode and supply chain) and 5b (hosted
surface) "Done when" criteria. Most of 5b was already checked: PR #8 built the
hosted surface this checklist originally scoped 5b to audit, its own tests cover
several of these properties directly, and a same-day follow-up fixed a TOTP-replay
issue this checklist called for. The rest — Docker image hardening, the SMTP SSRF
gap, and three backfilled regression tests for properties that were already true —
closed out the checklist below.

### Secrets and data at rest
- [x] Local-mode keychain/encrypted-file paths, as documented above.
- [x] Hosted-mode `DbEncryptedStore`: secrets unreadable in the DB without the master
      key (`test_database_alone_reveals_no_secret`, `test_wrong_master_key_fails_clearly`).
- [x] The SQLite file and its directory are 0600/0700, not just relying on encryption
      for the columns that have it — the file also holds unencrypted sales data and,
      in hosted mode, session token hashes.
- [x] `.gitignore` covers every secret file type this document and `CLAUDE.md` name.

### Secrets never appear in...
- [x] `config.toml`, the SQLite database's plaintext columns, raw report caches, or
      logs.
- [x] Log output or error messages, across every CLI command and both mail-send
      paths — swept broadly, not just unit-tested in isolation.
- [x] Hosted-mode error pages (404/405/500) and JSON responses
      (`test_no_page_or_response_contains_a_stored_secret` crawls a 404 and a 405
      too, not just 200 pages; 500 is FastAPI/Starlette's static default body,
      pinned by `test_debug_mode_is_never_enabled`).

### Supply chain
- [x] Runtime dependencies pinned to compatible ranges, audited clean by `pip-audit`.
- [x] Dependabot configured for pip and for GitHub Actions.
- [x] Every GitHub Actions `uses:` pinned to a commit SHA, not a mutable tag —
      especially `release.yml`, which holds PyPI trusted publishing and GHCR
      permissions.
- [x] A secret-scanning step runs in CI.
- [x] The base Docker image is pinned by digest; the built image is scanned for
      known vulnerabilities in CI (Trivy, `ci.yml`'s `docker` job; Dependabot's
      `docker` ecosystem keeps the pinned digest current).

### Least privilege / no unexpected calls
- [x] Setup guides request only the documented read-only roles; Android revenue is
      opt-in.
- [x] Only Apple, Google, RevenueCat (opt-in), and the user's own SMTP server are
      contacted — no telemetry.

### Hosted auth surface
- [x] Login, TOTP, and the setup token are all rate-limited; documented above as
      in-memory and reset on restart.
- [x] A TOTP code cannot be replayed within its validity window (`claim_totp_step`).
- [x] CSRF is rejected on every state-changing route; a GET never requires a token.
- [x] A new session token is issued on login (no session fixation from a pre-auth
      cookie): `start_session` always mints a fresh token, kept separate from the
      anonymous CSRF cookie used before login (`test_login_rotates_the_session_token`).
- [x] Session expiry is enforced server-side end-to-end (`get_session`'s
      `expires_at > now` check), not only via cookie `Max-Age`
      (`test_expired_session_is_rejected_server_side`).
- [x] `X-Forwarded-For` is not trusted for rate-limiting/throttle keys unless
      `STOREPULSE_TRUSTED_PROXIES` is explicitly configured (uvicorn's
      `forwarded_allow_ips`, default `127.0.0.1`).
- [x] Chart JSON embedded via `|safe` cannot break out of its `<script>` block for
      any app-controlled label: `_chart_json` already escapes `<` defensively, though
      nothing reaches it with an app-controlled string today (only computed dates and
      floats). Pinned regardless by renaming an app to
      `</script><script>alert(1)</script>` and confirming it's escaped on every page
      (`test_app_rename_to_script_payload_is_escaped_everywhere`).
- [x] The SMTP-test-connection feature's ability to reach internal hosts from an
      admin session is a documented, deliberate decision, not an oversight — see
      "The SMTP 'Test connection' feature..." above: link-local addresses
      (169.254.0.0/16, almost always a cloud metadata endpoint) are rejected outright;
      RFC1918/loopback reachability is the accepted, deliberate scope.

### Second review
- [x] A full Claude review pass over the hosted surface finds no open high-severity
      issue.
