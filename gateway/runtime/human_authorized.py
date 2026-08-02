"""Human-authorization execution path: recover actions the ENFORCER must deny by
letting a human hold FN=0 for them -- with single-use, fully-bound grants.

The static Planner plans from the trusted prompt only, so a data-asymmetry action
(value lives in untrusted data the plan never read) is simply never emitted; the
enforcer has no rule for it. To recover such an action WITHOUT re-opening the
injection surface, a human -- not the enforcer -- authorizes it:

  1. A PROPOSER (pluggable: an oracle in eval, an LLM extractor in production --
     its accuracy is a SEPARATE problem) reads untrusted data and proposes the
     missing action with a concrete, UNTRUSTED-labelled candidate value.
  2. The action is shown to a CONFIRMER (the human). FN=0 for this call now rests
     on the human's judgement, so a rubber-stamp human loses it and an informed
     human keeps it (condition 1 -- meaningful confirmation).
  3. On approval a single-use HumanGrant is MINTED, bound to the tool and ALL its
     control operands and signed. The action is executed only by REDEEMING that
     grant, which consumes it (condition 2 -- so a replayed or operand-spliced
     injection cannot reuse the blessing).

This does not touch the enforcer's own guarantee: everything the enforcer already
authorizes is unchanged. It only ADDS a human-held path for calls the enforcer
cannot verify, and the grant ledger bounds exactly what a single approval permits.
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import secrets
from typing import Any, Callable, Protocol

from pauth.enforcer import CallEvent, Decision, Enforcer, execute_generated_code
from pauth.envelope import KeyRing
from pauth.evaluator import wrap

from gateway.runtime.confirmation import PendingConfirmation, control_operands, is_side_effecting
from gateway.planning.prechecks import PrecheckPolicy

_GRANT_SIGNER = "human-confirmation-authority"


def _canon(values) -> tuple:
    """Canonical form of the control operands a grant binds -- string-normalised so
    the same value from different reads compares equal, mirroring enforcer matching."""
    return tuple(str(v) for v in values)


@dataclasses.dataclass(frozen=True)
class HumanGrant:
    """A single-use, signed authorization a human minted for ONE call, bound to the
    tool and ALL its control operands. Consumed on redemption."""

    tool: str
    control_values: tuple
    nonce: str
    signature: str

    @staticmethod
    def _digest(tool: str, control_values: tuple, nonce: str) -> bytes:
        return f"{tool}|{list(control_values)!r}|{nonce}".encode()


@dataclasses.dataclass
class ProposedAction:
    """A missing action a proposer surfaces for human authorization. ``args`` are the
    full positional operands; ``sources`` names the untrusted origin (for the human)."""

    tool: str
    args: list[Any]
    sources: tuple[str, ...] = ()
    unverifiable: bool = True   # a proposed value is untrusted-derived by construction


class Proposer(Protocol):
    def propose(self) -> list[ProposedAction]: ...


class StaticProposer:
    """A proposer returning a fixed list -- the eval stand-in for an extractor (whose
    accuracy is a separate problem). In a benchmark the actions carry the ground-truth
    control values, so this measures the CEILING assuming perfect extraction."""

    def __init__(self, actions: list[ProposedAction]) -> None:
        self._actions = actions

    def propose(self) -> list[ProposedAction]:
        return list(self._actions)


class GrantLedger:
    """Mints and redeems single-use human grants. Redemption is the ONLY way a
    human-authorized call executes; it verifies the signature, matches the tool and
    EVERY control operand, and consumes the grant so it cannot be reused."""

    def __init__(self, keyring: KeyRing | None = None) -> None:
        self._keyring = keyring or KeyRing()
        self._live: list[HumanGrant] = []

    def mint(self, tool: str, control_values: tuple) -> HumanGrant:
        nonce = secrets.token_hex(8)
        sig = hmac.new(self._keyring.key(_GRANT_SIGNER),
                       HumanGrant._digest(tool, control_values, nonce),
                       hashlib.sha256).hexdigest()
        grant = HumanGrant(tool, control_values, nonce, sig)
        self._live.append(grant)
        return grant

    def _valid(self, g: HumanGrant) -> bool:
        expected = hmac.new(self._keyring.key(_GRANT_SIGNER),
                            HumanGrant._digest(g.tool, g.control_values, g.nonce),
                            hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, g.signature)

    def redeem(self, tool: str, args: list[Any], control_indices: list[int]) -> bool:
        """Consume an unconsumed grant that matches ``tool`` and every control operand.
        Returns True (authorized, grant consumed) or False (no grant -- default deny)."""
        want = _canon(args[i] for i in control_indices if i < len(args))
        for g in self._live:
            if g.tool == tool and g.control_values == want and self._valid(g):
                self._live.remove(g)          # single use: gone after one redemption
                return True
        return False


@dataclasses.dataclass
class HumanAuthReport:
    events: list[CallEvent]                 # enforcer-authorized plan calls
    human_authorized: list[CallEvent]       # calls a human authorized via a grant
    rejected: list[ProposedAction]          # proposed actions the human declined
    denied_reuse: list[ProposedAction]      # approved but no unconsumed grant (replay/splice)
    tool_errors: list[str]
    crashed: str | None


def _control_indices(tool: str, docs: dict[str, Any]) -> list[int]:
    return [i for i, _ in control_operands(tool, docs, PrecheckPolicy())]


def _pending_for(action: ProposedAction, ci: list[int],
                 tool_params: dict[str, list[str]]) -> PendingConfirmation:
    idx = ci[0] if ci else 0
    params = tool_params.get(action.tool, [])
    return PendingConfirmation(
        confirmation_id=secrets.token_hex(4), tool=action.tool, param_index=idx,
        param_name=params[idx] if idx < len(params) else "?",
        value=action.args[idx] if idx < len(action.args) else None,
        source=action.sources, unverifiable=action.unverifiable)


def authorize_proposals(
    proposals: list[ProposedAction], *, confirmer: Any, ledger: GrantLedger,
    docs: dict[str, Any], tool_params: dict[str, list[str]],
) -> tuple[list[ProposedAction], list[ProposedAction]]:
    """CONFIRMATION-AUTHORITY phase: show each proposal to the human; for every
    APPROVED one, mint exactly one single-use grant bound to its control operands.
    Returns (approved, rejected). The grants (in ``ledger``) are the only tokens
    that let the executor act -- an action never approved here has no grant."""
    approved, rejected = [], []
    for action in proposals:
        ci = _control_indices(action.tool, docs)
        if confirmer.confirm(_pending_for(action, ci, tool_params)):
            ledger.mint(action.tool, _canon(action.args[i] for i in ci if i < len(action.args)))
            approved.append(action)
        else:
            rejected.append(action)
    return approved, rejected


def redeem_and_execute(
    actions: list[ProposedAction], *, ledger: GrantLedger, docs: dict[str, Any],
    tool_params: dict[str, list[str]], tool_executor: Callable[[str, dict[str, Any]], Any],
) -> tuple[list[CallEvent], list[ProposedAction], list[str]]:
    """EXECUTOR phase (may be a separate service across a trust boundary): execute
    each action ONLY by redeeming a matching single-use grant. An action with no
    unconsumed grant -- a replay of an approved call, or an injected operand-splice --
    is denied. Returns (executed_events, denied, tool_errors)."""
    executed: list[CallEvent] = []
    denied: list[ProposedAction] = []
    tool_errors: list[str] = []
    for action in actions:
        ci = _control_indices(action.tool, docs)
        if not ledger.redeem(action.tool, action.args, ci):
            denied.append(action)             # no grant: never approved, or already spent
            continue
        params = tool_params.get(action.tool, [])
        try:
            raw = tool_executor(action.tool, dict(zip(params, action.args)))
        except Exception as exc:  # noqa: BLE001 -- tool-level failure
            tool_errors.append(f"{action.tool}: {type(exc).__name__}: {exc}")
            continue
        _ = wrap(raw)
        executed.append(CallEvent(
            action.tool, list(action.args),
            Decision(True, None, "authorized by human grant (FN=0 held by the human)")))
    return executed, denied, tool_errors


def execute_with_human_authorization(
    plan_code: str,
    enforcer: Enforcer,
    tool_params: dict[str, list[str]],
    tool_executor: Callable[[str, dict[str, Any]], Any],
    *,
    proposer: Proposer,
    confirmer: Any,
    docs: dict[str, Any],
    ledger: GrantLedger | None = None,
) -> HumanAuthReport:
    """Full path: run ``plan_code`` under the enforcer, then authorize proposed
    missing actions (mint grants) and execute them (redeem grants). The mint/redeem
    split makes the single-use, fully-bound grant the exact unit of what one human
    approval permits -- a replayed or spliced injection finds no grant."""
    ledger = ledger or GrantLedger()
    plan_rep = execute_generated_code(plan_code, enforcer, tool_params, tool_executor)

    proposals = proposer.propose()
    approved, rejected = authorize_proposals(
        proposals, confirmer=confirmer, ledger=ledger, docs=docs, tool_params=tool_params)
    executed, denied_reuse, tool_errors = redeem_and_execute(
        approved, ledger=ledger, docs=docs, tool_params=tool_params, tool_executor=tool_executor)

    return HumanAuthReport(
        events=list(plan_rep.events), human_authorized=executed, rejected=rejected,
        denied_reuse=denied_reuse, tool_errors=list(plan_rep.tool_errors) + tool_errors,
        crashed=plan_rep.crashed)


@dataclasses.dataclass
class StreamReport:
    executed: list[CallEvent]            # ran: enforcer-permitted reads/calls + human-authorized calls
    human_authorized: list[CallEvent]    # off-plan side-effects a human approved (FN=0 held by human)
    rejected: list[ProposedAction]       # off-plan side-effects a human declined
    reads: list[tuple]                   # untrusted reads observed, in order (provenance)


def gate_agent_stream(
    agent_calls: list[tuple],
    enforcer: Enforcer,
    tool_params: dict[str, list[str]],
    tool_executor: Callable[[str, dict[str, Any]], Any],
    *,
    confirmer: Any,
    docs: dict[str, Any],
    ledger: GrantLedger | None = None,
) -> StreamReport:
    """Gate a LIVE agent's tool-call stream -- the runtime form of "read, THEN act".

    The confirmation gate is not only for operands: it confirms whole ACTIONS. An
    agent reads untrusted content (gmail, a file) and only THEN determines what to
    do; that determined action cannot be in the static plan, so the enforcer denies
    it. Instead of hard-blocking, the action is routed to WHOLE-ACTION human
    confirmation carrying the untrusted reads as provenance; on approval it runs
    under a single-use, fully-bound grant (FN=0 held by the human).

    Per call:
      * a READ runs and is recorded as untrusted provenance (reads are side-effect-free);
      * a side-effecting call the enforcer AUTHORIZES (in-plan) runs;
      * a side-effecting call the enforcer DENIES (its action was determined by
        untrusted content) -> whole-action human confirmation -> single-use grant.
    """
    ledger = ledger or GrantLedger()
    executed: list[CallEvent] = []
    human_authorized: list[CallEvent] = []
    rejected: list[ProposedAction] = []
    reads: list[tuple] = []

    for tool, args in agent_calls:
        args = list(args)
        decision = enforcer.check(tool, args)
        if not is_side_effecting(tool):
            params = tool_params.get(tool, [])
            try:
                raw = tool_executor(tool, dict(zip(params, args)))
            except Exception:  # noqa: BLE001 -- a read failure is not a gate decision
                raw = None
            reads.append((tool, args, raw))
            executed.append(CallEvent(tool, args, decision))
            continue

        if decision.permit:                      # in-plan side-effect: enforcer authorized
            params = tool_params.get(tool, [])
            try:
                raw = tool_executor(tool, dict(zip(params, args)))
            except Exception:  # noqa: BLE001
                raw = None
            executed.append(CallEvent(tool, args, decision))
            continue

        # off-plan side-effect: its action was determined by untrusted reads ->
        # confirm the WHOLE action, provenance = the untrusted sources read so far
        action = ProposedAction(tool, args, sources=tuple(r[0] for r in reads))
        approved, rej = authorize_proposals(
            [action], confirmer=confirmer, ledger=ledger, docs=docs, tool_params=tool_params)
        rejected.extend(rej)
        ev, _denied, _errs = redeem_and_execute(
            approved, ledger=ledger, docs=docs, tool_params=tool_params, tool_executor=tool_executor)
        executed.extend(ev)
        human_authorized.extend(ev)

    return StreamReport(executed=executed, human_authorized=human_authorized,
                        rejected=rejected, reads=reads)
