"""Hosted mode's core pieces: settings table, DbEncryptedStore, migration 0003, pairing,
and how pairs and hidden apps render in the digest (docs/SPEC.md, Phase 4)."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from storepulse.core import config, db, digest, discovery
from storepulse.core.secrets import (
    MASTER_KEY_ENV,
    MASTER_KEY_FILE_ENV,
    DbEncryptedStore,
    SecretStoreError,
    fingerprint,
    read_master_key,
)

KEY = "k" * 40
OTHER_KEY = "z" * 40
P8 = (  # shape only; never parsed as a key here
    "-----BEGIN PRIVATE KEY-----\n"
    "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQg\n"
    "-----END PRIVATE KEY-----"
)


# -- settings table <-> Config ----------------------------------------------------------


def _full_config() -> config.Config:
    return config.Config(
        secret_store="db",
        apple=config.AppleConfig("issuer", "KEYID", "85000000"),
        google=config.GoogleConfig("gs://pubsite_prod_1/", "sa@x.iam.gserviceaccount.com"),
        email=config.EmailConfig("smtp.x", 587, "starttls", "u", "a@x", ["a@x", "b@x"]),
        digest=config.DigestConfig(rates={"CAD": 0.73}, display_currency="USD"),
        schedule=config.ScheduleConfig(run_time="09:15", days=10),
    )


def test_settings_round_trip(conn: sqlite3.Connection) -> None:
    cfg = _full_config()
    config.save_hosted(conn, cfg)
    rows = db.get_settings(conn)
    assert rows["apple.vendor_number"] == "85000000"
    assert rows["digest.rates"] == {"CAD": 0.73}
    assert rows["email.to_addrs"] == ["a@x", "b@x"]
    assert "secret_store" not in rows and not any(k.startswith("extra") for k in rows)
    assert config.load_hosted(conn) == cfg


def test_save_hosted_removes_a_dropped_section(conn: sqlite3.Connection) -> None:
    cfg = _full_config()
    config.save_hosted(conn, cfg)
    cfg.google = None
    config.save_hosted(conn, cfg)
    assert not any(k.startswith("google.") for k in db.get_settings(conn))
    assert config.load_hosted(conn).google is None


def test_hosted_only_keys_survive_config_saves(conn: sqlite3.Connection) -> None:
    db.set_settings(conn, {"hosted.base_url": "https://sp.example.com"})
    config.save_hosted(conn, _full_config())
    assert db.get_settings(conn)["hosted.base_url"] == "https://sp.example.com"


def test_config_load_reads_the_database_in_hosted_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = db.connect(config.db_path())
    config.save_hosted(conn, _full_config())
    conn.close()
    monkeypatch.setenv("STOREPULSE_MODE", "hosted")
    cfg = config.load()
    assert cfg.secret_store == "db"
    assert cfg.apple == config.AppleConfig("issuer", "KEYID", "85000000")
    with pytest.raises(config.ConfigError, match="Settings page"):
        config.save(cfg)


# -- master key ----------------------------------------------------------------------------


def test_master_key_missing_short_and_from_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(SecretStoreError, match="no master key"):
        read_master_key()
    monkeypatch.setenv(MASTER_KEY_ENV, "short")
    with pytest.raises(SecretStoreError, match="too short") as exc:
        read_master_key()
    assert "short" not in str(exc.value).replace("too short", "")  # never echoes the key
    monkeypatch.delenv(MASTER_KEY_ENV)
    key_file = tmp_path / "master_key"
    key_file.write_text(KEY + "\n")
    monkeypatch.setenv(MASTER_KEY_FILE_ENV, str(key_file))
    assert read_master_key() == KEY


# -- DbEncryptedStore ---------------------------------------------------------------------


def test_db_store_round_trip_and_metadata(conn: sqlite3.Connection) -> None:
    store = DbEncryptedStore(conn, KEY)
    store.set("apple_p8", P8)
    assert store.get("apple_p8") == P8
    assert store.get("missing") is None
    fp = store.fingerprint("apple_p8")
    assert fp is not None and fp.startswith("hmac:")
    # Keyed: not the plain sha256 fingerprint local mode shows.
    assert fp != fingerprint(P8)
    assert store.updated_at("apple_p8")
    store.delete("apple_p8")
    assert store.get("apple_p8") is None


def test_database_alone_reveals_no_secret(tmp_path: Path) -> None:
    path = tmp_path / "sp.db"
    conn = db.connect(path)
    DbEncryptedStore(conn, KEY).set("smtp_password", "hunter2-app-password")
    conn.close()
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("sp.db*"))
    assert b"hunter2-app-password" not in raw
    assert KEY.encode() not in raw


def test_wrong_master_key_fails_clearly(conn: sqlite3.Connection) -> None:
    DbEncryptedStore(conn, KEY).set("apple_p8", P8)
    with pytest.raises(SecretStoreError, match="doesn't match") as exc:
        DbEncryptedStore(conn, OTHER_KEY)
    assert "re-enter" in str(exc.value)
    assert OTHER_KEY not in str(exc.value)


def test_wrong_key_detected_even_with_no_secrets_yet(conn: sqlite3.Connection) -> None:
    DbEncryptedStore(conn, KEY)
    with pytest.raises(SecretStoreError, match="doesn't match"):
        DbEncryptedStore(conn, OTHER_KEY)


def test_moving_a_ciphertext_to_another_name_fails(conn: sqlite3.Connection) -> None:
    store = DbEncryptedStore(conn, KEY)
    store.set("apple_p8", P8)
    row = db.get_secret_row(conn, "apple_p8")
    assert row is not None
    db.put_secret_row(conn, "google_sa", row.ciphertext, row.nonce, row.fingerprint)
    with pytest.raises(SecretStoreError, match="tampered"):
        store.get("google_sa")


def test_seal_binds_its_label(conn: sqlite3.Connection) -> None:
    store = DbEncryptedStore(conn, KEY)
    blob = store.seal("users/1/totp", "JBSWY3DPEHPK3PXP")
    assert store.unseal("users/1/totp", blob) == "JBSWY3DPEHPK3PXP"
    with pytest.raises(SecretStoreError):
        store.unseal("users/2/totp", blob)


# -- migration 0003 on an existing v0.1 database --------------------------------------------


def test_0003_upgrades_a_v01_database_in_place(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.row_factory = sqlite3.Row
    old.isolation_level = None
    for number, sql in db._migrations():
        if number > 2:
            break
        for statement in db._split_sql(sql):
            old.execute(statement)
        db.set_kv(old, "schema_version", str(number))
    app_id = db.upsert_app(old, "ios", "1", "deskFT")
    db.replace_source_day(
        old,
        "apple_sales",
        "2026-09-20",
        [db.MetricRow("2026-09-20", app_id, "US", "installs", "", 3.0)],
    )
    old.close()

    conn = db.connect(path)
    assert db.schema_version(conn) == 3
    assert conn.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0] == 1
    [app] = db.list_apps(conn)
    assert (app.name, app.pair_key, app.pair_source, app.hidden) == ("deskFT", None, None, False)


# -- pairing --------------------------------------------------------------------------------


LIVE_APPS = [  # the owner's real account, from the first live run
    ("ios", "1", "libFT"),
    ("ios", "2", "billFT"),
    ("ios", "3", "storyFT - Bedtime Stories"),
    ("ios", "4", "echoFT: Free Audio Archive"),
    ("ios", "5", "Desk FT: Client Portal"),
    ("ios", "6", "Bus Wiser"),
    ("ios", "7", "TAK ft"),
    ("android", "com.logicftinc.libft", "libFT"),
    ("android", "com.logicftinc.billft", "billFT"),
    ("android", "com.logicftinc.storyft", "storyFT - Bedtime Stories"),
    ("android", "com.logicftinc.echoft", "echoFT"),
    ("android", "com.logicftinc.deskft", "Desk FT"),
    ("android", "com.jasonsb.busarrival", "Bus Wise"),
    ("android", "com.logicftinc.buswise", "Bus Wise"),
]


def _register(conn: sqlite3.Connection, apps: list[tuple[str, str, str]]) -> dict[str, int]:
    return {store_id: db.upsert_app(conn, p, store_id, name) for p, store_id, name in apps}


def _pairs(conn: sqlite3.Connection) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for app in db.list_apps(conn):
        if app.pair_key:
            out.setdefault(app.pair_key, []).append(app.store_id)
    return {k: sorted(v) for k, v in out.items()}


def test_auto_pairing_on_the_live_account(conn: sqlite3.Connection) -> None:
    _register(conn, LIVE_APPS)
    discovery.pair_apps(conn)
    pairs = _pairs(conn)
    assert sorted(pairs.values()) == [
        ["1", "com.logicftinc.libft"],
        ["2", "com.logicftinc.billft"],
        ["3", "com.logicftinc.storyft"],
    ]
    assert all(a.pair_source in (None, "auto") for a in db.list_apps(conn))


def test_matching_ignores_case_and_whitespace_but_not_display_names(
    conn: sqlite3.Connection,
) -> None:
    ids = _register(conn, [("ios", "1", " LibFT "), ("android", "p.lib", "libft")])
    ids |= _register(
        conn, [("ios", "2", "echoFT: Free Audio Archive"), ("android", "p.echo", "echoFT")]
    )
    db.set_app_display(conn, ids["2"], display_name="echoFT", hidden=False)
    discovery.pair_apps(conn)
    assert sorted(_pairs(conn).values()) == [["1", "p.lib"]]


def test_a_user_unpair_survives_rediscovery(conn: sqlite3.Connection) -> None:
    ids = _register(conn, [("ios", "1", "libFT"), ("android", "p.lib", "libFT")])
    discovery.pair_apps(conn)
    discovery.unpair(conn, ids["1"])
    discovery.pair_apps(conn)
    assert _pairs(conn) == {}
    assert {a.pair_source for a in db.list_apps(conn)} == {"user"}


def test_an_auto_pair_is_dropped_when_it_becomes_ambiguous(conn: sqlite3.Connection) -> None:
    _register(conn, [("ios", "1", "Bus Wise"), ("android", "p.a", "Bus Wise")])
    discovery.pair_apps(conn)
    assert len(_pairs(conn)) == 1
    _register(conn, [("android", "p.b", "Bus Wise")])  # a second same-named Android app
    discovery.pair_apps(conn)
    assert _pairs(conn) == {}
    assert {a.pair_source for a in db.list_apps(conn)} == {None}


def test_manual_pair_releases_the_old_partner(conn: sqlite3.Connection) -> None:
    ids = _register(
        conn,
        [
            ("ios", "1", "Bus Wiser"),
            ("android", "p.a", "Bus Wiser"),
            ("android", "p.b", "Bus Wise"),
        ],
    )
    discovery.pair_apps(conn)
    discovery.pair_manually(conn, ids["1"], ids["p.b"])
    assert list(_pairs(conn).values()) == [["1", "p.b"]]
    old = next(a for a in db.list_apps(conn) if a.store_id == "p.a")
    assert (old.pair_key, old.pair_source) == (None, None)


def test_rename_and_hide_never_touch_pairing(conn: sqlite3.Connection) -> None:
    ids = _register(conn, [("ios", "1", "libFT"), ("android", "p.lib", "libFT")])
    discovery.pair_apps(conn)
    db.set_app_display(conn, ids["1"], display_name="Library", hidden=True)
    app = next(a for a in db.list_apps(conn) if a.id == ids["1"])
    assert (app.pair_source, app.label, app.hidden) == ("auto", "Library", True)


# -- digest: pairs and hidden apps ------------------------------------------------------------

AS_OF = date(2026, 9, 26)
TODAY = date(2026, 9, 27)
CFG = config.Config(
    apple=config.AppleConfig("i", "k", "1"), google=config.GoogleConfig("gs://x/", "a@b")
)


def _seed_pair(conn: sqlite3.Connection) -> dict[str, int]:
    ids = _register(
        conn,
        [("ios", "1", "libFT"), ("android", "p.lib", "libFT"), ("ios", "2", "deskFT")],
    )
    for i in range(7):
        d = (AS_OF - timedelta(days=i)).isoformat()
        db.replace_source_day(
            conn,
            "apple_sales",
            d,
            [
                db.MetricRow(d, ids["1"], "US", "installs", "", 3.0),
                db.MetricRow(d, ids["1"], "US", "proceeds", "USD", 1.0),
                db.MetricRow(d, ids["2"], "US", "installs", "", 5.0),
            ],
        )
        db.log_ingest(
            conn, source="apple_sales", report_date=d, started_at=db.utc_now(), status="ok"
        )
    rows = []
    for i in range(7):
        d = (AS_OF - timedelta(days=i)).isoformat()
        rows.append(db.MetricRow(d, ids["p.lib"], "ALL", "installs", "", 4.0))
    rows.append(db.MetricRow(AS_OF.isoformat(), ids["p.lib"], "US", "proceeds", "EUR", 2.0))
    db.replace_source_range(conn, "play_installs", "2026-09-20", "2026-09-26", rows[:-1])
    db.replace_source_day(conn, "play_earnings", AS_OF.isoformat(), rows[-1:])
    db.replace_source_range(
        conn,
        "play_vitals",
        AS_OF.isoformat(),
        AS_OF.isoformat(),
        [db.MetricRow(AS_OF.isoformat(), ids["p.lib"], "ALL", "crash_rate_28d", "", 0.02)],
        app_ids=[ids["p.lib"]],
    )
    db.log_ingest(
        conn, source="play_installs", report_date="2026-09-01", started_at=db.utc_now(), status="ok"
    )
    return ids


def _row_totals(text: str, name: str) -> list[str]:
    """A digest table row's last three cells: latest day, month and lifetime installs."""
    (row,) = [line for line in text.splitlines() if line.startswith(f"  {name} ")]
    return row.split()[-3:]


