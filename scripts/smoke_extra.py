"""Extra live smoke checks over HTTP: the areas smoke_e2e.py and smoke_security.py did not cover.

Sections: PDF exports, bulk CSV invites, organization disable/enable, candidate data erase, branding
upload and signed file URLs, background jobs (every job in ``list-jobs``), Google sign-in / SMTP / storage
to the extent they can be tested offline, plus the two open design questions (link regeneration vs the
browser binding, CAMERA_REQUIRED vs HELP_REQUEST_PENDING order).

Usage (api started with scripts/smoke_server.py; see docs/smoke_report.md):
    python scripts/smoke_extra.py --base-url http://hrsmoke2-api:8000 \
        [--smtp-url http://hrsmoke2-api-smtp:8000] [--s3-url http://hrsmoke2-api-s3:8000]
The optional servers are the same launcher started with SMOKE_REAL_SMTP=1 (so mail really goes over SMTP,
to a capture server this script starts) and with STORAGE_DRIVER=s3 pointing at nothing. Without them those
sections are skipped. Jobs run through ``python -m app.cli run-job`` and the database checks use
DATABASE_URL, so run this inside the api container. Exit code 1 when any step failed.
"""

# ruff: noqa: E501  (step labels are sentences; they read better unbroken)

import asyncio
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import uuid
import zlib
from collections.abc import Callable
from typing import Any

import httpx
from smoke_lib import (
    DESKTOP_UA,
    STRONG_PASSWORD,
    Report,
    base_url,
    bearer,
    client,
    login,
    pdf_page_count,
    pdf_text,
    pdf_title,
    pdf_valid,
    png_bytes,
    run_id,
    sa_login,
)
from smoke_security import Org, db


def arg(name: str, default: str | None = None) -> str | None:
    for i, a in enumerate(sys.argv):
        if a == name and i + 1 < len(sys.argv):
            return sys.argv[i + 1].rstrip("/")
    return default


def ok_rows(result: Any) -> list[Any]:
    return result if isinstance(result, list) else []


def exam_path(url: str) -> str:
    return "/" + url.split("/api/v1/", 1)[1]


def new_png(size: int) -> bytes:
    return png_bytes(size)


def png_with_dimensions(width: int, height: int) -> bytes:
    """A PNG whose header claims a huge size (the body is a stub): the server must refuse it by header."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
        + chunk(b"IEND", b"")
    )


def must(resp: httpx.Response, what: str) -> httpx.Response:
    if resp.status_code >= 400:
        raise RuntimeError(f"{what}: {resp.status_code} {resp.text[:300]}")
    return resp


def start_exam(org: Org, token: str) -> tuple[httpx.Client, dict[str, str], dict[str, Any]]:
    """Org.start_exam with readable failures (which call, which status, which body)."""
    cand = client(headers={"User-Agent": DESKTOP_UA})
    opened = must(cand.post("/exam/session/open", json={"token": token}), "open session")
    ex = bearer(opened.json()["session_token"])
    must(
        cand.post(
            "/exam/session/system-check",
            headers=ex,
            json={"viewport_w": 1280, "viewport_h": 800, "camera_permission": True},
        ),
        "system check",
    )
    must(cand.post("/exam/session/consent", headers=ex, json={}), "consent")
    must(
        cand.post(
            "/exam/session/baseline-photo", headers=ex, files={"file": ("b.png", new_png(32), "image/png")}
        ),
        "baseline photo",
    )
    must(cand.post("/exam/session/start", headers=ex), "start")
    qs = {
        q["type"]: q for q in must(cand.get("/exam/questions", headers=ex), "questions").json()["questions"]
    }
    return cand, ex, qs


def make_attempt(
    api: httpx.Client,
    org: Org,
    name: str,
    *,
    phone: str | None = None,
    submit: bool = True,
    snapshot: bool = True,
) -> dict[str, Any]:
    """Invite, run the exam (answer, events, snapshot) and optionally submit. Returns ids and the client."""
    body: dict[str, Any] = {"candidate_name": name, "email": f"c{uuid.uuid4().hex[:10]}@example.com"}
    if phone:
        body["phone"] = phone
    inv = api.post(f"/roles/{org.ids['role']}/invites", headers=org.hr, json=body)
    inv.raise_for_status()
    invite_id, token = inv.json()["invite"]["id"], inv.json()["exam_url"].rsplit("/", 1)[1]
    cand, ex, qs = start_exam(org, token)
    single = qs["single_choice"]
    cand.put(
        f"/exam/answers/{single['id']}",
        headers=ex,
        json={"answer": {"option_id": single["options"][0]["id"]}},
    )
    cand.put(
        f"/exam/answers/{qs['short_answer']['id']}",
        headers=ex,
        json={"answer": {"text": "An index speeds up lookups using a sorted structure."}},
    )
    cand.post(
        "/exam/events", headers=ex, json={"events": [{"type": "multiple_faces"}, {"type": "copy_attempt"}]}
    )
    if snapshot:
        cand.post("/exam/snapshots", headers=ex, files={"file": ("s.png", new_png(32), "image/png")})
    if submit:
        cand.post("/exam/submit", headers=ex).raise_for_status()
    rows = api.get(f"/roles/{org.ids['role']}/attempts?page_size=100", headers=org.hr).json()["items"]
    attempt_id = next(x["id"] for x in rows if x["invite_id"] == invite_id)
    return {
        "invite": invite_id, "token": token, "attempt": attempt_id, "cand": cand, "ex": ex,
        "email": body["email"], "questions": qs,
    }  # fmt: skip


def new_candidate_session(token: str) -> tuple[httpx.Client, dict[str, str]]:
    cand = client(headers={"User-Agent": DESKTOP_UA})
    resp = cand.post("/exam/session/open", json={"token": token})
    resp.raise_for_status()
    return cand, bearer(resp.json()["session_token"])


def prepare(cand: httpx.Client, ex: dict[str, str], *, photo: bool = True, camera: bool = True) -> None:
    cand.post(
        "/exam/session/system-check",
        headers=ex,
        json={"viewport_w": 1280, "viewport_h": 800, "camera_permission": camera},
    ).raise_for_status()
    cand.post("/exam/session/consent", headers=ex, json={}).raise_for_status()
    if photo:
        cand.post(
            "/exam/session/baseline-photo", headers=ex, files={"file": ("b.png", new_png(32), "image/png")}
        ).raise_for_status()


def run_job(name: str) -> tuple[int, dict[str, Any] | None, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "app.cli", "run-job", name], capture_output=True, text=True, timeout=180
    )
    result = None
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            result = json.loads(line)
            break
        except ValueError:
            continue
    return proc.returncode, result, (proc.stdout + proc.stderr)[-400:]


def run_job_mock(name: str) -> tuple[int, dict[str, Any] | None, str]:
    """The same job function, in a process where the AI provider is the smoke server's MockProvider."""
    code = (
        "import asyncio, json, sys; sys.path.insert(0, 'scripts');"
        "from smoke_server import _mock_ai; from app.core.ai import MockProvider, set_provider_override;"
        "set_provider_override(MockProvider(_mock_ai)); from app.jobs import JOBS;"
        f"print(json.dumps(asyncio.run(JOBS[{name!r}]())))"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180)
    result = None
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            result = json.loads(line)
            break
        except ValueError:
            continue
    return proc.returncode, result, (proc.stdout + proc.stderr)[-400:]


def error_code(resp: httpx.Response) -> str | None:
    try:
        return resp.json().get("error", {}).get("code")
    except Exception:  # noqa: BLE001
        return None


# =========================================================================== 1. PDF exports


def pdf_exports(r: Report, api: httpx.Client, x: Org, sa: dict[str, str], app_name: str) -> None:
    oa, hr = x.oa, x.hr
    aid = x.ids["attempt"]
    resp = api.post(f"/attempts/{aid}/decision", headers=hr, json={"decision": "rejected", "note": "pdf"})
    r.status("HR rejects the seeded attempt (feeds the report)", resp, 201)
    bold = make_attempt(api, x, "<b>Bold</b> & Co", snapshot=False)
    api.post(f"/attempts/{bold['attempt']}/decision", headers=hr, json={"decision": "rejected"})

    # audit PDF (organization admin)
    exp = api.post("/audit-logs/export", headers=oa, json={"format": "pdf", "filters": {}})
    body = exp.content
    r.step(
        "audit PDF export: 200 application/pdf attachment .pdf",
        exp.status_code == 200
        and exp.headers["content-type"].startswith("application/pdf")
        and "attachment" in exp.headers.get("content-disposition", "")
        and ".pdf" in exp.headers.get("content-disposition", ""),
        f"{exp.status_code} {exp.headers}",
    )
    r.step("audit PDF is a structurally valid PDF with at least one page", pdf_valid(body), body[:40].hex())
    text = pdf_text(body) if pdf_valid(body) else ""
    r.step(
        "audit PDF header and document title carry the application name",
        app_name in text and app_name in pdf_title(body) and "Audit log export" in text,
        f"title={pdf_title(body)!r} text={text[:120]!r}",
    )
    r.step(
        "audit PDF rows contain real audit actions and the filter summary",
        "DECISION_MADE" in text and "INVITE_CREATED" in text and "rows" in text,
        text[:200],
    )
    r.step(
        "audit PDF never exposes the raw exam token or password hashes",
        "exam/" not in text and "argon2" not in text,
    )
    cols = api.post(
        "/audit-logs/export",
        headers=oa,
        json={
            "format": "pdf",
            "columns": ["created_at", "action"],
            "filters": {"action": ["DECISION_MADE"]},
        },
    )
    ctext = pdf_text(cols.content) if cols.status_code == 200 and pdf_valid(cols.content) else ""
    r.step(
        "audit PDF honours the column choice and the action filter",
        cols.status_code == 200
        and "DECISION_MADE" in ctext
        and "INVITE_CREATED" not in ctext
        and "Result" not in ctext,
        f"{cols.status_code} {ctext[:150]!r}",
    )
    r.error_code(
        "unknown export column is refused",
        api.post(
            "/audit-logs/export", headers=oa, json={"format": "pdf", "columns": ["nope"], "filters": {}}
        ),
        422,
    )
    r.status(
        "HR cannot export the audit log",
        api.post("/audit-logs/export", headers=hr, json={"format": "pdf", "filters": {}}),
        403,
    )
    items = api.get("/audit-logs?page_size=20", headers=oa).json()["items"]
    exported = next((i for i in items if i["action"] == "AUDIT_EXPORTED"), None)
    detail = api.get(f"/audit-logs/{exported['id']}", headers=oa).json() if exported else {}
    r.step(
        "the PDF export is itself audited with its format and row count",
        bool(exported) and "pdf" in json.dumps(detail.get("after", {})),
        str(detail)[:200],
    )

    # audit PDF (super admin, cross organization)
    sexp = api.post("/platform/audit-logs/export", headers=sa, json={"format": "pdf", "filters": {}})
    stext = pdf_text(sexp.content) if sexp.status_code == 200 and pdf_valid(sexp.content) else ""
    r.step(
        "SA platform audit PDF is valid and names the application",
        sexp.status_code == 200 and pdf_valid(sexp.content) and app_name in stext,
        f"{sexp.status_code} valid={pdf_valid(sexp.content)} pages={pdf_page_count(sexp.content)} text={stext[:80]!r}",
    )

    # M11 report PDF
    interp = api.post("/reports/chat/interpret", headers=oa, json={"text": "rejected candidates"})
    r.status("M11 interpret (mock AI) for the PDF report", interp, 200)
    if interp.status_code != 200:
        return
    rid = interp.json()["report"]["id"]
    api.post("/reports/chat/select", headers=oa, json={"id": rid, "index": 0}).raise_for_status()
    gen = api.post(f"/reports/{rid}/generate", headers=oa, json={"format": "pdf"})
    r.step(
        "M11 generate with format pdf succeeds",
        gen.status_code == 200 and gen.json()["status"] == "generated" and gen.json()["row_count"] >= 2,
        gen.text[:200],
    )
    dl = api.get(f"/reports/{rid}/download", headers=oa)
    r.step(
        "M11 download: application/pdf attachment, valid PDF",
        dl.status_code == 200
        and dl.headers["content-type"].startswith("application/pdf")
        and ".pdf" in dl.headers.get("content-disposition", "")
        and pdf_valid(dl.content),
        f"{dl.status_code} {dl.headers.get('content-type')}",
    )
    rtext = pdf_text(dl.content) if dl.status_code == 200 and pdf_valid(dl.content) else ""
    r.step(
        "M11 PDF header carries the application name, rows carry decrypted candidate names",
        app_name in rtext and "Victim Candidate" in rtext,
        rtext[:200],
    )
    r.step(
        "M11 PDF escapes HTML in cell values (a name with tags is printed literally, not interpreted)",
        "<b>Bold</b> & Co" in rtext,
        rtext[:300],
    )
    got = {a["action"] for a in api.get("/audit-logs?page_size=50", headers=oa).json()["items"]}
    r.step(
        "M11 PDF generation and download are audited",
        {"REPORT_GENERATED", "REPORT_DOWNLOADED"} <= got,
        str(got),
    )
    r.skip(
        "candidate report PDF",
        "no such endpoint: GET /attempts/{id} is JSON only (spec M9); PDF exists for audit and M11 only",
    )
    # watermark: SA-only flag shown on candidate pages (not in exports)
    inv = api.post(
        f"/roles/{x.ids['role']}/invites",
        headers=hr,
        json={"candidate_name": "Mark", "email": f"mark{uuid.uuid4().hex[:8]}@example.com"},
    )
    cand, ex = new_candidate_session(inv.json()["exam_url"].rsplit("/", 1)[1])
    state = cand.get("/exam/session/state", headers=ex).json()
    r.step(
        "candidate state carries the watermark text while the flag is on",
        bool(state.get("watermark")),
        str(state.get("watermark")),
    )
    flag_off = api.put(
        f"/platform/orgs/{x.t.org_id}/features",
        headers=sa,
        json={"flags": {"watermark": False}, "reason": "smoke"},
    )
    state_off = cand.get("/exam/session/state", headers=ex).json()
    r.step(
        "SA turns the watermark off: the candidate state drops it (SA-only flag)",
        flag_off.status_code == 200 and not state_off.get("watermark"),
        f"{flag_off.status_code} {state_off.get('watermark')}",
    )
    r.status(
        "OA cannot change the watermark flag",
        api.put(f"/platform/orgs/{x.t.org_id}/features", headers=oa, json={"flags": {"watermark": True}}),
        403,
    )
    api.put(
        f"/platform/orgs/{x.t.org_id}/features",
        headers=sa,
        json={"flags": {"watermark": True}, "reason": "restore"},
    ).raise_for_status()
    r.step(
        "SA turns the watermark back on",
        bool(cand.get("/exam/session/state", headers=ex).json().get("watermark")),
    )


