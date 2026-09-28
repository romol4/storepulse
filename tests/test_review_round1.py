"""Regression tests for the first review round on the Phase 1 PR."""

from __future__ import annotations

import gzip
import io
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from conftest import FT_APPS, TODAY, FakeApple, MemoryKeyring, apple_error, fixture_bytes
from storepulse.cli.main import Env, main
from storepulse.core import db, runner
from storepulse.core.secrets import EncryptedFileStore
from storepulse.core.sources import apple_sales
from storepulse.core.sources.apple_client import (
    AGREEMENT_CODE,
    AppleAgreementError,
    AppleAuthError,
    AppleClient,
)

AGREEMENT_DETAIL = "A required agreement is missing or has expired."


def _client(fake: FakeApple, p8_pem: str) -> AppleClient:
    return AppleClient("i", "k", p8_pem, transport=fake.transport, sleep=lambda _: None)


def _tsv(*rows: tuple[str, ...]) -> bytes:
    header = "\t".join(apple_sales.REQUIRED_COLUMNS)
    body = "\n".join("\t".join(r) for r in rows)
    return gzip.compress(f"{header}\n{body}\n".encode())


# -- Apple 403s keep Apple's reason; agreements get their own fix ---------------------------


def test_agreement_403_is_not_blamed_on_the_role(p8_pem: str) -> None:
    fake = FakeApple(sales={"2026-09-20": apple_error(403, AGREEMENT_DETAIL, AGREEMENT_CODE)})
    with pytest.raises(AppleAgreementError) as info:
        apple_sales.fetch_day(_client(fake, p8_pem), "1", date(2026, 9, 20), TODAY)
    message = str(info.value)
    assert "accept the pending agreement" in message
    assert AGREEMENT_DETAIL in message
    assert "role" not in message


def test_other_403_keeps_apples_detail(p8_pem: str) -> None:
    fake = FakeApple(sales={"2026-09-20": apple_error(403, "You are not allowed.", "FORBIDDEN")})
    with pytest.raises(AppleAuthError) as info:
        apple_sales.fetch_day(_client(fake, p8_pem), "1", date(2026, 9, 20), TODAY)
    assert "Sales and Reports" in str(info.value)
    assert "Apple said: You are not allowed." in str(info.value)


def _init_env(fake: FakeApple, p8_file: Path) -> Env:
    answers = iter(["ISSUER-1", "KEYID12345", "85000000", str(p8_file), "n"])
    return Env(
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        input=lambda prompt: next(answers),
        getpass=lambda prompt: "x",
        transport=fake.transport,
        sleep=lambda _: None,
        today=TODAY,
    )


@pytest.fixture
def p8_file(tmp_path: Path, p8_pem: str) -> Path:
    path = tmp_path / "AuthKey.p8"
    path.write_text(p8_pem)
    return path


def test_init_reports_agreement_on_sales(memory_keyring: MemoryKeyring, p8_file: Path) -> None:
    probe = (TODAY - timedelta(days=3)).isoformat()
    fake = FakeApple(
        apps=FT_APPS, sales={probe: apple_error(403, AGREEMENT_DETAIL, AGREEMENT_CODE)}
    )
    env = _init_env(fake, p8_file)
    assert main(["init"], env) == 1
    err = env.stderr.getvalue()  # type: ignore[attr-defined]
    assert "accept the pending agreement" in err and "lacks the" not in err
    assert not memory_keyring.store


def test_init_agreement_on_app_list_fails(memory_keyring: MemoryKeyring, p8_file: Path) -> None:
    fake = FakeApple(default_report=fixture_bytes("summary_normal.tsv.gz"))
    base = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/apps":
            return apple_error(403, AGREEMENT_DETAIL, AGREEMENT_CODE)
        return base(request)

    env = _init_env(fake, p8_file)
    env.transport = httpx.MockTransport(handler)
    assert main(["init"], env) == 1
    assert "accept the pending agreement" in env.stderr.getvalue()  # type: ignore[attr-defined]
    assert "can't list apps" not in env.stdout.getvalue()  # type: ignore[attr-defined]


# -- empty country is ZZ, never ALL ---------------------------------------------------------


def test_empty_country_is_zz(conn: sqlite3.Connection) -> None:
    report = apple_sales.parse_summary(
        gzip.decompress(
            _tsv(
                ("DESKFT", "deskFT", "1F", "10", "0", "US", "USD", "1000000001", ""),
                ("DESKFT", "deskFT", "1F", "2", "0", "", "USD", "1000000001", ""),
            )
        ).decode()
    )
    mapped = apple_sales.map_rows(conn, date(2026, 9, 20), report)
    assert sorted((r.country, r.value) for r in mapped.rows) == [("US", 10.0), ("ZZ", 2.0)]


# -- a second source can't silently take over a row -----------------------------------------