def test_a_pair_is_one_digest_row_with_per_currency_money(conn: sqlite3.Connection) -> None:
    _seed_pair(conn)
    discovery.pair_apps(conn)
    result = digest.build_digest(conn, CFG, today=TODAY)
    # 3 iOS + 4 Android installs a day; 21 + 28 over the week, which is also the whole
    # month and lifetime here. Every column is summed across the pair.
    assert _row_totals(result.text, "libFT (iOS + Android)") == ["*7*", "*49*", "*49*"]
    # Money summed per currency, never converted, on the line under the app.
    assert "    €2.00, $7.00\n" in result.text
    assert "(iOS)" not in result.text.replace("deskFT (iOS)", "")
    # Top app ranks the merged total (49), not deskFT's 35 or either platform alone.
    assert "Top app   libFT 49 installs" in result.text
    # Vitals stay per platform underneath the merged row.
    assert "libFT Android crash rate 2.0% ⚠" in result.text
    assert "crash — daily, 2.0% 28d" in result.html


def test_local_mode_never_merges(conn: sqlite3.Connection) -> None:
    _seed_pair(conn)  # no pair_apps call: local mode never writes pair_key
    result = digest.build_digest(conn, CFG, today=TODAY)
    assert _row_totals(result.text, "libFT (iOS)") == ["*3*", "*21*", "*21*"]
    assert _row_totals(result.text, "libFT (Android)") == ["*4*", "*28*", "*28*"]


