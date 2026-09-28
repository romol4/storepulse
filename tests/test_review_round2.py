"""Regression tests for the first review round on the Phase 2 PR."""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path

import httpx
import pytest

from conftest import (
    BUCKET,
    PKG,
    TODAY,
    FakeGoogle,
    MemoryKeyring,
    play_bytes,
    router,
    standard_objects,
)
from storepulse.cli.main import Env, main
from storepulse.core import db, discovery, runner
from storepulse.core.sources.google_auth import load_service_account
from storepulse.core.sources.google_client import (
    BULK_PERMISSION,
    FINANCIAL_PERMISSION,
    GoogleAuthError,
    GoogleClient,
)
from storepulse.core.sources.play_common import Month

URI = f"gs://{BUCKET}/"
SEP = Month(2026, 9)
NEW_PKG = "com.example.newapp"

SERVICE_DISABLED = {
    "error": {
        "code": 403,
        "status": "PERMISSION_DENIED",
        "message": "Google Play Developer Reporting API has not been used in project 123 "
        "before or it is disabled. Enable it by visiting https://console.developers.google"
        ".com/apis/api/playdeveloperreporting.googleapis.com/overview?project=123 then retry.",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SERVICE_DISABLED"}
        ],
    }
}
GCS_FORBIDDEN = {
    "error": {
        "code": 403,
        "message": "reports@x does not have storage.objects.list access to the bucket.",
        "errors": [{"reason": "forbidden", "domain": "global"}],
    }
}


def _wrap(fake: FakeGoogle, override: dict[str, httpx.Response]) -> httpx.MockTransport:
    """Serve ``override`` for requests whose path (or prefix param) starts with a key."""

    def handler(request: httpx.Request) -> httpx.Response:
        target = request.url.params.get("prefix") or request.url.path
        for key, response in override.items():
            if target.startswith(key) or request.url.path.startswith(key):
                fake.requests.append(request)
                return response
        return fake.handler(request)

    return httpx.MockTransport(handler)


def _client(sa_json: str, transport: httpx.BaseTransport) -> GoogleClient:
    return GoogleClient(load_service_account(sa_json), transport=transport, sleep=lambda _: None)


# -- Google 403s are mapped by reason and keep Google's message ------------------------------


def test_service_disabled_is_a_hard_failure(sa_json: str) -> None:
    fake = FakeGoogle()
    transport = _wrap(fake, {"/v1beta1/apps:search": httpx.Response(403, json=SERVICE_DISABLED)})
    with pytest.raises(GoogleAuthError) as info:
        _client(sa_json, transport).search_apps()
    assert not info.value.pending_permission
    message = str(info.value)
    assert "has not been used in project 123" in message and "Enable it by visiting" in message
    assert "24-48 hours" not in message


def test_financial_prefix_names_the_financial_permission(sa_json: str) -> None:
    fake = FakeGoogle()
    transport = _wrap(fake, {"earnings/": httpx.Response(403, json=GCS_FORBIDDEN)})
    with pytest.raises(GoogleAuthError) as info:
        _client(sa_json, transport).list_objects(BUCKET, "earnings/")
    assert info.value.pending_permission
    assert FINANCIAL_PERMISSION in str(info.value)
    assert "does not have storage.objects.list access" in str(info.value)  # Google's text kept


def test_installs_prefix_names_the_bulk_permission(sa_json: str) -> None:
    fake = FakeGoogle()
    transport = _wrap(fake, {"stats/": httpx.Response(403, json=GCS_FORBIDDEN)})
    with pytest.raises(GoogleAuthError) as info:
        _client(sa_json, transport).list_objects(BUCKET, "stats/installs/")
    assert BULK_PERMISSION in str(info.value)


# -- "no files visible" is loud, "before the first file" stays quiet ------------------------


@pytest.fixture
def app_id(conn: sqlite3.Connection) -> int:
    return db.upsert_app(conn, "android", PKG, "billFT")


