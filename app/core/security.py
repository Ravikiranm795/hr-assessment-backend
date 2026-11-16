"""Password hashing, tokens, HMAC hashing and field encryption (spec 4.2, 4.8, 8.1).

Key formats (environment):
- ``JWT_PRIVATE_KEY`` / ``JWT_PUBLIC_KEY``: PEM, Ed25519 (EdDSA) or RSA (RS256), with ``JWT_KID``.
- ``FIELD_ENCRYPTION_KEYS``: comma-separated ``<kid>:<base64 32 bytes>``; the first is the
  active encryption key, all are accepted for decryption (rotation).
- ``TOKEN_PEPPER``: HMAC-SHA-512 key for token hashes and the email blind index.
"""

import base64
import hashlib
import hmac
import os
import secrets
import string
import time
import unicodedata
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key

from app.core.config import settings
from app.core.errors import AppError
from app.core.roles import CANDIDATE

# --------------------------------------------------------------------------- passwords

PASSWORD_MIN_LENGTH = 12
TEMP_PASSWORD_LENGTH = 16
TEMP_PASSWORD_SYMBOLS = "!@#$%^&*-_=+?"
# Small built-in blocklist; checked case-insensitively, also with trailing digits/symbols removed.
COMMON_PASSWORDS = frozenset(
    {
        "password",
        "password1",
        "passw0rd",
        "qwerty",
        "qwertyuiop",
        "letmein",
        "welcome",
        "admin",
        "administrator",
        "iloveyou",
        "monkey",
        "dragon",
        "football",
        "baseball",
        "abc123",
        "123456",
        "12345678",
        "123456789",
        "1234567890",
        "123123",
        "111111",
        "sunshine",
        "princess",
        "trustno1",
        "changeme",
        "secret",
        "master",
        "login",
        "starwars",
        "whatever",
    }
)


@lru_cache(maxsize=4)
def _hasher(time_cost: int, memory_kib: int, parallelism: int) -> PasswordHasher:
    return PasswordHasher(time_cost=time_cost, memory_cost=memory_kib, parallelism=parallelism)


def _current_hasher() -> PasswordHasher:
    return _hasher(settings.argon2_time_cost, settings.argon2_memory_kib, settings.argon2_parallelism)


def hash_password(pw: str) -> str:
    """Argon2id hash (parameters from settings, calibrated to about 250 ms)."""
    return _current_hasher().hash(pw)


