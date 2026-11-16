"""M10 audit API: scoping, filters, exports, append-only, permission matrix (spec 6/M10, 4.3, 4.5)."""

import csv
import io
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core import audit
from app.core.db import tenant_session
from tests.m10_audit.conftest import make_ctx

OA_LIST = "/api/v1/audit-logs"
SA_LIST = "/api/v1/platform/audit-logs"


def _ids(body):
    return {item["entity_id"] for item in body["items"]}


async def _disable_flag(db_session, org, key):
    await db_session.execute(
        text("INSERT INTO org_feature_flags (org_id, flag_key, enabled) VALUES (:o, :k, false)"),
        {"o": org.id, "k": key},
    )
    await db_session.commit()


# --------------------------------------------------------------------------- scoping


async def test_oa_sees_only_own_org_rows(client, world, add_row):
    mine, theirs = f"mine-{uuid.uuid4().hex[:8]}", f"theirs-{uuid.uuid4().hex[:8]}"
    await add_row(world.org_a.id, entity_id=mine)
    await add_row(world.org_b.id, entity_id=theirs)
    await add_row(None, audit.AUDIT_PURGE_RUN, entity_type="system", entity_id="platform-row", role="system")

    res = await client.get(OA_LIST, headers=world.oa_a, params={"page_size": 100})
    assert res.status_code == 200, res.text
    body = res.json()
    assert mine in _ids(body)
    assert theirs not in _ids(body)
    assert "platform-row" not in _ids(body)
    assert {item["org_id"] for item in body["items"]} == {str(world.org_a.id)}

    # An OA cannot widen the scope with an org_id query parameter.
    res = await client.get(OA_LIST, headers=world.oa_a, params={"org_id": str(world.org_b.id)})
    assert theirs not in _ids(res.json())

    # Cross-tenant detail is 404, never 403.
    other = await client.get(OA_LIST, headers=world.oa_b, params={"q": theirs})
    other_id = other.json()["items"][0]["id"]
    assert (await client.get(f"{OA_LIST}/{other_id}", headers=world.oa_a)).status_code == 404
    assert (await client.get(f"{OA_LIST}/{other_id}", headers=world.oa_b)).status_code == 200


async def test_sa_platform_view_all_orgs_read_only(client, world, add_row):
    a, b = f"a-{uuid.uuid4().hex[:8]}", f"b-{uuid.uuid4().hex[:8]}"
    await add_row(world.org_a.id, entity_id=a)
    await add_row(world.org_b.id, entity_id=b)
    await add_row(None, audit.AUDIT_PURGE_RUN, entity_type="system", entity_id="plat", role="system")

    res = await client.get(SA_LIST, headers=world.sa, params={"page_size": 100})
    assert res.status_code == 200, res.text
    ids = _ids(res.json())
    assert {a, b, "plat"} <= ids

    scoped = await client.get(SA_LIST, headers=world.sa, params={"org_id": str(world.org_b.id)})
    assert b in _ids(scoped.json()) and a not in _ids(scoped.json())

    row_id = next(i["id"] for i in res.json()["items"] if i["entity_id"] == a)
    detail = await client.get(f"{SA_LIST}/{row_id}", headers=world.sa)
    assert detail.status_code == 200
    assert detail.json()["org_id"] == str(world.org_a.id)
    assert (await client.get(f"{SA_LIST}/{uuid.uuid4()}", headers=world.sa)).status_code == 404

    # Read-only: no mutating verbs exist on the platform view.
    for method in ("put", "patch", "delete"):
        assert (await client.request(method, f"{SA_LIST}/{row_id}", headers=world.sa)).status_code == 405
    assert (await client.post(SA_LIST, headers=world.sa, json={})).status_code == 405

    # The SA view is not reachable by org roles, and the org view is not the SA's.
    assert (await client.get(SA_LIST, headers=world.oa_a)).status_code == 403
    assert (await client.get(OA_LIST, headers=world.sa)).status_code == 403


