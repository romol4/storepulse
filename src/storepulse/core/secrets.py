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
# For scheduled runs, which can't prompt: a path to a 0600 file holding the passphrase.
# Only this module reads it (cli/schedule.py just writes the file and sets the env var).
PASSPHRASE_FILE_ENV = "STOREPULSE_PASSPHRASE_FILE"  # noqa: S105 (env var name)
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

    # Implementations must not assume a size limit (a service-account JSON is ~2.3 KB).
    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> None: ...

    def describe(self) -> str: ...

    def fingerprint(self, name: str) -> str | None: ...


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON via a temp file and ``os.replace``; 0600 on POSIX."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".secrets-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _seal(key: bytes, aad_prefix: bytes, name: str, value: str) -> dict[str, str]:
    """AES-GCM encrypt one entry, binding its name as associated data."""
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, value.encode("utf-8"), aad_prefix + name.encode())
    return {"nonce": _b64(nonce), "ciphertext": _b64(ciphertext)}


def _unseal(key: bytes, aad_prefix: bytes, name: str, entry: Any, where: Path) -> str:
    try:
        nonce = _unb64(entry["nonce"])
        ciphertext = _unb64(entry["ciphertext"])
        plain = AESGCM(key).decrypt(nonce, ciphertext, aad_prefix + name.encode())
    except InvalidTag:
        raise SecretStoreError(
            f"could not decrypt {name!r} in {where}: wrong passphrase or tampered file"
        ) from None
    except (KeyError, TypeError, ValueError):
        raise SecretStoreError(f"entry {name!r} in {where} is corrupt") from None
    value = plain.decode("utf-8")
    register_secret(value)
    return value


WRAPPED_FILENAME = "keychain-wrapped.json"
WRAPPED_FORMAT = "storepulse-keychain-wrapped"
WRAPPED_MARKER = "wrapped:v1"
WRAPPING_KEY_NAME = "__wrapping_key__"
_WRAPPED_AAD_PREFIX = b"storepulse-keychain-wrapped/v1/"
# Windows Credential Manager caps a secret at ~2.5 KB and keyring's encoding roughly
# doubles it, so anything over this goes into the wrapped file instead.
KEYCHAIN_DIRECT_LIMIT = 1024


