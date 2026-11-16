"""Guards, OpenAPI extensions, flags and pagination (pure unit tests, no database)."""

import uuid
from types import SimpleNamespace
from typing import Annotated

import pytest
from fastapi import APIRouter, Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.core import deps, flags, security
from app.core.db import TenantContext
from app.core.errors import install_error_handlers
from app.core.pagination import Page, PageParams, page_params, parse_sort
from app.core.roles import HR_USER, ORG_ADMIN, SUPER_ADMIN

ORG = uuid.uuid4()
OWNER = uuid.uuid4()


def _ctx(role, user_id=None, org_id=ORG):
    return TenantContext(
        org_id=org_id,
        user_id=user_id or uuid.uuid4(),
        role=role,
        is_platform=role == SUPER_ADMIN,
        request_id="r",
        ip=None,
        user_agent=None,
    )


class FlagSession:
    """Fake AsyncSession answering the flags query."""

    def __init__(self, rows):
        self.rows = rows

    async def execute(self, statement, params=None):
        rows = self.rows
        return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows))


def _flag_rows(**overrides):
    rows = [{"key": k, "enabled": True, "visible_to_org": k != "watermark"} for k in flags.FLAG_KEYS]
    for row in rows:
        if row["key"] in overrides:
            row["enabled"] = overrides[row["key"]]
    return rows


async def _owner_of_thing(thing_id: str):
    return OWNER if thing_id == "mine" else (uuid.uuid4() if thing_id == "other" else None)


def _app(ctx, flag_rows=None):
    app = FastAPI()
    install_error_handlers(app)
    router = APIRouter(prefix="/api/v1/things", dependencies=[Depends(deps.require_feature("assessments"))])

    @router.get("/admin", dependencies=[Depends(deps.require_role(ORG_ADMIN))])
    async def admin_only():
        return {"ok": True}

    @router.patch("/{thing_id}")
    async def edit(
        thing_id: str, c: Annotated[TenantContext, Depends(deps.require_owner_or_admin(_owner_of_thing))]
    ):
        return {"ok": True}

    app.include_router(router)

    @app.get("/public", openapi_extra={"x-roles": [], "x-required-flag": None})
    async def public():
        return {}

    @app.get("/me")
    async def me(c: Annotated[TenantContext, Depends(deps.current_ctx)]):
        return {"role": c.role}

    deps.install_openapi_extensions(app)
    app.dependency_overrides[deps.current_ctx] = lambda: ctx

    async def fake_session():
        yield FlagSession(flag_rows or _flag_rows())

    app.dependency_overrides[deps.db_session] = fake_session
    return app


async def _get(app, method, path, **kw):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        return await http.request(method, path, **kw)


async def test_require_role_allows_and_forbids():
    assert (await _get(_app(_ctx(ORG_ADMIN)), "GET", "/api/v1/things/admin")).status_code == 200
    resp = await _get(_app(_ctx(HR_USER)), "GET", "/api/v1/things/admin")
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "FORBIDDEN"


async def test_require_owner_or_admin():
    owner = _ctx(HR_USER, user_id=OWNER)
    other_hr = _ctx(HR_USER)
    assert (await _get(_app(owner), "PATCH", "/api/v1/things/mine")).status_code == 200
    assert (await _get(_app(_ctx(ORG_ADMIN)), "PATCH", "/api/v1/things/other")).status_code == 200
    assert (await _get(_app(other_hr), "PATCH", "/api/v1/things/mine")).status_code == 403
    assert (await _get(_app(owner), "PATCH", "/api/v1/things/missing")).status_code == 404
    assert (await _get(_app(_ctx(SUPER_ADMIN)), "PATCH", "/api/v1/things/mine")).status_code == 403


async def test_require_feature_disabled_returns_403():
    app = _app(_ctx(ORG_ADMIN), _flag_rows(assessments=False))
    resp = await _get(app, "GET", "/api/v1/things/admin")
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "FEATURE_DISABLED"
    assert resp.json()["error"]["details"] == {"flag": "assessments"}


