"""Runtime enforcement (paper sec. 3.4 and sec. 4.1.3, steps runtime enforcement).

The agent's tool calls are proxied through the enforcer.  For each call the
enforcer searches for the rules applicable to the tool, then checks that the
guard predicates hold and that every operand equals the value implied by the
slice.  PAuth is *default-deny*: "a call is by default denied unless an
exact-matching rule is found" (paper sec. 5.2).

The legacy sandboxed executor that runs generated ``run`` code lives in
:mod:`pauth.tool_executor`.  It is distinct from the Gateway-owned tool-call
execution component defined in ``docs/SYSTEM_MODEL.md``.
"""

from __future__ import annotations

import ast
import dataclasses
import threading
from typing import Any, Callable

from .envelope import (
    EnvelopeStore,
    TamperedEnvelopeError,
    make_envelope,
    occurrence_symbolic,
)
from .evaluator import Evaluator, NotConcretizable, values_match
from .rule_compiler import Rule
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
_EXTERNAL_SITE_PREFIX = "reauthorization:"


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
                external = (
                    isinstance(site_key, str)
                    and site_key.startswith(_EXTERNAL_SITE_PREFIX)
                    and len(site_key) == len(_EXTERNAL_SITE_PREFIX) + 64
                    and all(
                        character in "0123456789abcdef"
                        for character in site_key[len(_EXTERNAL_SITE_PREFIX):]
                    )
                )
                if not isinstance(site_key, str) or (
                    site_key not in self._site_rules and not external
                ):
                    raise ExecutionStateError("execution state references an unknown call site")
                if not isinstance(loop_path, list) or any(
                    isinstance(index, bool) or not isinstance(index, int) or index < 0
                    for index in loop_path
                ):
                    raise ExecutionStateError("execution state has an invalid loop path")
                if external and loop_path:
                    raise ExecutionStateError(
                        "external execution state must not have a loop path"
                    )
                if not external and not any(
                    len(rule.loops) + len(rule.helper_frames) == len(loop_path)
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

    def complete_external(self, token: tuple | None) -> None:
        """Persist success for a user-reauthorized call with no compiled rule."""
        if token is None or not str(token[0]).startswith(_EXTERNAL_SITE_PREFIX):
            raise ExecutionStateError("invalid external execution token")
        with self._attempt_lock:
            if self._attempts.get(token) != "started":
                raise ExecutionStateError("external execution attempt was not started")
            self.consume(token)
            self._attempts[token] = "succeeded"
            self._persist_execution_state()

    def attempt_state(self, token: tuple) -> str | None:
        """Return a durable attempt tombstone without exposing mutable state."""
        with self._attempt_lock:
            return self._attempts.get(token)

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

    def _argument_mismatches(
        self, tool: str, expected: list[Any], actual: list[Any]
    ) -> list[int]:
        """Operand positions that fail this enforcer's configured policy.

        The base relation is exact matching.  Deployment policy subclasses may
        remove explicitly declared positions without duplicating the loop,
        replay, ordering, guard, and error-handling logic in :meth:`check`.
        """
        return [
            index
            for index, (expected_value, actual_value) in enumerate(
                zip(expected, actual)
            )
            if not values_match(expected_value, actual_value)
        ]

    def site_complete(self, key: str) -> bool:
        """Whether every active authorization token at one call site is spent.

        This is used by the composite-plan coordinator.  Counting compiled
        rules is wrong because branch alternatives share one site and a bounded
        loop rule represents one token per observed tuple.
        """
        rules = self._site_rules.get(key, [])
        if not rules:
            return False

        def guard_active(rule: Rule) -> bool | None:
            ev = Evaluator(self.store, rule.lets)
            try:
                return all(bool(ev.eval(predicate)) for predicate in rule.guard)
            except Exception:  # noqa: BLE001 -- unresolved is not complete
                return None

        def loop_paths(rule: Rule, level: int, binds: dict, path: tuple) -> list[tuple] | None:
            if level == len(rule.loops):
                return [path]
            variable, iterable = rule.loops[level]
            try:
                collection = Evaluator(self.store, rule.lets, binds).eval(iterable)
            except Exception:  # noqa: BLE001 -- unresolved is not complete
                return None
            if not isinstance(collection, (list, tuple)):
                return None
            paths: list[tuple] = []
            for index, element in enumerate(collection):
                nested = loop_paths(
                    rule,
                    level + 1,
                    {**binds, variable: element},
                    path + (index,),
                )
                if nested is None:
                    return None
                paths.extend(nested)
            return paths

        for rule in rules:
            active = guard_active(rule)
            if active is None:
                return False
            if not active:
                continue
            if rule.helper_frames:
                return False
            paths = loop_paths(rule, 0, {}, ()) if rule.loops else [()]
            if paths is None:
                return False
            if any(not self._unavailable((key, path)) for path in paths):
                return False
        return True

    def completed_site_keys(self) -> set[str]:
        """Return compiled call-site keys whose active tokens are exhausted."""
        return {key for key in self._site_rules if self.site_complete(key)}

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

            # GrammarValidator currently rejects every helper-lambda tool
            # occurrence.  Keep this guard so a caller that bypasses validation
            # still fails closed instead of treating helper traversal as an
            # unordered explicit loop.
            if rule.helper_frames:
                reasons.append(
                    f"{rule.key}: helper-lambda tool traversal is not executable"
                )
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
                        if not self._argument_mismatches(tool, expected, args):
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
            mismatches = self._argument_mismatches(tool, expected, args)
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
        if (rule.loops or rule.helper_frames) and token is not None:
            try:
                path = token[1]
                expected_depth = len(rule.loops) + len(rule.helper_frames)
                if len(path) != expected_depth:
                    raise NotConcretizable("loop token depth mismatch")
                symbolic = occurrence_symbolic(rule.key, path)
            except (KeyError, NotConcretizable, TamperedEnvelopeError) as exc:
                raise NotConcretizable(
                    f"cannot bind quantified envelope key for {rule.key}: {exc}"
                ) from exc
        env = make_envelope(result, symbolic, signer, self._keyring())
        self.store.put(env)
        with self._attempt_lock:
            self.consume(token)
            if token is not None:
                self._attempts[token] = "succeeded"
                self._persist_execution_state()

    def _keyring(self):
        return self.store._keyring  # the store owns the shared keyring


def check_injection(enforcer: Enforcer, tool: str, args: list[Any]) -> Decision:
    """Check a single forced-injection call against the task's rules.

    A faithful injection test: the spurious call is offered to the enforcer
    with the benign task's envelope store already populated.  If *any* rule
    would authorize it the result is a false negative.
    """
    return enforcer.check(tool, args)
