from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import keyring
import pytest
from keyring.backends import fail

from conftest import MemoryKeyring
from storepulse.core import secrets
from storepulse.core.secrets import (
    EncryptedFileStore,
    KeyringStore,
    SecretStoreError,
    default_store,
    open_store,
    redact,
)

PEM = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIGTAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBHkwdwIBAQQg\n"
    "-----END PRIVATE KEY-----\n"
)


@pytest.fixture
def no_keychain() -> Iterator[None]:
    previous = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    yield
    keyring.set_keyring(previous)


def test_file_store_roundtrip_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "secrets.enc"
    EncryptedFileStore(path, "correct horse").set("apple_p8", PEM)
    EncryptedFileStore(path, "correct horse").set("smtp_password", "hunter2hunter2")
    # A fresh instance reads the salt and scrypt parameters back from the header.
    reopened = EncryptedFileStore(path, "correct horse")
    assert reopened.get("apple_p8") == PEM
    assert reopened.get("smtp_password") == "hunter2hunter2"
    assert reopened.get("missing") is None

    data = json.loads(path.read_text())
    assert data["format"] == "storepulse-secrets" and data["version"] == 1
    assert data["kdf"]["name"] == "scrypt" and len(data["kdf"]["salt"]) > 0
    assert {"n", "r", "p"} <= set(data["kdf"])
    raw = path.read_text()
    assert "PRIVATE KEY" not in raw and "hunter2" not in raw


def test_file_store_nonces_unique(tmp_path: Path) -> None:
    path = tmp_path / "secrets.enc"
    store = EncryptedFileStore(path, "pw-pw-pw-pw")
    store.set("a", "same value!")
    store.set("b", "same value!")
    entries = json.loads(path.read_text())["entries"]
    assert entries["a"]["nonce"] != entries["b"]["nonce"]
    assert entries["a"]["ciphertext"] != entries["b"]["ciphertext"]


def test_wrong_passphrase_fails_without_leaking(tmp_path: Path) -> None:
    path = tmp_path / "secrets.enc"
    EncryptedFileStore(path, "right-passphrase").set("apple_p8", PEM)
    with pytest.raises(SecretStoreError, match="wrong passphrase") as info:
        EncryptedFileStore(path, "wrong-passphrase").get("apple_p8")
    assert "PRIVATE" not in str(info.value)
    # And a wrong passphrase can't add entries encrypted under a different key.
    with pytest.raises(SecretStoreError):
        EncryptedFileStore(path, "wrong-passphrase").set("other", "value1234")


def test_swapped_entries_fail_authentication(tmp_path: Path) -> None:
    path = tmp_path / "secrets.enc"
    store = EncryptedFileStore(path, "passphrase!")
    store.set("apple_p8", PEM)
    store.set("smtp_password", "hunter2hunter2")
    data = json.loads(path.read_text())
    e = data["entries"]
    e["apple_p8"], e["smtp_password"] = e["smtp_password"], e["apple_p8"]
    path.write_text(json.dumps(data))
    with pytest.raises(SecretStoreError, match="tampered"):
        EncryptedFileStore(path, "passphrase!").get("apple_p8")


def test_unknown_version_rejected(tmp_path: Path) -> None:
    path = tmp_path / "secrets.enc"
    EncryptedFileStore(path, "passphrase!").set("a", "value1234")
    data = json.loads(path.read_text())
    data["version"] = 99
    path.write_text(json.dumps(data))
    with pytest.raises(SecretStoreError, match="version"):
        EncryptedFileStore(path, "passphrase!").get("a")