async def test_sa_export_audited_with_null_org(client, world, add_row, db_session):
    await add_row(world.org_a.id, entity_id="sa-exp")
    res = await client.post(
        f"{SA_LIST}/export",
        headers=world.sa,
        json={"format": "csv", "columns": ["action", "org_id"], "filters": {"org_id": str(world.org_a.id)}},
    )
    assert res.status_code == 200, res.text
    row = (
        (
            await db_session.execute(
                text(
                    "SELECT org_id, actor_role, after->>'format' AS fmt FROM audit_logs "
                    "WHERE action = 'AUDIT_EXPORTED' AND org_id IS NULL ORDER BY created_at DESC LIMIT 1"
                )
            )
        )
        .mappings()
        .one()
    )
    assert row["actor_role"] == "super_admin" and row["fmt"] == "csv"


# --------------------------------------------------------------------------- exports


async def test_export_is_itself_audited(client, world, add_row):
    await add_row(world.org_a.id, entity_id="exp-1")
    res = await client.post(
        f"{OA_LIST}/export",
        headers=world.oa_a,
        json={"format": "csv", "columns": ["created_at", "action", "entity_id"], "filters": {"q": "exp-1"}},
    )
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("text/csv")
    assert "attachment" in res.headers["content-disposition"] and ".csv" in res.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(res.content.decode("utf-8-sig"))))
    assert rows[0] == ["created_at", "action", "entity_id"]
    assert [r[2] for r in rows[1:]] == ["exp-1"]

    listed = await client.get(OA_LIST, headers=world.oa_a, params={"action": "AUDIT_EXPORTED"})
    items = listed.json()["items"]
    assert len(items) == 1
    detail = (await client.get(f"{OA_LIST}/{items[0]['id']}", headers=world.oa_a)).json()
    assert detail["action"] == "AUDIT_EXPORTED"
    assert detail["after"]["format"] == "csv"
    assert detail["after"]["columns"] == ["created_at", "action", "entity_id"]
    assert detail["after"]["row_count"] == 1
    assert detail["after"]["filters"]["q"] == "exp-1"
    assert detail["actor_role"] == "org_admin"
    assert "exp-1" not in str(detail["after"].get("rows", ""))  # never the rows themselves


async def test_pdf_export_has_header_and_is_audited(client, world, add_row):
    await add_row(world.org_a.id, entity_id="pdf-1")
    res = await client.post(
        f"{OA_LIST}/export", headers=world.oa_a, json={"format": "pdf", "filters": {"q": "pdf-1"}}
    )
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("application/pdf")
    assert res.content.startswith(b"%PDF")
    listed = await client.get(OA_LIST, headers=world.oa_a, params={"action": "AUDIT_EXPORTED"})
    assert listed.json()["total"] == 1


async def test_pdf_header_uses_app_name(world, monkeypatch):
    from app.core.config import settings
    from app.modules.m10_audit import export

    captured = {}

    def fake_render(title, html):
        captured.update(title=title, html=html)
        return b"%PDF-fake"

    monkeypatch.setattr(export, "render_pdf", fake_render)
    monkeypatch.setattr(settings, "app_name", "Acme <Assess>")
    content = export.render_export([{"action": "X<script>"}], ["action"], "pdf", filters_summary="q=1")
    assert content == b"%PDF-fake"
    assert "Acme &lt;Assess&gt;" in captured["html"]
    assert "X&lt;script&gt;" in captured["html"] and "<script>" not in captured["html"]


async def test_csv_formula_injection_prefixed(client, world, add_row):
    token = uuid.uuid4().hex[:6]
    await add_row(
        world.org_a.id,
        entity_id=f"@SUM({token})",
        reason='=HYPERLINK("http://evil")',
        name="+cmd|' /C calc'!A0",
    )
    res = await client.post(
        f"{OA_LIST}/export",
        headers=world.oa_a,
        json={
            "format": "csv",
            "columns": ["actor_name", "entity_id", "reason"],
            "filters": {"q": token},
        },
    )
    assert res.status_code == 200, res.text
    rows = list(csv.reader(io.StringIO(res.content.decode("utf-8-sig"))))
    assert len(rows) == 2
    name, entity_id, reason = rows[1]
    assert name.startswith("'+") and entity_id.startswith("'@") and reason.startswith("'=")


