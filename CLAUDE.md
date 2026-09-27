# CLAUDE.md — Storepulse

Storepulse is an open-source tool that pulls a developer's App Store Connect and
Google Play data into SQLite and sends a daily email (local mode) or also serves a
web dashboard (hosted, self-hosted via Docker).

**The spec is `docs/SPEC.md`. Read it before starting any task.** It is the source
of truth for architecture, schema, security model, and build phases. If a task
seems to require deviating from the spec, stop and ask instead of improvising.

## How we work

- Implement **one build phase per branch / PR**, exactly as scoped in the spec.
  Do not start the next phase in the same PR.
- A phase is done only when its "Done when" criteria in the spec are met and
  CI is green.
- Prefer small, reviewable commits with clear messages (`feat:`, `fix:`,
  `test:`, `docs:`, `chore:`).
- When the spec is ambiguous, pick the simplest option, note the assumption in
  the PR description, and flag it for review.
- If you change behavior the spec describes, update `docs/SPEC.md` in the same PR.

## Stack and conventions

- Python 3.11+. Package name `storepulse`, src layout (`src/storepulse/`).
- Dependencies managed in `pyproject.toml`; pin versions. Keep the dependency
  list short — justify every new dependency in the PR description.
- Formatting and linting: `ruff format` and `ruff check`. Types: `mypy --strict`
  on `src/`. All three must pass.
- Tests: `pytest`. Run with `pytest -q` before every commit.
- Layout follows the spec: `core/` (sources, db, secrets, digest, mailer,
  runner), `cli/`, `web/`, `docker/`. `core/` must never import from `cli/` or
  `web/`.
- SQLite access through `core/db.py` only. Every schema change is a numbered
  migration; never edit an existing migration.
- All writes to `daily_metrics` are idempotent upserts. Re-running any date
  range must not change row counts.
- Times: store dates as `YYYY-MM-DD` in the store's reporting day; timestamps
  in UTC ISO 8601.

## Testing rules

- **No network calls in tests.** Mock HTTP and GCS clients.
- Parsers are tested against sample report files in `tests/fixtures/`. These
  are real reports with identifiers scrubbed; do not replace them with
  invented data unless asked.
- Every parser needs a test for: a normal file, an empty/zero-row file, and a
  malformed file.
- Add a regression test with every bug fix.

## Security — non-negotiable

- **Never commit secrets**: no `.p8` files, service-account JSON, API keys,
  SMTP passwords, `.env` files, or real `storepulse.db` files. `.gitignore`
  must cover them.
- No real Apple or Google credentials exist in this environment. Do not ask
  for them; build and test against fixtures and mocks.
- Secrets are only ever accessed through the `SecretStore` interface
  (`core/secrets.py`). No other module reads keys from disk or env directly.
- Never log, print, or include secrets in exceptions. Use the redaction helper
  for any log line that could contain credential material.
- The hosted web app never returns a stored secret through any endpoint or
  template — only its fingerprint.
- No telemetry and no outbound calls other than Apple, Google, RevenueCat
  (if enabled), and the user's SMTP server.

## Open-source hygiene

- License: Apache-2.0. New source files need no license header.
- Keep `README.md` quickstart accurate whenever CLI commands or setup steps
  change.
- User-facing error messages say which credential or permission to check and
  point to the relevant docs section.

## Commands

```bash
pip install -e ".[dev]"   # install with dev dependencies
ruff format && ruff check # format + lint
mypy src/                 # type check
pytest -q                 # tests
```
