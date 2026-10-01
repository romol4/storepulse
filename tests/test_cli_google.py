from __future__ import annotations

import io
from pathlib import Path

import pytest

from conftest import (
    FT_APPS,
    PKG,
    TODAY,
    FakeApple,
    FakeGoogle,
    MemoryKeyring,
    apple_error,
    fixture_bytes,
    router,
    standard_objects,
)
from storepulse.cli.main import Env, main
from storepulse.core import config, db
from storepulse.core.secrets import KeyringStore

URI = "gs://pubsite_prod_1234567890/"


@pytest.fixture
def sa_file(tmp_path: Path, sa_json: str) -> Path:
    path = tmp_path / "storepulse-sa.json"
    path.write_text(sa_json)
    return path


@pytest.fixture
def p8_file(tmp_path: Path, p8_pem: str) -> Path:
    path = tmp_path / "AuthKey.p8"
    path.write_text(p8_pem)
    return path


def _env(
    answers: list[str], apple: FakeApple | None = None, google: FakeGoogle | None = None
) -> Env:
    it = iter(answers)
    return Env(
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        input=lambda prompt: next(it),
        getpass=lambda prompt: "unused-passphrase",
        transport=router(apple, google),
        sleep=lambda _: None,
        today=TODAY,
    )


def _out(env: Env) -> str:
    return env.stdout.getvalue()  # type: ignore[attr-defined, no-any-return]


def _err(env: Env) -> str:
    return env.stderr.getvalue()  # type: ignore[attr-defined, no-any-return]


def _scalar(sql: str, *params: object) -> int:
    conn = db.connect(config.db_path())
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    finally:
        conn.close()


def _google_only(sa_file: Path, *extra: str) -> list[str]:
    return ["n", "y", str(sa_file), URI, *extra]


# -- init ----------------------------------------------------------------------------------


def test_init_google_only(memory_keyring: MemoryKeyring, sa_file: Path, sa_json: str) -> None:
    env = _env(_google_only(sa_file, "n", "n", "n"), google=FakeGoogle(objects=standard_objects()))
    assert main(["init"], env) == 0
    out = _out(env)
    assert "Google key accepted" in out and "billFT (com.example.billft)" in out
    assert "service account fingerprint sha256:" in out
    cfg = config.load()
    assert cfg.apple is None
    assert cfg.google == config.GoogleConfig(
        URI.strip(), "reports@storepulse-test.iam.gserviceaccount.com"
    )
    # The ~2.3 KB JSON is wrapped: only a marker lives in the keychain.
    assert memory_keyring.store[("storepulse", "google_sa")] == "wrapped:v1"
    store = KeyringStore(config_dir=config.config_dir())
    assert store.get("google_sa") == sa_json
    assert "PRIVATE KEY" not in (config.config_dir() / "config.toml").read_text()
    assert _scalar("SELECT COUNT(*) FROM apps WHERE platform = 'android'") == 1


def test_init_both_platforms_then_backfill(
    memory_keyring: MemoryKeyring, sa_file: Path, p8_file: Path
) -> None:
    apple = FakeApple(apps=FT_APPS, default_report=fixture_bytes("summary_normal.tsv.gz"))
    google = FakeGoogle(objects=standard_objects())
    answers = ["y", "ISSUER-1", "KEYID12345", "85000000", str(p8_file)]
    answers += ["y", str(sa_file), URI, "n", "n", "y", "3", "2"]
    env = _env(answers, apple, google)
    assert main(["init"], env) == 0, _err(env)
    cfg = config.load()
    assert cfg.apple is not None and cfg.google is not None
    assert _scalar("SELECT COUNT(*) FROM daily_metrics WHERE source = 'apple_sales'") == 33
    assert _scalar("SELECT COUNT(*) FROM daily_metrics WHERE source = 'play_installs'") == 22
    assert _scalar("SELECT COUNT(*) FROM daily_metrics WHERE source = 'play_earnings'") == 3
    assert _scalar("SELECT COUNT(*) FROM daily_metrics WHERE source = 'play_vitals'") > 0


def test_init_neither_is_an_error(memory_keyring: MemoryKeyring) -> None:
    env = _env(["n", "n", "n", "n"])
    assert main(["init"], env) == 1
    assert "nothing was set up" in _err(env)


def test_init_pending_permission_declined_saves_nothing(
    memory_keyring: MemoryKeyring, sa_file: Path
) -> None:
    env = _env(_google_only(sa_file, "n"), google=FakeGoogle(gcs_status=403, reporting_status=403))
    assert main(["init"], env) == 1
    assert "24-48 hours" in _out(env)
    assert not memory_keyring.store
    assert not (config.config_dir() / "config.toml").exists()


def test_init_pending_permission_can_save_anyway(
    memory_keyring: MemoryKeyring, sa_file: Path
) -> None:
    env = _env(
        _google_only(sa_file, "y", "n", "n", "n"),
        google=FakeGoogle(gcs_status=403, reporting_status=403),
    )
    assert main(["init"], env) == 0
    assert config.load().google is not None


