"""The CLI inside the hosted container (STOREPULSE_MODE=hosted): `docker compose exec …
storepulse run` works off the database; init and schedule point to the web app instead;
`serve` explains a missing [web] extra. Plus the in-process scheduler's trigger."""

from __future__ import annotations

import io
from datetime import time as dtime

import pytest

from conftest import FT_APPS, TODAY, FakeApple, fixture_bytes
from storepulse.cli import serve
from storepulse.cli.main import Env, main
from storepulse.core import config, db
from storepulse.core.secrets import APPLE_P8_SECRET, DbEncryptedStore
from storepulse.web import scheduler

MASTER_KEY = "m" * 40


def _env(fake: FakeApple | None = None) -> Env:
    return Env(
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        input=lambda prompt: "",
        getpass=lambda prompt: "",
        transport=fake.transport if fake else None,
        sleep=lambda _: None,
        today=TODAY,
    )


def _err(env: Env) -> str:
    return env.stderr.getvalue()  # type: ignore[attr-defined, no-any-return]


@pytest.fixture
def hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STOREPULSE_MODE", "hosted")
    monkeypatch.setenv("STOREPULSE_MASTER_KEY", MASTER_KEY)


@pytest.mark.usefixtures("hosted")
def test_init_and_schedule_point_to_the_web_app() -> None:
    env = _env()
    assert main(["init"], env) == 1
    assert "web app" in _err(env)
    for command in (["schedule", "install"], ["schedule", "show"], ["schedule", "remove"]):
        env = _env()
        assert main(command, env) == 1
        assert "own scheduler" in _err(env)


@pytest.mark.usefixtures("hosted")
def test_run_uses_the_database_store(p8_pem: str) -> None:
    conn = db.connect(config.db_path())
    DbEncryptedStore(conn, MASTER_KEY).set(APPLE_P8_SECRET, p8_pem)
    config.save_hosted(
        conn, config.Config(apple=config.AppleConfig("ISSUER-1", "KEYID12345", "85000000"))
    )
    conn.close()
    fake = FakeApple(apps=FT_APPS, default_report=fixture_bytes("summary_normal.tsv.gz"))
    env = _env(fake)
    main(["run", "--no-email", "--days", "2"], env)
    # Sales plus 3b's two subscription reports, for each of the 2 days.
    assert sorted(set(fake.sales_dates())) == ["2026-09-25", "2026-09-26"], _err(env)
    conn = db.connect(config.db_path())
    assert conn.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0] > 0


def test_serve_without_the_web_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(serve, "missing_web_modules", lambda: ["fastapi", "uvicorn"])
    env = _env()
    assert main(["serve"], env) == 1
    assert "storepulse[web]" in _err(env) and "Docker" in _err(env)


def test_serve_without_a_master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env()
    assert main(["serve"], env) == 1
    assert "no master key" in _err(env)


def test_scheduler_trigger_uses_the_time_zone() -> None:
    trigger = scheduler.trigger("09:15", "Europe/Kyiv")
    assert str(trigger.timezone) == "Europe/Kyiv"
    fields = {f.name: str(f) for f in trigger.fields}
    assert (fields["hour"], fields["minute"]) == ("9", "15")
    # An unknown zone falls back to UTC rather than failing the scheduler.
    assert str(scheduler.trigger("09:15", "Mars/Base").timezone) == "UTC"
    assert dtime(9, 15)  # the run time format is the same HH:MM as local mode
