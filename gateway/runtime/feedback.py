"""Value-free agent-facing denial feedback.

When the gateway denies a call it may return a reason to the agent. That text
enters the agent's model context, so it must never carry attacker-controlled
bytes -- a poisoned operand value (e.g. a recipient taken from an injected
email) could otherwise re-enter the context as a prompt-injection payload.

The guarantee here is by CONSTRUCTION, not by sanitization. Sanitizing a
poisoned value (stripping/escaping injection) can never be certain -- you
cannot reliably detect every payload in free text. Instead the feedback is
composed only from:

* a fixed template table (this source file -- trusted),
* a closed ``ReasonCode`` enum,
* validated schema identifiers (tool names -- safe charset, bounded length),
* integers (argument positions).

There is NO parameter through which a runtime data value can pass, so no
attacker-controlled byte can appear in the output. ``build_agent_feedback``
takes no operand value at all; ``test_feedback.py`` proves the result is
invariant to operand values.

The actual (possibly poisoned) value that a HUMAN must judge travels on a
separate user-facing channel (the confirmation dialog), never through this
function and never back into the agent's model context.
"""

from __future__ import annotations

import enum
import re

# Safe identifier charset: alphanumerics plus the separators real tool and
# parameter names use (``get_product_details``, ``<suite>:<tool>``). Bounded
# length. Anything outside this cannot appear in agent feedback.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")
_TOOL_PLACEHOLDER = "the requested tool"
_PARAM_PLACEHOLDER = "an argument"


class ReasonCode(enum.Enum):
    """Closed set of denial reasons. Each maps to a fixed, value-free template."""

    NO_RULE = "no_rule"
    NO_MATCHING_RULE = "no_matching_rule"
    ARITY_MISMATCH = "arity_mismatch"
    GUARD_UNSATISFIED = "guard_unsatisfied"
    OPERAND_OFF_SLICE = "operand_off_slice"
    RULE_CONSUMED = "rule_consumed"
    STAGE_INACTIVE = "stage_inactive"
    PLAN_COMPLETE = "plan_complete"
    SESSION_REJECTED = "session_rejected"
    PENDING_CONFIRMATION = "pending_confirmation"
    PRECHECK_DENIED = "precheck_denied"
    SIDE_CHANNEL_DENIED = "side_channel_denied"
    TOOL_ERROR = "tool_error"


_TEMPLATES: dict[ReasonCode, str] = {
    ReasonCode.NO_RULE:
        "Tool {tool} is not part of the approved task plan (default-deny).",
    ReasonCode.NO_MATCHING_RULE:
        "Tool {tool} is not authorized with these arguments by the approved plan.",
    ReasonCode.ARITY_MISMATCH:
        "Tool {tool} was called with the wrong number of arguments.",
    ReasonCode.GUARD_UNSATISFIED:
        "Tool {tool} requires an earlier step to complete first; "
        "do that step before retrying.",
    ReasonCode.OPERAND_OFF_SLICE:
        "Argument {param} of {tool} does not match the value approved for this task.",
    ReasonCode.RULE_CONSUMED:
        "Tool {tool} was already executed for this task step and cannot be repeated.",
    ReasonCode.STAGE_INACTIVE:
        "Tool {tool} belongs to a later task stage that is not active yet.",
    ReasonCode.PLAN_COMPLETE:
        "The approved task is complete; no further actions are authorized.",
    ReasonCode.SESSION_REJECTED:
        "No approved plan is active; the task was not authorized.",
    ReasonCode.PENDING_CONFIRMATION:
        "Argument {param} of {tool} is derived from untrusted data and needs "
        "user confirmation before it can proceed.",
    ReasonCode.PRECHECK_DENIED:
        "The plan for {tool} was rejected by a deterministic safety check.",
    ReasonCode.SIDE_CHANNEL_DENIED:
        "Tool {tool} is a raw side channel and is not permitted; route all "
        "outbound actions through approved, task-scoped tools.",
    ReasonCode.TOOL_ERROR:
        "Tool {tool} failed to execute.",
}


def is_safe_identifier(name: object) -> bool:
    """True iff ``name`` is a safe schema identifier (charset + length)."""
    return isinstance(name, str) and bool(_IDENTIFIER_RE.match(name))