def test_invisible_financial_prefixes_warn(
    conn: sqlite3.Connection, app_id: int, sa_json: str
) -> None:
    objects = {k: v for k, v in standard_objects().items() if k.startswith("stats/")}
    client = _client(sa_json, FakeGoogle(objects=objects).transport)
    months = [Month(2026, m) for m in range(1, 10)]
    for collect in (runner.collect_play_sales, runner.collect_play_earnings):
        summary = collect(conn, client, BUCKET, months, today=TODAY)
        assert summary.ok == 0 and len(summary.warnings) == 1
        assert FINANCIAL_PERMISSION in summary.warnings[0]
    assert conn.execute("SELECT COUNT(*) FROM ingest_log").fetchone()[0] == 0


def test_app_without_installs_files_warns(conn: sqlite3.Connection, sa_json: str) -> None:
    db.upsert_app(conn, "android", NEW_PKG, "newapp")
    client = _client(sa_json, FakeGoogle(objects=standard_objects()).transport)
    summary = runner.collect_play_installs(conn, client, BUCKET, [SEP], today=TODAY)
    assert any(NEW_PKG in w for w in summary.warnings)


# -- discovery keeps working after setup ---------------------------------------------------


def _new_app_objects() -> dict[str, bytes]:
    objects = standard_objects()
    objects[f"stats/installs/installs_{NEW_PKG}_202609_overview.csv"] = play_bytes(
        f"installs_{PKG}_202609_overview.csv"
    )
    return objects


def test_refresh_finds_new_app(conn: sqlite3.Connection, app_id: int, sa_json: str) -> None:
    fake = FakeGoogle(
        objects=_new_app_objects(),
        apps=[
            {"packageName": PKG, "displayName": "billFT"},
            {"packageName": NEW_PKG, "displayName": "newapp"},
        ],
    )
    found = discovery.refresh_play_apps(_client(sa_json, fake.transport), conn, BUCKET)
    assert found.status == "ok"
    assert set(db.apps_for(conn, "android")) == {PKG, NEW_PKG}


def test_refresh_falls_back_to_bucket(conn: sqlite3.Connection, app_id: int, sa_json: str) -> None:
    fake = FakeGoogle(objects=_new_app_objects())
    transport = _wrap(fake, {"/v1beta1/apps:search": httpx.Response(403, json=SERVICE_DISABLED)})
    found = discovery.refresh_play_apps(_client(sa_json, transport), conn, BUCKET)
    assert found.status == "permission_denied" and found.apps == [(NEW_PKG, NEW_PKG)]
    names = dict(conn.execute("SELECT store_id, name FROM apps").fetchall())
    assert names == {PKG: "billFT", NEW_PKG: NEW_PKG}  # existing name untouched


# -- CLI: init, backfill, doctor -----------------------------------------------------------


@pytest.fixture
def sa_file(tmp_path: Path, sa_json: str) -> Path:
    path = tmp_path / "sa.json"
    path.write_text(sa_json)
    return path


