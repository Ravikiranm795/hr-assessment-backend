"""Branding strings and the starter theme. Product and vendor names come only from settings."""

from app.core.config import settings

# Curated font keys the frontend knows how to load (branding_themes.font_key).
FONT_KEYS: tuple[str, ...] = (
    "inter",
    "source_sans_3",
    "nunito_sans",
    "ibm_plex_sans",
    "lato",
    "merriweather",
)

BACKGROUND_STYLES: tuple[str, ...] = ("mint", "plain_white", "soft_gray", "navy_band")

# Starter palette (spec 16.3): navy, dark teal, coral, mint background; bright logo teal only as
# a small accent. Keys match branding_themes columns minus the ``_color`` suffix.
STARTER_THEME: dict[str, str] = {
    "primary": "#1B2A4A",  # navy
    "secondary": "#0E5E6F",  # dark teal
    "accent": "#F26B5B",  # coral
    "highlight": "#2EC4B6",  # bright logo teal, small accents only
    "background": "#EAF7F2",  # mint
    "background_style": "mint",
    "font_key": "inter",
}


def watermark() -> str:
    return f"Powered by {settings.vendor_name}"


def _luminance(hex_color: str) -> float:
    value = hex_color.lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    channels = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(color_a: str, color_b: str) -> float:
    """WCAG contrast ratio (1 to 21). M4 rejects themes below 4.5 with BRANDING_CONTRAST_TOO_LOW."""
    lighter, darker = sorted((_luminance(color_a), _luminance(color_b)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)
