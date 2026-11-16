import base64
import os
import string
import time
import uuid
from types import SimpleNamespace

import pytest

from app.core import security
from app.core.config import settings
from app.core.errors import AppError


def test_argon2_hash_verify_and_timing_budget():
    hashed = security.hash_password("correct horse battery staple")
    assert hashed.startswith("$argon2id$")
    started = time.perf_counter()
    assert security.verify_password(hashed, "correct horse battery staple") is True
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert 50 <= elapsed_ms <= 1500, f"argon2 verify took {elapsed_ms:.0f} ms"
    assert security.verify_password(hashed, "wrong password!!") is False
    assert security.verify_password("not-a-hash", "whatever") is False


def test_temp_password_meets_policy():
    seen = set()
    for _ in range(50):
        pw = security.generate_temp_password()
        seen.add(pw)
        assert len(pw) >= security.PASSWORD_MIN_LENGTH
        assert any(c in string.ascii_lowercase for c in pw)
        assert any(c in string.ascii_uppercase for c in pw)
        assert any(c in string.digits for c in pw)
        assert any(c in security.TEMP_PASSWORD_SYMBOLS for c in pw)
        assert security.password_policy_violations(pw) == []
    assert len(seen) == 50


def test_password_policy_rejects_short_and_common():
    assert "too_short" in security.password_policy_violations("Ab1!short")
    assert "common_password" in security.password_policy_violations("password1234")


def test_blind_index_equal_for_case_and_whitespace_variants():
    base = security.blind_index("bob@x.com")
    assert security.blind_index("Bob@X.com ") == base
    assert security.blind_index("  BOB@x.COM\t") == base
    # Fullwidth characters fold to ASCII under NFKC.
    assert security.blind_index("ｂｏｂ@ｘ.ｃｏｍ") == base
    assert security.blind_index("alice@x.com") != base
    assert len(base) == 128  # HMAC-SHA-512 hex


def test_hmac_hash_is_deterministic_and_peppered(monkeypatch):
    first = security.hmac_hash("token-value")
    assert first == security.hmac_hash("token-value")
    monkeypatch.setattr(settings, "token_pepper", "another-pepper")
    assert security.hmac_hash("token-value") != first


def test_new_opaque_token_is_urlsafe_and_long():
    token = security.new_opaque_token()
    assert len(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))) == 32
    assert token != security.new_opaque_token()


def _key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def test_encrypt_roundtrip_and_key_rotation(monkeypatch):
    monkeypatch.setattr(settings, "field_encryption_keys", f"k1:{_key()}")
    blob = security.encrypt_field("candidate@example.com")
    kid, nonce, ciphertext = blob.split(":")
    assert kid == "k1" and nonce and ciphertext
    assert "candidate" not in blob
    assert security.decrypt_field(blob) == "candidate@example.com"
    assert security.encrypt_field("same") != security.encrypt_field("same")  # random nonce

    old_keys = settings.field_encryption_keys
    monkeypatch.setattr(settings, "field_encryption_keys", f"k2:{_key()},{old_keys}")
    assert security.decrypt_field(blob) == "candidate@example.com"  # old key still decrypts
    new_blob = security.encrypt_field("candidate@example.com")
    assert new_blob.startswith("k2:")

    monkeypatch.setattr(settings, "field_encryption_keys", f"k3:{_key()}")
    with pytest.raises(ValueError):
        security.decrypt_field(blob)  # retired key


def test_encrypt_rejects_tampered_ciphertext():
    blob = security.encrypt_field("secret")
    kid, nonce, ct = blob.split(":")
    tampered = f"{kid}:{nonce}:{ct[:-2]}{'A' if ct[-2] != 'A' else 'B'}{ct[-1]}"
    with pytest.raises(ValueError):
        security.decrypt_field(tampered)


def test_encrypt_bytes_roundtrip():
    data = os.urandom(1000)
    sealed = security.encrypt_bytes(data)
    assert sealed != data
    assert security.decrypt_bytes(sealed) == data


def _user(role="hr_user"):
    return SimpleNamespace(id=uuid.uuid4(), org_id=uuid.uuid4(), role=role, token_version=3)


def test_access_token_roundtrip_has_required_claims():
    user = _user()
    sid = uuid.uuid4()
    token = security.create_access_token(user, sid)
    claims = security.decode_token(token)
    assert claims.sub == str(user.id)
    assert claims.org_id == str(user.org_id)
    assert claims.role == "hr_user"
    assert claims.sid == str(sid)
    assert claims.tv == 3
    assert claims.iss == settings.app_slug
    assert claims.exp - claims.iat == settings.access_ttl_min * 60
    assert claims.jti


def test_token_audiences_do_not_mix():
    api_token = security.create_access_token(_user(), uuid.uuid4())
    exam_token = security.create_exam_token(
        attempt_id=uuid.uuid4(), invite_id=uuid.uuid4(), org_id=uuid.uuid4(), binding="b"
    )
    with pytest.raises(AppError) as exc:
        security.decode_token(api_token, audience="exam")
    assert exc.value.code == "UNAUTHENTICATED"
    with pytest.raises(AppError):
        security.decode_token(exam_token)
    exam_claims = security.decode_token(exam_token, audience="exam")
    assert exam_claims.role == "candidate" and exam_claims.binding == "b"
    assert exam_claims.exp - exam_claims.iat <= 12 * 3600


def test_expired_and_garbage_tokens_rejected(monkeypatch):
    monkeypatch.setattr(settings, "access_ttl_min", -1)
    expired = security.create_access_token(_user(), uuid.uuid4())
    for bad in (expired, "not.a.jwt", ""):
        with pytest.raises(AppError) as exc:
            security.decode_token(bad)
        assert exc.value.code == "UNAUTHENTICATED"