def validate_identifier(name: str, *, kind: str = "identifier") -> str:
    """Registration-time gate: raise if a schema identifier is unsafe.

    Call when a suite/tool is registered so tool and parameter names can never
    carry an injection payload (relevant for tool surfaces reflected from an
    untrusted OpenAPI spec). Returns the name unchanged when it is safe.
    """
    if not is_safe_identifier(name):
        raise ValueError(
            f"unsafe {kind} {name!r}: must match {_IDENTIFIER_RE.pattern}"
        )
    return name


def assert_safe_identifiers(names: object, *, kind: str = "identifier") -> None:
    """Validate an iterable of identifiers; raise on the first unsafe one."""
    for name in names or []:
        validate_identifier(name, kind=kind)


def assert_safe_suite(suite: object) -> None:
    """Validate every tool and parameter name a suite exposes.

    The secondary defense: tool surfaces reflected from an
    untrusted OpenAPI spec could otherwise carry an injection payload in a tool
    or parameter name, which would then be echoed by agent feedback. Reject
    such a suite at registration time. Best-effort: silently returns if the
    object does not expose the expected accessors.
    """
    tool_names = getattr(suite, "tool_names", None)
    tool_params = getattr(suite, "tool_params", None)
    if callable(tool_names):
        assert_safe_identifiers(tool_names(), kind="tool name")
    if callable(tool_params):
        for tool, params in tool_params().items():
            validate_identifier(tool, kind="tool name")
            assert_safe_identifiers(params, kind="parameter name")


def _safe_tool(tool: str | None) -> str:
    # Defense in depth: even if registration validation was bypassed and a
    # malicious tool name slipped through, we substitute a placeholder rather
    # than echo it. The guarantee (no untrusted bytes) holds unconditionally.
    return tool if is_safe_identifier(tool) else _TOOL_PLACEHOLDER


def build_agent_feedback(
    code: ReasonCode,
    *,
    tool: str | None = None,
    param_index: int | None = None,
) -> str:
    """Compose value-free feedback safe to place in the agent's model context.

    Only a fixed template, the reason code, a validated tool identifier, and an
    integer argument position appear in the output. This function has NO
    parameter for an operand value or read result, so by construction no
    attacker-controlled byte can appear in what it returns.
    """
    template = _TEMPLATES[code]
    tool_str = _safe_tool(tool)
    if isinstance(param_index, int) and param_index >= 0:
        param_str = f"#{param_index}"
    else:
        param_str = _PARAM_PLACEHOLDER
    return template.format(tool=tool_str, param=param_str)


# Ordered (substring -> code) rules mapping the enforcer's free-text reasons to
# a ReasonCode. Only the CODE is used downstream; the free text (which may
# contain values, e.g. precheck messages) is discarded here and never reaches
# ``build_agent_feedback``. Misclassification only picks a different safe
# template -- it can never leak a value.
_CLASSIFY_RULES: tuple[tuple[str, ReasonCode], ...] = (
    ("already consumed", ReasonCode.RULE_CONSUMED),
    ("complete", ReasonCode.PLAN_COMPLETE),
    ("no active session", ReasonCode.SESSION_REJECTED),
    ("not active yet", ReasonCode.STAGE_INACTIVE),
    ("no rule exists", ReasonCode.NO_RULE),
    ("arity", ReasonCode.ARITY_MISMATCH),
    ("guard", ReasonCode.GUARD_UNSATISFIED),
    ("off-slice", ReasonCode.OPERAND_OFF_SLICE),
    ("operand unresolved", ReasonCode.OPERAND_OFF_SLICE),
    ("no rule authorizes", ReasonCode.NO_MATCHING_RULE),
    ("side channel", ReasonCode.SIDE_CHANNEL_DENIED),
    ("pending", ReasonCode.PENDING_CONFIRMATION),
    ("precheck", ReasonCode.PRECHECK_DENIED),
    ("tool execution error", ReasonCode.TOOL_ERROR),
    ("default-deny", ReasonCode.SESSION_REJECTED),
)


def classify_reason(reason: str) -> ReasonCode:
    """Map a free-text enforcer reason to a ReasonCode (value is discarded)."""
    r = (reason or "").lower()
    for needle, code in _CLASSIFY_RULES:
        if needle in r:
            return code
    return ReasonCode.NO_MATCHING_RULE
