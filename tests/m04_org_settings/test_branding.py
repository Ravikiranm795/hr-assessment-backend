"""Branding: contrast guardrail, curated fonts, uploads with magic-byte checks, reset (A-12, S-09)."""

import pytest
from sqlalchemy import text

from app.core.branding_text import FONT_KEYS, STARTER_THEME
from app.core.config import settings
from tests.m04_org_settings.conftest import audit_rows, ico_bytes, jpeg_bytes, multipart, png_bytes, row

API = "/api/v1/org/branding"


async def test_get_returns_starter_theme_until_first_save(http, tenant, db_session):
    r = await http.get(API, headers=tenant.headers)
    assert r.status_code == 200
    body = r.json()
    assert (
        body["primary_color"] == STARTER_THEME["primary"] and body["accent_color"] == STARTER_THEME["accent"]
    )
    assert (
        body["background_style"] == STARTER_THEME["background_style"]
        and body["font_key"] == STARTER_THEME["font_key"]
    )
    assert body["logo_url"] is None and body["favicon_url"] is None and body["is_customized"] is False
    assert set(body["status_colors"]) == {"success", "warning", "error"}
    assert (
        await row(db_session, "SELECT 1 AS x FROM branding_themes WHERE org_id = :o", o=tenant.org.id) is None
    )


