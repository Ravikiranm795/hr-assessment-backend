"""M5 assessments: lifecycle, publish validations, version rules, preview."""

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


async def audit_actions(db_session, entity_id: str | None = None) -> list[str]:
    sql = "SELECT action FROM audit_logs" + (" WHERE entity_id = :e" if entity_id else "") + " ORDER BY created_at"
    rows = await db_session.execute(text(sql), {"e": entity_id} if entity_id else {})
    return [r[0] for r in rows]


async def make_published_versions(client, headers, aid: str, count: int) -> list[str]:
    """Version ids v1..vN, all published (each new draft is cloned from the latest published)."""
    ids = []
    created = await client.get(f"{BASE}/{aid}", headers=headers)
    draft = next(v for v in created.json()["versions"] if v["status"] == "draft")
    await add_question(client, headers, aid, draft["id"])
    for n in range(count):
        if n:
            resp = await client.post(f"{BASE}/{aid}/versions", headers=headers)
            assert resp.status_code == 201, resp.text
            draft = resp.json()
        resp = await publish(client, headers, aid, draft["id"])
        assert resp.status_code == 200, resp.text
        ids.append(draft["id"])
    return ids


# --------------------------------------------------------------------------- creation


async def test_create_assessment_makes_v1_draft(client, world, db_session):
    resp = await client.post(BASE, json={"title": "Python basics", "description": "x"}, headers=world.h.hr_owner)
    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "Python basics"
    assert body["owner_id"] == str(world.hr_owner.id)
    assert body["status"] == "active"
    assert len(body["versions"]) == 1
    v1 = body["versions"][0]
    assert v1["version_no"] == 1 and v1["status"] == "draft" and v1["question_count"] == 0
    assert v1["duration_minutes"] == 60 and v1["pass_mark_percent"] == 50

    actions = await audit_actions(db_session, body["id"]) + await audit_actions(db_session, v1["id"])
    assert "ASSESSMENT_CREATED" in actions and "VERSION_CREATED" in actions


async def test_create_validates_title(client, world):
    resp = await client.post(BASE, json={"title": "  "}, headers=world.h.oa)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_list_filters_search_mine_linkable_and_pagination(client, world):
    mine = await published_assessment(client, world.h.hr_owner, "Alpha java")
    await new_assessment(client, world.h.hr_other, "Beta draft only")
    arch = await new_assessment(client, world.h.hr_owner, "Gamma archived")
    assert (await client.post(f"{BASE}/{arch.id}/archive", headers=world.h.hr_owner)).status_code == 200

    everything = (await client.get(BASE, headers=world.h.oa)).json()
    assert everything["total"] == 3 and {"items", "page", "page_size"} <= set(everything)
    assert [i["title"] for i in (await client.get(BASE, params={"q": "java"}, headers=world.h.oa)).json()["items"]] == [
        "Alpha java"
    ]
    assert (await client.get(BASE, params={"status": "archived"}, headers=world.h.oa)).json()["total"] == 1
    assert (await client.get(BASE, params={"mine": "true"}, headers=world.h.hr_other)).json()["total"] == 1
    linkable = (await client.get(BASE, params={"linkable": "true"}, headers=world.h.oa)).json()
    assert [i["id"] for i in linkable["items"]] == [mine.id]
    assert linkable["items"][0]["latest_published_version_id"] == mine.v1
    assert (await client.get(BASE, params={"page_size": 1000}, headers=world.h.oa)).status_code == 422
    page = (await client.get(BASE, params={"page_size": 1, "sort": "title:asc"}, headers=world.h.oa)).json()
    assert page["total"] == 3 and page["items"][0]["title"] == "Alpha java"


async def test_cross_tenant_ids_are_404(client, world):
    made = await published_assessment(client, world.h.hr_owner)
    for method, path in [
        ("GET", f"{BASE}/{made.id}"),
        ("PATCH", f"{BASE}/{made.id}"),
        ("DELETE", f"{BASE}/{made.id}"),
        ("GET", f"{BASE}/{made.id}/versions"),
        ("GET", f"{BASE}/{made.id}/versions/{made.v1}"),
        ("GET", f"{BASE}/{made.id}/versions/{made.v1}/preview"),
        ("POST", f"{BASE}/{made.id}/versions"),
        ("POST", f"{BASE}/{made.id}/duplicate"),
        ("POST", f"{BASE}/{made.id}/archive"),
        ("POST", f"{BASE}/{made.id}/versions/{made.v1}/publish"),
    ]:
        resp = await client.request(method, path, json={"title": "x"} if method == "PATCH" else None,
                                    headers=world.h.outsider)
        assert resp.status_code == 404, (method, path, resp.text)
        assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert (await client.get(BASE, headers=world.h.outsider)).json()["total"] == 0
    assert (await client.get(f"{BASE}/{uuid4()}", headers=world.h.oa)).status_code == 404


