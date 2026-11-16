"""Policy documents: sanitized HTML, PDF upload, immutable published versions (D-11)."""

import pytest
from sqlalchemy import text

from app.core.config import settings
from tests.m04_org_settings.conftest import audit_rows, multipart, pdf_bytes, row

API = "/api/v1/org/documents"
DIRTY = (
    '<h2>Privacy</h2><p onclick="steal()">We keep <strong>data</strong>.</p>'
    '<script>alert(1)</script><a href="javascript:alert(1)">bad</a>'
    '<img src=x onerror=alert(1)><a href="https://example.com">good</a>'
)


async def _create(http, headers, **over):
    body = {"doc_type": "privacy", "title": "Privacy policy"} | over
    r = await http.post(API, headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def test_create_list_get_patch_document(http, tenant, other_tenant, db_session):
    doc = await _create(http, tenant.headers)
    assert doc["doc_type"] == "privacy" and doc["title"] == "Privacy policy"
    assert doc["version_count"] == 0 and doc["current_version_id"] is None
    await _create(http, tenant.headers, doc_type="terms", title="Terms")
    await _create(http, other_tenant.headers, title="Theirs")
    assert len(await audit_rows(db_session, tenant.org.id, "DOCUMENT_CREATED")) == 2

    listing = (await http.get(API, headers=tenant.headers)).json()
    assert listing["total"] == 2 and {d["title"] for d in listing["items"]} == {"Privacy policy", "Terms"}
    filtered = (await http.get(API, headers=tenant.headers, params={"doc_type": "terms"})).json()
    assert filtered["total"] == 1 and filtered["items"][0]["title"] == "Terms"
    assert (await http.get(API, headers=tenant.headers, params={"page_size": 1})).json()[
        "items"
    ].__len__() == 1
    assert (await http.get(API, headers=tenant.headers, params={"page_size": 1000})).status_code == 422
    assert (await http.get(API, headers=tenant.headers, params={"doc_type": "bogus"})).status_code == 422

    got = await http.get(f"{API}/{doc['id']}", headers=tenant.headers)
    assert got.status_code == 200 and got.json()["id"] == doc["id"]
    patched = await http.patch(f"{API}/{doc['id']}", headers=tenant.headers, json={"title": "Privacy notice"})
    assert patched.status_code == 200 and patched.json()["title"] == "Privacy notice"


@pytest.mark.parametrize(
    "body",
    [
        {"doc_type": "cookie", "title": "x"},
        {"doc_type": "privacy", "title": ""},
        {"title": "x"},
        {"doc_type": "privacy", "title": "x" * 301},
    ],
)
async def test_create_document_validation(http, tenant, body):
    r = await http.post(API, headers=tenant.headers, json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_documents_are_tenant_isolated_404(http, tenant, other_tenant):
    theirs = await _create(http, other_tenant.headers)
    vid = (
        await http.post(
            f"{API}/{theirs['id']}/versions", headers=other_tenant.headers, json={"content_html": "<p>x</p>"}
        )
    ).json()["id"]
    for method, url, kwargs in [
        ("GET", f"{API}/{theirs['id']}", {}),
        ("PATCH", f"{API}/{theirs['id']}", {"json": {"title": "hijack"}}),
        ("GET", f"{API}/{theirs['id']}/versions", {}),
        ("POST", f"{API}/{theirs['id']}/versions", {"json": {"content_html": "<p>x</p>"}}),
        ("POST", f"{API}/{theirs['id']}/versions/{vid}/publish", {}),
    ]:
        r = await http.request(method, url, headers=tenant.headers, **kwargs)
        assert r.status_code == 404, (method, url)
        assert r.json()["error"]["code"] == "NOT_FOUND"
    # a version id from another doc of the same org is also 404
    mine = await _create(http, tenant.headers)
    r = await http.post(f"{API}/{mine['id']}/versions/{vid}/publish", headers=tenant.headers)
    assert r.status_code == 404


async def test_document_html_sanitized_scripts_removed(http, tenant, db_session):
    doc = await _create(http, tenant.headers)
    r = await http.post(f"{API}/{doc['id']}/versions", headers=tenant.headers, json={"content_html": DIRTY})
    assert r.status_code == 201, r.text
    version = r.json()
    for needle in ("<script", "onclick", "onerror", "javascript:", "<img", "alert"):
        assert needle not in version["content_html"].lower()
    assert (
        "<strong>data</strong>" in version["content_html"]
        and 'href="https://example.com"' in version["content_html"]
    )
    stored = await row(
        db_session, "SELECT content_html FROM document_versions WHERE id = :i", i=version["id"]
    )
    assert stored["content_html"] == version["content_html"]
    audit = await audit_rows(db_session, tenant.org.id, "DOCUMENT_VERSION_CREATED")
    assert len(audit) == 1 and "alert" not in str(audit[0]["after"])


async def test_version_requires_exactly_one_content_source(http, tenant):
    doc = await _create(http, tenant.headers)
    for body in ({}, {"content_html": ""}, {"content_html": "x" * 600_001}):
        r = await http.post(f"{API}/{doc['id']}/versions", headers=tenant.headers, json=body)
        assert r.status_code == 422, body[:1]
    r = await http.post(
        f"{API}/{doc['id']}/versions", headers=tenant.headers, json={"content_html": "<script>1</script>"}
    )
    assert r.status_code == 422  # nothing left after sanitizing
    r = await http.post(
        f"{API}/{doc['id']}/versions",
        headers=tenant.headers,
        content=b"x",
    )
    assert r.status_code == 422


async def test_document_publish_creates_immutable_version(http, tenant, db_session):
    doc = await _create(http, tenant.headers, doc_type="consent", title="Consent")
    v1 = (
        await http.post(
            f"{API}/{doc['id']}/versions", headers=tenant.headers, json={"content_html": "<p>v1 text</p>"}
        )
    ).json()
    assert v1["version_no"] == 1 and v1["status"] == "draft" and v1["published_at"] is None

    pub = await http.post(f"{API}/{doc['id']}/versions/{v1['id']}/publish", headers=tenant.headers)
    assert pub.status_code == 200, pub.text
    assert pub.json()["status"] == "published" and pub.json()["published_at"]
    saved = await row(
        db_session,
        "SELECT status, published_by, content_html FROM document_versions WHERE id = :i",
        i=v1["id"],
    )
    assert saved["status"] == "published" and saved["published_by"] == tenant.admin.id
    assert len(await audit_rows(db_session, tenant.org.id, "DOCUMENT_PUBLISHED")) == 1

    # published versions never change: re-publishing is a conflict, edits create a new version
    again = await http.post(f"{API}/{doc['id']}/versions/{v1['id']}/publish", headers=tenant.headers)
    assert again.status_code == 409 and again.json()["error"]["code"] == "CONFLICT"
    v2 = (
        await http.post(
            f"{API}/{doc['id']}/versions", headers=tenant.headers, json={"content_html": "<p>v2 text</p>"}
        )
    ).json()
    assert v2["version_no"] == 2 and v2["status"] == "draft"
    unchanged = await row(
        db_session, "SELECT content_html, status FROM document_versions WHERE id = :i", i=v1["id"]
    )
    assert unchanged == {"content_html": saved["content_html"], "status": "published"}

    doc_now = (await http.get(f"{API}/{doc['id']}", headers=tenant.headers)).json()
    assert doc_now["current_version_id"] == v1["id"] and doc_now["version_count"] == 2
    await http.post(f"{API}/{doc['id']}/versions/{v2['id']}/publish", headers=tenant.headers)
    doc_now = (await http.get(f"{API}/{doc['id']}", headers=tenant.headers)).json()
    assert doc_now["current_version_id"] == v2["id"]

    history = (await http.get(f"{API}/{doc['id']}/versions", headers=tenant.headers)).json()
    assert [v["version_no"] for v in history["items"]] == [2, 1]
    assert [v["status"] for v in history["items"]] == ["published", "published"]

    # changing the type of a document that was published would orphan consent references
    r = await http.patch(f"{API}/{doc['id']}", headers=tenant.headers, json={"doc_type": "privacy"})
    assert r.status_code == 409


async def test_concurrent_version_numbers_are_unique(http, tenant, db_session):
    import asyncio

    doc = await _create(http, tenant.headers)
    results = await asyncio.gather(
        *[
            http.post(
                f"{API}/{doc['id']}/versions", headers=tenant.headers, json={"content_html": f"<p>{i}</p>"}
            )
            for i in range(5)
        ]
    )
    assert [r.status_code for r in results] == [201] * 5
    assert sorted(r.json()["version_no"] for r in results) == [1, 2, 3, 4, 5]


async def test_publish_version_service_interface(http, tenant, db_session):
    from app.core.db import tenant_session
    from app.modules.m04_org_settings.service import DocumentService

    doc = await _create(http, tenant.headers)
    v = (
        await http.post(
            f"{API}/{doc['id']}/versions", headers=tenant.headers, json={"content_html": "<p>x</p>"}
        )
    ).json()
    from app.core.db import TenantContext
    from tests.m04_org_settings.conftest import Tenant  # noqa: F401  (type only)

    ctx = TenantContext(tenant.org.id, tenant.admin.id, "org_admin", False, "r1", None, None, actor_name="A")
    async with tenant_session(ctx) as session:
        service = DocumentService(session)
        published = await service.publish_version(ctx, doc["id"], v["id"])
        assert published.status == "published"
        latest = await service.latest_published(tenant.org.id, "privacy")
        assert str(latest.id) == v["id"]
        assert await service.latest_published(tenant.org.id, "terms") is None


async def test_pdf_upload_version(http, tenant, db_session):
    doc = await _create(http, tenant.headers, doc_type="other", title="Handbook")
    r = await http.post(
        f"{API}/{doc['id']}/versions",
        headers=tenant.headers,
        **multipart("file", "handbook.pdf", pdf_bytes(), "application/pdf"),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["content_html"] is None and body["pdf_url"].startswith("http://api.test/api/v1/files/")
    asset = await row(
        db_session, "SELECT * FROM assets WHERE org_id = :o AND kind = 'doc_pdf'", o=tenant.org.id
    )
    assert asset["content_type"] == "application/pdf" and asset["storage_key"].endswith(".pdf")
    fetched = await http.get(body["pdf_url"].removeprefix("http://api.test"))
    assert fetched.status_code == 200 and fetched.content == pdf_bytes()
    linked = await row(db_session, "SELECT pdf_asset_id FROM document_versions WHERE id = :i", i=body["id"])
    assert linked["pdf_asset_id"] == asset["id"]


async def test_pdf_upload_rejects_non_pdf_oversize_and_active_content(http, tenant, db_session, monkeypatch):
    doc = await _create(http, tenant.headers)
    url = f"{API}/{doc['id']}/versions"
    cases = [
        ("a.pdf", b"<html>not a pdf</html>", "application/pdf"),
        ("a.pdf", pdf_bytes(b"/JavaScript (app.alert(1))"), "application/pdf"),
        ("a.pdf", pdf_bytes(), "text/html"),
        ("a.pdf", b"", "application/pdf"),
    ]
    for name, data, ctype in cases:
        r = await http.post(url, headers=tenant.headers, **multipart("file", name, data, ctype))
        assert r.status_code == 422 and r.json()["error"]["code"] == "UPLOAD_INVALID", (name, ctype)
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    r = await http.post(
        url,
        headers=tenant.headers,
        **multipart("file", "a.pdf", pdf_bytes(b"x" * (1024 * 1024)), "application/pdf"),
    )
    assert r.status_code == 422 and r.json()["error"]["details"]["reason"] == "too_large"
    n = await row(db_session, "SELECT count(*) AS n FROM assets WHERE org_id = :o", o=tenant.org.id)
    m = await row(
        db_session,
        "SELECT count(*) AS n FROM document_versions "
        "WHERE document_id IN (SELECT id FROM documents WHERE org_id = :o)",
        o=tenant.org.id,
    )
    assert n["n"] == 0 and m["n"] == 0


async def test_document_audit_rows_belong_to_org(http, tenant, db_session):
    doc = await _create(http, tenant.headers)
    rows = (
        await db_session.execute(
            text(
                "SELECT entity_type, entity_id FROM audit_logs WHERE action = 'DOCUMENT_CREATED' AND org_id = :o"
            ),
            {"o": tenant.org.id},
        )
    ).all()
    assert rows[0].entity_type == "document" and rows[0].entity_id == doc["id"]
