"""WCAG contrast helpers for the branding guardrail (spec 6/M4, A-12).

The rule: a theme colour used as a button or header fill must have a readable text colour,
that is white or the ink colour with a contrast ratio of at least 4.5 (WCAG AA, normal text).
"""

import math
import re
from dataclasses import dataclass

from app.core.branding_text import contrast_ratio

MIN_RATIO = 4.5
WHITE = "#FFFFFF"
INK = "#111827"

_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def normalize_hex(value: str) -> str:
    """``#abc`` or ``#aabbcc`` to upper-case ``#AABBCC``. ``ValueError`` for anything else."""
    if not isinstance(value, str) or not _HEX.match(value):
        raise ValueError("invalid hex colour")
    digits = value[1:].upper()
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    return f"#{digits}"


def check_contrast(fg: str, bg: str) -> float:
    """WCAG contrast ratio between two hex colours (1.0 to 21.0)."""
    return contrast_ratio(normalize_hex(fg), normalize_hex(bg))


@dataclass(frozen=True)
class TextContrast:
    color: str
    text_color: str  # best of white / ink on this fill
    ratio: float
    minimum: float = MIN_RATIO

    @property
    def passes(self) -> bool:
        return self.ratio >= self.minimum

    def as_dict(self) -> dict:
        return {
            "color": self.color,
            "text_color": self.text_color,
            "ratio": math.floor(self.ratio * 100) / 100,  # never round a failing ratio up to the minimum
            "minimum": self.minimum,
            "passes": self.passes,
        }


def check_text_contrast(fill: str) -> TextContrast:
    """Best readable text colour (white or ink) over ``fill`` and its ratio."""
    fill = normalize_hex(fill)
    ratio_white, ratio_ink = check_contrast(WHITE, fill), check_contrast(INK, fill)
    if ratio_white >= ratio_ink:
        return TextContrast(fill, WHITE, ratio_white)
    return TextContrast(fill, INK, ratio_ink)
