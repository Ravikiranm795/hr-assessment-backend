"""RFC 6238 time-based one-time passwords (SHA-1, 6 digits, 30 s), standard library only.

Compatible with Google Authenticator, Microsoft Authenticator, 1Password and similar apps.
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6
WINDOW = 1  # accept one step either side for clock drift
RECOVERY_CODE_COUNT = 8


def generate_secret() -> str:
    """160-bit random secret, base32 without padding (what authenticator apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def code_at(secret: str, at: float | None = None) -> str:
    """The code valid at ``at`` (default now). Used by the verifier and by tests."""
    return _hotp(secret, int((time.time() if at is None else at) // STEP_SECONDS))


def verify(secret: str, code: str, at: float | None = None) -> bool:
    candidate = code.strip().replace(" ", "")
    if len(candidate) != DIGITS or not candidate.isdigit():
        return False
    counter = int((time.time() if at is None else at) // STEP_SECONDS)
    matched = False
    for drift in range(-WINDOW, WINDOW + 1):  # no early exit: constant work
        if hmac.compare_digest(_hotp(secret, counter + drift), candidate):
            matched = True
    return matched


def otpauth_uri(secret: str, account: str, issuer: str) -> str:
    return (
        f"otpauth://totp/{quote(issuer)}:{quote(account)}"
        f"?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"
    )


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """Human-friendly one-time codes like ``a1b2c-3d4e5`` (shown once, stored only as hashes)."""
    codes = []
    for _ in range(count):
        raw = secrets.token_hex(5)
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def normalize_recovery_code(code: str) -> str:
    return code.strip().lower().replace("-", "").replace(" ", "")
