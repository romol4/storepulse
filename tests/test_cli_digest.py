from __future__ import annotations

import io
from pathlib import Path

from storepulse.cli.main import Env, main
from storepulse.core import config, db


def _env() -> Env:
    return Env(stdout=io.StringIO(), stderr=io.StringIO())


def _out(env: Env) -> str:
    return env.stdout.getvalue()  # type: ignore[attr-defined, no-any-return]


def test_digest_dry_run_prints_text_and_never_sends() -> None:
    conn = db.connect(config.db_path())
    conn.close()
    env = _env()
    assert main(["digest", "--dry-run"], env) == 0
    assert "No data has been collected yet" in _out(env)


def test_digest_writes_html_when_requested(tmp_path: Path) -> None:
    conn = db.connect(config.db_path())
    app_id = db.upsert_app(conn, "ios", "1", "deskFT")
    conn.execute(
        "INSERT INTO daily_metrics (date, app_id, country, metric, currency, value, source, "
        "updated_at) VALUES ('2026-09-26', ?, 'US', 'installs', '', 5, 'apple_sales', "
        "'2026-09-26T00:00:00+00:00')",
        (app_id,),
    )
    # apple_sales is a daily-cadence source, so the digest's as-of day reads ingest_log
    # (not just daily_metrics) to also recognize a real zero-sales day as collected;
    # a matching 'ok' row is what the real collector always writes alongside the data.
    db.log_ingest(
        conn,
        source="apple_sales",
        report_date="2026-09-26",
        started_at="2026-09-26T00:00:00+00:00",
        status="ok",
        rows=1,
    )
    conn.close()

    html_path = tmp_path / "digest.html"
    env = _env()
    cfg = config.Config(apple=config.AppleConfig("i", "k", "1"))
    config.save(cfg)
    assert main(["digest", "--html", str(html_path)], env) == 0
    assert html_path.exists()
    assert "deskFT" in html_path.read_text()
    assert f"HTML written to {html_path}" in _out(env)
    assert "deskFT" in _out(env)