async def test_patch_archive_unarchive_and_audit(client, world, db_session):
    made = await new_assessment(client, world.h.hr_owner)
    resp = await client.patch(f"{BASE}/{made.id}", json={"title": "Renamed"}, headers=world.h.hr_owner)
    assert resp.status_code == 200 and resp.json()["title"] == "Renamed"
    assert (await client.post(f"{BASE}/{made.id}/archive", headers=world.h.oa)).json()["status"] == "archived"
    assert (await client.post(f"{BASE}/{made.id}/unarchive", headers=world.h.oa)).json()["status"] == "active"
    actions = await audit_actions(db_session, made.id)
    assert actions.count("ASSESSMENT_UPDATED") >= 2 and "ASSESSMENT_ARCHIVED" in actions


async def test_duplicate_copies_latest_version_into_new_assessment(client, world, db_session):
    made = await new_assessment(client, world.h.hr_owner, "Source")
    await add_question(client, world.h.hr_owner, made.id, made.draft_id, multi(partial=True))
    resp = await client.post(f"{BASE}/{made.id}/duplicate", headers=world.h.hr_other)
    assert resp.status_code == 201, resp.text
    copy = resp.json()
    assert copy["id"] != made.id and copy["title"] == "Copy of Source"
    assert copy["owner_id"] == str(world.hr_other.id)
    version = copy["versions"][0]
    assert version["version_no"] == 1 and version["status"] == "draft" and version["question_count"] == 1
    full = (await client.get(f"{BASE}/{copy['id']}/versions/{version['id']}", headers=world.h.hr_other)).json()
    assert full["questions"][0]["answer_key"] == {"partial": True}
    assert len(full["questions"][0]["options"]) == 3
    assert "ASSESSMENT_DUPLICATED" in await audit_actions(db_session, copy["id"])


# --------------------------------------------------------------------------- questions


async def test_question_crud_reorder_and_totals(client, world, db_session):
    made = await new_assessment(client, world.h.hr_owner)
    q1 = await add_question(client, world.h.hr_owner, made.id, made.draft_id, single(marks=1))
    q2 = await add_question(client, world.h.hr_owner, made.id, made.draft_id, fill(marks=2.5))
    assert (q1["position"], q2["position"]) == (1, 2)
    qbase = f"{BASE}/{made.id}/versions/{made.draft_id}/questions"

    resp = await client.put(f"{qbase}/reorder", json={"question_ids": [q2["id"], q1["id"]]}, headers=world.h.hr_owner)
    assert resp.status_code == 200
    assert [q["id"] for q in resp.json()["items"]] == [q2["id"], q1["id"]]
    bad = await client.put(f"{qbase}/reorder", json={"question_ids": [q1["id"]]}, headers=world.h.hr_owner)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "VALIDATION_ERROR"

    resp = await client.patch(
        f"{qbase}/{q1['id']}",
        json={"marks": 4, "options": [{"text_html": "A", "is_correct": True}, {"text_html": "B"}]},
        headers=world.h.hr_owner,
    )
    assert resp.status_code == 200
    assert resp.json()["marks"] == 4 and [o["text_html"] for o in resp.json()["options"]] == ["A", "B"]

    version = (await client.get(f"{BASE}/{made.id}/versions/{made.draft_id}", headers=world.h.oa)).json()
    assert version["total_marks"] == 6.5 and version["question_count"] == 2
    assert [q["id"] for q in version["questions"]] == [q2["id"], q1["id"]]

    assert (await client.delete(f"{qbase}/{q2['id']}", headers=world.h.hr_owner)).status_code == 204
    version = (await client.get(f"{BASE}/{made.id}/versions/{made.draft_id}", headers=world.h.oa)).json()
    assert version["total_marks"] == 4 and version["questions"][0]["position"] == 1
    assert (await client.delete(f"{qbase}/{q2['id']}", headers=world.h.hr_owner)).status_code == 404
    actions = await audit_actions(db_session)
    for name in ("QUESTION_ADDED", "QUESTION_UPDATED", "QUESTION_DELETED", "QUESTIONS_REORDERED"):
        assert name in actions


