"""Delimiting of untrusted text (job descriptions, notes, candidate answers)."""

import re

_DELIM = re.compile(r"[<>]{3,}")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def neutralize(text: str) -> str:
    """Remove control characters and break up any ``<<<`` or ``>>>`` run so the text cannot
    forge or close a delimiter block."""
    return _DELIM.sub(lambda m: m.group(0).replace("<", "(").replace(">", ")"), _CONTROL.sub("", text))


def untrusted_block(label: str, text: str) -> str:
    """``text`` wrapped in a labelled block. The label is a fixed template constant."""
    return f"<<<{label}_UNTRUSTED_DATA>>>\n{neutralize(text)}\n<<<END_{label}_UNTRUSTED_DATA>>>"
