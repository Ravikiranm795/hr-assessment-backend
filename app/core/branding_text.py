"""Branding strings derived from settings. Task 2 extends this module (STARTER_THEME)."""

from app.core.config import settings


def watermark() -> str:
    return f"Powered by {settings.vendor_name}"