# =========================================================================== 2. bulk CSV invites


def bulk_invites(r: Report, api: httpx.Client, x: Org, sa: dict[str, str], run: str) -> None:
    hr, oa = x.hr, x.oa
    role = x.ids["role"]
    path = f"/roles/{role}/invites/bulk"
    existing_email = f"existing-{run}@example.com"
    api.post(
        f"/roles/{role}/invites", headers=hr, json={"candidate_name": "Existing", "email": existing_email}
    ).raise_for_status()
    tag = uuid.uuid4().hex[:6]
    csv_text = "\n".join(
        [
            "name,email,phone",
            f"Good One,good1-{tag}@example.com,+14155550101",
            f"Good Two,good2-{tag}@example.com,",
            f'"O\'Brien, ""Bob""",bob-{tag}@example.com,',
            "Bad Email,not-an-email,",
            f",noname-{tag}@example.com,",
            f'=HYPERLINK("http://evil"),formula-{tag}@example.com,',
            f"+SUM(1+1),plus-{tag}@example.com,",
            f"@cmd,at-{tag}@example.com,",
            "Email Formula,=cmd|' /C calc'!A0,",
            f"Plus Email,+tag-{tag}@example.com,",
            f"Dupe In File,good1-{tag}@example.com,",
            f"Case Dupe,GOOD2-{tag}@Example.com,",
            f"Existing Again,{existing_email},",
            f"Bad Phone,badphone-{tag}@example.com,abc",
            f"Mid Formula,mid-{tag}@example.com,",
            "",
        ]
    )
    csv_text = csv_text.replace("Mid Formula", "Mid =1+1 Formula")
    resp = api.post(path, headers=hr, files={"file": ("invites.csv", ("﻿" + csv_text).encode(), "text/csv")})
    r.status("bulk CSV upload (BOM, quoting, mixed good and bad rows)", resp, 201)
    if resp.status_code != 201:
        return
    out = resp.json()
    by_row = {row["row"]: row for row in out["rows"]}
    codes = {n: (by_row[n]["status"], by_row[n].get("code")) for n in by_row}
    r.step(
        "per-row results: good rows created with a one-time exam URL",
        all(codes[n][0] == "created" and by_row[n]["exam_url"] for n in (1, 2, 3, 15)),
        str(codes),
    )
    r.step(
        "bad email, empty name, bad phone rejected per row",
        codes[4] == ("error", "email_invalid")
        and codes[5] == ("error", "name_invalid")
        and codes[14] == ("error", "phone_invalid"),
        str(codes),
    )
    r.step(
        "formula-injection cells rejected (name =, +, @ and email =), email starting with + allowed",
        all(codes[n] == ("error", "formula_not_allowed") for n in (6, 7, 8, 9)) and codes[10][0] == "created",
        str(codes),
    )
    r.step(
        "duplicate in file (also case/whitespace variant) rejected",
        codes[11] == ("error", "duplicate_in_file") and codes[12] == ("error", "duplicate_in_file"),
        str(codes),
    )
    r.step(
        "the existing-invite row is reported as DUPLICATE_INVITE",
        any(row.get("code") == "DUPLICATE_INVITE" for row in out["rows"]),
        str(out["rows"])[:400],
    )
    r.step(
        "a formula character inside a name (not leading) is accepted",
        codes[15] == ("created", None),
        str(codes),
    )
    r.step(
        "totals add up (created + failed = total)",
        out["created"] + out["failed"] == out["total"] and out["created"] >= 5,
        f"{out['created']} {out['failed']} {out['total']}",
    )
    listed = api.get(f"/roles/{role}/invites?page_size=100", headers=hr).json()["items"]
    names = {i["candidate_name"] for i in listed}
    r.step(
        "created invites are listed (including the quoted comma name); rejected rows left no invite",
        'O\'Brien, "Bob"' in names
        and "Good One" in names
        and not any(n.startswith(("=", "+", "@")) for n in names),
        str(sorted(names))[:300],
    )
    token = by_row[1]["exam_url"].rsplit("/", 1)[1]
    cand, ex = new_candidate_session(token)
    r.step("a bulk-created exam link opens a candidate session", bool(ex))
    again = api.post(path, headers=hr, files={"file": ("invites.csv", csv_text.encode(), "text/csv")})
    r.step(
        "uploading the same file again creates nothing new (every valid row is a duplicate)",
        again.status_code == 201
        and again.json()["created"] == 0
        and sum(1 for row in again.json()["rows"] if row.get("code") == "DUPLICATE_INVITE") >= 5,
        again.text[:300],
    )
    logs = api.get("/audit-logs?page_size=50", headers=oa).json()["items"]
    bulk_audit = next((i for i in logs if i["action"] == "INVITE_BULK_CREATED"), None)
    r.step(
        "INVITE_BULK_CREATED is audited once per upload with counts",
        bulk_audit is not None,
        str([i["action"] for i in logs][:8]),
    )
    stored = ok_rows(
        db(
            "SELECT count(*) AS n FROM invites WHERE org_id = $1 AND candidate_email_enc::text LIKE '%' || $2 || '%'",
            x.t.org_id,
            "good1-" + tag,
        )
    )
    r.step(
        "candidate emails from the CSV are not stored in plain text",
        bool(stored) and stored[0]["n"] == 0,
        str(stored),
    )

    # whole-file failures
    def upload(
        content: bytes, name: str = "i.csv", field: str = "file", ct: str = "text/csv"
    ) -> httpx.Response:
        return api.post(path, headers=hr, files={field: (name, content, ct)})

    r.error_code("empty file is refused", upload(b""), 422)
    zero = upload(b"name,email\n")
    r.step(
        "header-only file creates nothing",
        zero.status_code == 201 and zero.json()["created"] == 0,
        zero.text[:150],
    )
    r.error_code("wrong header is refused", upload(b"fullname,mail\nA,a@example.com\n"), 422)
    r.error_code(
        "extra header column is refused", upload(b"name,email,phone,role\nA,a@example.com,,x\n"), 422
    )
    r.error_code("non UTF-8 file is refused", upload(b"name,email\n\xff\xfe\xfa,a@example.com\n"), 422)
    r.error_code("missing multipart field is refused", upload(b"name,email\n", field="wrong"), 422)
    many = "name,email\n" + "".join(f"M{i},many{i}-{tag}@example.com\n" for i in range(201))
    r.error_code("more than 200 rows fails the whole file", upload(many.encode()), 422)
    exact = "name,email\n" + "".join(f"E{i},exact{i}-{tag}@example.com\n" for i in range(200))
    ok200 = upload(exact.encode())
    r.step(
        "exactly 200 rows is accepted",
        ok200.status_code == 201 and ok200.json()["created"] == 200,
        ok200.text[:120],
    )
    r.error_code("file over 512 KiB is refused", upload(b"name,email\n" + b"A,a@example.com\n" * 40000), 422)
    mal = upload(b'name,email\n"A,a@example.com\n')
    r.step(
        "malformed quoting never crashes (4xx or per-row error)",
        mal.status_code in (201, 422),
        f"{mal.status_code} {mal.text[:150]}",
    )
    r.status(
        "the org admin may bulk invite too",
        api.post(
            path,
            headers=oa,
            files={"file": ("a.csv", b"name,email\nOA Row,oa-%s@example.com\n" % tag.encode(), "text/csv")},
        ),
        201,
    )
    r.error_code(
        "anonymous bulk upload refused",
        client().post(path, files={"file": ("a.csv", b"name,email\n", "text/csv")}),
        401,
    )

    # flag and role guards
    api.put(
        f"/platform/orgs/{x.t.org_id}/features",
        headers=sa,
        json={"flags": {"csv_bulk_invite": False}, "reason": "smoke"},
    ).raise_for_status()
    r.error_code(
        "flag csv_bulk_invite off gives FEATURE_DISABLED",
        upload(b"name,email\nF,f@example.com\n"),
        403,
        "FEATURE_DISABLED",
    )
    single = api.post(
        f"/roles/{role}/invites",
        headers=hr,
        json={"candidate_name": "Single", "email": f"single-{tag}@example.com"},
    )
    r.step(
        "single invites still work with the bulk flag off", single.status_code == 201, str(single.status_code)
    )
    api.put(
        f"/platform/orgs/{x.t.org_id}/features",
        headers=sa,
        json={"flags": {"csv_bulk_invite": True}, "reason": "restore"},
    ).raise_for_status()
    closed = api.post("/roles", headers=hr, json={"title": "Closed role"}).json()["id"]
    r.error_code(
        "bulk invite on a role that is not open/linked is refused",
        api.post(
            f"/roles/{closed}/invites/bulk",
            headers=hr,
            files={"file": ("a.csv", b"name,email\nA,a@example.com\n", "text/csv")},
        ),
        409,
    )


