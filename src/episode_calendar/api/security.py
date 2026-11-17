"""Passwordless login primitives.

The service never stores passwords. Login codes are short-lived, so a database
dump alone must not be enough to recover them: codes are stretched with scrypt
and a per-token salt. Magic-link and session tokens carry enough entropy to use
a plain SHA-256 digest.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Final

EMAIL_PATTERN: Final = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_EMAIL_LENGTH: Final = 320
MAX_TOKEN_LENGTH: Final = 512
OTP_DIGITS: Final = 6
OTP_ATTEMPTS: Final = 5
SCRYPT_N: Final = 2**14
SCRYPT_R: Final = 8
SCRYPT_P: Final = 1
SCRYPT_DKLEN: Final = 32


def normalize_email(value: str) -> str:
    """Return the canonical lookup form of an email address."""

    return value.strip().lower()


def is_valid_email(value: str) -> bool:
    return len(value) <= MAX_EMAIL_LENGTH and EMAIL_PATTERN.fullmatch(value) is not None


def generate_login_code() -> str:
    return f"{secrets.randbelow(10**OTP_DIGITS):0{OTP_DIGITS}d}"


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def generate_salt() -> str:
    return secrets.token_hex(16)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_login_code(code: str, salt: str) -> str:
    digest = hashlib.scrypt(
        code.encode("utf-8"),
        salt=bytes.fromhex(salt),
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return digest.hex()


def verify_login_code(code: str, salt: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_login_code(code, salt), expected_hash)