async def test_bulk_add_is_atomic_and_sanitizes(client, world):
    made = await new_assessment(client, world.h.hr_owner)
    url = f"{BASE}/{made.id}/versions/{made.draft_id}/questions/bulk"
    xss = single(prompt='<p onclick="x()">Hi</p><script>alert(1)</script><a href="javascript:alert(1)">l</a>')
    resp = await client.post(url, json={"questions": [xss, fill(), gap(), short("ai", model_answer="Handshake")]},
                             headers=world.h.hr_owner)
    assert resp.status_code == 201, resp.text
    items = resp.json()["items"]
    assert [i["position"] for i in items] == [1, 2, 3, 4]
    assert "script" not in items[0]["prompt_html"] and "onclick" not in items[0]["prompt_html"]
    assert "javascript:" not in items[0]["prompt_html"]
    assert "<p>Hi</p>" in items[0]["prompt_html"]

    broken = {"questions": [single(), {"type": "single_choice", "prompt_html": "<p></p>", "options": []}]}
    resp = await client.post(url, json=broken, headers=world.h.hr_owner)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "QUESTION_INVALID"
    version = (await client.get(f"{BASE}/{made.id}/versions/{made.draft_id}", headers=world.h.oa)).json()
    assert version["question_count"] == 4  # nothing from the failed batch was kept


@pytest.mark.parametrize(
    ("payload", "status", "code"),
    [
        (short("ai"), 422, "MODEL_ANSWER_REQUIRED"),
        (gap("ai", answer_key={"gaps": []}), 422, "MODEL_ANSWER_REQUIRED"),
        (single(scoring_mode="ai", model_answer="x"), 422, "QUESTION_INVALID"),
        (short("auto"), 422, "QUESTION_INVALID"),
        ({**fill(), "options": [{"text_html": "x"}]}, 422, "QUESTION_INVALID"),
        ({**single(), "answer_key": {"blanks": []}}, 422, "QUESTION_INVALID"),
        ({**single(), "type": "essay"}, 422, "VALIDATION_ERROR"),
        ({**single(), "prompt_html": "<script>x</script>"}, 422, "QUESTION_INVALID"),
        (short("ai", model_answer="  "), 422, "MODEL_ANSWER_REQUIRED"),
        (short("ai", model_answer="Syn, syn-ack, ack"), 201, None),
        (short("ai", answer_key={"key_points": ["handshake"]}), 201, None),
        (gap("ai", model_answer="range"), 201, None),
    ],
)
async def test_question_save_rules(client, world, payload, status, code):
    made = await new_assessment(client, world.h.hr_owner)
    resp = await client.post(
        f"{BASE}/{made.id}/versions/{made.draft_id}/questions", json=payload, headers=world.h.hr_owner
    )
    assert resp.status_code == status, resp.text
    if code:
        assert resp.json()["error"]["code"] == code


async def test_question_in_other_version_or_assessment_is_404(client, world):
    a = await new_assessment(client, world.h.hr_owner)
    b = await new_assessment(client, world.h.hr_owner)
    q = await add_question(client, world.h.hr_owner, a.id, a.draft_id)
    url = f"{BASE}/{b.id}/versions/{b.draft_id}/questions/{q['id']}"
    assert (await client.patch(url, json={"marks": 3}, headers=world.h.hr_owner)).status_code == 404
    assert (await client.delete(url, headers=world.h.hr_owner)).status_code == 404
    wrong = f"{BASE}/{b.id}/versions/{a.draft_id}/questions"
    assert (await client.post(wrong, json=single(), headers=world.h.hr_owner)).status_code == 404


# --------------------------------------------------------------------------- publish


async def _insert_ai_question_without_model_answer(db_session, version_id: str, org_id) -> None:
    from app.db import Question

    db_session.add(
        Question(
            version_id=UUID(version_id),
            org_id=org_id,
            position=1,
            type="short_answer",
            prompt_html="<p>Explain</p>",
            marks=2,
            scoring_mode="ai",
        )
    )
    await db_session.commit()