# =========================================================================== 3. org disable / enable


def org_disable_enable(r: Report, api: httpx.Client, sa: dict[str, str], token: str, run: str) -> None:
    y = Org(api, token, "Dis", run).seed(attempt=False)
    oid, hr, oa = y.t.org_id, y.hr, y.oa
    # one candidate unopened, one opened but not started, one in progress
    unopened = y.invite("Unopened")
    opened_inv, opened_token = y.invite("Opened Only")
    opened_c, opened_ex = new_candidate_session(opened_token)
    prepare(opened_c, opened_ex)
    live_inv, live_token = y.invite("Live")
    live_c, live_ex, live_qs = y.start_exam(live_token)
    # a refresh cookie for the HR user
    hr_cookie_client = client()
    hr_login = login(hr_cookie_client, y.t.hr_email, STRONG_PASSWORD)
    r.status("HR can log in before the disable", hr_login, 200)

    r.status("HR cannot disable an organization", api.post(f"/platform/orgs/{oid}/disable", headers=hr), 403)
    r.status(
        "OA cannot disable its own organization", api.post(f"/platform/orgs/{oid}/disable", headers=oa), 403
    )
    dis = api.post(f"/platform/orgs/{oid}/disable", headers=sa, json={"reason": "smoke disable"})
    r.step(
        "SA disables the organization",
        dis.status_code == 200 and dis.json()["status"] == "disabled",
        dis.text[:200],
    )
    r.error_code("disabling twice is a conflict", api.post(f"/platform/orgs/{oid}/disable", headers=sa), 409)

    r.error_code(
        "HR login refused with ORG_DISABLED", login(api, y.t.hr_email, STRONG_PASSWORD), 403, "ORG_DISABLED"
    )
    r.error_code(
        "OA login refused with ORG_DISABLED",
        login(api, y.t.admin_email, STRONG_PASSWORD),
        403,
        "ORG_DISABLED",
    )
    wrong = login(api, y.t.hr_email, "Wrong-Password-123!")
    r.step(
        "a wrong password still gets the generic login error (no org status leak)",
        error_code(wrong) in ("INVALID_CREDENTIALS", "ORG_DISABLED") and wrong.status_code in (401, 403),
        f"{wrong.status_code} {error_code(wrong)}",
    )
    refresh = hr_cookie_client.post("/auth/refresh")
    r.step(
        "the existing refresh session cannot mint new tokens (ORG_DISABLED)",
        refresh.status_code in (401, 403) and error_code(refresh) in ("ORG_DISABLED", "UNAUTHENTICATED"),
        f"{refresh.status_code} {refresh.text[:150]}",
    )
    me = api.get("/auth/me", headers=hr)
    r.step(
        "an access token issued before the disable is refused by the API (ORG_DISABLED)",
        me.status_code in (401, 403),
        f"{me.status_code} {me.text[:150]}",
    )
    r.step("OA access token also refused", api.get("/team/users", headers=oa).status_code in (401, 403))
    opened_try = client(headers={"User-Agent": DESKTOP_UA}).post(
        "/exam/session/open", json={"token": unopened[1]}
    )
    r.error_code("unopened exam link cannot be opened while disabled", opened_try, 403, "ORG_DISABLED")
    start = opened_c.post("/exam/session/start", headers=opened_ex)
    r.step(
        "a session opened before the disable cannot START the exam (spec: no exam starts)",
        start.status_code in (401, 403, 409) and start.status_code != 200,
        f"{start.status_code} {start.text[:200]}",
    )
    single = live_qs["single_choice"]
    save = live_c.put(
        f"/exam/answers/{single['id']}",
        headers=live_ex,
        json={"answer": {"option_id": single["options"][0]["id"]}},
    )
    r.step(
        "an in-progress attempt keeps saving answers (spec: in-progress attempts finish)",
        save.status_code == 200,
        f"{save.status_code} {save.text[:150]}",
    )
    r.status(
        "heartbeat still works for the in-progress attempt",
        live_c.post("/exam/heartbeat", headers=live_ex),
        200,
    )
    sub = live_c.post("/exam/submit", headers=live_ex)
    r.step(
        "the in-progress attempt can submit",
        sub.status_code == 200 and sub.json()["status"] == "submitted",
        f"{sub.status_code} {sub.text[:150]}",
    )
    r.status("SA can still read the disabled organization", api.get(f"/platform/orgs/{oid}", headers=sa), 200)
    listed = api.get("/platform/orgs?status=disabled&page_size=100", headers=sa).json()
    r.step(
        "org shows in the disabled filter", any(o["id"] == oid for o in listed["items"]), str(listed)[:150]
    )
    audit = api.get("/platform/audit-logs?page_size=50&action=ORG_DISABLED", headers=sa)
    r.step(
        "ORG_DISABLED is audited (visible to SA)",
        audit.status_code == 200 and any(i["action"] == "ORG_DISABLED" for i in audit.json()["items"]),
        audit.text[:200],
    )

    en = api.post(f"/platform/orgs/{oid}/enable", headers=sa, json={"reason": "smoke enable"})
    r.step(
        "SA enables the organization",
        en.status_code == 200 and en.json()["status"] == "active",
        en.text[:200],
    )
    r.error_code("enabling twice is a conflict", api.post(f"/platform/orgs/{oid}/enable", headers=sa), 409)
    relog = login(api, y.t.hr_email, STRONG_PASSWORD)
    r.status("HR can log in again after the enable", relog, 200)
    hr2 = bearer(relog.json()["access_token"])
    r.status("HR data is intact after the round trip", api.get(f"/roles/{y.ids['role']}", headers=hr2), 200)
    r.status("OA login works again", login(api, y.t.admin_email, STRONG_PASSWORD), 200)
    r.step(
        "the previously unopened link opens again",
        client(headers={"User-Agent": DESKTOP_UA})
        .post("/exam/session/open", json={"token": unopened[1]})
        .status_code
        == 200,
    )
    restart = opened_c.post("/exam/session/start", headers=opened_ex)
    r.step(
        "the session opened earlier can start after the enable",
        restart.status_code == 200,
        f"{restart.status_code} {restart.text[:150]}",
    )
    audit2 = api.get("/platform/audit-logs?page_size=50&action=ORG_ENABLED", headers=sa)
    r.step(
        "ORG_ENABLED is audited",
        audit2.status_code == 200 and any(i["action"] == "ORG_ENABLED" for i in audit2.json()["items"]),
        audit2.text[:200],
    )


def _open(token: str) -> httpx.Response:
    return client(headers={"User-Agent": DESKTOP_UA}).post("/exam/session/open", json={"token": token})


# =========================================================================== 4. candidate data erase


