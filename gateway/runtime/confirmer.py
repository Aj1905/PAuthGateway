"""Human-in-the-loop confirmers: WHO answers a gated call, and how.

The confirmation gate holds a side-effecting call whose control operand is
untrusted-derived. The gate's security IS the answer it gets: a vigilant human
rejects a poisoned value; a rubber-stamping one approves it. So a benchmark that
skips the gate measures nothing about this layer. These confirmers make the
decision explicit and pluggable:

  * InteractiveConfirmer -- a real human on stdin (when a person runs the bench).
  * OracleConfirmer      -- a perfectly-informed human: always decides correctly
                            (approve clean, reject poison), never stalls. The
                            CEILING of the human-confirmation path; uses ground
                            truth the real gateway cannot have, so it is a headless
                            upper bound, not an autonomous capability.
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
class OracleConfirmer:
    """A PERFECTLY-INFORMED human (benchmark oracle): approves the presented value
    iff it equals the ground-truth-correct one, rejects otherwise. It ALWAYS
    decides -- it never leaves a call pending -- so a headless run never stalls at
    the gate: the benign value is approved (available) and any poisoned value is
    rejected (FN=0), by construction.

    It reaches this by using knowledge the production gateway CANNOT have -- the
    clean value, supplied by the benchmark harness via ``expected``. So this is the
    theoretical CEILING of the human-confirmation path (what completes if the human
    always decides correctly), NOT an autonomous capability and NOT a realistic
    human. Report it as the UPPER bound next to trusting (lower bound); never
    present its number as the gateway's own.
    """

    expected: object = None
    tol: float = 1e-6
    name: str = "oracle"

    def confirm(self, pending: PendingConfirmation) -> bool:  # noqa: D102
        exp = self.expected
        val = pending.value
        if exp is None:
            return False  # no ground truth supplied -> fail-safe reject, still no stall
        if isinstance(exp, float) and isinstance(val, (int, float)) and not isinstance(val, bool):
            return abs(val - exp) < self.tol
        return val == exp


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
    """Asks a real human on stdin -- used when a person runs the benchmark.

    ``ground_truth`` (set by a benchmark harness) is appended as the last field
    of the structured display; production leaves it empty."""

    name = "interactive"
    ground_truth: str = ""

    def confirm(self, pending: PendingConfirmation) -> bool:
        print("\n----- 確認ゲート -----")
        print(pending.structured_display(ground_truth=self.ground_truth))
        try:
            ans = input("承認しますか? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        return ans in ("y", "yes")
