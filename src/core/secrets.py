"""Rotatable envelope-encryption boundary for application secrets.

Production keeps this material separate from JWT signing and can decrypt with
short-lived previous keys during a controlled rotation. A KMS/secret manager
can supply the same values without changing callers.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from src.core.config import settings


def _fernet_for(material: str) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(material.encode("utf-8")).digest())
    return Fernet(key)


def _key_materials() -> list[str]:
    """Primary key followed by rotation fallbacks, without duplicates."""
    values = [settings.DATA_ENCRYPTION_KEY or settings.SECRET_KEY]
    values.extend(key.strip() for key in settings.PREVIOUS_DATA_ENCRYPTION_KEYS.split(",") if key.strip())
    # Existing installs encrypted values with SECRET_KEY. Keeping it as a
    # transition fallback lets them introduce DATA_ENCRYPTION_KEY safely;
    # remove it from PREVIOUS_DATA_ENCRYPTION_KEYS only after re-encryption.
    if settings.DATA_ENCRYPTION_KEY:
        values.append(settings.SECRET_KEY)
    return list(dict.fromkeys(values))


def encrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    return _fernet_for(_key_materials()[0]).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    for material in _key_materials():
        try:
            return _fernet_for(material).decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError, ValueError):
            continue
    return None
