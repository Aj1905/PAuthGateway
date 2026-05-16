"""Runtime enforcement (paper sec. 3.4 and sec. 4.1.3, steps B1-B4).

The agent's tool calls are proxied through the enforcer.  For each call the
enforcer searches for the rules applicable to the tool, then checks that the
guard predicates hold and that every operand equals the value implied by the
slice.  PAuth is *default-deny*: "a call is by default denied unless an
exact-matching rule is found" (paper sec. 5.2).

This module also provides the sandboxed runner that executes the generated
``run`` function, routing every tool call through the enforcer and turning
each permitted result into a signed envelope (B4).
"""

from __future__ import annotations

import ast
import dataclasses
from typing import Any, Callable

from .envelope import EnvelopeStore, TamperedEnvelopeError, make_envelope
from .evaluator import EXEC_HELPERS, Evaluator, NotConcretizable, values_match, wrap
from .rules import Rule
from .symbolic import canon


@dataclasses.dataclass
class Decision:
    """The outcome of checking one concrete call."""

    permit: bool
    rule: Rule | None
    reason: str


class Enforcer:
    """Checks concrete calls against compiled rules and signs results."""

    def __init__(
        self,
        rules: list[Rule],
        store: EnvelopeStore,
        tool_signer: dict[str, str],
    ) -> None:
        self.store = store
        self.tool_signer = tool_signer
        self.rules_by_tool: dict[str, list[Rule]] = {}
        for rule in rules:
            self.rules_by_tool.setdefault(rule.tool, []).append(rule)

    def check(self, tool: str, args: list[Any]) -> Decision:
        """B1-B3: decide whether a concrete call is authorized.

        The call is permitted iff some rule for ``tool`` has all guards
        satisfied and every operand expression evaluating to the supplied
        argument.  Otherwise it is denied (default-deny).
        """
        rules = self.rules_by_tool.get(tool, [])
        if not rules:
            return Decision(False, None, f"no rule exists for tool '{tool}' (default-deny)")

        reasons: list[str] = []
        for rule in rules:
            if rule.n_args != len(args):
                reasons.append(f"{rule.key}: arity {rule.n_args} != {len(args)}")
                continue
            ev = Evaluator(self.store, rule.lets)
            try:
                guard_ok = all(bool(ev.eval(pred)) for pred in rule.guard)
            except (NotConcretizable, TamperedEnvelopeError) as exc:
                reasons.append(f"{rule.key}: guard unresolved ({exc})")
                continue
            except Exception as exc:  # noqa: BLE001 -- a rule must never crash a run
                reasons.append(f"{rule.key}: guard evaluation error ({type(exc).__name__}: {exc})")
                continue
            if not guard_ok:
                reasons.append(f"{rule.key}: guard predicate is false")
                continue
            try:
                expected = [ev.eval(expr) for expr in rule.arg_exprs]
            except (NotConcretizable, TamperedEnvelopeError) as exc:
                reasons.append(f"{rule.key}: operand unresolved ({exc})")
                continue
            except Exception as exc:  # noqa: BLE001 -- a rule must never crash a run
                reasons.append(f"{rule.key}: operand evaluation error ({type(exc).__name__}: {exc})")
                continue
            mismatches = [
                i for i, (e, a) in enumerate(zip(expected, args)) if not values_match(e, a)
            ]
            if mismatches:
                reasons.append(f"{rule.key}: operand(s) {mismatches} off-slice")
                continue
            return Decision(True, rule, f"authorized by rule {rule.key}")

        return Decision(
            False, None, "no rule authorizes this call (default-deny) :: " + " ; ".join(reasons)
        )

    def record(self, rule: Rule, result: Any) -> None:
        """B4: wrap a permitted call's result into a signed envelope."""
        signer = self.tool_signer.get(rule.tool, rule.tool)
        symbolic = canon(rule.call_node)
        env = make_envelope(result, symbolic, signer, self._keyring())
        self.store.put(env)

    def _keyring(self):
        return self.store._keyring  # the store owns the shared keyring


# --------------------------------------------------------------------------
# Sandboxed runner for the generated code
# --------------------------------------------------------------------------

class _Denied(Exception):
    """Internal: unwinds execution on the first denied call."""

    def __init__(self, event: "CallEvent") -> None:
        self.event = event


@dataclasses.dataclass
class CallEvent:
    tool: str
    args: list[Any]
    decision: Decision


@dataclasses.dataclass
class ExecReport:
    """Outcome of executing a generated task."""

    events: list[CallEvent]
    denied: list[CallEvent]
    tool_errors: list[str]
    crashed: str | None

    @property
    def has_denial(self) -> bool:
        return bool(self.denied)


def execute_generated_code(
    code: str,
    enforcer: Enforcer,
    tool_params: dict[str, list[str]],
    tool_runner: Callable[[str, dict[str, Any]], Any],
    stop_on_denial: bool = True,
) -> ExecReport:
    """Execute ``run`` with every tool call proxied through the enforcer.

    Tool calls are intercepted (B1), checked (B2-B3), executed when permitted,
    and their results recorded as envelopes (B4).  A denial is recorded; with
    ``stop_on_denial`` the run halts on the first denial, mirroring the paper's
    "execution stops with a denial".
    """
    events: list[CallEvent] = []
    tool_errors: list[str] = []
    crashed: str | None = None
    denied: list[CallEvent] = []

    def make_wrapper(name: str) -> Callable[..., Any]:
        def wrapper(*args: Any) -> Any:
            decision = enforcer.check(name, list(args))
            event = CallEvent(name, list(args), decision)
            events.append(event)
            if not decision.permit:
                denied.append(event)
                if stop_on_denial:
                    raise _Denied(event)
                return None
            params = tool_params.get(name, [])
            kwargs = dict(zip(params, args))
            try:
                result = tool_runner(name, kwargs)
            except Exception as exc:  # noqa: BLE001 -- tool-level failure
                tool_errors.append(f"{name}: {type(exc).__name__}: {exc}")
                return None
            result = wrap(result)
            assert decision.rule is not None
            enforcer.record(decision.rule, result)
            return result

        return wrapper

    namespace: dict[str, Any] = {name: make_wrapper(name) for name in tool_params}
    namespace.update(EXEC_HELPERS)
    namespace["__builtins__"] = {}

    try:
        exec(compile(code, "<pauth-run>", "exec"), namespace)  # noqa: S102
        run = namespace.get("run")
        if not callable(run):
            crashed = "generated code defines no callable 'run'"
        else:
            run()
    except _Denied:
        pass  # already recorded in `denied`
    except Exception as exc:  # noqa: BLE001 -- generated-code bug, not a denial
        crashed = f"{type(exc).__name__}: {exc}"

    return ExecReport(events=events, denied=denied, tool_errors=tool_errors, crashed=crashed)


def check_injection(enforcer: Enforcer, tool: str, args: list[Any]) -> Decision:
    """Check a single forced-injection call against the task's rules.

    A faithful injection test: the spurious call is offered to the enforcer
    with the benign task's envelope store already populated.  If *any* rule
    would authorize it the result is a false negative.
    """
    return enforcer.check(tool, args)