def test_require_feature_unknown_key_fails_fast():
    with pytest.raises(ValueError):
        deps.require_feature("not_a_flag")


async def test_openapi_has_x_roles_and_x_required_flag():
    schema = (await _get(_app(_ctx(ORG_ADMIN)), "GET", "/openapi.json")).json()
    admin = schema["paths"]["/api/v1/things/admin"]["get"]
    assert admin["x-roles"] == [ORG_ADMIN] and admin["x-required-flag"] == "assessments"
    edit = schema["paths"]["/api/v1/things/{thing_id}"]["patch"]
    assert edit["x-roles"] == [ORG_ADMIN, HR_USER]
    public = schema["paths"]["/public"]["get"]
    assert public["x-roles"] == [] and public["x-required-flag"] is None
    me = schema["paths"]["/me"]["get"]
    assert me["x-roles"] == [SUPER_ADMIN, ORG_ADMIN, HR_USER] and me["x-required-flag"] is None


async def test_current_ctx_from_bearer_token(monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4(), org_id=ORG, role=HR_USER, token_version=2)
    principal = deps.Principal(
        user_id=user.id,
        org_id=ORG,
        role=HR_USER,
        status="active",
        token_version=2,
        must_change_password=False,
        name="Hal",
        org_status="active",
    )

    async def loader(claims):
        return principal

    monkeypatch.setattr(deps, "load_principal", loader)
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/me")
    async def me(c: Annotated[TenantContext, Depends(deps.current_ctx)]):
        return {"user": str(c.user_id), "org": str(c.org_id), "name": c.actor_name}

    token = security.create_access_token(user, uuid.uuid4())
    ok = await _get(app, "GET", "/me", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200 and ok.json() == {"user": str(user.id), "org": str(ORG), "name": "Hal"}
    assert (await _get(app, "GET", "/me")).json()["error"]["code"] == "UNAUTHENTICATED"

    principal.token_version = 3  # tv bumped: old tokens die
    assert (await _get(app, "GET", "/me", headers={"Authorization": f"Bearer {token}"})).status_code == 401
    principal.token_version = 2
    principal.must_change_password = True
    resp = await _get(app, "GET", "/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "MUST_CHANGE_PASSWORD"


async def test_flags_effective_map_hides_and_applies_dependencies():
    session = FlagSession(_flag_rows(roles=False))
    eff = await flags.effective_map(session, ORG)
    assert "watermark" not in eff
    assert eff["roles"] is False and eff["invites"] is False  # invites needs roles
    assert eff["assessments"] is True
    full = await flags.effective_map(session, ORG, include_hidden=True)
    assert full["watermark"] is True
    assert await flags.is_enabled(session, ORG, "invites") is False
    assert await flags.is_enabled(session, ORG, "unknown") is False


def test_flag_catalog_is_appendix_e():
    assert len(flags.FLAG_KEYS) == 22


def test_parse_sort_and_page():
    assert parse_sort("created_at:desc", {"created_at", "name"}) == [("created_at", "desc")]
    assert parse_sort(None, {"x"}, default="x:asc") == [("x", "asc")]
    for bad in ("evil:desc", "name:sideways", "name"):
        with pytest.raises(deps.AppError):
            parse_sort(bad, {"created_at", "name"})
    params = PageParams(page=3, page_size=20, sort=None)
    assert params.offset == 40 and params.limit == 20
    page = Page[int].build([1, 2], total=42, params=params)
    assert page.model_dump() == {"items": [1, 2], "total": 42, "page": 3, "page_size": 20}


async def test_page_size_over_100_rejected():
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/list")
    async def lst(p: Annotated[PageParams, Depends(page_params)]):
        return {"size": p.page_size}

    assert (await _get(app, "GET", "/list?page_size=100")).json() == {"size": 100}
    resp = await _get(app, "GET", "/list?page_size=1000")
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "VALIDATION_ERROR"