@pytest.mark.parametrize(
    ("name", "payload", "status", "code", "issue"),
    [
        ("no_questions", None, 422, "QUESTION_INVALID", "no_questions"),
        ("zero_marks", single(marks=0), 422, "QUESTION_INVALID", "marks_not_positive"),
        (
            "one_option",
            {**single(), "options": [{"text_html": "only", "is_correct": True}]},
            422,
            "QUESTION_INVALID",
            "too_few_options",
        ),
        (
            "no_correct",
            {**single(), "options": [{"text_html": "a"}, {"text_html": "b"}]},
            422,
            "QUESTION_INVALID",
            "no_correct_option",
        ),
        (
            "single_with_two_correct",
            {**single(), "options": [{"text_html": "a", "is_correct": True}, {"text_html": "b", "is_correct": True}]},
            422,
            "QUESTION_INVALID",
            "multiple_correct_options",
        ),
        (
            "multi_without_correct",
            {**multi(), "options": [{"text_html": "a"}, {"text_html": "b"}]},
            422,
            "QUESTION_INVALID",
            "no_correct_option",
        ),
        ("blank_without_answers", fill([["Paris"], []]), 422, "QUESTION_INVALID", "blank_without_answers"),
        ("fill_without_blanks", fill([]), 422, "QUESTION_INVALID", "no_blanks"),
        (
            "gap_without_answers",
            gap(answer_key={"gaps": [{"accepted": []}]}),
            422,
            "QUESTION_INVALID",
            "blank_without_answers",
        ),
        ("ai_without_model_answer", "direct", 422, "MODEL_ANSWER_REQUIRED", "model_answer_required"),
        ("valid_mixed", [single(), multi(), fill(), gap(), short("manual")], 200, None, None),
    ],
)
async def test_publish_validations(client, world, db_session, name, payload, status, code, issue):
    made = await new_assessment(client, world.h.hr_owner, name)
    if payload == "direct":
        await _insert_ai_question_without_model_answer(db_session, made.draft_id, world.org.id)
    elif isinstance(payload, list):
        for item in payload:
            await add_question(client, world.h.hr_owner, made.id, made.draft_id, item)
    elif payload is not None:
        await add_question(client, world.h.hr_owner, made.id, made.draft_id, payload)
    resp = await publish(client, world.h.hr_owner, made.id, made.draft_id)
    assert resp.status_code == status, resp.text
    if code is None:
        body = resp.json()
        assert body["status"] == "published" and body["published_at"] and body["total_marks"] == 11
        return
    error = resp.json()["error"]
    assert error["code"] == code
    assert issue in {i["code"] for i in error["details"]["issues"]}
    after = (await client.get(f"{BASE}/{made.id}/versions/{made.draft_id}", headers=world.h.oa)).json()
    assert after["status"] == "draft"


async def test_publish_validates_duration_and_pass_mark(client, world, db_session):
    made = await new_assessment(client, world.h.hr_owner)
    await add_question(client, world.h.hr_owner, made.id, made.draft_id)
    await db_session.execute(
        text("UPDATE assessment_versions SET duration_minutes = 1 WHERE id = :v"), {"v": made.draft_id}
    )
    await db_session.commit()
    resp = await publish(client, world.h.hr_owner, made.id, made.draft_id)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert {"field": "duration_minutes", "code": "out_of_range"} in resp.json()["error"]["details"]["fields"]


async def test_version_patch_draft_only_and_ranges(client, world):
    made = await new_assessment(client, world.h.hr_owner)
    url = f"{BASE}/{made.id}/versions/{made.draft_id}"
    body = {
        "duration_minutes": 90,
        "pass_mark_percent": 65.5,
        "instructions_html": "<p>Read <b>carefully</b></p><iframe src='x'></iframe>",
        "footer_html": "<p>Policy</p>",
        "header_config": {"company_name": "Acme"},
        "proctoring_config": {"snapshot_interval_seconds": 30},
        "shuffle_questions": True,
        "shuffle_options": True,
    }
    resp = await client.patch(url, json=body, headers=world.h.hr_owner)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["duration_minutes"] == 90 and out["pass_mark_percent"] == 65.5
    assert "iframe" not in out["instructions_html"] and out["shuffle_options"] is True
    assert out["header_config"]["company_name"] == "Acme"
    for bad in ({"duration_minutes": 4}, {"duration_minutes": 481}, {"pass_mark_percent": 101}, {"pass_mark_percent": -1},
                {"proctoring_config": {"snapshot_interval_seconds": 1}}, {"header_config": {"logo_asset_id": str(uuid4())}}):
        resp = await client.patch(url, json=bad, headers=world.h.hr_owner)
        assert resp.status_code in (404, 422), (bad, resp.text)
    foreign = await client.patch(url, json={"header_config": {"logo_asset_id": str(uuid4())}}, headers=world.h.hr_owner)
    assert foreign.status_code == 404  # asset ids from outside the org are not found


async def test_published_version_immutable_ASSESSMENT_IMMUTABLE(client, world, db_session):
    made = await published_assessment(client, world.h.hr_owner)
    base = f"{BASE}/{made.id}/versions/{made.v1}"
    full = (await client.get(base, headers=world.h.hr_owner)).json()
    qid = full["questions"][0]["id"]
    attempts = [
        await client.patch(base, json={"duration_minutes": 30}, headers=world.h.hr_owner),
        await client.post(f"{base}/questions", json=single(), headers=world.h.hr_owner),
        await client.patch(f"{base}/questions/{qid}", json={"marks": 9}, headers=world.h.hr_owner),
        await client.delete(f"{base}/questions/{qid}", headers=world.h.hr_owner),
        await client.put(f"{base}/questions/reorder", json={"question_ids": [qid]}, headers=world.h.hr_owner),
        await client.post(f"{base}/questions/bulk", json={"questions": [single()]}, headers=world.h.hr_owner),
        await publish(client, world.h.hr_owner, made.id, made.v1),
    ]
    for resp in attempts:
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "ASSESSMENT_IMMUTABLE"

    # The database trigger is the second layer (spec 5.4): even raw SQL is refused.
    for sql in (
        "UPDATE assessment_versions SET duration_minutes = 10 WHERE id = :v",
        "UPDATE questions SET marks = 9 WHERE version_id = :v",
        "DELETE FROM questions WHERE version_id = :v",
        "INSERT INTO questions (id, version_id, org_id, position, type, prompt_html)"
        " SELECT gen_random_uuid(), id, org_id, 9, 'short_answer', 'x' FROM assessment_versions WHERE id = :v",
    ):
        with pytest.raises(DBAPIError, match="ASSESSMENT_IMMUTABLE"):
            await db_session.execute(text(sql), {"v": made.v1})
        await db_session.rollback()


