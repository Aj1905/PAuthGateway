"""ToolExecutor -- the sandboxed plan executor for the generated code.

The ToolExecutor node runs the validated ``run`` function with every tool
call proxied through the :class:`~pauth.enforcer.Enforcer`.  Calls are
intercepted (call interception), checked (the authorization check), executed
when permitted, and their results recorded as envelopes (envelope signing).
The actual tool implementation is supplied by the caller as the
``tool_executor`` callable (the suite's tool adapter); this module owns only
the dispatch discipline around it.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable

from .enforcer import Decision, Enforcer
from .evaluator import EXEC_HELPERS, wrap


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
