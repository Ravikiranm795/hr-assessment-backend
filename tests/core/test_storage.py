import time
from urllib.parse import urlparse

import pytest

from app.core.errors import AppError
from app.core.storage.local import LocalStorage


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(root=str(tmp_path), base_url="http://api.test")


async def test_put_get_delete_roundtrip(storage, tmp_path):
    stored = await storage.put("org1/logos/a.png", b"\x89PNGdata", "image/png")
    assert stored.size_bytes == 8 and stored.encrypted is False and len(stored.sha256) == 64
    assert await storage.get("org1/logos/a.png") == b"\x89PNGdata"
    await storage.delete("org1/logos/a.png")
    with pytest.raises(AppError) as exc:
        await storage.get("org1/logos/a.png")
    assert exc.value.code == "NOT_FOUND"


async def test_encrypted_put_is_not_plaintext_on_disk(storage, tmp_path):
    await storage.put("org1/snap/1.jpg", b"face-bytes", "image/jpeg", encrypt=True)
    on_disk = (tmp_path / "org1" / "snap" / "1.jpg").read_bytes()
    assert b"face-bytes" not in on_disk
    assert await storage.get("org1/snap/1.jpg") == b"face-bytes"


@pytest.mark.parametrize("key", ["../etc/passwd", "/abs/path", "a/../../b", "", "a//b", "a\\b"])
async def test_unsafe_keys_rejected(storage, key):
    with pytest.raises(AppError) as exc:
        await storage.put(key, b"x", "text/plain")
    assert exc.value.code == "UPLOAD_INVALID"


async def test_local_storage_signed_url_expires(storage):
    await storage.put("org1/snap/2.jpg", b"img", "image/jpeg")
    url = await storage.signed_url("org1/snap/2.jpg", ttl_sec=60)
    parsed = urlparse(url)
    assert parsed.netloc == "api.test" and parsed.path.startswith("/api/v1/files/")
    token = parsed.path.rsplit("/", 1)[1]
    now = time.time()
    assert storage.verify_signed_token(token, now=now) == "org1/snap/2.jpg"
    assert storage.verify_signed_token(token, now=now + 59) == "org1/snap/2.jpg"
    with pytest.raises(AppError) as exc:
        storage.verify_signed_token(token, now=now + 61)
    assert exc.value.code == "NOT_FOUND"


async def test_signed_token_tamper_rejected(storage):
    await storage.put("org1/a.jpg", b"img", "image/jpeg")
    url = await storage.signed_url("org1/a.jpg")
    token = url.rsplit("/", 1)[1]
    payload, sig = token.split(".")
    forged = payload[:-1] + ("A" if payload[-1] != "A" else "B") + "." + sig
    for bad in (forged, token + "x", "garbage"):
        with pytest.raises(AppError):
            storage.verify_signed_token(bad)


async def test_files_endpoint_serves_signed_object(tmp_path):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.core.errors import install_error_handlers
    from app.core.storage import router as storage_router
    from app.core.storage import set_storage

    local = LocalStorage(root=str(tmp_path), base_url="http://t")
    set_storage(local)
    try:
        await local.put("org1/x.jpg", b"jpeg!", "image/jpeg", encrypt=True)
        app = FastAPI()
        install_error_handlers(app)
        app.include_router(storage_router.router)
        url = await local.signed_url("org1/x.jpg")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
            ok = await http.get(urlparse(url).path)
            bad = await http.get("/api/v1/files/garbage")
        assert ok.status_code == 200 and ok.content == b"jpeg!"
        assert ok.headers["content-type"] == "image/jpeg"
        assert ok.headers["cache-control"] == "no-store"
        assert bad.status_code == 404
    finally:
        set_storage(None)
