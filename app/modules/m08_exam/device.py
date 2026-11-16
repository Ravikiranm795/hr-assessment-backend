"""Device checks (spec 8.2): laptop or desktop only, viewport at least 900 px wide."""

import re

from app.core.errors import AppError

MIN_VIEWPORT_W = 900
_MOBILE = re.compile(
    r"iphone|ipod|ipad|android|mobile|tablet|silk|kindle|playbook|windows phone|blackberry|opera mini", re.I
)


def is_supported_user_agent(user_agent: str | None) -> bool:
    return not _MOBILE.search(user_agent or "")


def check_device(user_agent: str | None, viewport_w: int) -> None:
    """``DEVICE_NOT_SUPPORTED`` (``details.reason``) for phones, tablets and narrow windows."""
    if not is_supported_user_agent(user_agent):
        raise AppError("DEVICE_NOT_SUPPORTED", details={"reason": "user_agent"})
    if viewport_w < MIN_VIEWPORT_W:
        raise AppError("DEVICE_NOT_SUPPORTED", details={"reason": "viewport", "min_width": MIN_VIEWPORT_W})