async def test_export_validation_and_flags(client, world, add_row, db_session, monkeypatch):
    await add_row(world.org_a.id)
    bad_col = await client.post(
        f"{OA_LIST}/export", headers=world.oa_a, json={"format": "csv", "columns": ["nope"]}
    )
    assert bad_col.status_code == 422
    assert bad_col.json()["error"]["code"] == "VALIDATION_ERROR"
    assert bad_col.json()["error"]["details"]["fields"][0]["field"] == "columns"
    bad_fmt = await client.post(f"{OA_LIST}/export", headers=world.oa_a, json={"format": "xls"})
    assert bad_fmt.status_code == 422

    from app.modules.m10_audit import export

    monkeypatch.setattr(export, "MAX_EXPORT_ROWS", {"csv": 0, "pdf": 0})
    too_many = await client.post(f"{OA_LIST}/export", headers=world.oa_a, json={"format": "csv"})
    assert too_many.status_code == 422
    assert too_many.json()["error"]["details"]["fields"][0]["code"] == "too_many_rows"

    # audit_export off: the screen still works, the export is blocked and nothing is audited.
    await _disable_flag(db_session, world.org_a, "audit_export")
    blocked = await client.post(f"{OA_LIST}/export", headers=world.oa_a, json={"format": "csv"})
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "FEATURE_DISABLED"
    assert blocked.json()["error"]["details"]["flag"] == "audit_export"
    assert (await client.get(OA_LIST, headers=world.oa_a)).status_code == 200
    assert (await client.get(OA_LIST, headers=world.oa_b)).status_code == 200

    # audit_view off: the whole screen is blocked.
    await _disable_flag(db_session, world.org_a, "audit_view")
    res = await client.get(OA_LIST, headers=world.oa_a)
    assert res.status_code == 403 and res.json()["error"]["details"]["flag"] == "audit_view"
    # Super admins are never flag-gated.
    assert (await client.get(SA_LIST, headers=world.sa)).status_code == 200


# --------------------------------------------------------------------------- append-only


async def test_audit_table_rejects_update_via_api_role(world, add_row, client):
    await add_row(world.org_a.id, entity_id="immutable")
    ctx = make_ctx(world.org_a.id)
    for statement in (
        "UPDATE audit_logs SET reason = 'tampered'",
        "DELETE FROM audit_logs",
        "TRUNCATE audit_logs",
    ):
        with pytest.raises(DBAPIError) as exc:
            async with tenant_session(ctx) as session:
                assert (await session.execute(text("select current_user"))).scalar_one() == "app_rw"
                await session.execute(text(statement))
        assert "permission denied" in str(exc.value).lower()
    # And no HTTP route mutates audit rows.
    row_id = (await client.get(OA_LIST, headers=world.oa_a, params={"q": "immutable"})).json()["items"][0][
        "id"
    ]
    for method in ("put", "patch", "delete", "post"):
        res = await client.request(method, f"{OA_LIST}/{row_id}", headers=world.oa_a)
        assert res.status_code == 405


# --------------------------------------------------------------------------- filters


