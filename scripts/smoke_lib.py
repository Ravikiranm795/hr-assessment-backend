"""Shared helpers for scripts/smoke_e2e.py and scripts/smoke_security.py (live HTTP checks, httpx only).

Both scripts talk to a RUNNING api (default http://localhost:8001, override with ``--base-url`` or
``SMOKE_BASE_URL``). They create their own organizations with unique names, so they can be re-run
against the same database. Start the target with ``scripts/smoke_server.py`` (mock AI provider, captured
email); see docs/smoke_report.md.
"""

import base64
import hashlib
import hmac
import os
import struct
import sys
import time
import uuid
import zlib
from typing import Any

import httpx

API = "/api/v1"
SA_EMAIL = os.environ.get("SMOKE_SA_EMAIL", "sa@smoke.test")
SA_PASSWORD = os.environ.get("SMOKE_SA_PASSWORD", "Smoke-SA-Password-123!")
STRONG_PASSWORD = "Smoke-Strong-Passphrase-42!"
DESKTOP_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"


def base_url() -> str:
    for i, arg in enumerate(sys.argv):
        if arg == "--base-url" and i + 1 < len(sys.argv):
            return sys.argv[i + 1].rstrip("/")
    return os.environ.get("SMOKE_BASE_URL", "http://localhost:8001").rstrip("/")


def run_id() -> str:
    return uuid.uuid4().hex[:8]


class Report:
    def __init__(self, title: str) -> None:
        self.title = title
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.failures: list[str] = []
        print(f"== {title} ==", flush=True)

    def step(self, name: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"PASS  {name}", flush=True)
        else:
            self.failed += 1
            self.failures.append(name)
            print(f"FAIL  {name}  {detail}"[:600], flush=True)
        return ok

    def skip(self, name: str, why: str) -> None:
        self.skipped += 1
        print(f"SKIP  {name}  ({why})", flush=True)

    def status(self, name: str, resp: httpx.Response, *expected: int) -> bool:
        return self.step(name, resp.status_code in expected, f"got {resp.status_code} {resp.text[:300]}")

    def error_code(self, name: str, resp: httpx.Response, status: int, code: str | None = None) -> bool:
        got = None
        try:
            got = resp.json().get("error", {}).get("code")
        except Exception:  # noqa: BLE001
            pass
        ok = resp.status_code == status and (code is None or got == code)
        return self.step(name, ok, f"want {status} {code}, got {resp.status_code} {resp.text[:300]}")

    def finish(self) -> int:
        print(
            f"\n{self.title}: {self.passed} passed, {self.failed} failed, {self.skipped} skipped",
            flush=True,
        )
        for name in self.failures:
            print(f"  FAILED: {name}")
        return 1 if self.failed else 0


def client(**kwargs: Any) -> httpx.Client:
    kwargs.setdefault("timeout", 60)
    return httpx.Client(base_url=base_url() + API, **kwargs)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def login(c: httpx.Client, email: str, password: str) -> httpx.Response:
    return c.post("/auth/login", json={"email": email, "password": password})


