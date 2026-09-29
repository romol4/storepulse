from __future__ import annotations

import io
from datetime import timedelta
from pathlib import Path

import keyring
import pytest
from keyring.backends import fail

from conftest import (
    FT_APPS,
    NO_SALES_DETAIL,
    TODAY,
    FakeApple,
    MemoryKeyring,
    apple_error,
    fixture_bytes,
    subscription_events_fixture_bytes,
    subscriptions_fixture_bytes,
)
from storepulse.cli.main import Env, main
from storepulse.core import config, db

CHECK_DAY = (TODAY - timedelta(days=3)).isoformat()


@pytest.fixture
def p8_file(tmp_path: Path, p8_pem: str) -> Path:
    path = tmp_path / "AuthKey_ABC123.p8"
    path.write_text(p8_pem)
    return path


def _env(fake: FakeApple, answers: list[str], secrets: list[str] | None = None) -> Env:
    inputs = iter(answers)
    passes = iter(secrets or [])
    return Env(
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        input=lambda prompt: next(inputs),
        getpass=lambda prompt: next(passes),
        transport=fake.transport,
        sleep=lambda _: None,
        today=TODAY,
    )


def _answers(p8_file: Path, *extra: str) -> list[str]:
    """Answers: Apple yes (four prompts), Google no, email no, then ``extra``."""
    return ["y", "ISSUER-1", "KEYID12345", "85000000", str(p8_file), "n", "n", *extra]


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


def _nothing_saved(memory_keyring: MemoryKeyring) -> bool:
    return not memory_keyring.store and not (config.config_dir() / "config.toml").exists()


def test_happy_path(memory_keyring: MemoryKeyring, p8_file: Path, p8_pem: str) -> None:
    fake = FakeApple(apps=FT_APPS, default_report=fixture_bytes("summary_normal.tsv.gz"))
    env = _env(fake, _answers(p8_file, "n"))
    assert main(["init"], env) == 0
    out = _out(env)
    assert "Secrets stored in: OS keychain" in out
    assert "vendor number confirmed" in out
    assert "deskFT" in out and "billFT" in out
    assert memory_keyring.store[("storepulse", "apple_p8")] == p8_pem
    cfg = config.load()
    assert cfg.secret_store == "keyring"
    assert cfg.apple == config.AppleConfig("ISSUER-1", "KEYID12345", "85000000")
    assert "PRIVATE KEY" not in (config.config_dir() / "config.toml").read_text()
    assert _scalar("SELECT COUNT(*) FROM apps") == 2
    assert fake.sales_dates() == [CHECK_DAY]


def test_wrong_role(memory_keyring: MemoryKeyring, p8_file: Path) -> None:
    fake = FakeApple(apps=FT_APPS, sales={CHECK_DAY: apple_error(403, "forbidden")})
    env = _env(fake, _answers(p8_file))
    assert main(["init"], env) == 1
    assert "Sales and Reports" in _err(env)
    assert _nothing_saved(memory_keyring)


@pytest.mark.parametrize("status", [400, 403])
def test_wrong_vendor(memory_keyring: MemoryKeyring, p8_file: Path, status: int) -> None:
    fake = FakeApple(
        apps=FT_APPS, sales={CHECK_DAY: apple_error(status, "Invalid vendor number specified")}
    )
    env = _env(fake, _answers(p8_file))
    assert main(["init"], env) == 1
    assert "vendor number" in _err(env) and "Payments and Financial Reports" in _err(env)
    assert _nothing_saved(memory_keyring)


def test_bad_key_401(memory_keyring: MemoryKeyring, p8_file: Path, p8_pem: str) -> None:
    fake = FakeApple(apps_status=401)
    env = _env(fake, _answers(p8_file))
    assert main(["init"], env) == 1
    assert "401" in _err(env) and "issuer ID" in _err(env)
    assert p8_pem.splitlines()[1] not in _err(env)
    assert _nothing_saved(memory_keyring)


def test_unreadable_p8(memory_keyring: MemoryKeyring, tmp_path: Path) -> None:
    env = _env(FakeApple(), _answers(tmp_path / "missing.p8"))
    assert main(["init"], env) == 1
    assert "could not read the .p8" in _err(env)


def test_unexplained_404_passes_with_warning(memory_keyring: MemoryKeyring, p8_file: Path) -> None:
    fake = FakeApple(apps=FT_APPS, sales={CHECK_DAY: apple_error(404, "Not found")})
    env = _env(fake, _answers(p8_file, "n"))
    assert main(["init"], env) == 0
    assert "couldn't fully confirm the vendor number; the first backfill will tell" in _out(env)
    assert config.load().apple is not None


def test_no_sales_on_check_day_passes(memory_keyring: MemoryKeyring, p8_file: Path) -> None:
    fake = FakeApple(apps=FT_APPS, sales={CHECK_DAY: apple_error(404, NO_SALES_DETAIL)})
    env = _env(fake, _answers(p8_file, "n"))
    assert main(["init"], env) == 0
    assert "No sales" in _out(env)