class KeyringStore:
    """Secrets in the OS keychain via ``keyring``.

    Small secrets (like the Apple .p8) are stored directly. Larger ones (like a Google
    service-account JSON) are AES-GCM encrypted into ``keychain-wrapped.json`` under a
    random key that itself lives in the keychain; the keychain entry then holds only a
    marker. Without the keychain the file is useless.
    """

    kind = STORE_KEYRING

    def __init__(self, service: str = KEYRING_SERVICE, config_dir: Path | None = None) -> None:
        self.service = service
        self.config_dir = config_dir

    def _raw_get(self, name: str) -> str | None:
        try:
            return keyring.get_password(self.service, name)
        except keyring.errors.KeyringError as exc:
            raise SecretStoreError(
                f"could not read {name!r} from the OS keychain ({type(exc).__name__})"
            ) from None

    def _raw_set(self, name: str, value: str) -> None:
        try:
            keyring.set_password(self.service, name, value)
        except keyring.errors.KeyringError as exc:
            raise SecretStoreError(
                f"could not save {name!r} to the OS keychain ({type(exc).__name__})"
            ) from None

    def _raw_delete(self, name: str) -> None:
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(self.service, name)

    # -- wrapped storage for large secrets ---------------------------------------------

    @property
    def wrapped_path(self) -> Path:
        if self.config_dir is None:
            raise SecretStoreError("this keychain store has no config directory for large secrets")
        return self.config_dir / WRAPPED_FILENAME

    def _wrapping_key(self, create: bool) -> bytes:
        stored = self._raw_get(WRAPPING_KEY_NAME)
        if stored is not None:
            try:
                key = _unb64(stored)
            except ValueError:
                key = b""
            if len(key) != 32:
                raise SecretStoreError(
                    "the keychain wrapping key is corrupt; re-run `storepulse init`"
                )
            return key
        if not create:
            raise SecretStoreError(
                "the keychain wrapping key is missing, so large secrets can't be decrypted; "
                "re-run `storepulse init` (see README, 'Where secrets live')"
            )
        key = os.urandom(32)
        self._raw_set(WRAPPING_KEY_NAME, _b64(key))
        return key

    def _load_wrapped(self) -> dict[str, Any]:
        path = self.wrapped_path
        if not path.exists():
            return {"format": WRAPPED_FORMAT, "version": 1, "entries": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise SecretStoreError(f"{path} is unreadable or corrupt") from None
        if (
            not isinstance(data, dict)
            or data.get("format") != WRAPPED_FORMAT
            or data.get("version") != 1
            or not isinstance(data.get("entries"), dict)
        ):
            raise SecretStoreError(f"{path} is not a supported Storepulse wrapped-secrets file")
        return data

    # -- SecretStore -------------------------------------------------------------------

    def get(self, name: str) -> str | None:
        value = self._raw_get(name)
        if value != WRAPPED_MARKER:
            return value
        data = self._load_wrapped()
        entry = data["entries"].get(name)
        if entry is None:
            raise SecretStoreError(
                f"{name!r} is missing from {self.wrapped_path}; re-run `storepulse init`"
            )
        key = self._wrapping_key(create=False)
        return _unseal(key, _WRAPPED_AAD_PREFIX, name, entry, self.wrapped_path)

    def set(self, name: str, value: str) -> None:
        if name == WRAPPING_KEY_NAME:
            raise SecretStoreError(f"{name!r} is reserved")
        if len(value) <= KEYCHAIN_DIRECT_LIMIT:
            self._raw_set(name, value)
            self._drop_wrapped(name)
        else:
            key = self._wrapping_key(create=True)
            data = self._load_wrapped()
            data["entries"][name] = _seal(key, _WRAPPED_AAD_PREFIX, name, value)
            _atomic_write_json(self.wrapped_path, data)
            self._raw_set(name, WRAPPED_MARKER)
        register_secret(value)

    def _drop_wrapped(self, name: str) -> None:
        if self.config_dir is None or not self.wrapped_path.exists():
            return
        data = self._load_wrapped()
        if name in data["entries"]:
            del data["entries"][name]
            _atomic_write_json(self.wrapped_path, data)

    def delete(self, name: str) -> None:
        self._drop_wrapped(name)
        self._raw_delete(name)

    def describe(self) -> str:
        backend = keyring.get_keyring()
        label = getattr(backend, "name", type(backend).__name__)
        return f"OS keychain ({label})"

    def fingerprint(self, name: str) -> str | None:
        value = self.get(name)
        return None if value is None else fingerprint(value)


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
        _atomic_write_json(self.path, data)

    def _new_file(self) -> dict[str, Any]:
        return {
            "format": FILE_FORMAT,
            "version": FILE_VERSION,
            "kdf": {"name": "scrypt", "salt": _b64(os.urandom(16)), **_SCRYPT_DEFAULTS},
            "entries": {},
        }

    def _passphrase(self) -> str:
        # Direct value first, then the file (for scheduled runs, which can't prompt),
        # then the interactive callback; callers pass only their interactive prompt and
        # never read either variable themselves.
        source = self._passphrase_source
        value = os.environ.get(PASSPHRASE_ENV, "")
        if not value:
            value = self._read_passphrase_file()
        if not value and callable(source):
            value = source()
        elif not value and isinstance(source, str):
            value = source
        if not value:
            raise SecretStoreError(
                f"a passphrase is required to unlock {self.path}; set {PASSPHRASE_ENV} or "
                f"{PASSPHRASE_FILE_ENV}, or run interactively (see README, 'Where secrets live')"
            )
        return value

    def _read_passphrase_file(self) -> str:
        file_path = os.environ.get(PASSPHRASE_FILE_ENV, "")
        if not file_path:
            return ""
        try:
            return Path(file_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SecretStoreError(
                f"could not read {PASSPHRASE_FILE_ENV} at {file_path!r}: {type(exc).__name__}"
            ) from None

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

    def _decrypt(self, data: dict[str, Any], name: str, entry: Any) -> str:
        return _unseal(self._derive(data["kdf"]), _AAD_PREFIX, name, entry, self.path)

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
        data["entries"][name] = _seal(key, _AAD_PREFIX, name, value)
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
        return KeyringStore(config_dir=config_dir)
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
        return KeyringStore(config_dir=config_dir)
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