def test_hidden_app_leaves_lists_but_stays_in_totals_and_warnings(
    conn: sqlite3.Connection,
) -> None:
    ids = _seed_pair(conn)
    db.set_app_display(conn, ids["p.lib"], display_name=None, hidden=True)
    result = digest.build_digest(conn, CFG, today=TODAY)
    assert "libFT (Android)" not in result.text
    assert "Installs  84" in result.text  # 21 + 35 iOS + 28 Android: totals unchanged
    assert "libFT Android crash rate 2.0% ⚠" in result.text


def test_display_name_replaces_the_store_name(conn: sqlite3.Connection) -> None:
    ids = _seed_pair(conn)
    db.set_app_display(conn, ids["2"], display_name="Desk", hidden=False)
    result = digest.build_digest(conn, CFG, today=TODAY)
    assert _row_totals(result.text, "Desk (iOS)") == ["*5*", "*35*", "*35*"]


def test_claim_totp_step_accepts_each_step_once() -> None:
    conn = db.connect(config.db_path())
    user_id = db.create_user(conn, "a@example.com", "hash", None)
    assert db.claim_totp_step(conn, user_id, 100)
    assert not db.claim_totp_step(conn, user_id, 100)  # the same code again
    assert not db.claim_totp_step(conn, user_id, 99)  # an older one
    assert db.claim_totp_step(conn, user_id, 101)
    other = db.create_user(conn, "b@example.com", "hash", None)
    assert db.claim_totp_step(conn, other, 100)  # per user