# --------------------------------------------------------------------------- version rules


async def test_one_draft_per_assessment_DRAFT_ALREADY_EXISTS(client, world):
    made = await new_assessment(client, world.h.hr_owner)
    resp = await client.post(f"{BASE}/{made.id}/versions", headers=world.h.hr_owner)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "DRAFT_ALREADY_EXISTS"
    await add_question(client, world.h.hr_owner, made.id, made.draft_id)
    await publish(client, world.h.hr_owner, made.id, made.draft_id)
    resp = await client.post(f"{BASE}/{made.id}/versions", headers=world.h.hr_owner)
    assert resp.status_code == 201
    v2 = resp.json()
    assert v2["version_no"] == 2 and v2["status"] == "draft" and len(v2["questions"]) == 1
    again = await client.post(f"{BASE}/{made.id}/versions", headers=world.h.hr_owner)
    assert again.status_code == 409 and again.json()["error"]["code"] == "DRAFT_ALREADY_EXISTS"


async def test_new_version_is_cloned_from_latest_published(client, world):
    made = await new_assessment(client, world.h.hr_owner)
    await client.patch(
        f"{BASE}/{made.id}/versions/{made.draft_id}",
        json={"duration_minutes": 45, "footer_html": "<p>f</p>"},
        headers=world.h.hr_owner,
    )
    await add_question(client, world.h.hr_owner, made.id, made.draft_id, multi(partial=True))
    await publish(client, world.h.hr_owner, made.id, made.draft_id)
    v2 = (await client.post(f"{BASE}/{made.id}/versions", headers=world.h.hr_owner)).json()
    assert v2["duration_minutes"] == 45 and v2["footer_html"] == "<p>f</p>"
    q = v2["questions"][0]
    assert q["answer_key"] == {"partial": True} and len(q["options"]) == 3
    old = (await client.get(f"{BASE}/{made.id}/versions/{made.draft_id}", headers=world.h.hr_owner)).json()
    assert q["id"] != old["questions"][0]["id"]  # copies, never shared rows
    # The draft is editable and the published original is untouched.
    r = await client.post(f"{BASE}/{made.id}/versions/{v2['id']}/questions", json=single(), headers=world.h.hr_owner)
    assert r.status_code == 201
    old = (await client.get(f"{BASE}/{made.id}/versions/{made.draft_id}", headers=world.h.hr_owner)).json()
    assert len(old["questions"]) == 1


@pytest.mark.parametrize("protecting_role_status", ["open", "draft"])
async def test_fourth_version_purges_oldest_unprotected(client, world, db_session, protecting_role_status):
    made = await new_assessment(client, world.h.hr_owner)
    v1, v2, v3 = await make_published_versions(client, world.h.hr_owner, made.id, 3)
    # v1 is linked to a non-closed role, so v2 is the oldest unprotected version.
    await f.make_role(db_session, world.org, world.hr_owner, version=_Id(v1), status=protecting_role_status)
    await db_session.commit()

    resp = await client.post(f"{BASE}/{made.id}/versions", headers=world.h.hr_owner)
    assert resp.status_code == 201, resp.text
    assert resp.json()["version_no"] == 4
    versions = (await client.get(f"{BASE}/{made.id}/versions", headers=world.h.hr_owner)).json()["items"]
    assert sorted(v["version_no"] for v in versions) == [1, 3, 4]
    left = await db_session.execute(text("SELECT count(*) FROM questions WHERE version_id = :v"), {"v": v2})
    assert left.scalar_one() == 0  # questions of the purged version went with it
    assert "VERSION_PURGED" in await audit_actions(db_session, v2)


async def test_closed_role_does_not_protect_a_version(client, world, db_session):
    made = await new_assessment(client, world.h.hr_owner)
    v1, v2, v3 = await make_published_versions(client, world.h.hr_owner, made.id, 3)
    await f.make_role(db_session, world.org, world.hr_owner, version=_Id(v1), status="closed")
    await db_session.commit()
    assert (await client.post(f"{BASE}/{made.id}/versions", headers=world.h.hr_owner)).status_code == 201
    versions = (await client.get(f"{BASE}/{made.id}/versions", headers=world.h.hr_owner)).json()["items"]
    assert sorted(v["version_no"] for v in versions) == [2, 3, 4]


