"""Test-only helpers. Never called by application code."""

import base64
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core.config import Settings


def generate_ed25519_pem_pair() -> tuple[str, str]:
    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem


def configure_test_secrets(cfg: Settings) -> None:
    """Fill empty JWT keys, field encryption keys and pepper with throwaway values (idempotent).

    Mutates the given settings object (normally the ``settings`` singleton) and mirrors the
    values into ``os.environ`` so subprocesses and fresh ``Settings()`` see the same keys.
    """
    if not (cfg.jwt_private_key and cfg.jwt_public_key):
        cfg.jwt_private_key, cfg.jwt_public_key = generate_ed25519_pem_pair()
    if not cfg.field_encryption_keys:
        cfg.field_encryption_keys = "test1:" + base64.b64encode(os.urandom(32)).decode()
    if not cfg.token_pepper:
        cfg.token_pepper = "test-pepper-not-a-secret"
    os.environ["JWT_PRIVATE_KEY"] = cfg.jwt_private_key
    os.environ["JWT_PUBLIC_KEY"] = cfg.jwt_public_key
    os.environ["FIELD_ENCRYPTION_KEYS"] = cfg.field_encryption_keys
    os.environ["TOKEN_PEPPER"] = cfg.token_pepper
