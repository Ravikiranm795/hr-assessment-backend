"""Swagger completeness (spec 15): every operation is documented, gated and typed, and the bearer and
exam-session security schemes exist."""

from app.main import create_app

_METHODS = ("get", "put", "post", "delete", "patch")


def _has_example(schema: dict, defs: dict, depth: int = 0) -> bool:
    if not schema or depth > 5:
        return False
    if "example" in schema or "examples" in schema:
        return True
    if "$ref" in schema:
        return _has_example(defs.get(schema["$ref"].split("/")[-1], {}), defs, depth + 1)
    if "items" in schema:
        return _has_example(schema["items"], defs, depth + 1)
    return any(
        _has_example(s, defs, depth + 1) for k in ("allOf", "anyOf", "oneOf") for s in schema.get(k, [])
    )


def test_every_operation_documented_and_security_schemes_present() -> None:
    document = create_app().openapi()
    defs = document["components"]["schemas"]
    problems: list[str] = []
    count = 0
    for path, item in document["paths"].items():
        for method in _METHODS:
            operation = item.get(method)
            if operation is None:
                continue
            count += 1
            where = f"{method.upper()} {path}"
            for field in ("summary", "description"):
                if not operation.get(field):
                    problems.append(f"{where}: no {field}")
            if "x-roles" not in operation or "x-required-flag" not in operation:
                problems.append(f"{where}: missing x-roles / x-required-flag")
            if "security" not in operation:
                problems.append(f"{where}: no security requirement (public must say [])")
            statuses = [code for code in operation["responses"] if code[0] in "45" and code != "422"]
            if not statuses:
                problems.append(f"{where}: no error response")
            for code, response in operation["responses"].items():
                if code[0] != "2":
                    continue
                contents = list(response.get("content", {}).values())
                documented = any(
                    "example" in c or "examples" in c or _has_example(c.get("schema"), defs) for c in contents
                )
                if contents and not documented:  # (a PDF body next to a CSV example is fine)
                    problems.append(f"{where}: {code} response has no example")
    assert count > 150
    assert not problems, "\n".join(problems)

    schemes = document["components"]["securitySchemes"]
    assert schemes["bearerAuth"]["scheme"] == "bearer"
    assert schemes["examSession"]["scheme"] == "bearer"
    assert schemes["examBindingCookie"]["in"] == "cookie"
    login = document["paths"]["/api/v1/auth/login"]["post"]
    exam = document["paths"]["/api/v1/exam/questions"]["get"]
    staff = document["paths"]["/api/v1/roles"]["get"]
    assert login["security"] == []
    assert exam["security"] == [{"examSession": [], "examBindingCookie": []}]
    assert staff["security"] == [{"bearerAuth": []}]