class _Id:
    def __init__(self, value: str):
        self.id = UUID(value)


async def _protect_all_three(client, world, db_session):
    made = await new_assessment(client, world.h.hr_owner)
    v1, v2, v3 = await make_published_versions(client, world.h.hr_owner, made.id, 3)
    await f.make_role(db_session, world.org, world.hr_owner, version=_Id(v1), status="open")  # open role link
    closed = await f.make_role(db_session, world.org, world.hr_owner, version=_Id(v2), status="closed")
    undecided = await f.make_invite(db_session, world.org, closed, _Id(v2))
    attempt = await f.make_attempt(db_session, undecided, status="submitted")  # submitted, decision waiting
    closed3 = await f.make_role(db_session, world.org, world.hr_owner, version=_Id(v3), status="closed")
    await f.make_invite(db_session, world.org, closed3, _Id(v3))  # still-pending invite
    await db_session.commit()
    return made, (v1, v2, v3), attempt


async def test_all_three_protected_blocks_creation(client, world, db_session):
    made, _, _ = await _protect_all_three(client, world, db_session)
    resp = await client.post(f"{BASE}/{made.id}/versions", headers=world.h.hr_owner)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "VERSION_LIMIT_REACHED_ALL_IN_USE"
    versions = (await client.get(f"{BASE}/{made.id}/versions", headers=world.h.hr_owner)).json()["items"]
    assert len(versions) == 3
    detail = (await client.get(f"{BASE}/{made.id}", headers=world.h.hr_owner)).json()
    assert all(v["in_use"] for v in detail["versions"])


async def test_decided_attempt_releases_its_version(client, world, db_session):
    made, (_, v2, _), attempt = await _protect_all_three(client, world, db_session)
    await db_session.execute(text("UPDATE attempts SET decision = 'approved' WHERE id = :a"), {"a": attempt.id})
    await db_session.commit()
    resp = await client.post(f"{BASE}/{made.id}/versions", headers=world.h.hr_owner)
    assert resp.status_code == 201, resp.text
    versions = (await client.get(f"{BASE}/{made.id}/versions", headers=world.h.hr_owner)).json()["items"]
    assert sorted(v["version_no"] for v in versions) == [1, 3, 4]
    # The attempt keeps its score and decision after the version is gone.
    row = (await db_session.execute(text("SELECT decision, assessment_version_id FROM attempts WHERE id = :a"),
                                    {"a": attempt.id})).one()
    assert row[0] == "approved" and row[1] is None


# --------------------------------------------------------------------------- delete


async def test_delete_blocked_while_linked_to_open_role(client, world, db_session):
    made = await published_assessment(client, world.h.hr_owner)
    role = await f.make_role(db_session, world.org, world.hr_owner, version=_Id(made.v1), status="open")
    await db_session.commit()
    resp = await client.delete(f"{BASE}/{made.id}", headers=world.h.hr_owner)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "CONFLICT"
    assert resp.json()["error"]["details"]["role_ids"] == [str(role.id)]
    assert (await client.get(f"{BASE}/{made.id}", headers=world.h.hr_owner)).status_code == 200

    await db_session.execute(text("UPDATE open_roles SET status = 'closed' WHERE id = :r"), {"r": role.id})
    await db_session.commit()
    assert (await client.delete(f"{BASE}/{made.id}", headers=world.h.hr_owner)).status_code == 204
    assert (await client.get(f"{BASE}/{made.id}", headers=world.h.hr_owner)).status_code == 404
    assert (await client.get(BASE, headers=world.h.oa)).json()["total"] == 0
    assert "ASSESSMENT_DELETED" in await audit_actions(db_session, made.id)
    kept = await db_session.execute(text("SELECT deleted_at FROM assessments WHERE id = :i"), {"i": made.id})
    assert kept.scalar_one() is not None  # soft delete


# --------------------------------------------------------------------------- preview