def test_not_a_secrets_file(tmp_path: Path) -> None:
    path = tmp_path / "secrets.enc"
    path.write_text("{}")
    with pytest.raises(SecretStoreError, match="not a Storepulse"):
        EncryptedFileStore(path, "x").get("a")


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_file_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "secrets.enc"
    EncryptedFileStore(path, "passphrase!").set("a", "value1234")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_passphrase_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STOREPULSE_PASSPHRASE", "from-env-pass")
    path = tmp_path / "secrets.enc"
    EncryptedFileStore(path).set("a", "value1234")
    assert EncryptedFileStore(path, "from-env-pass").get("a") == "value1234"


def test_missing_passphrase_explains(tmp_path: Path) -> None:
    with pytest.raises(SecretStoreError, match="STOREPULSE_PASSPHRASE"):
        EncryptedFileStore(tmp_path / "secrets.enc").set("a", "value1234")


def test_delete(tmp_path: Path) -> None:
    store = EncryptedFileStore(tmp_path / "secrets.enc", "passphrase!")
    store.set("a", "value1234")
    store.delete("a")
    assert store.get("a") is None


def test_keyring_store(memory_keyring: MemoryKeyring) -> None:
    store = KeyringStore()
    store.set("apple_p8", PEM)
    assert memory_keyring.store[("storepulse", "apple_p8")] == PEM
    assert store.get("apple_p8") == PEM
    assert store.fingerprint("apple_p8") == secrets.fingerprint(PEM)
    assert "OS keychain" in store.describe()
    store.delete("apple_p8")
    store.delete("apple_p8")  # idempotent
    assert store.get("apple_p8") is None


def test_default_store_prefers_keychain(memory_keyring: MemoryKeyring, tmp_path: Path) -> None:
    assert isinstance(default_store(tmp_path), KeyringStore)


def test_default_store_falls_back_and_says_why(no_keychain: None, tmp_path: Path) -> None:
    store = default_store(tmp_path, passphrase="passphrase!")
    assert isinstance(store, EncryptedFileStore)
    assert store.kind == "file"
    assert "no OS keychain available" in store.describe()
    assert str(tmp_path / "secrets.enc") in store.describe()


def test_open_store_never_falls_back(no_keychain: None, tmp_path: Path) -> None:
    with pytest.raises(SecretStoreError, match="no keychain is reachable"):
        open_store("keyring", tmp_path)
    assert isinstance(open_store("file", tmp_path, "x"), EncryptedFileStore)
    with pytest.raises(SecretStoreError):
        open_store("mystery", tmp_path)


def test_fingerprint_stable() -> None:
    assert secrets.fingerprint("abc") == secrets.fingerprint("abc")
    assert secrets.fingerprint("abc") != secrets.fingerprint("abd")
    assert secrets.fingerprint("abc").startswith("sha256:")
    assert len(secrets.fingerprint("abc")) == len("sha256:") + 12


def test_redact_patterns() -> None:
    jwt_like = "eyJhbGciOiJFUzI1NiJ9.eyJpc3MiOiJ4In0.c2lnbmF0dXJl"
    text = f"key={PEM} token {jwt_like} header Authorization: Bearer abc.def-ghi"
    cleaned = redact(text, extra=["supersecretvalue"])
    assert "PRIVATE KEY" not in cleaned
    assert jwt_like not in cleaned
    assert "abc.def-ghi" not in cleaned
    assert redact("pw supersecretvalue!", extra=["supersecretvalue"]) == "pw [REDACTED]!"


def test_registered_secrets_redacted(tmp_path: Path) -> None:
    EncryptedFileStore(tmp_path / "s.enc", "passphrase!").set("smtp", "registered-secret-42")
    assert "registered-secret-42" not in redact("login failed for registered-secret-42")
    # A PEM body line on its own is also caught.
    secrets.register_secret(PEM)
    assert PEM.splitlines()[1] not in redact("oops " + PEM.splitlines()[1])


