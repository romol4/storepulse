from __future__ import annotations

import io

import httpx

from conftest import REVENUECAT_PROJECT, FakeRevenueCat, MemoryKeyring, revenuecat_overview, router
from storepulse.cli.main import Env, main
from storepulse.core import config
from storepulse.core.secrets import KeyringStore


def _env(answers: list[str], revenuecat: FakeRevenueCat | None = None) -> Env:
    it = iter(answers)
    return Env(
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        input=lambda prompt: next(it),
        getpass=lambda prompt: "sk_test_secretkey",
        transport=router(None, None, revenuecat),
        sleep=lambda _: None,
    )


def _out(env: Env) -> str:
    return env.stdout.getvalue()  # type: ignore[attr-defined, no-any-return]


def _err(env: Env) -> str:
    return env.stderr.getvalue()  # type: ignore[attr-defined, no-any-return]


def _revenuecat_only(*extra: str) -> list[str]:
    """Answers: Apple no, Google no, RevenueCat yes + project id, then ``extra``."""
    return ["n", "n", "y", REVENUECAT_PROJECT, *extra]


# -- init --------------------------------------------------------------------------------


def test_init_revenuecat_only(memory_keyring: MemoryKeyring) -> None:
    fake = FakeRevenueCat(default_overview=revenuecat_overview())
    env = _env(_revenuecat_only("n"), revenuecat=fake)
    assert main(["init", "--no-backfill"], env) == 0, _err(env)
    out = _out(env)
    assert "RevenueCat key accepted" in out
    assert "RevenueCat key fingerprint sha256:" in out
    cfg = config.load()
    assert cfg.apple is None and cfg.google is None
    assert cfg.revenuecat == config.RevenueCatConfig(REVENUECAT_PROJECT)
    store = KeyringStore(config_dir=config.config_dir())
    assert store.get("revenuecat_key") == "sk_test_secretkey"
    assert "sk_test_secretkey" not in (config.config_dir() / "config.toml").read_text()


def test_init_revenuecat_bad_key_fails_without_saving(memory_keyring: MemoryKeyring) -> None:
    fake = FakeRevenueCat(
        overview={REVENUECAT_PROJECT: httpx.Response(401, json={"message": "no"})}
    )
    env = _env(_revenuecat_only(), revenuecat=fake)
    assert main(["init"], env) == 1
    assert "revoked" in _err(env)
    assert not memory_keyring.store
    assert not (config.config_dir() / "config.toml").exists()


def test_init_revenuecat_bad_project_id_fails_without_saving(memory_keyring: MemoryKeyring) -> None:
    fake = FakeRevenueCat()  # nothing registered for any project id -> 404
    env = _env(_revenuecat_only(), revenuecat=fake)
    assert main(["init"], env) == 1
    assert "project ID" in _err(env)
    assert not memory_keyring.store


def test_init_neither_apple_google_nor_revenuecat_is_an_error(
    memory_keyring: MemoryKeyring,
) -> None:
    env = _env(["n", "n", "n", "n"])
    assert main(["init"], env) == 1
    assert "nothing was set up" in _err(env)


def _setup_revenuecat() -> FakeRevenueCat:
    fake = FakeRevenueCat(default_overview=revenuecat_overview())
    env = _env(_revenuecat_only("n"), revenuecat=fake)
    assert main(["init", "--no-backfill"], env) == 0
    return fake


def test_reinit_revenuecat_reuses_saved_key(memory_keyring: MemoryKeyring) -> None:
    _setup_revenuecat()
    # Re-run: blank key reuses the saved one; a different project id is still re-checked.
    fake = FakeRevenueCat(default_overview=revenuecat_overview())
    it_answers = ["n", "n", "y", REVENUECAT_PROJECT, "n"]
    inputs = iter(it_answers)
    passes = iter([""])  # blank key -> reuse the saved one
    env = Env(
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        input=lambda prompt: next(inputs),
        getpass=lambda prompt: next(passes),
        transport=router(None, None, fake),
        sleep=lambda _: None,
    )
    assert main(["init", "--no-backfill"], env) == 0, env.stderr.getvalue()  # type: ignore[attr-defined]
    store = KeyringStore(config_dir=config.config_dir())
    assert store.get("revenuecat_key") == "sk_test_secretkey"


# -- doctor ------------------------------------------------------------------------------


def test_doctor_revenuecat_ok(memory_keyring: MemoryKeyring) -> None:
    _setup_revenuecat()
    fake = FakeRevenueCat(default_overview=revenuecat_overview())
    env = _env([], revenuecat=fake)
    assert main(["doctor"], env) == 0
    out = _out(env)
    assert "All checks passed." in out
    assert "RevenueCat key accepted" in out


def test_doctor_revenuecat_revoked_key(memory_keyring: MemoryKeyring) -> None:
    _setup_revenuecat()
    broken = FakeRevenueCat(
        overview={REVENUECAT_PROJECT: httpx.Response(401, json={"message": "no"})}
    )
    env = _env([], revenuecat=broken)
    assert main(["doctor"], env) == 1
    out = _out(env)
    assert "FAIL" in out and "revoked" in out


def test_doctor_revenuecat_missing_key(memory_keyring: MemoryKeyring) -> None:
    _setup_revenuecat()
    store = KeyringStore(config_dir=config.config_dir())
    store.delete("revenuecat_key")
    env = _env([])
    assert main(["doctor"], env) == 1
    assert "no RevenueCat key saved" in _out(env)


def test_doctor_before_init() -> None:
    env = _env([])
    assert main(["doctor"], env) == 1
    assert "storepulse init" in _err(env)