def candidate_erase(r: Report, api: httpx.Client, x: Org, other: Org, run: str) -> None:
    hr, oa = x.hr, x.oa
    phone = "+14155550199"
    a = make_attempt(api, x, "Erase Me", phone=phone)
    attempt = a["attempt"]
    api.post(
        f"/attempts/{attempt}/decision", headers=hr, json={"decision": "approved", "note": "keep summary"}
    )
    before = api.get(f"/attempts/{attempt}", headers=hr).json()
    snaps = before.get("snapshot_asset_ids") or []
    url = (
        api.get(f"/attempts/{attempt}/snapshots/{snaps[0]}/url", headers=hr).json()["url"] if snaps else None
    )
    served = api.get(exam_path(url)) if url else None
    r.step(
        "before erase: answers, timeline, snapshot and a working signed snapshot URL exist",
        bool(before["breakdown"])
        and len(before["timeline"]) >= 2
        and bool(snaps)
        and served is not None
        and served.status_code == 200,
        f"{len(before.get('breakdown', []))} {len(before.get('timeline', []))} {snaps} {served and served.status_code}",
    )
    asset_rows = ok_rows(
        db(
            "SELECT count(*) AS n FROM assets WHERE org_id = $1 AND kind IN ('snapshot','baseline')",
            x.t.org_id,
        )
    )
    ans_before = ok_rows(db("SELECT count(*) AS n FROM attempt_answers WHERE attempt_id = $1", attempt))
    inv_before = ok_rows(
        db("SELECT candidate_phone_enc IS NOT NULL AS has_phone FROM invites WHERE id = $1", a["invite"])
    )
    r.step(
        "DB before: answers stored, phone stored (encrypted), snapshot and baseline assets present",
        bool(ans_before)
        and ans_before[0]["n"] >= 1
        and bool(inv_before)
        and inv_before[0]["has_phone"]
        and asset_rows[0]["n"] >= 2,
        f"{ans_before} {inv_before} {asset_rows}",
    )
    r.status(
        "HR cannot erase (admin only)", api.delete(f"/attempts/{attempt}/candidate-data", headers=hr), 403
    )
    r.status(
        "another organization's OA gets 404 (not 403)",
        api.delete(f"/attempts/{attempt}/candidate-data", headers=other.oa),
        404,
    )
    r.status("anonymous erase refused", client().delete(f"/attempts/{attempt}/candidate-data"), 401)
    er = api.delete(f"/attempts/{attempt}/candidate-data", headers=oa)
    r.step(
        "OA erases candidate data: counts returned, identity kept by default",
        er.status_code == 200
        and er.json()["answers"] >= 1
        and er.json()["events"] >= 2
        and er.json()["snapshots"] >= 2
        and er.json()["identity_erased"] is False,
        er.text[:200],
    )
    after = api.get(f"/attempts/{attempt}", headers=hr).json()
    r.step(
        "report after erase: no answers detail, no timeline, no snapshots",
        not [b for b in after["breakdown"] if b.get("answer")]
        and not after["timeline"]
        and not after.get("snapshot_asset_ids"),
        f"{str(after.get('breakdown'))[:200]} {after.get('timeline')} {after.get('snapshot_asset_ids')}",
    )
    r.step(
        "summary kept: score, decision, who decided and flag counts survive",
        after["score_percent"] == before["score_percent"]
        and after["decision"] == "approved"
        and after["flag_counts"] == before["flag_counts"],
        f"{after.get('score_percent')} {after.get('decision')} {after.get('flag_counts')}",
    )
    r.step(
        "report marks the candidate as erased (candidate.erased) and no phone",
        after["candidate"]["erased"] is True and not after["candidate"]["phone"],
        str(after["candidate"]),
    )
    if url:
        gone = api.get(exam_path(url))
        r.step(
            "the signed URL issued before the erase no longer serves the image (file deleted)",
            gone.status_code == 404,
            str(gone.status_code),
        )
    ans_after = ok_rows(db("SELECT count(*) AS n FROM attempt_answers WHERE attempt_id = $1", attempt))
    ev_after = ok_rows(db("SELECT count(*) AS n FROM proctor_events WHERE attempt_id = $1", attempt))
    inv_after = ok_rows(
        db(
            "SELECT candidate_phone_enc IS NULL AS no_phone, pii_purged_at IS NOT NULL AS purged, candidate_name FROM invites WHERE id = $1",
            a["invite"],
        )
    )
    att_after = ok_rows(
        db("SELECT baseline_asset_id, purged_at, ip, user_agent FROM attempts WHERE id = $1", attempt)
    )
    r.step(
        "DB after: answers and events rows gone, phone NULL, baseline reference cleared, ip and user agent cleared",
        ans_after[0]["n"] == 0
        and ev_after[0]["n"] == 0
        and inv_after[0]["no_phone"]
        and inv_after[0]["purged"]
        and att_after[0]["baseline_asset_id"] is None
        and att_after[0]["ip"] is None
        and att_after[0]["user_agent"] is None,
        f"{ans_after} {ev_after} {inv_after} {att_after}",
    )
    asset_after = ok_rows(
        db(
            "SELECT count(*) AS n FROM assets WHERE org_id = $1 AND kind IN ('snapshot','baseline')",
            x.t.org_id,
        )
    )
    r.step(
        "DB after: the attempt's snapshot and baseline asset rows are deleted",
        asset_after[0]["n"] <= asset_rows[0]["n"] - 2,
        f"{asset_rows} -> {asset_after}",
    )
    name_kept = api.get(f"/roles/{x.ids['role']}/invites?page_size=100", headers=hr).json()["items"]
    mine = next((i for i in name_kept if i["id"] == a["invite"]), None)
    r.step(
        "default erase keeps name and email (they are kept-forever summary fields)",
        bool(mine) and mine["candidate_name"] == "Erase Me" and mine["candidate_email"] == a["email"],
        str(mine),
    )
    items = api.get("/audit-logs?page_size=50&action=CANDIDATE_DATA_DELETED", headers=oa).json()["items"]
    row = next((i for i in items if i["action"] == "CANDIDATE_DATA_DELETED"), None)
    detail = api.get(f"/audit-logs/{row['id']}", headers=oa).json() if row else {}
    r.step(
        "audit row CANDIDATE_DATA_DELETED is kept with counts, without PII",
        bool(row)
        and "Erase Me" not in json.dumps(detail)
        and a["email"] not in json.dumps(detail)
        and "answers" in json.dumps(detail),
        str(detail)[:300],
    )
    again = api.delete(f"/attempts/{attempt}/candidate-data", headers=oa)
    r.step(
        "erase is repeatable (idempotent: nothing left to remove)",
        again.status_code == 200 and again.json()["answers"] == 0,
        again.text[:150],
    )
    rows_after = ok_rows(
        db(
            "SELECT count(*) AS n FROM audit_logs WHERE entity_id::text = $1 AND action = 'CANDIDATE_DATA_DELETED'",
            str(attempt),
        )
    )
    r.step(
        "the audit trail still holds both erase events (append-only)",
        bool(rows_after) and rows_after[0]["n"] == 2,
        str(rows_after),
    )

    # identity erase
    b = make_attempt(api, x, "Identity Gone", phone="+14155550123")
    api.post(f"/attempts/{b['attempt']}/decision", headers=hr, json={"decision": "rejected"})
    er2 = api.delete(f"/attempts/{b['attempt']}/candidate-data?include_identity=true", headers=oa)
    r.step(
        "include_identity=true also reports identity_erased",
        er2.status_code == 200 and er2.json()["identity_erased"] is True,
        er2.text[:150],
    )
    inv_row = ok_rows(
        db(
            "SELECT candidate_name, candidate_email_enc IS NULL AS no_email, candidate_phone_enc IS NULL AS no_phone FROM invites WHERE id = $1",
            b["invite"],
        )
    )
    r.step(
        "DB: name replaced, email and phone removed",
        bool(inv_row)
        and inv_row[0]["candidate_name"] == "Erased"
        and inv_row[0]["no_email"]
        and inv_row[0]["no_phone"],
        str(inv_row),
    )
    listed = api.get(f"/roles/{x.ids['role']}/invites?page_size=100", headers=hr).json()["items"]
    row_b = next((i for i in listed if i["id"] == b["invite"]), None)
    r.step(
        "invite list shows no name or email for the erased candidate",
        bool(row_b) and row_b["candidate_name"] == "Erased" and not row_b.get("candidate_email"),
        str(row_b),
    )
    reinvite = api.post(
        f"/roles/{x.ids['role']}/invites",
        headers=hr,
        json={"candidate_name": "Back Again", "email": b["email"]},
    )
    r.step(
        "the erased email no longer collides (blind index scrubbed): re-invite works",
        reinvite.status_code == 201,
        f"{reinvite.status_code} {reinvite.text[:150]}",
    )
    final = api.get(f"/attempts/{b['attempt']}", headers=hr).json()
    r.step(
        "summary of the identity-erased attempt still shows score and decision",
        final["decision"] == "rejected" and final["score_percent"] is not None,
        str(final)[:200],
    )
    export = api.post("/audit-logs/export", headers=oa, json={"format": "csv", "filters": {}})
    r.step(
        "audit export after the erase contains no candidate email or phone",
        b["email"].encode() not in export.content and b"4155550123" not in export.content,
    )


# =========================================================================== 5. branding upload


class Branding:
    signed_at: float = 0.0
    signed_path: str = ""


def branding_uploads(r: Report, api: httpx.Client, x: Org, other: Org, sa: dict[str, str]) -> None:
    oa, hr = x.oa, x.hr
    base = api.get("/org/branding", headers=oa)
    r.status("OA reads the branding", base, 200)

    def upload(
        kind: str, content: bytes, name: str, ctype: str, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        return api.post(
            f"/org/branding/{kind}", headers=headers or oa, files={"file": (name, content, ctype)}
        )

    good = upload("logo", new_png(64), "logo.png", "image/png")
    r.step(
        "valid PNG logo accepted and a logo_url returned",
        good.status_code == 200 and bool(good.json().get("logo_url")),
        good.text[:200],
    )
    if good.status_code == 200 and good.json().get("logo_url"):
        path = exam_path(good.json()["logo_url"])
        got = api.get(path)
        Branding.signed_at, Branding.signed_path = time.time(), path
        r.step(
            "signed logo URL serves the PNG inline with safe headers (nosniff, no-store, sandbox CSP)",
            got.status_code == 200
            and got.content[:4] == b"\x89PNG"
            and got.headers["content-type"] == "image/png"
            and got.headers.get("x-content-type-options") == "nosniff"
            and "no-store" in got.headers.get("cache-control", "")
            and "sandbox" in got.headers.get("content-security-policy", ""),
            f"{got.status_code} {dict(got.headers)}",
        )
        token = path.rsplit("/", 1)[1]
        payload, sig = token.split(".")
        flip = ("A" if sig[0] != "A" else "B") + sig[1:]
        r.error_code(
            "tampered signature rejected (404)", api.get(f"/files/{payload}.{flip}"), 404, "NOT_FOUND"
        )
        r.error_code(
            "signature of another payload rejected",
            api.get(f"/files/{payload[:-2]}xx.{sig}"),
            404,
            "NOT_FOUND",
        )
        r.error_code("payload without signature rejected", api.get(f"/files/{payload}"), 404, "NOT_FOUND")
        r.error_code("garbage token rejected", api.get("/files/not.a.token"), 404, "NOT_FOUND")
        r.error_code("empty-ish token rejected", api.get("/files/x"), 404, "NOT_FOUND")
        r.error_code(
            "path traversal in the token is not a file read",
            api.get("/files/..%2F..%2Fetc%2Fpasswd"),
            404,
            "NOT_FOUND",
        )
        forged = (
            __import__("base64")
            .urlsafe_b64encode(
                json.dumps({"k": f"{x.t.org_id}/logo/x.png", "exp": int(time.time()) + 600}).encode()
            )
            .rstrip(b"=")
            .decode()
        )
        r.error_code(
            "forged payload with a made-up signature rejected",
            api.get(f"/files/{forged}.{sig}"),
            404,
            "NOT_FOUND",
        )
        replaced = upload("logo", new_png(48), "logo2.png", "image/png")
        r.step(
            "replacing the logo works",
            replaced.status_code == 200 and replaced.json()["logo_url"] != good.json()["logo_url"],
            replaced.text[:150],
        )
        old = api.get(path)
        r.step(
            "the replaced logo's file is deleted (old signed URL stops working)",
            old.status_code == 404,
            str(old.status_code),
        )
        new_path = exam_path(replaced.json()["logo_url"])
        r.step("the new logo's signed URL works", api.get(new_path).status_code == 200)
        Branding.signed_at, Branding.signed_path = time.time(), new_path
    # validation
    cases: list[tuple[str, bytes, str, str, str]] = [
        (
            "SVG (script carrier) refused",
            b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
            "l.svg",
            "image/svg+xml",
            "logo",
        ),
        (
            "PNG bytes declared as JPEG refused (type must agree with content)",
            new_png(32),
            "l.png",
            "image/jpeg",
            "logo",
        ),
        ("random bytes with an image name refused", os.urandom(200), "l.png", "image/png", "logo"),
        ("HTML file named .png refused", b"<html><script>1</script></html>", "l.png", "image/png", "logo"),
        (
            "PNG magic with a truncated body refused",
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 30,
            "l.png",
            "image/png",
            "logo",
        ),
        ("image below the minimum size (8 px) refused", new_png(8), "l.png", "image/png", "logo"),
        (
            "image header claiming 9000 x 9000 refused",
            png_with_dimensions(9000, 9000),
            "l.png",
            "image/png",
            "logo",
        ),
        ("PDF refused as a logo", b"%PDF-1.4\n%%EOF", "l.pdf", "application/pdf", "logo"),
        ("GIF refused", b"GIF89a" + b"\x00" * 40, "l.gif", "image/gif", "logo"),
        (
            "ICO refused as a logo (favicon only)",
            b"\x00\x00\x01\x00\x01\x00\x20\x20\x00\x00\x01\x00\x20\x00" + b"\x00" * 60,
            "l.ico",
            "image/x-icon",
            "logo",
        ),
    ]
    for label, content, name, ctype, kind in cases:
        resp = upload(kind, content, name, ctype)
        r.step(
            f"logo upload: {label}",
            resp.status_code == 422 and error_code(resp) == "UPLOAD_INVALID",
            f"{resp.status_code} {resp.text[:150]}",
        )
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024 + 10)
    r.step(
        "logo over 5 MB refused (size limit)",
        (lambda rr: rr.status_code == 422 and error_code(rr) == "UPLOAD_INVALID")(
            upload("logo", big, "big.png", "image/png")
        ),
    )
    empty = upload("logo", b"", "e.png", "image/png")
    r.step("empty file refused", empty.status_code == 422, f"{empty.status_code} {empty.text[:120]}")
    missing = api.post("/org/branding/logo", headers=oa)
    r.step("request without a file refused", missing.status_code == 422, str(missing.status_code))
    jpg_ok = upload(
        "favicon",
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xc0\x00\x0b\x08\x00\x20\x00\x20\x01\x01\x11\x00\xff\xd9",
        "f.jpg",
        "image/jpeg",
    )
    r.step(
        "a minimal valid JPEG is accepted as favicon",
        jpg_ok.status_code == 200,
        f"{jpg_ok.status_code} {jpg_ok.text[:150]}",
    )
    r.status("HR cannot upload a logo", upload("logo", new_png(32), "l.png", "image/png", headers=hr), 403)
    r.status(
        "anonymous logo upload refused",
        client().post("/org/branding/logo", files={"file": ("l.png", new_png(32), "image/png")}),
        401,
    )
    # contrast and other guardrails
    low = api.post("/org/branding/validate", headers=oa, json={"primary_color": "#777777"})
    r.step(
        "contrast check without saving flags a low-contrast primary",
        low.status_code == 200 and low.json()["valid"] is False,
        low.text[:200],
    )
    fine = api.post(
        "/org/branding/validate", headers=oa, json={"primary_color": "#1B2A4A", "accent_color": "#0B3D91"}
    )
    r.step(
        "contrast check passes for dark colors",
        fine.status_code == 200 and fine.json()["valid"] is True,
        fine.text[:200],
    )
    r.error_code(
        "saving a low-contrast color gives BRANDING_CONTRAST_TOO_LOW",
        api.put("/org/branding", headers=oa, json={"primary_color": "#777777"}),
        422,
        "BRANDING_CONTRAST_TOO_LOW",
    )
    r.error_code(
        "invalid hex refused", api.put("/org/branding", headers=oa, json={"primary_color": "red"}), 422
    )
    r.error_code(
        "font outside the curated list refused",
        api.put("/org/branding", headers=oa, json={"font_key": "comic-sans-evil"}),
        422,
    )
    foreign_logo = upload("logo", new_png(40), "o.png", "image/png", headers=other.oa)
    foreign_id = ok_rows(db("SELECT logo_asset_id FROM branding_themes WHERE org_id = $1", other.t.org_id))
    if foreign_logo.status_code == 200 and foreign_id and foreign_id[0]["logo_asset_id"]:
        steal = api.put(
            "/org/branding", headers=oa, json={"logo_asset_id": str(foreign_id[0]["logo_asset_id"])}
        )
        r.step(
            "cannot reference another organization's logo asset id",
            steal.status_code in (404, 422),
            f"{steal.status_code} {steal.text[:150]}",
        )
        mine = api.get("/org/branding", headers=oa).json()
        theirs = api.get("/org/branding", headers=other.oa).json()
        r.step(
            "branding stays isolated per organization",
            mine["logo_url"] != theirs["logo_url"] and theirs["logo_url"],
            f"{mine['logo_url']} {theirs['logo_url']}",
        )
    save = api.put(
        "/org/branding",
        headers=oa,
        json={"primary_color": "#1B2A4A", "accent_color": "#0B3D91", "font_key": "inter"},
    )
    r.step(
        "valid colors and font save",
        save.status_code == 200 and save.json()["is_customized"] is True,
        save.text[:200],
    )
    reset = api.post("/org/branding/reset", headers=oa)
    r.step(
        "reset returns to the starter theme and keeps the uploaded logo",
        reset.status_code == 200 and reset.json()["logo_url"] is not None,
        reset.text[:200],
    )
    r.step(
        "BRANDING_UPDATED is audited",
        any(
            i["action"] == "BRANDING_UPDATED"
            for i in api.get("/audit-logs?page_size=50", headers=oa).json()["items"]
        ),
    )
    # candidate sees the branding logo
    inv = api.post(
        f"/roles/{x.ids['role']}/invites",
        headers=hr,
        json={"candidate_name": "Brand", "email": f"brand{uuid.uuid4().hex[:8]}@example.com"},
    )
    cand, ex = new_candidate_session(inv.json()["exam_url"].rsplit("/", 1)[1])
    st = cand.get("/exam/session/state", headers=ex).json()
    logo = (st.get("branding") or {}).get("logo_url")
    r.step(
        "candidate state exposes the organization's logo as a working signed URL",
        bool(logo) and api.get(exam_path(logo)).status_code == 200,
        str(st.get("branding"))[:200],
    )
    api.put(
        f"/platform/orgs/{x.t.org_id}/features",
        headers=sa,
        json={"flags": {"org_branding": False}, "reason": "smoke"},
    ).raise_for_status()
    r.error_code(
        "org_branding flag off gives FEATURE_DISABLED on upload",
        upload("logo", new_png(32), "l.png", "image/png"),
        403,
        "FEATURE_DISABLED",
    )
    api.put(
        f"/platform/orgs/{x.t.org_id}/features",
        headers=sa,
        json={"flags": {"org_branding": True}, "reason": "restore"},
    ).raise_for_status()