async def test_preview_hides_answer_keys(client, world, db_session):
    made = await new_assessment(client, world.h.hr_owner)
    for payload in (single(), multi(partial=True), fill(), gap(), short("ai", model_answer="SECRET-MODEL-ANSWER")):
        await add_question(client, world.h.hr_owner, made.id, made.draft_id, payload)
    await client.patch(
        f"{BASE}/{made.id}/versions/{made.draft_id}",
        json={"instructions_html": "<p>Do it</p>", "footer_html": "<p>Policy text</p>",
              "header_config": {"company_name": "Acme Corp"}, "shuffle_options": True},
        headers=world.h.hr_owner,
    )
    url = f"{BASE}/{made.id}/versions/{made.draft_id}/preview"
    resp = await client.get(url, headers=world.h.hr_other)  # any org user may preview
    assert resp.status_code == 200
    body = resp.json()
    raw = resp.text
    for forbidden in ("is_correct", "answer_key", "model_answer", "scoring_mode", "SECRET-MODEL-ANSWER",
                      "accepted", "partial", "key_points", "Paris", "range"):
        assert forbidden not in raw, forbidden
    assert body["header"]["company_name"] == "Acme Corp"
    assert body["footer_html"] == "<p>Policy text</p>" and body["instructions_html"] == "<p>Do it</p>"
    assert body["watermark"] == watermark()
    assert body["shuffle_options"] is True and body["duration_minutes"] == 60
    types = [q["type"] for q in body["questions"]]
    assert types == ["single_choice", "multi_choice", "fill_blank", "code_gap", "short_answer"]
    first = body["questions"][0]
    assert set(first) == {"id", "position", "type", "prompt_html", "marks", "options", "blank_count",
                          "code_language"}
    assert set(first["options"][0]) == {"id", "text_html"}
    assert body["questions"][2]["blank_count"] == 2 and body["questions"][3]["blank_count"] == 1
    assert body["total_marks"] == 11 and "pass_mark_percent" not in body

    # Watermark is a flag the SA controls: off for this org means no watermark in the preview.
    await db_session.execute(
        text("INSERT INTO org_feature_flags (org_id, flag_key, enabled) VALUES (:o, 'watermark', false)"),
        {"o": world.org.id},
    )
    await db_session.commit()
    assert (await client.get(url, headers=world.h.hr_other)).json()["watermark"] is None


async def test_preview_works_for_published_version_and_sa_needs_org(client, world):
    made = await published_assessment(client, world.h.hr_owner)
    url = f"{BASE}/{made.id}/versions/{made.v1}/preview"
    assert (await client.get(url, headers=world.h.oa)).status_code == 200
    sa = await client.get(url, headers=world.h.sa)
    assert sa.status_code == 422 and sa.json()["error"]["code"] == "VALIDATION_ERROR"
    sa = await client.get(url, params={"org_id": str(world.org.id)}, headers=world.h.sa)
    assert sa.status_code == 200


# --------------------------------------------------------------------------- permissions and flags


async def test_non_owner_cannot_edit(client, world):
    made = await new_assessment(client, world.h.hr_owner)
    q = await add_question(client, world.h.hr_owner, made.id, made.draft_id)
    base = f"{BASE}/{made.id}"
    denied = [
        await client.patch(base, json={"title": "x"}, headers=world.h.hr_other),
        await client.delete(base, headers=world.h.hr_other),
        await client.post(f"{base}/archive", headers=world.h.hr_other),
        await client.patch(f"{base}/versions/{made.draft_id}", json={"duration_minutes": 20}, headers=world.h.hr_other),
        await client.post(f"{base}/versions/{made.draft_id}/questions", json=single(), headers=world.h.hr_other),
        await client.delete(f"{base}/versions/{made.draft_id}/questions/{q['id']}", headers=world.h.hr_other),
        await publish(client, world.h.hr_other, made.id, made.draft_id),
    ]
    for resp in denied:
        assert resp.status_code == 403 and resp.json()["error"]["code"] == "FORBIDDEN", resp.text
    # Read access stays open to other HR users, and the OA may edit anything.
    assert (await client.get(base, headers=world.h.hr_other)).status_code == 200
    assert (await client.patch(base, json={"title": "ok"}, headers=world.h.oa)).status_code == 200


# role -> expected status for each operation, from spec 4.3 (SA read-only, HR other read-only on edits).
PERMISSION_TABLE = {
    "list": {"sa": 200, "oa": 200, "hr_owner": 200, "hr_other": 200},
    "create": {"sa": 403, "oa": 201, "hr_owner": 201, "hr_other": 201},
    "detail": {"sa": 200, "oa": 200, "hr_owner": 200, "hr_other": 200},
    "versions": {"sa": 200, "oa": 200, "hr_owner": 200, "hr_other": 200},
    "preview": {"sa": 200, "oa": 200, "hr_owner": 200, "hr_other": 200},
    "duplicate": {"sa": 403, "oa": 201, "hr_owner": 201, "hr_other": 201},
    "patch": {"sa": 403, "oa": 200, "hr_owner": 200, "hr_other": 403},
    "archive": {"sa": 403, "oa": 200, "hr_owner": 200, "hr_other": 403},
    "unarchive": {"sa": 403, "oa": 200, "hr_owner": 200, "hr_other": 403},
    "version_patch": {"sa": 403, "oa": 200, "hr_owner": 200, "hr_other": 403},
    "add_question": {"sa": 403, "oa": 201, "hr_owner": 201, "hr_other": 403},
    "bulk": {"sa": 403, "oa": 201, "hr_owner": 201, "hr_other": 403},
    "publish": {"sa": 403, "oa": 200, "hr_owner": 200, "hr_other": 403},
    "new_version": {"sa": 403, "oa": 201, "hr_owner": 201, "hr_other": 403},
    "delete": {"sa": 403, "oa": 204, "hr_owner": 204, "hr_other": 403},
}


