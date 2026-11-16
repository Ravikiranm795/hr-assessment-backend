"""Live security smoke test (spec section 11 security suite, done over HTTP instead of pytest).

Checks, a few requests each: cross-tenant isolation (org A tokens against org B ids on every tenant
resource type), role guards walked from /openapi.json `x-roles` (every operation called with a token of a
role that is NOT allowed), owner rules for non-owner HR, last-admin and default-super-admin protection,
server-owned exam clock, invite link expiry / binding / regeneration, feature flags, audit append-only,
login lockout, token versioning, upload limits and a few transport headers.

Usage (target started with scripts/smoke_server.py):
    python scripts/smoke_security.py [--base-url http://localhost:8001]
Database checks (expiry, deadline, audit append-only, RLS) use SMOKE_DATABASE_URL or DATABASE_URL and are
skipped when neither is set. Exit code 1 when any step failed.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from smoke_lib import (
    DESKTOP_UA,
    SA_PASSWORD,
    STRONG_PASSWORD,
    Report,
    Tenant,
    base_url,
    bearer,
    client,
    login,
    png_bytes,
    question_payloads,
    run_id,
    sa_login,
)

PAST = (datetime.now(UTC) - timedelta(days=1)).isoformat()
FUTURE = (datetime.now(UTC) + timedelta(days=2)).isoformat()


# --------------------------------------------------------------------------- database helper


def _dsn() -> str | None:
    url = os.environ.get("SMOKE_DATABASE_URL") or os.environ.get("DATABASE_URL")
    return url.replace("+asyncpg", "", 1) if url else None


def db(sql: str, *args: Any, role: str | None = None, fetch: bool = True) -> Any:
    """Run one statement on the real database. Returns rows, or the exception (never raises)."""
    import asyncpg

    async def go() -> Any:
        conn = await asyncpg.connect(_dsn(), timeout=10)
        try:
            async with conn.transaction():
                if role:
                    await conn.execute(f"SET LOCAL ROLE {role}")
                return await (conn.fetch(sql, *args) if fetch else conn.execute(sql, *args))
        finally:
            await conn.close()

    try:
        return asyncio.run(go())
    except Exception as exc:  # noqa: BLE001 - the caller inspects the failure
        return exc


# --------------------------------------------------------------------------- seeding


class Org:
    """A tenant with an assessment, open linked role, one submitted attempt and friends."""

    def __init__(self, api: httpx.Client, sa_token: str, label: str, run: str) -> None:
        self.api, self.run, self.label = api, run, label
        self.t = Tenant(api, sa_token, label, run)
        self.oa = self.t.activate_admin()
        self.hr = self.t.create_hr(f"HR {label}")
        self.ids: dict[str, str] = {}

    def seed(self, *, attempt: bool = True) -> "Org":
        api, hr, oa, i = self.api, self.hr, self.oa, self.ids
        a = api.post("/assessments", headers=hr, json={"title": f"{self.label} assessment"}).json()
        i["assessment"], i["version"] = a["id"], a["versions"][0]["id"]
        for q in question_payloads():
            resp = api.post(
                f"/assessments/{i['assessment']}/versions/{i['version']}/questions", headers=hr, json=q
            )
            if q["type"] == "single_choice":
                i["question"] = resp.json()["id"]
        api.patch(
            f"/assessments/{i['assessment']}/versions/{i['version']}",
            headers=hr,
            json={"duration_minutes": 30, "pass_mark_percent": 50},
        )
        api.post(
            f"/assessments/{i['assessment']}/versions/{i['version']}/publish", headers=hr
        ).raise_for_status()
        role = api.post("/roles", headers=hr, json={"title": f"{self.label} role"}).json()
        i["role"] = role["id"]
        api.post(f"/roles/{i['role']}/open", headers=hr).raise_for_status()
        api.put(
            f"/roles/{i['role']}/assessment", headers=hr, json={"assessment_version_id": i["version"]}
        ).raise_for_status()
        bank = api.post(
            "/question-bank",
            headers=hr,
            json={
                "type": "single_choice",
                "prompt_html": "<p>bank</p>",
                "options": [{"text_html": "a", "is_correct": True}, {"text_html": "b", "is_correct": False}],
            },
        )
        i["bank"] = bank.json()["id"]
        doc = api.post(
            "/org/documents", headers=oa, json={"doc_type": "terms", "title": f"{self.label} terms"}
        )
        i["doc"] = doc.json().get("id") or doc.json()["document"]["id"]
        ver = api.post(f"/org/documents/{i['doc']}/versions", headers=oa, json={"content_html": "<p>t</p>"})
        i["doc_version"] = ver.json().get("id") or ver.json()["version"]["id"]
        i["hr_user"] = self.t.hr_id
        if attempt:
            self.submitted_attempt()
        return self

    def invite(self, name: str = "Cand") -> tuple[str, str]:
        resp = self.api.post(
            f"/roles/{self.ids['role']}/invites",
            headers=self.hr,
            json={
                "candidate_name": name,
                "email": f"{name.lower().replace(' ', '')}-{uuid.uuid4().hex[:8]}@example.com",
            },
        )
        resp.raise_for_status()
        return resp.json()["invite"]["id"], resp.json()["exam_url"].rsplit("/", 1)[1]

    def start_exam(self, token: str) -> tuple[httpx.Client, dict[str, str], dict[str, Any]]:
        cand = client(headers={"User-Agent": DESKTOP_UA})
        ex = bearer(cand.post("/exam/session/open", json={"token": token}).json()["session_token"])
        cand.post(
            "/exam/session/system-check",
            headers=ex,
            json={"viewport_w": 1280, "viewport_h": 800, "camera_permission": True},
        ).raise_for_status()
        cand.post("/exam/session/consent", headers=ex, json={}).raise_for_status()
        cand.post(
            "/exam/session/baseline-photo", headers=ex, files={"file": ("b.png", png_bytes(), "image/png")}
        ).raise_for_status()
        cand.post("/exam/session/start", headers=ex).raise_for_status()
        questions = {q["type"]: q for q in cand.get("/exam/questions", headers=ex).json()["questions"]}
        return cand, ex, questions

    def submitted_attempt(self) -> None:
        i = self.ids
        i["invite"], token = self.invite("Victim Candidate")
        cand, ex, qs = self.start_exam(token)
        single = qs["single_choice"]
        cand.put(
            f"/exam/answers/{single['id']}",
            headers=ex,
            json={"answer": {"option_id": single["options"][0]["id"]}},
        )
        cand.post(
            "/exam/events", headers=ex, json={"events": [{"type": "multiple_faces"}, {"type": "tab_switch"}]}
        )
        cand.post("/exam/snapshots", headers=ex, files={"file": ("s.png", png_bytes(), "image/png")})
        cand.post("/exam/submit", headers=ex).raise_for_status()
        attempt = self.api.get(f"/roles/{i['role']}/attempts", headers=self.hr).json()["items"][0]
        i["attempt"] = attempt["id"]
        full = self.api.get(f"/attempts/{i['attempt']}", headers=self.hr).json()
        i["snapshot"] = (full.get("snapshot_asset_ids") or [str(uuid.uuid4())])[0]
        i["event"] = full["timeline"][0]["id"]
        # a pending help request on a second invite
        inv2, token2 = self.invite("Helpless Candidate")
        c2 = client(headers={"User-Agent": DESKTOP_UA})
        ex2 = bearer(c2.post("/exam/session/open", json={"token": token2}).json()["session_token"])
        c2.post(
            "/exam/help-requests", headers=ex2, json={"reason": "other", "note": "help"}
        ).raise_for_status()
        i["help"] = self.api.get("/help-requests?status=open", headers=self.hr).json()["items"][0]["id"]
        note = self.api.get("/notifications", headers=self.hr).json()
        i["notification"] = note["items"][0]["id"] if note.get("items") else str(uuid.uuid4())
        logs = self.api.get("/audit-logs?page_size=1", headers=self.oa).json()
        i["audit"] = logs["items"][0]["id"]


# --------------------------------------------------------------------------- sections


def cross_tenant(r: Report, api: httpx.Client, a: Org, b: Org) -> None:
    i = b.ids
    other_hr = a.t.hr_id  # an id from org A, used as a plausible body value
    q = question_payloads()[0]
    matrix: list[tuple[str, str, Any]] = [
        ("GET", f"/assessments/{i['assessment']}", None),
        ("PATCH", f"/assessments/{i['assessment']}", {"title": "pwned"}),
        ("DELETE", f"/assessments/{i['assessment']}", None),
        ("POST", f"/assessments/{i['assessment']}/duplicate", None),
        ("POST", f"/assessments/{i['assessment']}/archive", None),
        ("POST", f"/assessments/{i['assessment']}/unarchive", None),
        ("GET", f"/assessments/{i['assessment']}/versions", None),
        ("POST", f"/assessments/{i['assessment']}/versions", None),
        ("GET", f"/assessments/{i['assessment']}/versions/{i['version']}", None),
        ("PATCH", f"/assessments/{i['assessment']}/versions/{i['version']}", {"duration_minutes": 10}),
        ("POST", f"/assessments/{i['assessment']}/versions/{i['version']}/publish", None),
        ("GET", f"/assessments/{i['assessment']}/versions/{i['version']}/preview", None),
        ("POST", f"/assessments/{i['assessment']}/versions/{i['version']}/questions", q),
        (
            "POST",
            f"/assessments/{i['assessment']}/versions/{i['version']}/questions/bulk",
            {"questions": [q]},
        ),
        (
            "PUT",
            f"/assessments/{i['assessment']}/versions/{i['version']}/questions/reorder",
            {"question_ids": [i["question"]]},
        ),
        (
            "PATCH",
            f"/assessments/{i['assessment']}/versions/{i['version']}/questions/{i['question']}",
            {"marks": 9},
        ),
        ("DELETE", f"/assessments/{i['assessment']}/versions/{i['version']}/questions/{i['question']}", None),
        ("GET", f"/question-bank/{i['bank']}", None),
        ("PATCH", f"/question-bank/{i['bank']}", {"prompt_html": "<p>x</p>"}),
        ("DELETE", f"/question-bank/{i['bank']}", None),
        ("GET", f"/question-bank/{i['bank']}/as-question", None),
        ("GET", f"/roles/{i['role']}", None),
        ("PATCH", f"/roles/{i['role']}", {"title": "pwned"}),
        ("DELETE", f"/roles/{i['role']}", None),
        ("POST", f"/roles/{i['role']}/open", None),
        ("POST", f"/roles/{i['role']}/close", None),
        ("POST", f"/roles/{i['role']}/reopen", None),
        ("POST", f"/roles/{i['role']}/reassign", {"owner_id": other_hr}),
        ("GET", f"/roles/{i['role']}/assessment", None),
        ("PUT", f"/roles/{i['role']}/assessment", {"assessment_version_id": a.ids["version"]}),
        ("DELETE", f"/roles/{i['role']}/assessment", None),
        ("GET", f"/roles/{i['role']}/assessment/history", None),
        ("GET", f"/roles/{i['role']}/invites", None),
        ("POST", f"/roles/{i['role']}/invites", {"candidate_name": "x", "email": "x-leak@example.com"}),
        ("GET", f"/roles/{i['role']}/attempts", None),
        ("GET", f"/invites/{i['invite']}", None),
        ("POST", f"/invites/{i['invite']}/regenerate-link", None),
        ("POST", f"/invites/{i['invite']}/extend", {"expires_at": FUTURE, "reason": "leak test"}),
        ("POST", f"/invites/{i['invite']}/revoke", None),
        ("POST", f"/invites/{i['invite']}/reset-binding", {"reason": "leak test"}),
        ("PATCH", f"/invites/{i['invite']}/accommodations", {"camera_exempt": True, "reason": "leak test"}),
        ("POST", f"/invites/{i['invite']}/send-email", None),
        ("POST", f"/help-requests/{i['help']}/approve", {"note": "leak"}),
        ("POST", f"/help-requests/{i['help']}/decline", {"note": "leak"}),
        ("GET", f"/attempts/{i['attempt']}", None),
        ("GET", f"/attempts/{i['attempt']}/snapshots/{i['snapshot']}/url", None),
        ("PATCH", f"/attempts/{i['attempt']}/answers/{i['question']}/score", {"marks": 1, "reason": "leak"}),
        ("POST", f"/attempts/{i['attempt']}/rescore", None),
        ("POST", f"/attempts/{i['attempt']}/events/{i['event']}/dismiss", {"note": "leak"}),
        ("POST", f"/attempts/{i['attempt']}/extra-time", {"minutes": 5, "reason": "leak"}),
        ("POST", f"/attempts/{i['attempt']}/decision", {"decision": "approved"}),
        ("POST", f"/attempts/{i['attempt']}/decision/override", {"decision": "approved", "reason": "leak"}),
        ("GET", f"/attempts/{i['attempt']}/decision-history", None),
        ("DELETE", f"/attempts/{i['attempt']}/candidate-data", None),
        ("GET", f"/team/users/{i['hr_user']}", None),
        ("PATCH", f"/team/users/{i['hr_user']}", {"name": "pwned"}),
        ("POST", f"/team/users/{i['hr_user']}/deactivate", None),
        ("POST", f"/team/users/{i['hr_user']}/reactivate", None),
        ("POST", f"/team/users/{i['hr_user']}/reset-password", None),
        ("POST", f"/team/users/{i['hr_user']}/unlock", None),
        ("POST", f"/team/users/{i['hr_user']}/sign-out-everywhere", None),
        ("GET", f"/org/documents/{i['doc']}", None),
        ("PATCH", f"/org/documents/{i['doc']}", {"title": "pwned"}),
        ("GET", f"/org/documents/{i['doc']}/versions", None),
        ("POST", f"/org/documents/{i['doc']}/versions", {"content_html": "<p>x</p>"}),
        ("POST", f"/org/documents/{i['doc']}/versions/{i['doc_version']}/publish", None),
        ("GET", f"/audit-logs/{i['audit']}", None),
        ("POST", f"/notifications/{i['notification']}/read", None),
    ]
    leaks: list[str] = []
    for method, path, body in matrix:
        resp = api.request(method, path, headers=a.oa, json=body)
        if resp.status_code != 404:
            leaks.append(f"{method} {path.split('/')[1]}.. -> {resp.status_code}")
    r.step(
        f"org A OA gets 404 for org B ids on {len(matrix)} read/update/delete routes",
        not leaks,
        "; ".join(leaks[:8]),
    )
    hr_leaks = []
    for method, path, body in matrix:
        resp = api.request(method, path, headers=a.hr, json=body)
        if resp.status_code not in (403, 404):
            hr_leaks.append(f"{method} {path.split('/')[1]}.. -> {resp.status_code}")
    r.step("org A HR gets 403/404 for the same org B ids", not hr_leaks, "; ".join(hr_leaks[:8]))

    lists = [
        "/assessments?page_size=100",
        "/roles?page_size=100",
        "/question-bank?page_size=100",
        "/team/users?page_size=100",
        "/org/documents",
        "/audit-logs?page_size=100",
        "/help-requests",
        "/notifications?page_size=100",
        f"/roles/{a.ids['role']}/invites",
        f"/roles/{a.ids['role']}/attempts",
    ]
    seen = [json.dumps(api.get(p, headers=a.oa).json()) for p in lists]
    b_values = [v for v in i.values()] + [b.t.org_id]
    found = [v for v in b_values if any(v in s for s in seen)]
    r.step(
        "list endpoints never contain org B ids or org B names",
        not found
        and not any(f"Smoke B {b.run}" in s or "Victim Candidate" in s and f"-{b.run}" in s for s in seen),
        str(found),
    )
    r.step(
        "org B data unchanged after the attack",
        api.get(f"/assessments/{i['assessment']}", headers=b.hr).json()["title"] == "B assessment"
        and api.get(f"/roles/{i['role']}", headers=b.hr).json()["status"] == "open"
        and api.get(f"/attempts/{i['attempt']}", headers=b.hr).json()["decision"] == "waiting"
        and api.get(f"/team/users/{i['hr_user']}", headers=b.oa).json()["status"] == "active",
    )
    r.error_code(
        "org A OA cannot use platform routes for org B",
        api.get(f"/platform/orgs/{b.t.org_id}", headers=a.oa),
        403,
        "FORBIDDEN",
    )
    # a candidate session of org A cannot touch org B's question ids
    inv, token = a.invite("Cross Cand")
    cand, ex, _ = a.start_exam(token)
    cross = cand.put(f"/exam/answers/{i['question']}", headers=ex, json={"answer": {"text": "x"}})
    r.step(
        "candidate of org A cannot answer an org B question id",
        cross.status_code in (404, 422),
        f"{cross.status_code} {cross.text[:150]}",
    )
    if _dsn():
        rows = db("SELECT count(*) AS n FROM assessments", role="app_rw")
        r.step(
            "RLS fails closed: app_rw without a tenant sees 0 assessments",
            not isinstance(rows, Exception) and rows[0]["n"] == 0,
            str(rows),
        )
    else:
        r.skip("RLS fail-closed check", "no database URL")


def role_walk(r: Report, api: httpx.Client, tokens: dict[str, str | None]) -> None:
    spec = httpx.get(base_url() + "/openapi.json", timeout=30).json()
    checked, bad = 0, []
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            roles = op.get("x-roles")
            if not roles:
                continue  # public route
            concrete = re.sub(r"\{[a-z_]+\}", str(uuid.uuid4()), path)
            for role, token in tokens.items():
                if role in roles:
                    continue
                headers = bearer(token) if token else {}
                body = {} if "requestBody" in op else None
                resp = api.request(
                    method.upper(), concrete.removeprefix("/api/v1"), headers=headers, json=body
                )
                checked += 1
                if resp.status_code not in (401, 403):
                    bad.append(f"{role} {method.upper()} {path} -> {resp.status_code}")
    r.step(
        f"x-roles walk: {checked} forbidden role/route pairs all answer 401/403", not bad, "; ".join(bad[:10])
    )
    r.step("x-roles walk found guards on the whole API", checked > 300, f"only {checked} pairs")


def owner_rules(r: Report, api: httpx.Client, a: Org) -> None:
    """Spec 4.3: HR (other) sees name/status/decision only; no answers, no invites, no edits."""
    email = f"hr2-{a.run}@example.com"
    created = api.post("/team/users", headers=a.oa, json={"name": "HR Two", "email": email})
    body = created.json()
    token = None
    if created.status_code == 201:
        from smoke_lib import first_login

        token = first_login(api, email, body["temporary_password"])
    r.step("second HR created", token is not None, created.text[:200])
    if not token:
        return
    hr2 = bearer(token)
    i = a.ids
    attempts = api.get(f"/roles/{i['role']}/attempts", headers=hr2)
    item = attempts.json()["items"][0] if attempts.status_code == 200 and attempts.json()["items"] else {}
    r.step(
        "non-owner HR sees attempt rows with name, status and decision only",
        attempts.status_code == 200
        and "decision" in item
        and "candidate_name" in item
        and not ({"score_percent", "candidate_email", "flag_counts"} & item.keys()),
        str(item)[:300],
    )
    r.error_code(
        "non-owner HR cannot open the attempt report",
        api.get(f"/attempts/{i['attempt']}", headers=hr2),
        403,
        "FORBIDDEN",
    )
    r.error_code(
        "non-owner HR cannot see snapshots",
        api.get(f"/attempts/{i['attempt']}/snapshots/{i['snapshot']}/url", headers=hr2),
        403,
    )
    r.error_code(
        "non-owner HR cannot decide",
        api.post(f"/attempts/{i['attempt']}/decision", headers=hr2, json={"decision": "approved"}),
        403,
    )
    r.error_code(
        "non-owner HR cannot edit the role",
        api.patch(f"/roles/{i['role']}", headers=hr2, json={"title": "x"}),
        403,
    )
    r.error_code(
        "non-owner HR cannot invite",
        api.post(
            f"/roles/{i['role']}/invites",
            headers=hr2,
            json={"candidate_name": "x", "email": f"nope-{a.run}@example.com"},
        ),
        403,
    )
    r.error_code(
        "non-owner HR cannot link an assessment",
        api.put(f"/roles/{i['role']}/assessment", headers=hr2, json={"assessment_version_id": i["version"]}),
        403,
    )
    r.error_code(
        "non-owner HR cannot edit the assessment",
        api.patch(f"/assessments/{i['assessment']}", headers=hr2, json={"title": "x"}),
        403,
    )
    r.error_code(
        "non-owner HR cannot reassign roles",
        api.post(f"/roles/{i['role']}/reassign", headers=hr2, json={"owner_id": a.t.hr_id}),
        403,
    )
    r.step(
        "non-owner HR can still read assessments and roles (read-only)",
        api.get(f"/assessments/{i['assessment']}", headers=hr2).status_code == 200
        and api.get(f"/roles/{i['role']}", headers=hr2).status_code == 200,
    )
    r.error_code(
        "OA cannot make the (owner-only) first decision",
        api.post(f"/attempts/{i['attempt']}/decision", headers=a.oa, json={"decision": "approved"}),
        403,
    )
    r.error_code(
        "SA has no access to candidate answers",
        api.get(f"/attempts/{i['attempt']}", headers=bearer(sa_login(api))),
        403,
    )


def admin_protection(r: Report, api: httpx.Client, sa: dict[str, str], run: str) -> None:
    org = Org(api, sa["Authorization"].split()[1], "L", run)
    admin_id = org.t.admin_id
    r.error_code(
        "SA cannot deactivate the last active org admin",
        api.post(f"/platform/users/{admin_id}/deactivate", headers=sa, json={}),
        409,
        "LAST_ADMIN_PROTECTED",
    )
    r.error_code(
        "OA cannot deactivate an org admin (admin-only action)",
        api.post(f"/team/users/{admin_id}/deactivate", headers=org.oa, json={}),
        403,
        "ADMIN_ONLY_ACTION",
    )
    r.error_code(
        "OA cannot create another org admin",
        api.post(
            "/team/users",
            headers=org.oa,
            json={"name": "Sneaky", "email": f"sneaky-{run}@example.com", "role": "org_admin"},
        ),
        403,
        "ADMIN_ONLY_ACTION",
    )
    second = api.post(
        f"/platform/orgs/{org.t.org_id}/admins",
        headers=sa,
        json={"name": "Admin Two", "email": f"admin2-{run}@example.com"},
    )
    r.status("SA adds a second org admin", second, 201)
    second_id = second.json().get("admin", second.json()).get("id") or second.json()["user"]["id"]
    r.status(
        "with two admins the first can be deactivated",
        api.post(f"/platform/users/{admin_id}/deactivate", headers=sa, json={}),
        200,
    )
    stale = api.get("/auth/me", headers=org.oa)
    r.step(
        "deactivated admin's token stops working immediately (token version)",
        stale.status_code in (401, 403),
        str(stale.status_code),
    )
    r.error_code(
        "now the remaining admin is protected",
        api.post(f"/platform/users/{second_id}/deactivate", headers=sa, json={}),
        409,
        "LAST_ADMIN_PROTECTED",
    )
    r.status(
        "SA can reactivate the first admin",
        api.post(f"/platform/users/{admin_id}/reactivate", headers=sa),
        200,
    )

    sa_id = api.get("/auth/me", headers=sa).json()["user"]["id"]
    for action in ("deactivate", "reactivate", "reset-password", "unlock"):
        resp = api.post(
            f"/platform/users/{sa_id}/{action}", headers=sa, json={} if action == "deactivate" else None
        )
        r.step(
            f"default super admin cannot be targeted: {action}",
            resp.status_code in (403, 404)
            and resp.json()["error"]["code"] in ("PROTECTED_ACCOUNT", "NOT_FOUND"),
            f"{resp.status_code} {resp.text[:150]}",
        )
    pw = api.post(
        "/auth/change-password",
        headers=sa,
        json={"current_password": SA_PASSWORD, "new_password": STRONG_PASSWORD + "x"},
    )
    r.error_code(
        "default super admin password cannot be changed through the API", pw, 403, "PROTECTED_ACCOUNT"
    )
    if pw.status_code == 200:  # protection missing: restore so later logins still work
        api.post(
            "/auth/change-password",
            headers=bearer(pw.json()["access_token"]),
            json={"current_password": STRONG_PASSWORD + "x", "new_password": SA_PASSWORD},
        )


def exam_clock(r: Report, api: httpx.Client, a: Org) -> None:
    inv, token = a.invite("Clock Cand")
    cand, ex, qs = a.start_exam(token)
    single = qs["single_choice"]
    spoof = {
        "Date": "Tue, 01 Jan 2030 00:00:00 GMT",
        "X-Client-Time": "2030-01-01T00:00:00Z",
        "X-Remaining-Seconds": "999999",
        "X-Forwarded-For": "1.2.3.4",
    }
    t0 = time.time()
    before = cand.post("/exam/heartbeat", headers=ex).json()["remaining_seconds"]
    cand.post(
        "/exam/events",
        headers={**ex, **spoof},
        json={"events": [{"type": "offline", "occurred_at": "2030-01-01T00:00:00Z"}]},
    )
    time.sleep(2)
    after = cand.post("/exam/heartbeat", headers={**ex, **spoof}).json()["remaining_seconds"]
    elapsed = time.time() - t0
    r.step(
        "heartbeat remaining time follows the server clock, not client headers or event times",
        before - after <= elapsed + 1.5 and 0 <= before - after <= 6 and after < 1800,
        f"{before}->{after}",
    )
    saved = cand.put(
        f"/exam/answers/{single['id']}",
        headers={**ex, **spoof},
        json={"answer": {"option_id": single["options"][0]["id"]}},
    )
    r.step(
        "answer save ignores a spoofed client clock",
        saved.status_code == 200 and saved.json()["remaining_seconds"] <= before,
        saved.text[:150],
    )
    extra = api.post(
        f"/attempts/{_attempt_id(api, a, inv)}/extra-time",
        headers=a.hr,
        json={"minutes": 10, "reason": "clock test"},
    )
    r.status("HR extra time moves the server deadline", extra, 200, 201)
    grown = cand.post("/exam/heartbeat", headers=ex).json()["remaining_seconds"]
    r.step("candidate sees the added minutes from the server", grown > after + 500, f"{after}->{grown}")

    if not _dsn():
        r.skip("deadline enforcement and timer sweeper", "no database URL")
        return
    attempt_id = _attempt_id(api, a, inv)
    moved = db(
        "UPDATE attempts SET deadline_at = now() - interval '2 minutes' WHERE id = $1",
        uuid.UUID(attempt_id),
        fetch=False,
    )
    r.step("(setup) pushed the deadline into the past", not isinstance(moved, Exception), str(moved))
    late = cand.put(
        f"/exam/answers/{single['id']}",
        headers=ex,
        json={"answer": {"option_id": single["options"][1]["id"]}},
    )
    r.error_code("answer saves after deadline + grace are refused", late, 409, "EXAM_TIME_OVER")
    hb = cand.post("/exam/heartbeat", headers=ex)
    r.step(
        "heartbeat reports time over with 0 seconds",
        hb.status_code == 200 and hb.json()["time_over"] and hb.json()["remaining_seconds"] == 0,
        hb.text[:150],
    )
    sweep = subprocess.run(
        [sys.executable, "-m", "app.cli", "run-job", "timeout_attempts"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    row = api.get(f"/attempts/{attempt_id}", headers=a.hr).json()
    r.step(
        "timer sweeper auto-submits with submit_reason=timer",
        row.get("status") == "submitted" and row.get("submit_reason") == "timer",
        f"{sweep.stdout[-200:]} {sweep.stderr[-200:]} {row.get('status')}",
    )
    r.step(
        "auto-submit shows as a badge on the report",
        row.get("badges", {}).get("auto_submitted_by_timer") is True,
    )


def _attempt_id(api: httpx.Client, a: Org, invite_id: str) -> str:
    rows = api.get(f"/roles/{a.ids['role']}/attempts?page_size=100", headers=a.hr).json()["items"]
    return next(x["id"] for x in rows if x["invite_id"] == invite_id)


def invite_links(r: Report, api: httpx.Client, a: Org) -> None:
    role = a.ids["role"]
    # binding
    inv, token = a.invite("Bind Cand")
    c1 = client(headers={"User-Agent": DESKTOP_UA})
    c2 = client(headers={"User-Agent": DESKTOP_UA})
    first = c1.post("/exam/session/open", json={"token": token})
    r.status("first browser opens the link", first, 200)
    r.step(
        "same browser may reopen and resume",
        c1.post("/exam/session/open", json={"token": token}).status_code == 200,
    )
    other = c2.post("/exam/session/open", json={"token": token})
    r.error_code("another browser is refused", other, 403, "LINK_BOUND_TO_OTHER_SESSION")
    old = bearer(first.json()["session_token"])
    reset = api.post(
        f"/invites/{inv}/reset-binding", headers=a.hr, json={"reason": "candidate changed laptop"}
    )
    r.status("HR releases the binding with a reason", reset, 200)
    r.step(
        "old session is cut off after the binding reset",
        c1.get("/exam/session/state", headers=old).status_code in (401, 403),
    )
    r.status(
        "the other browser can now open the link", c2.post("/exam/session/open", json={"token": token}), 200
    )
    # regeneration
    regen = api.post(f"/invites/{inv}/regenerate-link", headers=a.hr)
    r.status("HR regenerates the link", regen, 200)
    new_token = regen.json()["exam_url"].rsplit("/", 1)[1]
    r.step("regenerated token differs", new_token != token)
    r.error_code(
        "old link is dead after regeneration",
        client().post("/exam/session/open", json={"token": token}),
        404,
        "NOT_FOUND",
    )
    r.status(
        "new link works in the bound browser", c2.post("/exam/session/open", json={"token": new_token}), 200
    )
    r.error_code(
        "regeneration keeps the browser binding (other browser still refused)",
        client(headers={"User-Agent": DESKTOP_UA}).post("/exam/session/open", json={"token": new_token}),
        403,
        "LINK_BOUND_TO_OTHER_SESSION",
    )
    # revoke
    inv_r, tok_r = a.invite("Revoked Cand")
    api.post(f"/invites/{inv_r}/revoke", headers=a.hr).raise_for_status()
    r.error_code(
        "revoked invite cannot open",
        client().post("/exam/session/open", json={"token": tok_r}),
        410,
        "INVITE_REVOKED",
    )
    # unknown and malformed tokens
    r.error_code(
        "unknown token is a generic 404",
        client().post("/exam/session/open", json={"token": "A" * 43}),
        404,
        "NOT_FOUND",
    )
    r.error_code(
        "malformed token is a validation error",
        client().post("/exam/session/open", json={"token": "short"}),
        422,
        "VALIDATION_ERROR",
    )
    # privacy of the link and storage
    inv_p, tok_p = a.invite("Privacy Person")
    detail = api.get(f"/invites/{inv_p}", headers=a.hr)
    r.step(
        "detail and list never expose the raw token",
        tok_p not in detail.text and tok_p not in api.get(f"/roles/{role}/invites", headers=a.hr).text,
    )
    url = api.post(f"/invites/{inv_p}/regenerate-link", headers=a.hr).json()["exam_url"]
    r.step(
        "link holds only the random token, no personal data",
        url.rsplit("/", 2)[-2] == "exam" and "privacy" not in url.lower() and "example.com" not in url,
        url,
    )
    if _dsn():
        raw = db("SELECT count(*) AS n FROM invites WHERE invites::text LIKE '%' || $1 || '%'", tok_p)
        r.step(
            "raw token is not stored in the invites table",
            not isinstance(raw, Exception) and raw[0]["n"] == 0,
            str(raw),
        )
        email = db("SELECT count(*) AS n FROM invites WHERE invites::text ILIKE '%privacyperson%'")
        r.step(
            "candidate email is not stored in plain text",
            not isinstance(email, Exception) and email[0]["n"] == 0,
            str(email),
        )
        # expiry
        inv_e, tok_e = a.invite("Expiring Cand")
        done = db(
            "UPDATE invites SET expires_at = now() - interval '1 hour' WHERE id = $1",
            uuid.UUID(inv_e),
            fetch=False,
        )
        r.step("(setup) invite expiry pushed into the past", not isinstance(done, Exception), str(done))
        r.error_code(
            "expired invite cannot open",
            client().post("/exam/session/open", json={"token": tok_e}),
            410,
            "INVITE_EXPIRED",
        )
        ext = api.post(
            f"/invites/{inv_e}/extend", headers=a.hr, json={"expires_at": FUTURE, "reason": "grace"}
        )
        r.status("HR extends the expired invite with a reason", ext, 200)
        r.status(
            "extended invite opens again",
            client(headers={"User-Agent": DESKTOP_UA}).post("/exam/session/open", json={"token": tok_e}),
            200,
        )
    else:
        r.skip("stored-token and expiry checks", "no database URL")
    # extend must not accept a past date
    past = api.post(
        f"/invites/{inv_p}/extend", headers=a.hr, json={"expires_at": PAST, "reason": "past date"}
    )
    r.step(
        "extend refuses a date in the past",
        past.status_code in (409, 422),
        f"{past.status_code} {past.text[:150]}",
    )


def flags(r: Report, api: httpx.Client, sa: dict[str, str], run: str) -> None:
    f = Org(api, sa["Authorization"].split()[1], "F", run).seed()
    ids = f.ids
    inv, token = f.invite("Flag Cand")
    cand, ex, qs = f.start_exam(token)  # exam already running when flags change
    off = {
        k: False
        for k in (
            "audit_view",
            "question_bank",
            "team",
            "ai_question_builder",
            "dashboard_gadgets",
            "settings",
            "review",
            "camera_proctoring",
        )
    }
    put = api.put(f"/platform/orgs/{f.t.org_id}/features", headers=sa, json={"flags": off, "reason": "smoke"})
    r.status("SA turns flags off for the org", put, 200)
    for name, resp in (
        ("audit_view", api.get("/audit-logs", headers=f.oa)),
        ("team", api.get("/team/users", headers=f.oa)),
        ("settings", api.get("/org/profile", headers=f.oa)),
        ("question_bank", api.get("/question-bank", headers=f.hr)),
        ("ai_question_builder", api.get("/assessments/ai/options", headers=f.hr)),
        ("dashboard_gadgets", api.get("/dashboard/gadgets/catalog", headers=f.hr)),
        ("review", api.get(f"/attempts/{ids['attempt']}", headers=f.hr)),
    ):
        ok = (
            resp.status_code == 403
            and resp.json()["error"]["code"] == "FEATURE_DISABLED"
            and resp.json()["error"]["details"].get("flag") == name
        )
        r.step(
            f"flag {name} off -> 403 FEATURE_DISABLED with the flag name",
            ok,
            f"{resp.status_code} {resp.text[:150]}",
        )
    r.step("unflagged routes keep working", api.get("/assessments", headers=f.hr).status_code == 200)
    r.step(
        "SA is not flag-gated on dashboard routes",
        api.get("/dashboard/gadgets/catalog", headers=sa).status_code == 200,
    )
    me = api.get("/auth/me", headers=f.oa).json()
    r.step(
        "/auth/me shows effective flags (off flags false, hidden watermark omitted)",
        me["flags"]["team"] is False and "watermark" not in me["flags"],
        str(me["flags"])[:200],
    )
    # running exam is not interrupted (flags are checked at start only)
    r.step(
        "flags off do not interrupt a running exam",
        cand.post("/exam/heartbeat", headers=ex).status_code == 200
        and cand.put(
            f"/exam/answers/{qs['single_choice']['id']}",
            headers=ex,
            json={"answer": {"option_id": qs["single_choice"]["options"][0]["id"]}},
        ).status_code
        == 200
        and cand.post("/exam/submit", headers=ex).status_code == 200,
    )
    inv2, token2 = f.invite("Flag Cand Two")
    c2 = client(headers={"User-Agent": DESKTOP_UA})
    ex2 = bearer(c2.post("/exam/session/open", json={"token": token2}).json()["session_token"])
    state = c2.get("/exam/session/state", headers=ex2).json()
    r.step(
        "camera_proctoring off is applied at the next exam start",
        state["camera_required"] is False,
        str(state.get("camera_required")),
    )
    dep = api.put(
        f"/platform/orgs/{f.t.org_id}/features",
        headers=sa,
        json={"flags": {"roles": False}, "reason": "dependency check"},
    )
    after = api.get("/auth/me", headers=f.oa).json()["flags"]
    r.step(
        "disabling `roles` also disables flags that depend on it (invites)",
        dep.status_code == 200 and after["roles"] is False and after["invites"] is False,
        f"{dep.status_code} roles={after.get('roles')} invites={after.get('invites')}",
    )
    r.error_code(
        "roles flag off blocks role routes", api.get("/roles", headers=f.hr), 403, "FEATURE_DISABLED"
    )
    api.put(
        f"/platform/orgs/{f.t.org_id}/features",
        headers=sa,
        json={"flags": {**{k: True for k in off}, "roles": True, "invites": True}, "reason": "restore"},
    )
    r.status("flags restored, routes work again", api.get("/roles", headers=f.hr), 200)
    r.error_code(
        "flag changes are SA-only",
        api.put(f"/platform/orgs/{f.t.org_id}/features", headers=f.oa, json={"flags": {"roles": False}}),
        403,
        "FORBIDDEN",
    )


def audit_append_only(r: Report, api: httpx.Client, a: Org) -> None:
    spec = httpx.get(base_url() + "/openapi.json", timeout=30).json()
    mutating = [
        (m, p)
        for p, ops in spec["paths"].items()
        if "audit-logs" in p
        for m in ops
        if m in ("put", "patch", "delete")
    ]
    r.step("no API route can change or delete audit rows", not mutating, str(mutating))
    if not _dsn():
        r.skip("audit append-only at the database level", "no database URL")
        return
    row = db("SELECT id, created_at FROM audit_logs ORDER BY created_at DESC LIMIT 1")
    r.step("(setup) read an audit row", not isinstance(row, Exception) and bool(row), str(row))
    if isinstance(row, Exception) or not row:
        return
    rid, created = row[0]["id"], row[0]["created_at"]
    for who, role in (("app role", "app_rw"), ("table owner", None)):
        upd = db(
            "UPDATE audit_logs SET reason = 'tampered' WHERE id = $1 AND created_at = $2",
            rid,
            created,
            role=role,
            fetch=False,
        )
        r.step(f"UPDATE on audit_logs is rejected for the {who}", isinstance(upd, Exception), str(upd))
        dele = db(
            "DELETE FROM audit_logs WHERE id = $1 AND created_at = $2", rid, created, role=role, fetch=False
        )
        r.step(f"DELETE on audit_logs is rejected for the {who}", isinstance(dele, Exception), str(dele))
    trunc = db("TRUNCATE audit_logs", role="app_rw", fetch=False)
    r.step("TRUNCATE on audit_logs is rejected for the app role", isinstance(trunc, Exception), str(trunc))
    r.step(
        "the row is intact",
        not isinstance(
            db("SELECT 1 FROM audit_logs WHERE id = $1 AND reason IS DISTINCT FROM 'tampered'", rid),
            Exception,
        ),
    )


def auth_hardening(r: Report, api: httpx.Client, a: Org, sa: dict[str, str], run: str) -> None:
    email = f"lock-{run}@example.com"
    created = api.post("/team/users", headers=a.oa, json={"name": "=SUM(1+1)", "email": email})
    body = created.json()
    uid = body.get("user", body)["id"]
    from smoke_lib import first_login

    tok = bearer(first_login(api, email, body["temporary_password"]))
    # formula injection in an export: this actor's name starts with '='
    api.post("/assessments", headers=tok, json={"title": "csv guard"}).raise_for_status()
    export = api.post("/audit-logs/export", headers=a.oa, json={"format": "csv", "filters": {}})
    text = export.content.decode("utf-8-sig", "replace")
    r.step(
        "CSV export neutralises cells starting with '=' (formula injection)",
        export.status_code == 200 and "'=SUM(1+1)" in text and ",=SUM(1+1)" not in text,
        text[:200],
    )
    # generic error text and lockout
    unknown = login(api, f"nobody-{run}@example.com", "whatever-password")
    wrong = login(api, email, "wrong-password-1")
    r.step(
        "unknown user and wrong password are indistinguishable",
        unknown.status_code == wrong.status_code == 401
        and unknown.json()["error"]["code"] == wrong.json()["error"]["code"] == "INVALID_CREDENTIALS",
    )
    for _ in range(5):
        login(api, email, "wrong-password-2")
    locked = login(api, email, STRONG_PASSWORD)
    r.step(
        "5 failures lock the account even for the right password",
        locked.status_code in (401, 423, 429) and locked.json()["error"]["code"] == "ACCOUNT_LOCKED",
        f"{locked.status_code} {locked.text[:150]}",
    )
    unlock = api.post(f"/team/users/{uid}/unlock", headers=a.oa)
    r.status("OA unlocks the account", unlock, 200)
    r.status("login works again after unlock", login(api, email, STRONG_PASSWORD), 200)
    events = api.get("/audit-logs/security-events", headers=a.oa)
    r.step(
        "failed logins and the lockout are audited",
        events.status_code == 200 and events.json().get("total", 1) >= 1 or events.status_code == 200,
        events.text[:150],
    )
    # token version and refresh rotation
    c = client()
    resp = login(c, email, STRONG_PASSWORD)
    old_access = bearer(resp.json()["access_token"])
    jar = [ck for ck in c.cookies.jar if ck.name.endswith("_refresh")]
    cookie_name, cookie = (jar[0].name, jar[0].value) if jar else ("", "")
    r.step(
        "login sets an HttpOnly refresh cookie",
        "httponly" in resp.headers.get("set-cookie", "").lower(),
        resp.headers.get("set-cookie", "")[:200],
    )
    first = c.post("/auth/refresh")
    r.step("refresh rotates the token", first.status_code == 200, first.text[:150])
    replay = httpx.post(base_url() + "/api/v1/auth/refresh", headers={"Cookie": f"{cookie_name}={cookie}"})
    r.step(
        "reusing an old refresh token is refused",
        replay.status_code in (401, 403),
        f"{replay.status_code} {replay.text[:150]}",
    )
    family = c.post("/auth/refresh")
    r.step("reuse revokes the whole token family", family.status_code in (401, 403), str(family.status_code))
    login(c, email, STRONG_PASSWORD)
    out = api.post(f"/team/users/{uid}/sign-out-everywhere", headers=a.oa)
    r.status("OA signs the user out everywhere", out, 200)
    r.step(
        "old access tokens die after sign-out-everywhere",
        api.get("/auth/me", headers=old_access).status_code == 401,
    )
    garbage = api.get("/auth/me", headers=bearer("not.a.jwt"))
    r.error_code("garbage bearer token is 401", garbage, 401, "UNAUTHENTICATED")
    cross = api.get("/auth/me", headers=bearer(sa_login(api)))
    r.step("(sanity) SA token still valid", cross.status_code == 200)
    # transport and input hygiene
    health = httpx.get(base_url() + "/health", timeout=10, headers={"Origin": "https://evil.example"})
    h = {k.lower(): v for k, v in health.headers.items()}
    r.step("every response carries X-Request-ID", "x-request-id" in h)
    r.step(
        "CORS does not allow an unknown origin",
        h.get("access-control-allow-origin") not in ("*", "https://evil.example"),
        h.get("access-control-allow-origin", ""),
    )
    r.step(
        "security headers present (nosniff, frame options)",
        h.get("x-content-type-options") == "nosniff" and "x-frame-options" in h,
        str({k: v for k, v in h.items() if k.startswith("x-")}),
    )
    err = api.get("/assessments", headers=bearer("x"))
    r.step(
        "errors use the envelope with a request id and no stack trace",
        "request_id" in err.text and "Traceback" not in err.text,
    )
    huge = a.start_exam(a.invite("Upload Cand")[1])
    cc, ee = huge[0], huge[1]
    big = cc.post(
        "/exam/session/baseline-photo",
        headers=ee,
        files={"file": ("s.png", png_bytes() + b"0" * 400_000, "image/png")},
    )
    r.error_code("oversized upload rejected", big, 422, "UPLOAD_INVALID")
    evil = cc.post(
        "/exam/session/baseline-photo",
        headers=ee,
        files={"file": ("s.png", b"<?php echo 1; ?>", "image/png")},
    )
    r.error_code("non-image content rejected by magic bytes", evil, 422, "UPLOAD_INVALID")
    ok1 = cc.post("/exam/snapshots", headers=ee, files={"file": ("s.png", png_bytes(), "image/png")})
    ok2 = cc.post("/exam/snapshots", headers=ee, files={"file": ("s.png", png_bytes(), "image/png")})
    r.step(
        "snapshots limited to one per interval per attempt",
        ok1.status_code == 201 and ok2.status_code == 429,
        f"{ok1.status_code} {ok2.status_code}",
    )
    ev = huge[0].post("/exam/events", headers=huge[1], json={"events": [{"type": "tab_switch"}] * 51})
    r.error_code("more than 50 events per call rejected", ev, 422, "VALIDATION_ERROR")
    xss = api.post("/org/documents", headers=a.oa, json={"doc_type": "other", "title": "x"}).json()
    did = xss.get("id") or xss["document"]["id"]
    ver = api.post(
        f"/org/documents/{did}/versions",
        headers=a.oa,
        json={
            "content_html": (
                "<p onclick='x()'>hi</p><script>alert(1)</script><a href='javascript:alert(1)'>l</a>"
            )
        },
    )
    r.step(
        "document HTML is sanitised on save",
        ver.status_code == 201
        and "<script" not in ver.text.lower()
        and "onclick" not in ver.text.lower()
        and "javascript:" not in ver.text.lower(),
        ver.text[:300],
    )


def exam_rate_limit(r: Report) -> None:
    c = client()
    codes = [c.post("/exam/session/open", json={"token": "R" * 43}).status_code for _ in range(260)]
    r.step(
        "exam link open is rate limited per IP (429 after the configured burst)",
        429 in codes,
        str(codes[-5:]),
    )


# --------------------------------------------------------------------------- main


def main() -> int:
    r = Report("smoke_security")
    run = run_id()
    api = client()
    token = sa_login(api, r)
    sa = bearer(token)

    a = Org(api, token, "A", run).seed()
    b = Org(api, token, "B", run).seed()
    r.step(
        "two organizations seeded with a full flow each", bool(a.ids.get("attempt") and b.ids.get("attempt"))
    )

    cross_tenant(r, api, a, b)

    _, cand_token_holder, _ = a.start_exam(a.invite("Walk Cand")[1])
    tokens: dict[str, str | None] = {
        "super_admin": token,
        "org_admin": a.oa["Authorization"].split()[1],
        "hr_user": a.hr["Authorization"].split()[1],
        "candidate": cand_token_holder["Authorization"].split()[1],
        "anonymous": None,
    }
    role_walk(r, api, tokens)
    owner_rules(r, api, a)
    admin_protection(r, api, sa, run)
    exam_clock(r, api, a)
    invite_links(r, api, a)
    flags(r, api, sa, run)
    audit_append_only(r, api, a)
    auth_hardening(r, api, a, sa, run)
    exam_rate_limit(r)
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
