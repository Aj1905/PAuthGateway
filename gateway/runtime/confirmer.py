"""Human-in-the-loop confirmers: WHO answers a gated call, and how.

The confirmation gate holds a side-effecting call whose control operand is
untrusted-derived. The gate's security IS the answer it gets: a vigilant human
rejects a poisoned value; a rubber-stamping one approves it. So a benchmark that
skips the gate measures nothing about this layer. These confirmers make the
decision explicit and pluggable:

  * InteractiveConfirmer -- a real human on stdin (when a person runs the bench).
  * VigilantConfirmer    -- heeds every warning: the UPPER bound on gate security.
  * TrustingConfirmer    -- rubber-stamps: the LOWER bound (worst-case human).
  * ScriptedConfirmer    -- fixed answers, for deterministic tests.

Reporting security as a range [Trusting .. Vigilant] is the honest HITL story:
the gate's protection depends on the human, and a benchmark must show that.
"""

from __future__ import annotations

import dataclasses
from typing import Protocol

from gateway.runtime.confirmation import PendingConfirmation


class Confirmer(Protocol):
    def confirm(self, pending: PendingConfirmation) -> bool: ...


class TrustingConfirmer:
    """Approves everything -- the worst-case human (lower bound on gate security)."""

    name = "trusting"

    def confirm(self, pending: PendingConfirmation) -> bool:  # noqa: D102
        return True


class VigilantConfirmer:
    """Rejects any call the gateway flagged (non-empty warning) -- a careful human
    who heeds every warning (upper bound on gate security)."""

    name = "vigilant"

    def confirm(self, pending: PendingConfirmation) -> bool:  # noqa: D102
        return not pending.human_warning()


class CautiousConfirmer:
    """A realistic human: REJECTS anything they cannot confidently judge.

    Approves only when the confirmation carries a BASIS to verify (a breakdown to
    check) and nothing is flagged unverifiable. A bare untrusted value, or a
    decision with no rationale, is 'too hard to judge' -> reject (fail-safe). So
    the confirmation UX QUALITY drives availability under this human: give it a
    decomposition and it can say yes; give it a bare value and it says no.

    This models the AVAILABILITY side of a careful human (what they are willing to
    approve given the UX). It does not, by itself, catch a poisoned value inside a
    judgeable breakdown -- that needs the real human to read it (InteractiveConfirmer).
    """

    name = "cautious"

    @staticmethod
    def judgeable(pending: PendingConfirmation) -> bool:
        if not pending.human_warning():
            return True                       # nothing flagged -> trivially fine
        if pending.unverifiable:
            return False                      # can't re-derive -> can't judge
        # a decomposition table OR a surfaced provenance (source to research)
        # gives the human a basis to judge; a bare value with neither does not.
        return pending.breakdown is not None or pending.provenance is not None

    def confirm(self, pending: PendingConfirmation) -> bool:
        return self.judgeable(pending)


@dataclasses.dataclass
class ScriptedConfirmer:
    """Deterministic answers for tests; defaults to reject once exhausted."""

    answers: list[bool]
    name: str = "scripted"
    _i: int = 0

    def confirm(self, pending: PendingConfirmation) -> bool:
        a = self.answers[self._i] if self._i < len(self.answers) else False
        self._i += 1
        return a


class InteractiveConfirmer:
    """Asks a real human on stdin -- used when a person runs the benchmark."""

    name = "interactive"

    def confirm(self, pending: PendingConfirmation) -> bool:
        print("\n----- CONFIRM REQUIRED -----")
        print(f"  call : {pending.tool}")
        print(f"  {pending.param_name} = {pending.value!r}")
        warning = pending.human_warning()
        if warning:
            print(f"  {warning}")
        try:
            ans = input("  approve this call? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        return ans in ("y", "yes")