def verify_password(hash: str, pw: str) -> bool:  # noqa: A002 - frozen signature
    try:
        return _current_hasher().verify(hash, pw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(hash: str) -> bool:  # noqa: A002
    try:
        return _current_hasher().check_needs_rehash(hash)
    except InvalidHashError:
        return True


_DUMMY_HASH: str | None = None


def dummy_verify(pw: str) -> None:
    """Spend the same time as a real verify; call on unknown-user logins (constant-time checks)."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_hex(16))
    verify_password(_DUMMY_HASH, pw)


def password_policy_violations(
    pw: str, *, min_length: int = PASSWORD_MIN_LENGTH, blocklist: frozenset[str] = COMMON_PASSWORDS
) -> list[str]:
    """Return policy codes the password breaks (``too_short``, ``common_password``); empty is ok.
    Reuse of the last 5 hashes is checked by the auth service against ``password_history``."""
    problems = []
    if len(pw) < min_length:
        problems.append("too_short")
    lowered = pw.lower()
    stripped = lowered.rstrip(string.digits + string.punctuation)
    if lowered in blocklist or stripped in blocklist:
        problems.append("common_password")
    return problems


def generate_temp_password() -> str:
    """Random strong password with at least one lower, upper, digit and symbol."""
    alphabet = string.ascii_letters + string.digits + TEMP_PASSWORD_SYMBOLS
    rng = secrets.SystemRandom()
    while True:
        chars = [
            secrets.choice(string.ascii_lowercase),
            secrets.choice(string.ascii_uppercase),
            secrets.choice(string.digits),
            secrets.choice(TEMP_PASSWORD_SYMBOLS),
        ] + [secrets.choice(alphabet) for _ in range(TEMP_PASSWORD_LENGTH - 4)]
        rng.shuffle(chars)
        pw = "".join(chars)
        if not password_policy_violations(pw):
            return pw


# --------------------------------------------------------------------------- JWT

API_AUDIENCE = "api"
EXAM_AUDIENCE = "exam"
EXAM_TOKEN_MAX_HOURS = 12


class TokenUser(Protocol):
    id: Any
    org_id: Any
    role: str
    token_version: int


@dataclass(frozen=True)
class Claims:
    sub: str
    org_id: str | None
    role: str
    sid: str
    tv: int
    jti: str
    iat: int
    exp: int
    iss: str
    aud: str
    binding: str | None = None  # exam tokens only: browser binding hash (spec 8.1)


@lru_cache(maxsize=4)
def _load_keys(private_pem: str, public_pem: str) -> tuple[Any, Any, str]:
    private_key = load_pem_private_key(private_pem.encode(), password=None) if private_pem else None
    public_key = load_pem_public_key(public_pem.encode()) if public_pem else None
    sample = public_key or private_key
    if sample is None:
        raise RuntimeError("JWT_PRIVATE_KEY / JWT_PUBLIC_KEY are not configured")
    algorithm = "EdDSA" if isinstance(sample, Ed25519PublicKey | Ed25519PrivateKey) else "RS256"
    return private_key, public_key, algorithm


def _keys() -> tuple[Any, Any, str]:
    return _load_keys(
        settings.jwt_private_key.replace("\\n", "\n"), settings.jwt_public_key.replace("\\n", "\n")
    )


def _encode(payload: dict[str, Any]) -> str:
    private_key, _, algorithm = _keys()
    if private_key is None:
        raise RuntimeError("JWT_PRIVATE_KEY is not configured")
    return jwt.encode(payload, private_key, algorithm=algorithm, headers={"kid": settings.jwt_kid})


def _base_claims(audience: str, ttl_sec: int) -> dict[str, Any]:
    now = int(time.time())
    return {
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + ttl_sec,
        "iss": settings.app_slug,
        "aud": audience,
    }


def create_access_token(user: TokenUser, sid: Any) -> str:
    """HR/admin access token (audience ``api``), ``settings.access_ttl_min`` minutes."""
    payload = _base_claims(API_AUDIENCE, settings.access_ttl_min * 60)
    payload.update(
        sub=str(user.id),
        org_id=str(user.org_id) if user.org_id else None,
        role=user.role,
        sid=str(sid),
        tv=int(user.token_version),
    )
    return _encode(payload)


def create_exam_token(
    *,
    attempt_id: Any,
    invite_id: Any,
    org_id: Any,
    binding: str | None,
    ttl_hours: int = EXAM_TOKEN_MAX_HOURS,
) -> str:
    """Candidate exam session token (audience ``exam``): ``sub`` is the attempt id, ``sid`` the
    invite id, ``role`` is ``candidate``. Never valid on HR endpoints."""
    payload = _base_claims(EXAM_AUDIENCE, min(ttl_hours, EXAM_TOKEN_MAX_HOURS) * 3600)
    payload.update(
        sub=str(attempt_id), org_id=str(org_id), role=CANDIDATE, sid=str(invite_id), tv=0, bnd=binding
    )
    return _encode(payload)


def decode_token(token: str, audience: str = API_AUDIENCE) -> Claims:
    """Verify signature, ``kid``, issuer, audience and expiry. Raises ``AppError(UNAUTHENTICATED)``."""
    try:
        _, public_key, algorithm = _keys()
        if jwt.get_unverified_header(token).get("kid") != settings.jwt_kid:
            raise jwt.InvalidTokenError("unknown kid")
        data = jwt.decode(
            token,
            public_key,
            algorithms=[algorithm],
            audience=audience,
            issuer=settings.app_slug,
            options={"require": ["exp", "iat", "jti", "sub", "aud", "iss"]},
        )
        return Claims(
            sub=data["sub"],
            org_id=data.get("org_id"),
            role=data["role"],
            sid=data["sid"],
            tv=int(data["tv"]),
            jti=data["jti"],
            iat=int(data["iat"]),
            exp=int(data["exp"]),
            iss=data["iss"],
            aud=data["aud"],
            binding=data.get("bnd"),
        )
    except (jwt.PyJWTError, KeyError, ValueError, TypeError) as exc:
        raise AppError("UNAUTHENTICATED") from exc


# --------------------------------------------------------------------------- opaque tokens and HMAC


def new_opaque_token() -> str:
    """32 random bytes, URL-safe (invite links, refresh tokens). Store only ``hmac_hash`` of it."""
    return secrets.token_urlsafe(32)


def _pepper() -> bytes:
    if not settings.token_pepper:
        raise RuntimeError("TOKEN_PEPPER is not configured")
    return settings.token_pepper.encode()


def hmac_hash(value: str) -> str:
    """HMAC-SHA-512 hex digest keyed with ``TOKEN_PEPPER``."""
    return hmac.new(_pepper(), value.encode(), hashlib.sha512).hexdigest()


def normalize_email(email: str) -> str:
    return unicodedata.normalize("NFKC", email).strip().lower()


def blind_index(email: str) -> str:
    """Blind index for candidate email: HMAC of the NFKC-normalized, trimmed, lowercased address."""
    return hmac_hash(normalize_email(email))


# --------------------------------------------------------------------------- field encryption (AES-256-GCM)


@lru_cache(maxsize=4)
def _parse_keys(raw: str) -> tuple[str, dict[str, bytes]]:
    keys: dict[str, bytes] = {}
    active = ""
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        kid, _, b64 = item.partition(":")
        key = base64.b64decode(b64)
        if not kid or len(key) != 32 or ":" in kid:
            raise RuntimeError("FIELD_ENCRYPTION_KEYS entries must be '<kid>:<base64 32 bytes>'")
        keys[kid] = key
        active = active or kid
    if not keys:
        raise RuntimeError("FIELD_ENCRYPTION_KEYS is not configured")
    return active, keys


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def encrypt_field(plain: str) -> str:
    """Encrypt with the active key. Blob is ``kid:nonce:ciphertext`` (base64url, kid bound as AAD)."""
    active, keys = _parse_keys(settings.field_encryption_keys)
    nonce = os.urandom(12)
    ciphertext = AESGCM(keys[active]).encrypt(nonce, plain.encode(), active.encode())
    return f"{active}:{_b64e(nonce)}:{_b64e(ciphertext)}"


def decrypt_field(blob: str) -> str:
    """Decrypt a blob from ``encrypt_field`` with whichever configured key its kid names.
    Raises ``ValueError`` for an unknown kid, a malformed blob or a failed integrity check."""
    _, keys = _parse_keys(settings.field_encryption_keys)
    try:
        kid, nonce, ciphertext = blob.split(":")
        key = keys[kid]
        return AESGCM(key).decrypt(_b64d(nonce), _b64d(ciphertext), kid.encode()).decode()
    except Exception as exc:  # InvalidTag, KeyError, binascii.Error, ...
        raise ValueError("cannot decrypt field") from exc


def encrypt_bytes(data: bytes) -> bytes:
    """Binary variant for stored objects: ``len(kid) | kid | nonce(12) | ciphertext``."""
    active, keys = _parse_keys(settings.field_encryption_keys)
    kid = active.encode()
    nonce = os.urandom(12)
    return bytes([len(kid)]) + kid + nonce + AESGCM(keys[active]).encrypt(nonce, data, kid)


def decrypt_bytes(blob: bytes) -> bytes:
    _, keys = _parse_keys(settings.field_encryption_keys)
    try:
        kid_len = blob[0]
        kid = blob[1 : 1 + kid_len]
        nonce = blob[1 + kid_len : 13 + kid_len]
        return AESGCM(keys[kid.decode()]).decrypt(nonce, blob[13 + kid_len :], kid)
    except Exception as exc:
        raise ValueError("cannot decrypt object") from exc


def mask_secret(value: str | None) -> str | None:
    """Masked placeholder for write-only secrets returned by the API (spec 4.8)."""
    if not value:
        return None
    return "********"