def _env(
    answers: list[str],
    google: FakeGoogle | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Env:
    it = iter(answers)
    return Env(
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        input=lambda prompt: next(it),
        getpass=lambda prompt: "x",
        transport=transport or router(None, google),
        sleep=lambda _: None,
        today=TODAY,
    )


def _out(env: Env) -> str:
    return env.stdout.getvalue() + env.stderr.getvalue()  # type: ignore[attr-defined, no-any-return]


def _init_google(sa_file: Path, google: FakeGoogle) -> None:
    env = _env(["n", "y", str(sa_file), URI, "n"], google)
    assert main(["init", "--no-backfill"], env) == 0, _out(env)


def test_backfill_collects_app_launched_after_init(
    memory_keyring: MemoryKeyring, sa_file: Path
) -> None:
    _init_google(sa_file, FakeGoogle(objects=standard_objects()))
    later = FakeGoogle(
        objects=_new_app_objects(),
        apps=[
            {"packageName": PKG, "displayName": "billFT"},
            {"packageName": NEW_PKG, "displayName": "newapp"},
        ],
    )
    env = _env([], later)
    assert main(["backfill", "--source", "play_installs", "--months", "1"], env) == 0
    assert "New Android apps: newapp" in _out(env)
    assert f"stats/installs/installs_{NEW_PKG}_202609_overview.csv" in later.downloads()


def test_save_anyway_recovers_without_the_key_file(
    memory_keyring: MemoryKeyring, sa_file: Path
) -> None:
    pending = FakeGoogle(gcs_status=403, reporting_status=403)
    env = _env(["n", "y", str(sa_file), URI, "y", "n"], pending)
    assert main(["init", "--no-backfill"], env) == 0
    assert "You can now delete" not in _out(env)
    assert "until `storepulse doctor` passes" in _out(env)
    sa_file.unlink()  # the user deletes it anyway

    # Two days later permissions work: doctor registers the apps, backfill collects.
    ready = FakeGoogle(objects=standard_objects())
    env = _env([], ready)
    assert main(["doctor"], env) == 0
    assert "1 Android app(s) visible, 1 registered." in _out(env)
    env = _env([], ready)
    assert main(["backfill", "--source", "play_installs", "--months", "1"], env) == 0
    assert "1 periods loaded (22 rows)" in _out(env)


def test_reinit_defaults_to_no_and_reuses_stored_key(
    memory_keyring: MemoryKeyring, sa_file: Path, sa_json: str
) -> None:
    google = FakeGoogle(objects=standard_objects())
    _init_google(sa_file, google)
    sa_file.unlink()
    # Google is configured, so Enter at "Set up Google Play? [y/N]" skips it.
    env = _env(["n", "", "n"], google)
    assert main(["init", "--no-backfill"], env) == 0
    assert "Nothing changed." in _out(env)
    # Re-running Google with a blank path reuses the stored key.
    env = _env(["n", "y", "", URI, "n"], google)
    assert main(["init", "--no-backfill"], env) == 0, _out(env)
    assert "You can now delete" not in _out(env)


def test_doctor_warns_when_nothing_can_be_collected(
    memory_keyring: MemoryKeyring, sa_file: Path
) -> None:
    _init_google(sa_file, FakeGoogle(objects=standard_objects()))
    empty = FakeGoogle(objects={}, apps=[])
    env = _env([], empty)
    assert main(["doctor"], env) == 0
    out = _out(env)
    assert "All checks passed." not in out
    assert "no installs reports yet" in out
    assert "0 app(s) visible" in out and "WARN" in out
    assert FINANCIAL_PERMISSION in out
    assert not any("/o/" in r.url.path for r in empty.requests)  # no report downloads


def test_doctor_financial_permission_missing_is_a_warning(
    memory_keyring: MemoryKeyring, sa_file: Path
) -> None:
    _init_google(sa_file, FakeGoogle(objects=standard_objects()))
    fake = FakeGoogle(objects=standard_objects())
    forbidden = httpx.Response(403, json=GCS_FORBIDDEN)
    transport = _wrap(fake, {"sales/": forbidden, "earnings/": forbidden})
    env = _env([], transport=transport)
    assert main(["doctor"], env) == 0
    out = _out(env)
    assert "No Android sales reports visible" in out and "FAIL" not in out


# -- ownership guard on range replacement --------------------------------------------------


def test_range_replace_refuses_foreign_rows(conn: sqlite3.Connection, app_id: int) -> None:
    row = db.MetricRow("2026-09-05", app_id, "ALL", "installs", "", 3)
    db.replace_source_range(conn, "play_installs", "2026-09-01", "2026-09-30", [row])
    with pytest.raises(db.SourceCollisionError):
        db.replace_source_range(conn, "other_source", "2026-09-01", "2026-09-30", [row])
    owner = conn.execute("SELECT source FROM daily_metrics").fetchone()[0]
    assert owner == "play_installs"
    # An explicit hand-over via clear_sources is still allowed.
    db.replace_source_range(
        conn, "other_source", "2026-09-01", "2026-09-30", [row], clear_sources=("play_installs",)
    )
    assert conn.execute("SELECT source FROM daily_metrics").fetchone()[0] == "other_source"