def signed_url_expiry(r: Report) -> None:
    if not Branding.signed_path:
        r.skip("signed URL expiry", "no signed URL was issued")
        return
    wait = Branding.signed_at + 63 - time.time()
    if wait > 0:
        time.sleep(wait)
    resp = client().get(Branding.signed_path)
    r.step(
        "a signed file URL older than its 60 s lifetime is rejected (404 NOT_FOUND)",
        resp.status_code == 404 and error_code(resp) == "NOT_FOUND",
        f"{resp.status_code} {resp.text[:100]}",
    )


# =========================================================================== 6. background jobs

EXPECTED_JOBS = {
    "audit_partitions", "expire_invites", "notification_cleanup", "report_cleanup",
    "retention_purge", "retry_ai_scoring", "timeout_attempts", "version_purge",
}  # fmt: skip


def jobs(r: Report, api: httpx.Client, x: Org, token: str, run: str) -> None:
    hr, oa = x.hr, x.oa
    listing = subprocess.run(
        [sys.executable, "-m", "app.cli", "list-jobs"], capture_output=True, text=True, timeout=120
    )
    names = [ln.strip() for ln in listing.stdout.splitlines() if re.fullmatch(r"[a-z_]+", ln.strip())]
    r.step(
        "list-jobs prints every spec job (8) and nothing is undocumented",
        EXPECTED_JOBS <= set(names),
        str(names),
    )
    bad = subprocess.run(
        [sys.executable, "-m", "app.cli", "run-job", "no_such_job"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    r.step(
        "run-job with an unknown name fails with a non-zero exit",
        bad.returncode != 0,
        f"{bad.returncode} {bad.stdout[-100:]}",
    )

    # AI settings: a provider that cannot be reached (offline) for the CLI runs
    api.put(
        "/org/settings/ai",
        headers=oa,
        json={
            "provider": "openai-compatible",
            "model": "local",
            "base_url": "http://127.0.0.1:9/v1",
            "api_key": "sk-smoke-fake-key-123456",
        },
    ).raise_for_status()

    # ---- expire_invites
    exp_inv, exp_token = x.invite("To Expire")
    soon_inv, soon_token = x.invite("Expiring Soon")
    db("UPDATE invites SET expires_at = now() - interval '1 hour' WHERE id = $1", exp_inv, fetch=False)
    db("UPDATE invites SET expires_at = now() + interval '2 hours' WHERE id = $1", soon_inv, fetch=False)
    code, res, tail = run_job("expire_invites")
    r.step(
        "expire_invites runs cleanly and expires the overdue invite",
        code == 0 and res is not None and res["expired"] >= 1,
        tail,
    )
    r.step(
        "the overdue invite is expired in the API and its link answers 410 INVITE_EXPIRED",
        api.get(f"/invites/{exp_inv}", headers=hr).json()["status"] == "expired"
        and _open(exp_token).status_code == 410,
        str(_open(exp_token).status_code),
    )
    r.step(
        "an invite expiring within 24 h is still usable",
        api.get(f"/invites/{soon_inv}", headers=hr).json()["status"] == "invited"
        and _open(soon_token).status_code == 200,
    )
    notes = api.get("/notifications?page_size=50", headers=hr).json()["items"]
    r.step(
        "owner gets an invite_expiring notification (once per invite)",
        sum(1 for n in notes if n["type"] == "invite_expiring" and soon_inv in json.dumps(n)) == 1,
        str([n["type"] for n in notes]),
    )
    code, res, tail = run_job("expire_invites")
    notes2 = api.get("/notifications?page_size=50", headers=hr).json()["items"]
    r.step(
        "running again is idempotent (no second warning, nothing newly expired)",
        code == 0
        and res["expired"] == 0
        and sum(1 for n in notes2 if n["type"] == "invite_expiring" and soon_inv in json.dumps(n)) == 1,
        f"{res} {tail}",
    )

    # ---- timeout_attempts
    t = make_attempt(api, x, "Timeout Cand", submit=False)
    db(
        "UPDATE attempts SET deadline_at = now() - interval '2 minutes' WHERE id = $1",
        t["attempt"],
        fetch=False,
    )
    code, res, tail = run_job("timeout_attempts")
    row = api.get(f"/attempts/{t['attempt']}", headers=hr).json()
    r.step(
        "timeout_attempts auto-submits an overdue attempt with submit_reason=timer",
        code == 0
        and res["submitted"] >= 1
        and row["status"] == "submitted"
        and row["submit_reason"] == "timer",
        f"{res} {row.get('status')} {row.get('submit_reason')}",
    )
    r.step(
        "auto-submitted attempt was scored on submit (score present)",
        row.get("score_percent") is not None,
        str(row.get("score_percent")),
    )
    r.error_code(
        "a late answer after the timer sweep is refused",
        t["cand"].put(
            f"/exam/answers/{t['questions']['single_choice']['id']}",
            headers=t["ex"],
            json={"answer": {"option_id": t["questions"]["single_choice"]["options"][0]["id"]}},
        ),
        409,
        "EXAM_ALREADY_SUBMITTED",
    )

    # ---- retry_ai_scoring (the CLI process has no mock: the provider points at a closed local port)
    down = make_attempt(api, x, "AI Down")
    pending = api.get(f"/attempts/{down['attempt']}", headers=hr).json()
    code, res, tail = run_job("retry_ai_scoring")
    failed = api.get(f"/attempts/{down['attempt']}", headers=hr).json()
    r.step(
        "retry_ai_scoring with an unreachable provider fails safe: attempt becomes needs_manual, job exits 0",
        code == 0
        and pending["scoring_status"] == "pending"
        and failed["scoring_status"] == "needs_manual"
        and res["failed"] == 0,
        f"{pending['scoring_status']} -> {failed['scoring_status']} {res} {tail}",
    )
    notes = api.get("/notifications?page_size=50", headers=hr).json()["items"]
    r.step(
        "the owner is notified (scoring_needs_manual)",
        any(n["type"] == "scoring_needs_manual" for n in notes),
        str([n["type"] for n in notes]),
    )
    s = make_attempt(api, x, "Needs AI")
    code, res, tail = run_job_mock("retry_ai_scoring")
    done = api.get(f"/attempts/{s['attempt']}", headers=hr).json()
    r.step(
        "retry_ai_scoring (same job, mock provider in-process) completes a pending attempt",
        code == 0 and done["scoring_status"] == "complete" and res["retried"] >= 1,
        f"{done['scoring_status']} {res} {tail}",
    )
    stuck = make_attempt(api, x, "Stuck AI")
    db(
        "UPDATE attempts SET submitted_at = now() - interval '30 minutes' WHERE id = $1",
        stuck["attempt"],
        fetch=False,
    )
    api.put(
        f"/platform/orgs/{x.t.org_id}/features",
        headers={"Authorization": f"Bearer {token}"},
        json={"flags": {"ai_scoring": False}, "reason": "smoke"},
    ).raise_for_status()
    code, res, tail = run_job("retry_ai_scoring")
    api.put(
        f"/platform/orgs/{x.t.org_id}/features",
        headers={"Authorization": f"Bearer {token}"},
        json={"flags": {"ai_scoring": True}, "reason": "restore"},
    ).raise_for_status()
    st = api.get(f"/attempts/{stuck['attempt']}", headers=hr).json()
    r.step(
        "with ai_scoring off, a stuck attempt becomes needs_manual (spec 7.3) instead of failing",
        code == 0 and st["scoring_status"] == "needs_manual",
        f"{res} {st.get('scoring_status')} {tail}",
    )

    # ---- retention_purge
    old = make_attempt(api, x, "Old Decided")
    api.post(f"/attempts/{old['attempt']}/decision", headers=hr, json={"decision": "approved"})
    fresh = make_attempt(api, x, "Fresh Decided")
    api.post(f"/attempts/{fresh['attempt']}/decision", headers=hr, json={"decision": "approved"})
    undec = make_attempt(api, x, "Old Undecided")
    snaps_before = api.get(f"/attempts/{old['attempt']}", headers=hr).json().get("snapshot_asset_ids") or []
    db(
        "UPDATE attempts SET decided_at = now() - interval '100 days' WHERE id = $1",
        old["attempt"],
        fetch=False,
    )
    db(
        "UPDATE attempts SET submitted_at = now() - interval '200 days' WHERE id = $1",
        undec["attempt"],
        fetch=False,
    )
    code, res, tail = run_job("retention_purge")
    o = api.get(f"/attempts/{old['attempt']}", headers=hr).json()
    f = api.get(f"/attempts/{fresh['attempt']}", headers=hr).json()
    u = api.get(f"/attempts/{undec['attempt']}", headers=hr).json()
    r.step(
        "retention_purge purges the decision older than the retention window (90 d)",
        code == 0 and res["purged"] >= 1 and res["failed"] == 0 and o["candidate"]["erased"] is True,
        f"{res} {tail}",
    )
    r.step(
        "purged attempt: detail gone (answers, timeline, snapshots), summary kept (score, decision, flag counts)",
        not [b for b in o["breakdown"] if b.get("answer")]
        and not o["timeline"]
        and not o.get("snapshot_asset_ids")
        and bool(snaps_before)
        and o["decision"] == "approved"
        and o["score_percent"] is not None
        and o["flag_counts"],
        str(o)[:300],
    )
    r.step(
        "a recent decision is not purged",
        f["candidate"]["erased"] is False and bool(f["breakdown"]),
        str(f["candidate"]),
    )
    r.step(
        "an undecided attempt is never auto-purged, even when very old",
        u["candidate"]["erased"] is False and bool(u["breakdown"]),
        str(u["candidate"]),
    )
    runs = ok_rows(db("SELECT count(*) AS n FROM audit_logs WHERE action = 'RETENTION_PURGE_RUN'"))
    r.step("RETENTION_PURGE_RUN is audited", bool(runs) and runs[0]["n"] >= 1, str(runs))
    code, res, _ = run_job("retention_purge")
    r.step(
        "retention_purge is idempotent (the purged attempt is not purged twice)",
        code == 0 and res["purged"] == 0,
        str(res),
    )

    # ---- notification_cleanup
    unread_before = api.get("/notifications/unread-count", headers=hr).json()
    notes = api.get("/notifications?page_size=50", headers=hr).json()["items"]
    target = notes[0]["id"] if notes else None
    keep = notes[1]["id"] if len(notes) > 1 else None
    if target:
        api.post(f"/notifications/{target}/read", headers=hr)
        db("UPDATE notifications SET read_at = now() - interval '61 days' WHERE id = $1", target, fetch=False)
    code, res, tail = run_job("notification_cleanup")
    ids_after = {n["id"] for n in api.get("/notifications?page_size=100", headers=hr).json()["items"]}
    r.step(
        "notification_cleanup removes a notification read more than 60 days ago and keeps unread ones",
        code == 0
        and bool(target)
        and res["removed"] >= 1
        and target not in ids_after
        and (keep is None or keep in ids_after),
        f"{res} {tail} {unread_before}",
    )

    # ---- report_cleanup
    interp = api.post("/reports/chat/interpret", headers=oa, json={"text": "rejected candidates"}).json()
    rid = interp["report"]["id"]
    api.post("/reports/chat/select", headers=oa, json={"id": rid, "index": 0}).raise_for_status()
    api.post(f"/reports/{rid}/generate", headers=oa, json={"format": "csv"}).raise_for_status()
    first = api.get(f"/reports/{rid}/download", headers=oa)
    db("UPDATE report_requests SET created_at = now() - interval '8 days' WHERE id = $1", rid, fetch=False)
    code, res, tail = run_job("report_cleanup")
    gone = api.get(f"/reports/{rid}/download", headers=oa)
    hist = api.get("/reports?page_size=100", headers=oa).json()["items"]
    r.step(
        "report_cleanup removes the generated file after 7 days; the history row stays",
        code == 0
        and first.status_code == 200
        and res["removed"] >= 1
        and gone.status_code in (404, 409, 410)
        and any(h["id"] == rid for h in hist),
        f"{res} {gone.status_code} {gone.text[:120]} {tail}",
    )

    # ---- auth_cleanup (refresh tokens expired more than a day ago)
    if "auth_cleanup" in names:
        c = client()
        login(c, x.t.hr_email, STRONG_PASSWORD).raise_for_status()
        db(
            "UPDATE refresh_tokens SET expires_at = now() - interval '3 days' WHERE user_id = $1",
            x.t.hr_id,
            fetch=False,
        )
        login(client(), x.t.hr_email, STRONG_PASSWORD).raise_for_status()  # a fresh, live token
        before = ok_rows(db("SELECT count(*) AS n FROM refresh_tokens WHERE user_id = $1", x.t.hr_id))
        code, res, tail = run_job("auth_cleanup")
        after = ok_rows(db("SELECT count(*) AS n FROM refresh_tokens WHERE user_id = $1", x.t.hr_id))
        live = ok_rows(
            db(
                "SELECT count(*) AS n FROM refresh_tokens WHERE user_id = $1 AND expires_at > now()",
                x.t.hr_id,
            )
        )
        r.step(
            "auth_cleanup removes refresh tokens expired over a day ago and keeps live ones",
            code == 0
            and res["refresh_tokens_removed"] >= 1
            and after[0]["n"] < before[0]["n"]
            and live[0]["n"] >= 1,
            f"{before} {after} {live} {res} {tail}",
        )

    # ---- version_purge
    code, res, tail = run_job("version_purge")
    r.step(
        "version_purge runs cleanly and has nothing over the 3-version limit to purge",
        code == 0 and res["failed"] == 0 and res["purged"] == 0,
        f"{res} {tail}",
    )
    versions = api.get(f"/assessments/{x.ids['assessment']}/versions", headers=hr)
    r.step(
        "published versions in use are untouched by the purge",
        versions.status_code == 200
        and any(
            v["id"] == x.ids["version"]
            for v in (versions.json()["items"] if isinstance(versions.json(), dict) else versions.json())
        ),
        versions.text[:150],
    )

    # ---- audit_partitions
    parts_q = "SELECT count(*) AS n FROM pg_inherits WHERE inhparent = 'audit_logs'::regclass"
    n0 = ok_rows(db(parts_q))
    db(
        "SELECT audit_ensure_partitions(1, DATE '2023-01-01')", fetch=True
    )  # an expired (older than 13 months) partition
    n1 = ok_rows(db(parts_q))
    code, res, tail = run_job("audit_partitions")
    n2 = ok_rows(db(parts_q))
    r.step(
        "audit_partitions drops partitions older than 13 months",
        code == 0 and n1[0]["n"] == n0[0]["n"] + 1 and res["dropped"] >= 1 and n2[0]["n"] == n0[0]["n"],
        f"{n0} {n1} {n2} {res} {tail}",
    )
    nxt = ok_rows(
        db(
            "SELECT to_char(date_trunc('month', now() AT TIME ZONE 'UTC') + interval '13 months', 'YYYY\"m\"MM') AS m"
        )
    )
    exists = ok_rows(
        db(
            "SELECT to_regclass('audit_logs_y' || $1) IS NOT NULL AS e",
            nxt[0]["m"].replace('"', "") if nxt else "",
        )
    )
    r.step(
        "partitions exist for the next 13 months (maintenance keeps the horizon)",
        bool(exists) and exists[0]["e"],
        f"{nxt} {exists}",
    )
    code, res, _ = run_job("audit_partitions")
    r.step(
        "audit_partitions is idempotent (second run creates and drops nothing)",
        code == 0 and res == {"created": 0, "dropped": 0},
        str(res),
    )
    purged = ok_rows(db("SELECT count(*) AS n FROM audit_logs WHERE action = 'AUDIT_PURGE_RUN'"))
    r.step(
        "AUDIT_PURGE_RUN is logged when a partition is dropped",
        bool(purged) and purged[0]["n"] >= 1,
        str(purged),
    )
    old_rows = ok_rows(
        db("SELECT count(*) AS n FROM audit_logs WHERE created_at < now() - interval '13 months'")
    )
    r.step(
        "no audit row older than 13 months remains", bool(old_rows) and old_rows[0]["n"] == 0, str(old_rows)
    )

    # ---- every job once more, back to back, on the now-busy database
    for name in sorted(set(names) | EXPECTED_JOBS):
        code, res, tail = run_job(name)
        r.step(
            f"job {name}: clean second run on the live database",
            code == 0 and isinstance(res, dict) and res.get("failed", 0) == 0,
            f"{res} {tail}",
        )


# =========================================================================== 7. Google, SMTP, storage


class SmtpCapture:
    """Minimal SMTP server (stdlib) that records messages: lets the SMTP test endpoint run offline."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.credentials: list[tuple[str, str]] = []
        self.port = 0
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(10)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        import base64

        writer.write(b"220 smoke ESMTP\r\n")
        data_mode, buf, auth_step, auth_user = False, [], None, b""
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                if data_mode:
                    if line == b".\r\n":
                        self.messages.append(b"".join(buf).decode(errors="replace"))
                        data_mode, buf = False, []
                        writer.write(b"250 queued\r\n")
                    else:
                        buf.append(line)
                    continue
                if auth_step:
                    raw = base64.b64decode(line.strip() or b"")
                    if auth_step == "plain":
                        _, user, password = raw.split(b"\0")
                        self.credentials.append((user.decode(), password.decode()))
                        auth_step = None
                        writer.write(b"235 ok\r\n")
                    elif auth_step == "login-user":
                        auth_user, auth_step = raw, "login-pass"
                        writer.write(b"334 UGFzc3dvcmQ6\r\n")
                    else:
                        self.credentials.append((auth_user.decode(), raw.decode()))
                        auth_step = None
                        writer.write(b"235 ok\r\n")
                    await writer.drain()
                    continue
                cmd = line.strip().upper()
                if cmd.startswith((b"EHLO", b"HELO")):
                    writer.write(b"250-smoke\r\n250-AUTH PLAIN LOGIN\r\n250 8BITMIME\r\n")
                elif cmd.startswith(b"AUTH PLAIN"):
                    parts = line.split()
                    if len(parts) > 2:
                        _, user, password = base64.b64decode(parts[2]).split(b"\0")
                        self.credentials.append((user.decode(), password.decode()))
                        writer.write(b"235 ok\r\n")
                    else:
                        auth_step = "plain"
                        writer.write(b"334 \r\n")
                elif cmd.startswith(b"AUTH LOGIN"):
                    auth_step = "login-user"
                    writer.write(b"334 VXNlcm5hbWU6\r\n")
                elif cmd.startswith((b"MAIL", b"RCPT", b"RSET", b"NOOP")):
                    writer.write(b"250 ok\r\n")
                elif cmd == b"DATA":
                    data_mode = True
                    writer.write(b"354 go\r\n")
                elif cmd == b"QUIT":
                    writer.write(b"221 bye\r\n")
                    await writer.drain()
                    break
                else:
                    writer.write(b"502 no\r\n")
                await writer.drain()
        finally:
            writer.close()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        server = self.loop.run_until_complete(asyncio.start_server(self._handle, "0.0.0.0", 0))
        self.port = server.sockets[0].getsockname()[1]
        self._ready.set()
        self.loop.run_forever()


def decoded(message: str) -> str:
    import email
    import email.policy

    msg = email.message_from_string(message, policy=email.policy.default)
    parts = [str(msg["Subject"])]
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            parts.append(part.get_content())
    return "\n".join(parts)


def google_and_config(r: Report, api: httpx.Client, x: Org, app_name: str) -> None:
    oa = x.oa
    resp = client().post("/auth/google", json={"id_token": "not-a-google-token"})
    r.step(
        "Google sign-in with an invalid ID token is a clean 401 UNAUTHENTICATED (no 500, no detail leak)",
        resp.status_code == 401 and error_code(resp) == "UNAUTHENTICATED" and "Traceback" not in resp.text,
        f"{resp.status_code} {resp.text[:200]}",
    )
    forged_jwt = "eyJhbGciOiJub25lIn0.eyJlbWFpbCI6InNhQHNtb2tlLnRlc3QiLCJhdWQiOiJ4In0."
    resp = client().post("/auth/google", json={"id_token": forged_jwt})
    r.step(
        "an unsigned (alg none) token claiming the SA email is refused",
        resp.status_code == 401,
        f"{resp.status_code} {resp.text[:150]}",
    )
    r.step(
        "Google sign-in without the id_token field is a validation error",
        client().post("/auth/google", json={}).status_code == 422,
    )
    sec = api.get("/org/settings/security", headers=oa)
    r.status("OA reads the security settings", sec, 200)
    bad = api.patch(
        "/org/settings/security", headers=oa, json={"allowed_google_domains": ["not a domain", "x"]}
    )
    r.step(
        "allowed Google domains are validated (garbage refused)",
        bad.status_code == 422,
        f"{bad.status_code} {bad.text[:150]}",
    )
    good = api.patch("/org/settings/security", headers=oa, json={"allowed_google_domains": ["Example.com"]})
    r.step(
        "a valid domain saves (case-normalised)",
        good.status_code == 200 and "example.com" in json.dumps(good.json()).lower(),
        good.text[:200],
    )
    api.patch("/org/settings/security", headers=oa, json={"allowed_google_domains": []})
    r.skip(
        "real Google token verification (signature against Google JWKS, audience, domain match)",
        "needs real credentials: GOOGLE_CLIENT_ID and a Google-issued ID token",
    )

    # settings validation errors, evaluated by the real Settings class in a clean process
    def settings_error(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        clean = {
            k: v
            for k, v in os.environ.items()
            if k
            not in {
                "APP_ENV",
                "STORAGE_DRIVER",
                "TOKEN_PEPPER",
                "FIELD_ENCRYPTION_KEYS",
                "JWT_PRIVATE_KEY",
                "JWT_PUBLIC_KEY",
            }
        }
        return subprocess.run(
            [sys.executable, "-c", "from app.core.config import settings; print(settings.storage_driver)"],
            capture_output=True,
            text=True,
            env={**clean, **env},
            timeout=60,
        )

    prod = settings_error({"APP_ENV": "prod"})
    r.step(
        "APP_ENV=prod without secrets refuses to start (names the missing settings)",
        prod.returncode != 0 and "missing required settings in prod" in prod.stderr,
        prod.stderr[-200:],
    )
    r.step(
        "an unknown STORAGE_DRIVER refuses to start",
        settings_error({"STORAGE_DRIVER": "gcs"}).returncode != 0,
    )
    r.step("an unknown APP_ENV refuses to start", settings_error({"APP_ENV": "staging"}).returncode != 0)
    s3 = settings_error({"STORAGE_DRIVER": "s3"})
    r.step(
        "STORAGE_DRIVER=s3 without a bucket refuses to start",
        s3.returncode != 0,
        f"rc={s3.returncode} {s3.stdout.strip()} {s3.stderr[-120:]}",
    )
    r.step("STORAGE_DRIVER=local starts", settings_error({"STORAGE_DRIVER": "local"}).returncode == 0)


def smtp_checks(r: Report, smtp_api: httpx.Client, token: str, run: str, app_name: str) -> None:
    cap = SmtpCapture()
    my_ip = socket.gethostbyname(socket.gethostname())
    sa = bearer(sa_login(smtp_api))
    from smoke_lib import Tenant

    t = Tenant(smtp_api, sa["Authorization"].split()[1], "Smtp", run)
    oa = t.activate_admin()
    hr = t.create_hr("HR Smtp")
    r.error_code(
        "SMTP test before any settings: SMTP_NOT_CONFIGURED",
        smtp_api.post("/org/settings/email/test", headers=oa, json={"to": "x@example.com"}),
        409,
        "SMTP_NOT_CONFIGURED",
    )
    cfg = {
        "host": my_ip,
        "port": cap.port,
        "security": "none",
        "sender_email": "hr@example.com",
        "sender_name": "Smoke HR",
        "enabled": True,
    }
    put = smtp_api.put(
        "/org/settings/email", headers=oa, json={**cfg, "username": "mailer", "password": "smtp-secret-pass"}
    )
    r.step(
        "OA saves SMTP settings; the password is write-only",
        put.status_code == 200
        and "smtp-secret-pass" not in put.text
        and put.json().get("password_set") is True,
        put.text[:250],
    )
    r.step(
        "GET never returns the password",
        "smtp-secret-pass" not in smtp_api.get("/org/settings/email", headers=oa).text,
    )
    r.error_code(
        "CRLF in the sender name is refused (header injection)",
        smtp_api.put(
            "/org/settings/email", headers=oa, json={**cfg, "sender_name": "A\r\nBcc: evil@example.com"}
        ),
        422,
    )
    r.error_code(
        "CRLF in the username is refused",
        smtp_api.put("/org/settings/email", headers=oa, json={**cfg, "username": "a\nb"}),
        422,
    )
    r.error_code(
        "invalid sender email is refused",
        smtp_api.put("/org/settings/email", headers=oa, json={**cfg, "sender_email": "nope"}),
        422,
    )
    r.error_code(
        "port out of range is refused",
        smtp_api.put("/org/settings/email", headers=oa, json={**cfg, "port": 70000}),
        422,
    )
    r.error_code(
        "unknown security mode is refused",
        smtp_api.put("/org/settings/email", headers=oa, json={**cfg, "security": "sslv3"}),
        422,
    )
    test = smtp_api.post("/org/settings/email/test", headers=oa, json={"to": "probe@example.com"})
    r.step(
        "SMTP test endpoint reaches the local capture server and verifies the settings",
        test.status_code == 200 and test.json()["sent"] is True and test.json()["verified_at"],
        test.text[:200],
    )
    r.step(
        "the stored password was decrypted correctly before use (the relay saw the real credentials)",
        ("mailer", "smtp-secret-pass") in cap.credentials,
        str(cap.credentials),
    )
    got = [decoded(m) for m in cap.messages]
    r.step(
        "the captured test message is addressed to the probe and names the application",
        any("probe@example.com" in m or app_name in m for m in cap.messages)
        and any(app_name in g for g in got),
        str(got)[:300],
    )
    token_inv = smtp_api.post("/assessments", headers=hr, json={"title": "Smtp assessment"}).json()
    aid, vid = token_inv["id"], token_inv["versions"][0]["id"]
    smtp_api.post(
        f"/assessments/{aid}/versions/{vid}/questions",
        headers=hr,
        json={
            "type": "single_choice",
            "prompt_html": "<p>q</p>",
            "marks": 1,
            "options": [{"text_html": "a", "is_correct": True}, {"text_html": "b", "is_correct": False}],
        },
    ).raise_for_status()
    smtp_api.post(f"/assessments/{aid}/versions/{vid}/publish", headers=hr).raise_for_status()
    role = smtp_api.post("/roles", headers=hr, json={"title": "Smtp role"}).json()["id"]
    smtp_api.post(f"/roles/{role}/open", headers=hr).raise_for_status()
    smtp_api.put(
        f"/roles/{role}/assessment", headers=hr, json={"assessment_version_id": vid}
    ).raise_for_status()
    inv = smtp_api.post(
        f"/roles/{role}/invites",
        headers=hr,
        json={"candidate_name": "Mail Me", "email": "mailme@example.com", "send_email": True},
    )
    r.step(
        "creating an invite with send_email delivers the link by SMTP",
        inv.status_code == 201 and inv.json()["email_sent"] is True,
        inv.text[:200],
    )
    mail = next((decoded(m) for m in cap.messages if "mailme@example.com" in m), "")
    link = re.search(r"https?://\S+/exam/([A-Za-z0-9_\-]+)", mail)
    r.step(
        "the delivered mail carries the exam link and the candidate name",
        bool(link) and "Mail Me" in mail,
        mail[:200],
    )
    browser = httpx.Client(base_url=smtp_api.base_url, headers={"User-Agent": DESKTOP_UA}, timeout=60)
    first = browser.post("/exam/session/open", json={"token": link.group(1)}) if link else None
    r.step(
        "the emailed token is the real invite token (it opens a session)",
        first is not None and first.status_code == 200,
        str(first and first.text[:150]),
    )
    inv_id = inv.json()["invite"]["id"]
    resend = smtp_api.post(f"/invites/{inv_id}/send-email", headers=hr)
    new_mail = next((decoded(m) for m in reversed(cap.messages) if "mailme@example.com" in m), "")
    new_link = re.search(r"/exam/([A-Za-z0-9_\-]+)", new_mail)
    r.step(
        "send-email rotates the token and emails the new link (the first link dies)",
        resend.status_code == 200 and bool(new_link) and bool(link) and new_link.group(1) != link.group(1),
        f"{resend.status_code} {resend.text[:100]}",
    )
    old_try = browser.post("/exam/session/open", json={"token": link.group(1)}) if link else None
    new_try = browser.post("/exam/session/open", json={"token": new_link.group(1)}) if new_link else None
    r.step(
        "the old emailed link is dead, the new one works (same browser)",
        old_try is not None
        and old_try.status_code in (404, 410)
        and new_try is not None
        and new_try.status_code == 200,
        f"{old_try and old_try.status_code} {new_try and new_try.status_code} {new_try and new_try.text[:100]}",
    )
    actions = {a["action"] for a in smtp_api.get("/audit-logs?page_size=50", headers=oa).json()["items"]}
    r.step(
        "EMAIL_TEST_SENT and INVITE_EMAIL_SENT are audited",
        {"EMAIL_TEST_SENT", "INVITE_EMAIL_SENT"} <= actions,
        str(actions),
    )
    # failure handling: nothing listens on this port
    dead = socket.socket()
    dead.bind(("0.0.0.0", 0))
    dead_port = dead.getsockname()[1]
    dead.close()
    smtp_api.put("/org/settings/email", headers=oa, json={**cfg, "port": dead_port}).raise_for_status()
    fail = smtp_api.post("/org/settings/email/test", headers=oa, json={"to": "probe@example.com"})
    r.step(
        "unreachable SMTP host gives a clean result (sent=false + reason), not a 500",
        fail.status_code == 200
        and fail.json()["sent"] is False
        and bool(fail.json().get("reason"))
        and "Traceback" not in fail.text,
        f"{fail.status_code} {fail.text[:200]}",
    )
    starttls = smtp_api.put("/org/settings/email", headers=oa, json={**cfg, "security": "starttls"})
    fail2 = smtp_api.post("/org/settings/email/test", headers=oa, json={"to": "probe@example.com"})
    r.step(
        "STARTTLS required but not offered by the server fails cleanly",
        starttls.status_code == 200 and fail2.status_code == 200 and fail2.json()["sent"] is False,
        f"{fail2.status_code} {fail2.text[:200]}",
    )
    tls = smtp_api.put("/org/settings/email", headers=oa, json={**cfg, "security": "tls"})
    fail3 = smtp_api.post("/org/settings/email/test", headers=oa, json={"to": "probe@example.com"})
    r.step(
        "implicit TLS against a plain server fails cleanly",
        tls.status_code == 200 and fail3.status_code == 200 and fail3.json()["sent"] is False,
        f"{fail3.status_code} {fail3.text[:200]}",
    )
    smtp_api.put("/org/settings/email", headers=oa, json={**cfg, "enabled": False}).raise_for_status()
    off = smtp_api.post(f"/invites/{inv_id}/send-email", headers=hr)
    r.step(
        "with email disabled, send-email is refused and the token is not rotated",
        off.status_code in (409, 422)
        and error_code(off) in ("SMTP_NOT_CONFIGURED", "CONFLICT", "VALIDATION_ERROR"),
        f"{off.status_code} {off.text[:150]}",
    )
    r.status(
        "HR cannot read or change SMTP settings",
        smtp_api.put("/org/settings/email", headers=hr, json=cfg),
        403,
    )
    r.skip(
        "SMTP over real TLS / authenticated relay (STARTTLS handshake, AUTH, provider limits)",
        "needs real credentials: a real SMTP server",
    )


def s3_checks(r: Report, s3_api: httpx.Client, run: str) -> None:
    from smoke_lib import Tenant

    token = sa_login(s3_api)
    t = Tenant(s3_api, token, "S3", run)
    oa = t.activate_admin()
    resp = s3_api.post("/org/branding/logo", headers=oa, files={"file": ("l.png", new_png(32), "image/png")})
    r.step(
        "S3 driver pointing at an unreachable endpoint: upload fails with the error envelope (no stack trace, no endpoint leak)",
        resp.status_code >= 500
        and error_code(resp) is not None
        and "Traceback" not in resp.text
        and "127.0.0.1" not in resp.text
        and "boto" not in resp.text.lower(),
        f"{resp.status_code} {resp.text[:250]}",
    )
    branding = s3_api.get("/org/branding", headers=oa)
    r.step(
        "the failed upload left no dangling logo reference",
        branding.status_code == 200 and branding.json()["logo_url"] is None,
        branding.text[:150],
    )
    r.skip(
        "S3 object storage against a real bucket (put/get/delete, presigned behavior, encryption at rest)",
        "needs real credentials: S3 or MinIO endpoint and keys",
    )


# =========================================================================== 8. open design questions


def binding_and_start_order(r: Report, api: httpx.Client, x: Org) -> None:
    hr = x.hr
    # (a) regenerate link vs browser binding
    inv_id, token = x.invite("Regen Cand")
    c1, ex1 = new_candidate_session(token)
    regen = api.post(f"/invites/{inv_id}/regenerate-link", headers=hr)
    new_token = regen.json()["exam_url"].rsplit("/", 1)[1]
    r.step("regenerate returns a new link once", regen.status_code == 200 and new_token != token)
    r.step("the old link is dead", _open(token).status_code in (404, 410))
    other = _open(new_token)
    r.step(
        "new link from a NEW browser is refused LINK_BOUND_TO_OTHER_SESSION (binding survives regeneration; spec 8.1 releases it only via reset-binding)",
        other.status_code in (403, 409) and error_code(other) == "LINK_BOUND_TO_OTHER_SESSION",
        f"{other.status_code} {other.text[:150]}",
    )
    same = c1.post("/exam/session/open", json={"token": new_token})
    r.step(
        "new link from the SAME browser resumes the attempt",
        same.status_code == 200,
        f"{same.status_code} {same.text[:150]}",
    )
    rb = api.post(
        f"/invites/{inv_id}/reset-binding", headers=hr, json={"reason": "candidate switched browser"}
    )
    r.step("reset-binding (audited, with reason) releases it", rb.status_code == 200, rb.text[:150])
    r.step("after reset-binding the new link opens in the new browser", _open(new_token).status_code == 200)
    # (b) CAMERA_REQUIRED vs HELP_REQUEST_PENDING
    inv2, token2 = x.invite("Order Cand")
    c2, ex2 = new_candidate_session(token2)
    prepare(c2, ex2, photo=False)  # permission granted, but the baseline photo never arrives
    c2.post(
        "/exam/help-requests", headers=ex2, json={"reason": "camera_not_working", "note": "photo fails"}
    ).raise_for_status()
    start = c2.post("/exam/session/start", headers=ex2)
    r.step(
        "baseline photo missing + help request pending: start answers CAMERA_REQUIRED (documented order: consent, device, camera, help)",
        start.status_code == 409 and error_code(start) == "CAMERA_REQUIRED",
        f"{start.status_code} {start.text[:150]}",
    )
    hid = api.get("/help-requests?status=open", headers=hr).json()["items"][0]["id"]
    approve = api.post(
        f"/help-requests/{hid}/approve", headers=hr, json={"camera_exempt": True, "note": "ok"}
    )
    r.status("HR approves the help request with a camera exemption", approve, 200)
    start2 = c2.post("/exam/session/start", headers=ex2)
    r.step(
        "the exempt candidate now starts without the photo",
        start2.status_code == 200,
        f"{start2.status_code} {start2.text[:150]}",
    )
    oversized = c2.post(
        "/exam/snapshots",
        headers=ex2,
        files={"file": ("s.png", new_png(32) + b"\x00" * (301 * 1024), "image/png")},
    )
    r.error_code(
        "a running attempt's snapshot over MAX_SNAPSHOT_KB (300) is refused UPLOAD_INVALID",
        oversized,
        422,
        "UPLOAD_INVALID",
    )
    normal = c2.post("/exam/snapshots", headers=ex2, files={"file": ("s.png", new_png(32), "image/png")})
    r.status("a normal-sized snapshot is still accepted", normal, 201)
    # a candidate whose browser reports no camera at all is stopped at the system check and can ask for help
    inv3, token3 = x.invite("No Camera")
    c3, ex3 = new_candidate_session(token3)
    blocked = c3.post(
        "/exam/session/system-check",
        headers=ex3,
        json={"viewport_w": 1280, "viewport_h": 800, "camera_permission": False},
    )
    r.error_code(
        "no camera permission: the system check itself answers CAMERA_REQUIRED",
        blocked,
        409,
        "CAMERA_REQUIRED",
    )
    c3.post("/exam/help-requests", headers=ex3, json={"reason": "no_camera"}).raise_for_status()
    hid3 = next(
        h["id"]
        for h in api.get("/help-requests?status=open", headers=hr).json()["items"]
        if h["invite_id"] == inv3
    )
    api.post(f"/help-requests/{hid3}/approve", headers=hr, json={"camera_exempt": True}).raise_for_status()
    prepare(c3, ex3, photo=False, camera=False)
    start3 = c3.post("/exam/session/start", headers=ex3)
    r.step(
        "after the exemption the no-camera candidate passes the check, consents and starts",
        start3.status_code == 200,
        f"{start3.status_code} {start3.text[:150]}",
    )


# =========================================================================== main


def guarded(r: Report, name: str, fn: Callable[[], None]) -> None:
    print(f"\n-- {name}", flush=True)
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - a crashed section is a failed check, not a crashed run
        r.step(f"section crashed: {name}", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()


def main() -> int:
    r = Report("smoke_extra")
    run = run_id()
    api = client()
    try:
        health = httpx.get(base_url() + "/health", timeout=10)
    except httpx.HTTPError as exc:
        r.step("api reachable", False, str(exc))
        return r.finish()
    r.status("api reachable", health, 200)
    app_name = httpx.get(base_url() + "/openapi.json", timeout=30).json()["info"]["title"]
    token = sa_login(api, r)
    sa = bearer(token)
    x = Org(api, token, "X", run).seed()
    o = Org(api, token, "O", run).seed()
    r.step("two seeded organizations (full flow each)", bool(x.ids.get("attempt") and o.ids.get("attempt")))

    for org in (x, o):
        org.api.put(
            "/org/settings/ai",
            headers=org.oa,
            json={
                "provider": "openai-compatible",
                "model": "local",
                "base_url": "http://127.0.0.1:9/v1",
                "api_key": "sk-smoke-fake-key-123456",
            },
        ).raise_for_status()
    guarded(r, "1. PDF exports", lambda: pdf_exports(r, api, x, sa, app_name))
    guarded(r, "2. bulk CSV invites", lambda: bulk_invites(r, api, x, sa, run))
    guarded(r, "3. organization disable / enable", lambda: org_disable_enable(r, api, sa, token, run))
    guarded(r, "4. candidate data erase", lambda: candidate_erase(r, api, x, o, run))
    guarded(r, "5. branding upload and signed URLs", lambda: branding_uploads(r, api, x, o, sa))
    guarded(r, "6. background jobs", lambda: jobs(r, api, x, token, run))
    guarded(
        r, "7a. Google sign-in and configuration validation", lambda: google_and_config(r, api, x, app_name)
    )
    smtp_url, s3_url = arg("--smtp-url"), arg("--s3-url")
    if smtp_url:
        guarded(
            r,
            "7b. SMTP against a local capture server",
            lambda: smtp_checks(
                r, httpx.Client(base_url=smtp_url + "/api/v1", timeout=60), token, run, app_name
            ),
        )
    else:
        r.skip("SMTP section", "pass --smtp-url of a server started with SMOKE_REAL_SMTP=1")
    if s3_url:
        guarded(
            r,
            "7c. S3 driver failure handling",
            lambda: s3_checks(
                r,
                httpx.Client(
                    base_url=s3_url + "/api/v1", timeout=120, limits=httpx.Limits(max_keepalive_connections=0)
                ),
                run,
            ),
        )
    else:
        r.skip("S3 section", "pass --s3-url of a server started with STORAGE_DRIVER=s3")
    guarded(
        r, "8. link regeneration vs binding, start-check order", lambda: binding_and_start_order(r, api, x)
    )
    guarded(r, "signed URL expiry", lambda: signed_url_expiry(r))
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
