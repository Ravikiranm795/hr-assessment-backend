"""HTML sanitizer for policy documents (spec 6/M4, D-11), built on nh3 (ammonia) with an allowlist.

No scripts, no event handlers, no inline styles, no images/media/forms/iframes, and only
``http``, ``https`` and ``mailto`` URLs (relative and protocol-relative URLs are dropped). The
frontend sanitizes again at render time.
"""

import nh3

ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
        "strong", "b", "em", "i", "u", "s", "sub", "sup", "code", "pre", "blockquote",
        "ul", "ol", "li",
        "a",
        "table", "thead", "tbody", "tr", "th", "td",
    }
)  # fmt: skip
ALLOWED_ATTRIBUTES: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "title"}),
    "th": frozenset({"colspan", "rowspan"}),
    "td": frozenset({"colspan", "rowspan"}),
}
ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto"})
# Elements whose text content is dropped as well, not just the tag.
STRIP_WITH_CONTENT: frozenset[str] = frozenset({"script", "style", "iframe", "object", "embed", "noscript"})


def sanitize_html(html: str) -> str:
    """Return ``html`` reduced to the allowlist. Idempotent."""
    if not html:
        return ""
    return nh3.clean(
        html,
        tags=set(ALLOWED_TAGS),
        attributes={tag: set(attrs) for tag, attrs in ALLOWED_ATTRIBUTES.items()},
        clean_content_tags=set(STRIP_WITH_CONTENT),
        url_schemes=set(ALLOWED_URL_SCHEMES),
        url_relative="deny",
        link_rel="noopener noreferrer",
        strip_comments=True,
    )
