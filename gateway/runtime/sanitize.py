"""Detect hidden / invisible characters in untrusted text (content-injection layer 1).

Prompt injections hide instructions from a human reviewer while the LLM still
reads them: zero-width spaces, bidirectional overrides ("Trojan Source"), and
especially Unicode TAG characters (U+E0000..U+E007F) that smuggle a full invisible
ASCII string. Business data essentially never contains these, so a value carrying
them is a strong injection signal. We DETECT (and can strip) them so the
confirmation gate can warn the human and tool returns can be flagged.

Scope (the human/machine asymmetry). This is the ONE content-injection concern the
gateway should own: characters the LLM reads but the human CANNOT SEE. Injection in
*visible* text (a plausible but false review) is NOT the gateway's responsibility --
a human reading the same page would be fooled too, so there is no asymmetry for the
gateway to close, and being smarter than the human about content truth is not its
job. This layer flags only what the human's eyes cannot; visible content is the
human's call.
"""

from __future__ import annotations

from collections import Counter


def _category(cp: int) -> str | None:
    """Suspicious-character category for a codepoint, or None if ordinary."""
    if cp in (0x09, 0x0A, 0x0D):
        return None  # tab / newline / carriage return are ordinary whitespace
    if cp <= 0x1F or 0x7F <= cp <= 0x9F:
        return "control"
    if cp in (0x00AD, 0x061C, 0x180E, 0x200B, 0x200C, 0x200D, 0x200E, 0x200F,
              0x2060, 0x2061, 0x2062, 0x2063, 0x2064, 0xFEFF):
        return "zero-width"
    if 0x202A <= cp <= 0x202E or 0x2066 <= cp <= 0x2069:
        return "bidi-control"
    if 0xE0000 <= cp <= 0xE007F:
        return "tag (invisible ascii)"
    if 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF:
        return "variation-selector"
    return None


def find_hidden(text: str) -> list[tuple[int, int, str]]:
    """Return ``[(index, codepoint, category)]`` for each hidden character."""
    return [(i, ord(ch), cat)
            for i, ch in enumerate(text) if (cat := _category(ord(ch)))]


def has_hidden(text: str) -> bool:
    return any(_category(ord(ch)) for ch in text)


def strip_hidden(text: str) -> str:
    """Remove hidden characters, leaving the visible text a human actually sees."""
    return "".join(ch for ch in text if _category(ord(ch)) is None)


def describe_hidden(text: str) -> str:
    """Human summary of the hidden characters found (empty if none)."""
    found = find_hidden(text)
    if not found:
        return ""
    cats = Counter(cat for _, _, cat in found)
    parts = ", ".join(f"{n} {cat}" for cat, n in cats.items())
    return f"{len(found)} hidden character(s) [{parts}]"


# --- constrained extraction (content-injection layer 2) -------------------
# When a task needs a NUMBER, an untrusted-derived operand must BE a clean number.
# A numeric field carrying anything else ("5.0 IGNORE PREVIOUS ...", or a hidden
# char) is free-text smuggled through a slot that should hold only a scalar -- an
# injection, not a business value. This is a TYPE contract, not a truth judgment,
# so it stays in the gateway's scope.

def is_numeric_type(declared_type: str) -> bool:
    t = (declared_type or "").strip().lower()
    return t.startswith(("number", "integer", "int", "float", "double", "decimal"))


def numeric_conforms(value: object) -> bool:
    """True if ``value`` is a clean scalar number (int/float, or a string that is
    exactly a number). A string with any extra content or hidden char fails."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        s = value.strip()
        if not s or has_hidden(s):
            return False
        try:
            float(s)
            return True
        except ValueError:
            return False
    return False


def type_violation(value: object, declared_type: str) -> str:
    """Describe a constrained-extraction violation (empty if the value conforms).
    Currently enforces numeric-typed operands; other types are unconstrained."""
    if is_numeric_type(declared_type) and not numeric_conforms(value):
        return f"declared type '{declared_type}' but value is not a clean number"
    return ""
