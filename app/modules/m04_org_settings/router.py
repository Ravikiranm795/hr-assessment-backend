"""M4 organization settings routes (spec 6/M4): ``/api/v1/org/*``, organization admin only.

One sub-router per feature flag (settings, email_sending, ai_provider_setup, org_branding,
policy_documents) so each route declares exactly one ``x-required-flag``.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Request, UploadFile
from pydantic import ValidationError

from app.core import flags
from app.core.db import TenantContext
from app.core.deps import DbSession, require_feature, require_role
from app.core.errors import ERROR_CODES, validation_error
from app.core.pagination import Page, PageParams, page_params, parse_sort
from app.core.roles import ORG_ADMIN
from app.db import Document
from app.modules.m04_org_settings import catalog, schemas
from app.modules.m04_org_settings.service import (
    AiSettingsService,
    BrandingService,
    DocumentService,
    EmailSettingsService,
    OrgSettingsService,
)

Admin = Annotated[TenantContext, Depends(require_role(ORG_ADMIN))]


def _err(code: str, description: str) -> dict[str, Any]:
    spec = ERROR_CODES[code]
    example = {"error": {"code": code, "message_key": spec.message_key, "details": {}, "request_id": "req-1"}}
    return {"description": description, "content": {"application/json": {"example": example}}}


_AUTH = {
    401: _err("UNAUTHENTICATED", "Missing or invalid access token."),
    403: _err("FORBIDDEN", "Not an organization admin, or the feature flag is off (FEATURE_DISABLED)."),
}
_VALID = {422: _err("VALIDATION_ERROR", "Invalid body or file (details.fields).")}
_NOT_FOUND = {404: _err("NOT_FOUND", "Unknown id in this organization (foreign ids are also 404).")}


def _group(flag: str, tag: str) -> APIRouter:
    return APIRouter(
        prefix="/api/v1/org", tags=[tag], dependencies=[Depends(require_feature(flag))], responses=_AUTH
    )


# --------------------------------------------------------------------------- profile and settings

settings_router = _group(flags.SETTINGS, "org-settings")


@settings_router.get(
    "/profile",
    response_model=schemas.ProfileOut,
    summary="Organization profile",
    description="Name, slug, locale and timezone of the caller's organization.",
)
async def get_profile(ctx: Admin, session: DbSession):
    return await OrgSettingsService(session).get_profile(ctx)


@settings_router.patch(
    "/profile",
    response_model=schemas.ProfileOut,
    summary="Update the organization profile",
    description="Name, locale or timezone. Audited as `SETTINGS_UPDATED`.",
    responses=_VALID,
)
async def patch_profile(body: schemas.ProfileIn, ctx: Admin, session: DbSession):
    return await OrgSettingsService(session).update_profile(ctx, body)


@settings_router.get(
    "/settings/security",
    response_model=schemas.SecurityOut,
    summary="Security settings",
    description="Allowed Google domains (empty = Google sign-in off), session timeout, password policy "
    "and the admin TOTP requirement.",
)
async def get_security(ctx: Admin, session: DbSession):
    return await OrgSettingsService(session).get_security(ctx)


@settings_router.patch(
    "/settings/security",
    response_model=schemas.SecurityOut,
    summary="Update security settings",
    description="Partial update. Audited as `SETTINGS_UPDATED`.",
    responses=_VALID,
)
async def patch_security(body: schemas.SecurityIn, ctx: Admin, session: DbSession):
    return await OrgSettingsService(session).update_security(ctx, body)


@settings_router.get(
    "/settings/retention",
    response_model=schemas.RetentionOut,
    summary="Candidate retention",
    description="Days candidate detail data is kept (7 to 730, default 90).",
)
async def get_retention(ctx: Admin, session: DbSession):
    return await OrgSettingsService(session).get_retention(ctx)


@settings_router.patch(
    "/settings/retention",
    response_model=schemas.RetentionOut,
    summary="Update candidate retention",
    description="`retention_days` must be between 7 and 730.",
    responses=_VALID,
)
async def patch_retention(body: schemas.RetentionIn, ctx: Admin, session: DbSession):
    return await OrgSettingsService(session).update_retention(ctx, body)


@settings_router.get(
    "/settings/invites",
    response_model=schemas.InvitesOut,
    summary="Invite defaults",
    description="Default invite expiry in hours.",
)
async def get_invites(ctx: Admin, session: DbSession):
    return await OrgSettingsService(session).get_invites(ctx)


@settings_router.patch(
    "/settings/invites",
    response_model=schemas.InvitesOut,
    summary="Update invite defaults",
    description="Default invite expiry in hours.",
    responses=_VALID,
)
async def patch_invites(body: schemas.InvitesIn, ctx: Admin, session: DbSession):
    return await OrgSettingsService(session).update_invites(ctx, body)


# --------------------------------------------------------------------------- email

email_router = _group(flags.EMAIL_SENDING, "org-email")


@email_router.get(
    "/settings/email",
    response_model=schemas.EmailSettingsOut,
    summary="SMTP settings",
    description="The password is never returned, only `password_set`.",
)
async def get_email(ctx: Admin, session: DbSession):
    return await EmailSettingsService(session).get(ctx)


@email_router.put(
    "/settings/email",
    response_model=schemas.EmailSettingsOut,
    summary="Save SMTP settings",
    description="Password is write-only and stored encrypted. Saving resets the verified state.",
    responses=_VALID,
)
async def put_email(body: schemas.EmailSettingsIn, ctx: Admin, session: DbSession):
    return await EmailSettingsService(session).put(ctx, body)


@email_router.post(
    "/settings/email/test",
    response_model=schemas.EmailTestOut,
    summary="Send a test email",
    description="Sends one message to `to` using the saved settings. Audited (`EMAIL_TEST_SENT`) and rate "
    "limited. `SMTP_NOT_CONFIGURED` when nothing is saved.",
    responses={
        **_VALID,
        409: _err("SMTP_NOT_CONFIGURED", "No SMTP settings saved."),
        429: _err("RATE_LIMITED", "Too many test sends."),
    },
)
async def test_email(body: schemas.EmailTestIn, ctx: Admin, session: DbSession):
    return await EmailSettingsService(session).send_test(ctx, body.to)


# --------------------------------------------------------------------------- AI provider setup

ai_router = _group(flags.AI_PROVIDER_SETUP, "org-ai")


@ai_router.get(
    "/settings/ai/providers",
    response_model=schemas.AiProvidersOut,
    summary="AI provider catalog",
    description="Providers, the fields each needs and light default models.",
)
async def ai_providers(ctx: Admin):
    return schemas.AiProvidersOut(providers=catalog.provider_catalog())


@ai_router.get(
    "/settings/ai",
    response_model=schemas.AiSettingsOut,
    summary="AI settings",
    description="Provider, model, mode, base URL and cap. The key is never returned, only `api_key_set`.",
)
async def get_ai(ctx: Admin, session: DbSession):
    return await AiSettingsService(session).get(ctx)


@ai_router.put(
    "/settings/ai",
    response_model=schemas.AiSettingsOut,
    summary="Save AI settings",
    description="API key is write-only and encrypted. CLI mode only accepts a command key from the server "
    "allowlist and runs on the backend host.",
    responses=_VALID,
)
async def put_ai(body: schemas.AiSettingsIn, ctx: Admin, session: DbSession):
    return await AiSettingsService(session).put(ctx, body)


@ai_router.post(
    "/settings/ai/test",
    response_model=schemas.AiTestOut,
    summary="Test the AI connection",
    description="Calls the configured provider with a tiny request. `AI_NOT_CONFIGURED` when unset.",
    responses={409: _err("AI_NOT_CONFIGURED", "No AI provider configured.")},
)
async def test_ai(ctx: Admin, session: DbSession):
    return await AiSettingsService(session).test_connection(ctx)


@ai_router.get(
    "/ai-usage",
    response_model=schemas.AiUsageOut,
    summary="AI usage this month",
    description="Calls and tokens used this month against the optional monthly cap.",
)
async def ai_usage(ctx: Admin, session: DbSession):
    return await AiSettingsService(session).usage(ctx)


# --------------------------------------------------------------------------- branding

branding_router = _group(flags.ORG_BRANDING, "org-branding")


@branding_router.get(
    "/branding",
    response_model=schemas.BrandingOut,
    summary="Organization branding",
    description="Colors, background style, font, logo and favicon URLs and the contrast checks.",
)
async def get_branding(ctx: Admin, session: DbSession):
    return await BrandingService(session).get(ctx)


@branding_router.put(
    "/branding",
    response_model=schemas.BrandingOut,
    summary="Save branding",
    description="Fails with `BRANDING_CONTRAST_TOO_LOW` when text on primary or accent is under WCAG AA.",
    responses={**_VALID, 422: _err("BRANDING_CONTRAST_TOO_LOW", "Contrast below WCAG AA (details.checks).")},
)
async def put_branding(body: schemas.BrandingIn, ctx: Admin, session: DbSession):
    return await BrandingService(session).put(ctx, body)


@branding_router.post(
    "/branding/validate",
    response_model=schemas.BrandingValidateOut,
    summary="Check contrast without saving",
    description="Returns the contrast ratios for the given colors.",
    responses=_VALID,
)
async def validate_branding(body: schemas.BrandingValidateIn, ctx: Admin, session: DbSession):
    return await BrandingService(session).validate(ctx, body)


@branding_router.post(
    "/branding/reset",
    response_model=schemas.BrandingOut,
    summary="Reset to the starter theme",
    description="Restores the starter colors and font. The organization's own logo and favicon stay.",
)
async def reset_branding(ctx: Admin, session: DbSession):
    return await BrandingService(session).reset(ctx)


@branding_router.get(
    "/branding/fonts",
    response_model=schemas.FontsOut,
    summary="Curated font list",
    description="Fonts allowed for `font_key`.",
)
async def branding_fonts(ctx: Admin):
    return BrandingService.fonts()


@branding_router.post(
    "/branding/logo",
    response_model=schemas.BrandingOut,
    summary="Upload the logo",
    description="Multipart field `file`. PNG, JPEG or WebP, magic bytes and size are checked.",
    responses=_VALID,
)
async def upload_logo(ctx: Admin, session: DbSession, file: Annotated[UploadFile | None, File()] = None):
    return await BrandingService(session).upload(ctx, "logo", file)


@branding_router.post(
    "/branding/favicon",
    response_model=schemas.BrandingOut,
    summary="Upload the favicon",
    description="Multipart field `file`. PNG, ICO, JPEG or WebP, magic bytes and size are checked.",
    responses=_VALID,
)
async def upload_favicon(ctx: Admin, session: DbSession, file: Annotated[UploadFile | None, File()] = None):
    return await BrandingService(session).upload(ctx, "favicon", file)


# --------------------------------------------------------------------------- policy documents

documents_router = _group(flags.POLICY_DOCUMENTS, "org-documents")
_DOC_SORT = {"created_at", "updated_at", "title"}
_DOC_COLUMNS = {"created_at": Document.created_at, "updated_at": Document.updated_at, "title": Document.title}


@documents_router.get(
    "/documents",
    response_model=Page[schemas.DocumentOut],
    summary="List documents",
    description="Privacy, terms, consent and other documents, with version counts and the current "
    "published version. Filter with `doc_type`.",
    responses=_VALID,
)
async def list_documents(
    ctx: Admin,
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    doc_type: schemas.DocType | None = None,
):
    sort = parse_sort(params.sort, _DOC_SORT, default="created_at:desc")
    order = [_DOC_COLUMNS[f].asc() if d == "asc" else _DOC_COLUMNS[f].desc() for f, d in sort]
    items, total = await DocumentService(session).list(
        ctx, doc_type=doc_type, order=order, offset=params.offset, limit=params.limit
    )
    return Page[schemas.DocumentOut].build(items, total=total, params=params)


@documents_router.post(
    "/documents",
    response_model=schemas.DocumentOut,
    status_code=201,
    summary="Create a document",
    description="Metadata only; add content with a version. Audited as `DOCUMENT_CREATED`.",
    responses=_VALID,
)
async def create_document(body: schemas.DocumentCreate, ctx: Admin, session: DbSession):
    return await DocumentService(session).create(ctx, body)


@documents_router.get(
    "/documents/{doc_id}",
    response_model=schemas.DocumentOut,
    summary="Document detail",
    description="Metadata, version count and the current published version.",
    responses=_NOT_FOUND,
)
async def get_document(doc_id: UUID, ctx: Admin, session: DbSession):
    return await DocumentService(session).get(ctx, doc_id)


@documents_router.patch(
    "/documents/{doc_id}",
    response_model=schemas.DocumentOut,
    summary="Update document metadata",
    description="Title or type. Changing the type after a version is published is `CONFLICT`.",
    responses={**_VALID, **_NOT_FOUND, 409: _err("CONFLICT", "Document already has a published version.")},
)
async def patch_document(doc_id: UUID, body: schemas.DocumentPatch, ctx: Admin, session: DbSession):
    return await DocumentService(session).update(ctx, doc_id, body)


@documents_router.get(
    "/documents/{doc_id}/versions",
    response_model=schemas.VersionListOut,
    summary="Version history",
    description="All versions of the document, newest first.",
    responses=_NOT_FOUND,
)
async def list_versions(doc_id: UUID, ctx: Admin, session: DbSession):
    return await DocumentService(session).list_versions(ctx, doc_id)


@documents_router.post(
    "/documents/{doc_id}/versions",
    response_model=schemas.VersionOut,
    status_code=201,
    summary="Create a draft version",
    description="Send JSON `{content_html}` (sanitized on save) or multipart with a PDF in field `file`.",
    responses={**_VALID, **_NOT_FOUND},
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/VersionJsonIn"}},
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {"file": {"type": "string", "format": "binary"}},
                        "required": ["file"],
                    }
                },
            },
        }
    },
)
async def create_version(doc_id: UUID, request: Request, ctx: Admin, session: DbSession):
    service = DocumentService(session)
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        upload = (await request.form()).get("file")
        return await service.create_version(ctx, doc_id, pdf=upload if hasattr(upload, "read") else None)
    try:
        body = schemas.VersionJsonIn.model_validate(await request.json())
    except (ValidationError, ValueError) as exc:
        fields = (
            [{"field": ".".join(str(p) for p in e["loc"]), "code": e["type"]} for e in exc.errors()]
            if isinstance(exc, ValidationError)
            else [{"field": "body", "code": "invalid_json"}]
        )
        raise validation_error(fields) from exc
    return await service.create_version(ctx, doc_id, html=body.content_html)


@documents_router.post(
    "/documents/{doc_id}/versions/{version_id}/publish",
    response_model=schemas.VersionOut,
    summary="Publish a version",
    description="A published version never changes again. Audited as `DOCUMENT_PUBLISHED`.",
    responses={**_NOT_FOUND, 409: _err("CONFLICT", "Version already published.")},
)
async def publish_version(doc_id: UUID, version_id: UUID, ctx: Admin, session: DbSession):
    return await DocumentService(session).publish_out(ctx, doc_id, version_id)


# --------------------------------------------------------------------------- assemble

router = APIRouter()
for _sub in (settings_router, email_router, ai_router, branding_router, documents_router):
    router.include_router(_sub)
