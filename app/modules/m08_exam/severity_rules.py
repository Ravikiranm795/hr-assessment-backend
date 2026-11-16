"""Server-side severity rules for proctoring events (spec 8.5). The client's severity is never trusted."""

EVENT_TYPES = frozenset(
    {
        "tab_switch", "window_blur", "fullscreen_exit", "no_face", "multiple_faces", "looking_away",
        "camera_lost", "copy_attempt", "paste_attempt", "cut_attempt", "context_menu", "devtools_shortcut",
        "devtools_suspected", "page_reload", "offline",
    }
)  # fmt: skip
TAB_TYPES = frozenset({"tab_switch", "window_blur"})
SEVERITY_ORDER = ("info", "minor", "moderate", "serious")
LONG_MS = 10_000
TAB_ESCALATION = 3

_BASE = {
    "tab_switch": "minor", "window_blur": "minor", "fullscreen_exit": "moderate", "no_face": "minor",
    "multiple_faces": "serious", "looking_away": "minor", "camera_lost": "serious", "copy_attempt": "minor",
    "paste_attempt": "minor", "cut_attempt": "minor", "context_menu": "info", "devtools_shortcut": "minor",
    "devtools_suspected": "moderate", "page_reload": "minor", "offline": "info",
}  # fmt: skip


def severity_for(event_type: str, duration_ms: int | None = None, prior_tab_events: int = 0) -> str:
    """Severity of one event. ``prior_tab_events`` counts earlier tab_switch/window_blur events of the
    attempt: the third raises to serious. Long tab absences and long no-face spells are moderate."""
    severity = _BASE[event_type]
    if event_type in TAB_TYPES:
        if duration_ms is not None and duration_ms > LONG_MS:
            severity = "moderate"
        if prior_tab_events + 1 >= TAB_ESCALATION:
            severity = "serious"
    elif event_type == "no_face" and duration_ms is not None and duration_ms > LONG_MS:
        severity = "moderate"
    return severity
