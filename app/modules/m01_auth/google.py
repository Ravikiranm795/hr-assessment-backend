"""Google ID-token verification behind an injectable verifier (spec 4.2, A-08).

Tests (and any other deployment) swap the verifier with ``set_google_verifier``. The default
verifier checks the RS256 signature against Google's published keys, the audience
(``GOOGLE_CLIENT_ID``), the issuer and the expiry.
"""

import asyncio
from dataclasses import dataclass
from typing import Protocol

import jwt

from app.core.config import settings
from app.core.errors import AppError

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")


@dataclass(frozen=True)
class GoogleIdentity:
    email: str
    email_verified: bool
    sub: str


class GoogleVerifier(Protocol):
    async def verify(self, id_token: str) -> GoogleIdentity:
        """Return the identity inside a valid Google ID token; raise ``AppError(UNAUTHENTICATED)``."""
        ...


class GoogleJwksVerifier:
    """Production verifier. The JWKS client caches Google's keys between calls."""

    def __init__(self) -> None:
        self._client: jwt.PyJWKClient | None = None

    def _decode(self, id_token: str) -> dict:
        if not settings.google_client_id:
            raise AppError("UNAUTHENTICATED")
        if self._client is None:
            self._client = jwt.PyJWKClient(GOOGLE_JWKS_URL, cache_keys=True, timeout=5)
        try:
            key = self._client.get_signing_key_from_jwt(id_token)
            return jwt.decode(
                id_token,
                key.key,
                algorithms=["RS256"],
                audience=settings.google_client_id,
                issuer=list(GOOGLE_ISSUERS),
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except (jwt.PyJWTError, OSError) as exc:
            raise AppError("UNAUTHENTICATED") from exc

    async def verify(self, id_token: str) -> GoogleIdentity:
        claims = await asyncio.to_thread(self._decode, id_token)
        return GoogleIdentity(
            email=str(claims.get("email", "")),
            email_verified=claims.get("email_verified") in (True, "true"),
            sub=str(claims["sub"]),
        )


_verifier: GoogleVerifier | None = None


def get_google_verifier() -> GoogleVerifier:
    global _verifier
    if _verifier is None:
        _verifier = GoogleJwksVerifier()
    return _verifier


def set_google_verifier(verifier: GoogleVerifier | None) -> None:
    """Swap the process-wide verifier (``None`` restores the default on next use)."""
    global _verifier
    _verifier = verifier