def test_init_bad_key_fails_without_saving(memory_keyring: MemoryKeyring, sa_file: Path) -> None:
    env = _env(_google_only(sa_file), google=FakeGoogle(token_status=400))
    assert main(["init"], env) == 1
    assert "invalid_grant" in _err(env)
    assert not memory_keyring.store


def test_init_bad_bucket_uri(memory_keyring: MemoryKeyring, sa_file: Path) -> None:
    env = _env(["n", "y", str(sa_file), "pubsite_prod_1"], google=FakeGoogle())
    assert main(["init"], env) == 1
    assert "gs://pubsite_prod_" in _err(env)


# -- backfill ------------------------------------------------------------------------------


def _setup_google(sa_file: Path) -> None:
    env = _env(_google_only(sa_file, "n", "n"), google=FakeGoogle(objects=standard_objects()))
    assert main(["init", "--no-backfill"], env) == 0


def test_backfill_play_sources(memory_keyring: MemoryKeyring, sa_file: Path) -> None:
    _setup_google(sa_file)
    google = FakeGoogle(objects=standard_objects())
    env = _env([], google=google)
    assert main(["backfill", "--source", "play_installs", "--months", "1"], env) == 0, _err(env)
    assert "1 periods loaded (22 rows)" in _out(env)
    env = _env([], google=google)
    assert main(["backfill", "--source", "play_sales", "--from", "2026-09"], env) == 0
    env = _env([], google=google)
    assert main(["backfill", "--source", "play_vitals", "--days", "7"], env) == 0
    assert "not ready" in _out(env)


def test_backfill_403_is_reported_once(memory_keyring: MemoryKeyring, sa_file: Path) -> None:
    """Dogfood follow-up: a bucket 403 used to print twice per app (an ERROR log line and
    the summary warning); now it's one grouped warning."""
    _setup_google(sa_file)
    env = _env([], google=FakeGoogle(objects=standard_objects(), gcs_status=403))
    assert main(["backfill", "--source", "play_installs", "--months", "1"], env) == 1
    err = _err(env)
    assert err.count("HTTP 403") == 1, err
    assert "ERROR:" not in err


def test_backfill_argument_errors(memory_keyring: MemoryKeyring, sa_file: Path) -> None:
    _setup_google(sa_file)
    env = _env([], google=FakeGoogle())
    assert main(["backfill", "--source", "play_installs", "--days", "3"], env) == 1
    assert "by month" in _err(env)
    env = _env([], google=FakeGoogle())
    assert main(["backfill", "--source", "apple_sales", "--months", "3"], env) == 1
    env = _env([], google=FakeGoogle())
    assert main(["backfill", "--source", "play_sales", "--from", "2026-13"], env) == 1


def test_backfill_google_before_init() -> None:
    env = _env([], google=FakeGoogle())
    assert main(["backfill", "--source", "play_installs", "--months", "1"], env) == 1
    assert "storepulse init" in _err(env)


# -- doctor --------------------------------------------------------------------------------


def test_doctor_all_ok(memory_keyring: MemoryKeyring, sa_file: Path, p8_file: Path) -> None:
    apple = FakeApple(apps=FT_APPS, default_report=fixture_bytes("summary_normal.tsv.gz"))
    google = FakeGoogle(objects=standard_objects())
    answers = [
        "y", "ISSUER-1", "KEYID12345", "85000000", str(p8_file),
        "y", str(sa_file), URI,
        "n",  # RevenueCat
        "n",  # email
    ]  # fmt: skip
    assert main(["init", "--no-backfill"], _env(answers, apple, google)) == 0
    env = _env([], apple, google)
    assert main(["doctor"], env) == 0
    out = _out(env)
    assert "All checks passed." in out
    assert f"{PKG} crashRateMetricSet: data through 2026-09-22" in out
    assert "FAIL" not in out
    # doctor pulls no report data
    assert not any("/o/" in r.url.path for r in google.requests)


def test_doctor_pending_permission(memory_keyring: MemoryKeyring, sa_file: Path) -> None:
    _setup_google(sa_file)
    env = _env([], google=FakeGoogle(gcs_status=403, reporting_status=403))
    assert main(["doctor"], env) == 1
    out = _out(env)
    assert "FAIL" in out and "24-48 hours" in out
    assert "HTTP 403" in out


def test_doctor_apple_missing_role(memory_keyring: MemoryKeyring, p8_file: Path) -> None:
    ok = FakeApple(apps=FT_APPS, default_report=fixture_bytes("summary_normal.tsv.gz"))
    answers = ["y", "ISSUER-1", "KEYID12345", "85000000", str(p8_file), "n", "n", "n"]
    assert main(["init", "--no-backfill"], _env(answers, ok)) == 0
    broken = FakeApple(apps=FT_APPS, sales={})
    broken.default_report = None
    broken.sales = {d: apple_error(403, "forbidden") for d in ["2026-09-24"]}
    env = _env([], broken)
    assert main(["doctor"], env) == 1
    assert "Sales and Reports" in _out(env)


def test_doctor_before_init() -> None:
    env = _env([])
    assert main(["doctor"], env) == 1
    assert "storepulse init" in _err(env)
