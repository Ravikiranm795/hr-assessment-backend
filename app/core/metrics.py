"""Basic in-process metrics counters (spec 4.12) (endpoint: ``metrics_router.py``).

Counters live in the memory of one process: the API process reports HTTP requests, AI calls and exam
starts; the worker process counts job runs (not scrapable, they are also logged as ``job_run`` events).
Prometheus text format, so any scraper can read it. Never put ids, emails or other personal data in labels."""

from collections import defaultdict

HELP = {
    "http_requests_total": "HTTP requests by method, route template and status.",
    "http_request_duration_seconds_sum": "Total request handling time by method and route template.",
    "http_request_duration_seconds_count": "Number of timed requests by method and route template.",
    "ai_calls_total": "AI provider generate calls by provider and outcome (ok or error).",
    "exam_starts_total": "Exams started by candidates.",
    "job_runs_total": "Background job runs by job and outcome (ok or error); worker process only.",
    "audit_events_total": "Audit entries written by action.",
}

_counters: dict[str, dict[tuple[tuple[str, str], ...], float]] = defaultdict(lambda: defaultdict(float))


def inc(name: str, value: float = 1, **labels: str) -> None:
    _counters[name][tuple(sorted((k, str(v)) for k, v in labels.items()))] += value


def snapshot() -> dict[str, dict[tuple[tuple[str, str], ...], float]]:
    return {name: dict(series) for name, series in _counters.items()}


def reset() -> None:
    _counters.clear()


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render() -> str:
    lines: list[str] = []
    for name in sorted(_counters):
        lines.append(f"# HELP {name} {HELP.get(name, name)}")
        lines.append(f"# TYPE {name} counter")
        for labels, value in sorted(_counters[name].items()):
            label_text = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
            lines.append(f"{name}{{{label_text}}} {value:g}" if label_text else f"{name} {value:g}")
    return "\n".join(lines) + "\n"
