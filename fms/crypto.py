"""Encryption at rest for secrets (passwords, cookies, proxy creds, 2FA secrets).

Uses Fernet (AES-128-CBC + HMAC). The key lives in a local file inside the data
directory (see config.key_path) and never leaves the laptop. Back it up: if the
key file is lost, stored secrets cannot be decrypted.
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from . import config


def _load_or_create_key() -> bytes:
    kp = config.key_path()
    if kp.exists():
        return kp.read_bytes().strip()
    key = Fernet.generate_key()
    kp.write_bytes(key)
    try:
        # Best effort: restrict permissions (no-op on Windows, fine).
        kp.chmod(0o600)
    except OSError:
        pass
    return key


_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def reset_fernet_cache() -> None:
    """Forget the cached key (used in tests that switch FMS_DATA_DIR)."""
    global _fernet
    _fernet = None


def encrypt_str(plaintext: str | None) -> str | None:
    if plaintext is None or plaintext == "":
        return None
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_str(token: str | None) -> str | None:
    if token is None or token == "":
        return None
    try:
        return _get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError(
            "Could not decrypt a stored secret. The key file may have changed "
            f"or the data is corrupt ({config.key_path()})."
        ) from exc
