"""OpenAPI enrichment that applies to every operation (spec 15): the bearer and exam-session
security schemes, a per-operation ``security`` requirement derived from ``x-roles``, and generated
response examples for operations whose response model carries none of its own.

Metadata only: nothing here changes request handling. Hand-written examples always win.
"""

from typing import Any

BEARER = "bearerAuth"
EXAM_SESSION = "examSession"
EXAM_BINDING = "examBindingCookie"

SECURITY_SCHEMES: dict[str, dict[str, Any]] = {
    BEARER: {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": (
            "Staff access token (audience `api`) from `POST /api/v1/auth/login`, sent as "
            "`Authorization: Bearer <token>`. Lives 15 minutes; renew with `POST /api/v1/auth/refresh`."
        ),
    },
    EXAM_SESSION: {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": (
            "Candidate exam session token (audience `exam`) from `POST /api/v1/exam/session/open`, sent as "
            "`Authorization: Bearer <token>`. Valid for at most 12 hours."
        ),
    },
    EXAM_BINDING: {
        "type": "apiKey",
        "in": "cookie",
        "name": "exam_binding",
        "description": (
            "HttpOnly browser-binding cookie set by `POST /api/v1/exam/session/open`. Exam routes refuse a "
            "token that arrives without it (`LINK_BOUND_TO_OTHER_SESSION`)."
        ),
    },
}

INFO_DESCRIPTION = (
    "Every operation declares `x-roles` (who may call it) and `x-required-flag` (the feature flag that must "
    "be on for the organization; `null` when ungated). Public operations have `x-roles: []` and no security "
    "requirement. Errors use one envelope: `{error: {code, message_key, details, request_id}}`. "
    "Response examples without a hand-written one are generated from the response model."
)

_UUID = "0b6f5c0e-5d3a-4c8e-9a64-2f1c3e8d7a10"
_NOW = "2026-11-16T09:30:00Z"
_METHODS = ("get", "put", "post", "delete", "options", "head", "patch")


def apply_security(operation: dict[str, Any]) -> None:
    roles = operation.get("x-roles") or []
    if not roles:
        operation["security"] = []
    elif roles == ["candidate"]:
        operation["security"] = [{EXAM_SESSION: [], EXAM_BINDING: []}]
    else:
        operation["security"] = [{BEARER: []}]


# --------------------------------------------------------------------------- generated examples


def _by_name(name: str, prop: dict[str, Any]) -> Any:
    lowered = name.lower()
    if lowered == "id" or lowered.endswith("_id") or lowered.endswith("_ids"):
        return [_UUID] if lowered.endswith("_ids") else _UUID
    if lowered.endswith("_at") or lowered in {"expires_at", "deadline", "date"}:
        return _NOW
    if "email" in lowered:
        return "asha.rao@example.com"
    if lowered.endswith("url") or lowered == "exam_url":
        return "https://app.example.com/resource"
    if lowered in {"name", "candidate_name", "full_name"}:
        return "Asha Rao"
    if lowered in {"title", "role_title"}:
        return "Senior Java Developer"
    if lowered.endswith("percent") or lowered.endswith("_score") or lowered == "score":
        return 82.5
    if "token" in lowered:
        return "eyJhbGciOiJFZERTQSJ9.example.signature"
    if "html" in lowered:
        return "<p>Example</p>"
    if lowered == "status":
        return "active"
    if lowered in {"description", "note", "reason"}:
        return "Example text"
    if lowered in {"locale"}:
        return "en"
    if lowered in {"message_key"}:
        return "errors.common.example"
    return None


def _example(schema: dict[str, Any] | None, defs: dict[str, Any], name: str = "", depth: int = 0) -> Any:
    if not schema or depth > 6:
        return None
    if "example" in schema:
        return schema["example"]
    if schema.get("examples"):
        return schema["examples"][0]
    if "$ref" in schema:
        target = defs.get(schema["$ref"].split("/")[-1])
        return _example(target, defs, name, depth + 1)
    for combiner in ("allOf", "anyOf", "oneOf"):
        options = [s for s in schema.get(combiner, []) if s.get("type") != "null"]
        if options:
            return _example(options[0], defs, name, depth + 1)
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    if "default" in schema and schema["default"] is not None:
        return schema["default"]
    kind = schema.get("type")
    named = _by_name(name, schema) if name else None
    if kind == "object" or "properties" in schema:
        props = schema.get("properties", {})
        if not props:
            return {}
        return {key: _example(sub, defs, key, depth + 1) for key, sub in props.items()}
    if kind == "array":
        item = _example(schema.get("items"), defs, name, depth + 1)
        return [] if item is None else [item]
    if kind == "boolean":
        return True
    if kind == "integer":
        return named if isinstance(named, int) else max(schema.get("minimum", 0), 1)
    if kind == "number":
        return named if isinstance(named, int | float) else float(max(schema.get("minimum", 0), 1))
    if kind == "string":
        fmt = schema.get("format")
        if fmt == "uuid":
            return _UUID
        if fmt == "date-time":
            return _NOW
        if fmt == "date":
            return "2026-11-16"
        if fmt == "email":
            return "asha.rao@example.com"
        return named if isinstance(named, str) else "string"
    return None


def add_response_examples(schema: dict[str, Any]) -> None:
    defs = schema.get("components", {}).get("schemas", {})
    for item in schema.get("paths", {}).values():
        for method in _METHODS:
            operation = item.get(method)
            if not operation:
                continue
            for status, response in operation.get("responses", {}).items():
                if not str(status).startswith("2"):
                    continue
                for content in response.get("content", {}).values():
                    if "example" in content or "examples" in content or "schema" not in content:
                        continue
                    value = _example(content["schema"], defs)
                    if value is not None:
                        content["example"] = value


def enrich(schema: dict[str, Any]) -> None:
    components = schema.setdefault("components", {})
    components.setdefault("securitySchemes", {}).update(SECURITY_SCHEMES)
    info = schema.setdefault("info", {})
    info.setdefault("description", INFO_DESCRIPTION)
    for item in schema.get("paths", {}).values():
        for method in _METHODS:
            if method in item:
                apply_security(item[method])
    add_response_examples(schema)
