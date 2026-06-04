"""L3 -- reference code + forced-injection fixtures (paper sec. 5.1).

The canonical fixture type for paper-style A2/A3/B1-B4 evaluation: skip the
LLM A1, hand the enforcer a reference ``run`` function known to be correct,
and check that a list of malicious calls is rejected one by one.

This file currently defines the type only. The historical worked-example
data lives in ``pauth/suites/shopping.py`` (the ``_TASKS`` list and the
``_AURORA_CODE`` / ``_CHEAPEST_CODE`` strings). Migrating that data here
would invert the dependency direction (``pauth`` → ``tests``), so the move
is deferred. Use this module for *new* reference fixtures going forward;
import from ``pauth.suites.shopping`` for the historical ones until a
suite-data layer is introduced.
"""

from __future__ import annotations

import dataclasses
from typing import Any


@dataclasses.dataclass
class ForcedCall:
    """A spurious tool call the enforcer must reject."""

    tool: str
    args: list[Any]
    note: str = ""


@dataclasses.dataclass
class ReferenceCase:
    """A reference task: hand-written run() plus injections to probe it.

    ``reference_code`` MUST conform to ``pauth.grammar``. The enforcer is
    expected to (a) permit a benign execution of ``reference_code`` with
    zero false positives, and (b) reject every entry in
    ``forced_injections`` (zero false negatives).
    """

    id: str
    suite: str
    prompt: str
    reference_code: str
    forced_injections: list[ForcedCall]


REFERENCES: list[ReferenceCase] = [
    # Intentionally empty. Add new reference cases here. Historical
    # paper-worked examples are still in pauth/suites/shopping.py:_TASKS.
]
