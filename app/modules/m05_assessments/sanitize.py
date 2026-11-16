"""Local HTML sanitizer for question prompts, options, instructions and footers (nh3, allow-list)."""

import nh3

ALLOWED_TAGS = {
    "p", "br", "strong", "b", "em", "i", "u", "s", "sub", "sup", "ul", "ol", "li", "blockquote",
    "code", "pre", "span", "h1", "h2", "h3", "h4", "a", "table", "thead", "tbody", "tr", "th", "td",
}  # fmt: skip
ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "code": {"class"},
    "pre": {"class"},
    "span": {"class"},
    "th": {"colspan", "rowspan"},
    "td": {"colspan", "rowspan"},
}
ALLOWED_SCHEMES = {"http", "https", "mailto"}


def sanitize_html(value: str | None) -> str | None:
    """Return ``value`` with everything outside the allow-list removed (script, handlers, iframes,
    ``javascript:`` links); ``None`` stays ``None``."""
    if value is None:
        return None
    return nh3.clean(
        value,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes=ALLOWED_SCHEMES,
        link_rel="noopener noreferrer",
        strip_comments=True,
    )


def plain_text(value: str | None) -> str:
    """Visible text of an HTML fragment (used to reject prompts that sanitize to nothing)."""
    return nh3.clean(value or "", tags=set()).replace("&nbsp;", " ").strip()
