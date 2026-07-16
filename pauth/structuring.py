"""Deterministic, taint-preserving structuring of unstructured tool returns.

A read that returns free text (a bill, a landlord notice) hides values inside
prose. The restricted grammar has no string ops, so A1 cannot reference those
values -- they are prose-locked (the dominant G1 expressibility failure). This
layer runs a DETERMINISTIC, shape-keyed extractor over such text and exposes the
values as typed FIELDS, so a plan could reference ``view.ibans[0]`` /
``view.amounts[0]`` inside the grammar.

Two properties make it safe to add without touching FN=0:

* provenance-preserving -- ``structure`` is a pure function of the text, so the
  enforcer can re-derive and verify any field it produced.
* taint-preserving -- the text came from an untrusted source, so the view is
  marked ``taint=True``. Structuring does NOT launder trust: a side-effecting
  use of a tainted field must still be gated (human confirmation). Typing is
  syntactic; it never turns an untrusted value into a trusted one.

Shape-keyed, NOT document-keyed: it extracts by value SHAPE (money, IBAN, date,
email) rather than by knowing "this is a bill", so it does not overfit to any
one suite/framework. The cost of that generality is real and measured: values
with no regular shape (a free-form street address) are NOT reliably recovered,
and values that must be COMPUTED (rent = old + increase) are out of scope
entirely -- extraction surfaces an operand, it does not do arithmetic.
"""

from __future__ import annotations

import dataclasses
import re

# A money amount: 98.70, 1,234.56. Requires the cents to avoid matching bare
# integers / years. Not thousands-grouped integers without decimals (1200).
_AMOUNT = re.compile(r"(?<![\d.])\d{1,3}(?:,\d{3})*\.\d{2}(?!\d)")
# An IBAN-shaped token: 2 letters, 2 check digits, then 8-30 alphanumerics.
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{8,30}\b")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


def _uniq(xs: list) -> list:
    seen, out = set(), []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


@dataclasses.dataclass
class StructuredView:
    """Typed fields extracted from an untrusted text blob.

    ``taint`` is True by construction: the source was untrusted, so every field
    here is untrusted-derived and a side-effecting use must be gated.
    """

    amounts: list[float]
    ibans: list[str]
    dates: list[str]
    emails: list[str]
    lines: list[str]
    taint: bool = True

    def candidates(self) -> set:
        """All typed scalar candidates, for availability checks."""
        return {*self.amounts, *self.ibans, *self.dates, *self.emails}


def structure(text: str) -> StructuredView:
    """Deterministically extract shape-typed fields from ``text``."""
    return StructuredView(
        amounts=_uniq([float(m.replace(",", "")) for m in _AMOUNT.findall(text)]),
        ibans=_uniq(_IBAN.findall(text)),
        dates=_uniq(_DATE.findall(text)),
        emails=_uniq(_EMAIL.findall(text)),
        lines=[ln.strip() for ln in text.splitlines() if ln.strip()],
    )
