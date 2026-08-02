"""Runtime enforcement (paper sec. 3.4 and sec. 4.1.3, steps runtime enforcement).

The agent's tool calls are proxied through the enforcer.  For each call the
enforcer searches for the rules applicable to the tool, then checks that the
guard predicates hold and that every operand equals the value implied by the
slice.  PAuth is *default-deny*: "a call is by default denied unless an
exact-matching rule is found" (paper sec. 5.2).

This module also provides the sandboxed plan executor that executes the generated
``run`` function, routing every tool call through the enforcer and turning
each permitted result into a signed envelope (envelope signing).
"""

from __future__ import annotations

import ast
import dataclasses
import threading
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
    # Consumption token ``(site_key, loop_index_path)`` identifying which
    # authorization this decision spends.  Passed back to ``record`` so the
    # same site (or loop tuple) cannot authorize a second, replayed call.
    token: tuple | None = None


class ExecutionStateError(ValueError):
    """A persisted execution-attempt snapshot is malformed or incompatible."""


_EXECUTION_STATE_VERSION = 1
_ATTEMPT_STATES = frozenset({"started", "succeeded", "indeterminate"})


class Enforcer:
    """Checks concrete calls against compiled rules and signs results.

    Beyond the paper's per-call checks, the enforcer keeps two pieces of
    session state (an extension over the paper's stateless B-series):

    * consumption -- each call site authorizes at most as many calls as the
      plan contains (one for a straight-line site, one per loop tuple), so a
      permitted call cannot be replayed;
    * ordering (opt-in via ``ordered_tools``) -- a call site whose tool is in
      ``ordered_tools`` requires every earlier such site to have executed or
      to be skippable (all its rules' guards concretely false).
    """

    def __init__(
        self,
        rules: list[Rule],
        store: EnvelopeStore,
        tool_signer: dict[str, str],
        ordered_tools: set[str] | None = None,
    ) -> None:
        self.store = store
        self.tool_signer = tool_signer
        self.ordered_tools = ordered_tools or set()
        self.rules_by_tool: dict[str, list[Rule]] = {}
        self._site_rules: dict[str, list[Rule]] = {}
        for rule in rules:
            self.rules_by_tool.setdefault(rule.tool, []).append(rule)
            self._site_rules.setdefault(rule.key, []).append(rule)
        self._used: dict[tuple, int] = {}      # (site_key, idx_path) -> count
        self._site_used: dict[str, int] = {}   # site_key -> count
        # Gateway-owned ExecutionAttempt state projected into the Enforcer so
        # live matching can skip started/indeterminate calls as well as calls
        # whose successful envelope has consumed the token.
        self._attempts: dict[tuple, str] = {}
        self._attempt_lock = threading.RLock()
        self._plan_source_sha256: str | None = None
        self._execution_state_sink: Callable[[dict[str, Any]], None] | None = None

    # -- session-state helpers (consumption + ordering) --------------------

    def _consumed(self, token: tuple) -> bool:
        return self._used.get(token, 0) > 0

    def _unavailable(self, token: tuple) -> bool:
        return self._consumed(token) or token in self._attempts

    def _unavailable_reason(self, token: tuple) -> str:
        state = self._attempts.get(token)
        if state == "started":
            return "execution attempt already started (replay blocked)"
        if state == "indeterminate":
            return "indeterminate tool outcome (replay blocked)"
        return "call site already executed (replay)"

    def consume(self, token: tuple | None) -> None:
        if token is None:
            return
        if self._consumed(token):
            return
        self._used[token] = self._used.get(token, 0) + 1
        key = token[0]
        self._site_used[key] = self._site_used.get(key, 0) + 1

    def configure_execution_state(
        self,
        plan_source_sha256: str,
        restored: dict[str, Any] | None = None,
        sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Bind durable ExecutionAttempt state to one compiled plan.

        ``started`` snapshots are crash remnants: restoration normalizes them
        to ``indeterminate`` before any live call can be checked. Both states
        block redispatch, while only ``succeeded`` counts as consumed for call
        ordering.
        """
        if self._plan_source_sha256 is not None:
            raise ExecutionStateError("execution state is already configured")
        if (
            not isinstance(plan_source_sha256, str)
            or len(plan_source_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in plan_source_sha256)
        ):
            raise ExecutionStateError("invalid plan source_sha256")

        attempts: dict[tuple, str] = {}
        normalized_started = False
        if restored is not None:
            if not isinstance(restored, dict):
                raise ExecutionStateError("execution state must be a JSON object")
            if set(restored) != {"schema_version", "plan_source_sha256", "calls"}:
                raise ExecutionStateError("execution state has an invalid schema")
            version = restored.get("schema_version")
            if isinstance(version, bool) or version != _EXECUTION_STATE_VERSION:
                raise ExecutionStateError("unsupported execution state schema_version")
            if restored.get("plan_source_sha256") != plan_source_sha256:
                raise ExecutionStateError("execution state plan fingerprint mismatch")
            calls = restored.get("calls")
            if not isinstance(calls, list):
                raise ExecutionStateError("execution state calls must be a list")
            for row in calls:
                if not isinstance(row, dict) or set(row) != {
                    "site_key", "loop_path", "state"
                }:
                    raise ExecutionStateError("execution state call has an invalid schema")
                site_key = row.get("site_key")
                loop_path = row.get("loop_path")
                state = row.get("state")
                if not isinstance(site_key, str) or site_key not in self._site_rules:
                    raise ExecutionStateError("execution state references an unknown call site")
                if not isinstance(loop_path, list) or any(
                    isinstance(index, bool) or not isinstance(index, int) or index < 0
                    for index in loop_path
                ):
                    raise ExecutionStateError("execution state has an invalid loop path")
                if not any(
                    len(rule.loops) == len(loop_path)
                    for rule in self._site_rules[site_key]
                ):
                    raise ExecutionStateError("execution state loop path has the wrong depth")
                if state not in _ATTEMPT_STATES:
                    raise ExecutionStateError("execution state has an invalid attempt state")
                token = (site_key, tuple(loop_path))
                if token in attempts:
                    raise ExecutionStateError("execution state contains a duplicate call token")
                if state == "started":
                    state = "indeterminate"
                    normalized_started = True
                attempts[token] = state

        self._plan_source_sha256 = plan_source_sha256
        self._attempts = attempts
        for token, state in attempts.items():
            if state == "succeeded":
                self.consume(token)
        self._execution_state_sink = sink
        if normalized_started:
            self._persist_execution_state()

    def execution_state(self) -> dict[str, Any]:
        """Return the strict JSON snapshot persisted by the Gateway."""
        with self._attempt_lock:
            if self._plan_source_sha256 is None:
                raise ExecutionStateError("execution state is not configured")
            rows = [
                {
                    "site_key": token[0],
                    "loop_path": list(token[1]),
                    "state": state,
                }
                for token, state in sorted(
                    self._attempts.items(), key=lambda item: (item[0][0], item[0][1])
                )
            ]
            return {
                "schema_version": _EXECUTION_STATE_VERSION,
                "plan_source_sha256": self._plan_source_sha256,
                "calls": rows,
            }

    def _persist_execution_state(self) -> None:
        if self._execution_state_sink is not None:
            self._execution_state_sink(self.execution_state())

    def begin(self, token: tuple | None) -> bool:
        """Durably reserve one authorized call before tool_executor dispatch.

        A persistence exception is intentionally propagated and the in-memory
        ``started`` tombstone is retained. The caller must not run the tool.
        """
        if token is None:
            raise ExecutionStateError("authorized call has no execution token")
        with self._attempt_lock:
            if self._unavailable(token):
                return False
            self._attempts[token] = "started"
            self._persist_execution_state()
            return True

    def mark_indeterminate(self, token: tuple | None) -> None:
        """Keep a dispatched call unavailable when its outcome is unknown."""
        if token is None:
            return
        with self._attempt_lock:
            if self._attempts.get(token) == "succeeded":
                return
            self._attempts[token] = "indeterminate"
            self._persist_execution_state()

    def _site_skippable(self, key: str) -> bool:
        """A site is skippable iff every one of its rules is provably off-path:
        some guard predicate evaluates concretely to False."""
        rules = self._site_rules.get(key, [])
        if not rules:
            return False
        for rule in rules:
            if not rule.guard:
                return False
            ev = Evaluator(self.store, rule.lets)
            off_path = False
            for pred in rule.guard:
                try:
                    if not bool(ev.eval(pred)):
                        off_path = True
                        break
                except Exception:  # noqa: BLE001 -- unresolved guard: not provably off-path
                    continue
            if not off_path:
                return False
        return True

    def _order_ok(self, rule: Rule) -> tuple[bool, str]:
        """Every earlier ordered call site must have executed or be skippable."""
        if rule.tool not in self.ordered_tools:
            return True, ""
        for key, site in self._site_rules.items():
            first = site[0]
            if first.seq >= rule.seq or first.tool not in self.ordered_tools:
                continue
            if self._site_used.get(key, 0) > 0:
                continue
            if self._site_skippable(key):
                continue
            return False, (
                f"out of order: earlier call site {key} (seq {first.seq}) "
                f"has not executed and is not provably off-path"
            )
        return True, ""

    def check(self, tool: str, args: list[Any], *, live: bool = False) -> Decision:
        """the authorization check: decide whether a concrete call is authorized.

        The call is permitted iff some rule for ``tool`` has all guards
        satisfied and every operand expression evaluating to the supplied
        argument.  Otherwise it is denied (default-deny).

        ``live=False`` (the default) probes the pure authorization relation --
        post-hoc audits and injection probes see what the rules *could*
        authorize, blind to session state.  ``live=True`` is for a call that is
        about to execute: it additionally refuses consumed sites/tuples
        (replay) and, for ``ordered_tools``, out-of-order side effects.
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

            if live:
                order_ok, order_reason = self._order_ok(rule)
                if not order_ok:
                    reasons.append(f"{rule.key}: {order_reason}")
                    continue

            # Bounded for(s): quantified rule. The operand is authorized iff it
            # matches arg_exprs for SOME tuple of the NESTED enumeration over the
            # signed collections (each inner iter evaluated with the outer vars
            # bound). The collections are signed envelopes, so the enumeration is the
            # exact authorized set -- a tuple no loop can produce is off-slice, and a
            # value using one is denied (FN=0). Handles independent products
            # (for x in A: for y in B) and dependent nesting (for o in os: for i in o.items).
            # An already-consumed tuple no longer authorizes (replay protection);
            # duplicates in the collection are distinct tuples and count separately.
            if rule.loops:
                def _match(level: int, binds: dict, path: tuple) -> tuple | None:
                    if level == len(rule.loops):
                        if live and self._unavailable((rule.key, path)):
                            return None
                        ev_e = Evaluator(self.store, rule.lets, binds)
                        try:
                            expected = [ev_e.eval(expr) for expr in rule.arg_exprs]
                        except Exception:  # noqa: BLE001 -- this tuple just doesn't match
                            return None
                        if all(values_match(e, a) for e, a in zip(expected, args)):
                            return path
                        return None
                    var, it = rule.loops[level]
                    ev_l = Evaluator(self.store, rule.lets, binds)
                    try:
                        collection = ev_l.eval(it)
                    except (NotConcretizable, TamperedEnvelopeError):
                        return None
                    if not isinstance(collection, (list, tuple)):
                        return None
                    for i, element in enumerate(collection):
                        hit = _match(level + 1, {**binds, var: element}, path + (i,))
                        if hit is not None:
                            return hit
                    return None

                hit = _match(0, {}, ())
                if hit is None:
                    reasons.append(
                        f"{rule.key}: no unconsumed loop tuple of the observed collections matches"
                    )
                    continue
                return Decision(
                    True, rule, f"authorized by loop rule {rule.key}", token=(rule.key, hit)
                )

            token = (rule.key, ())
            if live and self._unavailable(token):
                reasons.append(f"{rule.key}: {self._unavailable_reason(token)}")
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
            return Decision(
                True, rule, f"authorized by rule {rule.key}", token=(rule.key, ())
            )

        return Decision(
            False, None, "no rule authorizes this call (default-deny) :: " + " ; ".join(reasons)
        )

    def record(self, rule: Rule, result: Any, token: tuple | None = None) -> None:
        """envelope signing: wrap a permitted call's result into a signed envelope.

        ``token`` marks the authorizing site (or loop tuple) as consumed; it is
        the ``Decision.token`` of the check that permitted this call.  Consumption
        happens here -- at actual execution -- so a call that is checked but never
        executed (e.g. held for confirmation) does not spend its authorization.
        """
        signer = self.tool_signer.get(rule.tool, rule.tool)
        symbolic = canon(rule.call_node)
        env = make_envelope(result, symbolic, signer, self._keyring())
        self.store.put(env)
        with self._attempt_lock:
            self.consume(token)
            if token is not None:
                self._attempts[token] = "succeeded"
                self._persist_execution_state()

    def _keyring(self):
        return self.store._keyring  # the store owns the shared keyring


# --------------------------------------------------------------------------
# Sandboxed plan executor for the generated code
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
    tool_executor: Callable[[str, dict[str, Any]], Any],
    stop_on_denial: bool = True,
) -> ExecReport:
    """Execute ``run`` with every tool call proxied through the enforcer.

    Tool calls are intercepted (call interception), checked (the authorization check), executed when permitted,
    and their results recorded as envelopes (envelope signing).  A denial is recorded; with
    ``stop_on_denial`` the run halts on the first denial, mirroring the paper's
    "execution stops with a denial".
    """
    events: list[CallEvent] = []
    tool_errors: list[str] = []
    crashed: str | None = None
    denied: list[CallEvent] = []

    def make_wrapper(name: str) -> Callable[..., Any]:
        def wrapper(*args: Any) -> Any:
            decision = enforcer.check(name, list(args), live=True)
            event = CallEvent(name, list(args), decision)
            events.append(event)
            if not decision.permit:
                denied.append(event)
                if stop_on_denial:
                    raise _Denied(event)
                return None
            params = tool_params.get(name, [])
            if not enforcer.begin(decision.token):
                replay = Decision(
                    False,
                    decision.rule,
                    "execution attempt already exists (replay blocked)",
                    decision.token,
                )
                replay_event = CallEvent(name, list(args), replay)
                denied.append(replay_event)
                if stop_on_denial:
                    raise _Denied(replay_event)
                return None
            kwargs = dict(zip(params, args))
            try:
                result = tool_executor(name, kwargs)
            except Exception as exc:  # noqa: BLE001 -- tool-level failure
                try:
                    enforcer.mark_indeterminate(decision.token)
                except Exception:
                    # The pre-dispatch ``started`` snapshot remains fail-closed
                    # even if persisting the more precise state fails.
                    pass
                tool_errors.append(f"{name}: {type(exc).__name__}: {exc}")
                return None
            result = wrap(result)
            assert decision.rule is not None
            enforcer.record(decision.rule, result, decision.token)
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
