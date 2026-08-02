"""Single-barrier confirmation: defer gated side-effects, ask ONCE, then commit.

The confirmation gate fires only on an UNTRUSTED-derived CONTROL operand, and
every untrusted value comes from a READ -- a side-effect's own result is trusted
(the gateway's action, envelope-signed from a trusted tool), so nothing gated
depends on a side-effect. Therefore every gated operand's concrete value is known
once the reads have run, BEFORE any side-effect. We exploit that:

  1. Execute run() with reads real; a gated side-effecting call is NOT executed
     -- it is recorded (with its concrete args) as a deferred action and returns
     None. Non-gated side-effects (all-trusted operands, e.g. create->share on a
     system id) run inline so their results still feed later calls.
  2. ONE barrier: present all deferred actions to the confirmer together.
  3. Commit the approved actions in program order.

FN=0 is unchanged: the enforcer still authorizes every call (control operands
verified) BEFORE it is deferred; batching moves only WHEN the human is asked, not
whether. A rejected action is never executed.

The rare case where a gated value genuinely depends on a side-effect (an
untrusted read keyed by a side-effect's result) is out of scope here and is the
one place a task boundary is legitimate; ``deferred_dependency`` flags it so the
caller can split rather than silently return a stale None.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable

from pauth.enforcer import CallEvent, Decision, Enforcer, _Denied
from pauth.evaluator import wrap

from gateway.runtime.confirmation import PendingConfirmation, is_side_effecting


class _DeferredRef:
    """Placeholder returned in place of a deferred side-effect's result. If it
    ever reaches a later call's args, a gated value depended on a not-yet-run
    side-effect -- the rare case that legitimately needs a task split."""

    __slots__ = ()


_DEFERRED = _DeferredRef()


@dataclasses.dataclass
class DeferredAction:
    tool: str
    args: list[Any]
    gated_indices: list[int]
    sources: tuple[str, ...]
    approved: bool | None = None      # None until the barrier decides
    result: Any = None


@dataclasses.dataclass
class BatchedReport:
    events: list[CallEvent]
    deferred: list[DeferredAction]
    tool_errors: list[str]
    crashed: str | None
    deferred_dependency: bool = False   # a deferred None was read downstream


def execute_with_batched_confirmation(
    code: str,
    enforcer: Enforcer,
    tool_params: dict[str, list[str]],
    tool_executor: Callable[[str, dict[str, Any]], Any],
    *,
    taint_map: dict[tuple[str, int], tuple[str, ...]],
    docs: dict[str, Any],
    confirmer: Any,
    build_pending: Callable[[DeferredAction], PendingConfirmation] | None = None,
) -> BatchedReport:
    """Run ``code`` deferring gated side-effects to a single barrier confirmation.

    ``taint_map`` is ``broad_taint_map(code, docs, source_trust)`` --
    ``(tool, param_index) -> sources`` for every untrusted-derived operand.
    ``confirmer.confirm(pending)`` decides each deferred action at the barrier.
    """
    gated_by_tool: dict[str, list[int]] = {}
    for (tool, idx), _srcs in taint_map.items():
        gated_by_tool.setdefault(tool, []).append(idx)

    events: list[CallEvent] = []
    deferred: list[DeferredAction] = []
    tool_errors: list[str] = []
    crashed: str | None = None
    saw_deferred_none = {"hit": False}

    def make_wrapper(name: str) -> Callable[..., Any]:
        def wrapper(*args: Any) -> Any:
            arglist = list(args)
            # a deferred action's placeholder reaching a later call means a gated
            # value depends on a not-yet-run side-effect -> the split case.
            if any(a is _DEFERRED for a in arglist):
                saw_deferred_none["hit"] = True
            decision = enforcer.check(name, arglist, live=True)
            event = CallEvent(name, arglist, decision)
            events.append(event)
            if not decision.permit:
                raise _Denied(event)
            gated = gated_by_tool.get(name, []) if is_side_effecting(name) else []
            if gated:
                srcs = tuple(sorted({s for i in gated for s in taint_map.get((name, i), ())}))
                deferred.append(DeferredAction(name, arglist, gated, srcs))
                return _DEFERRED  # DEFER: not executed until the barrier approves it
            params = tool_params.get(name, [])
            if not enforcer.begin(decision.token):
                raise _Denied(CallEvent(
                    name,
                    arglist,
                    Decision(
                        False,
                        decision.rule,
                        "execution attempt already exists (replay blocked)",
                        decision.token,
                    ),
                ))
            try:
                result = tool_executor(name, dict(zip(params, arglist)))
            except Exception as exc:  # noqa: BLE001 -- tool-level failure
                try:
                    enforcer.mark_indeterminate(decision.token)
                except Exception:
                    pass
                tool_errors.append(f"{name}: {type(exc).__name__}: {exc}")
                return None
            result = wrap(result)
            assert decision.rule is not None
            enforcer.record(decision.rule, result, decision.token)
            return result

        return wrapper

    namespace: dict[str, Any] = {name: make_wrapper(name) for name in tool_params}
    from pauth.evaluator import EXEC_HELPERS
    namespace.update(EXEC_HELPERS)
    namespace["__builtins__"] = {}

    try:
        exec(compile(code, "<pauth-batched>", "exec"), namespace)  # noqa: S102
        run = namespace.get("run")
        if not callable(run):
            crashed = "generated code defines no callable 'run'"
        else:
            run()
    except _Denied:
        pass
    except Exception as exc:  # noqa: BLE001 -- generated-code bug, not a denial
        crashed = f"{type(exc).__name__}: {exc}"

    # ---- barrier: one confirmation over all deferred actions ----
    for act in deferred:
        pending = (build_pending(act) if build_pending
                   else PendingConfirmation(
                       f"c{deferred.index(act)}", act.tool, act.gated_indices[0],
                       tool_params.get(act.tool, ["?"])[act.gated_indices[0]]
                       if act.gated_indices[0] < len(tool_params.get(act.tool, [])) else "?",
                       act.args[act.gated_indices[0]], source=act.sources))
        act.approved = bool(confirmer.confirm(pending))

    # ---- handover: the barrier has decided everything; confirm() is never
    # called again below, so a human-facing confirmer may announce that no
    # further confirmation will occur before the unattended commit begins.
    announce = getattr(confirmer, "announce_handover", None)
    if deferred and callable(announce):
        approved_count = sum(1 for act in deferred if act.approved)
        announce(approved_count, len(deferred) - approved_count)

    # ---- commit: execute approved actions in program order ----
    for act in deferred:
        if not act.approved:
            continue
        decision = enforcer.check(act.tool, act.args, live=True)
        if not decision.permit:
            act.approved = False
            continue
        params = tool_params.get(act.tool, [])
        if not enforcer.begin(decision.token):
            act.approved = False
            continue
        try:
            raw = tool_executor(act.tool, dict(zip(params, act.args)))
        except Exception as exc:  # noqa: BLE001
            try:
                enforcer.mark_indeterminate(decision.token)
            except Exception:
                pass
            tool_errors.append(f"{act.tool}: {type(exc).__name__}: {exc}")
            continue
        act.result = raw
        assert decision.rule is not None
        enforcer.record(decision.rule, wrap(raw), decision.token)

    return BatchedReport(
        events=events, deferred=deferred, tool_errors=tool_errors,
        crashed=crashed, deferred_dependency=saw_deferred_none["hit"],
    )
