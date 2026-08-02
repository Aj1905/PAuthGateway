"""Per-suite operand validation policy.

PAuth's enforcer checks every operand of every call against the slice's
arg expression. That is the right default for transactional operands
(IBAN, amount, recipient, date). But some tool surfaces -- notably
search queries, free-form message bodies, file paths -- carry operands
that are not meaningfully verifiable against a plan. The plan can say
"the agent will call ``search_emails``" but it cannot meaningfully
constrain the query string the user might want.

This module wraps ``pauth.Enforcer`` so a deployer can mark specific
``(tool_name, parameter_name)`` pairs as *free*. When the wrapped
enforcer sees a denial whose only off-slice operands sit at free
positions, it overrides the denial. Guard predicates and the other
operands are still verified normally.

The wildcards are configured at gateway-startup time (see
``gateway/serving/config.py``), not by the agent. The agent has no way to
extend the free-operand set.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from pauth.enforcer import Decision, Enforcer
from pauth.envelope import TamperedEnvelopeError
from pauth.evaluator import Evaluator, NotConcretizable, values_match
from pauth.rule_compiler import Rule


@dataclasses.dataclass(frozen=True)
class PolicySpec:
    """Per-tool free-operand declarations.

    ``free_positions[tool_name]`` is the set of arg indices that the
    enforcer should treat as "anything goes".
    """

    free_positions: dict[str, set[int]]

    @classmethod
    def from_param_names(
        cls,
        free_params: dict[str, list[str]],
        tool_params: dict[str, list[str]],
    ) -> "PolicySpec":
        """Build a PolicySpec from human-readable ``{tool: [param_name, ...]}``.

        Resolves each parameter name to its positional index using the
        tool's schema. Unknown tools or unknown parameter names raise
        ``ValueError`` -- typos in policy configs should fail loudly
        rather than silently leak operand checks.
        """
        resolved: dict[str, set[int]] = {}
        for tool, names in free_params.items():
            if tool not in tool_params:
                raise ValueError(
                    f"policy references unknown tool {tool!r} "
                    f"(known: {sorted(tool_params)})"
                )
            schema = tool_params[tool]
            positions: set[int] = set()
            for name in names:
                if name not in schema:
                    raise ValueError(
                        f"policy: parameter {name!r} not found on tool {tool!r} "
                        f"(schema: {schema})"
                    )
                positions.add(schema.index(name))
            resolved[tool] = positions
        return cls(free_positions=resolved)

    def is_free(self, tool: str, position: int) -> bool:
        return position in self.free_positions.get(tool, set())


class PolicyAwareEnforcer(Enforcer):
    """Enforcer that honours :class:`PolicySpec` free-operand declarations.

    Identical to :class:`pauth.enforcer.Enforcer` when ``policy`` is
    empty, so it is a drop-in replacement that the gateway can always
    use.
    """

    def __init__(
        self,
        rules: list[Rule],
        store,
        tool_signer,
        policy: PolicySpec,
        ordered_tools: set[str] | None = None,
    ) -> None:
        super().__init__(rules, store, tool_signer, ordered_tools=ordered_tools)
        self._policy = policy

    def check(self, tool: str, args: list[Any], *, live: bool = False) -> Decision:
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
            except Exception as exc:  # noqa: BLE001
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
                token = (rule.key, ())
                if self._unavailable(token):
                    reasons.append(f"{rule.key}: {self._unavailable_reason(token)}")
                    continue
            try:
                expected = [ev.eval(expr) for expr in rule.arg_exprs]
            except (NotConcretizable, TamperedEnvelopeError) as exc:
                reasons.append(f"{rule.key}: operand unresolved ({exc})")
                continue
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"{rule.key}: operand evaluation error ({type(exc).__name__}: {exc})")
                continue
            mismatches = [
                i for i, (e, a) in enumerate(zip(expected, args))
                if not values_match(e, a)
            ]
            # Drop free-operand positions: the policy declared the gateway
            # does not constrain them.
            effective = [i for i in mismatches if not self._policy.is_free(tool, i)]
            if effective:
                reasons.append(
                    f"{rule.key}: operand(s) {effective} off-slice "
                    f"(free positions ignored: {sorted(self._policy.free_positions.get(tool, set()))})"
                )
                continue
            return Decision(
                True, rule,
                f"authorized by rule {rule.key}"
                + (
                    f" (free operands: {sorted(set(mismatches) & self._policy.free_positions.get(tool, set()))})"
                    if mismatches else ""
                ),
                token=(rule.key, ()),
            )

        return Decision(
            False, None,
            "no rule authorizes this call (default-deny) :: " + " ; ".join(reasons)
        )
