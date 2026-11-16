"""Live end-to-end smoke test over HTTP (replaces the planned pytest e2e suite).

Flow: super admin -> organization + org admin -> TOTP -> HR user (forced password change) -> consent
document -> assessment with all five question types -> publish -> role -> open -> link -> invite ->
candidate exam (open, system check, consent, baseline photo, start, answers, proctoring events, snapshot,
submit) -> scoring (auto + mock AI) -> review -> decision -> override -> audit export -> dashboard gadgets
-> help-request branch. The M11 report step runs only when the report routes exist.

Usage (target started with scripts/smoke_server.py so AI is a mock and email is captured):
    python scripts/smoke_e2e.py [--base-url http://localhost:8001]
Prints PASS/FAIL per step; exit code 1 when any step failed.
"""

import sys

import httpx
from smoke_lib import (
    DESKTOP_UA,
    STRONG_PASSWORD,
    Report,
    Tenant,
    bearer,
    client,
    login,
    png_bytes,
    question_payloads,
    run_id,
    sa_login,
    totp_code,
)


def main() -> int:
    r = Report("smoke_e2e")
    run = run_id()
    api = client()
    try:
        health = httpx.get(str(api.base_url).split("/api/v1")[0] + "/health", timeout=10)
    except httpx.HTTPError as exc:
        r.step("api reachable", False, str(exc))
        return r.finish()
    r.status("api reachable", health, 200)

    # ------------------------------------------------------------------ platform and tenant setup
    sa_token = sa_login(api, r)
    sa = bearer(sa_token)
    me = api.get("/auth/me", headers=sa)
    r.step(
        "SA /auth/me is the protected default super admin",
        me.status_code == 200 and me.json()["user"]["is_default_super_admin"] is True,
        me.text[:200],
    )

    t = Tenant(api, sa_token, "E2E", run)
    r.step(
        "org + first org admin created with a one-time temporary password",
        bool(t.org_id) and bool(t.admin_temp_password),
    )

    forced = login(api, t.admin_email, t.admin_temp_password)
    r.step(
        "OA first login says must_change_password",
        forced.status_code == 200 and forced.json()["must_change_password"] is True,
        forced.text[:200],
    )
    blocked = api.get("/team/users", headers=bearer(forced.json()["access_token"]))
    r.step(
        "OA blocked from other routes until the password is changed",
        blocked.status_code in (401, 403),
        f"{blocked.status_code} {blocked.text[:200]}",
    )
    weak = api.post(
        "/auth/change-password",
        headers=bearer(forced.json()["access_token"]),
        json={"current_password": t.admin_temp_password, "new_password": "short"},
    )
    r.error_code("weak new password rejected by the policy", weak, 422)
    oa = t.activate_admin()
    r.step("OA changed password and logged in again", bool(oa))

    # TOTP: setup, verify, then login now needs the second factor
    setup = api.post("/auth/totp/setup", headers=oa)
    r.status("OA TOTP setup", setup, 200)
    secret = setup.json()["secret"]
    verify = api.post("/auth/totp/verify", headers=oa, json={"code": totp_code(secret)})
    r.step(
        "OA TOTP verify enables it and returns recovery codes",
        verify.status_code == 200 and verify.json()["enabled"] and len(verify.json()["recovery_codes"]) > 0,
        verify.text[:200],
    )
    challenge = login(api, t.admin_email, t.admin_password)
    r.step(
        "login now returns totp_required without an access token",
        challenge.status_code == 200
        and challenge.json()["totp_required"] is True
        and not challenge.json().get("access_token"),
        challenge.text[:200],
    )
    bad = api.post("/auth/login/totp", json={"totp_token": challenge.json()["totp_token"], "code": "000000"})
    r.step(
        "wrong TOTP code rejected", bad.status_code in (401, 403, 422), f"{bad.status_code} {bad.text[:200]}"
    )
    good = api.post(
        "/auth/login/totp", json={"totp_token": challenge.json()["totp_token"], "code": totp_code(secret)}
    )
    r.status("TOTP login completes", good, 200)
    oa = t.oa_headers = bearer(good.json()["access_token"])

    # HR user created by the OA, forced to change the temporary password
    hr_email = f"hr-e2e-{run}@example.com"
    created = api.post("/team/users", headers=oa, json={"name": "HR One", "email": hr_email})
    r.status("OA creates an HR user", created, 201)
    body = created.json()
    hr_user = body.get("user", body)
    hr_temp = body["temporary_password"]
    r.step("temporary password returned once, user is HR", hr_user["role"] == "hr_user" and bool(hr_temp))
    hr_first = login(api, hr_email, hr_temp)
    r.step("HR first login flagged must_change_password", hr_first.json().get("must_change_password") is True)
    api.post(
        "/auth/change-password",
        headers=bearer(hr_first.json()["access_token"]),
        json={"current_password": hr_temp, "new_password": STRONG_PASSWORD},
    ).raise_for_status()
    hr_login = login(api, hr_email, STRONG_PASSWORD)
    r.status("HR login with the new password", hr_login, 200)
    hr = bearer(hr_login.json()["access_token"])
    me_hr = api.get("/auth/me", headers=hr)
    r.step(
        "HR /auth/me returns the org and effective flags",
        me_hr.status_code == 200
        and me_hr.json()["organization"]["id"] == t.org_id
        and me_hr.json()["flags"]["roles"] is True,
        me_hr.text[:200],
    )

    # org settings used later: AI provider (mock server side) and a consent document
    ai = api.put(
        "/org/settings/ai",
        headers=oa,
        json={"provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-smoke-fake-key-123456"},
    )
    r.step(
        "OA saves AI settings, key is write-only",
        ai.status_code == 200 and "sk-smoke" not in ai.text,
        ai.text[:200],
    )
    doc = api.post("/org/documents", headers=oa, json={"doc_type": "consent", "title": "Exam consent"})
    r.status("OA creates a consent document", doc, 201)
    consent_version_id = None
    if doc.status_code == 201:
        doc_id = doc.json().get("id") or doc.json()["document"]["id"]
        ver = api.post(
            f"/org/documents/{doc_id}/versions",
            headers=oa,
            json={"content_html": "<p>I consent to proctoring.</p>"},
        )
        r.status("OA adds a consent version", ver, 201)
        if ver.status_code == 201:
            consent_version_id = ver.json().get("id") or ver.json()["version"]["id"]
            pub = api.post(f"/org/documents/{doc_id}/versions/{consent_version_id}/publish", headers=oa)
            r.status("OA publishes the consent version", pub, 200)

    # ------------------------------------------------------------------ assessment, role, invite
    assessment = api.post("/assessments", headers=hr, json={"title": f"E2E assessment {run}"})
    r.status("HR creates an assessment", assessment, 201)
    aid = assessment.json()["id"]
    vid = assessment.json()["versions"][0]["id"]
    qpath = f"/assessments/{aid}/versions/{vid}/questions"
    types = []
    for payload in question_payloads():
        resp = api.post(qpath, headers=hr, json=payload)
        types.append(resp.status_code == 201)
    r.step(
        "all 5 question types accepted (single, multi, fill blank, code gap, short answer)",
        all(types),
        str(types),
    )
    patch = api.patch(
        f"/assessments/{aid}/versions/{vid}",
        headers=hr,
        json={"duration_minutes": 30, "pass_mark_percent": 50, "instructions_html": "<p>Read carefully.</p>"},
    )
    r.status("HR sets duration, pass mark and instructions", patch, 200)
    early = api.get(f"/assessments/{aid}/versions/{vid}/preview", headers=hr)
    r.step(
        "preview hides answer keys",
        early.status_code == 200 and "is_correct" not in early.text and "model_answer" not in early.text,
        early.text[:200],
    )
    publish = api.post(f"/assessments/{aid}/versions/{vid}/publish", headers=hr)
    r.status("HR publishes the version", publish, 200)
    frozen = api.patch(f"{qpath}/{publish.json()['questions'][0]['id']}", headers=hr, json={"marks": 5})
    r.step("published version is immutable", frozen.status_code in (409, 422, 403), f"{frozen.status_code}")

    gen = api.post(
        "/assessments/ai/generate",
        headers=hr,
        json={
            "role_title": "Engineer",
            "job_description": "Build backend services.",
            "experience_level": "mid",
            "key_skills": ["python", "sql"],
        },
    )
    r.step(
        "AI question suggestions come from the mock provider",
        gen.status_code == 200 and len(gen.json().get("suggestions", [])) >= 10,
        f"{gen.status_code} {gen.text[:200]}",
    )

    role = api.post("/roles", headers=hr, json={"title": "Backend Engineer", "department": "Eng"})
    r.status("HR creates a role", role, 201)
    rid = role.json()["id"]
    early_invite = api.post(
        f"/roles/{rid}/invites",
        headers=hr,
        json={"candidate_name": "Early", "email": f"early-{run}@example.com"},
    )
    r.error_code("invite blocked while the role is not open and linked", early_invite, 409)
    r.status("HR opens the role", api.post(f"/roles/{rid}/open", headers=hr), 200)
    link = api.put(f"/roles/{rid}/assessment", headers=hr, json={"assessment_version_id": vid})
    r.status("HR links the published version to the role", link, 200)
    cand_email = f"cand-{run}@example.com"
    inv = api.post(
        f"/roles/{rid}/invites", headers=hr, json={"candidate_name": "Cand One", "email": cand_email}
    )
    r.status("HR creates an invite and gets the exam url once", inv, 201)
    token = inv.json()["exam_url"].rsplit("/", 1)[1]
    dup = api.post(
        f"/roles/{rid}/invites", headers=hr, json={"candidate_name": "Cand One", "email": cand_email}
    )
    r.error_code("duplicate invite rejected", dup, 409, "DUPLICATE_INVITE")
    listed = api.get(f"/roles/{rid}/invites", headers=hr)
    r.step("invite list never contains the raw token", listed.status_code == 200 and token not in listed.text)

    # ------------------------------------------------------------------ candidate exam
    cand = client(headers={"User-Agent": DESKTOP_UA})  # own cookie jar = the binding cookie
    opened = cand.post("/exam/session/open", json={"token": token})
    r.status("candidate opens the exam session", opened, 200)
    ex = bearer(opened.json()["session_token"])
    state = cand.get("/exam/session/state", headers=ex)
    r.step(
        "state shows watermark, camera requirement and the consent document",
        state.status_code == 200
        and state.json()["camera_required"] is True
        and bool(state.json()["watermark"])
        and (consent_version_id is None or state.json()["consent_document"] is not None),
        state.text[:300],
    )
    r.error_code(
        "questions blocked before start", cand.get("/exam/questions", headers=ex), 409, "EXAM_NOT_STARTED"
    )
    r.error_code(
        "start blocked before consent", cand.post("/exam/session/start", headers=ex), 409, "CONSENT_REQUIRED"
    )
    narrow = cand.post(
        "/exam/session/system-check",
        headers=ex,
        json={"viewport_w": 500, "viewport_h": 800, "camera_permission": True},
    )
    r.error_code("narrow viewport rejected", narrow, 422, "DEVICE_NOT_SUPPORTED")
    check = cand.post(
        "/exam/session/system-check",
        headers=ex,
        json={"viewport_w": 1280, "viewport_h": 800, "camera_permission": True},
    )
    r.status("system check passes", check, 200)
    consent = cand.post(
        "/exam/session/consent",
        headers=ex,
        json={"document_version_id": consent_version_id} if consent_version_id else {},
    )
    r.status("consent recorded", consent, 200)
    r.error_code(
        "start blocked without the baseline photo",
        cand.post("/exam/session/start", headers=ex),
        409,
        "CAMERA_REQUIRED",
    )
    bad_photo = cand.post(
        "/exam/session/baseline-photo",
        headers=ex,
        files={"file": ("x.png", b"not an image at all", "image/png")},
    )
    r.error_code("non-image baseline photo rejected", bad_photo, 422, "UPLOAD_INVALID")
    photo = cand.post(
        "/exam/session/baseline-photo", headers=ex, files={"file": ("b.png", png_bytes(), "image/png")}
    )
    r.status("baseline photo accepted", photo, 200)
    start = cand.post("/exam/session/start", headers=ex)
    r.step(
        "exam started with a server deadline",
        start.status_code == 200
        and start.json()["status"] == "in_progress"
        and start.json()["remaining_seconds"] > 1700,
        start.text[:200],
    )
    first_deadline = start.json().get("deadline_at")
    again = cand.post("/exam/session/start", headers=ex)
    r.step(
        "repeat start does not move the deadline",
        again.status_code == 200 and again.json()["deadline_at"] == first_deadline,
    )

    qs = cand.get("/exam/questions", headers=ex)
    r.status("questions served", qs, 200)
    questions = {q["type"]: q for q in qs.json()["questions"]}
    r.step(
        "questions carry no answer keys",
        "is_correct" not in qs.text and "answer_key" not in qs.text and "model_answer" not in qs.text,
    )
    single, multi = questions["single_choice"], questions["multi_choice"]
    answers = [
        (single, {"option_id": next(o["id"] for o in single["options"] if o["text_html"] == "4")}),
        (multi, {"option_ids": [o["id"] for o in multi["options"] if o["text_html"] in ("2", "4")]}),
        (questions["fill_blank"], {"blanks": ["paris", "Rome"]}),
        (questions["code_gap"], {"gaps": ["range"]}),
        (questions["short_answer"], {"text": "An index speeds up lookups using a sorted structure."}),
    ]
    saved = [
        cand.put(f"/exam/answers/{q['id']}", headers=ex, json={"answer": a}).status_code == 200
        for q, a in answers
    ]
    r.step("auto-save accepted for every question type", all(saved), str(saved))
    hb = cand.post("/exam/heartbeat", headers=ex)
    r.step(
        "heartbeat returns server-computed remaining seconds",
        hb.status_code == 200
        and 0 < hb.json()["remaining_seconds"] <= 1800
        and hb.json()["time_over"] is False,
        hb.text[:200],
    )
    events = cand.post(
        "/exam/events",
        headers=ex,
        json={
            "events": [
                {"type": "tab_switch", "duration_ms": 3000, "severity": "info"},  # client severity is ignored
                {"type": "copy_attempt"},
                {"type": "multiple_faces"},
            ]
        },
    )
    r.step(
        "proctoring events ingested",
        events.status_code == 200 and events.json()["accepted"] == 3,
        events.text[:200],
    )
    snap = cand.post("/exam/snapshots", headers=ex, files={"file": ("s.png", png_bytes(), "image/png")})
    r.status("snapshot uploaded", snap, 201)
    submit = cand.post("/exam/submit", headers=ex)
    r.step(
        "candidate submits",
        submit.status_code == 200 and submit.json()["status"] == "submitted",
        submit.text[:200],
    )
    r.step("submit is idempotent", cand.post("/exam/submit", headers=ex).status_code == 200)
    r.error_code(
        "answers locked after submit",
        cand.put(
            f"/exam/answers/{single['id']}",
            headers=ex,
            json={"answer": {"option_id": single["options"][0]["id"]}},
        ),
        409,
        "EXAM_ALREADY_SUBMITTED",
    )
    reuse = client(headers={"User-Agent": DESKTOP_UA}).post("/exam/session/open", json={"token": token})
    r.step(
        "link cannot be reopened from another browser after submit",
        reuse.status_code in (403, 409),
        f"{reuse.status_code} {reuse.text[:200]}",
    )

    # ------------------------------------------------------------------ scoring and review
    attempts = api.get(f"/roles/{rid}/attempts", headers=hr)
    r.status("HR lists attempts", attempts, 200)
    row = attempts.json()["items"][0]
    attempt_id = row["id"]
    r.step(
        "auto-scored on submit, AI answer pending (deferred)",
        row["scoring_status"] == "pending" and row["score_percent"] is not None,
        str(row),
    )
    rescore = api.post(f"/attempts/{attempt_id}/rescore", headers=hr)
    r.step(
        "HR rescore runs the mock AI provider",
        rescore.status_code == 200 and rescore.json()["scoring_status"] == "complete",
        rescore.text[:200],
    )
    detail = api.get(f"/attempts/{attempt_id}", headers=hr)
    r.status("HR opens the full attempt report", detail, 200)
    rep = detail.json()
    short = next(b for b in rep["breakdown"] if b["type"] == "short_answer")
    r.step(
        "AI marks and reason stored beside the answer",
        short["ai_marks"] == 2.0 and bool(short["ai_reason"]),
        str(short)[:300],
    )
    r.step("auto marks correct (8 of 8 auto-graded)", rep["auto_score"] == 8.0, str(rep["auto_score"]))
    r.step(
        "flag counts and timeline reflect server-assigned severities",
        rep["flag_counts"]["severity"].get("serious") == 1 and len(rep["timeline"]) >= 3,
        str(rep["flag_counts"]),
    )
    override = api.patch(
        f"/attempts/{attempt_id}/answers/{short['question_id']}/score",
        headers=hr,
        json={"marks": 3, "reason": "Reviewed manually"},
    )
    r.step(
        "HR overrides a score with a reason",
        override.status_code == 200 and override.json()["score_percent"] == 91.67,
        override.text[:200],
    )
    ids = rep.get("snapshot_asset_ids") or []
    if ids:
        url = api.get(f"/attempts/{attempt_id}/snapshots/{ids[0]}/url", headers=hr)
        r.status("HR gets a signed snapshot url", url, 200)
        if url.status_code == 200:
            path = "/" + url.json()["url"].split("/api/v1/", 1)[1]
            file = api.get(path)
            r.step(
                "signed url serves the decrypted image",
                file.status_code == 200 and file.content[:4] == b"\x89PNG",
                f"{file.status_code}",
            )
    else:
        r.step("snapshot present on the attempt", False, "no snapshot_asset_ids")

    decision = api.post(
        f"/attempts/{attempt_id}/decision", headers=hr, json={"decision": "approved", "note": "Good"}
    )
    r.status("owner HR decides", decision, 201)
    r.error_code(
        "second HR decision refused",
        api.post(f"/attempts/{attempt_id}/decision", headers=hr, json={"decision": "rejected"}),
        409,
        "DECISION_ALREADY_MADE",
    )
    over = api.post(
        f"/attempts/{attempt_id}/decision/override",
        headers=oa,
        json={"decision": "rejected", "reason": "Policy check"},
    )
    r.status("OA overrides the decision with a reason", over, 201)
    hist = api.get(f"/attempts/{attempt_id}/decision-history", headers=hr)
    r.step("decision history keeps both rows", hist.status_code == 200 and len(hist.json()) == 2)

    # ------------------------------------------------------------------ help-request branch
    inv2 = api.post(
        f"/roles/{rid}/invites",
        headers=hr,
        json={"candidate_name": "Cand Two", "email": f"cand2-{run}@example.com"},
    )
    token2 = inv2.json()["exam_url"].rsplit("/", 1)[1]
    c2 = client(headers={"User-Agent": DESKTOP_UA})
    ex2 = bearer(c2.post("/exam/session/open", json={"token": token2}).json()["session_token"])
    c2.post(
        "/exam/session/system-check",
        headers=ex2,
        json={"viewport_w": 1280, "viewport_h": 800, "camera_permission": True},
    )
    c2.post(
        "/exam/session/consent",
        headers=ex2,
        json={"document_version_id": consent_version_id} if consent_version_id else {},
    )
    c2.post("/exam/session/baseline-photo", headers=ex2, files={"file": ("b.png", png_bytes(), "image/png")})
    help_req = c2.post(
        "/exam/help-requests", headers=ex2, json={"reason": "extra_time", "note": "need longer"}
    )
    r.status("candidate raises a help request", help_req, 201)
    r.error_code(
        "start blocked while the help request is open",
        c2.post("/exam/session/start", headers=ex2),
        409,
        "HELP_REQUEST_PENDING",
    )
    inbox = api.get("/help-requests?status=open", headers=hr)
    r.step(
        "HR sees it in the inbox", inbox.status_code == 200 and inbox.json()["total"] >= 1, inbox.text[:200]
    )
    hid = inbox.json()["items"][0]["id"]
    approve = api.post(f"/help-requests/{hid}/approve", headers=hr, json={"extra_minutes": 10, "note": "ok"})
    r.status("HR approves the help request with extra minutes", approve, 200)
    start2 = c2.post("/exam/session/start", headers=ex2)
    r.step(
        "candidate starts with the approved extra time",
        start2.status_code == 200 and start2.json()["remaining_seconds"] > 1800 + 500,
        start2.text[:200],
    )

    # ------------------------------------------------------------------ audit export and dashboard
    logs = api.get("/audit-logs?page_size=100", headers=oa)
    r.status("OA reads the audit log", logs, 200)
    actions = {row["action"] for row in logs.json()["items"]}
    wanted = {"DECISION_MADE", "DECISION_OVERRIDDEN", "VERSION_PUBLISHED", "INVITE_CREATED"}
    r.step("audit carries the flow's actions", wanted <= actions, f"missing {sorted(wanted - actions)}")
    export = api.post("/audit-logs/export", headers=oa, json={"format": "csv", "filters": {}})
    r.step(
        "audit CSV export",
        export.status_code == 200
        and export.headers["content-type"].startswith("text/csv")
        and b"DECISION_MADE" in export.content,
        f"{export.status_code} {export.headers.get('content-type')}",
    )
    after = api.get("/audit-logs?page_size=20", headers=oa)
    r.step(
        "the export itself is audited",
        any(a["action"] == "AUDIT_EXPORTED" for a in after.json()["items"]),
        str({a["action"] for a in after.json()["items"]}),
    )
    platform_export = api.post(
        "/platform/audit-logs/export", headers=sa, json={"format": "csv", "filters": {}}
    )
    r.step(
        "SA cross-org audit CSV export",
        platform_export.status_code == 200 and platform_export.headers["content-type"].startswith("text/csv"),
        str(platform_export.status_code),
    )

    for who, headers in (("OA", oa), ("HR", hr), ("SA", sa)):
        cat = api.get("/dashboard/gadgets/catalog", headers=headers)
        r.status(f"{who} gadget catalog", cat, 200)
        bad_keys = []
        for item in cat.json()["items"]:
            data = api.get(f"/dashboard/gadgets/{item['key']}/data", headers=headers)
            if data.status_code != 200:
                bad_keys.append(f"{item['key']}={data.status_code}")
        r.step(
            f"{who}: every catalog gadget returns data ({len(cat.json()['items'])})",
            not bad_keys,
            ", ".join(bad_keys),
        )
    layout = api.put(
        "/dashboard/layout", headers=hr, json={"gadgets": [{"key": "summary_counts", "size": "lg"}]}
    )
    r.status("HR saves a dashboard layout", layout, 200)
    r.error_code("unknown gadget key is 404", api.get("/dashboard/gadgets/nope/data", headers=hr), 404)
    count = api.get("/notifications/unread-count", headers=hr)
    r.step(
        "HR has notifications (submitted attempt, serious flag)",
        count.status_code == 200 and count.json().get("count", count.json().get("unread", 0)) >= 1,
        count.text[:200],
    )

    # ------------------------------------------------------------------ M11 reports (only when landed)
    spec = httpx.get(str(api.base_url).split("/api/v1")[0] + "/openapi.json", timeout=30).json()
    if "/api/v1/reports/catalog" in spec["paths"]:
        cat = api.get("/reports/catalog", headers=oa)
        r.step(
            "M11 report catalog lists sources without rows",
            cat.status_code == 200 and any(s["source"] == "candidates" for s in cat.json()["sources"]),
            cat.text[:200],
        )
        interp = api.post("/reports/chat/interpret", headers=oa, json={"text": "show me rejected candidates"})
        r.step(
            "M11 interpret (mock AI) returns a validated interpretation with a counted estimate",
            interp.status_code == 200
            and len(interp.json()["interpretations"]) >= 1
            and interp.json()["interpretations"][0]["estimated_rows"] is not None,
            interp.text[:300],
        )
        report_id = interp.json()["report"]["id"] if interp.status_code == 200 else None
        if report_id:
            sel = api.post("/reports/chat/select", headers=oa, json={"id": report_id, "index": 0})
            r.step(
                "M11 select confirms the interpretation",
                sel.status_code == 200 and sel.json()["report"]["status"] == "confirmed",
                sel.text[:200],
            )
            cnt = api.post(f"/reports/{report_id}/preview-count", headers=oa)
            r.step(
                "M11 preview-count: the rejected (overridden) candidate is counted",
                cnt.status_code == 200 and cnt.json()["row_count"] >= 1,
                cnt.text[:200],
            )
            gen = api.post(f"/reports/{report_id}/generate", headers=oa, json={"format": "csv"})
            r.step(
                "M11 generate CSV",
                gen.status_code == 200
                and gen.json()["status"] == "generated"
                and gen.json()["row_count"] >= 1,
                gen.text[:200],
            )
            dl = api.get(f"/reports/{report_id}/download", headers=oa)
            r.step(
                "M11 download returns the CSV with the decrypted candidate name",
                dl.status_code == 200 and b"Cand One" in dl.content,
                f"{dl.status_code} {dl.content[:200]!r}",
            )
            hist = api.get("/reports", headers=oa)
            r.step("M11 history lists the request", hist.status_code == 200 and hist.json()["total"] >= 1)
            audit = api.get("/audit-logs?page_size=50", headers=oa).json()["items"]
            got = {a["action"] for a in audit}
            r.step(
                "M11 interpret, generate and download are audited",
                {"REPORT_INTERPRETED", "REPORT_GENERATED", "REPORT_DOWNLOADED"} <= got,
                str(got),
            )
        r.error_code("HR cannot use the report engine", api.get("/reports/catalog", headers=hr), 403)
        sa_cat = api.get("/platform/reports/catalog", headers=sa)
        r.status("M11 SA report catalog (aggregates only)", sa_cat, 200)
        if sa_cat.status_code == 200:
            src = sa_cat.json()["sources"][0]
            no_ai = api.post("/platform/reports/chat/interpret", headers=sa, json={"text": "anything"})
            r.error_code("SA free text is refused: the platform has no AI", no_ai, 409, "AI_NOT_CONFIGURED")
            spec_in = api.post(
                "/platform/reports/chat/interpret",
                headers=sa,
                json={
                    "filter_spec": {"source": src["source"], "filters": [], "columns": src["default_columns"]}
                },
            )
            r.status("SA structured filter_spec interpreted without AI", spec_in, 200)
            if spec_in.status_code == 200:
                sid = spec_in.json()["report"]["id"]
                api.post("/platform/reports/chat/select", headers=sa, json={"id": sid, "index": 0})
                sgen = api.post(f"/platform/reports/{sid}/generate", headers=sa, json={"format": "csv"})
                r.status("SA generates a CSV report", sgen, 200)
                sdl = api.get(f"/platform/reports/{sid}/download", headers=sa)
                r.step(
                    "SA downloads it, no candidate content",
                    sdl.status_code == 200 and b"Cand One" not in sdl.content,
                    str(sdl.status_code),
                )
    else:
        r.skip("M11 reports", "report routes are not in /openapi.json yet")

    return r.finish()
    r.status("api reachable", health, 200)

    # ------------------------------------------------------------------ platform and tenant setup
    sa_token = sa_login(api, r)
    sa = bearer(sa_token)
    me = api.get("/auth/me", headers=sa)
    r.step(
        "SA /auth/me is the protected default super admin",
        me.status_code == 200 and me.json()["user"]["is_default_super_admin"] is True,
        me.text[:200],
    )

    t = Tenant(api, sa_token, "E2E", run)
    r.step(
        "org + first org admin created with a one-time temporary password",
        bool(t.org_id) and bool(t.admin_temp_password),
    )

    forced = login(api, t.admin_email, t.admin_temp_password)
    r.step(
        "OA first login says must_change_password",
        forced.status_code == 200 and forced.json()["must_change_password"] is True,
        forced.text[:200],
    )
    blocked = api.get("/team/users", headers=bearer(forced.json()["access_token"]))
    r.step(
        "OA blocked from other routes until the password is changed",
        blocked.status_code in (401, 403),
        f"{blocked.status_code} {blocked.text[:200]}",
    )
    weak = api.post(
        "/auth/change-password",
        headers=bearer(forced.json()["access_token"]),
        json={"current_password": t.admin_temp_password, "new_password": "short"},
    )
    r.error_code("weak new password rejected by the policy", weak, 422)
    oa = t.activate_admin()
    r.step("OA changed password and logged in again", bool(oa))

    # TOTP: setup, verify, then login now needs the second factor
    setup = api.post("/auth/totp/setup", headers=oa)
    r.status("OA TOTP setup", setup, 200)
    secret = setup.json()["secret"]
    verify = api.post("/auth/totp/verify", headers=oa, json={"code": totp_code(secret)})
    r.step(
        "OA TOTP verify enables it and returns recovery codes",
        verify.status_code == 200 and verify.json()["enabled"] and len(verify.json()["recovery_codes"]) > 0,
        verify.text[:200],
    )
    challenge = login(api, t.admin_email, t.admin_password)
    r.step(
        "login now returns totp_required without an access token",
        challenge.status_code == 200
        and challenge.json()["totp_required"] is True
        and not challenge.json().get("access_token"),
        challenge.text[:200],
    )
    bad = api.post("/auth/login/totp", json={"totp_token": challenge.json()["totp_token"], "code": "000000"})
    r.step(
        "wrong TOTP code rejected", bad.status_code in (401, 403, 422), f"{bad.status_code} {bad.text[:200]}"
    )
    good = api.post(
        "/auth/login/totp", json={"totp_token": challenge.json()["totp_token"], "code": totp_code(secret)}
    )
    r.status("TOTP login completes", good, 200)
    oa = t.oa_headers = bearer(good.json()["access_token"])

    # HR user created by the OA, forced to change the temporary password
    hr_email = f"hr-e2e-{run}@example.com"
    created = api.post("/team/users", headers=oa, json={"name": "HR One", "email": hr_email})
    r.status("OA creates an HR user", created, 201)
    body = created.json()
    hr_user = body.get("user", body)
    hr_temp = body["temporary_password"]
    r.step("temporary password returned once, user is HR", hr_user["role"] == "hr_user" and bool(hr_temp))
    hr_first = login(api, hr_email, hr_temp)
    r.step("HR first login flagged must_change_password", hr_first.json().get("must_change_password") is True)
    api.post(
        "/auth/change-password",
        headers=bearer(hr_first.json()["access_token"]),
        json={"current_password": hr_temp, "new_password": STRONG_PASSWORD},
    ).raise_for_status()
    hr_login = login(api, hr_email, STRONG_PASSWORD)
    r.status("HR login with the new password", hr_login, 200)
    hr = bearer(hr_login.json()["access_token"])
    me_hr = api.get("/auth/me", headers=hr)
    r.step(
        "HR /auth/me returns the org and effective flags",
        me_hr.status_code == 200
        and me_hr.json()["organization"]["id"] == t.org_id
        and me_hr.json()["flags"]["roles"] is True,
        me_hr.text[:200],
    )

    # org settings used later: AI provider (mock server side) and a consent document
    ai = api.put(
        "/org/settings/ai",
        headers=oa,
        json={"provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-smoke-fake-key-123456"},
    )
    r.step(
        "OA saves AI settings, key is write-only",
        ai.status_code == 200 and "sk-smoke" not in ai.text,
        ai.text[:200],
    )
    doc = api.post("/org/documents", headers=oa, json={"doc_type": "consent", "title": "Exam consent"})
    r.status("OA creates a consent document", doc, 201)
    consent_version_id = None
    if doc.status_code == 201:
        doc_id = doc.json().get("id") or doc.json()["document"]["id"]
        ver = api.post(
            f"/org/documents/{doc_id}/versions",
            headers=oa,
            json={"content_html": "<p>I consent to proctoring.</p>"},
        )
        r.status("OA adds a consent version", ver, 201)
        if ver.status_code == 201:
            consent_version_id = ver.json().get("id") or ver.json()["version"]["id"]
            pub = api.post(f"/org/documents/{doc_id}/versions/{consent_version_id}/publish", headers=oa)
            r.status("OA publishes the consent version", pub, 200)

    # ------------------------------------------------------------------ assessment, role, invite
    assessment = api.post("/assessments", headers=hr, json={"title": f"E2E assessment {run}"})
    r.status("HR creates an assessment", assessment, 201)
    aid = assessment.json()["id"]
    vid = assessment.json()["versions"][0]["id"]
    qpath = f"/assessments/{aid}/versions/{vid}/questions"
    types = []
    for payload in question_payloads():
        resp = api.post(qpath, headers=hr, json=payload)
        types.append(resp.status_code == 201)
    r.step(
        "all 5 question types accepted (single, multi, fill blank, code gap, short answer)",
        all(types),
        str(types),
    )
    patch = api.patch(
        f"/assessments/{aid}/versions/{vid}",
        headers=hr,
        json={"duration_minutes": 30, "pass_mark_percent": 50, "instructions_html": "<p>Read carefully.</p>"},
    )
    r.status("HR sets duration, pass mark and instructions", patch, 200)
    early = api.get(f"/assessments/{aid}/versions/{vid}/preview", headers=hr)
    r.step(
        "preview hides answer keys",
        early.status_code == 200 and "is_correct" not in early.text and "model_answer" not in early.text,
        early.text[:200],
    )
    publish = api.post(f"/assessments/{aid}/versions/{vid}/publish", headers=hr)
    r.status("HR publishes the version", publish, 200)
    frozen = api.patch(f"{qpath}/{publish.json()['questions'][0]['id']}", headers=hr, json={"marks": 5})
    r.step("published version is immutable", frozen.status_code in (409, 422, 403), f"{frozen.status_code}")

    gen = api.post(
        "/assessments/ai/generate",
        headers=hr,
        json={
            "role_title": "Engineer",
            "job_description": "Build backend services.",
            "experience_level": "mid",
            "key_skills": ["python", "sql"],
        },
    )
    r.step(
        "AI question suggestions come from the mock provider",
        gen.status_code == 200 and len(gen.json().get("suggestions", [])) >= 10,
        f"{gen.status_code} {gen.text[:200]}",
    )

    role = api.post("/roles", headers=hr, json={"title": "Backend Engineer", "department": "Eng"})
    r.status("HR creates a role", role, 201)
    rid = role.json()["id"]
    early_invite = api.post(
        f"/roles/{rid}/invites",
        headers=hr,
        json={"candidate_name": "Early", "email": f"early-{run}@example.com"},
    )
    r.error_code("invite blocked while the role is not open and linked", early_invite, 409)
    r.status("HR opens the role", api.post(f"/roles/{rid}/open", headers=hr), 200)
    link = api.put(f"/roles/{rid}/assessment", headers=hr, json={"assessment_version_id": vid})
    r.status("HR links the published version to the role", link, 200)
    cand_email = f"cand-{run}@example.com"
    inv = api.post(
        f"/roles/{rid}/invites", headers=hr, json={"candidate_name": "Cand One", "email": cand_email}
    )
    r.status("HR creates an invite and gets the exam url once", inv, 201)
    token = inv.json()["exam_url"].rsplit("/", 1)[1]
    dup = api.post(
        f"/roles/{rid}/invites", headers=hr, json={"candidate_name": "Cand One", "email": cand_email}
    )
    r.error_code("duplicate invite rejected", dup, 409, "DUPLICATE_INVITE")
    listed = api.get(f"/roles/{rid}/invites", headers=hr)
    r.step("invite list never contains the raw token", listed.status_code == 200 and token not in listed.text)

    # ------------------------------------------------------------------ candidate exam
    cand = client(headers={"User-Agent": DESKTOP_UA})  # own cookie jar = the binding cookie
    opened = cand.post("/exam/session/open", json={"token": token})
    r.status("candidate opens the exam session", opened, 200)
    ex = bearer(opened.json()["session_token"])
    state = cand.get("/exam/session/state", headers=ex)
    r.step(
        "state shows watermark, camera requirement and the consent document",
        state.status_code == 200
        and state.json()["camera_required"] is True
        and bool(state.json()["watermark"])
        and (consent_version_id is None or state.json()["consent_document"] is not None),
        state.text[:300],
    )
    r.error_code(
        "questions blocked before start", cand.get("/exam/questions", headers=ex), 409, "EXAM_NOT_STARTED"
    )
    r.error_code(
        "start blocked before consent", cand.post("/exam/session/start", headers=ex), 409, "CONSENT_REQUIRED"
    )
    narrow = cand.post(
        "/exam/session/system-check",
        headers=ex,
        json={"viewport_w": 500, "viewport_h": 800, "camera_permission": True},
    )
    r.error_code("narrow viewport rejected", narrow, 422, "DEVICE_NOT_SUPPORTED")
    check = cand.post(
        "/exam/session/system-check",
        headers=ex,
        json={"viewport_w": 1280, "viewport_h": 800, "camera_permission": True},
    )
    r.status("system check passes", check, 200)
    consent = cand.post(
        "/exam/session/consent",
        headers=ex,
        json={"document_version_id": consent_version_id} if consent_version_id else {},
    )
    r.status("consent recorded", consent, 200)
    r.error_code(
        "start blocked without the baseline photo",
        cand.post("/exam/session/start", headers=ex),
        409,
        "CAMERA_REQUIRED",
    )
    bad_photo = cand.post(
        "/exam/session/baseline-photo",
        headers=ex,
        files={"file": ("x.png", b"not an image at all", "image/png")},
    )
    r.error_code("non-image baseline photo rejected", bad_photo, 422, "UPLOAD_INVALID")
    photo = cand.post(
        "/exam/session/baseline-photo", headers=ex, files={"file": ("b.png", png_bytes(), "image/png")}
    )
    r.status("baseline photo accepted", photo, 200)
    start = cand.post("/exam/session/start", headers=ex)
    r.step(
        "exam started with a server deadline",
        start.status_code == 200
        and start.json()["status"] == "in_progress"
        and start.json()["remaining_seconds"] > 1700,
        start.text[:200],
    )
    first_deadline = start.json().get("deadline_at")
    again = cand.post("/exam/session/start", headers=ex)
    r.step(
        "repeat start does not move the deadline",
        again.status_code == 200 and again.json()["deadline_at"] == first_deadline,
    )

    qs = cand.get("/exam/questions", headers=ex)
    r.status("questions served", qs, 200)
    questions = {q["type"]: q for q in qs.json()["questions"]}
    r.step(
        "questions carry no answer keys",
        "is_correct" not in qs.text and "answer_key" not in qs.text and "model_answer" not in qs.text,
    )
    single, multi = questions["single_choice"], questions["multi_choice"]
    answers = [
        (single, {"option_id": next(o["id"] for o in single["options"] if o["text_html"] == "4")}),
        (multi, {"option_ids": [o["id"] for o in multi["options"] if o["text_html"] in ("2", "4")]}),
        (questions["fill_blank"], {"blanks": ["paris", "Rome"]}),
        (questions["code_gap"], {"gaps": ["range"]}),
        (questions["short_answer"], {"text": "An index speeds up lookups using a sorted structure."}),
    ]
    saved = [
        cand.put(f"/exam/answers/{q['id']}", headers=ex, json={"answer": a}).status_code == 200
        for q, a in answers
    ]
    r.step("auto-save accepted for every question type", all(saved), str(saved))
    hb = cand.post("/exam/heartbeat", headers=ex)
    r.step(
        "heartbeat returns server-computed remaining seconds",
        hb.status_code == 200
        and 0 < hb.json()["remaining_seconds"] <= 1800
        and hb.json()["time_over"] is False,
        hb.text[:200],
    )
    events = cand.post(
        "/exam/events",
        headers=ex,
        json={
            "events": [
                {"type": "tab_switch", "duration_ms": 3000, "severity": "info"},  # client severity is ignored
                {"type": "copy_attempt"},
                {"type": "multiple_faces"},
            ]
        },
    )
    r.step(
        "proctoring events ingested",
        events.status_code == 200 and events.json()["accepted"] == 3,
        events.text[:200],
    )
    snap = cand.post("/exam/snapshots", headers=ex, files={"file": ("s.png", png_bytes(), "image/png")})
    r.status("snapshot uploaded", snap, 201)
    submit = cand.post("/exam/submit", headers=ex)
    r.step(
        "candidate submits",
        submit.status_code == 200 and submit.json()["status"] == "submitted",
        submit.text[:200],
    )
    r.step("submit is idempotent", cand.post("/exam/submit", headers=ex).status_code == 200)
    r.error_code(
        "answers locked after submit",
        cand.put(
            f"/exam/answers/{single['id']}",
            headers=ex,
            json={"answer": {"option_id": single["options"][0]["id"]}},
        ),
        409,
        "EXAM_ALREADY_SUBMITTED",
    )
    reuse = client(headers={"User-Agent": DESKTOP_UA}).post("/exam/session/open", json={"token": token})
    r.step(
        "link cannot be reopened from another browser after submit",
        reuse.status_code in (403, 409),
        f"{reuse.status_code} {reuse.text[:200]}",
    )

    # ------------------------------------------------------------------ scoring and review
    attempts = api.get(f"/roles/{rid}/attempts", headers=hr)
    r.status("HR lists attempts", attempts, 200)
    row = attempts.json()["items"][0]
    attempt_id = row["id"]
    r.step(
        "auto-scored on submit, AI answer pending (deferred)",
        row["scoring_status"] == "pending" and row["score_percent"] is not None,
        str(row),
    )
    rescore = api.post(f"/attempts/{attempt_id}/rescore", headers=hr)
    r.step(
        "HR rescore runs the mock AI provider",
        rescore.status_code == 200 and rescore.json()["scoring_status"] == "complete",
        rescore.text[:200],
    )
    detail = api.get(f"/attempts/{attempt_id}", headers=hr)
    r.status("HR opens the full attempt report", detail, 200)
    rep = detail.json()
    short = next(b for b in rep["breakdown"] if b["type"] == "short_answer")
    r.step(
        "AI marks and reason stored beside the answer",
        short["ai_marks"] == 2.0 and bool(short["ai_reason"]),
        str(short)[:300],
    )
    r.step("auto marks correct (8 of 8 auto-graded)", rep["auto_score"] == 8.0, str(rep["auto_score"]))
    r.step(
        "flag counts and timeline reflect server-assigned severities",
        rep["flag_counts"]["severity"].get("serious") == 1 and len(rep["timeline"]) >= 3,
        str(rep["flag_counts"]),
    )
    override = api.patch(
        f"/attempts/{attempt_id}/answers/{short['question_id']}/score",
        headers=hr,
        json={"marks": 3, "reason": "Reviewed manually"},
    )
    r.step(
        "HR overrides a score with a reason",
        override.status_code == 200 and override.json()["score_percent"] == 91.67,
        override.text[:200],
    )
    ids = rep.get("snapshot_asset_ids") or []
    if ids:
        url = api.get(f"/attempts/{attempt_id}/snapshots/{ids[0]}/url", headers=hr)
        r.status("HR gets a signed snapshot url", url, 200)
        if url.status_code == 200:
            path = "/" + url.json()["url"].split("/api/v1/", 1)[1]
            file = api.get(path)
            r.step(
                "signed url serves the decrypted image",
                file.status_code == 200 and file.content[:4] == b"\x89PNG",
                f"{file.status_code}",
            )
    else:
        r.step("snapshot present on the attempt", False, "no snapshot_asset_ids")

    decision = api.post(
        f"/attempts/{attempt_id}/decision", headers=hr, json={"decision": "approved", "note": "Good"}
    )
    r.status("owner HR decides", decision, 201)
    r.error_code(
        "second HR decision refused",
        api.post(f"/attempts/{attempt_id}/decision", headers=hr, json={"decision": "rejected"}),
        409,
        "DECISION_ALREADY_MADE",
    )
    over = api.post(
        f"/attempts/{attempt_id}/decision/override",
        headers=oa,
        json={"decision": "rejected", "reason": "Policy check"},
    )
    r.status("OA overrides the decision with a reason", over, 201)
    hist = api.get(f"/attempts/{attempt_id}/decision-history", headers=hr)
    r.step("decision history keeps both rows", hist.status_code == 200 and len(hist.json()) == 2)

    # ------------------------------------------------------------------ help-request branch
    inv2 = api.post(
        f"/roles/{rid}/invites",
        headers=hr,
        json={"candidate_name": "Cand Two", "email": f"cand2-{run}@example.com"},
    )
    token2 = inv2.json()["exam_url"].rsplit("/", 1)[1]
    c2 = client(headers={"User-Agent": DESKTOP_UA})
    ex2 = bearer(c2.post("/exam/session/open", json={"token": token2}).json()["session_token"])
    c2.post(
        "/exam/session/system-check",
        headers=ex2,
        json={"viewport_w": 1280, "viewport_h": 800, "camera_permission": True},
    )
    c2.post(
        "/exam/session/consent",
        headers=ex2,
        json={"document_version_id": consent_version_id} if consent_version_id else {},
    )
    c2.post("/exam/session/baseline-photo", headers=ex2, files={"file": ("b.png", png_bytes(), "image/png")})
    help_req = c2.post(
        "/exam/help-requests", headers=ex2, json={"reason": "extra_time", "note": "need longer"}
    )
    r.status("candidate raises a help request", help_req, 201)
    r.error_code(
        "start blocked while the help request is open",
        c2.post("/exam/session/start", headers=ex2),
        409,
        "HELP_REQUEST_PENDING",
    )
    inbox = api.get("/help-requests?status=open", headers=hr)
    r.step(
        "HR sees it in the inbox", inbox.status_code == 200 and inbox.json()["total"] >= 1, inbox.text[:200]
    )
    hid = inbox.json()["items"][0]["id"]
    approve = api.post(f"/help-requests/{hid}/approve", headers=hr, json={"extra_minutes": 10, "note": "ok"})
    r.status("HR approves the help request with extra minutes", approve, 200)
    start2 = c2.post("/exam/session/start", headers=ex2)
    r.step(
        "candidate starts with the approved extra time",
        start2.status_code == 200 and start2.json()["remaining_seconds"] > 1800 + 500,
        start2.text[:200],
    )

    # ------------------------------------------------------------------ audit export and dashboard
    logs = api.get("/audit-logs?page_size=100", headers=oa)
    r.status("OA reads the audit log", logs, 200)
    actions = {row["action"] for row in logs.json()["items"]}
    wanted = {"DECISION_MADE", "DECISION_OVERRIDDEN", "VERSION_PUBLISHED", "INVITE_CREATED"}
    r.step("audit carries the flow's actions", wanted <= actions, f"missing {sorted(wanted - actions)}")
    export = api.post("/audit-logs/export", headers=oa, json={"format": "csv", "filters": {}})
    r.step(
        "audit CSV export",
        export.status_code == 200
        and export.headers["content-type"].startswith("text/csv")
        and b"DECISION_MADE" in export.content,
        f"{export.status_code} {export.headers.get('content-type')}",
    )
    after = api.get("/audit-logs?page_size=20", headers=oa)
    r.step(
        "the export itself is audited",
        any(a["action"] == "AUDIT_EXPORTED" for a in after.json()["items"]),
        str({a["action"] for a in after.json()["items"]}),
    )
    platform_export = api.post(
        "/platform/audit-logs/export", headers=sa, json={"format": "csv", "filters": {}}
    )
    r.step(
        "SA cross-org audit CSV export",
        platform_export.status_code == 200 and platform_export.headers["content-type"].startswith("text/csv"),
        str(platform_export.status_code),
    )

    for who, headers in (("OA", oa), ("HR", hr), ("SA", sa)):
        cat = api.get("/dashboard/gadgets/catalog", headers=headers)
        r.status(f"{who} gadget catalog", cat, 200)
        bad_keys = []
        for item in cat.json()["items"]:
            data = api.get(f"/dashboard/gadgets/{item['key']}/data", headers=headers)
            if data.status_code != 200:
                bad_keys.append(f"{item['key']}={data.status_code}")
        r.step(
            f"{who}: every catalog gadget returns data ({len(cat.json()['items'])})",
            not bad_keys,
            ", ".join(bad_keys),
        )
    layout = api.put(
        "/dashboard/layout", headers=hr, json={"gadgets": [{"key": "summary_counts", "size": "lg"}]}
    )
    r.status("HR saves a dashboard layout", layout, 200)
    r.error_code("unknown gadget key is 404", api.get("/dashboard/gadgets/nope/data", headers=hr), 404)
    count = api.get("/notifications/unread-count", headers=hr)
    r.step(
        "HR has notifications (submitted attempt, serious flag)",
        count.status_code == 200 and count.json().get("count", count.json().get("unread", 0)) >= 1,
        count.text[:200],
    )

    # ------------------------------------------------------------------ M11 reports (only when landed)
    spec = httpx.get(str(api.base_url).split("/api/v1")[0] + "/openapi.json", timeout=30).json()
    if "/api/v1/reports/catalog" in spec["paths"]:
        cat = api.get("/reports/catalog", headers=oa)
        r.status("M11 report catalog", cat, 200)
        interp = api.post("/reports/chat/interpret", headers=oa, json={"text": "rejected candidates"})
        r.step(
            "M11 interpret returns interpretations or a clarifying question",
            interp.status_code == 200,
            interp.text[:300],
        )
    else:
        r.skip("M11 reports", "report routes are not in /openapi.json yet")

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