def totp_code(secret_b32: str, at: float | None = None) -> str:
    key = base64.b32decode(secret_b32.replace(" ", "").upper() + "=" * (-len(secret_b32) % 8))
    counter = int((at if at is not None else time.time()) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{value:06d}"


def png_bytes(size: int = 32) -> bytes:
    """A valid small PNG (passes the server's magic-byte and header checks)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x80\x80\x80" * size for _ in range(size))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def sa_login(c: httpx.Client, report: Report | None = None) -> str:
    resp = login(c, SA_EMAIL, SA_PASSWORD)
    if report is not None:
        report.status("super admin login", resp, 200)
    resp.raise_for_status()
    return resp.json()["access_token"]


def first_login(c: httpx.Client, email: str, temp_password: str, new_password: str = STRONG_PASSWORD) -> str:
    """Login with a temporary password, change it, log in again. Returns the new access token."""
    resp = login(c, email, temp_password)
    resp.raise_for_status()
    body = resp.json()
    if body.get("must_change_password"):
        change = c.post(
            "/auth/change-password",
            json={"current_password": temp_password, "new_password": new_password},
            headers=bearer(body["access_token"]),
        )
        change.raise_for_status()
        resp = login(c, email, new_password)
        resp.raise_for_status()
        body = resp.json()
    return body["access_token"]


class Tenant:
    """One organization with an org admin and an HR user, both ready to use."""

    def __init__(self, sa: httpx.Client, sa_token: str, label: str, run: str) -> None:
        self.label = label
        self.run = run
        self.sa_headers = bearer(sa_token)
        resp = sa.post(
            "/platform/orgs",
            headers=self.sa_headers,
            json={
                "name": f"Smoke {label} {run}",
                "admin": {"name": f"Admin {label}", "email": f"admin-{label.lower()}-{run}@example.com"},
            },
        )
        resp.raise_for_status()
        body = resp.json()
        self.org_id: str = body["organization"]["id"]
        self.admin_id: str = body["admin"]["id"]
        self.admin_email: str = body["admin"]["email"]
        self.admin_temp_password: str = body["temporary_password"]
        self.admin_password = STRONG_PASSWORD
        self._c = sa
        self.oa_headers: dict[str, str] = {}
        self.hr_headers: dict[str, str] = {}
        self.hr_id = ""
        self.hr_email = ""
        self.hr_temp_password = ""

    def activate_admin(self) -> dict[str, str]:
        token = first_login(self._c, self.admin_email, self.admin_temp_password, self.admin_password)
        self.oa_headers = bearer(token)
        return self.oa_headers

    def create_hr(self, name: str = "HR One") -> dict[str, str]:
        email = f"hr-{self.label.lower()}-{self.run}@example.com"
        resp = self._c.post("/team/users", headers=self.oa_headers, json={"name": name, "email": email})
        resp.raise_for_status()
        body = resp.json()
        user = body.get("user", body)
        self.hr_id = user["id"]
        self.hr_email = email
        self.hr_temp_password = body["temporary_password"]
        self.hr_headers = bearer(first_login(self._c, email, self.hr_temp_password))
        return self.hr_headers


def question_payloads() -> list[dict[str, Any]]:
    """One question of each of the five types (answer shapes match scoring_defs)."""
    return [
        {
            "type": "single_choice",
            "prompt_html": "<p>2 + 2 = ?</p>",
            "marks": 2,
            "options": [{"text_html": "4", "is_correct": True}, {"text_html": "5", "is_correct": False}],
        },
        {
            "type": "multi_choice",
            "prompt_html": "<p>Pick the even numbers</p>",
            "marks": 2,
            "options": [
                {"text_html": "2", "is_correct": True},
                {"text_html": "3", "is_correct": False},
                {"text_html": "4", "is_correct": True},
            ],
            "answer_key": {"partial": False},
        },
        {
            "type": "fill_blank",
            "prompt_html": "<p>The capital of France is ___ and of Italy is ___</p>",
            "marks": 2,
            "answer_key": {
                "blanks": [{"accepted": ["Paris"]}, {"accepted": ["Rome"]}],
                "case_sensitive": False,
            },
        },
        {
            "type": "code_gap",
            "prompt_html": "<p>Complete: for i in ___(3): print(i)</p>",
            "marks": 2,
            "code_language": "python",
            "answer_key": {"gaps": [{"accepted": ["range"]}], "case_sensitive": True},
        },
        {
            "type": "short_answer",
            "prompt_html": "<p>Explain what an index does in a database.</p>",
            "marks": 4,
            "scoring_mode": "ai",
            "model_answer": "An index speeds up lookups by keeping a sorted structure over a column.",
            "answer_key": {"key_points": ["speeds up lookups", "sorted structure"]},
        },
    ]
