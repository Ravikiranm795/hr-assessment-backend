"""M11 report chat: the lean, load-bearing checks (catalog-only prompt, validation, org scoping,
read-only execution, CSV guard, audit content, role and scope rules)."""

import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import settings
from app.core.db import TenantContext
from app.core.errors import AppError
from app.modules.m11_reports import executor, exporters
from app.modules.m11_reports.compiler import compile_spec
from app.modules.m11_reports.filterspec import validate_spec
from tests.m11_reports.conftest import PEOPLE, SPEC, interpretations

pytestmark = pytest.mark.asyncio


def ctx_for(org_id, role="org_admin") -> TenantContext:
    return TenantContext(
        org_id=org_id,
        user_id=None,
        role=role,
        is_platform=org_id is None,
        request_id="t",
        ip=None,
        user_agent=None,
    )


async def interpret(client, headers, **body):
    return await client.post("/api/v1/reports/chat/interpret", json=body, headers=headers)


async def test_prompt_contains_catalog_never_rows(client, world, ai):
    provider = ai(interpretations(SPEC))
    res = await interpret(client, world.h.oa, text="candidates waiting for a decision")
    assert res.status_code == 200, res.text
    prompt = provider.calls[0]["prompt"] + provider.calls[0]["system"]
    assert "candidate_name" in prompt and "invite_status" in prompt  # the catalog is there
    assert PEOPLE.name_a not in prompt and PEOPLE.email_a not in prompt and PEOPLE.name_b not in prompt


async def test_estimated_count_comes_from_db_not_ai(client, world, ai):
    ai({"interpretations": [{"label": "x", "spec": SPEC, "estimated_rows": 9999}]})
    data = (await interpret(client, world.h.oa, text="waiting")).json()
    assert data["interpretations"][0]["estimated_rows"] == 2  # org A has two invites; org B's is not counted


@pytest.mark.parametrize(
    "bad",
    [
        {"source": "candidates", "filters": [{"field": "password_hash", "op": "eq", "value": "x"}]},
        {"source": "candidates", "filters": [{"field": "score_percent", "op": "contains", "value": "x"}]},
        {"source": "candidates", "filters": [{"field": "decision", "op": "eq", "value": "maybe"}]},
        {"source": "users", "filters": []},
        {"source": "candidates", "columns": ["token_hash"]},
    ],
)
async def test_unknown_field_op_or_value_rejected(bad, client, world, ai):
    with pytest.raises(AppError) as exc:
        validate_spec(bad)
    assert exc.value.code == "REPORT_FILTER_INVALID"
    ai(interpretations(bad))  # an AI that invents things gets nowhere
    res = await interpret(client, world.h.oa, text="anything")
    assert res.status_code == 422 and res.json()["error"]["code"] == "REPORT_FILTER_INVALID"


async def test_compiler_forces_org_id_and_parameterizes(world):
    evil = "x'; DROP TABLE invites; --"
    spec = {"source": "candidates", "filters": [{"field": "role_title", "op": "contains", "value": evil}]}
    compiled = compile_spec(validate_spec(spec), world.org_a.id)
    assert "i.org_id = :org_id" in compiled.sql and "i.org_id = :org_id" in compiled.count_sql
    assert compiled.params["org_id"] == world.org_a.id
    assert "DROP TABLE" not in compiled.sql and any(evil in str(v) for v in compiled.params.values())
    rows = await executor.run_readonly(
        ctx_for(world.org_a.id), "SELECT candidate_name FROM invites WHERE org_id = :org_id", compiled.params
    )
    assert PEOPLE.name_b not in {
        r["candidate_name"] for r in rows
    }  # the engine sees only its own org (RLS too)
    other = await executor.run_readonly(ctx_for(world.org_a.id), "SELECT candidate_name FROM invites", {})
    assert PEOPLE.name_b not in {r["candidate_name"] for r in other}


async def test_readonly_role_cannot_write(world):
    for statement in (
        "DELETE FROM invites",
        "UPDATE users SET name = 'x'",
        "INSERT INTO feature_flags (key) VALUES ('z')",
    ):
        with pytest.raises(DBAPIError):
            await executor.run_readonly(ctx_for(world.org_a.id), statement, {})


