"""Authentication primitives.

The service never stores passwords or personal identifiers. Session and recovery
tokens carry enough entropy to use a SHA-256 digest; recovery codes are stretched
with scrypt and a per-code salt as defense in depth.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Final

MAX_TOKEN_LENGTH: Final = 512
RECOVERY_LOOKUP_LENGTH: Final = 12
SCRYPT_N: Final = 2**14
SCRYPT_R: Final = 8
SCRYPT_P: Final = 1
SCRYPT_DKLEN: Final = 32


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def generate_recovery_code() -> str:
    return secrets.token_urlsafe(24)


def recovery_lookup(code: str) -> str:
    return code[:RECOVERY_LOOKUP_LENGTH]


def generate_salt() -> str:
    return secrets.token_hex(16)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_recovery_code(code: str, salt: str) -> str:
    digest = hashlib.scrypt(
        code.encode("utf-8"),
        salt=bytes.fromhex(salt),
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return digest.hex()


def verify_recovery_code(code: str, salt: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_recovery_code(code, salt), expected_hash)