def test_apps_403_tolerated(memory_keyring: MemoryKeyring, p8_file: Path) -> None:
    fake = FakeApple(apps_status=403, default_report=fixture_bytes("summary_normal.tsv.gz"))
    env = _env(fake, _answers(p8_file, "n"))
    assert main(["init"], env) == 0
    assert "can't list apps" in _out(env)


def test_file_store_fallback_is_announced(p8_file: Path, p8_pem: str) -> None:
    previous = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    try:
        fake = FakeApple(apps=FT_APPS, default_report=fixture_bytes("summary_empty.tsv.gz"))
        env = _env(
            fake, _answers(p8_file, "n"), secrets=["short", "long-enough-pass", "long-enough-pass"]
        )
        assert main(["init"], env) == 0
        out = _out(env)
        assert "Secrets stored in: encrypted file" in out
        assert "no OS keychain available" in out
        assert config.load().secret_store == "file"
        enc = config.config_dir() / "secrets.enc"
        assert enc.exists() and "PRIVATE KEY" not in enc.read_text()
    finally:
        keyring.set_keyring(previous)


def test_init_then_backfill(memory_keyring: MemoryKeyring, p8_file: Path) -> None:
    fake = FakeApple(apps=FT_APPS, default_report=fixture_bytes("summary_normal.tsv.gz"))
    env = _env(fake, _answers(p8_file, "y", "5"))
    assert main(["init"], env) == 0
    assert _scalar("SELECT COUNT(*) FROM daily_metrics") == 5 * 11
    assert "Done: 5 days loaded" in _out(env)


def _init(p8_file: Path) -> None:
    fake = FakeApple(apps=FT_APPS, default_report=fixture_bytes("summary_empty.tsv.gz"))
    assert main(["init", "--no-backfill"], _env(fake, _answers(p8_file))) == 0


def test_backfill_clamps_old_from(memory_keyring: MemoryKeyring, p8_file: Path) -> None:
    _init(p8_file)
    fake = FakeApple(default_report=fixture_bytes("summary_normal.tsv.gz"))
    env = _env(fake, [])
    assert main(["backfill", "--source", "apple_sales", "--from", "2025-01-01"], env) == 0
    cutoff = (TODAY - timedelta(days=365)).isoformat()
    requested = fake.sales_dates()
    assert min(requested) == cutoff and len(requested) == 365
    assert "about a year" in _err(env) and "2025-01-01" in _err(env)
    assert _scalar("SELECT COUNT(*) FROM ingest_log WHERE report_date < ?", cutoff) == 0
    assert _scalar("SELECT COUNT(*) FROM ingest_log") == 365


def test_backfill_days(memory_keyring: MemoryKeyring, p8_file: Path) -> None:
    _init(p8_file)
    fake = FakeApple(default_report=fixture_bytes("summary_normal.tsv.gz"))
    env = _env(fake, [])
    assert main(["backfill", "--source", "apple_sales", "--days", "3"], env) == 0
    assert fake.sales_dates() == [(TODAY - timedelta(days=d)).isoformat() for d in (3, 2, 1)]
    assert "ZZ9" in _err(env)  # unknown product types are surfaced


def test_backfill_routes_apple_subscriptions_to_apple(
    memory_keyring: MemoryKeyring, p8_file: Path
) -> None:
    # Google is never configured in this flow: apple_subscriptions must route to
    # backfill_apple, not fall through to backfill_play (which would demand Google creds).
    _init(p8_file)
    fake = FakeApple(
        default_subscriptions_report=subscriptions_fixture_bytes("summary_normal.tsv.gz")
    )
    env = _env(fake, [])
    assert main(["backfill", "--source", "apple_subscriptions", "--days", "3"], env) == 0
    assert fake.dates_for("SUBSCRIPTION") == [
        (TODAY - timedelta(days=d)).isoformat() for d in (3, 2, 1)
    ]


def test_backfill_routes_apple_subscription_events_to_apple(
    memory_keyring: MemoryKeyring, p8_file: Path
) -> None:
    _init(p8_file)
    fake = FakeApple(
        default_subscription_events_report=subscription_events_fixture_bytes(
            "summary_normal.tsv.gz"
        )
    )
    env = _env(fake, [])
    assert main(["backfill", "--source", "apple_subscription_events", "--days", "3"], env) == 0
    assert fake.dates_for("SUBSCRIPTION_EVENT") == [
        (TODAY - timedelta(days=d)).isoformat() for d in (3, 2, 1)
    ]


def test_backfill_before_init() -> None:
    env = _env(FakeApple(), [])
    assert main(["backfill", "--source", "apple_sales", "--days", "3"], env) == 1
    assert "storepulse init" in _err(env)


def test_backfill_keychain_unreachable_does_not_fall_back(
    memory_keyring: MemoryKeyring, p8_file: Path
) -> None:
    _init(p8_file)
    previous = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    try:
        env = _env(FakeApple(), [])
        assert main(["backfill", "--source", "apple_sales", "--days", "3"], env) == 1
        assert "no keychain is reachable" in _err(env)
        assert not (config.config_dir() / "secrets.enc").exists()
    finally:
        keyring.set_keyring(previous)
