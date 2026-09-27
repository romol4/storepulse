"""Secret storage and redaction.

This is the only module that reads credential material from the OS keychain, the
encrypted secrets file, or secret-bearing environment variables.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Protocol

import keyring
import keyring.errors
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

KEYRING_SERVICE = "storepulse"
PASSPHRASE_ENV = "STOREPULSE_PASSPHRASE"  # noqa: S105 (env var name)
SECRETS_FILENAME = "secrets.enc"

FILE_FORMAT = "storepulse-secrets"
FILE_VERSION = 1
_AAD_PREFIX = b"storepulse-secrets/v1/"
_SCRYPT_DEFAULTS = {"n": 2**15, "r": 8, "p": 1}

STORE_KEYRING = "keyring"
STORE_FILE = "file"


class SecretStoreError(Exception):
    """Raised for store failures. Messages never contain secret values."""


def fingerprint(value: str) -> str:
    """Stable, non-reversible identifier shown in place of a secret."""
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


class SecretStore(Protocol):
    kind: str

    def get(self, name: str) -> str | None: ...

    # Implementations must not assume a size limit: Phase 2 stores service-account JSON,
    # which exceeds Windows Credential Manager's ~2.5 KB cap and will need chunking or
    # a keychain-held wrapping key.
    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> None: ...

    def describe(self) -> str: ...

    def fingerprint(self, name: str) -> str | None: ...


class KeyringStore:
    """Secrets in the OS keychain via ``keyring``."""

    kind = STORE_KEYRING

    def __init__(self, service: str = KEYRING_SERVICE) -> None:
        self.service = service

    def get(self, name: str) -> str | None:
        try:
            return keyring.get_password(self.service, name)
        except keyring.errors.KeyringError as exc:
            raise SecretStoreError(
                f"could not read {name!r} from the OS keychain ({type(exc).__name__})"
            ) from None

    def set(self, name: str, value: str) -> None:
        try:
            keyring.set_password(self.service, name, value)
        except keyring.errors.KeyringError as exc:
            raise SecretStoreError(
                f"could not save {name!r} to the OS keychain ({type(exc).__name__})"
            ) from None
        register_secret(value)

    def delete(self, name: str) -> None:
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(self.service, name)

    def describe(self) -> str:
        backend = keyring.get_keyring()
        label = getattr(backend, "name", type(backend).__name__)
        return f"OS keychain ({label})"

    def fingerprint(self, name: str) -> str | None:
        value = self.get(name)
        return None if value is None else fingerprint(value)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


class EncryptedFileStore:
    """Secrets in one JSON file, each entry AES-GCM encrypted under a scrypt-derived key.

    File layout::

        {"format": "storepulse-secrets", "version": 1,
         "kdf": {"name": "scrypt", "salt": <b64>, "n": .., "r": .., "p": ..},
         "entries": {"<name>": {"nonce": <b64>, "ciphertext": <b64>}}}

    The entry name is bound as associated data, so moving a ciphertext to another name
    fails authentication.
    """

    kind = STORE_FILE

    def __init__(
        self,
        path: Path,
        passphrase: Callable[[], str] | str | None = None,
        reason: str = "",
    ) -> None:
        self.path = path
        self._passphrase_source = passphrase
        self._reason = reason
        self._key: bytes | None = None

    # -- file handling -------------------------------------------------------------

    def _load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise SecretStoreError(f"secrets file {self.path} is unreadable or corrupt") from None
        if not isinstance(data, dict) or data.get("format") != FILE_FORMAT:
            raise SecretStoreError(f"{self.path} is not a Storepulse secrets file")
        if data.get("version") != FILE_VERSION:
            raise SecretStoreError(
                f"secrets file {self.path} has unsupported version {data.get('version')!r}; "
                "upgrade Storepulse"
            )
        kdf = data.get("kdf")
        if not isinstance(kdf, dict) or kdf.get("name") != "scrypt":
            raise SecretStoreError(f"secrets file {self.path} has an unsupported KDF")
        if not isinstance(data.get("entries"), dict):
            raise SecretStoreError(f"secrets file {self.path} is corrupt")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".secrets-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1, sort_keys=True)
            if os.name == "posix":
                os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def _new_file(self) -> dict[str, Any]:
        return {
            "format": FILE_FORMAT,
            "version": FILE_VERSION,
            "kdf": {"name": "scrypt", "salt": _b64(os.urandom(16)), **_SCRYPT_DEFAULTS},
            "entries": {},
        }

    def _passphrase(self) -> str:
        source = self._passphrase_source
        if callable(source):
            value = source()
        elif isinstance(source, str):
            value = source
        else:
            value = os.environ.get(PASSPHRASE_ENV, "")
        if not value:
            raise SecretStoreError(
                f"a passphrase is required to unlock {self.path}; set {PASSPHRASE_ENV} "
                "or run interactively (see README, 'Where secrets live')"
            )
        return value

    def _derive(self, kdf: dict[str, Any]) -> bytes:
        if self._key is None:
            try:
                salt = _unb64(str(kdf["salt"]))
                n, r, p = int(kdf["n"]), int(kdf["r"]), int(kdf["p"])
            except (KeyError, ValueError, TypeError):
                raise SecretStoreError(f"secrets file {self.path} has a corrupt header") from None
            self._key = Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(
                self._passphrase().encode("utf-8")
            )
        return self._key

    @staticmethod
    def _aad(name: str) -> bytes:
        return _AAD_PREFIX + name.encode("utf-8")

    def _decrypt(self, data: dict[str, Any], name: str, entry: Any) -> str:
        key = self._derive(data["kdf"])
        try:
            nonce = _unb64(entry["nonce"])
            ciphertext = _unb64(entry["ciphertext"])
            plain = AESGCM(key).decrypt(nonce, ciphertext, self._aad(name))
        except InvalidTag:
            raise SecretStoreError(
                f"could not decrypt {name!r} in {self.path}: wrong passphrase or tampered file"
            ) from None
        except (KeyError, TypeError, ValueError):
            raise SecretStoreError(f"entry {name!r} in {self.path} is corrupt") from None
        value = plain.decode("utf-8")
        register_secret(value)
        return value

    # -- SecretStore -----------------------------------------------------------------

    def get(self, name: str) -> str | None:
        data = self._load()
        if data is None or name not in data["entries"]:
            return None
        return self._decrypt(data, name, data["entries"][name])

    def set(self, name: str, value: str) -> None:
        data = self._load() or self._new_file()
        key = self._derive(data["kdf"])
        # Verify the passphrase against an existing entry before adding a new one, so a
        # typo doesn't leave a file whose entries need different passphrases.
        first = next(iter(data["entries"].items()), None)
        if first is not None:
            self._decrypt(data, *first)
        nonce = os.urandom(12)
        ciphertext = AESGCM(key).encrypt(nonce, value.encode("utf-8"), self._aad(name))
        data["entries"][name] = {"nonce": _b64(nonce), "ciphertext": _b64(ciphertext)}
        self._write(data)
        register_secret(value)

    def delete(self, name: str) -> None:
        data = self._load()
        if data is not None and name in data["entries"]:
            del data["entries"][name]
            self._write(data)

    def describe(self) -> str:
        suffix = f" ({self._reason})" if self._reason else ""
        return f"encrypted file {self.path}{suffix}"

    def fingerprint(self, name: str) -> str | None:
        value = self.get(name)
        return None if value is None else fingerprint(value)


def keyring_available() -> bool:
    """False when keyring only has its fail/null backend (headless machines)."""
    backend = keyring.get_keyring()
    module = type(backend).__module__
    if module.startswith(("keyring.backends.fail", "keyring.backends.null")):
        return False
    priority = getattr(backend, "priority", 1)
    return isinstance(priority, int | float) and priority > 0


def default_store(
    config_dir: Path, passphrase: Callable[[], str] | str | None = None
) -> SecretStore:
    """Pick a store for first-time setup: the OS keychain, else an encrypted file.

    Only ``init`` may call this. Every later run (including scheduled runs in Phase 3,
    where cron often has no Secret Service session) must open the kind recorded in
    config with ``open_store`` and fail clearly if it's unreachable, never silently
    re-detect and fall back to a different store.
    """
    if keyring_available():
        return KeyringStore()
    return EncryptedFileStore(
        config_dir / SECRETS_FILENAME, passphrase, reason="no OS keychain available"
    )


def open_store(
    kind: str, config_dir: Path, passphrase: Callable[[], str] | str | None = None
) -> SecretStore:
    """Open exactly the store kind recorded at setup time."""
    if kind == STORE_KEYRING:
        if not keyring_available():
            raise SecretStoreError(
                "secrets were saved in the OS keychain, but no keychain is reachable from "
                "this session; run from a desktop session or re-run `storepulse init` "
                "(see README, 'Where secrets live')"
            )
        return KeyringStore()
    if kind == STORE_FILE:
        return EncryptedFileStore(config_dir / SECRETS_FILENAME, passphrase)
    raise SecretStoreError(f"unknown secret store {kind!r} in config; re-run `storepulse init`")


# -- redaction -------------------------------------------------------------------------

_PEM_RE = re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.DOTALL)
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_REDACTED = "[REDACTED]"

_registered: set[str] = set()


def register_secret(value: str) -> None:
    """Remember a secret value so ``redact`` strips it wherever it appears."""
    if len(value) >= 8:
        _registered.add(value)
        # PEM bodies are often logged line by line; register each long line too.
        for line in value.splitlines():
            if len(line.strip()) >= 16 and not line.startswith("-----"):
                _registered.add(line.strip())


def redact(text: str, extra: Iterable[str] = ()) -> str:
    """Strip PEM blocks, JWTs, bearer tokens and known secret values from ``text``."""
    text = _PEM_RE.sub(_REDACTED, text)
    text = _JWT_RE.sub(_REDACTED, text)
    text = _BEARER_RE.sub(lambda m: m.group(1) + _REDACTED, text)
    for value in sorted({*_registered, *extra}, key=len, reverse=True):
        if value:
            text = text.replace(value, _REDACTED)
    return text


class RedactingFilter(logging.Filter):
    """Logging filter that redacts every formatted record."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = None
        return True
