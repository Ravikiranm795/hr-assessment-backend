import pytest
from sqlalchemy import text

BASE = "/api/v1/question-bank"
ITEM = {
    "type": "single_choice",
    "prompt_html": "<p>2 + 2?</p><script>x</script>",
    "marks": 2,
    "options": [{"text_html": "4", "is_correct": True}, {"text_html": "5", "is_correct": False}],
    "tags": ["Math", "easy"],
}


async def _save(client, world, who="hr_owner", **over):
    resp = await client.post(BASE, json={**ITEM, **over}, headers=getattr(world.h, who))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_save_get_as_question_and_tag_filter(client, world):
    item = await _save(client, world)
    assert item["tags"] == ["easy", "math"] and "script" not in item["prompt_html"]
    await _save(client, world, tags=["other"])
    listed = (await client.get(BASE, params={"tag": "MATH"}, headers=world.h.hr_other)).json()
    assert [i["id"] for i in listed["items"]] == [item["id"]] and listed["total"] == 1
    body = (await client.get(f"{BASE}/{item['id']}/as-question", headers=world.h.oa)).json()
    assert body["type"] == "single_choice" and body["options"][0] == {"text_html": "4", "is_correct": True}
    assert "tags" not in body and body["marks"] == 2


async def test_bank_is_org_scoped_and_edit_rights(client, world):
    item = await _save(client, world)
    url = f"{BASE}/{item['id']}"
    assert (await client.get(url, headers=world.h.outsider)).status_code == 404
    assert (await client.delete(url, headers=world.h.outsider)).status_code == 404
    assert (await client.patch(url, json={"marks": 3}, headers=world.h.hr_other)).status_code == 403
    assert (await client.patch(url, json={"marks": 3}, headers=world.h.hr_owner)).json()["marks"] == 3
    assert (await client.delete(url, headers=world.h.oa)).status_code == 204
    assert (await client.get(url, headers=world.h.oa)).status_code == 404


@pytest.mark.parametrize(
    ("who", "method", "status"),
    [("sa", "get", 403), ("sa", "post", 403), ("oa", "get", 200), ("hr_other", "post", 201)],
)
async def test_permission_matrix(client, world, who, method, status):
    kwargs = {"json": ITEM} if method == "post" else {}
    resp = await getattr(client, method)(BASE, headers=getattr(world.h, who), **kwargs)
    assert resp.status_code == status


async def test_flag_off_blocks_bank(client, world, db_session):
    await db_session.execute(
        text("INSERT INTO org_feature_flags (org_id, flag_key, enabled) VALUES (:o, 'question_bank', false)"),
        {"o": world.org.id},
    )
    await db_session.commit()
    resp = await client.get(BASE, headers=world.h.oa)
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "FEATURE_DISABLED"
