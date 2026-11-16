"""Dev/smoke API launcher: the real app with the AI provider forced to a deterministic mock and
outgoing email captured in memory. Used only by scripts/smoke_*.py; never a production entrypoint.

    docker compose run --rm -p 8001:8000 api python scripts/smoke_server.py
"""

import os

import uvicorn

from app.core.ai import MockProvider, set_provider_override
from app.core.email import MemoryEmailSender, set_email_sender
from app.main import create_app


def _mock_ai(call: dict) -> dict:
    required = call["json_schema"].get("required", [])
    if "interpretations" in call["json_schema"].get("properties", {}):  # M11 report chat
        return {
            "interpretations": [
                {
                    "label": "Rejected candidates",
                    "spec": {
                        "source": "candidates",
                        "filters": [{"field": "decision", "op": "eq", "value": "rejected"}],
                        "columns": ["candidate_name", "role_title", "decision"],
                    },
                }
            ]
        }
    if "questions" in required:
        return {
            "questions": [
                {
                    "type": "single_choice",
                    "difficulty": "easy",
                    "prompt": f"Mock question {i}: which option is correct?",
                    "marks": 1,
                    "options": [
                        {"text": "right", "is_correct": True},
                        {"text": "wrong", "is_correct": False},
                        {"text": "also wrong", "is_correct": False},
                    ],
                }
                for i in range(12)
            ]
        }
    if "fraction" in required:
        return {"fraction": 0.5, "reason": "mock: half of the key points", "matched": ["a"], "missed": ["b"]}
    return {}


def build():
    set_provider_override(MockProvider(_mock_ai))
    if os.environ.get("SMOKE_REAL_SMTP") != "1":  # smoke_extra.py sets it to reach a local capture server
        set_email_sender(MemoryEmailSender())
    return create_app()


if __name__ == "__main__":
    uvicorn.run(build(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), proxy_headers=True)