async def test_filters_actor_action_date(client, world, add_row, add_raw_row):
    actor_1, actor_2 = uuid.uuid4(), uuid.uuid4()
    org = world.org_a.id
    await add_row(org, audit.USER_CREATED, entity_id="f-1", user_id=actor_1, name="Alice Admin")
    await add_row(
        org, audit.ROLE_CREATED, entity_type="open_role", entity_id="f-2", user_id=actor_2, name="Bob"
    )
    await add_row(
        org, audit.AUTH_LOGIN_FAILED, entity_type="user", entity_id="f-3", user_id=actor_2, result="failure"
    )
    old = datetime.now(UTC) - timedelta(days=60)
    await add_raw_row(org, old, entity_id="f-old", action=audit.USER_UPDATED)

    async def ids(**params):
        res = await client.get(OA_LIST, headers=world.oa_a, params={"page_size": 100, **params})
        assert res.status_code == 200, res.text
        return _ids(res.json())

    assert await ids() == {"f-1", "f-2", "f-3"}  # default window is the last 30 days
    assert await ids(actor_user_id=str(actor_1)) == {"f-1"}
    assert await ids(actor_user_id=str(actor_2)) == {"f-2", "f-3"}
    assert await ids(action="ROLE_CREATED") == {"f-2"}
    assert await ids(action=["ROLE_CREATED", "USER_CREATED"]) == {"f-1", "f-2"}
    assert await ids(entity_type="open_role") == {"f-2"}
    assert await ids(entity_id="f-3") == {"f-3"}
    assert await ids(result="failure") == {"f-3"}
    assert await ids(actor_type="candidate") == set()
    assert await ids(q="alice") == {"f-1"}
    assert await ids(q="f-%") == set()  # LIKE wildcards are literal
    start = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    end = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    assert await ids(date_from=start) == {"f-1", "f-2", "f-3", "f-old"}
    assert await ids(date_from=start, date_to=end) == {"f-old"}
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    assert await ids(date_from=(datetime.now(UTC) - timedelta(hours=1)).isoformat(), date_to=future) == {
        "f-1",
        "f-2",
        "f-3",
    }

    # Sorting and pagination.
    res = await client.get(OA_LIST, headers=world.oa_a, params={"sort": "action:asc", "page_size": 2})
    body = res.json()
    assert body["total"] == 3 and body["page"] == 1 and body["page_size"] == 2 and len(body["items"]) == 2
    assert [i["action"] for i in body["items"]] == sorted(i["action"] for i in body["items"])


async def test_filter_validation(client, world):
    cases = [
        ({"action": "NOT_AN_ACTION"}, "action"),
        ({"result": "maybe"}, "result"),
        ({"actor_user_id": "nope"}, "actor_user_id"),
        ({"date_from": "2026-02-01T00:00:00Z", "date_to": "2026-01-01T00:00:00Z"}, "date_to"),
        ({"sort": "reason:asc"}, "sort"),
        ({"page_size": 101}, "page_size"),
    ]
    for params, field in cases:
        res = await client.get(OA_LIST, headers=world.oa_a, params=params)
        assert res.status_code == 422, (params, res.text)
        err = res.json()["error"]
        assert err["code"] == "VALIDATION_ERROR"
        assert field in {f["field"] for f in err["details"]["fields"]}, (params, err)
    assert (await client.get(f"{OA_LIST}/not-a-uuid", headers=world.oa_a)).status_code == 422


# --------------------------------------------------------------------------- content


async def test_redacted_values_never_returned(client, world, add_row, add_raw_row):
    org = world.org_a.id
    await add_row(
        org,
        audit.USER_UPDATED,
        entity_id="red-1",
        before={"name": "A", "password_hash": "secret-old"},
        after={"name": "B", "password_hash": "secret-new", "nested": {"api_key": "sk-123"}},
    )
    raw = await add_raw_row(
        org,
        datetime.now(UTC),
        entity_id="red-2",
        before={"token": "leaky-token", "ok": 1},
        after={"smtp_password": "leaky-smtp"},
    )
    for entity_id in ("red-1", "red-2"):
        item = (await client.get(OA_LIST, headers=world.oa_a, params={"entity_id": entity_id})).json()[
            "items"
        ][0]
        detail = (await client.get(f"{OA_LIST}/{item['id']}", headers=world.oa_a)).json()
        blob = str(detail)
        for leaked in ("secret-old", "secret-new", "sk-123", "leaky-token", "leaky-smtp"):
            assert leaked not in blob
    detail = (await client.get(f"{OA_LIST}/{raw}", headers=world.oa_a)).json()
    assert detail["before"] == {"token": "[REDACTED]", "ok": 1}
    first = (await client.get(OA_LIST, headers=world.oa_a, params={"entity_id": "red-1"})).json()["items"][0]
    assert "before" not in first and "after" not in first  # list rows are summaries

    # The export carries the same redacted JSON.
    res = await client.post(
        f"{OA_LIST}/export",
        headers=world.oa_a,
        json={"format": "csv", "columns": ["before", "after"], "filters": {"entity_id": "red-1"}},
    )
    text_out = res.content.decode("utf-8-sig")
    assert "secret-" not in text_out and "[REDACTED]" in text_out


