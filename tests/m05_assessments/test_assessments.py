"""M5 assessments (lean): golden path, publish table, immutability, version rules, preview, permissions."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.branding_text import watermark
from tests import factories as f
from tests.m05_assessments.helpers import (
    BASE,
    add_question,
    fill,
    gap,
    multi,
    new_assessment,
    publish,
    published_assessment,
    short,
    single,
)


class _Id:
    def __init__(self, value: str):
        self.id = UUID(value)


async def audit_actions(db_session, entity_id: str) -> list[str]:
    rows = await db_session.execute(
        text("SELECT action FROM audit_logs WHERE entity_id = :e ORDER BY created_at"), {"e": entity_id}
    )
    return [r[0] for r in rows]


async def make_published_versions(client, headers, aid: str, count: int) -> list[str]:
    """Version ids v1..vN, all published (each new draft is cloned from the latest published)."""
    ids = []
    draft = (await client.get(f"{BASE}/{aid}", headers=headers)).json()["versions"][0]
    await add_question(client, headers, aid, draft["id"])
    for n in range(count):
        if n:
            resp = await client.post(f"{BASE}/{aid}/versions", headers=headers)
            assert resp.status_code == 201, resp.text
            draft = resp.json()
        assert (await publish(client, headers, aid, draft["id"])).status_code == 200
        ids.append(draft["id"])
    return ids


# --------------------------------------------------------------------------- golden path


async def test_create_assessment_makes_v1_draft(client, world, db_session):
    h = world.h.hr_owner
    resp = await client.post(BASE, json={"title": "Python basics", "description": "x"}, headers=h)
    assert resp.status_code == 201
    body = resp.json()
    assert body["owner_id"] == str(world.hr_owner.id) and body["status"] == "active"
    v1 = body["versions"][0]
    assert (v1["version_no"], v1["status"], v1["question_count"]) == (1, "draft", 0)
    assert (v1["duration_minutes"], v1["pass_mark_percent"]) == (60, 50)
    actions = await audit_actions(db_session, body["id"]) + await audit_actions(db_session, v1["id"])
    assert {"ASSESSMENT_CREATED", "VERSION_CREATED"} <= set(actions)

    aid, vid = body["id"], v1["id"]
    xss = single(prompt='<p onclick="x()">Hi</p><script>alert(1)</script><a href="javascript:alert(1)">l</a>')
    bulk = await client.post(
        f"{BASE}/{aid}/versions/{vid}/questions/bulk",
        json={"questions": [xss, fill(), gap(), short("ai", model_answer="Handshake"), multi(partial=True)]},
        headers=h,
    )
    assert bulk.status_code == 201, bulk.text
    items = bulk.json()["items"]
    assert [i["position"] for i in items] == [1, 2, 3, 4, 5]
    assert "script" not in items[0]["prompt_html"] and "onclick" not in items[0]["prompt_html"]
    assert "javascript:" not in items[0]["prompt_html"]

    # One invalid item rejects the whole batch.
    bad = {"questions": [single(), {"type": "single_choice", "prompt_html": "<p></p>"}]}
    resp = await client.post(f"{BASE}/{aid}/versions/{vid}/questions/bulk", json=bad, headers=h)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "QUESTION_INVALID"

    order = [items[4]["id"], items[3]["id"], items[2]["id"], items[1]["id"], items[0]["id"]]
    resp = await client.put(
        f"{BASE}/{aid}/versions/{vid}/questions/reorder", json={"question_ids": order}, headers=h
    )
    assert [q["id"] for q in resp.json()["items"]] == order
    resp = await client.patch(
        f"{BASE}/{aid}/versions/{vid}",
        json={"duration_minutes": 90, "footer_html": "<p>Policy</p>"},
        headers=h,
    )
    assert resp.json()["duration_minutes"] == 90 and resp.json()["total_marks"] == 11

    resp = await publish(client, h, aid, vid)
    assert resp.status_code == 200 and resp.json()["status"] == "published"
    listed = (await client.get(BASE, params={"linkable": "true", "q": "python"}, headers=world.h.oa)).json()
    assert [i["latest_published_version_id"] for i in listed["items"]] == [vid]
    assert (await client.get(BASE, params={"page_size": 1000}, headers=world.h.oa)).status_code == 422

    dup = await client.post(f"{BASE}/{aid}/duplicate", headers=world.h.hr_other)
    assert dup.status_code == 201 and dup.json()["title"] == "Copy of Python basics"
    assert dup.json()["versions"][0]["question_count"] == 5 and dup.json()["owner_id"] == str(
        world.hr_other.id
    )
    assert {"VERSION_PUBLISHED", "ASSESSMENT_UPDATED"} & set(await audit_actions(db_session, vid))


# --------------------------------------------------------------------------- publish


async def _insert_ai_question_without_model_answer(db_session, version_id: str, org_id) -> None:
    from app.db import Question

    db_session.add(
        Question(version_id=UUID(version_id), org_id=org_id, position=1, type="short_answer",
                 prompt_html="<p>Explain</p>", marks=2, scoring_mode="ai")
    )  # fmt: skip
    await db_session.commit()


TWO = [{"text_html": "a", "is_correct": True}, {"text_html": "b", "is_correct": True}]
PUBLISH_CASES = [
    ("no_questions", None, "QUESTION_INVALID", "no_questions"),
    ("zero_marks", single(marks=0), "QUESTION_INVALID", "marks_not_positive"),
    ("one_option", {**single(), "options": TWO[:1]}, "QUESTION_INVALID", "too_few_options"),
    ("no_correct", {**single(), "options": [{"text_html": "a"}, {"text_html": "b"}]}, "QUESTION_INVALID",
     "no_correct_option"),
    ("single_two_correct", {**single(), "options": TWO}, "QUESTION_INVALID", "multiple_correct_options"),
    ("blank_without_answers", fill([["Paris"], []]), "QUESTION_INVALID", "blank_without_answers"),
    ("gap_without_answers", gap(answer_key={"gaps": [{"accepted": []}]}), "QUESTION_INVALID",
     "blank_without_answers"),
    ("ai_without_model_answer", "direct", "MODEL_ANSWER_REQUIRED", "model_answer_required"),
    ("valid_mixed", [single(), multi(), fill(), gap(), short("manual")], None, None),
]  # fmt: skip


@pytest.mark.parametrize(
    ("name", "payload", "code", "issue"), PUBLISH_CASES, ids=[c[0] for c in PUBLISH_CASES]
)
async def test_publish_validations(client, world, db_session, name, payload, code, issue):
    h = world.h.hr_owner
    made = await new_assessment(client, h, name)
    if payload == "direct":
        await _insert_ai_question_without_model_answer(db_session, made.draft_id, world.org.id)
    else:
        for item in payload if isinstance(payload, list) else [payload] if payload else []:
            await add_question(client, h, made.id, made.draft_id, item)
    resp = await publish(client, h, made.id, made.draft_id)
    if code is None:
        assert resp.status_code == 200 and resp.json()["total_marks"] == 11 and resp.json()["published_at"]
        return
    assert resp.status_code == 422 and resp.json()["error"]["code"] == code
    assert issue in {i["code"] for i in resp.json()["error"]["details"]["issues"]}
    assert (await client.get(f"{BASE}/{made.id}/versions/{made.draft_id}", headers=h)).json()[
        "status"
    ] == "draft"


async def test_published_version_immutable_ASSESSMENT_IMMUTABLE(client, world, db_session):
    h = world.h.hr_owner
    made = await published_assessment(client, h)
    base = f"{BASE}/{made.id}/versions/{made.v1}"
    qid = (await client.get(base, headers=h)).json()["questions"][0]["id"]
    attempts = [
        await client.patch(base, json={"duration_minutes": 30}, headers=h),
        await client.post(f"{base}/questions", json=single(), headers=h),
        await client.patch(f"{base}/questions/{qid}", json={"marks": 9}, headers=h),
        await client.delete(f"{base}/questions/{qid}", headers=h),
        await client.put(f"{base}/questions/reorder", json={"question_ids": [qid]}, headers=h),
        await client.post(f"{base}/questions/bulk", json={"questions": [single()]}, headers=h),
        await publish(client, h, made.id, made.v1),
    ]
    for resp in attempts:
        assert resp.status_code == 409 and resp.json()["error"]["code"] == "ASSESSMENT_IMMUTABLE", resp.text
    # The database trigger is the second layer (spec 5.4): even raw SQL is refused.
    for sql in (
        "UPDATE assessment_versions SET duration_minutes = 10 WHERE id = :v",
        "UPDATE questions SET marks = 9 WHERE version_id = :v",
        "DELETE FROM questions WHERE version_id = :v",
    ):
        with pytest.raises(DBAPIError, match="ASSESSMENT_IMMUTABLE"):
            await db_session.execute(text(sql), {"v": made.v1})
        await db_session.rollback()


# --------------------------------------------------------------------------- version rules


async def test_one_draft_per_assessment_DRAFT_ALREADY_EXISTS(client, world):
    h = world.h.hr_owner
    made = await new_assessment(client, h)
    resp = await client.post(f"{BASE}/{made.id}/versions", headers=h)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "DRAFT_ALREADY_EXISTS"
    await client.patch(f"{BASE}/{made.id}/versions/{made.draft_id}", json={"duration_minutes": 45}, headers=h)
    await add_question(client, h, made.id, made.draft_id, multi(partial=True))
    await publish(client, h, made.id, made.draft_id)
    v2 = (await client.post(f"{BASE}/{made.id}/versions", headers=h)).json()
    assert v2["version_no"] == 2 and v2["duration_minutes"] == 45  # cloned from the latest published
    assert v2["questions"][0]["answer_key"] == {"partial": True} and len(v2["questions"][0]["options"]) == 3
    again = await client.post(f"{BASE}/{made.id}/versions", headers=h)
    assert again.status_code == 409 and again.json()["error"]["code"] == "DRAFT_ALREADY_EXISTS"


async def test_fourth_version_purges_oldest_unprotected(client, world, db_session):
    h = world.h.hr_owner
    made = await new_assessment(client, h)
    v1, v2, v3 = await make_published_versions(client, h, made.id, 3)
    # v1 is linked to an open role, so v2 is the oldest unprotected version.
    await f.make_role(db_session, world.org, world.hr_owner, version=_Id(v1), status="open")
    await db_session.commit()
    resp = await client.post(f"{BASE}/{made.id}/versions", headers=h)
    assert resp.status_code == 201 and resp.json()["version_no"] == 4
    versions = (await client.get(f"{BASE}/{made.id}/versions", headers=h)).json()["items"]
    assert sorted(v["version_no"] for v in versions) == [1, 3, 4]
    left = await db_session.execute(text("SELECT count(*) FROM questions WHERE version_id = :v"), {"v": v2})
    assert left.scalar_one() == 0
    assert "VERSION_PURGED" in await audit_actions(db_session, v2)


async def test_all_three_protected_blocks_creation(client, world, db_session):
    h = world.h.hr_owner
    made = await new_assessment(client, h)
    v1, v2, v3 = await make_published_versions(client, h, made.id, 3)
    await f.make_role(db_session, world.org, world.hr_owner, version=_Id(v1), status="open")  # open role link
    closed = await f.make_role(db_session, world.org, world.hr_owner, version=_Id(v2), status="closed")
    attempt = await f.make_attempt(db_session, await f.make_invite(db_session, world.org, closed, _Id(v2)),
                                   status="submitted")  # undecided attempt  # fmt: skip
    closed3 = await f.make_role(db_session, world.org, world.hr_owner, version=_Id(v3), status="closed")
    await f.make_invite(db_session, world.org, closed3, _Id(v3))  # pending invite
    await db_session.commit()

    resp = await client.post(f"{BASE}/{made.id}/versions", headers=h)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "VERSION_LIMIT_REACHED_ALL_IN_USE"
    detail = (await client.get(f"{BASE}/{made.id}", headers=h)).json()
    assert len(detail["versions"]) == 3 and all(v["in_use"] for v in detail["versions"])

    # Once the attempt is decided its version is released; the attempt keeps its decision.
    await db_session.execute(
        text("UPDATE attempts SET decision = 'approved' WHERE id = :a"), {"a": attempt.id}
    )
    await db_session.commit()
    assert (await client.post(f"{BASE}/{made.id}/versions", headers=h)).status_code == 201
    row = await db_session.execute(
        text("SELECT decision, assessment_version_id FROM attempts WHERE id = :a"), {"a": attempt.id}
    )
    assert tuple(row.one()) == ("approved", None)


async def test_delete_blocked_while_linked_to_open_role(client, world, db_session):
    h = world.h.hr_owner
    made = await published_assessment(client, h)
    role = await f.make_role(db_session, world.org, world.hr_owner, version=_Id(made.v1), status="open")
    await db_session.commit()
    resp = await client.delete(f"{BASE}/{made.id}", headers=h)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "CONFLICT"
    assert resp.json()["error"]["details"]["role_ids"] == [str(role.id)]
    await db_session.execute(text("UPDATE open_roles SET status = 'closed' WHERE id = :r"), {"r": role.id})
    await db_session.commit()
    assert (await client.delete(f"{BASE}/{made.id}", headers=h)).status_code == 204
    assert (await client.get(f"{BASE}/{made.id}", headers=h)).status_code == 404
    assert "ASSESSMENT_DELETED" in await audit_actions(db_session, made.id)


# --------------------------------------------------------------------------- preview


async def test_preview_hides_answer_keys(client, world, db_session):
    h = world.h.hr_owner
    made = await new_assessment(client, h)
    for payload in (
        single(),
        multi(partial=True),
        fill(),
        gap(),
        short("ai", model_answer="SECRET-MODEL-ANSWER"),
    ):
        await add_question(client, h, made.id, made.draft_id, payload)
    await client.patch(
        f"{BASE}/{made.id}/versions/{made.draft_id}",
        json={"instructions_html": "<p>Do it</p>", "footer_html": "<p>Policy text</p>",
              "header_config": {"company_name": "Acme Corp"}},
        headers=h,
    )  # fmt: skip
    url = f"{BASE}/{made.id}/versions/{made.draft_id}/preview"
    resp = await client.get(url, headers=world.h.hr_other)
    assert resp.status_code == 200
    for forbidden in ("is_correct", "answer_key", "model_answer", "scoring_mode", "SECRET-MODEL-ANSWER",
                      "accepted", "partial", "key_points", "Paris", "range"):  # fmt: skip
        assert forbidden not in resp.text, forbidden
    body = resp.json()
    assert body["header"]["company_name"] == "Acme Corp" and body["footer_html"] == "<p>Policy text</p>"
    assert body["watermark"] == watermark() and body["total_marks"] == 11 and "pass_mark_percent" not in body
    assert [q["blank_count"] for q in body["questions"]] == [None, None, 2, 1, None]
    assert set(body["questions"][0]["options"][0]) == {"id", "text_html"}

    await db_session.execute(
        text("INSERT INTO org_feature_flags (org_id, flag_key, enabled) VALUES (:o, 'watermark', false)"),
        {"o": world.org.id},
    )
    await db_session.commit()
    assert (await client.get(url, headers=world.h.hr_other)).json()["watermark"] is None


# --------------------------------------------------------------------------- permissions


async def test_non_owner_cannot_edit(client, world):
    made = await new_assessment(client, world.h.hr_owner)
    resp = await client.patch(f"{BASE}/{made.id}", json={"title": "x"}, headers=world.h.hr_other)
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "FORBIDDEN"
    assert (await client.get(f"{BASE}/{made.id}", headers=world.h.hr_other)).status_code == 200
    assert (
        await client.patch(f"{BASE}/{made.id}", json={"title": "ok"}, headers=world.h.oa)
    ).status_code == 200


# Spec 4.3. Rows: operation -> status per caller; "outsider" = org admin of another org (404).
R = ("sa", "oa", "hr_owner", "hr_other", "outsider")
PERMISSIONS = {
    "list": (200, 200, 200, 200, 200),
    "create": (403, 201, 201, 201, 201),
    "detail": (200, 200, 200, 200, 404),
    "versions": (200, 200, 200, 200, 404),
    "preview": (200, 200, 200, 200, 404),
    "duplicate": (403, 201, 201, 201, 404),
    "patch": (403, 200, 200, 403, 404),
    "archive": (403, 200, 200, 403, 404),
    "version_patch": (403, 200, 200, 403, 404),
    "add_question": (403, 201, 201, 403, 404),
    "bulk": (403, 201, 201, 403, 404),
    "publish": (403, 200, 200, 403, 404),
    "new_version": (403, 201, 201, 403, 404),
    "delete": (403, 204, 204, 403, 404),
}


def _operation(op: str, aid: str, vid: str):
    v = f"{BASE}/{aid}/versions/{vid}"
    return {
        "list": ("GET", BASE, None),
        "create": ("POST", BASE, {"title": "t"}),
        "detail": ("GET", f"{BASE}/{aid}", None),
        "versions": ("GET", f"{BASE}/{aid}/versions", None),
        "preview": ("GET", f"{v}/preview", None),
        "duplicate": ("POST", f"{BASE}/{aid}/duplicate", None),
        "patch": ("PATCH", f"{BASE}/{aid}", {"title": "t2"}),
        "archive": ("POST", f"{BASE}/{aid}/archive", None),
        "version_patch": ("PATCH", v, {"duration_minutes": 30}),
        "add_question": ("POST", f"{v}/questions", single()),
        "bulk": ("POST", f"{v}/questions/bulk", {"questions": [single()]}),
        "publish": ("POST", f"{v}/publish", None),
        "new_version": ("POST", f"{BASE}/{aid}/versions", None),
        "delete": ("DELETE", f"{BASE}/{aid}", None),
    }[op]


@pytest.mark.parametrize(("op", "role"), [(op, r) for op in PERMISSIONS for r in R])
async def test_permission_matrix(client, world, op, role):
    made = await new_assessment(client, world.h.hr_owner)
    await add_question(client, world.h.hr_owner, made.id, made.draft_id)
    if op == "new_version":
        assert (await publish(client, world.h.hr_owner, made.id, made.draft_id)).status_code == 200
    method, path, body = _operation(op, made.id, made.draft_id)
    params = {"org_id": str(world.org.id)} if role == "sa" else None
    resp = await client.request(method, path, json=body, params=params, headers=getattr(world.h, role))
    assert resp.status_code == PERMISSIONS[op][R.index(role)], (op, role, resp.text)


async def test_flag_off_blocks_and_openapi_declares_guards(client, world, db_session):
    from app.main import create_app

    await db_session.execute(
        text("INSERT INTO org_feature_flags (org_id, flag_key, enabled) VALUES (:o, 'assessments', false)"),
        {"o": world.org.id},
    )
    await db_session.commit()
    resp = await client.get(BASE, headers=world.h.oa)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FEATURE_DISABLED" and resp.json()["error"]["details"] == {
        "flag": "assessments"
    }
    schema = create_app().openapi()
    ops = [
        op
        for path, item in schema["paths"].items()
        if path.startswith(BASE) and not path.startswith(f"{BASE}/ai/")  # M14 routes live under /ai
        for op in item.values()
    ]
    assert len(ops) == 19
    for op in ops:
        assert (
            op["x-required-flag"] == "assessments" and op["x-roles"] and op["summary"] and op["description"]
        )


# --------------------------------------------------------------------------- service interface


async def test_get_published_version_interface(client, world):
    from app.core.db import TenantContext, tenant_session
    from app.core.errors import AppError
    from app.modules.m05_assessments.service import get_published_version

    made = await new_assessment(client, world.h.hr_owner)
    for payload in (single(), multi(partial=True)):
        await add_question(client, world.h.hr_owner, made.id, made.draft_id, payload)
    draft_id = UUID(made.draft_id)
    ctx = TenantContext(world.org.id, world.oa.id, "org_admin", False, "t", None, None)
    async with tenant_session(ctx) as session:
        for org, vid, code in (
            (world.org.id, draft_id, "ASSESSMENT_NOT_PUBLISHED"),
            (world.org.id, uuid4(), "NOT_FOUND"),
        ):
            with pytest.raises(AppError) as err:
                await get_published_version(session, org, vid)
            assert err.value.code == code
    assert (await publish(client, world.h.hr_owner, made.id, made.draft_id)).status_code == 200
    async with tenant_session(ctx) as session:
        version = await get_published_version(session, world.org.id, draft_id)
        first, second = version.questions
        assert version.version_no == 1 and sum(o.is_correct for o in first.options) == 1
        assert second.answer_key == {"partial": True}
        with pytest.raises(AppError) as err:
            await get_published_version(session, world.other_org.id, draft_id)
        assert err.value.code == "NOT_FOUND"
