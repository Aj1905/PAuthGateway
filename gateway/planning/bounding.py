"""Detect an unbounded-loop wall and turn it into a grill for the missing bound.

A task that needs ``while`` (an unbounded, data-dependent loop) is inexpressible:
the enforcer cannot enumerate the authorised calls, so FN=0 cannot be proven.
Rather than fail with a bare G1 error, we detect the wall BEFORE execution and
ask the human for the bound -- the termination witness the DSL requires.

The human's answer is TRUSTED (a human is the authority), so it is materialised
as a signed, bounded collection and iterated with the EXISTING bounded-for. So
this module only adds DETECTION + the grill request; it grows no enforcement
surface (no new re-derivation in the enforcer -- the bounded plan is ordinary
bounded-for-over-a-collection, see pauth/suites/installments.py).

What it does NOT do (honest scope): it does not auto-rewrite while->for (that is
task-specific), and it cannot rescue open-ended tasks -- bounding "monitor
forever" to "monitor 30 days" SUBSTITUTES a different task, which must be an
explicit human decision, not a silent narrowing.
"""

from __future__ import annotations

import ast
import dataclasses


@dataclasses.dataclass(frozen=True)
class UnboundedWall:
    """An inexpressible unbounded construct found in a candidate plan."""

    construct: str      # "while"
    condition: str      # the unparsed loop condition, shown to the human
    lineno: int

    def grill_question(self) -> str:
        return (
            f"This task needs an unbounded loop (while {self.condition}), which the "
            "gateway cannot authorise as-is: it cannot enumerate how many calls the "
            "loop will make, so it cannot prove each one is intended. To run it "
            "safely, state the BOUND explicitly -- a maximum number of iterations, "
            "or the exact list of items to act on. Your answer is authoritative and "
            "becomes a fixed, signed schedule the plan iterates."
        )


def detect_unbounded(code: str) -> UnboundedWall | None:
    """Return the first unbounded-loop wall in ``code``, or None if it is already
    bounded. This is the pre-execution 'is this a grammar wall we can grill past?'
    check -- distinct from a hard grammar reject (a method call, say), which no
    bound can fix."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.While):
            return UnboundedWall("while", ast.unparse(node.test), node.lineno)
    return None


def is_boundable_wall(code: str) -> bool:
    """True iff the plan is inexpressible ONLY because of an unbounded loop -- i.e.
    a wall a human-supplied bound can convert into a valid bounded-for."""
    return detect_unbounded(code) is not None