async def test_actions_and_columns_catalog(client, world):
    res = await client.get(f"{OA_LIST}/actions", headers=world.oa_a)
    assert res.status_code == 200
    groups = res.json()["groups"]
    names = [a["action"] for g in groups for a in g["actions"]]
    assert set(names) == set(audit.AUDIT_ACTIONS) and len(names) == len(set(names))
    assert {g["key"] for g in groups} >= {"auth", "platform", "system"}

    cols = (await client.get(f"{OA_LIST}/columns", headers=world.oa_a)).json()["items"]
    keys = [c["key"] for c in cols]
    assert {"created_at", "actor_name", "action", "entity_id", "before", "after", "reason", "ip"} <= set(keys)
    assert all(c["label_key"].startswith("audit.columns.") for c in cols)
    assert any(c["default"] for c in cols)


async def test_security_events_failed_logins_and_lockouts(client, world, add_row, add_raw_row):
    org = world.org_a.id
    await add_row(org, audit.AUTH_LOGIN_FAILED, entity_id="s-1", result="failure")
    await add_row(org, audit.AUTH_LOGIN_FAILED, entity_id="s-2", result="failure")
    await add_row(org, audit.AUTH_ACCOUNT_LOCKED, entity_id="s-3", result="success")
    await add_row(org, audit.AUTH_LOGIN_SUCCESS, entity_id="s-4")
    await add_row(org, audit.USER_CREATED, entity_id="s-5")
    await add_row(world.org_b.id, audit.AUTH_LOGIN_FAILED, entity_id="s-other", result="failure")
    await add_raw_row(
        org, datetime.now(UTC) - timedelta(days=20), entity_id="s-old", action=audit.AUTH_LOGIN_FAILED
    )

    res = await client.get(f"{OA_LIST}/security-events", headers=world.oa_a)
    assert res.status_code == 200, res.text
    body = res.json()
    assert _ids(body) == {"s-1", "s-2", "s-3"}  # default window is 7 days
    assert body["failed_logins"] == 2 and body["lockouts"] == 1 and body["total"] == 3
    assert {i["action"] for i in body["items"]} == {"AUTH_LOGIN_FAILED", "AUTH_ACCOUNT_LOCKED"}
    wide = await client.get(
        f"{OA_LIST}/security-events",
        headers=world.oa_a,
        params={"date_from": (datetime.now(UTC) - timedelta(days=30)).isoformat()},
    )
    assert "s-old" in _ids(wide.json())
    assert wide.json()["failed_logins"] == 3
    # Not available to HR; paginated.
    assert (await client.get(f"{OA_LIST}/security-events", headers=world.hr_a)).status_code == 403
    page = await client.get(f"{OA_LIST}/security-events", headers=world.oa_a, params={"page_size": 1})
    assert len(page.json()["items"]) == 1 and page.json()["total"] == 3


# --------------------------------------------------------------------------- permissions


ROUTES = [
    ("get", "", None),
    ("get", "/actions", None),
    ("get", "/columns", None),
    ("get", "/security-events", None),
    ("get", "/{id}", None),
    ("post", "/export", {"format": "csv"}),
]
# Spec 4.3: audit view is OA (own org) here, SA (all orgs) under /platform.
ORG_EXPECT = {"org_admin": True, "hr_user": False, "super_admin": False, "anonymous": False}
PLATFORM_ROUTES = [
    ("get", "", None),
    ("get", "/actions", None),
    ("get", "/columns", None),
    ("post", "/export", {"format": "csv"}),
]
PLATFORM_EXPECT = {"org_admin": False, "hr_user": False, "super_admin": True, "anonymous": False}


