from __future__ import annotations

from pathlib import Path

import pytest

from storepulse.core import config


def test_email_digest_schedule_roundtrip(tmp_path: Path) -> None:
    cfg = config.Config(
        secret_store="keyring",
        email=config.EmailConfig(
            host="smtp.gmail.com",
            port=587,
            security="starttls",
            username="me@example.com",
            from_addr="me@example.com",
            to_addrs=["me@example.com", "you@example.com"],
        ),
        digest=config.DigestConfig(
            crash_threshold=0.02,
            anr_threshold=0.01,
            stale_days=5,
            rates={"EUR": 0.92, "GBP": 0.79},
            display_currency="USD",
        ),
        schedule=config.ScheduleConfig(run_time="09:00", days=10),
    )
    path = tmp_path / "config.toml"
    config.save(cfg, path)
    loaded = config.load(path)
    assert loaded.email == cfg.email
    assert loaded.digest == cfg.digest
    assert loaded.schedule == cfg.schedule
    assert loaded.extra == {}


def test_defaults_when_sections_absent(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config.save(config.Config(secret_store="file"), path)
    loaded = config.load(path)
    assert loaded.email is None
    assert loaded.digest == config.DigestConfig()
    assert loaded.schedule == config.ScheduleConfig()


def test_missing_config_file_has_defaults(tmp_path: Path) -> None:
    loaded = config.load(tmp_path / "does-not-exist.toml")
    assert loaded.digest == config.DigestConfig()
    assert loaded.schedule == config.ScheduleConfig()
    assert loaded.email is None


def test_email_requires_all_fields(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[email]\nhost = "smtp.example.com"\n')
    with pytest.raises(config.ConfigError, match=r"\[email\]"):
        config.load(path)


def test_email_rejects_bad_security(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[email]\n"
        'host = "smtp.example.com"\n'
        "port = 587\n"
        'security = "plaintext"\n'
        'username = "me"\n'
        'from_addr = "me@example.com"\n'
        'to_addrs = ["me@example.com"]\n'
    )
    with pytest.raises(config.ConfigError, match="security"):
        config.load(path)


def test_email_to_addrs_must_be_a_list(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[email]\n"
        'host = "smtp.example.com"\n'
        "port = 587\n"
        'security = "ssl"\n'
        'username = "me"\n'
        'from_addr = "me@example.com"\n'
        'to_addrs = "me@example.com"\n'
    )
    with pytest.raises(config.ConfigError, match="to_addrs"):
        config.load(path)


def test_digest_partial_overrides_keep_other_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[digest]\nstale_days = 2\n")
    loaded = config.load(path)
    assert loaded.digest.stale_days == 2
    assert loaded.digest.crash_threshold == config.DEFAULT_CRASH_THRESHOLD
    assert loaded.digest.anr_threshold == config.DEFAULT_ANR_THRESHOLD


def test_digest_rates_must_be_a_table(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[digest]\nrates = ["EUR"]\n')
    with pytest.raises(config.ConfigError, match="rates"):
        config.load(path)


def test_currency_codes_needing_quotes_roundtrip(tmp_path: Path) -> None:
    # TOML bare keys can't start with a digit or contain some symbols; quoting in the
    # writer means any currency code (however unusual) round-trips safely.
    cfg = config.Config(digest=config.DigestConfig(rates={"USD": 1.0, "999": 2.0}))
    path = tmp_path / "config.toml"
    config.save(cfg, path)
    assert config.load(path).digest.rates == {"USD": 1.0, "999": 2.0}