async def _flow(client, world, ai, fmt="csv"):
    ai(interpretations(SPEC))
    chat = (await interpret(client, world.h.oa, text="waiting candidates")).json()
    rid = chat["report"]["id"]
    sel = await client.post("/api/v1/reports/chat/select", json={"id": rid, "index": 0}, headers=world.h.oa)
    assert (
        sel.status_code == 200
        and sel.json()["report"]["status"] == "confirmed"
        and sel.json()["estimated_rows"] == 2
    )
    count = await client.post(f"/api/v1/reports/{rid}/preview-count", headers=world.h.oa)
    assert count.json()["row_count"] == 2
    gen = await client.post(f"/api/v1/reports/{rid}/generate", json={"format": fmt}, headers=world.h.oa)
    assert gen.status_code == 200, gen.text
    return rid, gen.json()


async def test_csv_cells_starting_with_formula_chars_prefixed(client, world, ai):
    rid, report = await _flow(client, world, ai)
    assert report["row_count"] == 2 and report["status"] == "generated" and report["file_available"]
    res = await client.get(f"/api/v1/reports/{rid}/download", headers=world.h.oa)
    body = res.content.decode("utf-8-sig")
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/csv")
    assert f"'={PEOPLE.name_a}" in body  # formula guard
    assert PEOPLE.email_a in body  # email decrypted at generation time
    assert PEOPLE.name_b not in body
    hist = (await client.get("/api/v1/reports", headers=world.h.oa)).json()
    assert hist["total"] == 1 and hist["items"][0]["id"] == rid


async def test_report_audit_stores_spec_not_rows(client, world, ai, db_session):
    rid, _ = await _flow(client, world, ai, fmt="pdf")
    assert (await client.get(f"/api/v1/reports/{rid}/download", headers=world.h.oa)).content.startswith(
        b"%PDF"
    )
    rows = (
        await db_session.execute(
            text("SELECT action, after FROM audit_logs WHERE entity_id = :rid ORDER BY created_at"),
            {"rid": rid},
        )
    ).all()
    assert [r.action for r in rows] == ["REPORT_INTERPRETED", "REPORT_GENERATED", "REPORT_DOWNLOADED"]
    dumped = json.dumps([r.after for r in rows])
    generated = rows[1].after
    assert generated["filter_spec"]["source"] == "candidates" and generated["row_count"] == 2
    assert generated["user_text"] == "waiting candidates"
    assert PEOPLE.name_a not in dumped and PEOPLE.email_a not in dumped and "Second Candidate" not in dumped


async def test_pdf_header_uses_app_name():
    html = exporters.pdf_html("Report", ["a"], [{"a": "<b>x</b>"}])
    assert f"<h1>{settings.app_name}</h1>" in html and "<b>x</b>" not in html


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/reports/catalog"),
        ("POST", "/api/v1/reports/chat/interpret"),
        ("GET", "/api/v1/reports"),
    ],
)
async def test_hr_forbidden(method, path, client, world):
    res = await client.request(
        method, path, json={"text": "x"} if method == "POST" else None, headers=world.h.hr
    )
    assert res.status_code == 403 and res.json()["error"]["code"] == "FORBIDDEN"
    assert (await client.get("/api/v1/platform/reports/catalog", headers=world.h.oa)).status_code == 403


async def test_sa_reports_exclude_candidate_content(client, world):
    cat = (await client.get("/api/v1/platform/reports/catalog", headers=world.h.sa)).json()
    assert {s["source"] for s in cat["sources"]} == {"organizations", "platform_audit"}
    assert "candidates" not in json.dumps(cat) and "candidate_name" not in json.dumps(cat)
    res = await client.post(
        "/api/v1/platform/reports/chat/interpret", json={"filter_spec": SPEC}, headers=world.h.sa
    )
    assert res.status_code == 422 and res.json()["error"]["code"] == "REPORT_FILTER_INVALID"
    ok = await client.post(
        "/api/v1/platform/reports/chat/interpret",
        json={"filter_spec": {"source": "organizations", "aggregate": {"group_by": "status"}}},
        headers=world.h.sa,
    )
    assert ok.status_code == 200 and ok.json()["interpretations"][0]["estimated_rows"] >= 1
    rid = ok.json()["report"]["id"]
    assert (
        await client.post(
            "/api/v1/platform/reports/chat/select", json={"id": rid, "index": 0}, headers=world.h.sa
        )
    ).status_code == 200
    gen = await client.post(
        f"/api/v1/platform/reports/{rid}/generate", json={"format": "csv"}, headers=world.h.sa
    )
    assert gen.status_code == 200 and gen.json()["source"] == "organizations"
    assert (
        await client.get(f"/api/v1/reports/{rid}/download", headers=world.h.oa)
    ).status_code == 404  # other scope