def _operation(op: str, aid: str, vid: str, qid: str):
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
        "unarchive": ("POST", f"{BASE}/{aid}/unarchive", None),
        "version_patch": ("PATCH", v, {"duration_minutes": 30}),
        "add_question": ("POST", f"{v}/questions", single()),
        "bulk": ("POST", f"{v}/questions/bulk", {"questions": [single()]}),
        "publish": ("POST", f"{v}/publish", None),
        "new_version": ("POST", f"{BASE}/{aid}/versions", None),
        "delete": ("DELETE", f"{BASE}/{aid}", None),
    }[op]


@pytest.mark.parametrize(("op", "role"), [(op, role) for op, roles in PERMISSION_TABLE.items() for role in roles])
async def test_permission_matrix(client, world, op, role):
    made = await new_assessment(client, world.h.hr_owner)
    await add_question(client, world.h.hr_owner, made.id, made.draft_id)
    if op == "new_version":
        assert (await publish(client, world.h.hr_owner, made.id, made.draft_id)).status_code == 200
    method, path, body = _operation(op, made.id, made.draft_id, "")
    params = {"org_id": str(world.org.id)} if role == "sa" else None
    resp = await client.request(method, path, json=body, params=params, headers=getattr(world.h, role))
    assert resp.status_code == PERMISSION_TABLE[op][role], (op, role, resp.text)


async def test_flag_off_blocks_assessments_api(client, world, db_session):
    await db_session.execute(
        text("INSERT INTO org_feature_flags (org_id, flag_key, enabled) VALUES (:o, 'assessments', false)"),
        {"o": world.org.id},
    )
    await db_session.commit()
    resp = await client.get(BASE, headers=world.h.oa)
    assert resp.status_code == 403
    error = resp.json()["error"]
    assert error["code"] == "FEATURE_DISABLED" and error["details"] == {"flag": "assessments"}
    assert (await client.get(BASE, headers=world.h.outsider)).status_code == 200  # other org unaffected


async def test_openapi_declares_roles_flag_and_docs():
    from app.main import create_app

    schema = create_app().openapi()
    ops = [
        (path, method, op)
        for path, item in schema["paths"].items()
        if path.startswith(BASE)
        for method, op in item.items()
    ]
    assert len(ops) >= 24
    for path, method, op in ops:
        assert op["x-required-flag"] == "assessments", (path, method)
        assert op["x-roles"], (path, method)
        assert op.get("summary") and op.get("description"), (path, method)
        assert any(code.startswith("4") for code in op["responses"]), (path, method)
    publish_op = schema["paths"][f"{BASE}/{{assessment_id}}/versions/{{version_id}}/publish"]["post"]
    assert set(publish_op["x-roles"]) == {"org_admin", "hr_user"}


# --------------------------------------------------------------------------- service interfaces


async def test_get_published_version_interface(client, world, db_session):
    from app.modules.m05_assessments.service import get_published_version

    from app.core.db import TenantContext, tenant_session
    from app.core.errors import AppError

    made = await new_assessment(client, world.h.hr_owner)
    for payload in (single(), multi(partial=True)):
        await add_question(client, world.h.hr_owner, made.id, made.draft_id, payload)
    draft_id = UUID(made.draft_id)
    ctx = TenantContext(world.org.id, world.oa.id, "org_admin", False, "t", None, None)
    async with tenant_session(ctx) as session:
        with pytest.raises(AppError) as err:
            await get_published_version(session, world.org.id, draft_id)
        assert err.value.code == "ASSESSMENT_NOT_PUBLISHED"
        with pytest.raises(AppError) as err:
            await get_published_version(session, world.org.id, uuid4())
        assert err.value.code == "NOT_FOUND"
    assert (await publish(client, world.h.hr_owner, made.id, made.draft_id)).status_code == 200
    async with tenant_session(ctx) as session:
        version = await get_published_version(session, world.org.id, draft_id)
        assert version.version_no == 1 and len(version.questions) == 2
        first, second = version.questions
        assert first.type == "single_choice" and sum(o.is_correct for o in first.options) == 1
        assert second.answer_key == {"partial": True}
        with pytest.raises(AppError) as err:  # another org's id is a plain 404
            await get_published_version(session, world.other_org.id, draft_id)
        assert err.value.code == "NOT_FOUND"
