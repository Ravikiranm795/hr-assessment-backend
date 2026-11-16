"""Importing this package registers every gadget."""

from app.modules.m12_dashboard.gadgets import (  # noqa: F401
    common_gadgets,
    hr_gadgets,
    oa_gadgets,
    platform_gadgets,
)
from app.modules.m12_dashboard.gadgets.base import GADGETS, Gadget, register_gadget

__all__ = ["GADGETS", "Gadget", "register_gadget"]