@pytest.mark.parametrize("role", list(ORG_EXPECT))
@pytest.mark.parametrize("method,suffix,body", ROUTES)
async def test_org_route_permission_matrix(client, world, add_row, role, method, suffix, body):
    await add_row(world.org_a.id)
    headers = {
        "org_admin": world.oa_a,
        "hr_user": world.hr_a,
        "super_admin": world.sa,
        "anonymous": {},
    }[role]
    row_id = (await client.get(OA_LIST, headers=world.oa_a)).json()["items"][0]["id"]
    path = OA_LIST + suffix.replace("{id}", row_id)
    res = await client.request(method, path, headers=headers, json=body)
    if ORG_EXPECT[role]:
        assert res.status_code == 200, res.text
    elif role == "anonymous":
        assert res.status_code == 401
    else:
        assert res.status_code == 403
        assert res.json()["error"]["code"] == "FORBIDDEN"


async def test_hr_forbidden(client, world):
    for method, suffix, body in ROUTES:
        path = OA_LIST + suffix.replace("{id}", str(uuid.uuid4()))
        res = await client.request(method, path, headers=world.hr_a, json=body)
        assert res.status_code == 403, (method, suffix, res.text)
    for method, suffix, body in PLATFORM_ROUTES:
        res = await client.request(method, SA_LIST + suffix, headers=world.hr_a, json=body)
        assert res.status_code == 403


@pytest.mark.parametrize("role", list(PLATFORM_EXPECT))
@pytest.mark.parametrize("method,suffix,body", PLATFORM_ROUTES)
async def test_platform_route_permission_matrix(client, world, role, method, suffix, body):
    headers = {
        "org_admin": world.oa_a,
        "hr_user": world.hr_a,
        "super_admin": world.sa,
        "anonymous": {},
    }[role]
    res = await client.request(method, SA_LIST + suffix, headers=headers, json=body)
    if PLATFORM_EXPECT[role]:
        assert res.status_code == 200, res.text
    else:
        assert res.status_code == (401 if role == "anonymous" else 403)


# --------------------------------------------------------------------------- docs and structure


def test_openapi_declares_roles_flags_and_examples():
    from app.main import create_app

    schema = create_app().openapi()
    ops = {}
    for path, methods in schema["paths"].items():
        if "audit-logs" in path:
            for method, op in methods.items():
                ops[(method, path)] = op
    assert len(ops) == 11
    for (method, path), op in ops.items():
        assert op["summary"] and op["description"], (method, path)
        if path.startswith("/api/v1/platform"):
            assert op["x-roles"] == ["super_admin"] and op["x-required-flag"] is None
        else:
            assert op["x-roles"] == ["org_admin"]
        assert "403" in op["responses"] and "401" in op["responses"]
        assert "example" in op["responses"]["403"]["content"]["application/json"]
    export = ops[("post", "/api/v1/audit-logs/export")]
    assert export["x-required-flag"] == ["audit_view", "audit_export"]
    assert ops[("get", "/api/v1/audit-logs")]["x-required-flag"] == "audit_view"
    assert "example" in ops[("get", "/api/v1/audit-logs")]["responses"]["200"]["content"]["application/json"]


def test_action_catalog_matches_core():
    from app.modules.m10_audit import catalog

    flat = [a for _, actions in catalog.ACTION_GROUPS for a in actions]
    assert set(flat) == set(audit.AUDIT_ACTIONS) and len(flat) == len(set(flat))


def test_queries_bound_created_at_for_partition_pruning():
    from app.modules.m10_audit import repo
    from app.modules.m10_audit.schemas import AuditFilters
    from app.modules.m10_audit.service import resolve_window

    window = resolve_window(AuditFilters())
    sql = str(repo.search_stmt(uuid.uuid4(), repo.Criteria(window=window), [("created_at", "desc")], 10, 0))
    assert "audit_logs.created_at >=" in sql
    row_id = uuid.uuid4()
    assert "audit_logs.created_at >=" in str(repo.get_stmt(uuid.uuid4(), row_id))
