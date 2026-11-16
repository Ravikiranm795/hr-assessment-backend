"""Table-driven permission test against spec 4.3: org settings, branding, documents, email and AI
setup belong to the organization admin only; the five flags gate the route groups."""

import uuid

import pytest

from tests.m04_org_settings.conftest import multipart, png_bytes

API = "/api/v1/org"
RANDOM = uuid.uuid4()

# (method, path, json body, flag)
ROUTES = [
    ("GET", f"{API}/profile", None, "settings"),
    ("PATCH", f"{API}/profile", {"name": "N"}, "settings"),
    ("GET", f"{API}/settings/security", None, "settings"),
    ("PATCH", f"{API}/settings/security", {"session_timeout_minutes": 30}, "settings"),
    ("GET", f"{API}/settings/retention", None, "settings"),
    ("PATCH", f"{API}/settings/retention", {"retention_days": 90}, "settings"),
    ("GET", f"{API}/settings/invites", None, "settings"),
    ("PATCH", f"{API}/settings/invites", {"invite_default_expiry_hours": 72}, "settings"),
    ("GET", f"{API}/settings/email", None, "email_sending"),
    (
        "PUT",
        f"{API}/settings/email",
        {"host": "h.example.com", "port": 25, "security": "none", "sender_email": "a@b.co"},
        "email_sending",
    ),
    ("POST", f"{API}/settings/email/test", {"to": "a@b.co"}, "email_sending"),
    ("GET", f"{API}/settings/ai/providers", None, "ai_provider_setup"),
    ("GET", f"{API}/settings/ai", None, "ai_provider_setup"),
    (
        "PUT",
        f"{API}/settings/ai",
        {"provider": "cli", "mode": "cli", "cli_command_key": "claude"},
        "ai_provider_setup",
    ),
    ("POST", f"{API}/settings/ai/test", None, "ai_provider_setup"),
    ("GET", f"{API}/ai-usage", None, "ai_provider_setup"),
    ("GET", f"{API}/branding", None, "org_branding"),
    ("PUT", f"{API}/branding", {"font_key": "lato"}, "org_branding"),
    ("POST", f"{API}/branding/validate", {}, "org_branding"),
    ("POST", f"{API}/branding/reset", None, "org_branding"),
    ("GET", f"{API}/branding/fonts", None, "org_branding"),
    ("GET", f"{API}/documents", None, "policy_documents"),
    ("POST", f"{API}/documents", {"doc_type": "terms", "title": "T"}, "policy_documents"),
    ("GET", f"{API}/documents/{RANDOM}", None, "policy_documents"),
    ("PATCH", f"{API}/documents/{RANDOM}", {"title": "T"}, "policy_documents"),
    ("GET", f"{API}/documents/{RANDOM}/versions", None, "policy_documents"),
    ("POST", f"{API}/documents/{RANDOM}/versions", {"content_html": "<p>x</p>"}, "policy_documents"),
    ("POST", f"{API}/documents/{RANDOM}/versions/{RANDOM}/publish", None, "policy_documents"),
]
IDS = [f"{m} {p.removeprefix(API)}".replace(str(RANDOM), "{id}") for m, p, _, _ in ROUTES]


@pytest.mark.parametrize(("method", "path", "body", "flag"), ROUTES, ids=IDS)
async def test_only_org_admin_allowed(http, role_headers, method, path, body, flag):
    anonymous = await http.request(method, path, json=body)
    assert anonymous.status_code == 401 and anonymous.json()["error"]["code"] == "UNAUTHENTICATED"
    for role in ("hr_user", "super_admin"):
        r = await http.request(method, path, json=body, headers=await role_headers(role))
        assert r.status_code == 403, (role, r.text)
        assert r.json()["error"]["code"] == "FORBIDDEN"
    r = await http.request(method, path, json=body, headers=await role_headers("org_admin"))
    assert r.status_code not in (401, 403), r.text


@pytest.mark.parametrize(("method", "path", "body", "flag"), ROUTES, ids=IDS)
async def test_flag_off_blocks_the_route(http, tenant, enable_flag, method, path, body, flag):
    await enable_flag(tenant.org.id, flag, False)
    r = await http.request(method, path, json=body, headers=tenant.headers)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FEATURE_DISABLED" and r.json()["error"]["details"]["flag"] == flag
    await enable_flag(tenant.org.id, flag, True)


async def test_upload_routes_are_guarded_too(http, role_headers, tenant, enable_flag):
    for path in (f"{API}/branding/logo", f"{API}/branding/favicon"):
        files = multipart("file", "a.png", png_bytes(64, 64), "image/png")
        assert (await http.post(path, **files)).status_code == 401
        assert (await http.post(path, headers=await role_headers("hr_user"), **files)).status_code == 403
        assert (await http.post(path, headers=await role_headers("super_admin"), **files)).status_code == 403
        await enable_flag(tenant.org.id, "org_branding", False)
        blocked = await http.post(path, headers=tenant.headers, **files)
        assert blocked.json()["error"]["code"] == "FEATURE_DISABLED"
        await enable_flag(tenant.org.id, "org_branding", True)
        assert (await http.post(path, headers=tenant.headers, **files)).status_code == 200


async def test_openapi_declares_roles_and_flags_for_every_m4_route(http):
    schema = (await http.get("/openapi.json")).json()
    seen = 0
    for path, item in schema["paths"].items():
        if not path.startswith(f"{API}/") and path != f"{API}/profile":
            continue
        for method, op in item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            seen += 1
            assert op["x-roles"] == ["org_admin"], (method, path)
            assert op["x-required-flag"] in {
                "settings",
                "email_sending",
                "ai_provider_setup",
                "org_branding",
                "policy_documents",
            }
            assert op["summary"] and op["description"], (method, path)
            assert "403" in op["responses"] or "401" in op["responses"], (method, path)
    assert seen == 30  # the 30 routes of the spec 6/M4 table