async def test_put_saves_and_audits(http, tenant, db_session):
    r = await http.put(
        API,
        headers=tenant.headers,
        json={
            "primary_color": "#0b3d91",
            "accent_color": "#ffd166",
            "background_style": "plain_white",
            "font_key": "lato",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["primary_color"] == "#0B3D91" and body["accent_color"] == "#FFD166"
    assert body["font_key"] == "lato" and body["is_customized"] is True
    assert body["contrast"]["primary"]["passes"] is True and body["contrast"]["accent"]["passes"] is True
    saved = await row(db_session, "SELECT * FROM branding_themes WHERE org_id = :o", o=tenant.org.id)
    assert (
        saved["scope"] == "org"
        and saved["primary_color"] == "#0B3D91"
        and saved["updated_by"] == tenant.admin.id
    )
    audits = await audit_rows(db_session, tenant.org.id, "BRANDING_UPDATED")
    assert len(audits) == 1 and audits[0]["after"]["primary_color"] == "#0B3D91"
    assert audits[0]["before"]["primary_color"] == STARTER_THEME["primary"]

    again = (await http.get(API, headers=tenant.headers)).json()
    assert again["primary_color"] == "#0B3D91"


async def test_branding_low_contrast_BRANDING_CONTRAST_TOO_LOW(http, tenant, db_session):
    r = await http.put(API, headers=tenant.headers, json={"primary_color": "#7A7A7A"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "BRANDING_CONTRAST_TOO_LOW"
    assert err["details"]["field"] == "primary_color" and err["details"]["minimum"] == 4.5
    assert err["details"]["ratio"] < 4.5
    r = await http.put(API, headers=tenant.headers, json={"accent_color": "#808080"})
    assert r.json()["error"]["code"] == "BRANDING_CONTRAST_TOO_LOW"
    assert (
        await row(db_session, "SELECT 1 AS x FROM branding_themes WHERE org_id = :o", o=tenant.org.id) is None
    )
    assert await audit_rows(db_session, tenant.org.id, "BRANDING_UPDATED") == []


async def test_validate_checks_without_saving(http, tenant, db_session):
    bad = await http.post(f"{API}/validate", headers=tenant.headers, json={"primary_color": "#7A7A7A"})
    assert bad.status_code == 200
    body = bad.json()
    assert body["valid"] is False
    assert body["checks"]["primary"]["passes"] is False and body["checks"]["accent"]["passes"] is True
    good = await http.post(
        f"{API}/validate",
        headers=tenant.headers,
        json={"primary_color": "#000000", "accent_color": "#ffffff"},
    )
    assert good.json()["valid"] is True and good.json()["checks"]["primary"]["text_color"] == "#FFFFFF"
    assert (
        await row(db_session, "SELECT 1 AS x FROM branding_themes WHERE org_id = :o", o=tenant.org.id) is None
    )


@pytest.mark.parametrize(
    "body",
    [
        {"primary_color": "red"},
        {"primary_color": "#12"},
        {"accent_color": "url(javascript:x)"},
        {"font_key": "comic_sans"},
        {"font_key": "Inter, sans-serif; background:url(x)"},
        {"background_style": "rainbow"},
        {"success_color": "#00ff00"},  # fixed status colours cannot be overridden
        {"watermark": False},  # watermark is independent of the theme (SA flag)
    ],
)
async def test_put_rejects_invalid_theme_input(http, tenant, body):
    r = await http.put(API, headers=tenant.headers, json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_fonts_list_is_the_curated_set(http, tenant):
    r = await http.get(f"{API}/fonts", headers=tenant.headers)
    assert r.status_code == 200
    fonts = r.json()["fonts"]
    assert [f["key"] for f in fonts] == list(FONT_KEYS)
    assert all(f["label"] and f["category"] in {"sans", "serif"} for f in fonts)
    assert r.json()["background_styles"]


async def test_logo_upload_checks_magic_bytes_size(http, tenant, db_session, monkeypatch):
    ok = await http.post(
        f"{API}/logo", headers=tenant.headers, **multipart("file", "logo.png", png_bytes(80, 40), "image/png")
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["logo_url"].startswith("http://api.test/api/v1/files/")
    asset = await row(db_session, "SELECT * FROM assets WHERE org_id = :o AND kind = 'logo'", o=tenant.org.id)
    assert asset["content_type"] == "image/png" and asset["storage_key"].startswith(f"{tenant.org.id}/logo/")
    assert (
        asset["encrypted"] is False and len(asset["sha256"]) == 64 and asset["created_by"] == tenant.admin.id
    )
    theme = await row(
        db_session, "SELECT logo_asset_id FROM branding_themes WHERE org_id = :o", o=tenant.org.id
    )
    assert theme["logo_asset_id"] == asset["id"]
    assert len(await audit_rows(db_session, tenant.org.id, "BRANDING_UPDATED")) == 1

    # the signed URL really serves the stored bytes
    path = body["logo_url"].removeprefix("http://api.test")
    fetched = await http.get(path)
    assert fetched.status_code == 200 and fetched.content == png_bytes(80, 40)

    # magic bytes beat the filename and declared type
    for name, data, ctype in [
        ("evil.png", b"<?php echo 1; ?>", "image/png"),
        ("evil.png", b"<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>", "image/png"),
        ("logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml"),
        ("empty.png", b"", "image/png"),
        ("fake.png", b"\x89PNG\r\n\x1a\n", "image/png"),
        ("anim.gif", b"GIF89a" + b"\x00" * 30, "image/gif"),
        ("real.png", png_bytes(80, 40), "text/html"),  # declared type contradicts content
        ("real.png", ico_bytes(), "image/x-icon"),  # ico is favicon-only
    ]:
        r = await http.post(f"{API}/logo", headers=tenant.headers, **multipart("file", name, data, ctype))
        assert r.status_code == 422, (name, ctype)
        assert r.json()["error"]["code"] == "UPLOAD_INVALID"

    # size limit comes from settings
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    big = png_bytes(64, 64) + b"\x00" * (1024 * 1024)
    r = await http.post(
        f"{API}/logo", headers=tenant.headers, **multipart("file", "big.png", big, "image/png")
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "UPLOAD_INVALID"
    assert r.json()["error"]["details"]["reason"] == "too_large"

    missing = await http.post(f"{API}/logo", headers=tenant.headers)
    assert missing.status_code == 422

    # failed uploads left exactly one asset behind
    count = await row(db_session, "SELECT count(*) AS n FROM assets WHERE org_id = :o", o=tenant.org.id)
    assert count["n"] == 1


async def test_replacing_logo_removes_old_asset(http, tenant, db_session):
    await http.post(
        f"{API}/logo", headers=tenant.headers, **multipart("file", "a.png", png_bytes(64, 64), "image/png")
    )
    first = await row(db_session, "SELECT id, storage_key FROM assets WHERE org_id = :o", o=tenant.org.id)
    await http.post(
        f"{API}/logo", headers=tenant.headers, **multipart("file", "b.jpg", jpeg_bytes(50, 50), "image/jpeg")
    )
    await db_session.commit()
    rows = (
        await db_session.execute(
            text("SELECT id, content_type FROM assets WHERE org_id = :o"), {"o": tenant.org.id}
        )
    ).all()
    assert len(rows) == 1 and rows[0].id != first["id"] and rows[0].content_type == "image/jpeg"


async def test_favicon_accepts_ico_and_png(http, tenant, db_session):
    for name, data, ctype in [
        ("f.ico", ico_bytes(), "image/x-icon"),
        ("f.png", png_bytes(32, 32), "image/png"),
    ]:
        r = await http.post(f"{API}/favicon", headers=tenant.headers, **multipart("file", name, data, ctype))
        assert r.status_code == 200, (name, r.text)
        assert r.json()["favicon_url"]
    kinds = await db_session.execute(text("SELECT kind FROM assets WHERE org_id = :o"), {"o": tenant.org.id})
    await db_session.commit()
    assert [k for (k,) in kinds] == ["favicon"]


async def test_put_can_clear_logo_and_rejects_foreign_asset(http, tenant, other_tenant, db_session):
    await http.post(
        f"{API}/logo", headers=tenant.headers, **multipart("file", "a.png", png_bytes(64, 64), "image/png")
    )
    await http.post(
        f"{API}/logo",
        headers=other_tenant.headers,
        **multipart("file", "a.png", png_bytes(64, 64), "image/png"),
    )
    foreign = await row(db_session, "SELECT id FROM assets WHERE org_id = :o", o=other_tenant.org.id)
    r = await http.put(API, headers=tenant.headers, json={"logo_asset_id": str(foreign["id"])})
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    cleared = await http.put(API, headers=tenant.headers, json={"logo_asset_id": None})
    assert cleared.status_code == 200 and cleared.json()["logo_url"] is None


async def test_branding_reset_copies_starter_theme(http, tenant, db_session):
    await http.put(
        API,
        headers=tenant.headers,
        json={
            "primary_color": "#0b3d91",
            "accent_color": "#ffd166",
            "background_style": "navy_band",
            "font_key": "merriweather",
        },
    )
    await http.post(
        f"{API}/logo", headers=tenant.headers, **multipart("file", "a.png", png_bytes(64, 64), "image/png")
    )
    r = await http.post(f"{API}/reset", headers=tenant.headers)
    assert r.status_code == 200
    body = r.json()
    assert (
        body["primary_color"] == STARTER_THEME["primary"] and body["accent_color"] == STARTER_THEME["accent"]
    )
    assert (
        body["background_style"] == STARTER_THEME["background_style"]
        and body["font_key"] == STARTER_THEME["font_key"]
    )
    assert body["is_customized"] is False
    assert body["logo_url"] is not None  # the organization's own logo is kept
    saved = await row(db_session, "SELECT * FROM branding_themes WHERE org_id = :o", o=tenant.org.id)
    assert (
        saved["primary_color"] == STARTER_THEME["primary"] and saved["font_key"] == STARTER_THEME["font_key"]
    )
    assert len(await audit_rows(db_session, tenant.org.id, "BRANDING_UPDATED")) == 3


async def test_theme_is_isolated_per_organization(http, tenant, other_tenant):
    await http.put(API, headers=tenant.headers, json={"primary_color": "#0b3d91"})
    other = (await http.get(API, headers=other_tenant.headers)).json()
    assert other["primary_color"] == STARTER_THEME["primary"]


async def test_starter_theme_passes_its_own_contrast_rules(http, tenant):
    body = (await http.post(f"{API}/validate", headers=tenant.headers, json={})).json()
    assert body["valid"] is True


async def test_copy_starter_theme_service_for_new_orgs(db_session, tenant):
    from app.modules.m04_org_settings.service import BrandingService

    other_org_row = await row(
        db_session, "SELECT count(*) AS n FROM branding_themes WHERE org_id = :o", o=tenant.org.id
    )
    assert other_org_row["n"] == 0
    await BrandingService(db_session).copy_starter_theme(tenant.org.id)
    await db_session.commit()
    saved = await row(db_session, "SELECT * FROM branding_themes WHERE org_id = :o", o=tenant.org.id)
    assert saved["primary_color"] == STARTER_THEME["primary"] and saved["scope"] == "org"
    await BrandingService(db_session).copy_starter_theme(tenant.org.id)  # idempotent
    await db_session.commit()