def test_source_collision_fails_loudly(conn: sqlite3.Connection) -> None:
    app = db.upsert_app(conn, "ios", "1", "a")
    row = db.MetricRow("2026-09-01", app, "US", "installs", "", 5)
    db.replace_source_day(conn, "apple_sales", "2026-09-01", [row])
    other = db.MetricRow("2026-09-01", app, "US", "installs", "", 7)
    with pytest.raises(db.SourceCollisionError, match="belongs to apple_sales"):
        db.replace_source_day(conn, "apple_analytics", "2026-09-01", [other])
    rows = conn.execute("SELECT source, value FROM daily_metrics").fetchall()
    assert [tuple(r) for r in rows] == [("apple_sales", 5.0)]
    assert db.count_source_day(conn, "apple_sales", "2026-09-01") == 1


def test_duplicate_keys_within_one_source_still_merge(conn: sqlite3.Connection) -> None:
    app = db.upsert_app(conn, "ios", "1", "a")
    rows = [db.MetricRow("2026-09-01", app, "US", "installs", "", v) for v in (1, 2)]
    assert db.replace_source_day(conn, "apple_sales", "2026-09-01", rows) == 2
    assert conn.execute("SELECT value FROM daily_metrics").fetchone()[0] == 2.0


# -- IA3 restores are known, not "unknown" --------------------------------------------------


def test_ia3_is_known_and_adds_no_units(conn: sqlite3.Connection) -> None:
    report = apple_sales.parse_summary(
        gzip.decompress(
            _tsv(
                ("DESKFT", "deskFT", "1F", "1", "0", "US", "USD", "1000000001", ""),
                ("PRO", "deskFT Pro", "IA3", "4", "0", "US", "USD", "2000000001", "DESKFT"),
            )
        ).decode()
    )
    mapped = apple_sales.map_rows(conn, date(2026, 9, 20), report)
    assert not mapped.unknown_types and mapped.unmapped_rows == 0
    assert [(r.metric, r.value) for r in mapped.rows] == [("installs", 1.0)]
    names = [r["name"] for r in conn.execute("SELECT name FROM apps")]
    assert names == ["deskFT"]


# -- IAP rows seen before their parent app are re-mapped in the same run --------------------


def test_iap_before_parent_is_remapped(conn: sqlite3.Connection, p8_pem: str) -> None:
    day1, day2 = date(2026, 9, 20), date(2026, 9, 21)
    fake = FakeApple(
        sales={
            day1.isoformat(): httpx.Response(
                200,
                content=_tsv(
                    ("SUB", "deskFT Plus", "IAY", "1", "3.49", "CA", "CAD", "2000000002", "DESKFT")
                ),
            ),
            day2.isoformat(): httpx.Response(
                200,
                content=_tsv(("DESKFT", "deskFT", "1F", "3", "0", "US", "USD", "1000000001", "")),
            ),
        }
    )
    summary = runner.collect_apple_sales(
        conn, _client(fake, p8_pem), "1", [day1, day2], delay=0, today=TODAY
    )
    assert summary.remapped == [day1]
    assert summary.unmapped_rows == 0 and summary.unmapped_child_rows == 0
    day1_rows = conn.execute(
        "SELECT metric, value FROM daily_metrics WHERE date = ? ORDER BY metric",
        (day1.isoformat(),),
    ).fetchall()
    assert [tuple(r) for r in day1_rows] == [("iap_units", 1.0), ("proceeds", 3.49)]


def test_iap_with_unknown_parent_is_reported(conn: sqlite3.Connection, p8_pem: str) -> None:
    day = date(2026, 9, 20)
    fake = FakeApple(
        sales={
            day.isoformat(): httpx.Response(
                200,
                content=_tsv(
                    ("SUB", "Plus", "IAY", "1", "3.49", "CA", "CAD", "2000000002", "NEVERSEEN")
                ),
            )
        }
    )
    summary = runner.collect_apple_sales(conn, _client(fake, p8_pem), "1", [day], today=TODAY)
    assert summary.unmapped_child_rows == 1 and not summary.remapped


# -- the passphrase env var is read by the store, not the CLI -------------------------------


def test_env_passphrase_beats_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STOREPULSE_PASSPHRASE", "from-env-pass")

    def prompt() -> str:
        raise AssertionError("must not prompt when the env var is set")

    store = EncryptedFileStore(tmp_path / "s.enc", prompt)
    store.set("a", "value1234")
    assert EncryptedFileStore(tmp_path / "s.enc", prompt).get("a") == "value1234"


def test_cli_does_not_read_passphrase_env() -> None:
    source = (Path(__file__).parents[1] / "src/storepulse/cli").glob("*.py")
    for path in source:
        text = path.read_text()
        assert "os.environ" not in text and "PASSPHRASE_ENV" not in text, path