def test_logging_filter(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("storepulse.test")
    handler_filter = secrets.RedactingFilter()
    logger.addFilter(handler_filter)
    try:
        with caplog.at_level(logging.WARNING, logger="storepulse.test"):
            logger.warning("key is %s", PEM)
        assert "PRIVATE KEY" not in caplog.text
        assert "[REDACTED]" in caplog.text
    finally:
        logger.removeFilter(handler_filter)


# -- keychain wrapping key for large secrets ---------------------------------------------

BIG = '{"type": "service_account", "private_key": "' + "k" * 2000 + '"}'


def test_large_secret_is_wrapped(memory_keyring: MemoryKeyring, tmp_path: Path) -> None:
    store = KeyringStore(config_dir=tmp_path)
    store.set("google_sa", BIG)
    assert memory_keyring.store[("storepulse", "google_sa")] == secrets.WRAPPED_MARKER
    assert ("storepulse", secrets.WRAPPING_KEY_NAME) in memory_keyring.store
    wrapped = (tmp_path / secrets.WRAPPED_FILENAME).read_text()
    assert "kkkk" not in wrapped
    assert KeyringStore(config_dir=tmp_path).get("google_sa") == BIG
    # Every keychain value stays well under Windows Credential Manager's limit.
    assert all(len(v) <= secrets.KEYCHAIN_DIRECT_LIMIT for v in memory_keyring.store.values())


def test_small_secret_stays_direct(memory_keyring: MemoryKeyring, tmp_path: Path) -> None:
    KeyringStore(config_dir=tmp_path).set("apple_p8", PEM)
    assert memory_keyring.store[("storepulse", "apple_p8")] == PEM
    assert not (tmp_path / secrets.WRAPPED_FILENAME).exists()


def test_missing_wrapping_key_is_clear(memory_keyring: MemoryKeyring, tmp_path: Path) -> None:
    store = KeyringStore(config_dir=tmp_path)
    store.set("google_sa", BIG)
    del memory_keyring.store[("storepulse", secrets.WRAPPING_KEY_NAME)]
    with pytest.raises(SecretStoreError, match="wrapping key is missing"):
        store.get("google_sa")


def test_wrapped_entry_moved_fails(memory_keyring: MemoryKeyring, tmp_path: Path) -> None:
    store = KeyringStore(config_dir=tmp_path)
    store.set("google_sa", BIG)
    store.set("other_big", BIG + "x")
    path = tmp_path / secrets.WRAPPED_FILENAME
    data = json.loads(path.read_text())
    e = data["entries"]
    e["google_sa"], e["other_big"] = e["other_big"], e["google_sa"]
    path.write_text(json.dumps(data))
    with pytest.raises(SecretStoreError, match="tampered"):
        store.get("google_sa")


def test_delete_cleans_both_places(memory_keyring: MemoryKeyring, tmp_path: Path) -> None:
    store = KeyringStore(config_dir=tmp_path)
    store.set("google_sa", BIG)
    store.delete("google_sa")
    assert ("storepulse", "google_sa") not in memory_keyring.store
    data = json.loads((tmp_path / secrets.WRAPPED_FILENAME).read_text())
    assert data["entries"] == {}
    assert store.get("google_sa") is None


def test_shrinking_secret_drops_wrapped_copy(memory_keyring: MemoryKeyring, tmp_path: Path) -> None:
    store = KeyringStore(config_dir=tmp_path)
    store.set("google_sa", BIG)
    store.set("google_sa", "small-now-12345")
    data = json.loads((tmp_path / secrets.WRAPPED_FILENAME).read_text())
    assert data["entries"] == {} and store.get("google_sa") == "small-now-12345"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_wrapped_file_mode_0600(memory_keyring: MemoryKeyring, tmp_path: Path) -> None:
    KeyringStore(config_dir=tmp_path).set("google_sa", BIG)
    assert stat.S_IMODE((tmp_path / secrets.WRAPPED_FILENAME).stat().st_mode) == 0o600
    # Written atomically: no temp files left behind.
    assert [p.name for p in tmp_path.iterdir()] == [secrets.WRAPPED_FILENAME]
